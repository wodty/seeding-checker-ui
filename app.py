#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Seeding Checker UI - 基于 qBittorrent 的 NAS 冗余/缺失文件检查工具
- Web UI：浏览器访问 http://<host>:8000
- 冗余文件：NAS 有文件 && qB 无任何种子占用 → 可删除（移回收站/彻底删除）
- 缺失文件：qB 有种子 && NAS 文件丢失 → 可移除种子

启动：
  python app.py                  # 正常模式（读 config.ini）
  python app.py --demo           # 演示模式（生成模拟数据，无需真实 qB）
  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os
import sys
import logging
import configparser
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import checker
import deleter
import re as _re
from qb_client import QBClient, QBError, normalize_path
from tr_client import TRClient, TRError

# ---------------- 日志 ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("seeding_checker_ui")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = Path(os.environ.get("CONFIG_FILE", BASE_DIR / "config.ini"))

# 版本号：用于确认容器内运行的是最新代码（旧版本没有 /api/version 端点）
APP_VERSION = "1.4.1"

DEFAULT_CONFIG = """[general]
# NAS 目录（逗号分隔）
nas_directories = /path/to/dir1, /path/to/dir2
# 排除目录（逗号分隔，留空则不排除）
exclude_directories = 
# 最小文件大小阈值(MB)，0 = 全部检查
size_threshold = 0
# 忽略软链接/硬链接
ignore_links = true
# 回收目录（删除时选择"移回收站"的文件会到这里，可恢复）
trash_dir = ./trash

[downloader]
# 启用的下载器实例 ID（逗号分隔），每个实例对应下方一个同名配置节
# 支持 qBittorrent 与 Transmission，可同时配置多个实例
enabled_clients = qb1

[qb1]
# 下载器类型: qbittorrent | transmission
type = qbittorrent
host = 192.168.1.100
port = 8080
username = admin
password = admin
# 该下载器的路径映射：下载器内路径=NAS真实路径（逗号分隔，如 /downloads=/vol1/data）
path_mappings = 
# qBittorrent BT_backup 目录（可选，配置后可一并清理 .torrent 备份文件）
torrent_backup_dir = 
"""

# ---------------- 配置 ----------------
_lock = threading.Lock()
_last_result = None  # 最近一次扫描结果


def load_config():
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    if not CONFIG_PATH.exists():
        cfg.read_string(DEFAULT_CONFIG)
        CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")
        logger.info("已生成默认配置文件: %s", CONFIG_PATH)
    else:
        cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg


def save_config(cfg):
    with _lock:
        CONFIG_PATH.write_text("\n".join(
            f"[{s}]" + "\n" + "\n".join(f"{k} = {v}" for k, v in cfg.items(s))
            for s in cfg.sections()
        ) + "\n", encoding="utf-8")


def config_to_dict(cfg):
    return {s: dict(cfg.items(s)) for s in cfg.sections()}


def resolve_trash_dir(cfg):
    raw = cfg.get("general", "trash_dir", fallback="./trash").strip()
    if not os.path.isabs(raw):
        raw = str(BASE_DIR / raw)
    return raw


def load_clients_config(cfg):
    """
    解析下载器实例列表（新格式：[downloader].enabled_clients + 同名配置节）。
    兼容旧格式：无 [downloader] 但存在 [qb] 节时，视为单个 qBittorrent 实例。
    :return: list[dict] {id, type, host, port, username, password, path_mappings, torrent_backup_dir}
    """
    clients = []
    enabled = cfg.get("downloader", "enabled_clients", fallback="").strip() if cfg.has_section("downloader") else ""
    if enabled:
        for cid in _re.split(r"[，,]", enabled):
            cid = cid.strip()
            if not cid or not cfg.has_section(cid):
                logger.warning("下载器实例 %s 在 enabled_clients 中但缺少配置节，跳过", cid)
                continue
            clients.append(dict(cfg.items(cid), id=cid))
    elif cfg.has_section("qb"):
        # 旧版单 qB 配置自动迁移（不写回文件，读时生效）。
        # 实例 id 沿用 "qb"（保存时 [downloader]+[qb] 新格式落盘，qb 是合法 id 而非保留字）
        clients.append(dict(cfg.items("qb"), id="qb", type="qbittorrent"))
        logger.info("检测到旧版 [qb] 单下载器配置，按实例 qb 处理（保存后自动迁移为新格式）")
    return clients


