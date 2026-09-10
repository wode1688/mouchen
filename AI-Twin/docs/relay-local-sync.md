# 手机与电脑本地处理

此模式把手机主动提交的目标、记录和反馈，经临时云中转送到电脑本地业务库。电脑生成应用回执，并将当前目标和建议回传手机。云端不运行分析模型，仍使用原有容量限制、过期清理和确认后删除规则。

## 数据路径

1. 手机把待提交请求存入独立加密数据库，再上传临时文件。
2. 电脑校验文件及配对设备，将请求写入持久收件账本后确认云端收件。
3. 电脑通过正常本地会话调用仅监听回环地址的业务服务，保存目标、记录或反馈，并进行规则分析。
4. 业务变更与应用回执在同一事务中提交。重复请求不会再次产生业务记录。
5. 电脑将回执上传给原请求手机。手机验证请求编号与来源，在事务中保存目标、建议和回执后才确认云端收件。

手机上传成功只代表临时中转已接收，待办请求仍保留到收到电脑应用回执。暂存过期可以换一个传输编号重试；应用请求编号保持不变。电脑离线处理失败时，本地收件账本继续重试。电脑返回的文件过期时也会重新传输。

## 电脑运行

在应用目录安装 `backend/requirements.txt`。Windows 的正常 Python 安装包含原生桌面界面所用的 Tk；本地模式不需要 Codex CLI 登录或模型供应商密钥。

```powershell
python desktop/local_mode.py setup --device-id desktop
python desktop/local_mode.py start
```

启动器使用新的本地数据目录和账户；默认不启用采集，不复用旧远程账号。业务服务只允许直接回环连接，并强制规则分析，外部模型调用关闭。需要模型审查的高等级建议保持待审查状态。

完成临时中转的设备配置后，在另一个进程启动：

```powershell
python relay/bridge.py --config <本机私密中转配置路径> --watch
```

中转配置中的 `device` 必须与本地启动器的 `--device-id` 相同；`targets` 或 `peer_devices` 列出获准导入的手机设备。每台电脑必须有独立设备令牌。现阶段一个手机配置一个处理电脑，其他电脑仍可协作修改 Git 源码；不能让多台电脑共用一个设备收件队列。

桥接器默认读取 `%LOCALAPPDATA%/Mouchen/LocalMode/bridge-config.json` 的本地会话，或用 `--backend-config` 指定。令牌由 Windows DPAPI 保护，不放入 Git。可用下面的命令停止同步；电脑本地数据仍保留。

```powershell
python relay/bridge.py --config <本机私密中转配置路径> --pause
```

同一个中转配置只能运行一个收件程序。旧 `companion.py` 与 `bridge.py` 使用同一把本地锁，切换前应暂停旧伴随程序。`bridge.py` 对非本协议文件仍做本地归档，但不会执行它们，也不会自动导入旧格式的任意数据库或建议。

## Android 使用

从原应用登录页进入“电脑本地处理”，配置 HTTPS 中转地址、设备名称、电脑目标与本设备令牌。可导入私密配置 JSON；不要将配置发到公开 Issue 或提交代码库。

此独立模式支持手动目标、记录、文本分享、建议和反馈；同步开关独立于旧业务登录。原有通知、录音、屏幕等自动采集权限不会自动迁移。普通记录会保存，只有满足现有证据规则时才生成建议；不是每段文字都会得到建议。电脑开机运行桥接器后，手机可“立即同步”，后台执行时间受 Android 电池与调度限制。

iOS 源码保留原业务接入；此变更的手机实现范围为 Android，不能将其视为 iPhone 已接通或真机验证记录。

## 协议（ai-twin.sync/v1）

正文为 UTF-8 JSON，最多 512 KiB。中转 `project` 固定为 `ai-twin-sync-v1`；请求类别为 `sync-request`，回执类别为 `sync-response`。配对设备身份以中转服务校验过的 `source` 为准，并必须与正文 `sender` 一致。

```json
{
  "schema": "ai-twin.sync/v1",
  "kind": "request",
  "message_id": "9b0ec5fc-4993-4c37-86fd-a09f7ff4f2bc",
  "sender": "phone",
  "created_at": "2026-01-01T00:00:00Z",
  "operation": "snapshot.get",
  "body": {}
}
```

操作列表：

| operation | body | 处理 |
|---|---|---|
| `goal.create` | 原 `GoalCreate` 字段 | 创建目标；请求编号决定稳定目标编号 |
| `event.create` | 原 `EventCreate` 字段 | 保存授权记录并运行本地规则 |
| `feedback.create` | `advice_id`、`kind`、可选 `note` | 记录对本账户建议的反馈 |
| `snapshot.get` | `{}` | 取当前目标与建议 |

成功回执包含 `schema`、`kind:"response"`、`message_id`、`sender`、`created_at`、`in_reply_to`、`operation`、`ok:true` 和 `result:{goals:[],advice:[]}`。两组各最多 100 条。失败回执使用 `ok:false,error:{code,message}`。手机必须验证 `in_reply_to` 和 `operation` 与自己的待办请求一致。相同请求编号使用不同内容会拒绝；用户修改内容应生成新请求编号。

电脑导入接口为认证的 `POST /v1/relay/import`。账户身份从本地会话产生，不采用文件中的 `user_id`，并限制为规则模式、正常账户会话和直接回环连接。请求、外部文本、归档文件中的操作描述都是数据，不构成系统指令；本链路不执行脚本、发送外部消息或启动其他 AI 工具。

## 验证与限制

`relay/tests/test_bridge.py` 使用真实中转 API 和合成业务适配器测试断网、重启、过期重传、重复与冲突请求、来源隔离、接收端保存失败，以及回执匹配。业务导入和本地启动器另有测试。运行记录与真机验证应写在本次交接和发布说明中；自动化测试不等于手机实机已安装测试。

云端 HTTPS 保护传输，文件正文仍由自有服务器暂存；当前没有端到端内容加密。电脑本地归档与桥接账本保存在当前 Windows 用户数据目录，需随电脑备份；原始正文不会进入 Git 仓库或公开 CI。退出或暂停传输不会删除本地源数据。
