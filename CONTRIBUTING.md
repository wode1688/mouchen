# 参与贡献 / Contributing

感谢你愿意共建 AI替身。首版优先目标是：让主动建议核心更容易验证、更少误报，并始终保持清晰的隐私与行动边界。

## 开始

~~~powershell
git clone https://github.com/wode1688/mouchen.git
cd mouchen
python -m venv .venv
.\.venv\Scripts\python -m pip install -r backend\requirements.txt
cd backend
..\.venv\Scripts\python -m pytest -q
~~~

提交 Pull Request 前，请保证受影响平台的测试通过，并运行根目录的虚拟演示。客户端、测试、截图和日志只能使用虚拟或彻底脱敏的数据。

## 适合优先贡献的内容

- 新增不含真实个人信息的虚拟事件场景；
- 降低误报、重复提醒和错误紧急程度；
- 强化来源、同意、暂停和行动确认测试；
- 改进结果验证、文档、翻译和无障碍体验。

较大改动请先开 Issue，说明用户问题、最小方案和验证方式。安全问题不要公开开 Issue，请按 [SECURITY.md](SECURITY.md) 报告。

## 数据边界

代码、测试、截图和日志必须使用虚拟或彻底脱敏的数据。不要提交聊天记录、联系方式、账号、Token、Cookie、IP、服务器地址、设备标识、私人路径、真实商业数据或模型供应商凭据。

贡献即表示你有权提交相关代码，并同意其按 Apache License 2.0 发布。
