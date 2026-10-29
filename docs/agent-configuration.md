# Agent 配置与使用说明

## 当前完成程度

六个角色、十项技能、18 项服务和五条工作流已形成可校验配置。统筹、钉钉、Teambition、开发、评审为 `configured`；GitLab 为 `paused_by_user`。`enabled=true` 只表示允许装配，不表示已部署进程或可以无人值守执行。

当前主机已具备 18 项 MCP 连接及 DWS 登录；15 项服务曾有只读探测成功。群聊、视频会议、AI 印仅发现工具，没有为了验证而创建业务对象。探测成功不是对所有操作权限的保证。

## 四份配置

| 文件 | 用途 |
| --- | --- |
| [agents.json](../config/agents.json) | 角色开关、角色文件、技能、候选服务、任务效果与执行边界 |
| [mcp-catalog.json](../config/mcp-catalog.json) | 真实工具名、人工分类的效果、归属、历史验证及限制；不存令牌 |
| [workflows.json](../config/workflows.json) | 五条任务模板、依赖、交付目标及必需输入 |
| [approvals.json](../config/approvals.json) | auto/ai/user 模式、必须用户审核的动作、有效期与 AI 门槛 |

技能中的 `effects` 表示能处理的任务效果，工具单独标注 `read/write`。任务效果 `local_write` 用于开发的本地代码修改，不授予业务 MCP。写技能也可只按 `read` 装配，用于定位目标；只有选择 `write` 才附带候选写工具，且不代表已有任务授权。

## 工作流选择

| 模板 | 用户请求示例 | 角色与交付 |
| --- | --- | --- |
| `office-query` | 查询通讯录人数、今天的日程 | 统筹→钉钉→统筹；返回口径和证据 |
| `project-query` | 查询某项目未完成任务 | 统筹→Teambition→统筹；返回任务与范围 |
| `document-to-tasks` | 按指定需求文档在指定项目建任务 | 读取→规划→创建→查询核验；返回任务链接 |
| `office-action` | 为明确参与者预约明确时间的会议 | 定位与授权→执行→验证；返回对象与验证层级 |
| `local-code-change` | 在本地仓库实现并评审一项需求 | 统筹→开发→评审→统筹；返回本地变更与证据 |

模板中出现“授权”字段只表示所需输入，填一个字符串不能授予访问权。真正执行时必须来自可信用户上下文与策略引擎。简单查询不创建项目任务，也不会默认向钉钉再次发送消息。

## 配置工具

```powershell
python scripts/agents.py validate
python scripts/agents.py plan document-to-tasks
python scripts/agents.py assemble dingtalk dingtalk-office-query
python scripts/agents.py assemble teambition teambition-task-sync --effect write
python scripts/agents.py assemble developer implement-change --effect local_write
```

`validate` 检查角色/技能文件、服务归属、显式工具清单、版本和任务依赖。`assemble` 输出指令和候选工具，供后续运行时加载；引用的共同协议需运行时按任务补入，参数 Schema 应从实时工具发现加载。输出不包含工具调用或认证材料。`plan` 只预览模板，不读取业务数据、不分派真实任务。

装配器会拒绝暂停角色、跨角色借用服务、只读技能请求写工具、未知工具通配符和无效依赖。现有 [执行器](executor.md) 和 [调度器](scheduler.md) 已连接批准、持久化、分派与验收；真实模型、授权签发、生产客户端及认证渠道需另行注册，它们不是安全沙箱。

## 写入与结果验证

现有请求明确对象和动作时，不重复询问业务信息。执行按 [auto/ai/user 批准策略](approvals.md) 判断：auto 在授权内直接执行，ai 等待独立审查，user 展示具体动作后等待任务用户；批准不增加资源或工具权限。

任何工具调用都检查业务返回值，不能只看 MCP 外层 `isError`。结果分为：`discovered`、`read_probe_passed`、`write_acknowledged`、`readback_verified`、`unknown`。MCP 不提供回读能力时保留回执和限制，按验收标准决定是否足够；不能凭成功文字推断最终业务效果。

通讯录曾出现 `12063` 权限错误，仅限员工属性字段查询。基础个人信息和部门人数查询成功；不能把这两种情况混为整个服务不可用或全权限可用。

## 后续运行时入口

先选模型并设置费用预算，接入任务/结果协议校验与持久化；再用实时工具发现把候选名映射到 SDK 工具，按每次任务筛选资源与动作。当前 Codex 的 OAuth 凭证由其自身存储管理，不复制到仓库、提示词或任意代码工作区。

DWS 作为钉钉角色的受控适配器：验证版本和 Schema，以参数数组执行，动态解析安装路径及身份。普通模型进程不直接拥有任意 Shell 权限，也不因 MCP 权限错误而换用 DWS 越权。
