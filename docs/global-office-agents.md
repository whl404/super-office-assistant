# 全局角色、项目规则与绑定

源码位于 config/global-agents/common.md 和 roles.json。运行 `python scripts/install_global_agents.py` 安装六个个人角色到用户目录 `.codex/agents/`，更新全局 AGENTS.md 的带标记路由段；修改前备份，不改变模型、MCP、审批配置或监听开关。

角色包含统筹、开发、评审、Teambition、GitLab、钉钉。全局角色不硬编码项目ID、成员和GitLab实例；遵守父会话权限及委派限制。项目角色注册表 config/agents.json 仍用于本仓库自己的运行时，不等同于 Codex 全局角色发现机制。

执行时按目标仓库绝对路径匹配用户目录 `.codex/workspace-monitors/*/config.json`，读取目标仓库适用的 AGENTS.md，再读取项目 state.json。执行模式与前后端开发范围独立。无绑定不推断群聊、收件人或外部项目授权。

新增工作区时创建独立绑定（workspace.path/repository、Teambition项目、群、人员、执行和交接政策），建立独立 state.json；不要复制旧任务ID或消息游标。任务基线首次完整查询后建立，不把旧任务当新任务。监听需要独立创建或启用自动化。

CPQ 已有绑定继续使用；仓库根目录 AGENTS.md 保存代码与分支规则。个人交接技能沿用现有安装。CPQ 心跳保持暂停；安装或普通会话不得启动监听。

验证层级：TOML解析、六个角色名称/规则、JSON绑定和技能文件存在性可本地检查；当前聊天不能仅凭文件写入证明客户端已加载新角色。需在重新加载后的会话核验实际可用角色。角色规则不是程序级拦截，也不启动常驻后台进程。
