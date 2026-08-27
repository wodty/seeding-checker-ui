#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
删除操作：
1. 冗余文件删除：移回收站(trash 目录) / 彻底删除 两种模式
2. 种子移除：调用 qBittorrent API 删除种子记录，并按需清理 .torrent 备份文件
"""
import os
import shutil
import logging
from datetime import datetime

logger = logging.getLogger("deleter")


class DeleteError(Exception):
    pass


def delete_files(file_paths, mode="trash", trash_dir=None):
    """
    删除 NAS 冗余文件
    :param file_paths: 绝对路径列表
    :param mode: 'trash' 移入回收目录（可恢复，推荐）| 'permanent' 彻底删除
    :param trash_dir: 回收目录（mode=trash 时必填）
    :return: {"ok": int, "failed": list[dict], "freed": int}
    """
    if not file_paths:
        return {"ok": 0, "failed": [], "freed": 0}
    if mode == "trash":
        if not trash_dir:
            raise DeleteError("回收站模式需要配置 trash_dir")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trash_root = os.path.join(trash_dir, f"redundant_{stamp}")
        os.makedirs(trash_root, exist_ok=True)

    ok = 0
    freed = 0
    failed = []
    for p in file_paths:
        try:
            if not os.path.exists(p):
                failed.append({"path": p, "reason": "文件已不存在"})
                continue
            size = os.path.getsize(p)
            if mode == "permanent":
                os.remove(p)
            else:
                # 保留原文件名，避免同名冲突时加序号
                dest = os.path.join(trash_root, os.path.basename(p))
                if os.path.exists(dest):
                    base, ext = os.path.splitext(dest)
                    i = 1
                    while os.path.exists(f"{base}_{i}{ext}"):
                        i += 1
                    dest = f"{base}_{i}{ext}"
                shutil.move(p, dest)
            ok += 1
            freed += size
        except OSError as e:
            logger.error("删除失败 %s: %s", p, e)
            failed.append({"path": p, "reason": str(e)})
    logger.info("文件删除完成: 成功 %d, 失败 %d, 释放 %d bytes (mode=%s)", ok, len(failed), freed, mode)
    return {"ok": ok, "failed": failed, "freed": freed, "trash_root": trash_root if mode == "trash" else None}


def delete_torrent_backups(torrent_hashes, backup_dir):
    """
    清理 qBittorrent BT_backup 目录下的 .torrent 备份文件（文件名 = hash.torrent）
    目录不可访问或文件不存在时静默跳过，不阻断主流程
    :return: 实际删除数量
    """
    if not backup_dir or not torrent_hashes:
        return 0
    deleted = 0
    for h in torrent_hashes:
        if not h:
            continue
        for name in (f"{h}.torrent", f"{h}.fastresume"):
            p = os.path.join(backup_dir, name)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    deleted += 1
            except OSError as e:
                logger.warning("清理备份文件失败 %s: %s", p, e)
    return deleted
