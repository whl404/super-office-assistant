---
name: gitlab-submit-mr
description: 提交合入 develop 的测试环境 GitLab MR，标题为当前完整分支名，描述为 fix:任务号|任务标题|任务链接；用于提测，不用于生产 MR、合并或发布。
---

# 提交测试 MR

依据用户的 create-test-mr 规则，适配本项目角色分工。创建本技能不代表立即创建 MR。

## 输入与固定格式

开发 Agent 提供用户指定仓库（未指定时当前仓库）的 remote、当前本地分支、HEAD SHA、工作区状态和推送证据。目标固定为 develop，标题等于完整源分支名（保留 feature/ 等前缀），描述只允许一行：

`fix:{任务号}|{任务标题}|{任务链接}`

不增加空格、Markdown 标题、验证段落或其他描述。任务号、标题、链接不得为空或包含换行和竖线；有冲突时请求用户提供符合格式的单行内容，不自行改写标题。生产 MR 不使用此技能。

## Teambition 信息

- 用户已给任务号时直接使用；否则向统筹返回 NeedInput，请认证渠道弹窗询问任务号（如 CPQ-728），不从分支名或历史任务猜测。
- 去除首尾空白，按项目前缀和数字编号解析；分支后缀如 _2 不属于任务号。
- 通过统筹交 Teambition Agent 只读核验项目和任务。该角色先按 docs.search/docs.get 阅读 task/tql，再使用 project.task-query 的 projectId 和 uniqueId = 数字 查询，核对返回编号、content、projectId、taskId。
- CPQ 的历史项目 ID 为 66b0353b1f1e659482e27da7（CPQ报价管理系统），使用前核对前缀与访问权限；其他前缀重新定位项目。
- 优先取返回的任务网页 URL，否则按实际 ID 组成 https://www.teambition.com/project/{projectId}/tasks/{taskId} 并注明未经浏览器验证。多匹配要求选择；空结果、权限错误、网络失败分别处理，不能编造资料。
- 唯一结果直接展示使用，不要求用户抄写确认。无法查询时由统筹收集修正任务号或用户提供的标题和 HTTP/HTTPS 链接。GitLab Agent 不借用 Teambition 凭据。

## 执行

1. 核对 remote 对应的实例与项目、源分支和 develop。detached HEAD、源分支为 develop、目标不存在或无可合并差异时停止。差异证据由开发 Agent 刷新远程引用后提供；证据不足时返回缺口。
2. 查询同项目、同源分支到 develop 的已打开 MR，分页查全。已有则核对标题和描述并返回链接；不符时展示具体差异，取得用户对更新已有 MR 的明确指示后才更新。
3. 未推送时交统筹安排开发 Agent 普通 push 已有提交并按需设置 upstream，不自动提交工作区修改、强推、重置或更改已有跟踪关系。说明未提交改动不包含在 MR 中。推送拒绝时返回原因，不擅自变基。
4. 核验远程源分支 SHA 与本地提交一致；完整研发工作流中还必须与评审 SHA 一致。展示项目、源/目标分支、标题和单行描述。用户要求创建 MR 即为业务授权，不额外要求重复口头确认；仍由执行器落实现有具体动作审批，技能不代用户批准。
5. 本角色 local_execution=none，使用已注册 GitLab MCP（原 create-test-mr 允许的 API 替代路径）。调用 create_merge_request：project_id、source_branch、target_branch=develop、title=完整源分支名、description=固定单行；创建普通 MR。缺少 MCP/适配器时返回阻塞，不自行改用 Shell/glab。
6. 不合并、自动合并、部署、删除源分支、指派评审或另发通知。创建失败或超时先查询对账；无法确定结果时返回 UNKNOWN，不重复创建。
7. 创建后 get_merge_request 复核源/目标分支、标题、描述和 head SHA，返回 URL、IID、SHA、复用/创建结果及已知冲突/CI 状态。CI 失败不抹去 MR 已创建事实，也不宣称交付验收通过。统筹通过具备 attach_artifact 能力的渠道附加链接；缺少该能力时直接返回链接。
