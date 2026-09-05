# AI替身 iPhone 私测版

该目录是原生 SwiftUI iOS 17 客户端源码。Android、iPhone 与 Windows 使用同一个后端、同一个
`User ID`，即可共享目标、事件、谏言、采纳反馈和预测核验记录。

## 当前能力

- 连接AI替身 HTTPS 服务，Token 保存在 iOS Keychain。
- 同步目标与谏言，新增目标，提交问题，直接问策。
- 获得用户授权后读取近 7 天及未来 21 天日历、联系人快照、一次性位置。
- 通过系统分享扩展接收网页、聊天选中文本和文档文字。
- 本地待发送队列使用 Complete File Protection，联网后补传。
- 同步谏言列表；仅在服务器授予跨端提醒 claim 后显示 iOS 本地通知，并反馈采纳、撤下和结果核验。

iOS 不允许普通 App 全局读取其他 App、微信完整历史、通知中心或持续后台屏幕内容。因此，
本版以系统分享扩展、日历授权和用户主动提交作为可信入口。后台刷新由 iOS 决定，不保证准时；
真正的即时远程唤醒还需服务器增加 APNs 推送，这是后续项。

## 云端地址

专用 VPS 的可信 HTTPS 地址为 `https://example.invalid`，App 已预填该地址。只有在该服务器已经：

1. 部署AI替身后端；
2. 配置 Apple 信任的 HTTPS 证书（推荐绑定域名，不推荐直接给 IP 申请证书）；
3. 配置远程访问 Token；

之后，真机才能连接。客户端不会跳过 TLS 校验，也不会把 Token 写进源码。

## 在 Mac 上生成并运行

需要 macOS、Xcode 16+、Apple 开发者签名和 XcodeGen：

```bash
cd /path/to/mouchen/ios
brew install xcodegen
xcodegen generate
open MouchenIOS.xcodeproj
```

在 Xcode 中为 `Mouchen` 和 `MouchenShare` 选择同一个 Team，并把两个 target 的 App Group
改成你的开发者账号可用且一致的值。选择真机后运行。首次启动填写：

- 服务器：正式域名对应的 `https://...`
- 用户 ID：与 Android、Windows 完全相同
- Token：服务器生成的 Bearer Token

## 验收顺序

1. iPhone 显示“云端在线”。
2. 新建目标，Windows 与 Android 同一用户刷新后能看到。
3. 分享一个网页到“AI替身”，回到 App 点“立即同步并研判”。
4. 后端生成谏言，iPhone 刷新后出现列表；服务器将提醒 claim 分配给 iPhone 时才出现系统通知。
5. 在 iPhone 点击“采纳”，另外两端能读到同一台账状态。

Windows 环境不能编译、签名或产出可安装 IPA；必须在 Mac/Xcode 上完成上述真机闭环。

## 跨端提醒一致性

- `/v1/advice` 仅用于同步列表，不会直接触发 iOS 通知。
- iOS 使用 Keychain 中稳定的安装级 `device_id` 向服务器领取提醒；服务器决定 Windows、Android、iOS 中哪一端获得本次提醒。
- 领取前会检查通知授权；权限关闭时不领取，避免消耗服务器最多两次的提醒机会。领取后的本地异常会提交 `fail`，成功加入系统后才确认 `complete`。
- 未完成领取会保存在本机，App 崩溃或网络中断后继续确认；同一次领取使用固定通知标识，避免恢复时再次排队。
- 用户提交反馈后会撤销该建言在本机尚未处理的通知；旧版由列表生成的通知会在升级后清理一次。
