#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qBittorrent Web API v2 客户端
- 登录会话（Cookie 保持）
- 拉取全部种子（不过滤状态：只要 qB 中存在该种子，NAS 文件即视为"被占用"）
- 逐种子获取文件清单
- 删除种子（可选删除 .torrent 备份文件）
"""
import os
import re
import logging
import requests

logger = logging.getLogger("qb_client")


class QBError(Exception):
    pass


# 种子状态中文映射（仅用于展示，不影响占用判定）
STATE_LABELS = {
    "error": "错误",
    "missingFiles": "文件丢失",
    "uploading": "做种中",
    "stalledUP": "做种中(停滞)",
    "forcedUP": "做种中(强制)",
    "queuedUP": "排队做种",
    "checkingUP": "校验做种",
    "downloading": "下载中",
    "stalledDL": "下载中(停滞)",
    "forcedDL": "下载中(强制)",
    "queuedDL": "排队下载",
    "checkingDL": "校验下载",
    "checkingResumeData": "校验数据",
    "pausedDL": "已暂停",
    "pausedUP": "已暂停",
    "metaDL": "获取元数据",
    "allocating": "分配空间",
    "unknown": "未知",
}


def normalize_path(p: str) -> str:
    """统一路径分隔符并规范化"""
    if not p:
        return ""
    p = p.replace("\\", "/")
    return os.path.normpath(p)


def sanitize_segment(name: str) -> str:
    """文件名中的特殊字符替换为 _（与缺失检测的替代路径校验保持一致）"""
    return re.sub(r'[^a-zA-Z0-9/\._\-]', '_', name)


class QBClient:
    def __init__(self, host, port, username, password, path_mappings=""):
        # 兼容 host 配置带/不带协议前缀（如 http://qb.example.com 或 qb.example.com）
        host = str(host or "").strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        netloc = host.split("://", 1)[1] if "://" in host else host
        self.base = host if ":" in netloc else f"{host}:{port}"
        self.username = username
        self.password = password
        self.session = requests.Session()
        # 同时设置 UA + Referer：qB 4.4+ 默认开启 Web API CSRF 校验，
        # 登录请求的 Referer 必须与请求 host 一致，否则会返回 204 No Content
        self.session.headers.update({
            "User-Agent": "seeding-checker-ui/1.0",
            "Referer": f"{self.base}/",
        })
        self._mappings = self._parse_mappings(path_mappings)
        self.logged_in = False

    # ---------- 内部工具 ----------

    @staticmethod
    def _parse_mappings(path_mappings):
        """解析 容器路径=NAS路径, 逗号分隔（兼容中英文逗号）"""
        mappings = []
        if not path_mappings:
            return mappings
        for item in re.split(r"[，,]", str(path_mappings)):
            item = item.strip()
            if not item or "=" not in item:
                continue
            src, dst = item.split("=", 1)
            src, dst = src.strip().strip("/"), dst.strip()
            if not src or not dst:
                continue
            mappings.append((src, dst))
        return mappings

    def apply_path_mapping(self, file_path):
        """将下载器容器内路径映射为 NAS 真实路径；无匹配则原样返回"""
        if not file_path:
            return file_path
        norm = file_path.replace("\\", "/").lstrip("/")
        for src, dst in self._mappings:
            src_norm = src.lstrip("/")
            if norm == src_norm:
                return dst.rstrip("/")
            if norm.startswith(src_norm + "/"):
                rel = norm[len(src_norm) + 1:]
                return os.path.join(dst.rstrip("/"), rel).replace("\\", "/")
        return file_path

    # ---------- 连接 ----------

    def login(self):
        resp = self.session.post(
            f"{self.base}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            timeout=10,
        )
        # qB 开启「本地/子网免认证」时，login 接口不创建会话，返回 204 No Content。
        # 此时 API 本身可直接访问：探测 /api/v2/app/version，200 即视为已登录。
        if resp.status_code == 204:
            check = self.session.get(f"{self.base}/api/v2/app/version", timeout=10)
            if check.status_code == 200:
                self.logged_in = True
                logger.info(
                    "qBittorrent 处于免认证模式（login 返回 204），API 直接可用: %s",
                    self.base,
                )
                return True
            raise QBError(
                f"登录返回 HTTP 204，且 API 探测失败（HTTP {check.status_code}）。"
                "请检查 qB Web UI 是否开启了「本地/子网免认证」，或端口是否正确"
            )
        if resp.status_code != 200:
            raise QBError(f"登录请求失败 HTTP {resp.status_code}")
        body = resp.text.strip()
        if body != "Ok.":
            raise QBError(f"qBittorrent 登录失败：{body}")
        self.logged_in = True
        logger.info("qBittorrent 登录成功: %s", self.base)
        return True

    def _get(self, path, **params):
        if not self.logged_in:
            self.login()
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp

    def _post(self, path, data=None, params=None):
        if not self.logged_in:
            self.login()
        resp = self.session.post(f"{self.base}{path}", data=data, params=params, timeout=30)
        resp.raise_for_status()
        return resp

    # ---------- 数据 ----------

    def get_torrents(self):
        """获取全部种子（不过滤状态）"""
        resp = self._get("/api/v2/torrents/info")
        return resp.json()

    def get_torrent_files(self, torrent_hash):
        """获取指定种子的文件清单"""
        resp = self._get("/api/v2/torrents/files", hash=torrent_hash)
        return resp.json()

    def collect_seeding_info(self):
        """
        遍历所有种子与文件，产出：
        - seeding_paths: set[str]  规范化 NAS 路径（映射后），只要种子存在即算占用
        - seeding_torrents: list[dict]  与路径顺序对应的种子元数据（缺失检测用）
        """
        torrents = self.get_torrents()
        logger.info("qBittorrent 共 %d 个种子", len(torrents))
        seeding_paths = set()
        seeding_torrents = []
        failed_files = 0
        save_path_prefixes = set()

        for t in torrents:
            try:
                files = self.get_torrent_files(t["hash"])
            except Exception as e:
                failed_files += 1
                logger.warning("获取种子 %s(%s) 文件失败: %s", t.get("name"), t.get("hash"), e)
                continue
            save_path = normalize_path(t.get("save_path", ""))
            if save_path:
                # 取顶层目录作为路径前缀样例，便于调试路径映射
                top = save_path.split("/")[0] if "/" in save_path else save_path
                save_path_prefixes.add(top)
            for f in files:
                file_name = f.get("name", "")
                orig_path = os.path.join(save_path, file_name).replace("\\", "/")
                nas_path = normalize_path(self.apply_path_mapping(orig_path))
                if not nas_path:
                    continue
                seeding_paths.add(nas_path)
                seeding_torrents.append({
                    "torrent_name": t.get("name", "未知"),
                    "torrent_hash": t.get("hash", ""),
                    "torrent_state": t.get("state", "unknown"),
                    "state_label": STATE_LABELS.get(t.get("state"), t.get("state", "未知")),
                    "progress": t.get("progress", 0),
                    "save_path": save_path,
                    "original_path": orig_path,
                    "nas_path": nas_path,
                    "file_name": file_name,
                    "file_size": f.get("size", 0),
                    "client": "qBittorrent",
                })

        logger.info("qB save_path 顶层前缀样例: %s", sorted(save_path_prefixes)[:20] or "(空)")
        logger.info("映射后 NAS 路径样例(前10): %s", sorted(seeding_paths)[:10] or "(空)")
        if failed_files:
            logger.warning("获取种子文件失败共 %d 个", failed_files)
        logger.info("共收集做种文件路径 %d 条（含重复路径去重后 %d 条）",
                    len(seeding_torrents), len(seeding_paths))
        return seeding_paths, seeding_torrents

    def delete_torrents(self, hashes, delete_local_files=False):
        """
        从 qBittorrent 删除种子记录
        - delete_local_files=True 时同时删除种子对应的下载数据文件（危险，不可恢复）
        - .torrent 备份文件由 qB 的 BT_backup 目录托管，Web API 不支持单独删除，
          若配置了 torrent_backup_dir，可由 deleter 模块按 hash 清理
        """
        if not hashes:
            return {"ok": 0, "failed": 0}
        self._post(
            "/api/v2/torrents/delete",
            data={
                "hashes": "|".join(hashes),
                "deleteFiles": "true" if delete_local_files else "false",
            },
        )
        logger.info("已提交删除 %d 个种子 (deleteFiles=%s)", len(hashes), delete_local_files)
        return {"ok": len(hashes), "failed": 0}
