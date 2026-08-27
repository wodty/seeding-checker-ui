# Seeding Checker UI

基于 qBittorrent 的 NAS 冗余/缺失文件检查工具（Web UI 版），在 [Seeding-Checker](https://github.com/eternalcurse/Seeding-Checker) 基础上重构：

- **冗余文件**：NAS 上存在文件，但 qBittorrent 中**没有任何种子**占用 → 可删除（移回收站/彻底删除，二选一）
- **缺失文件**：qBittorrent 有种子，但 NAS 上文件已丢失 → 可移除种子
- **异常状态**：qB 中状态为 error / missingFiles 的种子，独立分类展示 → 可移除种子
- 移除种子时可选：**同时删除本地文件**（危险，默认不勾选）/ **同时清理 .torrent 备份**（需配置 BT_backup 目录），两个选项相互独立
- 全部删除操作带**二次确认 + 风险提示**，支持单条、所选、一键全部
- 修正原项目逻辑：**只要 qB 中存在种子即视为文件被占用**（不再只统计"做种中"状态，避免把有种子但未做种的文件误判为冗余）

## 功能

| 功能 | 说明 |
|------|------|
| Web UI | 浏览器访问，无需安装客户端，适合 NAS/Docker 部署 |
| 冗余检测 | NAS 有文件 && qB 无任何种子占用，按类型/目录分类展示 |
| 缺失检测 | qB 有种子 && NAS 文件丢失 |
| 异常检测 | qB 状态为 error/missingFiles 的种子（独立 tab，按 hash 去重） |
| 删除 | 冗余文件：移回收站（可恢复）/ 彻底删除；缺失/异常种子：移除种子（可勾选连带删除本地文件 / 清理 .torrent 备份） |
| 二次确认 | 删除前弹窗展示数量、大小、风险提示，勾选确认后才可执行 |
| 安全校验 | 后端校验只允许删除 nas_directories 监控范围内的文件 |

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 编辑 config.ini（NAS 目录、qB 连接信息）

# 3. 启动
python app.py
# 浏览器打开 http://<机器IP>:8000
```

### 演示模式（无 qB 环境体验 UI）

```bash
python app.py --demo
```

自动生成模拟数据（含冗余文件、缺失文件、异常状态种子），可完整体验扫描与删除流程。

### Docker 部署（NAS 推荐）

项目已内置 `Dockerfile` 与 `docker-compose.yml`，NAS（群晖 Container Manager / 威联通 Container Station 等）拷入项目后：

```bash
docker compose up -d --build     # 老版本 NAS 用 docker-compose up -d --build
```

完整部署步骤（文件拷贝、路径挂载、开机自启、常见问题）见 [deploy_nas.md](deploy_nas.md)。

## 配置说明（config.ini）

```ini
[general]
nas_directories = /vol1/data, /vol2/media   # NAS 目录（逗号分隔）
exclude_directories = /vol1/data/temp       # 排除目录（可选）
size_threshold = 0                           # 最小文件大小 MB（0=全部）
trash_dir = ./trash                          # 回收目录（移回收站时使用）
ignore_links = true                          # 忽略软/硬链接

[qb]
host = 192.168.1.100                         # qBittorrent 地址
port = 8080
username = admin
password = admin
path_mappings = /downloads=/vol1/data        # 容器路径=NAS路径（逗号分隔）
torrent_backup_dir =                          # qB BT_backup 目录（可选，用于清理 .torrent）
```

## 项目结构

```
seeding-checker-ui/
├── app.py          # FastAPI 后端（API + 静态服务）
├── qb_client.py    # qBittorrent Web API v2 客户端
├── checker.py      # NAS 扫描 + 冗余/缺失检测
├── deleter.py      # 删除逻辑（回收站/彻底删除/种子备份清理）
├── static/
│   └── index.html  # 单页 Web UI
├── Dockerfile      # NAS/Docker 部署
├── docker-compose.yml
├── deploy_nas.md   # NAS 部署指南
├── test_guide_real.md  # 真实环境测试指南
├── smoke_test.py   # 冒烟测试脚本
└── requirements.txt
```

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | / | Web UI |
| GET | /api/config | 读取配置 |
| POST | /api/config | 保存配置 |
| POST | /api/qb/test | 测试 qB 连接 |
| POST | /api/scan | 执行扫描（冗余+缺失+异常） |
| POST | /api/delete-files | 删除冗余文件 `{paths, mode: trash\|permanent}` |
| POST | /api/delete-torrents | 移除种子 `{hashes, delete_local_files, delete_torrent_backup}`（两个选项独立，删除本地文件危险） |

## 注意事项

- 删除文件前会校验路径必须在 nas_directories 范围内，越界请求直接拒绝
- 「移回收站」模式：文件移动到 `trash_dir` 下的 `redundant_时间戳/` 目录，可手动恢复
- 「移除种子」默认不删除任何 NAS 文件；勾选「同时删除本地文件」才会调用 qB 的 deleteFiles（后端会先校验涉及文件在监控范围内）；「同时清理 .torrent 备份」需要配置 qB 的 BT_backup 目录且该目录对本程序可访问，两者相互独立
