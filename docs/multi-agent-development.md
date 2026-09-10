# Codex、Claude Code、Hermes 共同开发

GitHub 仓库 `wode1688/mouchen` 保存共同源码、任务、修改历史和交接证据。每台电脑运行各自的开发工具，在任务目录里修改，再通过分支和 PR 汇合。计算继续在电脑或工具自身环境执行，临时中转服务器无需承载开发调度。

## 三个工具如何接入

| 工具 | 项目规则入口 | 使用方法 |
|---|---|---|
| Codex | 根目录 `AGENTS.md` | 在对应任务工作目录打开项目，启动新会话并读取当前任务记录 |
| Claude Code | 根目录 `CLAUDE.md` 引用 `AGENTS.md` | 在对应任务工作目录启动 Claude Code，读取同一份规则 |
| Hermes Agent（Nous Research） | 根目录 `AGENTS.md` | 从对应任务工作目录启动 Hermes，读取同一份规则 |
| 其他能访问本地文件和 Git 的工具 | 明确让它读取 `AGENTS.md` 和任务记录 | 若不支持自动读取项目规则，在该工具的项目入口引用这两个文件 |

根据 [Codex 官方文档](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[Claude Code 官方文档](https://code.claude.com/docs/en/memory) 和 [Hermes 官方文档](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files) 核对，日期为 2026-09-10。Hermes 存在优先级更高的 `.hermes.md`、`HERMES.md` 或 `AGENTS.override.md` 时，可能不再采用共享入口；本仓库不另建这些覆盖文件。已有本机覆盖应检查是否仍包含共同约定。

规则文件是工具读取的上下文，不是强制访问控制。不同工具的账号、费用、权限和原始聊天记录仍独立。项目交接依靠经过整理的任务记录、代码和测试结果，不依赖共享完整聊天历史。这里尚未实现跨产品自动调用、远程唤醒、全局任务锁或自动合并。

## 日常分工

例如将一次同步功能改进拆为三个任务：Codex 修改传输逻辑，Claude Code 修改界面，Hermes 检查文档和测试；也可以任意互换。每个任务的 Issue 写清目标、范围、执行工具、分支和验收标准，避免两个执行者同时占用同一任务。

GitHub Issue 模板 `AI 协作任务` 用于派工，PR 模板用于交付。多人或多工具应以 Issue 的当前记录核对负责人；认领评论只是约定，没有原子锁，离线工作也无法自动获知另一台电脑是否正在做同一件事。

## 为每个并行任务创建独立目录

需要 Git 和 Python 3.11 或以上。辅助脚本默认先获取远端更新，从 `origin/main` 创建任务分支。工作目录在主克隆旁边的 `<仓库名>-worktrees/` 中；主克隆的当前分支和未提交文件保持原状。

```powershell
python tools/ai_workspace.py --tool codex --task issue-101-sync
python tools/ai_workspace.py --tool claude --task issue-102-ui
python tools/ai_workspace.py --tool hermes --task issue-103-review
```

每条命令打印独立目录、分支和交接文件。将对应目录交给对应工具打开，一份目录同时只有一个写入者。脚本不启动或安装任何 AI 工具，不读取它们的凭据。任意工具名可用于 `--tool`，名字只需使用小写英文、数字或短横线。

若本次协作入口尚未合并到主分支，使用发布的功能分支作为起点：

```powershell
python tools/ai_workspace.py --tool codex --task first-task --base origin/codex/cloud-relay-sync
```

明确在本机当前已提交版本上开工时，可使用 `--offline --base HEAD`。这不会验证远端是否更新，开始任务前应另外核对负责人和基准版本。脚本发现目录、任务分支或同名交接记录已存在时停止，不覆盖旧工作。

同一任务需要并行时拆成子任务，每个子任务单独运行脚本。不同任务的公共接口仍需协调；独立目录隔离未提交文件，不能消除最终合并冲突。关于目录与分支的行为见 [Git worktree 官方说明](https://git-scm.com/docs/git-worktree)。

## 换电脑或换工具接着做

1. 原执行者更新任务交接文件：目标、完成内容、改动范围、实际检查、未完成事项和下一步。
2. 检查待提交文件，提交该任务的源码和交接文件，推送当前分支。将真实分支和提交号记入 Issue，停止原执行者。
3. 新电脑克隆或获取仓库，在独立克隆中切换到该任务的远端分支；不要为同一交接再次运行创建新任务的脚本。
4. 新工具先读 `AGENTS.md`、Issue、交接文件和实际 Git 状态，核对后继续。需要依赖或本机配置时在新电脑独立准备。

可把这段话直接交给任意工具：

> 在这个任务目录继续工作。先读取 AGENTS.md 和 docs/handoffs/ 中当前任务的记录，核对分支、最近提交和未提交修改。按记录完成下一步，保留其他任务的工作；结束时更新交接文件，说明实际测试和剩余事项。若尚未获取 Issue 内容，先说明当前依据的本地记录，不推断远端任务已被认领。

## 汇总与验证

提交 PR 后，由当前指定的集成者检查冲突、测试和需求，再将改动汇入主分支；负责人可以由用户或任意一个工具承担。其他电脑获取合并后的主分支继续开新任务。

`Relay tests` 验证中转组件，`Collaboration tools` 验证任务目录辅助脚本；GitHub Actions 是否已经运行、是否通过，必须从远端实际结果判断。若希望主分支强制要求检查或 PR，需要在 GitHub 另设分支规则；提交模板和工作流不会自动启用这些限制。

目前提供的是统一协作基础：规则入口、任务与 PR 模板、独立目录辅助脚本及交接记录。将来若需要“一句话自动派给几个 AI 并自动收结果”，再增加常用电脑上的调度程序，分别接入各工具经过验证的接口和登录状态。调度应具备任务状态持久化、领取冲突处理、执行超时和失败恢复；不能用共享 Markdown 文件假装已经完成这层自动化。
