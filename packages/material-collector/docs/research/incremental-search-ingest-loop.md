# 查询—切分落库—充分性判断增量闭环研究

## 结论

该闭环在工程上可行，并且比“先搜到一个大候选池，再一次性下载和落库”更适合短视频素材采集。推荐的精确定义是：

```text
主题段 / QueryPlan
  → 冻结一轮 QueryBatch
  → 多平台搜索与元数据初筛
  → 只下载本轮新增且合格的候选
  → 调用切分落库
  → 等待落库进入终态
  → 读取带版本号的“有效片段快照”
  → 累计评估 required facets、替代数、时长、多样性与权利状态
  → 足够：完成
  → 不足且仍有预算：只针对缺失面向生成下一批检索表达
  → 预算耗尽：以 partial/exhausted 结束并报告缺口
```

关键限制是：**下一轮判断必须基于实际已经提交、可用、未重复、未隔离的素材片段，而不能基于候选标题、下载成功数或异步落库的 `accepted` 回执。**

本方案并不要求第一版就引入重量级工作流引擎。MVP 可以用关系数据库中的持久状态机和后台 Worker 实现；当任务需要跨进程运行数小时、频繁恢复或并行扩展时，再把同一状态机迁移到 Temporal 一类持久工作流引擎。Temporal 的 Workflow Execution 会持久保存状态并在故障后从最近事件恢复；Activity 适合承载搜索、下载、转码等非确定性副作用，并明确建议保持幂等和拆分大步骤以缩小失败重试范围。[Temporal Workflow Execution](https://docs.temporal.io/workflow-execution)；[Temporal Activities](https://docs.temporal.io/activities)

## 1. 为什么“每轮先落库，再判断”成立

现有设计已经区分“候选内容”和“素材片段”。这个区分决定了充分性判断应位于切分落库之后：

- 平台标题、描述和标签只能说明源内容可能相关，不能证明视频中存在可独立剪辑的片段。
- 下载成功只证明获得了源文件，不能证明切分器接受了片段，也不能证明片段满足质量、权利、去重和可访问要求。
- 切分后产生的片段数量、时间范围、视觉描述、技术质量和拒绝原因，才是下一轮检索缺口的可靠证据。
- 本轮返回的标题/关键词可以帮助扩展查询，但它们必须来自已接纳片段或高置信候选，并受原 `theme_intent` 和 `target_facet` 约束；不能让标题词自行决定下一轮方向。

两个参考项目适合放在闭环的基础设施侧：

- MediaCrawler 展示了按平台和关键词分页搜索、详情补全和存储的实现，但其搜索主要通过存储副作用结束，没有本项目所需的统一累计片段快照和充分性闭环。[爬虫抽象接口](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/base/base_crawler.py#L26-L40)；[存储抽象接口](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/base/base_crawler.py#L86-L100)
- Douyin_TikTok_Download_API 以已有 URL 为入口解析和下载整段媒体，适合作为发现后的 `ResolveProvider` / `DownloadProvider` 参考，但没有片段切分和闭环编排。[混合解析端点](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/app/api/endpoints/hybrid_parsing.py#L15-L46)；[下载端点](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/app/api/endpoints/download.py#L111-L145)

因此，已有项目可以提供平台访问能力，但状态机、批次、累计充分性和落库回执协议需要本项目自行定义。

## 2. 推荐状态机

### 2.1 查询计划级状态

```text
PLANNED
  → SEARCHING
  → BATCH_FROZEN
  → DOWNLOADING
  → INGESTING
  → EVALUATING
      ├─→ SUFFICIENT
      ├─→ EXPANDING → SEARCHING
      ├─→ EXHAUSTED_PARTIAL
      └─→ PAUSED

任意活动步骤：
  → RETRY_WAIT
  → PAUSED
  → CANCELLED
  → FAILED
```

建议把“业务状态”和“活动尝试状态”分开。计划处于 `INGESTING` 时，单个候选可以分别为 `completed`、`rejected`、`retry_wait` 或 `failed_permanent`；不能因一个源视频失败就回滚已经成功落库的其他片段。

### 2.2 批次级状态

每一轮创建不可变的 `QueryBatch`：

```text
DRAFT → FROZEN → RUNNING → SETTLING → CLOSED
```

- `DRAFT`：可添加查询表达和候选。
- `FROZEN`：确定本轮查询表达、平台、页游标、预算以及选中候选；冻结后不得继续塞入新结果。
- `RUNNING`：执行下载、切分和落库。
- `SETTLING`：仍有异步落库任务或“结果未知”的调用需要对账。
- `CLOSED`：本轮所有条目都有终态，生成 `BatchIngestReport`。

“冻结”是重要的恢复边界。若执行过程中继续把新候选混入同一批次，重启后很难判断哪些条目应该重放、成本算在哪一轮、充分性快照是否完整。

### 2.3 条目级状态

```text
DISCOVERED
  → REJECTED_PRE_DOWNLOAD
  → DUPLICATE_SOURCE
  → SELECTED
  → DOWNLOADING
      ├─→ DOWNLOAD_FAILED_RETRYABLE
      └─→ DOWNLOAD_FAILED_PERMANENT
  → DOWNLOADED
  → INGEST_SUBMITTED
      ├─→ INGEST_PROCESSING
      ├─→ INGEST_RESULT_UNKNOWN
      ├─→ INGEST_REJECTED
      ├─→ INGEST_PARTIAL
      └─→ INGEST_COMPLETED
```

`INGEST_RESULT_UNKNOWN` 不能直接当作失败并重新提交。典型情况是落库服务已经提交成功，但调用方在收到响应前断线；此时应先用 `idempotency_key` 或 `ingest_job_id` 查询原调用，再决定是否重试。

## 3. 幂等与一致性

### 3.1 为什么必须按“至少一次”设计

搜索、下载、切分和落库都是外部副作用。无论采用简单 Worker 还是持久工作流，Worker 都可能在副作用已经成功但尚未保存完成状态时崩溃。Temporal 官方文档明确说明 Activity 采用至少一次执行模型：如果 Activity 已执行成功但 Worker 在通知服务端前崩溃，该 Activity 会被重试；官方建议使用跨重试保持不变的幂等键，并将大 Activity 拆分成较小、原子的步骤。[Temporal Python 错误处理：幂等与原子 Activity](https://docs.temporal.io/develop/python/best-practices/error-handling#make-activities-idempotent)

因此，本项目不应宣称“每个操作恰好执行一次”，而应实现：

> 调用可能至少执行一次，但相同业务键只产生一个逻辑结果。

### 3.2 建议的稳定键

| 层级 | 建议唯一键 | 作用 |
| --- | --- | --- |
| 查询计划 | `plan_id` + `plan_revision` | 区分同一主题段的不同计划版本 |
| 检索请求 | `provider + normalized_query + normalized_filters + page_token` 的稳定摘要 | 防止恢复时重复计费、重复抓取同一页 |
| 候选源 | 优先 `platform + platform_content_id`；缺失时使用规范化 URL | 合并同平台重复发现 |
| 下载 | `source_key + source_revision` | 同一版本源文件只下载一次 |
| 原文件 | `sha256` / `Content-Digest` | 识别字节级完全相同的文件并校验传输完整性 |
| 落库调用 | `plan_id + source_key + source_revision + splitter_profile_version` | 重试时返回原任务或原结果，不重复切分入库 |
| 素材片段 | `source_digest + start_ms + end_ms + splitter_profile_version` | 同一源的同一切分结果只产生一个逻辑片段 |

HTTP 的 `Content-Digest` / `Repr-Digest` 标准定义了对消息内容或表示计算完整性摘要的字段，并将 `sha-256`、`sha-512` 列为有效算法；它适合做下载完整性与字节级重复检测，而不适合单独判断重新编码后的近重复。[RFC 9530](https://www.rfc-editor.org/rfc/rfc9530.html)

近重复需要另一个层次。pHash 的官方设计包含基于关键帧的可变长度 DCT 视频哈希，目标是让同一感知内容的不同版本得到接近的签名；因此接口应允许返回带算法和版本的 `perceptual_fingerprint`，但近重复只能用于“合并/复核”决策，不能替代源 ID 和精确摘要。[pHash Design](https://www.phash.org/docs/design.html)

### 3.3 数据库约束

幂等不能只靠“先查询是否存在，再插入”的应用逻辑；并发 Worker 仍可能同时通过检查。应为上述业务键建立数据库唯一约束，并用原子 UPSERT 提交结果。PostgreSQL 官方文档说明，唯一约束保证一列或列组合在表中唯一；`INSERT ... ON CONFLICT` 可以在冲突时 `DO NOTHING` 或 `DO UPDATE`，并在没有其他错误时保证每行得到原子的插入或更新结果。[PostgreSQL Unique Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html#DDL-CONSTRAINTS-UNIQUE-CONSTRAINTS)；[PostgreSQL INSERT / ON CONFLICT](https://www.postgresql.org/docs/current/sql-insert.html#SQL-ON-CONFLICT)

如果采用可序列化事务，应准备重试完整事务；PostgreSQL 会用 SQLSTATE `40001` 报告序列化失败，官方要求应用中止并从头重试该事务。[PostgreSQL Transaction Isolation](https://www.postgresql.org/docs/current/transaction-iso.html)

### 3.4 本地状态与外部素材库之间

本项目数据库和外部素材库通常无法共享一个 ACID 事务，所以不要尝试“本地记录 + 远端落库一次原子提交”。推荐：

1. 本地先持久化 `INGEST_SUBMITTED` 和稳定幂等键。
2. 调用外部落库接口。
3. 保存远端 `ingest_job_id`。
4. 若响应丢失，按幂等键或任务 ID查询，而不是重新生成新请求。
5. 收到终态报告后，以 UPSERT 保存 `BatchIngestReport` 和逐片段回执。
6. 周期性 reconciler 对账长时间停留在 `PROCESSING` / `RESULT_UNKNOWN` 的任务。

已经成功入库的片段不因同批其他条目失败而删除。删除属于新的、显式的补偿操作；默认通过 `quarantined` / `superseded` 标志撤出有效集合，保留审计链。

## 4. “批次完成”的精确定义

一轮并不要求所有候选都成功，而要求所有候选都已经**结算**：

```text
batch_settled =
  每个 selected candidate 均处于终态
  AND 不存在 INGEST_RESULT_UNKNOWN
  AND 素材库返回了可读取的 snapshot_version / committed_at
```

终态包括：

- `ingest_completed`
- `ingest_partial`
- `ingest_rejected`
- `duplicate_existing`
- `failed_permanent`
- `retry_exhausted`
- `cancelled`

只有进入 `batch_settled` 后才运行充分性判断。若落库工具是异步接口，HTTP `202 Accepted` 只说明请求被接受，不等于本轮完成；接口必须提供轮询、回调或消息通知，并最终返回完成报告。

充分性评估读取的是**计划级累计快照**：

```text
有效片段 =
  属于 plan_id / theme_segment_id
  AND ingest_status = committed
  AND availability = available
  AND admission_status = accepted
  AND rights_status 满足当前策略
  AND 非 exact/near duplicate 的被淘汰副本
  AND 非 quarantined / deleted / superseded
```

不能只看当前批次，否则上一轮已经覆盖的面向会被误判为缺失。建议每次评估保存 `library_snapshot_version` 或单调递增的 `material_revision`，以便重现“当时为什么判定足够”。

## 5. 切分落库接口必须返回的内容

接口文档尚未提供时，至少应预留下列契约。字段名称可调整，但语义不能缺失。

### 5.1 作业与一致性字段

```json
{
  "schema_version": "1.0",
  "request_id": "req_...",
  "idempotency_key": "plan:source:revision:splitter-v3",
  "ingest_job_id": "job_...",
  "plan_id": "plan_...",
  "batch_id": "batch_...",
  "status": "completed",
  "submitted_at": "...",
  "completed_at": "...",
  "library_snapshot_version": "material-rev-1842"
}
```

必需语义：

- `schema_version`：支持协议演进。
- `idempotency_key`：重试同一调用时定位原结果。
- `ingest_job_id`：异步查询和恢复。
- `status`：至少区分 `accepted`、`processing`、`partial`、`completed`、`failed`。
- `library_snapshot_version` / `committed_at`：证明返回的是已提交、可查询状态。

### 5.2 源内容回执

```json
{
  "source": {
    "source_key": "douyin:123",
    "platform": "douyin",
    "platform_content_id": "123",
    "source_url": "...",
    "source_revision": "...",
    "source_sha256": "...",
    "original_title": "...",
    "author": "...",
    "published_at": "...",
    "rights": {
      "status": "allowed",
      "license": "...",
      "allowed_uses": ["edit"],
      "evidence_uri": "..."
    }
  }
}
```

这些字段让系统能追溯片段、去重、重新处理，并避免把“技术上能下载”误当成“允许剪辑使用”。

### 5.3 每个有效片段

```json
{
  "clip_id": "clip_...",
  "source_key": "douyin:123",
  "start_ms": 12400,
  "end_ms": 17800,
  "duration_ms": 5400,
  "storage_ref": "material://clip_...",
  "availability": "available",
  "admission_status": "accepted",
  "exact_digest": {
    "algorithm": "sha-256",
    "value": "..."
  },
  "perceptual_fingerprint": {
    "algorithm": "dct-video-hash",
    "version": "1",
    "value": "..."
  },
  "duplicate_of": null,
  "summary": "夜晚地铁站中一名行人独自等待",
  "keywords": ["夜晚", "地铁", "独自等待"],
  "entities": ["行人", "地铁站"],
  "actions": ["等待"],
  "scenes": ["夜间公共交通"],
  "facet_matches": [
    {
      "facet_id": "isolated_person",
      "confidence": 0.91,
      "evidence": "单人在拥挤公共空间中静止等待"
    }
  ],
  "technical": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "video_codec": "h264",
    "has_audio": true,
    "quality_score": 0.84
  },
  "provenance": {
    "query_variant_id": "query_...",
    "parent_query_id": "query_...",
    "expansion_terms": ["地铁站"],
    "round_index": 2
  }
}
```

对于充分性判断，真正不可缺的字段是：

- `clip_id`、`source_key`、时间范围；
- 已提交/可访问状态；
- exact 与 near-duplicate 信息；
- 片段摘要、关键词以及实体/动作/场景；
- `facet_matches` 及置信度/证据；
- 质量与权利状态；
- 查询与批次发现链路。

只返回一个总体摘要或源视频标题是不够的，因为无法计算各 `required facet` 的独立候选数、去重后时长和视觉多样性。

### 5.4 拒绝、重复与失败

```json
{
  "item_ref": "source-or-clip-id",
  "outcome": "rejected",
  "stage": "segment_quality",
  "reason_code": "NO_RELEVANT_SEGMENT",
  "retryable": false,
  "message": "未发现与目标面向匹配且达到最小时长的片段",
  "duplicate_of": null,
  "suggested_action": "expand_query_for_missing_facet"
}
```

推荐稳定的 `reason_code` 类别：

- `SOURCE_UNAVAILABLE`
- `DOWNLOAD_AUTH_EXPIRED`
- `DOWNLOAD_RATE_LIMITED`
- `UNSUPPORTED_MEDIA`
- `CORRUPT_MEDIA`
- `RIGHTS_NOT_ALLOWED`
- `NO_RELEVANT_SEGMENT`
- `LOW_TECHNICAL_QUALITY`
- `DURATION_TOO_SHORT`
- `EXACT_DUPLICATE`
- `NEAR_DUPLICATE`
- `POLICY_REJECTED`
- `INTERNAL_RETRY_EXHAUSTED`

`message` 可以给人看，状态机应依据 `reason_code` 和 `retryable` 行动。拒绝原因也是下一轮的重要信号：例如大量 `NO_RELEVANT_SEGMENT` 表明查询语义需要收窄；大量 `EXACT_DUPLICATE` 表明当前查询分支边际收益已经很低；`RIGHTS_NOT_ALLOWED` 则应换来源平台或许可过滤，而不是仅换同义词。

### 5.5 统计与真实成本

报告还应返回：

```json
{
  "counts": {
    "sources_received": 10,
    "sources_processed": 9,
    "clips_created": 24,
    "clips_accepted": 12,
    "clips_rejected": 8,
    "clips_duplicate": 4,
    "items_failed": 1
  },
  "actual_cost": {
    "download_bytes": 1384921834,
    "media_seconds_processed": 1140,
    "gpu_seconds": 86.4,
    "llm_input_tokens": 0,
    "llm_output_tokens": 0
  },
  "warnings": []
}
```

没有真实成本回执，编排器只能控制搜索请求，不能控制下载、转码、视觉模型和存储成本。

## 6. 下一轮如何判断与补搜

一次评估应返回：

```json
{
  "sufficient": false,
  "snapshot_version": "material-rev-1842",
  "covered_facets": ["crowded_city"],
  "missing_facets": ["isolated_person"],
  "weak_facets": [
    {
      "facet_id": "contrast",
      "accepted_alternatives": 1,
      "required_alternatives": 3
    }
  ],
  "usable_clip_count": 8,
  "usable_duration_ms": 51600,
  "visual_families": 2,
  "marginal_yield": {
    "new_accepted_clips": 2,
    "new_unique_duration_ms": 9400,
    "duplicate_rate": 0.62
  },
  "decision": "expand",
  "stop_reason": null
}
```

补搜规则：

1. 只针对 `missing_facets` / `weak_facets` 生成新查询。
2. 标题、摘要和关键词只从以下来源提取：
   - 已接纳且匹配目标 facet 的片段；
   - 高置信但因非语义原因被拒绝的候选；
   - 原主题段、原 `theme_intent` 和既有查询表达。
3. 每条新查询必须记录 `target_facet`、`parent_query_id`、`expansion_terms` 和 `depth`。
4. 必须保留至少一个主题锚点，禁止用标题新词完全替换原意图。
5. 已访问查询、平台源 ID、URL、文件摘要和片段近重复集合均参与去重。
6. 当前批次重复率升高、有效新增趋近于零时，应剪枝而不是继续换同义词。

“有效片段的摘要、关键词、标题”是可用的反馈信号，但应作为缺失面向约束下的词汇来源，不应直接作为充分性事实。充分性事实来自片段级覆盖、替代数、时长、多样性、质量和权利状态。

## 7. 失败恢复

### 7.1 重试分类

- 瞬时错误：网络中断、5xx、短暂超时，可指数退避并加入抖动。
- 间歇错误：429、平台风控、配额不足，应读取平台重置时间，延迟到可重试窗口。
- 永久错误：无效 URL、不支持格式、权利不允许、输入校验失败，不应自动重试。
- 结果未知：调用可能已成功但响应丢失，必须查询原任务，不能直接新建任务。

Temporal 官方错误处理文档同样区分瞬时、间歇和永久故障，并建议对永久错误标记为不可重试。[Temporal Python Error Handling](https://docs.temporal.io/develop/python/best-practices/error-handling)

### 7.2 长下载/转码检查点

长下载和切分任务应报告进度和取消状态。若使用 Temporal，Activity 可以通过 heartbeat details 保存检查点，后续尝试从检查点恢复，而不是从初始状态重跑；官方 Activities 文档明确说明 Activity 默认失败后从初始状态开始，除非使用 heartbeat details 做 checkpoint。[Temporal Activities](https://docs.temporal.io/activities)

无论具体框架如何，接口应能够保存：

- 已下载字节数和临时文件摘要；
- 远端 ETag / Last-Modified（若平台提供）；
- 已处理到的时间码；
- 已生成但尚未提交的片段；
- 最近 heartbeat 时间；
- 可取消标志。

临时文件只有在完整性校验通过后才原子改名为正式缓存文件；失败残片应可识别、可续传或可安全清理。

### 7.3 循环过长

若采用持久工作流，递归轮次会持续增长历史。Temporal 的 Continue-As-New 能把最新相关状态作为参数传给具有相同 Workflow ID、不同 Run ID 和全新 Event History 的新执行，从而控制长历史的性能和上限问题。[Temporal Continue-As-New](https://docs.temporal.io/workflow-execution/continue-as-new)

如果不用 Temporal，也应定期把：

- `visited_queries`
- 已知源/片段去重索引
- 当前 facet 覆盖
- 累计成本
- 剩余预算
- 最新素材库快照版本

压缩成计划检查点，避免每次恢复都重放完整日志。

## 8. 成本与配额控制

### 8.1 分层投入

推荐按成本从低到高逐级推进：

1. 查询缓存和历史素材库命中；
2. 搜索元数据；
3. 标题/描述/标签相关性与权利初筛；
4. 缩略图、关键帧或低码率预览；
5. 完整下载；
6. 切分、ASR/OCR、视觉理解和嵌入；
7. 长期存储。

只有通过上一层的候选才进入下一层。尤其不要在相关性、权利和源级去重之前下载完整视频。

### 8.2 必须同时存在的硬预算

每个 `QueryPlan` 至少配置：

- `max_rounds`
- `max_depth`
- `max_query_variants`
- `max_provider_requests`
- `max_candidates`
- `max_download_count`
- `max_download_bytes`
- `max_media_seconds_processed`
- `max_gpu_seconds`
- `max_llm_tokens`
- `max_wall_clock_seconds`
- 每个平台独立的并发和速率限制

平台成本不是同质的。YouTube 官方当前配额说明中，`search.list` 使用独立查询配额，每个项目默认每天 100 次，每一个额外分页请求也会消耗一次调用；因此“翻下一页”必须算入预算，而不能把一次检索表达视为一次固定成本。[YouTube Quota Calculator](https://developers.google.com/youtube/v3/determine_quota_cost)

Pexels 默认限制为每小时 200 请求、每月 20,000 请求，并通过响应头返回剩余额度和重置时间；Pixabay 默认每 60 秒 100 请求，要求缓存 API 请求 24 小时，并禁止系统化批量下载。适配器必须记录这些配额信息和平台规则，而不是由递归 Agent 无限发请求。[Pexels API Documentation](https://www.pexels.com/api/documentation/)；[Pixabay API Documentation](https://pixabay.com/api/docs/)

### 8.3 边际收益停止

除硬预算外，还需要软停止条件：

```text
若连续 K 个已结算查询分支：
  new_accepted_clips = 0
  OR new_unique_duration 很低
  OR duplicate_rate 高于阈值
  OR accepted_clips / processed_sources 低于阈值
则停止该分支。
```

阈值应通过真实剪辑采用率校准，不能假装存在通用学术定值。报告应保留每轮新增量，使后续能回答“第几轮以后已不值得继续搜”。

## 9. 推荐的最小实现边界

在落库接口尚未给出、技术栈尚未确定时，第一版建议只确定协议和状态机：

```text
SearchLoopService
  ├─ QueryPlannerPort
  ├─ SearchProvider[]
  ├─ CandidatePolicy
  ├─ MediaResolverPort
  ├─ DownloaderPort
  ├─ MaterialIngestPort
  ├─ MaterialSnapshotPort
  ├─ SufficiencyEvaluator
  └─ QueryExpander
```

持久化至少包含：

- `query_plans`
- `query_batches`
- `query_requests`
- `candidate_sources`
- `download_attempts`
- `ingest_jobs`
- `ingest_receipts`
- `material_snapshot_refs`
- `sufficiency_decisions`
- `budget_ledger`

CLI、前端和 Agent 都只调用 `SearchLoopService`。CLI 可输出最终 JSON 和增量事件；前端订阅同一状态事件；两者不各自实现循环逻辑。

## 10. 最小验证方案

在正式实现前，用 10–20 个真实主题段做故障注入原型：

1. 每段设置 2–4 个 required facets 和固定预算。
2. 用伪 SearchProvider 和伪落库工具制造：
   - 同一候选跨平台/跨查询重复；
   - 下载后崩溃；
   - 落库已成功但响应丢失；
   - 部分片段接纳、部分拒绝；
   - 429 与永久失败；
   - 同一幂等键重复调用；
   - 并发 Worker 同时提交同一候选。
3. 验证：
   - 重启后不会重复入库；
   - 结果未知能通过对账恢复；
   - 充分性只读取已提交片段；
   - 累计快照不会丢失前一轮覆盖；
   - 达到预算或边际收益阈值能稳定停止；
   - 相同输入和相同快照得到相同充分性决策；
   - 每个决策都能追溯到批次、查询、源内容和片段。

## 最终建议

接受“查询一批 → 下载与切分落库 → 返回有效片段摘要/关键词/标题/拒绝原因 → 判断 → 定向补搜”的设计，但在规格中加入四条不可妥协的约束：

1. **按已提交的片段快照判断，不按候选或下载数判断。**
2. **每轮批次先冻结、后执行、全部结算后再评估。**
3. **所有外部副作用采用稳定幂等键、数据库唯一约束和结果未知对账。**
4. **补搜由缺失 facet 驱动，片段摘要和标题只提供受控扩展词。**

满足这些条件后，该闭环不仅可行，而且能够自然支持中断恢复、分平台降级、成本上限、CLI/前端进度展示以及将来替换切分落库工具。
