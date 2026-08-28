#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Transmission RPC 客户端（接口与 qb_client.QBClient 保持一致）
- X-Transmission-Session-Id 会话握手（409 → 从响应头取 Session-Id 重试）
- HTTP Basic 认证
- 拉取全部种子与文件清单（映射为与 qB 相同的数据结构，便于聚合检测）
- 删除种子（torrent-remove，可选 delete-local-data）
"""
import os
import logging
import requests

from qb_client import normalize_path, parse_path_mappings, apply_path_mappings

logger = logging.getLogger("tr_client")


class TRError(Exception):
    pass


# Transmission 状态码 → qB 风格 torrent_state（统一异常检测逻辑 ABNORMAL_STATES）
STATUS_TO_STATE = {
    0: "pausedDL",        # 已停止（后面按进度细分为 pausedUP/pausedDL）
    1: "checkingDL",     # 等待校验
    2: "checkingDL",      # 校验中
    3: "queuedDL",       # 等待下载
    4: "downloading",     # 下载中
    5: "queuedUP",        # 等待做种
    6: "uploading",       # 做种中
}

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


class TRClient:
    def __init__(self, host, port, username, password, path_mappings=""):
        # 兼容 host 配置带/不带协议前缀（如 http://tr.example.com 或 tr.example.com）
        host = str(host or "").strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        netloc = host.split("://", 1)[1] if "://" in host else host
        self.base = host if ":" in netloc else f"{host}:{port}"
        self.username = username
        self.password = password
        self.session = requests.Session()
        if username or password:
            self.session.auth = (username or "", password or "")
        self.session.headers.update({"User-Agent": "seeding-checker-ui/1.0"})
        self._mappings = parse_path_mappings(path_mappings)
        self._session_id = None
        self.logged_in = False

    # ---------- RPC ----------

    def _rpc(self, method, arguments=None):
        """Transmission RPC 调用（自动处理 409 Session-Id 握手）"""
        payload = {"method": method, "arguments": arguments or {}}
        for _ in range(3):
            headers = {}
            if self._session_id:
                headers["X-Transmission-Session-Id"] = self._session_id
            resp = self.session.post(f"{self.base}/transmission/rpc",
                                     json=payload, headers=headers, timeout=30)
            if resp.status_code == 409:
                self._session_id = resp.headers.get("X-Transmission-Session-Id")
                if not self._session_id:
                    raise TRError("Transmission 返回 409 但未携带 Session-Id，请检查地址/端口")
                continue
            if resp.status_code == 401:
                raise TRError("Transmission 认证失败（401），请检查用户名/密码")
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", "")
            if result != "success":
                raise TRError(f"Transmission RPC 调用失败: {result}")
            self.logged_in = True
            return data.get("arguments", {})
        raise TRError("无法获取 Transmission Session-Id（连续 409），请检查地址/端口是否正确")

    def login(self):
        self._rpc("session-get")
        logger.info("Transmission 连接成功: %s", self.base)
        return True

    # ---------- 数据 ----------

    def get_torrents(self):
        args = self._rpc("torrent-get", {
            "fields": ["hashString", "name", "status", "percentDone",
                       "downloadDir", "error", "errorString", "files"],
        })
        return args.get("torrents", [])

    def collect_seeding_info(self):
        """
        与 QBClient.collect_seeding_info 返回结构一致：
        - seeding_paths: set[str] 规范化 NAS 路径（映射后）
        - seeding_torrents: list[dict] 种子元数据（缺失检测用）
        """
        torrents = self.get_torrents()
        logger.info("Transmission 共 %d 个种子", len(torrents))
        seeding_paths = set()
        seeding_torrents = []

        for t in torrents:
            save_path = normalize_path(t.get("downloadDir", ""))
            error = t.get("error", 0)
            if error:
                state = "error"
                state_label = f"错误({t.get('errorString') or error})"
            else:
                state = STATUS_TO_STATE.get(t.get("status"), "unknown")
                if state == "pausedDL" and (t.get("percentDone") or 0) >= 1.0:
                    state = "pausedUP"
                state_label = STATE_LABELS.get(state, state)
            for f in t.get("files", []):
                file_name = f.get("name", "")
                orig_path = os.path.join(save_path, file_name).replace("\\", "/")
                nas_path = normalize_path(apply_path_mappings(orig_path, self._mappings))
                if not nas_path:
                    continue
                seeding_paths.add(nas_path)
                seeding_torrents.append({
                    "torrent_name": t.get("name", "未知"),
                    "torrent_hash": t.get("hashString", ""),
                    "torrent_state": state,
                    "state_label": state_label,
                    "progress": t.get("percentDone", 0),
                    "save_path": save_path,
                    "original_path": orig_path,
                    "nas_path": nas_path,
                    "file_name": file_name,
                    "file_size": f.get("length", 0),
                    "client": "Transmission",
                })

        logger.info("映射后 NAS 路径样例(前10): %s", sorted(seeding_paths)[:10] or "(空)")
        logger.info("共收集做种文件路径 %d 条（去重后 %d 条）",
                    len(seeding_torrents), len(seeding_paths))
        return seeding_paths, seeding_torrents

    def delete_torrents(self, hashes, delete_local_files=False):
        """
        从 Transmission 删除种子记录
        - delete_local_files=True 时同时删除本地数据（delete-local-data，危险）
        """
        if not hashes:
            return {"ok": 0, "failed": 0}
        arguments = {"ids": list(hashes), "delete-local-data": bool(delete_local_files)}
        self._rpc("torrent-remove", arguments)
        logger.info("已提交删除 %d 个种子 (delete-local-data=%s)", len(hashes), delete_local_files)
        return {"ok": len(hashes), "failed": 0}
