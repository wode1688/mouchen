# 电脑本地处理与临时中转

这个入口在当前 Windows 用户目录建立独立账号和数据目录，复用原生 AI 替身界面。电脑保存目标、事件、建言和反馈，运行原有确定性规则；云中转只负责传输。默认不会调用 Codex、Claude 或外部模型，也不会继承旧账号的采集授权。

## 启动

在 `AI-Twin/` 中，使用 Python 3.12 或以上版本并安装依赖：

```powershell
python -m pip install -r backend/requirements.txt
python desktop/local_mode.py run --device-id desktop
```

`run` 先准备独立账号，再启动本机后端和原生界面。首次运行时，采集授权仍需通过原应用界面选择；中转的手动目标与事件请求可以单独导入本机账号。

也可以分开准备和运行：

```powershell
python desktop/local_mode.py setup --device-id desktop
python desktop/local_mode.py start --no-ui
```

去掉 `--no-ui` 会打开原生界面。默认地址为 `http://127.0.0.1:8788`，只监听本机。用 `setup --port 8790` 可以选择其他本机端口；已有目录不会被覆盖，端口和设备名称需沿用首次设置。`--profile` 可以指定源码目录以外的私有目录。

默认目录是 `%LOCALAPPDATA%\Mouchen\LocalMode`，与原程序 `%LOCALAPPDATA%\Mouchen\Desktop` 分离。单实例锁也按目录隔离。这个入口不替换旧 EXE，不将旧数据库自动混入新账号。

Windows 源码界面还需要原桌面包运行依赖。目标、建言、反馈由原界面显示；“问策”的模型生成在此模式下关闭。若只运行中转桥接器，可使用 `--no-ui`。

## 私密桥接配置

`bridge-config.json` 由准备程序生成在上述私有目录，不能提交到 GitHub。

| 字段 | 内容 |
|---|---|
| `version` | `1` |
| `backend_url` | 已设置的本机 HTTP 地址 |
| `desktop_device_id` | 本机中转设备名称，需与中转配置一致 |
| `session_user_id` | 本地正常账号会话的用户 ID |
| `bearer_token_protected` | DPAPI 加密后的 Token，再做 Base64 编码 |
| `token_protection` | `windows-dpapi` |
| `dpapi_entropy` | `mouchen-desktop-v1` |
| `desktop_data_dir`、`database_path` | 独立界面目录和本机业务数据库 |
| `username`、`recovery_password_protected` | 随机本地账号恢复信息；密码同样用 DPAPI 加密 |

桥接器必须使用 `WindowsDataProtector(entropy=b"mouchen-desktop-v1")` 在当前 Windows 用户身份下解密 Token，认证后调用本机接口。初始化使用标准账号和会话机制，未开启匿名或旧版身份头访问。会话有效期为一年；会话过期、被撤销、账号被切换或删除时必须停止导入，并重新完成该本地账号的登录。启动器遇到这些情况会报错，不会绕过认证。DPAPI 恢复信息应由受信任的本地恢复工具使用，不能写入命令行或日志。

桌面事件缓存及凭据采用原来的 DPAPI 保护；业务 SQLite 数据库位于私有用户目录，当前没有额外数据库加密。不要将该目录加入源码同步。

## 已认证导入接口

`POST /v1/relay/import` 只在 `MOUCHEN_MODEL_PROVIDER=rules` 且直接本机请求时可用，拒绝转发来源头、匿名和旧版账号模式。

```json
{
  "schema": "ai-twin.sync/v1",
  "kind": "request",
  "message_id": "7ec8b814-b98f-41b7-aa4b-178c81591865",
  "sender": "phone",
  "created_at": "2026-01-01T12:00:00Z",
  "operation": "goal.create",
  "body": {
    "domain": "work",
    "title": "虚拟项目",
    "quote": "本周为虚拟项目投入十小时",
    "target": {"weekly_hours": 10}
  }
}
```

支持 `goal.create`、`event.create`、`feedback.create`、`snapshot.get`。目标和事件使用原 `GoalCreate`、`EventCreate` 字段；反馈使用 `advice_id`、`kind`、可选 `note`；快照的 `body` 必须为空对象。不能传入用户身份；实际归属始终取本机认证会话。

响应使用相同 `schema` 和 `operation`，`kind=response`、`in_reply_to` 指向请求 ID。成功为 `ok=true`，`result.goals` 和 `result.advice` 是原业务记录；失败为 `ok=false` 和不含输入内容的 `error.code/message`。协议格式或访问权限错误仍使用 HTTP 错误状态。

一个账号内，同一请求 ID 和内容返回完全相同的已保存回执；更改内容复用 ID 返回 HTTP 409。业务修改和回执在同一 SQLite 事务提交，进程中断不会留下“已经修改但没有回执”的半成品。新请求可用 `snapshot.get` 获取后续状态。

请求、响应最大 512 KiB；快照每类至多 100 条，超出时 `result.truncated=true`。当前快照是有界的最新记录视图，不是全量备份接口，也尚未提供分页。事件大小、账号容量、目标数量和反馈数量沿用原有限额。

规则判断保留原来的目标关联、来源、偏好暂停、去重与证据门槛。未经模型复核的高级警告保持暂缓；不会用未验证的模型结果替代规则，也不会执行消息、付款等外部动作。
