---
name: teambition-project-query
description: 查询 Teambition 当前用户参与项目、任务详情、进展、成员和工作流状态，或核验前次任务写入结果；不创建或修改项目数据。
---

# 项目与任务查询

本项目逻辑服务 ID 为 `dingtalk-teambition`，当前宿主工具前缀为 `mcp__teambition__teambition_`。先发现当前用户参与的项目，再核对项目详情；已有任务链接可直接定位任务，保留任务与项目 ID。

- 查询任务前明确项目、执行人、时间和状态条件。编写或修正 TQL 前读取 docs_get（path=task/tql）；陌生能力先 docs_search 再 docs_get。已知 ID、my 模式或结构化条件可直接查询，翻页保持原条件与返回游标。
- 查询统计需要遍历完整分页并去重；只取一页时说明范围。无权限、未开启进展功能与空结果分别报告。
- 状态以项目工作流真实返回为准。状态更新支持 taskflowstatusId 或 tfsName；先查询任务类型状态，task_tfs_available 仅为本地推算，不是后端权威结果。
- 写入核验对照本次预期字段，关注用户并发改动；不能为“修正统计”顺手更新任务。
- 任务和项目链接优先使用工具返回值；没有任务 URL 时，按已确认 ID 组成 `https://www.teambition.com/project/{projectId}/tasks/{taskId}` 并说明未做浏览器验证。链接不能代替查询成功证据。

输出查询事实、来源、统计范围、完整性及限制。需要写入时向统筹提出动作请求，由任务同步技能执行。
