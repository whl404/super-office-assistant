# Super Office Assistant

主 Agent 统筹，钉钉、Teambition、GitLab、开发、评审 Agent 协作的任务自动化系统。

当前为 **v0.5 持久化调度版**。当前主机已配置 18 项钉钉/Teambition MCP 和 GitLab MCP。GitLab 已通过项目列表只读探测；六个角色均已启用。各工具的业务权限仍需执行时核验。

项目已实现配置装配、异步工具执行、三档批准、持久化 Agent/工具循环、依赖调度、产物、验收、输入/审批等待、后台唤醒、取消与中断恢复。真实模型、生产 MCP 客户端、钉钉消息/批准卡片及认证渠道尚未接入；当前宿主连接不会自动传给独立服务。

新增 auto/ai/user 三种批准模式，默认 75 个只读工具自动、15 个常规写工具由 AI 审核、17 个发送/创建及通用任务更新工具由用户审核。真实 AI 模型和用户审核界面尚未接入；缺少 AI 时转用户，不直接放行。

## 阅读顺序

1. [架构与设计决策](docs/architecture.md)
2. [工作流、状态与恢复](docs/workflow.md)
3. [任务与结果协议](docs/contracts.md)
4. [工具接入与授权](docs/integrations.md)
5. [第一版实现顺序与验收](docs/implementation.md)
6. [Agent 配置与使用说明](docs/agent-configuration.md)
7. [执行器、异常处理与恢复](docs/executor.md)
8. [操作批准策略与接口](docs/approvals.md)
9. [持久化调度器与完整闭环](docs/scheduler.md)

## Agent 定义与核心技能

| Agent | 角色指令 | 核心技能 |
| --- | --- | --- |
| 统筹 | [AGENT.md](agents/orchestrator/AGENT.md) | [任务统筹](agents/orchestrator/skills/orchestrate-delivery/SKILL.md) |
| 钉钉 | [AGENT.md](agents/dingtalk/AGENT.md) | 任务沟通、办公查询、限定范围写入、开放平台文档检索 |
| Teambition | [AGENT.md](agents/teambition/AGENT.md) | 项目查询、任务同步 |
| GitLab | [AGENT.md](agents/gitlab/AGENT.md) | [创建分支](agents/gitlab/skills/gitlab-create-branch/SKILL.md)、[提交测试 MR](agents/gitlab/skills/gitlab-submit-mr/SKILL.md)、[MR 交付](agents/gitlab/skills/gitlab-mr-delivery/SKILL.md) |
| 开发 | [AGENT.md](agents/developer/AGENT.md) | [需求实现](agents/developer/skills/implement-change/SKILL.md) |
| 评审 | [AGENT.md](agents/reviewer/AGENT.md) | [变更验收](agents/reviewer/skills/review-delivery/SKILL.md) |

这里的 `AGENT.md` 和技能目录由 [注册表](config/agents.json) 显式引用，不代表 Codex 或其他运行框架会自动加载。[配置入口](scripts/agents.py) 可以装配角色指令、所需技能与候选工具，但不执行模型或业务调用。

## 本地校验和预览

```powershell
python scripts/agents.py validate
python scripts/agents.py approvals
python scripts/agents.py plan office-query
python scripts/agents.py assemble dingtalk dingtalk-office-query
python -m unittest discover -s tests -v
python -m agent_runtime.demo
python -m agent_runtime.workflow_demo
```

当前 [MCP 能力清单](config/mcp-catalog.json) 选择 107 个常用工具作为候选，未列入的工具默认不分配；任务执行仍需实时 Schema、身份、资源及授权核验。共享配置没有凭证或业务明细。

## 默认试点范围

先支持办公查询、项目查询、文档到任务、明确授权的办公操作；本地代码交付需要明确仓库和验收标准。MR/CI 工作流模板已启用，实际执行仍需注册模型、工具适配器、授权签发器及验收器。用户已取消 DING 消息（应用授权）和工作通知（应用授权），不再列为待接入项。

## 已核对的接入资料

- [DWS 命令 Wiki](https://open.dingtalk.com/dingtalk-dev/wiki)：钉钉 Workspace CLI，属于钉钉 Agent 的工具层。
- [Teambition MCP 入口说明](https://open.teambition.com/docs/documents/695b28bcfca732ddb816bd7e)：业务 MCP 与文档检索 MCP 分开授权。
- [GitLab MCP Server](https://docs.gitlab.com/user/model_context_protocol/mcp_server/)：部署前核对实例版本、许可和实际工具。

能力以部署时的工具发现、当前命令 Schema 和账户权限为准。
