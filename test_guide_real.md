# 真实环境测试指南（NAS + qBittorrent）

本指南针对「程序跑在能直接访问 NAS 原生路径的环境」设计（NAS 本机 / Docker / 挂载了 NFS·SMB 的机器，路径与 qB 容器一致时无需 path_mappings）。

## 0. 测试前必改的配置（已帮你改好）

| 项目 | 原值 | 现值 | 说明 |
|------|------|------|------|
| `[qb] host` | `http://<qB地址>` | `<qB地址>` | 代码按 `http://{host}:{port}` 拼接，带协议前缀会生成 `http://http://...` 导致连接失败 |
| `qb_client.py` 构造函数 | 直接拼接 | 自动兼容带/不带协议前缀 | 防止以后再填错 |

## 1. 前置检查（10 分钟）

### 1.1 确认 qBittorrent Web UI 可访问
- 浏览器打开：`http://<qB地址>:<Web UI 端口>`
- 用你的 qB 账号密码登录，能正常看到种子列表
- 若打不开：检查 qB 是否开启了 Web UI（工具 → 选项 → Web UI → 勾选"启用 Web 用户界面"）、防火墙是否放行 Web UI 端口

### 1.2 确认 qB 账号有删除权限
- 登录 qB 后：工具 → 选项 → Web UI → 认证，确认该账号属于管理员组（Admin）
- 删除种子、删除文件都需要管理员权限，普通用户权限不够

### 1.3 确认程序运行环境能访问 NAS 路径
```bash
# 在运行本程序的机器上执行（路径换成你自己的）
ls -la /volume1/downloads
# 能列出文件即可；若报"没有那个文件/目录"，说明路径在别处或未挂载
```
> 程序跑在哪，`nas_directories` 就得写哪台机器能看到的路。程序跑在 Windows 电脑上时，NAS 目录需挂载为网络盘，且 `nas_directories`、`path_mappings` 都要写成 Windows 挂载路径（见第 6 节）。

### 1.4 检查 config.ini 最终值
```ini
[general]
nas_directories = /volume1/downloads
exclude_directories =
size_threshold = 0
trash_dir = ./trash
[qb]
host = <qB地址>
port = <Web UI 端口>
username = <用户名>
password = <密码>
path_mappings = /volume1/downloads=/volume1/downloads
```
- `path_mappings` 当前是「自身映射」= 不映射，适合 qB 和本程序看到的路径一致的情况
- 如果 qB 跑在 Docker 里、容器内路径是 `/downloads`，则必须改成 `/downloads=/volume1/downloads`，否则缺失/冗余检测会全部错乱

## 2. 启动与连接测试

```bash
# 在项目目录
python app.py            # 正常模式（不要加 --demo！）
# 看到日志输出即成功，默认端口 8000
```

浏览器打开 `http://localhost:8000`，页面右上角配置区点 **「测试连接」**：
- 显示「连接成功，共 N 个种子」→ 通过
- 报错 → 看第 5 节排查

## 3. 扫描验证（核心步骤，务必逐项核对）

点 **「开始扫描」**，然后核对三个 tab 的结果是否与你的真实情况一致：

| Tab | 期望结果 | 验证方法 |
|-----|----------|----------|
| 冗余文件 | 只有「NAS 有文件但 qB 完全没种子的文件」 | 随机挑 3 个结果，去 qB 搜索对应文件名，确认确实没有种子 |
| 缺失文件 | 只有「qB 有种子但 NAS 文件没了」的文件 | 随机挑 3 个结果，去 NAS 上 `ls` 对应路径，确认文件确实不存在 |
| 异常状态 | error / missingFiles 的种子 | 与 qB Web UI 的「状态」列逐条比对 |

**如果冗余/缺失数量异常**，优先检查（按概率排序）：
1. **路径不一致**：qB 返回的路径经 path_mappings 映射后 ≠ NAS 实际路径 → 检查 path_mappings
2. **硬链接/软链接被跳过**：`ignore_links = true` 会跳过 `st_nlink > 1` 的文件。若你的下载目录用硬链接做种，改为 `false` 重扫
3. **监控范围不对**：qB 的 save_path 在 nas_directories 之外的种子，其文件不会参与缺失检测

## 4. 删除演练（先小后大，先用回收站）

**第一次真实删除，只做一件小事：**
1. 在「冗余文件」tab 选 1 个**最不值钱**的小文件
2. 删除方式选 **「移回收站」**
3. 确认执行后，去 `./trash/redundant_时间戳/` 下确认文件还在 → 可恢复性验证 ✅

**再验证缺失/异常种子删除：**
1. 在「缺失文件」或「异常状态」tab 选 1 个种子
2. **不要勾选**「同时删除本地文件」（默认不勾选，安全）
3. 确认后去 qB 看该种子是否已消失

**最后（可选）验证连带删除本地文件：**
1. 选 1 个确认不要了的种子，勾选「同时删除本地文件」
2. 执行后检查 NAS 对应文件是否被删、qB 种子是否消失
3. ⚠️ 此操作不可恢复，务必确认该文件没有其他种子占用

## 5. 常见问题排查

| 现象 | 原因 | 解决 |
|------|------|------|
| 测试连接报错 `Failed to establish a new connection` | 网络不通/防火墙 | 先浏览器访问 `http://<qB地址>:<端口>` 确认能开 |
| 报错 `401 Unauthorized` 或登录失败 | 账号密码错 / Web UI 未开 | 浏览器登录一次确认凭据 |
| 扫描报「qBittorrent 连接失败」 | 上面任一种 | 先点测试连接定位 |
| 冗余把做种中的文件也列出来了 | path_mappings 没配对 | 修正 path_mappings 后重扫 |
| NAS 文件数偏少 | ignore_links 跳过了硬链接文件 | 改 `ignore_links = false` 重扫 |
| 删除报「拒绝删除监控范围外的文件」 | 文件路径不在 nas_directories 下 | 检查 path_mappings 映射后的路径归属 |

## 6. 程序跑在 Windows 电脑上的特殊配置

如果程序跑在 Windows、NAS 目录通过 SMB 挂载（如映射为 `Z:`）：
```ini
[general]
nas_directories = Z:/downloads6          # Windows 挂载路径
[qb]
path_mappings = /volume1/downloads=Z:/downloads   # qB 看到的路径=Windows 实际路径
```
此时 `trash_dir` 默认是程序目录下的 `./trash`（本地磁盘），移回收站 = 从 NAS 移动到本地，跨设备移动会**复制+删除**，大文件会很慢。建议把 `trash_dir` 改到 NAS 目录内，例如 `Z:/trash`，实现 NAS 内部快速移动。

## 7. 测试完成后的检查清单

- [ ] 测试连接成功（显示种子数量）
- [ ] 冗余文件：随机抽查 3 个，确认确实无种子占用
- [ ] 缺失文件：随机抽查 3 个，确认文件确实丢失
- [ ] 异常状态：与 qB 状态列一致
- [ ] 移回收站：文件出现在 `./trash/redundant_时间戳/`，且原位置消失
- [ ] 彻底删除：文件真正消失（选小文件试）
- [ ] 移除种子：qB 中种子消失
- [ ] 连带删除本地文件：NAS 文件被删（可选，谨慎）
- [ ] 刷新页面再扫描一次，数据与删除操作一致（无残留显示）