CLIENT_FIELDS = ("type", "host", "port", "username", "password",
                 "path_mappings", "torrent_backup_dir")


def save_clients_config(cfg, clients):
    """把客户端列表写回配置文件（移除旧 [qb] 节与已删除的实例节）"""
    new_ids = []
    for c in clients:
        cid = str(c.get("id", "")).strip()
        if not cid or not _re.fullmatch(r"[A-Za-z0-9_]+", cid):
            raise HTTPException(status_code=400, detail=f"无效的下载器名称: {cid!r}（仅限字母/数字/下划线）")
        if cid in ("general", "downloader"):
            raise HTTPException(status_code=400, detail=f"下载器名称不能是保留字: {cid}")
        if cid in new_ids:
            raise HTTPException(status_code=400, detail=f"下载器名称重复: {cid}")
        new_ids.append(cid)
    old_ids = [c["id"] for c in load_clients_config(cfg)]
    for old in set(old_ids) - set(new_ids):
        if cfg.has_section(old):
            cfg.remove_section(old)
    if cfg.has_section("qb"):
        cfg.remove_section("qb")  # 旧格式迁移
    for c in clients:
        cid = c["id"]
        if not cfg.has_section(cid):
            cfg.add_section(cid)
        else:
            cfg.remove_section(cid)
            cfg.add_section(cid)
        for f in CLIENT_FIELDS:
            cfg.set(cid, f, str(c.get(f, "")).strip())
    if not cfg.has_section("downloader"):
        cfg.add_section("downloader")
    cfg.set("downloader", "enabled_clients", ",".join(new_ids))


def build_client(c):
    """按 type 构建下载器客户端实例"""
    ctype = str(c.get("type", "qbittorrent")).strip().lower()
    if ctype == "transmission":
        return TRClient(
            host=c.get("host", "127.0.0.1"),
            port=c.get("port", "9091"),
            username=c.get("username", ""),
            password=c.get("password", ""),
            path_mappings=c.get("path_mappings", ""),
        )
    return QBClient(
        host=c.get("host", "127.0.0.1"),
        port=c.get("port", "8080"),
        username=c.get("username", "admin"),
        password=c.get("password", ""),
        path_mappings=c.get("path_mappings", ""),
    )


# ---------------- Demo 模式 ----------------
_demo = "--demo" in sys.argv


def build_demo_environment():
    """生成模拟 NAS 文件与模拟种子，便于无真实环境演示 UI（小体积文件，避免占用真实空间）"""
    root = Path(tempfile.mkdtemp(prefix="sc_demo_nas_"))
    MB = 1024 * 1024
    for name, size in [("movie1.mkv", 5 * MB), ("movie2.mkv", 6 * MB),
                       ("album.mp3", 2 * MB), ("doc.pdf", MB),
                       ("corrupted.mkv", 2 * MB)]:
        p = root / name
        p.write_bytes(b"\0" * size)
    (root / "redundant_old_show").mkdir()
    (root / "redundant_old_show" / "ep01.mkv").write_bytes(b"\0" * (3 * MB))
    return root


_demo_root = build_demo_environment() if _demo else None


