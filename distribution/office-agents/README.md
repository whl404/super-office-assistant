# Office Agents 分享包

六个个人全局角色：office-orchestrator、office-developer、office-reviewer、office-teambition、office-gitlab、office-dingtalk。包含 development-handoff、create-test-mr、create-prod-mr 三个配套技能。开发和评审流程内置在角色中。

**默认关闭自动更新。** 安装后不会后台检查或拉取 Git，不创建定时更新任务；只有用户手动执行更新命令才更新。定时更新须由接收者另行明确开启。

## 首次安装

需要 Python 3.9+（命令为 python），以及支持个人自定义 Agent 的 Codex 客户端。安装脚本无第三方依赖，不联网、不运行模型、不修改账号配置或监听状态。

解压后进入本目录，先检查，再安装：

```powershell
python install.py check
python install.py install
# PowerShell 包装入口也可用： .\install.ps1 -Mode install
```

默认写入用户目录 ~/.codex/agents 和 ~/.codex/skills。自定义位置用 --codex-home；该位置必须与实际客户端配置目录一致。已有同名且内容不同的非本包文件会阻止安装，不擅自覆盖。

重新加载客户端或打开新聊天，验证六个角色可见。可先请求“使用 office-orchestrator，只读检查当前工作区规则与绑定，不修改业务数据”。实际委派仍受客户端及会话限制；安装文件不证明客户端已加载，也不启动后台服务。

## 工作区接入

1. 自行连接并授权所需 Teambition、GitLab、钉钉工具；DWS 和 glab 仅在对应操作需要时自行安装登录。包内无账号、令牌或连接配置。
2. 复制 templates/workspace.json 到 ~/.codex/workspace-monitors/你的工作区/config.json，填写真实仓库路径、项目、群与成员。不要使用示例空值执行任务。
3. 复制 templates/state.json 到同目录 state.json，只用于首次建立状态，更新时不要覆盖。
4. 将 templates/AGENTS.md.example 中适用的要求合并到目标仓库 AGENTS.md，不覆盖已有规则。按项目填写基线分支、构建命令、交接等。
5. 默认 human_review；安装不授权自动合并、发消息、部署或建立监听。按项目逐项确认后配置。
6. 需要监听时在 Codex 单独创建自动化，指定工作区、时间范围、基线及批准规则；本包没有轮询服务或可执行的全流程调度器。

数据库规则默认要求 mysql_dev 读、mysql_dev_write 逐操作确认写、保留 prompt、禁用 DDL。若环境不具备这些工具，应报告缺口，不换连接绕过。
MR 技能采用本包约定：测试目标 develop，标题=完整分支名，描述 fix:任务号|标题|链接。项目不采用此约定时先调整技能与绑定，不直接执行。创建生产MR不等于授权发布。

## 更新

从可信来源获取新版本 ZIP，解压到新目录，然后：

```powershell
python install.py check
python install.py update
# 或 .\install.ps1 -Mode update
```

更新依据 ~/.codex/office-agents/install.json 的已安装哈希核对文件，仅替换本包管理的角色和技能。保留工作区绑定、state.json、AGENTS.md、config.toml、账号、业务代码和自动化开关。模板不自动覆盖到工作区。

更新前备份到 ~/.codex/office-agents/backups/时间戳。若用户改过某个待替换或待删除的管理文件，则整次更新停止并列出冲突；手工比较新旧版本、合并或恢复原版后重试，没有强制覆盖选项。请将长期自定义优先放入项目规则，减少角色更新冲突。

写文件失败会回滚本次已修改的角色/技能。需要手工恢复历史版本时，停止相关任务，按备份 changes.json 恢复旧文件、移除标记为原本不存在的新增文件，并恢复 previous-install.json 为安装记录；不要删除整个用户配置目录。禁止直接用旧包覆盖新版本。

## 从 Git 自动更新

维护者将本包完整目录（含 manifest.json）放到仓库 distribution/office-agents 并提交；接收者保留解压后的本地安装器，首次安装后运行：

```powershell
.\update-from-git.ps1 -RepoUrl https://YOUR-HOST/YOUR-TEAM/YOUR-REPO.git -Ref main -CheckOnly
.\update-from-git.ps1 -RepoUrl https://YOUR-HOST/YOUR-TEAM/YOUR-REPO.git -Ref main
```

main 为更新通道，也可以指定固定发行 tag。私有仓库使用系统 Git 已配置的凭据，不能在 URL 中写密码或令牌。只有你明确指定并信任的仓库才应作为更新源；角色和技能文本会影响 Agent 行为，校验和不是签名。

脚本下载指定分支最新快照，使用本地 install.py 检查版本、清单、备份和冲突后更新。不执行下载的安装器、构建脚本或其他代码，只允许更新这六个角色与三个技能文件；新增技能或安装器升级需手工下载新包。相同版本内容不同会拒绝更新，发布者必须递增版本。

需要定时自动检查并更新时，可自行在 Windows 任务计划程序中建立每日任务，以本地 update-from-git.ps1 为入口并带上述固定 RepoUrl/Ref；在没有活跃开发任务时运行，并保存输出日志。包本身不创建计划任务，不恢复工作区心跳。更新后新会话加载新配置，已有会话不保证热更新。

## 分享与校验

分享整个 ZIP；manifest.json 记录发行文件 SHA-256，外部 .sha256 文件用于核对 ZIP。校验和用于检测损坏，并非发行者数字签名。个人项目资料、运行历史、授权和账号不在包内。

源码维护者更新角色/技能后提升版本号，重新构建和验证安装、更新、冲突保护，再发布完整新ZIP；无需对方重新填写工作区资料。
