# 任务与结果协议

## 标识和可信边界

`run_id` 是整次目标执行，`task_id` 是逻辑子任务，`attempt_id` 是一次执行尝试；重试更换 attempt，保留 task。`plan_version` 防止旧计划结果推进新计划。时间使用 ISO 8601 并包含时区，内部推荐 UTC，用户展示使用 Asia/Shanghai。

身份、授权、预算、允许资源和工具来自可信运行上下文。以下任务 JSON 是业务载荷，不能通过模型修改来扩大权限。协议版本首版为 `1.0`；未知版本拒绝执行。

## TaskEnvelope

```json
{
  "protocol_version": "1.0",
  "run_id": "run-demo-001",
  "task_id": "task-implement-001",
  "attempt_id": "attempt-001",
  "plan_version": 1,
  "agent_id": "developer",
  "skill_id": "implement-change",
  "effect": "local_write",
  "goal": "为订单服务增加符合既有权限规则的导出接口",
  "input_refs": ["artifact:req-v1", "artifact:repo-context-v1"],
  "acceptance_criteria": [
    {"id": "AC-1", "text": "筛选条件符合需求快照"},
    {"id": "AC-2", "text": "既有权限校验适用于导出接口"}
  ],
  "depends_on": ["task-repo-001"],
  "resource_refs": {"repository": "repo:demo-orders", "base_sha": "fixture-base-v1"},
  "expected_outputs": ["commit", "diff", "test_evidence"],
  "execution_context_ref": "context:run-demo-001/developer",
  "deadline_at": "2026-10-30T18:00:00+08:00"
}
```

以上为模拟数据，不可直接发往真实系统。全部字段必填；无依赖/验收项/资源时使用空数组或对象。研发任务必须有验收项和确定的基础 SHA，实际 SHA 必须是仓库返回的有效提交标识。每个 `input_ref` 都需能解析且处于当前 Agent 的可见范围。

`skill_id` 必须属于指定角色，`effect` 为 `read / write / local_write` 且包含在技能声明的效果中。`local_write` 允许分配隔离工作区能力，不授予业务 MCP；`write` 只是动作类别，不能替代用户授权。技能及工具映射来自可信注册表，不接受任务中临时添加工具名来扩大权限。

`execution_context_ref` 由引擎解析，不从模型提供的地址获取授权。上下文包含发起人、组织、资源范围、允许动作、预算、工具策略和工作区。工具代理对每次实际调用重新核验。

## ResultEnvelope

```json
{
  "protocol_version": "1.0",
  "run_id": "run-demo-001",
  "task_id": "task-implement-001",
  "attempt_id": "attempt-001",
  "plan_version": 1,
  "agent_id": "developer",
  "status": "succeeded",
  "summary": "已提交接口实现及测试，等待独立评审",
  "artifact_refs": ["artifact:commit-v1", "artifact:test-run-v1"],
  "operation_refs": [],
  "acceptance_results": [
    {"criterion_id": "AC-1", "outcome": "passed", "evidence_refs": ["artifact:test-run-v1"]},
    {"criterion_id": "AC-2", "outcome": "passed", "evidence_refs": ["artifact:test-run-v1"]}
  ],
  "issues": [],
  "handoff_requests": [
    {"agent_id": "reviewer", "reason": "请基于提交进行独立验收", "input_refs": ["artifact:commit-v1"]}
  ]
}
```

全部字段必填。`status` 枚举为 `succeeded / failed / blocked / cancelled`。`acceptance_results.outcome` 为 `passed / failed / unknown / not_applicable`，不适用必须解释原因。开发返回的通过只属于自检，不能替代评审结论。

`issues` 元素含 `code`、`message`、`retryable`、`evidence_refs`；阻塞还需 `required_input` 和 `resume_condition`。建议错误码：`MISSING_INPUT`、`AMBIGUOUS_RESOURCE`、`AUTH_REQUIRED`、`PERMISSION_DENIED`、`CAPABILITY_UNAVAILABLE`、`REMOTE_STATE_UNKNOWN`、`CI_FAILED`、`BUDGET_EXCEEDED`。运行时拒绝未知字段、越权资源和与尝试记录不匹配的返回。

引擎校验标识、版本、当前租约及产物可解析性，统筹检查语义。成功结果缺少必需产物或验证证据时不能推进依赖任务。`handoff_requests` 只是建议，没有调度权限。

执行器内部 Outcome 另有 approval_id。适配成 ResultEnvelope 时，将 AI_REVIEW_REQUIRED / USER_APPROVAL_REQUIRED 和 approval_id 映射到 issues 的 required_input，保存工作流恢复点；该 ID 仅引用审核记录，模型不能用它自行批准。批准策略见 [操作批准](approvals.md)。

## ArtifactRecord

每个产物记录：`artifact_id`、类型、存储引用、创建时间、生产任务及 attempt、内容版本或摘要、可见范围、来源对象 ID。代码与评审产物必须记录提交 SHA，CI 产物还需 pipeline ID 与 pipeline SHA。日志保留命令、退出码和必要输出，过滤密钥及无关个人资料。

业务操作证据额外记录 `verification_level`（`discovered / read_probe_passed / write_acknowledged / readback_verified / unknown`）及 `verification_limitations`。某个接口探测成功不代表整个服务具有全部权限；只收到写入回执不能冒充独立读回核验。办公任务没有提交 SHA 时按业务对象版本及操作记录验证，不强制伪造代码字段。

首版类型：`requirement_snapshot`、`plan`、`teambition_task`、`repo_context`、`commit`、`diff`、`test_evidence`、`review`、`merge_request`、`ci_evidence`、`notification`。链接只是引用，不代表产物已验证存在。

## OperationRecord

业务写入保存 `operation_id`、逻辑幂等键、run/task/attempt、目标 ID、参数摘要、授权策略版本、预期效果、实际结果及状态。

状态为 `pending / confirmed / rejected / unknown`。重试沿用同一逻辑幂等键；内容或目标发生变化则产生新操作并重新检查授权。消息发送不能仅靠文本判断是否重复；保存消息 ID，并结合服务能力确定是否可查询、重试或进入人工对账。

## 首版持久化实体

| 实体 | 主要内容与约束 |
| --- | --- |
| `runs` | 身份、目标、状态、版本、预算、取消标记、恢复点 |
| `plans` | run + plan_version 唯一；需求快照、验收标准、任务 DAG |
| `tasks` | 角色、状态、依赖、输入和验收，关联计划版本 |
| `attempts` | 尝试、租约代号、时限、结果，不覆盖历史尝试 |
| `artifacts` | 内容引用、版本、来源、权限范围 |
| `operations` | 幂等键唯一；写入前后结果及授权记录 |
| `events` | 组织 + 来源 + 事件 ID 唯一；关联业务对象版本 |
| `waits` | 问题/授权 ID、允许回复者、恢复状态、期限 |
| `outbox` | 与状态变更同事务保存待发送事件及重试状态 |
| `approvals` | 精确动作及预览摘要、策略、审核模式、状态、期限和审核者 |
| `approval_events` | 自动/AI/用户决定及撤销的审计记录 |
| `operation_approvals` | 已执行操作与批准记录关联 |

数据保留期限、备份与删除策略在试点配置中明确；不能用“永久记忆”代替项目可控的数据生命周期。