class DemoQB:
    """模拟 qBittorrent 客户端（demo 模式种子状态真实可删）"""
    def __init__(self, root):
        self.root = Path(root)
        self.logged_in = True
        self._deleted = set()      # 已移除的种子 hash
        self._torrent_files = {}   # hash -> NAS 路径（删除本地文件用）

    def collect_seeding_info(self):
        root = self.root
        seeding_paths = set()
        seeding_torrents = []
        self._torrent_files = {}
        # 种子A：movie1.mkv 正常做种（文件存在）
        # 种子B：movie2.mkv 正常做种（文件存在）
        # 种子C：lost.mkv 文件丢失（异常 missingFiles）
        # 种子D：corrupted.mkv 文件存在但状态 error（异常）
        for i, (name, fname, state) in enumerate([
            ("Demo Movie Pack", "movie1.mkv", "uploading"),
            ("Demo Album", "album.mp3", "stalledUP"),
            ("Demo Doc", "doc.pdf", "pausedUP"),
            ("Lost Movie", "lost.mkv", "missingFiles"),
            ("Corrupted File", "corrupted.mkv", "error"),
        ], 1):
            torrent_hash = f"demo{str(i).zfill(8)}" + "0" * 24
            if torrent_hash in self._deleted:
                continue  # 已移除的种子不再出现
            full = root / fname
            exists = full.exists()
            nas = str(full).replace("\\", "/") if exists else f"{root}/missing_dir/{fname}"
            nas = nas.replace("\\", "/")
            seeding_paths.add(nas)
            self._torrent_files[torrent_hash] = nas
            seeding_torrents.append({
                "torrent_name": name,
                "torrent_hash": torrent_hash,
                "torrent_state": state,
                "state_label": {"uploading": "做种中", "stalledUP": "做种中(停滞)",
                                "pausedUP": "已暂停", "missingFiles": "文件丢失",
                                "error": "错误"}[state],
                "progress": 1.0,
                "save_path": str(root).replace("\\", "/"),
                "original_path": str(full).replace("\\", "/"),
                "nas_path": nas,
                "file_name": fname,
                "file_size": full.stat().st_size if exists else 0,
                "client": "qBittorrent",
            })
        return seeding_paths, seeding_torrents

    def delete_torrents(self, hashes, delete_local_files=False):
        """真实移除 demo 种子；delete_local_files=True 时连带删除 NAS 上的本地文件"""
        for h in hashes:
            self._deleted.add(h)
        result = {"ok": len(hashes), "failed": 0}
        if delete_local_files:
            paths = [self._torrent_files.get(h) for h in hashes]
            paths = [p for p in paths if p and os.path.exists(p)]
            if paths:
                try:
                    r = deleter.delete_files(paths, mode="permanent", trash_dir=None)
                    result["local_files_deleted"] = r.get("ok", 0)
                    result["freed"] = r.get("freed", 0)
                    result["failed"] = r.get("failed", 0)
                except Exception as e:
                    logger.warning("[demo] 删除种子本地文件失败: %s", e)
                    result["local_files_deleted"] = 0
                    result["freed"] = 0
        logger.info("[demo] 移除种子: %s (delete_local_files=%s)", hashes, delete_local_files)
        return result


_demo_qb = None  # demo 客户端单例：保持种子删除状态在进程内持久


def get_data_sources(cfg):
    """返回 {(client_id, client, client_config)} 列表；demo 模式返回单个模拟客户端"""
    global _demo_qb
    if _demo:
        if _demo_qb is None:
            _demo_qb = DemoQB(_demo_root)
        return [("demo", _demo_qb, {"id": "demo", "type": "qbittorrent"})]
    clients = []
    for c in load_clients_config(cfg):
        clients.append((c["id"], build_client(c), c))
    if not clients:
        raise HTTPException(status_code=400,
                            detail="未配置任何下载器，请先在「下载器配置」中添加 qBittorrent / Transmission 实例")
    return clients


# ---------------- FastAPI ----------------

@asynccontextmanager
async def lifespan(app):
    logger.info("Seeding Checker UI 启动 (demo=%s)", _demo)
    yield


