# 部署到 NAS 测试指南

目标：把 seeding-checker-ui 跑在 NAS 上，让程序直接读写 NAS 本地文件（路径与 qB 一致，检测最准确）。

两种方式二选一：
- **方式一：Docker** — NAS 装过 Docker / Container Manager / Container Station 且**镜像源可用**时用，环境干净、开机自启
- **方式二：SSH + Python venv（镜像源拉不下来时的首选）** — 依赖系统 Python3，不碰 Docker，**国内网络环境最稳**

> 前提：qB 与 NAS 是同一套路径（自身映射，qB 与 NAS 路径一致），这是最简单的场景；若 qB 跑在 Docker 里容器内路径不同，需配合 `path_mappings` 映射。

---

## 第 0 步：确认 NAS 环境

先确认两件事：

```bash
# 1. NAS 是否支持 Docker（群晖：套件中心搜索 Container Manager；威联通：Container Station；或用 SSH 执行）
docker --version

# 2. SSH 是否可用（方式二需要）
# 群晖：控制面板 → 终端机和 SNMP → 启用 SSH；然后用
ssh admin@<NAS的IP>
```

---

## 第 1 步：把项目文件拷到 NAS

在 NAS 上建一个目录（例如 `/opt/seeding-checker-ui`），把整个项目拷进去。任选一种：

**方式 A：File Station / 网页上传（最简单）**
1. 本地把 `seeding-checker-ui` 文件夹压缩成 zip
2. NAS 网页端 File Station → 进入 `tools` 目录（没有就新建）→ 上传 zip → 右键解压

**方式 B：SCP（有 SSH 时）**
```bash
scp -r ./seeding-checker-ui admin@<NAS的IP>:/opt/
```

**方式 C：Git（如果 NAS 上能访问 GitHub）**
```bash
git clone https://github.com/<你的仓库>/seeding-checker-ui.git
```

拷完后核对目录结构：
```
/opt/seeding-checker-ui/
├── app.py
├── qb_client.py
├── checker.py
├── deleter.py
├── config.ini          ← 关键，检查里面的路径
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── static/
└── test_guide_real.md
```

## 第 2 步：核对 config.ini（在 NAS 上）

用 NAS 文件管理器编辑 `/opt/seeding-checker-ui/config.ini`：

```ini
[general]
nas_directories = /volume1/downloads   # 必须是 NAS 上的真实路径
trash_dir = ./trash                     # 相对路径 = 程序目录下 trash/，OK
[qb]
host = 192.168.1.100                    # 不带 http://
port = 8080
path_mappings = /volume1/downloads=/volume1/downloads   # 自身映射（qB 与 NAS 路径一致时）
```

---

## 方式一：Docker 部署（推荐）

### 1. 进入项目目录
```bash
cd /opt/seeding-checker-ui
```

### 2. 检查 docker-compose.yml 的挂载
确认第 2 处挂载路径正确（当前是 `/path/to/downloads:/path/to/downloads`，左侧改成你的 NAS 真实下载路径，右侧与 `nas_directories` 保持一致）。

### 2.5 配置 Docker 走代理（有代理时强烈推荐）

有代理就绕开公共镜像源问题，直接拉官方镜像（把 `<代理地址>` 换成你的，例如 `http://192.168.1.10:7890`）：

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf >/dev/null <<'EOF'
[Service]
Environment="HTTP_PROXY=http://<代理地址>"
Environment="HTTPS_PROXY=http://<代理地址>"
Environment="NO_PROXY=localhost,127.0.0.1"
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker
```

> ⚠️ 重启 Docker 会短暂中断 NAS 上所有正在运行的容器，请确认没有正在下载/写入的关键任务。
> 验证生效：`sudo docker info | grep -i proxy`（能看到 HTTP_PROXY 即成功）。
> 代理地址按你的实际地址替换；如果代理不在 NAS 本机，把 IP 换成代理所在机器的内网地址。

### 3. 构建并启动
```bash
docker compose up -d --build
```
> 群晖老版本没有 `docker compose` 子命令，用 `docker-compose up -d --build`（带横杠）。
> 基础镜像默认走官方源（配合上面代理）；无代理时用 `PY_IMAGE=docker.1ms.run/library/python:3.11-slim docker compose up -d --build` 换国内源。

> **镜像拉不下来（401 / timeout / connection reset）？** 2026 年公共加速器大量失效，**先测再配**（只查元数据，很快）：
> ```bash
> for src in docker.fxxk.dedyn.io doublezonline.cloud dislabaiot.xyz atomhub.openatom.cn; do
>   timeout 15 docker manifest inspect "$src/library/python:3.11-slim" >/dev/null 2>&1 \
>     && echo "OK   $src" || echo "FAIL $src"
> done
> ```
> 有 `OK` 就换源构建（sudo 会清空环境变量，用 env 传）：
> ```bash
> sudo env PY_IMAGE=xxx/library/python:3.11-slim docker compose up -d --build
> ```
> **最稳的 Docker 方案：阿里云专属加速器**（免费，需账号）：登录 https://cr.console.aliyun.com → 镜像工具 → 镜像加速器 → 复制专属地址（形如 `https://xxxxxxxx.mirror.aliyuncs.com`）→ 填进 daemon.json 或 **fnOS Docker 应用 → 镜像仓库 → 设置 → 加速源设置**（图形界面，系统更新不覆盖）。
> 公共源全部失败时**直接用「方式二：SSH + venv」部署，不依赖 Docker**。

