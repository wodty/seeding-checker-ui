#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NAS 文件扫描 + 冗余/缺失检测核心逻辑

判定标准（按用户需求修正原项目逻辑）：
- 冗余文件：NAS 上存在文件，且 qBittorrent 中【不存在任何种子】关联该文件
  （只要存在种子就算占用，不做做种状态过滤 —— 避免把"有种子但未做种"的文件误判为冗余）
- 缺失文件：qBittorrent 中存在种子，但 NAS 上对应文件已不存在（含防误报多重校验）
"""
import os
import re
import logging

logger = logging.getLogger("checker")


# ---------- NAS 扫描 ----------

FILE_TYPE_RULES = [
    (".m2ts", "蓝光原盘"),
    ((".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"), "视频"),
    ((".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"), "音频"),
    ((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"), "图片"),
    ((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt"), "文档"),
]


def classify_file(path):
    ext = os.path.splitext(path)[1].lower()
    for rule_exts, label in FILE_TYPE_RULES:
        exts = rule_exts if isinstance(rule_exts, tuple) else (rule_exts,)
        if ext in exts:
            return label
    return "其他"


def scan_nas(directories, exclude_dirs=None, size_threshold_mb=0, ignore_links=True):
    """
    扫描 NAS 目录
    :param directories: 目录列表（逗号分隔字符串或 list）
    :param exclude_dirs: 排除目录（前缀匹配，支持逗号分隔字符串或 list）
    :param size_threshold_mb: 最小文件大小阈值(MB)，0 = 不过滤
    :param ignore_links: 忽略软链接/硬链接
    :return: list[dict] {path, size, size_human, type, ext, root}
    """
    if isinstance(directories, str):
        directories = [d.strip() for d in re.split(r"[，,]", directories) if d.strip()]
    excludes = []
    if exclude_dirs:
        if isinstance(exclude_dirs, str):
            excludes = [d.strip().replace("\\", "/").rstrip("/") for d in re.split(r"[，,]", exclude_dirs) if d.strip()]
        else:
            excludes = [d.replace("\\", "/").rstrip("/") for d in exclude_dirs]

    threshold = int(size_threshold_mb or 0) * 1024 * 1024
    results = []
    seen = set()
    seen_inodes = set()
    stats = {"files": 0, "links_skipped": 0, "errors": 0, "dirs": 0}

    for root_dir in directories:
        root_dir = root_dir.rstrip("/\\")
        if not os.path.isdir(root_dir):
            logger.warning("NAS 目录不存在，跳过: %s", root_dir)
            continue
        logger.info("开始扫描目录: %s", root_dir)
        for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=True):
            norm_dir = dirpath.replace("\\", "/").rstrip("/")
            if any(norm_dir.startswith(ex + "/") or norm_dir == ex for ex in excludes):
                dirnames[:] = []
                continue
            stats["dirs"] += 1
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    if ignore_links:
                        if os.path.islink(fpath):
                            stats["links_skipped"] += 1
                            continue
                        # 对硬链接只保留第一次出现的 inode，避免重复统计；
                        # 后续指向同一 inode 的其他路径才跳过。
                        st = os.stat(fpath, follow_symlinks=False)
                        nlink = getattr(st, "st_nlink", 1)
                        if nlink > 1:
                            inode = (st.st_dev, st.st_ino)
                            if inode in seen_inodes:
                                stats["links_skipped"] += 1
                                continue
                            seen_inodes.add(inode)
                    else:
                        st = os.stat(fpath, follow_symlinks=True)
                    if threshold and st.st_size < threshold:
                        continue
                    norm = os.path.normpath(fpath).replace("\\", "/")
                    if norm in seen:
                        continue
                    seen.add(norm)
                    results.append({
                        "path": fpath,
                        "size": st.st_size,
                        "size_human": human_size(st.st_size),
                        "type": classify_file(fpath),
                        "ext": os.path.splitext(fname)[1].lower(),
                        "root": root_dir,
                    })
                    stats["files"] += 1
                except OSError as e:
                    stats["errors"] += 1
                    logger.debug("扫描文件失败 %s: %s", fpath, e)
    logger.info("NAS 扫描完成: %d 个文件, %d 个目录, 跳过链接 %d, 错误 %d",
                stats["files"], stats["dirs"], stats["links_skipped"], stats["errors"])
    return results, stats


def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if num < 1024 or unit == "PB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} PB"


# ---------- 检测 ----------

# qBittorrent 异常状态集合：状态损坏的种子，独立分类展示与删除
ABNORMAL_STATES = {"error", "missingFiles"}


def detect_abnormal(seeding_torrents):
    """
    异常状态种子：qB 中状态为 error/missingFiles 的种子（按 torrent_hash 去重）
    与缺失检测的区别：只看种子状态，不要求文件丢失 —— 文件还在的 error 种子同样归类
    :return: list[dict] 每条对应一个异常种子（含首个关联文件信息）
    """
    abnormal = []
    seen = set()
    for item in seeding_torrents:
        h = item.get("torrent_hash", "")
        if not h or h in seen:
            continue
        if item.get("torrent_state") in ABNORMAL_STATES:
            seen.add(h)
            abnormal.append(item)
    logger.info("异常状态种子检测: %d 个 (状态: %s)", len(abnormal), ",".join(sorted(ABNORMAL_STATES)))
    return abnormal


def detect_redundant(nas_files, seeding_paths):
    """
    冗余文件：NAS 有文件 && qB 无任何对应种子
    注意：不再像原项目那样只看"做种状态"，只要种子存在即视为占用
    """
    redundant = []
    for f in nas_files:
        if f["path"].replace("\\", "/") not in seeding_paths:
            redundant.append(f)
    logger.info("冗余文件检测: %d / %d 个 NAS 文件未被任何种子占用", len(redundant), len(nas_files))
    return redundant


def _alt_candidates(nas_path, torrent_info):
    """生成缺失判定的替代路径候选（防路径格式差异误报）"""
    candidates = []
    base = nas_path.replace("\\", "/")
    candidates.append(base)
    candidates.append(base.replace("/", "\\"))
    candidates.append(re.sub(r'[^a-zA-Z0-9/\._\-\\]', '_', base))
    save = (torrent_info.get("save_path") or "").replace("\\", "/")
    fname = torrent_info.get("file_name") or ""
    if save and fname:
        candidates.append((save.rstrip("/") + "/" + fname).replace("\\", "/"))
        candidates.append((save.rstrip("/") + "/" + fname).replace("/", "\\"))
    orig = torrent_info.get("original_path") or ""
    if orig:
        candidates.append(orig.replace("\\", "/"))
    return list(dict.fromkeys(c for c in candidates if c))


def detect_missing(seeding_torrents, nas_paths, nas_directories):
    """
    缺失文件：qB 有种子 && NAS 文件不存在（限 nas_directories 监控范围，防误报多重校验）
    :return: list[dict] 每个缺失条目对应一条种子文件记录
    """
    nas_set = set(p.replace("\\", "/") for p in nas_paths)
    if isinstance(nas_directories, str):
        nas_dirs = [d.strip().replace("\\", "/").rstrip("/") for d in re.split(r"[，,]", nas_directories) if d.strip()]
    else:
        nas_dirs = [d.replace("\\", "/").rstrip("/") for d in nas_directories]

    missing = []
    processed = set()
    for item in seeding_torrents:
        nas_path = item.get("nas_path") or ""
        if not nas_path:
            continue
        # 范围限定：只检查 nas_directories 监控范围内的种子文件
        in_scope = False
        for d in nas_dirs:
            if nas_path.startswith(d + "/") or nas_path == d:
                in_scope = True
                break
        if not in_scope:
            continue

        key = item.get("torrent_hash", "") + "|" + nas_path
        if key in processed:
            continue
        processed.add(key)

        # 存在性检查 + 替代路径多重校验
        if any(os.path.exists(c) and os.path.isfile(c) for c in _alt_candidates(nas_path, item)):
            continue

        missing.append({
            **item,
            "nas_path": nas_path,
        })

    logger.info("缺失文件检测: 监控范围内 %d 条记录, 确认缺失 %d 条", len(processed), len(missing))
    return missing
