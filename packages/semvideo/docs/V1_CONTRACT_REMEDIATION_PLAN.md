# V1 正式契约验收修复计划

状态：完成；最终 Standards/Spec 双轴复审无阻断
依据：2026-07-29 Standards/Spec 代码审查
基线提交：`836b1c6`

## 1. 复核结论

审查报告列出的三类正式验收阻断项均成立：

1. `windows/evidence-windows.jsonl` 和
   `semantics/segmentation-proposals.jsonl` 没有遵循 `CONTRACTS.md`
   已发布的稳定 Schema。
2. 按需导出会改写已经被检查点和清单登记哈希的
   `retrieval/segments.jsonl`，使已完成任务包失去内部一致性。
3. 模型最终失败时不会留下完整的模型运行和原始尝试记录。

其余中等级问题也成立：持久化读取方没有一致拒绝未来 Schema、取消用例返回
错误对象且接受终态任务、缺少结构化进度和 `--quiet`、Doctor 没有验证 PyAV
能力，以及真实 Worker 的取消与中断恢复缺少自动化覆盖。

CLI 直接读取任务文件的问题属于应用 Interface 违规，将在契约修复后收拢。
`processor.py` 的整体拆分属于低风险可维护性改进；本轮只提取由修复自然形成的深
Module，不进行与验收无关的全面重写。

## 2. 本轮确认的测试 Seam

以下 Interface 已由 `ARCHITECTURE.md`、`CONTRACTS.md` 和本次审查共同确定，
是本轮 TDD 的公共测试 Seam：

1. **视频处理应用 Module**：提交、列出、查询、等待、取消、恢复、重试和日志读取。
   CLI 只调用这些 Interface，不读取 `TaskStore`、Worker 日志或检索文件。
2. **Worker 执行 Interface**：
   `run_job(workspace, store, job_id, attempt_id, progress_sink=None)`。
   `progress_sink` 接收契约规定的结构化进度事件。
3. **任务包持久化 Interface**：`TaskStore` 和检索记录读取方必须拒绝高于支持范围
   的必需 Schema。
4. **正式片段 Interface**：列表、单条查询和导出通过应用 Module；按需导出不得
   改写完成任务包内已登记的事实。
5. **CLI 黑盒 Interface**：`process --wait --json` 的 stdout 只输出最终 JSON，
   stderr 输出 JSONL 进度；`--quiet` 关闭进度。

测试只从上述 Interface 观察行为，不断言私有辅助函数的实现细节。

## 3. 修复顺序与依赖

### R1：稳定产物 Schema

- 先增加红测，按 `CONTRACTS.md` 的字面字段验证证据窗口和语义提案。
- 证据窗口补齐 `source_video_id`、`contact_sheet_ids`、嵌套 `overlap` 和带版本
  的 `policy`。
- 语义提案改为单窗口一条记录，顶层直接包含 `segments` 和 `warnings`；片段使用
  正式候选边界引用，自然端点使用 `null`。
- 允许追加摘要和实体等可选字段，但稳定字段不得改名或嵌套在 `response` 中。
- 删除处理器中的重复时间线校验，统一调用语义规范化 Module 的验证 Interface。

### R2：失败模型调用审计

- 先增加两类红测：两次无效响应，以及提供商最终失败。
- 模型运行 ID、计时和审计路径在发起请求前确定。
- 无论成功、结构修复耗尽或提供商失败，都原子写入：
  - `model-runs/<id>.raw.json`：所有已经收到的响应尝试；
  - `model-runs/<id>.json`：状态、错误、累计用量、耗时、Trace 和原始记录引用。
- 失败审计文件不伪装成成功检查点；恢复仍重新执行 analyze 阶段。

### R3：按需导出不改变完成任务包

- 先增加红测，记录导出前后的检索文件、manifest、检查点和报告哈希。
- 已有正式渲染时只读取并复制已登记产物。
- 没有正式渲染时直接生成用户指定的任务包外文件，不再创建内部缓存、sidecar 或
  改写检索记录。
- 未登记的任务包内部输出路径会被拒绝，避免绕过清单登记。

### R4：版本、取消、进度和 Doctor

- 所有任务 JSON/JSONL、流水线恢复产物、manifest 和检索记录读取方拒绝
  `schema_version` 高于当前上限。
- `cancel_job` 返回 `JobSnapshot`；完成、失败、取消和中断任务拒绝新的取消请求。
- `run_job` 增加可选 `progress_sink`；应用层等待用例把状态变化投影为统一进度事件。
- CLI 增加 `--quiet`，JSON 模式的进度只写 stderr JSONL。
- Doctor 同时验证 `faster_whisper` 和可导入的 PyAV 能力，诊断逻辑位于应用层，
  CLI 只负责参数和输出。

### R5：应用 Interface 与 Worker 生命周期测试

- 新建查询/观察应用 Module，隐藏 TaskStore、日志游标、过滤和分页。
- CLI 删除对 `TaskStore`、Worker 日志和检索文件的直接访问。
- 增加真实子进程测试：
  - 运行中取消后到达 `cancelled`，Worker 退出且取消事件可见；
  - 杀死 Worker 后查询得到 `interrupted`，`resume` 创建新 attempt，并从有效
    检查点继续完成。

## 4. 验收条件

- 所有新增红测先失败、最小实现后通过。
- 原有 62 项测试继续通过。
- `git diff --check` 通过，已跟踪和待提交文件不含凭据。
- 产物示例通过稳定 Schema 断言。
- 按需导出前后完成任务包内已登记文件哈希不变。
- 失败模型调用留下可读取的失败模型运行记录。
- CLI 源码不再导入或实例化 `TaskStore`，不直接打开 Worker 日志或检索 JSONL。
- 真实 Worker 取消和中断恢复测试通过。
- 最终执行 Standards/Spec 双轴复审；低等级全面拆分若仍需进行，单独建立后续任务，
  不阻塞本次 V1 正式契约验收。

## 5. 实现结果

截至 2026-07-29，本计划 R1–R5 已落地：

- 稳定的证据窗口与语义提案 Schema 已替换旧内部结构。
- 模型的成功、无效响应修复和最终 HTTP/网络失败均保留脱敏尝试记录；运行记录
  使用契约字段 `raw_response_artifact_id`。
- 按需导出不再改变完成任务包；只有 `process --render` 登记内部视频产物。
- 任务状态、检查点、流水线恢复产物、manifest 与检索 JSONL 均执行版本上限检查。
- 取消、结构化进度、`--quiet`、PyAV Doctor、应用查询 Interface 已补齐。
- 真实 Worker 自动化覆盖运行中取消，以及异常终止后创建新 attempt、复用有效
  checkpoint 并完成。

长视频动态重叠多窗口仍是 `V1_STATUS.md` 明确记录的后续能力，不属于本次既有契约
缺陷的最小修复范围。`processor.py` 的进一步拆分同样作为维护性工作另行安排。

最终验证结果：`78 passed`，`git diff --check` 通过，项目改动凭据模式扫描无匹配。
Standards 复审无 Hard，Spec 复审无 P1/P2。后续非阻断维护项是拆分
`processor.py`，以及把 Adapter/Application 间的模型失败尝试字典改为类型化内部
信封。