### 4. 验证
```bash
docker compose ps              # STATUS 应为 Up
docker logs -f seeding-checker-ui   # 看启动日志，Ctrl+C 退出
```
浏览器打开 `http://<NAS的IP>:8000` → 点「测试连接」→ 应显示种子数量。

### 5. 以后更新代码
```bash
cd /opt/seeding-checker-ui
# 用新文件覆盖旧文件（config.ini 不动）
docker compose up -d --build
```

---

## 方式二：SSH + Python venv（无 Docker / 镜像拉不下来时）

以**飞牛 fnOS** 为例（群晖/威联通类似）。fnOS 基于 Debian，直接用 apt 装 Python3。

### 1. 装 Python3
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
python3 --version    # 确认有输出（如 3.11.x）
```

### 2. 建虚拟环境装依赖（pip 必须用国内源，海外 PyPI 会超时）
```bash
cd /opt/seeding-checker-ui
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3. 启动（放后台运行）
```bash
cd /opt/seeding-checker-ui
nohup ./venv/bin/python app.py > app.log 2>&1 &
sleep 3 && tail -20 app.log     # 看到 Uvicorn running 即成功
```
浏览器打开 `http://<NAS的IP>:8000`。

### 4. 开机自启（systemd，fnOS 支持）
```bash
sudo tee /etc/systemd/system/seeding-checker.service >/dev/null <<'EOF'
[Unit]
Description=Seeding Checker UI
After=network.target

[Service]
User=<你的用户名>
WorkingDirectory=/opt/seeding-checker-ui
ExecStart=/opt/seeding-checker-ui/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now seeding-checker
```

### 5. 停止 / 看日志
```bash
sudo systemctl stop seeding-checker
sudo journalctl -u seeding-checker -f
```
> 不用 systemd 时，手动启停：`pkill -f "python app.py"` 停止，第 3 步命令重启。

---

## 第 3 步：在 NAS 上完成测试流程

按 `test_guide_real.md` 走一遍：测试连接 → 扫描 → 逐项核对冗余/缺失/异常 → 先移回收站试删一个小文件 → 再试移除种子。

**特别提醒（NAS 场景）：**
- **端口**：8000 若被占，改 `docker-compose.yml` 左边端口（如 `18000:8000`），或原生方式 `PORT=18000 python app.py`
- **回收站**：文件在 `<项目目录>/trash/redundant_时间戳/` 下，确认没问题后记得手动清理，避免占空间
- **防火墙**：NAS 若开了防火墙，放行 8000 端口，否则外部设备打不开页面
- **扫描大目录**：首次扫描几千个文件可能要几分钟，属正常现象；页面会卡在「扫描中」状态，等它完成

---

## 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 构建报 `connection reset` / `401` | 公共镜像源失效 | 见方式一第 3 步换源测试；全部失败**改用方式二（venv 部署）** |
| 容器起不来 / `Permission denied` | NAS 目录权限 | 用 `docker compose` 前确认运行用户对下载目录（如 `/volume1/downloads`）有读权限 |
| 页面打不开 | 端口/防火墙 | 检查端口映射和 NAS 防火墙；群晖还需确认「容器网络」为 bridge |
| 测试连接失败 | 容器里访问不到 qB | qB 若在 NAS 本机，用 NAS 局域网 IP 代替域名试；确认 qB Web UI 端口对容器开放 |
| 扫描结果数量不对 | 容器内路径 ≠ qB 路径 | 检查 docker-compose 挂载与 path_mappings 是否一致 |
| 删除报「监控范围外」 | 路径映射错位 | 在 NAS 上重扫后看提示的具体路径，修正 path_mappings |

---

## 完成后回到本地开发

NAS 上验证通过后，本地代码改动重新 `docker compose up -d --build` 即可热更新；配置文件始终以 NAS 上的 `config.ini` 为准（已挂载进容器）。
