# 分享包维护与发布

发行源码在 distribution/office-agents，六个角色由 config/global-agents/common.md 和 roles.json 生成。三个发行技能已脱敏并自包含，日常维护直接修改发行技能，不从个人配置重新覆盖。

构建：`python scripts/build_office_agents_release.py --version 1.0.1`（每次内容变化递增版本）。输出 dist/office-agents-1.0.1.zip 及 .zip.sha256；自动重建发行目录 manifest.json。个人技能导入仅首次必要时使用 --refresh-skills，导入后检查脱敏及差异。

发 ZIP 时分享完整压缩包。Git 发布时仅将干净的 distribution/office-agents 目录连同清单发布到指定仓库的同名路径，接收者运行包内 update-from-git.ps1 指向该仓库。不要把整个个人 .codex 或本项目的运行状态发布出去。发布分支变化与 ZIP 保持同一版本。

Git 更新器只用接收者本机安装器处理远程数据，不执行远程脚本，只更新允许的角色和技能。修改安装器、增加技能或更改清单格式时需要接收者手工获取新版安装包。

发布前至少验证：ZIP可解压、TOML/技能格式、首次安装、升级、本地修改冲突保护、备份、工作区状态保留和篡改检测。真实远程Git拉取和客户端角色加载需在目标环境确认。安装包不注册计划任务，不启动项目心跳，不包含自动化授权。
