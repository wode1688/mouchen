# 谋臣 Mouchen

## 不等你提问的，可审计主动私人参谋

**谋臣面向高负荷的创业者和知识工作者。** 在用户逐项授权后，它把日历、通知、待办和工作上下文与长期目标对照，提前发现冲突、遗漏、风险和机会；给出证据与行动草稿，需要时等待用户确认，执行后核对真实结果。

> 大多数 AI 帮你回答已经想到的问题。谋臣要解决的是：在正确的时间，发现你还没来得及问、却正在影响结果的事情。

**产品状态：** Android-first 私测 Alpha；已有 Android、Windows 私测客户端，iOS 源码仍在验证。<br>
**本仓状态：** Developer Preview；当前仅开源平台无关的判断与安全核心，三个客户端均未公开。

[3 分钟体验公开核心](#3-分钟体验公开核心) · [开源范围](#本仓库到底公开了什么) · [最终形态](VISION.md) · [参与谋臣](#参与谋臣)

## 它不是另一个聊天机器人

| | 常见聊天 AI | 谋臣要成为的产品 |
|---|---|---|
| 起点 | 等用户发现问题并提问 | 在授权范围内持续比较现实与目标 |
| 输出 | 一次回答 | 带来源的判断、建议与行动草稿 |
| 行动 | 回答结束即结束 | 风险闸门、用户确认、有限执行 |
| 结果 | 对话听起来是否合理 | 外部状态是否改变、建议是否真正有用 |
| 权力边界 | 主要依赖一次提示词 | 授权可撤回，重要动作逐次确认，全程可追溯 |

谋臣的核心闭环是：

~~~text
明确授权的信号 → 本地过滤 → 长期记忆 → 局势分析
→ 建议 / 行动草稿 → 风险闸门 → 用户确认
→ 有限执行 → 外部状态回读 → 从结果中学习
~~~

价值不在于回答更多问题，而在于更早发现真正重要的事，同时把误打扰、权限、隐私和错误执行控制在可验证的边界内。

## 如果谋臣已经成熟

你告诉谋臣一个目标：

> 未来三个月拿到三个付费企业客户，同时不能让广告现金流失控。

在你的授权范围内，它会发现本周大量时间正在流向无关工作，指出某个高意向客户已经多日没有跟进，并在广告支出异常时先核实证据。它可以准备客户跟进和预算调整草稿，但不会擅自发送消息或修改账户。

你确认后，它只执行被允许的动作并回读平台真实状态。七天后，它核对回复率、预约数和现金流，判断这次建议究竟改变了什么。

最终价值不是“AI 和你聊了很多”，而是你更早发现问题、减少关键遗漏，并能证明哪些建议改变了结果。

> 以上是目标产品体验，不是当前公开仓库已经具备的完整能力，也不是未经验证的用户成绩。

## 产品今天做到哪一步

完整产品正在受控私测，当前事实与本仓库的公开范围必须分开看：

| 层级 | 私测产品现状 | 本仓是否开源 |
|---|---|---:|
| Android | Android-first 私测客户端与授权信号链路 | 否 |
| Windows | 桌面私测客户端与跨设备目标、建言、反馈链路 | 否 |
| iOS | SwiftUI 源码验证中 | 否 |
| 判断核心 | 事件归一化、目标偏差、问题检测、评分与信任控制 | **是** |
| 安全边界 | 同意、暂停、去重和会议/驾驶/睡眠等情境闸门 | **是** |
| 开发验证 | JSON Schema、18 项测试、纯虚拟数据离线 Demo | **是** |
| 完整服务端 | API、认证、存储、同步、模型路由与生产部署 | 否 |
| 真实动作执行 | 消息、付款、账号修改等外部操作链路 | 否 |

目前没有经过独立验证的规模化用户、留存、收入或付费数据。本项目不会把愿景、私测能力或验证目标写成已经取得的市场成绩。

## 本仓库到底公开了什么

**我们先开源“谋臣如何判断、何时开口、何时闭嘴”，而不是采集私人数据的客户端和操作真实账号的执行层。**

~~~text
backend/app/domain/   目标、证据、检测、评分、信任与安全闸门
backend/tests/        18 项核心测试
shared/               事件与建言 JSON Schema
examples/demo.py      不联网、只使用虚拟数据的演示
docs/                 架构与内容归因规则
~~~

当前公开能力包括：

- 将目标、事件、证据、预测、建言、反馈和结果表达为可检查的数据结构；
- 识别时间投入偏差、承诺滑动、外部风险及六类高优先级问题信号；
- 按证据价值、潜在收益、打扰成本和历史可信度决定是否应该开口；
- 防止把网页、聊天或他人话语误认成用户本人事实；
- 在未授权、暂停、重复提醒或不合适情境下延后或拦截建言。

没有公开的部分包括 Android、Windows、iOS 客户端，设备采集器，完整后端，账号认证，数据库，模型连接，跨设备同步，通知与动作执行，以及服务器、证书、密钥、真实数据和商业后台。这些部分涉及设备权限和生产安全，需要完成脱敏拆分与发布审查后再决定开放范围。

## 3 分钟体验公开核心

下面运行的是跨平台 Python Demo，**不是 Windows、Android 或 iOS 客户端**。它不需要账号、API Key、数据库或网络，只使用虚拟数据。

Windows（Python 3.12+）：

~~~powershell
git clone https://github.com/wode1688/mouchen.git
cd mouchen

python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python examples\demo.py

cd backend
..\.venv\Scripts\python -m pytest -q
~~~

macOS / Linux：

~~~bash
git clone https://github.com/wode1688/mouchen.git
cd mouchen

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python examples/demo.py

cd backend
../.venv/bin/python -m pytest -q
~~~

Demo 会展示一条虚拟事件如何形成证据、触发建言、完成价值评分，再经过冷启动信任和上下文安全闸门。

## 最终形态：由用户拥有的跨设备 AI 幕僚

谋臣最终不是另一个聊天框，而是一套贯穿手机和电脑的个人决策系统：只观察用户明确授权的最小信号；建立可查看、可纠正、可迁移、可删除的长期记忆；持续比较现实与目标；在真正值得打扰时带着证据建言；由用户决定是否行动；执行后读取真实结果并复盘。

用户拥有目标、数据和最终决定。谋臣负责观察、求证、建言和复盘。更完整的边界与路线见 [VISION.md](VISION.md)。

## 参与谋臣

- **使用者**：告诉我们你最希望 AI 提前发现什么问题、愿意授权什么、什么结果才算真的有用。
- **贡献者**：帮助扩充虚拟场景、降低误报、完善隐私测试和结果验证。
- **长期合伙人**：如果你愿意长期负责客户端、隐私安全、记忆系统或产品增长，请附上可验证作品和预计投入。
- **投资与产业合作**：如果你认同“用户拥有数据、AI 主动但不越权、价值由真实结果衡量”，欢迎通过 [Discussions](https://github.com/wode1688/mouchen/discussions) 建立公开联系，再转入私下沟通。

请勿在公开 Issue 或 Discussion 提交聊天记录、账号信息、Token、证件、私人联系方式或商业秘密。贡献代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 开源许可

公开代码采用 [Apache License 2.0](LICENSE)。名称、Logo、真实用户数据、私有测试服务、商业服务，以及未包含在本仓库中的代码或资产，不因本仓库开源而获得许可。

---

## English

**Mouchen is an auditable proactive private strategist for high-load founders and knowledge workers.** With explicit permission, it is designed to compare bounded signals with a user's goals, surface conflicts, risks, omissions and opportunities, prepare evidence-backed advice or action drafts, require confirmation when needed, and verify the real outcome.

The complete product is in controlled Alpha. Android, Windows and iOS client code is **not** included in this repository. This public Developer Preview contains only the provider-independent Python decision and safety core, JSON schemas, 18 tests, and an offline synthetic-data demo. It collects no device data, requires no account or API key, calls no model service, and performs no external action.

The user owns the goals, data and final decision. Mouchen observes, verifies, advises and reviews. See [VISION.md](VISION.md) for the intended final form and [CONTRIBUTING.md](CONTRIBUTING.md) to join.