app = FastAPI(title="Seeding Checker UI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ScanResponse(BaseModel):
    summary: dict
    redundant: list
    missing: list
    abnormal: list


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/version")
def api_version():
    return {"version": APP_VERSION}


@app.get("/api/config")
def api_get_config():
    cfg = load_config()
    general = dict(cfg.items("general")) if cfg.has_section("general") else {}
    clients = load_clients_config(cfg)
    return {"general": general, "clients": clients}


@app.post("/api/config")
def api_save_config(payload: dict):
    cfg = load_config()
    # general 段
    for k, v in (payload.get("general") or {}).items():
        if not cfg.has_section("general"):
            cfg.add_section("general")
        cfg.set("general", k, str(v))
    # 下载器实例列表
    if "clients" in payload:
        save_clients_config(cfg, payload.get("clients") or [])
    save_config(cfg)
    return {"ok": True}


@app.post("/api/clients/test")
def api_clients_test(payload: dict = None):
    """逐个测试下载器连接；可传入客户端列表（未保存的配置也能测）"""
    if _demo:
        return {"ok": True, "results": [{"client": "demo", "ok": True, "message": "演示模式，模拟连接成功"}],
                "message": "演示模式，模拟连接成功"}
    if payload and payload.get("clients"):
        clients = payload["clients"]
    else:
        cfg = load_config()
        clients = load_clients_config(cfg)
    if not clients:
        raise HTTPException(status_code=400, detail="未配置任何下载器")
    results = []
    for c in clients:
        try:
            client = build_client(c)
            client.login()
            torrents = client.get_torrents()
            results.append({"client": c.get("id", "?"), "ok": True,
                            "message": f"连接成功，共 {len(torrents)} 个种子"})
        except Exception as e:
            results.append({"client": c.get("id", "?"), "ok": False, "message": f"连接失败: {e}"})
    ok = all(r["ok"] for r in results)
    msg = "；".join(f"{r['client']}: {r['message']}" for r in results)
    return {"ok": ok, "results": results, "message": msg}


@app.post("/api/qb/test")
def api_qb_test(payload: dict = None):
    """兼容旧前端：等价于测试全部下载器"""
    return api_clients_test(payload)


@app.post("/api/qb/test")
def api_qb_test(payload: dict = None):
    if _demo:
        return {"ok": True, "message": "演示模式，模拟连接成功"}
    cfg = load_config()
    qb = build_qb_client(cfg)
    try:
        qb.login()
        torrents = qb.get_torrents()
        return {"ok": True, "torrents": len(torrents), "message": f"连接成功，共 {len(torrents)} 个种子"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"连接失败: {e}")


@app.post("/api/scan", response_model=ScanResponse)
def api_scan():
    global _last_result
    cfg = load_config()
    src = get_data_sources(cfg)

    # 1. 采集所有下载器的种子（单实例失败不阻断其他实例）
    seeding_paths = set()
    seeding_torrents = []
    hash_client = {}      # torrent_hash -> client_id（删除种子时路由）
    client_errors = []    # 连接失败的实例诊断信息
    for cid, client, cconf in src:
        try:
            paths, torrents = client.collect_seeding_info()
        except (QBError, TRError) as e:
            client_errors.append({"client": cid, "error": f"连接失败: {e}"})
            logger.error("下载器 %s 采集失败: %s", cid, e)
            continue
        except Exception as e:
            client_errors.append({"client": cid, "error": f"采集种子失败: {e}"})
            logger.error("下载器 %s 采集异常: %s", cid, e)
            continue
        seeding_paths |= paths
        for t in torrents:
            t["client_id"] = cid
            hash_client[t.get("torrent_hash", "")] = cid
        seeding_torrents.extend(torrents)
        logger.info("下载器 %s(%s): 种子文件 %d 条", cid, cconf.get("type"), len(torrents))
    if client_errors and not seeding_torrents:
        raise HTTPException(status_code=400,
                            detail="所有下载器均连接失败：" +
                                   "；".join(f"{e['client']}: {e['error']}" for e in client_errors))

    # 2. 扫描 NAS
    if _demo:
        nas_dirs = [str(_demo_root)]
        nas_files, nas_stats = checker.scan_nas(nas_dirs, size_threshold_mb=0)
    else:
        nas_dirs = [d.strip() for d in cfg.get("general", "nas_directories", fallback="").split(",") if d.strip()]
        if not nas_dirs:
            raise HTTPException(status_code=400, detail="请先配置 nas_directories")
        nas_files, nas_stats = checker.scan_nas(
            nas_dirs,
            exclude_dirs=cfg.get("general", "exclude_directories", fallback=""),
            size_threshold_mb=int(cfg.get("general", "size_threshold", fallback="0") or 0),
            ignore_links=cfg.getboolean("general", "ignore_links", fallback=True),
        )

    # 3. 检测
    logger.info("扫描配置: nas_dirs=%s, ignore_links=%s, 下载器=%s",
                nas_dirs, cfg.getboolean("general", "ignore_links", fallback=True),
                {cid: cconf.get("path_mappings", "") for cid, _, cconf in src})
    logger.info("NAS 路径样例(前10): %s", [f["path"].replace("\\", "/") for f in nas_files[:10]])

    # 3.1 路径匹配诊断：用于排查 path_mappings 配置错误导致的误判
    nas_path_set = set(f["path"].replace("\\", "/") for f in nas_files)
    matched_files = len(nas_path_set & seeding_paths)
    match_rate = round(matched_files * 100.0 / len(nas_path_set), 1) if nas_path_set else 0.0
    # 下载器全部 save_path、所属实例及是否在 NAS 扫描范围内（范围外路径不会出现在扫描结果里）
    nas_dirs_norm = [d.replace("\\", "/").rstrip("/") for d in nas_dirs]
    path_clients = {}
    for t in seeding_torrents:
        p = (t.get("save_path") or "").replace("\\", "/").rstrip("/")
        if p:
            path_clients.setdefault(p, set()).add(t.get("client_id", "?"))
    qb_save_paths = [{
        "path": p,
        "client": ",".join(sorted(cs)),
        "in_scope": any(p == d or p.startswith(d + "/") for d in nas_dirs_norm),
    } for p, cs in sorted(path_clients.items())]
    diag = {
        "matched_files": matched_files,
        "match_rate": match_rate,
        "qb_save_paths": qb_save_paths,
        "client_errors": client_errors,
        "nas_samples": sorted(p for p in nas_path_set if p not in seeding_paths)[:5],
        "qb_samples": sorted(p for p in seeding_paths if p not in nas_path_set)[:5],
        "suggest_mappings": checker.suggest_path_mappings(nas_files, seeding_torrents),
    }
    logger.info("路径匹配诊断: %d/%d 个 NAS 文件匹配 qB 种子 (%.1f%%)",
                matched_files, len(nas_path_set), match_rate)

    redundant = checker.detect_redundant(nas_files, seeding_paths)
    missing = checker.detect_missing(seeding_torrents, [f["path"] for f in nas_files], nas_dirs)
    abnormal = checker.detect_abnormal(seeding_torrents)

    # 4. 汇总
    redundant_total = sum(f["size"] for f in redundant)
    missing_total = sum(m["file_size"] for m in missing)
    abnormal_total = sum(a["file_size"] for a in abnormal)
    summary = {
        "nas_files": len(nas_files),
        "nas_total": checker.human_size(sum(f["size"] for f in nas_files)),
        "nas_dirs": nas_stats.get("dirs", 0),
        "nas_links_skipped": nas_stats.get("links_skipped", 0),
        "nas_symlinks_skipped": nas_stats.get("symlinks_skipped", 0),
        "nas_hardlinks_skipped": nas_stats.get("hardlinks_skipped", 0),
        "nas_errors": nas_stats.get("errors", 0),
        "version": APP_VERSION,
        "diag": diag,
        "torrents": len(set(t["torrent_hash"] for t in seeding_torrents)),
        "seeding_files": len(seeding_paths),
        "redundant": len(redundant),
        "redundant_total": checker.human_size(redundant_total),
        "missing": len(missing),
        "missing_total": checker.human_size(missing_total),
        "missing_torrents": len(set(m["torrent_hash"] for m in missing)),
        "abnormal": len(abnormal),
        "abnormal_total": checker.human_size(abnormal_total),
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "demo": _demo,
    }
    _last_result = {"redundant": redundant, "missing": missing, "abnormal": abnormal,
                    "summary": summary, "hash_client": hash_client}
    logger.info("扫描完成: 冗余 %d 个(%.1f), 缺失 %d 个, 异常 %d 个",
                len(redundant), redundant_total / 1024**3, len(missing), len(abnormal))
    return ScanResponse(summary=summary, redundant=redundant, missing=missing, abnormal=abnormal)


class DeleteFilesRequest(BaseModel):
    paths: list[str]
    mode: str = "trash"  # trash | permanent


@app.post("/api/delete-files")
def api_delete_files(req: DeleteFilesRequest):
    cfg = load_config()
    if _demo:
        # 演示模式：真实删除 demo 目录下的文件（可安全执行）
        trash_dir = None if req.mode == "permanent" else resolve_trash_dir(cfg)
        result = deleter.delete_files(req.paths, mode=req.mode, trash_dir=trash_dir)
        return result
    if req.mode not in ("trash", "permanent"):
        raise HTTPException(status_code=400, detail="mode 必须是 trash 或 permanent")
    # 安全校验：只允许删除 nas_directories 范围内的文件
    nas_dirs = [d.strip().replace("\\", "/").rstrip("/") for d in cfg.get("general", "nas_directories", fallback="").split(",") if d.strip()]
    for p in req.paths:
        norm = os.path.normpath(p).replace("\\", "/")
        if not any(norm.startswith(d + "/") for d in nas_dirs):
            raise HTTPException(status_code=400, detail=f"拒绝删除监控范围外的文件: {p}")
    trash_dir = None if req.mode == "permanent" else resolve_trash_dir(cfg)
    result = deleter.delete_files(req.paths, mode=req.mode, trash_dir=trash_dir)
    return result


class DeleteTorrentsRequest(BaseModel):
    hashes: list[str]
    delete_local_files: bool = False      # 同时删除 NAS 本地数据文件（危险）
    delete_torrent_backup: bool = False   # 同时清理 qB BT_backup 里的 .torrent 备份
    delete_torrent_file: bool = False     # 兼容旧版前端：等价于 delete_torrent_backup


@app.post("/api/delete-torrents")
def api_delete_torrents(req: DeleteTorrentsRequest):
    cfg = load_config()
    delete_backup = req.delete_torrent_backup or req.delete_torrent_file
    if not _demo and req.delete_local_files:
        # 安全校验：连带删除本地文件时，只允许删除 nas_directories 范围内的文件
        nas_dirs = [d.strip().replace("\\", "/").rstrip("/") for d in
                    cfg.get("general", "nas_directories", fallback="").split(",") if d.strip()]
        targets = _last_result.get("missing", []) + _last_result.get("abnormal", []) if _last_result else []
        for m in targets:
            if m["torrent_hash"] in req.hashes and m.get("nas_path"):
                norm = os.path.normpath(m["nas_path"]).replace("\\", "/")
                if not any(norm.startswith(d + "/") for d in nas_dirs):
                    raise HTTPException(status_code=400,
                                        detail=f"拒绝删除监控范围外的文件: {m['nas_path']}")
    src = get_data_sources(cfg)
    client_map = {cid: client for cid, client, _ in src}
    conf_map = {cid: cconf for cid, _, cconf in src}
    # 路由：hash -> 所属下载器（来自最近一次扫描；未知 hash 时若仅有一个实例则兜底）
    hash_client = _last_result.get("hash_client", {}) if _last_result else {}
    groups = {}
    for h in req.hashes:
        cid = hash_client.get(h) or (next(iter(client_map)) if len(client_map) == 1 else None)
        if cid is None:
            raise HTTPException(status_code=400,
                                detail=f"种子 {h} 无法确定所属下载器（多实例需先扫描一次）")
        groups.setdefault(cid, []).append(h)

    result = {"ok": 0, "failed": []}
    for cid, hashes in groups.items():
        try:
            r = client_map[cid].delete_torrents(hashes, delete_local_files=req.delete_local_files)
            result["ok"] += r.get("ok", 0)
            result["failed"].extend(r.get("failed", []))
        except Exception as e:
            logger.error("下载器 %s 删除种子失败: %s", cid, e)
            result["failed"].extend({"path": h, "reason": f"{cid}: {e}"} for h in hashes)
    # 清理 .torrent 备份（可选，独立于本地文件删除；仅 qBittorrent 实例支持）
    if delete_backup and not _demo:
        cleaned = 0
        for cid, hashes in groups.items():
            backup_dir = (conf_map.get(cid, {}).get("torrent_backup_dir") or "").strip()
            if backup_dir:
                cleaned += deleter.delete_torrent_backups(hashes, backup_dir)
        if cleaned:
            result["torrent_backups_cleaned"] = cleaned
    return result


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
