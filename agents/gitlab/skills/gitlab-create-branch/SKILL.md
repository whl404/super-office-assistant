---
name: gitlab-create-branch
description: 先 fetch 并核对远程同名分支，再从最新远程 master 创建需求 feature/、生产补丁 hotfix/ 或发布 release/ 分支；不提交代码、不覆盖已有分支。
---

# 创建远程任务分支

输入明确的仓库/remote、项目及 Teambition 任务号或日期命名方式。基础分支固定为 master，不使用当前本地 HEAD、旧的本地 master 或项目默认分支替代。

需求分支必须从 fetch 后最新的远程 master 创建。提测目标 develop 与创建基础是两个独立概念；遇到 master/develop 历史分叉或 MR 差异包含历史变更时如实报告，不得为了缩小差异将需求分支改为从 develop 创建或移植到 develop 基础。

## 分支名称

- 通用前缀按用途选择：需求分支 `feature/`，生产补丁 `hotfix/`，发布分支 `release/`。不得用 `codex/` 替代；用途不明确时先澄清，不把生产补丁或发布当作普通需求。
- 需求有 Teambition 任务号时使用 `feature/{任务号}`，例如 `feature/CPQ-728`；生产补丁对应 `hotfix/{任务号}`。保留任务前缀，去除输入首尾空白；任务号应为项目前缀加数字编号，不从历史任务猜测。
- 发布分支使用 `release/{发布标识}`，后缀复用用户指定的日期、版本或项目发布约定；本规则仅固定前缀，不擅自生成发布标识。
- 使用日期命名时，格式为 `feature/{YYYYMMDD}{英文序号}`，例如 `feature/20261030a`、`feature/20261030b`。日期按用户时区 Asia/Shanghai 的当前日期计算；英文序号采用小写 a、b、…、z、aa、ab…，从 a 开始选择未占用名称。
- 未提供任务号或命名方式时，向统筹请求用户选择，不自行改用 codex/ 等前缀。用户已指定完整名称时核对上述规则，不悄悄改名。

## 执行顺序

1. 核对仓库 remote、GitLab 实例和项目 ID。多个 remote 有歧义时请求明确选择，不仅凭项目显示名称定位。
2. **先 fetch，再检查分支。** 本角色 `local_execution=none`，由统筹安排开发 Agent 在已授权仓库执行 `git fetch <remote>`，回传 remote URL、执行结果及更新后的远程 master SHA。参数以数组传递。没有本地仓库或 fetch 执行能力时返回具体缺口；fetch 失败不得继续，也不能以 GitLab API 查询代替用户要求的 fetch。无需切换或重置本地分支，不清理工作区。
3. 确认远程 master 存在；用 get_branch（branch_name=master）读取实时 SHA，与 fetch 后的远程跟踪 SHA 核对。若已变化，重新 fetch 并核对；持续变化时停止并报告。冻结该 master 完整 SHA 为本次创建基础。
4. **检查远程是否已有同名分支。** 用 get_branch 查询精确目标名，不能只凭本地分支列表判断。只有明确的不存在响应才算可创建，网络/权限错误不算不存在。
   - 任务号分支或用户明确指定的完整名称已存在：返回已有分支及 SHA，说明未新建；需要复用还是改名由用户决定，不覆盖、不重置，也不声称已有分支基于本次 master。
   - 自动生成的日期名称已存在：按英文序号继续检查，选择首个明确不存在的名称；超过 100 次候选检查时报告已占用范围并请求用户指定序号，不无限查询。
5. 按执行器授权和审批策略调用 `mcp__gitlab__create_branch`，参数为 `project_id`、`branch`、`ref`；ref 显式传入已冻结的远程 master SHA。审批参数应是最终未占用名称。生产适配器须先发现 branches 工具组并注册实时 Schema。
6. 创建后用 get_branch 回读名称与 commit SHA，确认等于冻结的 master SHA。创建期间发生同名冲突时重新查询并报告，不覆盖；写入超时或响应丢失时先对账，不自动换序号再次创建，不把其他人创建的同名分支认作本次成功。

输出项目 ID、remote、fetch 结果、分支名、master 基础 SHA、实际 SHA、创建/已存在/结果未知状态及回执。远程分支创建成功不等于本地已 checkout。删除分支、修改保护规则、修改默认分支、代码提交和推送不属于本技能。
