# 多台电脑统一修改 AI 替身

所有平台继续使用同一个仓库：`https://github.com/wode1688/mouchen`。Windows、Android、iOS、后端和临时中转分别在各自目录，修改通过 Git 提交记录保留。

## 第一次在电脑上使用

```powershell
git clone https://github.com/wode1688/mouchen.git
cd mouchen
git config user.name "你的 GitHub 用户名"
git config user.email "GitHub 提供的 noreply 邮箱"
```

需要上传修改的电脑应通过 GitHub CLI、Git Credential Manager 或 SSH 登录。GitHub 账号密码不能直接用于 Git 推送。参见 [GitHub 认证说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github)。不要把密码写进仓库地址或配置示例。

## 每次开始修改

先检查有没有尚未保存到 Git 的修改，再同步主分支并新建自己的功能分支：

```powershell
git status
git switch main
git pull --ff-only
git switch -c feature/describe-your-change
```

如果 Git 提示有未提交修改，先提交或妥善暂存，不要用强制覆盖来“解决”。两台电脑同时做不同事情时使用不同分支，避免互相覆盖。

## 修改完成后

运行受影响组件的测试和根目录虚拟演示；检查修改中不含个人数据或真实配置，再提交具体文件：

```powershell
git diff
git add path/to/changed-file
git commit -m "说明这次解决了什么问题"
git push -u origin HEAD
```

在 GitHub 创建 Pull Request，写清问题、变化和验证结果。测试通过后合并，再让其他电脑拉取最新主分支。较大改动先开 Issue 说明方案，遵循根目录的 [贡献说明](../CONTRIBUTING.md)。

## 换电脑继续同一个修改

先在电脑 A 提交并推送当前分支。电脑 B 中执行：

```powershell
git fetch origin
git switch --track origin/feature/describe-your-change
```

如果该分支已经存在，切换后用 `git pull --ff-only` 更新。尽量不要两台电脑同时改同一分支；需要同时修改时，各自开分支后通过 Pull Request 合并。

## 记录在哪里

| 记录 | 位置 |
|---|---|
| 每次代码修改、时间、提交者 | GitHub 的 Commits，或 `git log --oneline --graph --all` |
| 修改原因、讨论和验证结果 | Issues 与 Pull Requests |
| 自动测试结果 | GitHub Actions |
| 部署版本 | 使用已验证提交或标签，部署记录保存准确提交号 |

本次临时中转组件使用 `codex/cloud-relay-sync` 功能分支。对应测试工作流为 `Relay tests`，Windows 和 Linux 分别验证 Python 3.11、3.12。工作流文件存在不代表远端测试已经执行，需在 Actions 中确认本次提交的结果。

## 配置和数据放在哪里

每台电脑独立安装依赖、创建配置。令牌、模型密钥、真实服务器地址、证书、数据库、日志和个人归档留在本机或服务器的私有目录。示例配置只有占位值。

源码目录不用于存放同步的业务数据；GitHub 不充当个人数据库。Windows DPAPI 配置和加密数据库绑定原 Windows 用户，不能依赖 GitHub 复制到其他电脑后直接解密。

代码合并不会自动升级正在运行的服务器，也不会自动覆盖已安装的 EXE。部署前选择经过测试的提交，保留回退版本；见 [中转部署说明](../deploy/relay/README.md)。
