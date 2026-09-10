# 临时文件中转与本地归档

这个目录把轻量云中转服务、电脑端同步组件、配置工具和测试放在同一份源码中。服务端只负责临时存取和传输状态；内容处理在接收设备执行。它是可选的独立组件，目前不替换原有业务后端，也不自动把接收的归档写回 AI 替身数据库。

## 当前行为

- SQLite 保存传输描述和接收状态，文件正文独立存放。
- 默认总配额 2 GiB、单文件 100 MiB、保留 24 小时；可设置 1～72 小时保留期。
- 接收端先保存和校验文件，再确认接收；多个目标设备全部确认后删除云端副本。
- 未完成上传的预留在 15 分钟后失效，后台每分钟清理过期副本。
- 已完成传输记录最多保留 7 天、1 万条，避免元数据无限增长。
- 每台设备有独立令牌，接收队列按设备隔离。
- 接收文件按“项目 / 类型 / 日期”在本地归档。发送端原件保留；过期传输需要使用新编号重新发送。

传输使用 HTTPS。当前实现不是端到端加密：服务器文件正文以明文存储在受权限保护的目录，服务端管理员仍有能力读取。请只在自己信任的服务器上使用。

## 在另一台电脑开发

先克隆整个仓库，再进入本目录，不要单独复制某份安装包中的文件。

```powershell
git clone https://github.com/wode1688/mouchen.git
cd mouchen
git fetch origin
# 如果该功能分支尚未合并：
git switch --track origin/codex/cloud-relay-sync
cd AI-Twin/relay
.\scripts\setup.ps1
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Python 3.11 或更新版本可运行此组件。主项目的其他组件继续遵循各自的版本要求。macOS / Linux 可用 `sh scripts/setup.sh` 建立本目录的环境，随后使用 `.venv/bin/python`。

## 每台设备独立配置

服务端必须事先添加相应设备及令牌摘要。设备名称应唯一，例如 `laptop-a`、`laptop-b`。不同电脑不要共用同一个设备身份，否则它们会竞争同一接收队列。

```powershell
.\.venv\Scripts\python configure.py --url https://relay.example.com --device laptop-a --target laptop-b
```

配置工具会隐藏令牌输入。Windows 配置默认存到当前用户的 `%LOCALAPPDATA%\Mouchen\Relay\client-config.json`，令牌使用该 Windows 用户的 DPAPI 加密。其他系统在本机用户数据目录中以限制访问权限的文件保存配置。

也可以用 `--config` 或环境变量 `AI_TWIN_RELAY_CONFIG` 指定本机配置。路径和真实令牌不放入 Git。Windows 加密配置不能直接拷贝给另一台电脑，应在那台电脑重新运行配置工具。

默认只同步显式放入 Outbox 的文件。读取原 AI 替身的加密事件和建议需要在原 Windows 用户下明确启用：

```powershell
.\.venv\Scripts\python configure.py --url https://relay.example.com --device laptop-a --target laptop-b --export-existing-records
```

该适配器只读取原库的 `events` 和 `advice`，不会导出设备身份表或登录凭据，不修改原库及其同步标记。它不代替原应用的采集授权；用户应先确认允许导出这些已经保存的记录。

## 运行和暂停

```powershell
.\scripts\start-sync.ps1 -Once
.\scripts\start-sync.ps1
# 在另一个终端中请求暂停：
.\scripts\pause-sync.ps1
```

后台模式每 30 秒检查一次；暂停请求在当前传输完成后生效。状态、日志、Outbox 和 Archive 默认都在本机数据目录，位于源码之外。暂停的 worker 退出后，重新运行 `start-sync.ps1` 即可恢复。

也可以直接使用 `client.py status`、`client.py push`、`client.py pull`，详见各命令的 `--help`。Windows 可选启动器源码位于 `windows/`；设置 `AI_TWIN_APP_EXE` 后，它可以启动当前电脑上安装的 AI 替身。不要把编译产物提交到仓库。

## 服务端

本地开发时复制 `config/server-config.example.json` 到被忽略的本机配置文件，填入自行生成的高强度令牌的 SHA-256 摘要。设置 `RELAY_CONFIG` 指向该文件，运行：

```powershell
$env:RELAY_CONFIG = (Resolve-Path .\server-config.json).Path
.\.venv\Scripts\python -m uvicorn server:app --host 127.0.0.1 --port 8788 --workers 1
```

**必须使用一个服务进程。** 当前清理与上传恢复逻辑不支持多个 worker 共同管理同一目录。生产部署应将配置和数据分别放在源码外，使用独立服务账户及 HTTPS 入口；模板见 [部署说明](../deploy/relay/README.md)。

接口顺序为建立传输 `POST /v1/transfers`、上传 `PUT /v1/transfers/{id}/content`、查看收件箱 `GET /v1/inbox`、领取 `POST .../claim`、下载 `GET .../content`、本地校验后确认 `POST .../ack`。所有 `/v1/` 请求都需要 `Authorization: Bearer <本设备令牌>`，下载还需 `X-Claim-Token`。保存失败时调用 `POST .../release` 或等待领取过期。

## 协作和能力边界

多电脑统一使用本仓库的分支、提交和 Pull Request 流程，见 [多电脑开发说明](../docs/multi-computer-development.md)。源代码同步不等于业务数据同步：代码用 GitHub，个人数据通过中转服务。

本组件测试使用合成数据。Windows 加密记录读取已在开发环境验证；其他系统的文件传输客户端可以运行，但不能解密另一台 Windows 电脑的原应用数据库。手机应用需要另外接入这些接口，不能把这里的测试当作真实手机端验证。该组件不会运行模型，也不会自动执行收到的文件或建议。
