# AI 替身源码与多工具协作

应用源码统一放在 **[AI-Twin/](AI-Twin/)**，供多台电脑上的 Codex、Claude Code、Hermes 等工具共同维护。

| 入口 | 内容 |
|---|---|
| [应用说明](AI-Twin/README.md) | 产品、构建和运行说明 |
| [Windows 客户端](AI-Twin/desktop/) | 桌面程序源码 |
| [Android](AI-Twin/android/) / [iOS](AI-Twin/ios/) | 手机客户端源码 |
| [业务后端](AI-Twin/backend/) | 原业务服务源码 |
| [临时中转](AI-Twin/relay/) | 临时数据传输与电脑伴随程序 |
| [多工具协作](AI-Twin/docs/multi-agent-development.md) | 分工、独立目录和交接流程 |
| [交接记录](AI-Twin/docs/handoffs/) | 已完成内容、验证和下一步 |

## 开始开发

```powershell
git clone https://github.com/wode1688/mouchen.git
cd mouchen/AI-Twin
```

在需要修改的任务分支工作；多个工具同时写代码时各用独立任务目录。可从应用目录运行：

```powershell
python tools/ai_workspace.py --tool codex --task issue-101-sync
```

本次目录整理若尚未合并到主分支，先获取并切换到 `codex/cloud-relay-sync`，创建任务时使用 `--base origin/codex/cloud-relay-sync`。工具脚本不会自动启动 AI 或部署服务。

共同规则在根目录 [AGENTS.md](AGENTS.md)；Claude Code 通过 [CLAUDE.md](CLAUDE.md) 引用它。应用目录内的相对命令都应从 `AI-Twin/` 执行。任务、讨论和验收使用仓库的 Issues、Pull Requests 和 Actions。

源码沿用已有项目并包含本次中转与协作修改。尚未重新构建并证明其与某份已安装 EXE 完全一致；发布安装包前需另做构建和验证。

[贡献约定](CONTRIBUTING.md) · [安全说明](SECURITY.md) · [Apache 2.0 许可证](LICENSE)
