# 任务追踪：本地 Markdown

本仓库的任务和规格（PRD）以 Markdown 文件形式保存在 `.scratch/` 中。

## 约定

- 每项功能使用一个目录：`.scratch/<feature-slug>/`。
- 规格文件为 `.scratch/<feature-slug>/spec.md`。
- 实施票据分别保存在 `.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 开始编号；不得把全部票据合并到一个文件。
- 分类状态记录在票据顶部附近的 `Status:` 行中，角色字符串见 `triage-labels.md`。
- 评论和讨论历史追加到文件末尾的 `## Comments` 标题下。

## 技能要求“发布到任务追踪器”时

在 `.scratch/<feature-slug>/` 下创建相应文件；目录不存在时一并创建。

## 技能要求“读取相关票据”时

读取用户给出的文件路径或编号对应的票据文件。

## Wayfinder 操作

Wayfinder 使用一个地图文件和每项决策对应的一个子票据文件：

- **地图**：`.scratch/<effort>/map.md`，保存目标、备注、决策索引、尚未明确内容和范围外内容。
- **子票据**：`.scratch/<effort>/issues/<NN>-<slug>.md`，从 `01` 开始编号，正文描述需要回答的问题。
- **类型**：票据顶部的 `Type:` 行，值为 `research`、`prototype`、`grilling` 或 `task`。
- **状态**：票据顶部的 `Status:` 行，使用 `open`、`claimed` 或 `resolved`。
- **阻塞关系**：票据顶部使用 `Blocked by: NN, NN`。列出的所有票据均为 `resolved` 时，本票据才解除阻塞。
- **Frontier**：扫描 `issues/`，选择状态为 `open`、未被阻塞且未被认领的票据；按编号选择第一个。
- **认领**：开始处理前先把 `Status` 改为 `claimed` 并保存。
- **解决**：把答案追加到票据的 `## Answer` 下，将 `Status` 改为 `resolved`，再向地图的 `Decisions so far` 添加摘要和链接。

地图只保存决策索引，不重复保存详细答案；每项决策的完整内容只存在于对应票据中。
