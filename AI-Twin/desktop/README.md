# AI替身 Windows Private Alpha

Windows 客户端复用现有AI替身主动引擎，数据默认保存在当前 Windows 账户的本地目录，并使用 DPAPI 加密。

## 启动

需要“云端只传输、电脑本地处理”时，使用新的 [本地模式入口](LOCAL-MODE.md)：在应用目录执行 `python desktop/local_mode.py run`。它建立独立账号和目录，默认只运行本地规则，保留旧程序配置，并与手机的临时中转请求衔接。

双击 `Start-Mouchen-Windows.cmd`。若桌面设置中已配置非本机 HTTPS 服务，启动器只检查该服务并直接打开客户端，不要求本机 Codex 登录，也不会启动 8787；使用本机地址时才按需静默启动本地后端。
需要长期守候时，可在“设置”中勾选“登录 Windows 后自动运行”；AI替身只管理自己的用户启动项。

首次启动会先显示登录/注册界面。商业账号使用服务器会话确定用户归属，客户端不允许手填 `X-User-Id`；访问令牌继续由当前 Windows 账户的 DPAPI 加密保存。退出账号会撤销当前设备令牌并清除本机账号缓存，但保留匿名安装设备编号。旧版私测配置仍可在迁移期使用原有账号头。

给测试用户的无凭据便携包位于 `artifacts\windows\portable\Mouchen-Windows-Portable-0.1.0-alpha03.zip`。完整解压后双击 `Mouchen.exe`；便携包不要求预装 Python，也不包含本机设置、数据库、Token、SSH 密钥或 API Key。

运行环境：

- Windows 10/11
- Python 3.12+
- 已安装根目录 `backend/requirements.txt` 中的依赖

上述 Python 依赖仅适用于源码启动；便携包已自带运行环境。

## 首版信息源

- 活动窗口、应用进程和使用时长
- 当前前台应用通过 Windows UI Automation 暴露的可见文字；默认跳过密码控件、安全桌面、AI替身自身与密码管理器
- Chrome、Edge 最近 7 天浏览活动，之后增量监听；只保存页面标题和站点域名
- 剪贴板文本，私人自用配置默认开启
- 用户选择的文件夹变化
- 文本文件内容，默认关闭
- 手动提交的当前问题

所有信息源均可独立关闭。文件夹只读取用户在客户端中添加的目录。远程后端默认仅收到脱敏片段；本机后端可以接收本地分析所需上下文。私人自用配置默认开启主动云分析。

客户端每 10 分钟把窗口、前台可见正文、浏览、剪贴板和授权文件变化归并为一个有界活动窗口。正文事件携带内容类型、说话人、可见范围、证据强度、匿名会话键和内容指纹；看到他人消息或视频观点不会自动当作用户事实。后端先将内容脱敏，再让模型对照主公原话目标判断是否存在目标偏离、风险、未解决问题或机会；没有充分证据时不生成建言。该间隔可在“设置”中调整为 5 至 120 分钟。

## 本地数据

默认目录：`%LOCALAPPDATA%\Mouchen\Desktop`

- `settings.json`：连接和开关设置；Token 使用 Windows DPAPI 加密。
- `mouchen-desktop.db`：事件队列和建言；JSON 内容使用 Windows DPAPI 加密。

Windows 安装会在加密数据库中生成稳定的 `device_id`。建言列表同步只负责展示；系统通知必须先从服务器取得一次性投递权，实际显示成功后回报完成，显示失败则释放。重启不会根据本地“未确认”状态自行重复弹出，跨设备总提醒次数和间隔由服务器统一控制。

## 测试

```powershell
New-Item -ItemType Directory -Force desktop\.runtime | Out-Null
python -m pytest desktop\tests -q --basetemp=desktop\.runtime\pytest
python desktop\smoke_test.py
desktop\build_windows.ps1
```

便携包构建固定使用 Python 3.14，且构建依赖按 wheel SHA-256 锁定。每次构建输出的 `SHA256SUMS.txt` 是该次产物的权威校验值；由于 PyInstaller PE 元数据和 ZIP 时间戳可能随构建机变化，本项目不承诺不同机器构建出的 EXE/ZIP 字节级完全一致。

浏览活动在私人自用配置中默认开启。AI替身不会保存完整 URL、查询参数、浏览器 Cookie 或密码。开启“当前应用可见文字”后，只读取当前前台应用主动暴露给 Windows 辅助功能的文字，不会模拟点击或绕过应用沙箱；自绘界面、安全桌面、DRM 内容和未显示的微信历史可能无法读取。本轮不做持续截图或 OCR。

当前版本尚未包含 Chrome 扩展、Outlook 原生连接、会议转写、文档 OCR 和签名安装器；现有 Windows 产物是需完整解压的便携包。这些能力按独立信息源继续接入。
