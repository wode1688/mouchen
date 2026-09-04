# 谋臣 Mouchen

> 重要的事，不必等你想起来。
> Things that matter should not depend on you remembering to ask.

谋臣是一个本地优先、由用户掌握最终决定权的主动式个人 AI 核心。它尝试把用户明确授权的信号与目标对照，发现偏差、风险与机会，给出带证据、可验证、有行动边界的建议。

当前仓库是可运行的公开核心，不是完整产品：它包含事件归一化、目标偏差检测、问题识别、建言评分、发言权限和上下文安全闸门，以及一个完全使用虚拟数据的离线演示。它不采集真实设备数据、不连接模型服务，也不会执行外部操作。

**项目状态：Engineering Alpha / 工程验证版。** 请勿将当前版本用于医疗、法律、金融或安全等高风险决策。

[3 分钟运行](#3-分钟运行) · [现在公开了什么](#现在公开了什么) · [参与谋臣](#参与谋臣) · [完整愿景](VISION.md) · [架构与边界](docs/architecture.md)

## 为什么做谋臣

今天的大多数 AI 都在等人提问，但真正重要的问题往往发生在你还没有意识到“应该问什么”的时候：

- 目标写下后，被日常事务逐渐淹没；
- 风险已经出现，却散落在不同信号里；
- 建议听起来正确，但没有证据，也没有结果回读；
- AI 能回答很多问题，却不了解你此刻真正要完成什么。

谋臣探索的是另一条路径：

~~~text
用户目标
  ↓
明确授权的最小信号
  ↓
证据与目标差异
  ↓
偏差、风险或机会
  ↓
建议 + 第一步行动 + 可验证结果
  ↓
用户采纳、拒绝或纠正
~~~

聊天仍然存在，但不再是唯一入口。

## 3 分钟运行

需要 Git 和 Python 3.12 或更高版本。演示不需要账号、API Key、数据库或网络。

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

演示会构造一个虚拟目标和一条虚拟时间投入事件，然后显示：

- 观察到了什么证据；
- 为什么产生建言；
- 建言分数与请求级别；
- 冷启动信任如何降低发言级别；
- 当前情境允许立即提示、延后还是拦截。

## 现在公开了什么

~~~text
虚拟事件 → 事件归一化 → 目标差异/问题检测
        → 建言候选 → 价值评分 → 发言权限
        → 同意、暂停、重复与场景闸门 → 结构化结果
~~~

公开仓库包括：

- 与 HTTP、数据库和模型供应商解耦的 Python 领域逻辑；
- 目标、事件、证据、预测、建言与反馈的数据模型；
- 时间投入偏差、承诺滑动和外部风险的确定性检测示例；
- 防止把网页、聊天或他人话语误判为用户事实的内容归因规则；
- 基于证据、价值、打扰成本和历史可信度的评分与发言权限；
- 同意撤回、暂停、重复建言和会议/驾驶/睡眠等上下文闸门；
- JSON Schema、自动测试与纯虚拟数据 Demo。

首版刻意不公开、也不随仓库分发：

- 任何真实用户记录、聊天、数据库、日志或分析结果；
- 设备采集器、账号认证、服务器地址、证书、密钥和部署配置；
- 私测客户端、生产服务、商业后台及高风险动作执行器；
- 未经安全审查的模型路由和第三方连接器。

这是一条产品与隐私边界，不是把完整产品伪装成已开源。公开核心可独立阅读、运行和验证；真实个人数据与生产基础设施继续隔离。

## 隐私与行动边界

- **逐项授权**：每类数据来源独立开启，默认不收集。
- **本地优先**：高敏感原始内容尽量留在用户设备内。
- **最少外发**：只有完成任务所需的最小脱敏片段可以进入云端分析。
- **证据可追溯**：建议应说明来源、时间、置信度和不确定性。
- **权限不越级**：重要程度不会自动变成执行权限。
- **高风险逐次确认**：付款、发消息、公开发布、账号修改、签署和重要删除必须针对当次动作确认。
- **结果必须回读**：发起操作不等于完成，系统需要核对真实外部状态。
- **用户可退出**：观察、云分析和执行都应能暂停；数据应能导出、纠正和删除。

详细安全报告方式见 [SECURITY.md](SECURITY.md)。

## 参与谋臣

我们欢迎四类同行者：

- **使用者**：在 Discussions 说明“你最希望 AI 提前发现什么问题、愿意授权什么、什么结果算有用”。请只使用虚拟或脱敏例子。
- **贡献者**：优先帮助扩充虚拟场景、降低误报、完善隐私测试、结果验证、文档和跨平台运行。
- **长期合伙人**：说明你能长期负责的结果、每周投入、过往作品，以及你对个人数据边界的判断。
- **投资合作**：谋臣仍处于工程 Alpha，不声称已验证规模化增长。认同“用户拥有数据、主动但不越权、用真实结果衡量 AI”的投资人，可在 Discussions 发起不含敏感信息的联系，再转入私下沟通。

不要在公开 Issue 或 Discussion 上传聊天记录、Token、密码、证件、电话、私人邮箱或商业秘密。提交代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 开源许可

公开代码采用 [Apache License 2.0](LICENSE)。名称、Logo、真实用户数据、私有测试服务、商业服务，以及未包含在本仓库中的代码或资产，不因本仓库开源而获得许可。

---

## English

Mouchen is a local-first, human-controlled core for proactive personal AI. With explicit permission, it is designed to compare bounded signals with a user's goals, detect drift, risks and opportunities, and produce evidence-backed advice with a verifiable outcome.

This repository is an **engineering alpha**, not the complete product. It contains provider-independent Python domain logic, schemas, tests, and an offline synthetic-data demo. It collects no device data, requires no account or API key, calls no model service, and performs no external action.

The long-term product is intended to follow a strict loop:

~~~text
permissioned signal → evidence → goal gap → advice draft
→ risk gate → human confirmation when required
→ bounded action → external-state verification → learning
~~~

The user owns the goals, data and final decision. Mouchen observes, verifies, advises and reviews. See [VISION.md](VISION.md) for the intended final form and [CONTRIBUTING.md](CONTRIBUTING.md) to join.
