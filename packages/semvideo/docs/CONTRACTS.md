# 接口、CLI、产物与数据契约

本文件定义首版实现必须保持稳定的可观察行为。示例使用 JSON 表达，具体 Python 类型和文件实现可以调整，但字段语义和领域不变量不可静默改变。

## 1. 通用约定

- 所有 ID 使用不透明字符串，调用方不得从 ID 推断路径或顺序。
- 时间点使用源视频时间线上的整数毫秒。
- 时间范围使用半开区间 `[start_ms, end_ms)`。
- 所有持久化 JSON 顶层包含 `schema_version`。
- 所有枚举使用小写蛇形命名。
- 未知字段由读取方忽略；缺失必需字段必须报错。
- 文件路径在产物中使用相对于任务根目录的 POSIX 风格路径。
- 内容校验值使用带算法前缀的字符串，例如 `sha256:...`。
- 模型置信度仅用于排序和复核提示，不能自动替代结构验证。

## 2. 应用 Interface

### CreateJobRequest

```json
{
  "workspace": {
    "kind": "local",
    "root": "D:/video-project"
  },
  "source": {
    "kind": "local_file",
    "value": "D:/videos/example.mp4"
  },
  "profile": "default",
  "overrides": {
    "render_final_segments": false
  },
  "idempotency_key": null
}
```

规则：

- 首版只接受 `local_file`。
- CLI 在调用应用 Interface 前解析视频处理工作区根目录；任务数据根固定为 `<workspace-root>/semvideo-data/`。
- Agent 会话不参与任务身份；不同会话只要解析到同一工作区，就能访问同一批任务。
- `idempotency_key` 相同且请求内容相同时返回同一任务。
- 输入路径只在创建阶段解析；领域记录保存源视频 ID，不依赖原始字符串路径。

### JobSnapshot

```json
{
  "job_id": "job_01...",
  "source_video_id": "video_01...",
  "state": "analyzing",
  "current_stage": "analyze",
  "attempt_id": "attempt_01...",
  "worker": {
    "status": "running",
    "pid": 18420,
    "started_at": "2026-07-27T12:00:02Z"
  },
  "progress": {
    "completed_units": 2,
    "total_units": 5,
    "unit": "evidence_window"
  },
  "created_at": "2026-07-27T12:00:00Z",
  "updated_at": "2026-07-27T12:03:14Z",
  "failure": null
}
```

### JobResult

```json
{
  "job_id": "job_01...",
  "state": "completed",
  "final_segment_count": 12,
  "rendered_segment_count": 0,
  "review_required_count": 2,
  "segment_records": {
    "artifact_id": "artifact_segments_01...",
    "path": "retrieval/segments.jsonl",
    "count": 12,
    "schema_version": 1
  },
  "manifest_artifact_id": "artifact_01...",
  "report_artifact_id": "artifact_02..."
}
```

任务可以在存在 `review` 边界判断时完成，但结果必须显式给出 `review_required_count`，并且不得把这些边界自动移除。

## 3. 核心领域产物

### 3.1 媒体事实

```json
{
  "schema_version": 1,
  "source_video_id": "video_01...",
  "content_hash": "sha256:...",
  "duration_ms": 183420,
  "video_stream": {
    "codec": "h264",
    "width": 1920,
    "height": 1080,
    "nominal_fps": 29.97,
    "variable_frame_rate": false,
    "rotation_degrees": 0
  },
  "audio_streams": [
    {
      "index": 1,
      "codec": "aac",
      "channels": 2,
      "sample_rate": 48000
    }
  ]
}
```

### 3.2 候选时间线

```json
{
  "schema_version": 1,
  "source_video_id": "video_01...",
  "algorithm": {
    "name": "ffmpeg_scene_v1",
    "version": "1",
    "parameters": {
      "scene_threshold": 0.3,
      "min_segment_ms": 500,
      "max_segment_ms": 20000
    }
  },
  "boundaries": [
    {
      "candidate_boundary_id": "boundary_001",
      "timestamp_ms": 12400,
      "reasons": ["scene_change"],
      "scores": {
        "scene_change": 0.71
      }
    }
  ],
  "segments": [
    {
      "candidate_segment_id": "candidate_001",
      "ordinal": 0,
      "start_ms": 0,
      "end_ms": 12400,
      "left_boundary_id": null,
      "right_boundary_id": "boundary_001"
    }
  ]
}
```

不变量：

- 第一段从 `0` 开始，最后一段在 `duration_ms` 结束。
- 候选片段按 `ordinal` 连续覆盖时间线，不重叠、不留空隙。
- 相邻片段共享同一个候选边界。
- 证据去重不能新增、删除或移动候选边界。

### 3.3 证据时间线

```json
{
  "schema_version": 1,
  "source_video_id": "video_01...",
  "frames": [
    {
      "evidence_frame_id": "frame_001",
      "timestamp_ms": 1200,
      "artifact_id": "artifact_frame_001",
      "extraction_reasons": ["visual_change", "maximum_interval"],
      "scene_score": 0.12,
      "dedup": {
        "decision": "kept",
        "reason": "local_change"
      }
    }
  ],
  "contact_sheets": [
    {
      "contact_sheet_id": "sheet_001",
      "artifact_id": "artifact_sheet_001",
      "layout": {"rows": 3, "columns": 3},
      "cell_frame_ids": ["frame_001", "frame_002", "frame_003"]
    }
  ],
  "transcript_spans": [
    {
      "transcript_span_id": "transcript_001",
      "start_ms": 900,
      "end_ms": 2600,
      "text": "用户打开视频导入窗口。",
      "source": "embedded_subtitle",
      "confidence": 0.99
    }
  ],
  "ocr_observations": [
    {
      "timestamp_ms": 1200,
      "text": "导入媒体",
      "confidence": 0.97
    }
  ]
}
```

九宫格的 `cell_frame_ids` 必须保持时间顺序并可映射回证据帧。证据帧和候选边界是独立集合，证据去重不得移动或删除候选边界。

### 3.4 证据窗口

```json
{
  "schema_version": 1,
  "evidence_window_id": "window_001",
  "source_video_id": "video_01...",
  "ordinal": 0,
  "start_ms": 0,
  "end_ms": 90000,
  "contact_sheet_ids": ["sheet_001", "sheet_002"],
  "evidence_frame_ids": ["frame_001", "frame_002", "frame_003"],
  "transcript_span_ids": ["transcript_001"],
  "candidate_boundary_ids": ["boundary_001", "boundary_002"],
  "overlap": {
    "previous_window_ms": 0,
    "next_window_ms": 18000
  },
  "policy": {
    "name": "long_context_v1",
    "version": 1
  }
}
```

窗口必须是连续源时间范围。相邻窗口可以重叠，但所有窗口的并集必须覆盖整个源视频；窗口首尾不是隐含语义边界。

### 3.5 语义分段提案

```json
{
  "schema_version": 1,
  "evidence_window_id": "window_001",
  "model_run_id": "model_run_001",
  "segments": [
    {
      "proposal_segment_id": "proposal_segment_001",
      "start_boundary_id": null,
      "end_boundary_id": "boundary_002",
      "title": "导入视频文件",
      "summary": "用户打开导入窗口并选择视频文件。",
      "narrative_event": "一次连续的视频导入操作",
      "boundary_reason": "下一阶段开始处理已选择的视频。",
      "evidence_frame_ids": ["frame_001"],
      "transcript_span_ids": ["transcript_001"],
      "confidence": 0.91,
      "needs_boundary_refinement": false
    }
  ],
  "warnings": []
}
```

提案中的片段必须按时间排序并连续覆盖证据窗口。边界只能引用该窗口提供的候选边界；`null` 仅表示窗口或源视频的自然端点。模型输出不得包含可执行命令，也不得引用窗口之外的证据 ID。

### 3.6 边界判断

```json
{
  "schema_version": 1,
  "candidate_boundary_id": "boundary_001",
  "left_segment_id": "candidate_001",
  "right_segment_id": "candidate_002",
  "decision": "remove",
  "relationship": "same_continuous_event",
  "reason": "右侧片段继续完成同一次视频导入操作。",
  "model_run_id": "model_run_042",
  "confidence": 0.94,
  "review_reasons": []
}
```

允许的 `relationship`：

- `same_continuous_event`
- `same_topic_new_event`
- `different_topic`
- `insufficient_evidence`
- `model_disagreement`

映射规则：

- `same_continuous_event` 通常对应 `remove`。
- `same_topic_new_event` 和 `different_topic` 对应 `keep`。
- `insufficient_evidence` 和 `model_disagreement` 对应 `review`。
- 规则引擎可以把低置信度判断提升为 `review`，但不能把 `review` 降级为自动移除。

边界判断可以由多个重叠窗口提案协调得到；`model_run_id` 可以为空，此时必须通过来源引用关联参与协调的模型运行和确定性规则版本。

### 3.7 合并计划

```json
{
  "schema_version": 1,
  "job_id": "job_01...",
  "candidate_timeline_hash": "sha256:...",
  "boundary_decisions_hash": "sha256:...",
  "final_segments": [
    {
      "final_segment_id": "segment_001",
      "ordinal": 0,
      "start_ms": 0,
      "end_ms": 24100,
      "candidate_segment_ids": [
        "candidate_001",
        "candidate_002"
      ],
      "removed_boundary_ids": ["boundary_001"],
      "review_boundary_ids": []
    }
  ]
}
```

合并计划是不可执行数据，不包含 FFmpeg 参数或文件路径。

### 3.8 正式摘要

```json
{
  "schema_version": 1,
  "final_segment_id": "segment_001",
  "model_run_id": "model_run_050",
  "title": "导入视频文件",
  "short_summary": "用户打开导入窗口并选择待处理的视频文件。",
  "detailed_summary": "该片段展示了一次连续的视频导入操作，从打开导入窗口开始，到选中视频文件结束。",
  "visual_summary": "画面显示用户在桌面应用中打开文件选择窗口，并选中一个视频文件。",
  "topics": ["视频导入"],
  "participants": ["用户"],
  "locations": ["桌面应用界面"],
  "organizations": [],
  "objects": ["文件选择窗口", "视频文件"],
  "actions": ["打开导入窗口", "选择视频文件"],
  "keywords": ["视频导入", "文件选择", "本地视频"],
  "source_candidate_segment_ids": [
    "candidate_001",
    "candidate_002"
  ],
  "confidence": 0.93
}
```

### 3.9 检索片段记录

```json
{
  "schema_version": 1,
  "segment_id": "segment_001",
  "job_id": "job_01...",
  "source_video_id": "video_01...",
  "ordinal": 0,
  "start_ms": 0,
  "end_ms": 24100,
  "duration_ms": 24100,
  "title": "直升机起飞并进入巡逻航线",
  "short_summary": "边防官兵搭乘直升机起飞，开始执行空中巡逻。",
  "detailed_summary": "片段连续展示官兵登机、直升机起飞并进入巡逻航线的过程，镜头变化没有改变同一次任务的连续性。",
  "visual_summary": "画面包括官兵进入机舱、旋翼启动、直升机离地以及空中飞行。",
  "topics": ["边防巡逻", "直升机"],
  "participants": ["边防官兵", "机组人员"],
  "locations": ["边防地区", "空中巡逻航线"],
  "organizations": [],
  "objects": ["直升机", "机舱"],
  "actions": ["登机", "起飞", "空中巡逻"],
  "keywords": ["边防", "直升机", "空中巡逻"],
  "transcript": {
    "text": "边防官兵搭乘直升机展开空中巡逻……",
    "span_ids": ["transcript_001", "transcript_002"],
    "language": "zh",
    "source": "asr",
    "quality": "asr_with_warnings",
    "clipped_span_ids": []
  },
  "search_text": "直升机起飞并进入巡逻航线。边防官兵搭乘直升机起飞，开始执行空中巡逻……",
  "confidence": 0.93,
  "review_required": false,
  "review_reasons": [],
  "artifacts": {
    "video": null,
    "thumbnail": "evidence/frames/frame_001.jpg",
    "contact_sheets": [
      "evidence/contact-sheets/sheet_001.jpg"
    ]
  },
  "provenance": {
    "profile": "default",
    "profile_version": 1,
    "search_text_version": "retrieval-text-v1",
    "summary_model_run_id": "model_run_050",
    "merge_plan_hash": "sha256:..."
  }
}
```

不变量：

- 时间使用正式片段的半开区间，不得为检索方便改变边界。
- `duration_ms = end_ms - start_ms`。
- `search_text` 由结构化字段确定性构造，生成规则必须版本化；它不是新的模型判断。
- `confidence` 表示视频理解可靠性，不是查询相关性分数。
- 记录不得包含 `rank`、`relevance_score`、`query`、embedding 或 Top-K 状态。
- `artifacts.video` 在尚未渲染时为 `null`；只有成功执行
  `process --render` 才引用任务包内经过校验且登记到 manifest 的媒体文件。
  `segment export` 是只读投影，不得反向改写本记录。
- `transcript.text` 保存正式片段时间范围内的完整转写，不做面向 Agent 上下文的截断；无语音或无可用转写时使用空字符串并记录质量状态。
- `transcript.span_ids` 保留到证据时间线的引用。跨越正式片段边界的转写区间应保留相交文本并记录裁剪信息，不能静默丢失边界词句。
- 全量记录按 `ordinal` 写入 `retrieval/segments.jsonl`，每行一个完整 JSON 对象。
- 下游搜索可以读取记录，但不得反向修改正式片段、摘要或任务状态。

### 3.10 镜头时间线与镜头语言标注

`segmentation/shot-timeline.json` 连续覆盖源视频。它只把带 `scene_change` 来源的
候选边界投影为镜头边界，`max_duration` 或普通语义锚点不得成为镜头事实。

```json
{
  "schema_version": 1,
  "source_video_id": "video_01...",
  "duration_ms": 24100,
  "detector": {
    "name": "ffmpeg_scene_v1",
    "version": "1",
    "projection": "scene_change_only_v1"
  },
  "shots": [
    {
      "shot_id": "shot_0001",
      "ordinal": 0,
      "start_ms": 0,
      "end_ms": 8200,
      "right_boundary": {
        "timestamp_ms": 8200,
        "kind": "hard_cut_candidate",
        "score": 0.71,
        "detector": "ffmpeg_scene_v1",
        "source_candidate_boundary_id": "boundary_0003"
      }
    }
  ]
}
```

`semantics/cinematography-annotations.jsonl` 每行对应一个镜头：

```json
{
  "schema_version": 1,
  "source_video_id": "video_01...",
  "model_run_id": "modelrun_01...",
  "shot_id": "shot_0001",
  "viewpoints": ["aerial"],
  "shot_scale": {
    "start": "extreme_wide",
    "end": "wide"
  },
  "camera_motions": [
    {
      "type": "rise",
      "direction": "up",
      "speed": "slow",
      "temporal_profile": "gradual",
      "start_ms": 0,
      "end_ms": 8200,
      "confidence": 0.88
    }
  ],
  "cinematography_summary": "航拍大全景缓慢升高并向巡逻区域推进。",
  "cinematography_keywords": ["航拍", "大全景", "缓慢推进", "逐渐拉高"],
  "evidence_frame_ids": ["shot_0001_frame_01"],
  "confidence": 0.88
}
```

标注必须使用受控词表，逐镜头完整覆盖且不引用镜头范围外的时间和证据。镜头内帧序列
及本地运动测量写入 `evidence/cinematography/`。每批模型调用独立写入
`model-runs/`，失败和修复响应同样必须留档。

`evidence/cinematography/index.json` 中每个镜头同时保存兼容字段
`contact_sheet_path`（第一张图）和完整的 `contact_sheet_paths`。每张联系图最多按
时间顺序容纳九帧；合法的 10–24 帧策略必须生成 2–3 张图，模型请求为每张图提供其
覆盖的 frame IDs 并逐张上传，不得仅在文本中列出不可见帧。本地运动测量包含
`foreground_suppression=tile_median_v1`、`speed_curve`、
`temporal_profile_hint` 和 `direction_change_count`；旧任务包缺少这些新增字段时按
无本地提示读取，不伪造历史测量结果。

### 3.11 模型运行

```json
{
  "schema_version": 1,
  "model_run_id": "model_run_050",
  "purpose": "final_summary",
  "provider": "configured_provider",
  "model": "configured_model",
  "prompt_version": "final-summary-v1",
  "input_hash": "sha256:...",
  "raw_response_artifact_id": "artifact_model_response_050",
  "status": "succeeded",
  "usage": {
    "input_tokens": 3210,
    "output_tokens": 380
  },
  "started_at": "2026-07-27T12:03:00Z",
  "duration_ms": 1840
}
```

## 4. 任务目录与文件产物

本地实现使用以下逻辑布局。它既是可供 Agent 检查的工作区任务包，也是 V1 的持久化事实：

```text
<workspace-root>/
└── semvideo-data/
    ├── workspace.json
    ├── config.toml                   只保存非敏感工作区配置和凭据环境变量名
    ├── sources/
    │   └── <source-video-id>/
    │       ├── source.<ext>
    │       ├── source.json
    │       └── analysis/
    │           ├── analysis-<policy-id>.mp4
    │           └── analysis-<policy-id>.json
    ├── jobs/
    │   └── <job-id>/
    │       ├── job.json
    │       ├── state.json
    │       ├── worker.json
    │       ├── cancel.request        仅请求取消时存在
    │       ├── events.jsonl
    │       ├── logs/
    │       │   └── <attempt-id>.log
    │       ├── stages/
    │       │   └── <stage-name>/
    │       │       └── checkpoint.json
    │       ├── media/
    │       │   └── facts.json
    │       ├── segmentation/
    │       │   ├── candidate-timeline.json
    │       │   └── shot-timeline.json
    │       ├── evidence/
    │       │   ├── frames/
    │       │   ├── contact-sheets/
    │       │   ├── evidence-timeline.json
    │       │   ├── transcript.json
    │       │   ├── ocr.jsonl
    │       │   └── cinematography/
    │       │       ├── index.json
    │       │       ├── frames/
    │       │       └── contact-sheets/
    │       ├── windows/
    │       ├── semantics/
    │       │   └── cinematography-annotations.jsonl
    │       ├── plans/
    │       ├── summaries/
    │       ├── retrieval/
    │       │   └── segments.jsonl
    │       ├── renders/
    │       ├── reports/
    │       ├── model-runs/
    │       └── manifest.json
    └── runtime/
        ├── locks/
        └── provider-cooldowns/
```

`semvideo-data/` 之外不写入任务内容。目录名使用不透明 ID。调用方通过 CLI 或产物 ID 查询，不得从绝对路径推断领域含义。源视频默认复制到工作区的 `sources/`；`source.json` 保存内容哈希和原始文件名。同一工作区内相同内容哈希只保存一份源视频，但可被多个任务引用；不同工作区之间不做隐式全局去重。

所有小型状态 JSON 使用同目录临时文件、刷新和原子替换。JSONL 事件只能追加完整行。任何产物只有在内容校验和阶段检查点同时成功后才可复用。

### manifest.json

任务清单是最后生成的索引：

```json
{
  "schema_version": 1,
  "job_id": "job_01...",
  "source_video_id": "video_01...",
  "profile": {
    "name": "default",
    "version": 1
  },
  "state": "completed",
  "artifacts": [
    {
      "artifact_id": "artifact_01...",
      "kind": "candidate_timeline",
      "path": "segmentation/candidate-timeline.json",
      "content_hash": "sha256:...",
      "schema_version": 1
    }
  ],
  "counts": {
    "candidate_segments": 42,
    "final_segments": 12,
    "review_required": 2
  }
}
```

manifest 必须登记任务结论可追溯所需的全部流水线产物。对于镜头语言阶段，除
`evidence/cinematography/index.json` 与标注 JSONL 外，还必须逐文件登记
`evidence/cinematography/frames/` 和 `evidence/cinematography/contact-sheets/`
下被索引引用的证据及内容哈希。阶段 checkpoint 的登记不能替代最终 manifest。

## 5. 文件任务状态与恢复

### state.json

`state.json` 是任务当前状态的权威快照：

```json
{
  "schema_version": 1,
  "job_id": "job_01...",
  "state": "analyzing",
  "current_stage": "analyze",
  "attempt_id": "attempt_01...",
  "progress": {
    "completed_units": 2,
    "total_units": 5,
    "unit": "evidence_window"
  },
  "updated_at": "2026-07-27T12:03:14Z",
  "failure": null
}
```

`events.jsonl` 是追加式审计记录，不是当前状态的唯一事实来源。`manifest.json` 只在任务形成完整可用结果后生成或更新。

### worker.json

```json
{
  "schema_version": 1,
  "job_id": "job_01...",
  "attempt_id": "attempt_01...",
  "pid": 18420,
  "process_started_at": "2026-07-27T12:00:02Z",
  "command_version": 1,
  "log_path": "logs/attempt_01....log"
}
```

CLI 判断 Worker 是否仍属于该任务时必须同时核对 PID 和进程创建时间。PID 不存在或创建时间不匹配时，`running` 任务被协调为 `interrupted`。

### checkpoint.json

```json
{
  "schema_version": 1,
  "stage": "evidence",
  "status": "succeeded",
  "implementation_version": "1",
  "config_hash": "sha256:...",
  "input_hashes": ["sha256:..."],
  "outputs": [
    {
      "path": "evidence/evidence-timeline.json",
      "content_hash": "sha256:..."
    }
  ],
  "completed_at": "2026-07-27T12:02:00Z"
}
```

恢复规则：

- `resume` 从第一个缺失、失败或校验不通过的阶段继续。
- `retry --from <stage>` 使指定阶段及其下游检查点失效。
- 进程意外退出不删除已经验证的检查点或不可变产物。
- 机器重启后不自动执行任务；Agent 先查询，再显式 `resume`。
- 取消通过创建 `cancel.request` 协作完成；Worker 在安全检查点停止并将任务写为 `cancelled`。

### 并发文件锁

- 媒体、ASR、LLM 和渲染分别使用固定数量的锁槽位。
- CPU 密集型媒体分析和软件渲染还共享 `ffmpeg_cpu = 2` 总预算。
- 槽位数量来自配置文件，内置值只是默认值。
- Worker 进入受限阶段前获取一个槽位，离开阶段后释放。
- Worker 先获取共享 `ffmpeg_cpu` 槽，再获取 `media` 或 `render` 分类槽；所有路径必须使用同一锁顺序。
- 锁由进程持有，进程崩溃或退出时由操作系统释放。
- Windows 打开共享锁文件时的 sharing violation 按锁竞争处理，在同一有界
  等待期限内退避重试；期限耗尽返回可重试的锁超时，不得归类为内部错误。
- V1 不使用租约和 Worker 心跳。
- 阶段超时、模型请求超时和 FFmpeg 子进程退出码负责识别“进程仍在但工作已卡住”的情况。

当前开发机认证结果：

| 资源 | V1 默认值 | 当前建议操作上限 | 当前开发机已测试上限 | 解释 |
|---|---:|---:|---:|---|
| `media` | `2` | `2` | `8` | 1–8 路总吞吐几乎不增长；8 只表示短测无错 |
| `asr` | `1` | `2` | `4` | `base/int8`、每 Worker 4 CPU 线程；4 路无吞吐收益 |
| `llm` | `2` | 未知 | 未知 | 供应商未公开当前账户和模型的同时在途请求上限 |
| `render` | `1` | `2` | `8` | `libx264` 8 路总吞吐下降；8 只表示短测无错 |

“已测试上限”不是跨机器硬上限。硬件、ASR Profile、编码器或模型供应商账户改变后必须重新认证。配置高于已测试上限时允许显式覆盖，但 `doctor` 和配置验证必须输出“未验证组合”警告。

本机混合短测表明 `media` 与 `render` 会竞争同一 CPU，因此 V1 接受共享 `ffmpeg_cpu = 2` 预算。该预算限制两类 CPU 密集型 FFmpeg 进程的合计并发，不改变各自分类默认值。未来硬件编码路径应使用独立资源预算和 Profile，不得未经认证直接绕过共享限制。

详细证据：

- [`research/CONCURRENCY_MEDIA_RENDER_LOCAL.md`](./research/CONCURRENCY_MEDIA_RENDER_LOCAL.md)
- [`research/CONCURRENCY_ASR_LOCAL.md`](./research/CONCURRENCY_ASR_LOCAL.md)
- [`research/CONCURRENCY_LLM_OFFICIAL_LIMITS.md`](./research/CONCURRENCY_LLM_OFFICIAL_LIMITS.md)

## 6. CLI 契约

工作命令名暂定为 `semvideo`。

所有正式版本提供：

```text
semvideo --version [--json]
```

机器可读版本结果至少包含 CLI 版本、支持的工作区 Schema 范围、支持的任务 Schema 范围和 Skill 协议版本。

### 工作区初始化与发现

```text
semvideo init <workspace-root> [--json]
semvideo workspace show [--workspace <path>] [--json]
```

`init` 创建：

```text
<workspace-root>/semvideo-data/workspace.json
<workspace-root>/semvideo-data/config.toml
<workspace-root>/semvideo-data/sources/
<workspace-root>/semvideo-data/jobs/
<workspace-root>/semvideo-data/runtime/
```

规则：

- 已存在兼容工作区时 `init` 保持幂等，不覆盖已有配置或任务。
- 已存在不兼容标记、普通文件占用目标目录或目录不可写时失败。
- 除 `init` 外，工作区命令默认从当前目录向父目录查找 `semvideo-data/workspace.json`。
- 通用选项 `--workspace <path>` 显式指定工作区根目录，并优先于自动发现。
- Windows CLI Adapter 接受原生盘符路径和常见 MSYS `/c/...` 盘符路径；
  标准化必须发生在进入应用接口之前。
- 找不到工作区时返回配置错误并提示运行 `semvideo init`，不得静默创建。
- `workspace show --json` 返回工作区 ID、根目录、数据目录、Schema 版本和配置文件位置。

### 环境诊断

```text
semvideo doctor [--workspace <path>] [--json]
```

检查 Python、FFmpeg/ffprobe、文件任务目录读写、原子替换、锁能力、模型配置、PyAV、ASR 和本地证据模块。诊断不得发起付费模型请求。

工作区配置可显式指定非敏感媒体工具路径：

```toml
[media]
ffmpeg_path = "C:/absolute/path/to/ffmpeg.exe"
ffprobe_path = "C:/absolute/path/to/ffprobe.exe"
analysis_proxy_max_width = 1280
analysis_proxy_max_height = 720
analysis_proxy_codec = "libx264"
analysis_proxy_preset = "veryfast"
analysis_proxy_crf = 23
```

未配置时使用命令名 `ffmpeg` 和 `ffprobe` 并按进程环境解析。Doctor、Worker
和 `segment export` 必须读取同一配置，不得各自实现不同的 PATH 解析规则。
Doctor 除版本和 `-fps_mode` 外还检查 V1 渲染所需的 `libx264` 与 `aac`。
诊断临时文件清理失败不阻断其他检查；`atomic_replace.warnings` 使用稳定
`code` 报告该告警。

Agent 通过公开配置用例写入路径，不直接修改任务包：

```text
semvideo config set-media-tools
  --ffmpeg <absolute-path>
  --ffprobe <absolute-path>
  [--workspace <path>]
  [--json]
```

应用接口验证两个路径均为文件，再原子更新 `[media]` 并返回生效值。
更新媒体工具路径时必须保留已有分析代理策略字段。

`analysis_proxy_max_width` 与 `analysis_proxy_max_height` 是旋转后显示尺寸的包围盒。
任一显示尺寸超限时生成保持宽高比、不放大、视频轨独占的分析代理；两项均未超限
时直接使用源视频。视觉候选切分、证据帧和镜头语言阶段读取代理，字幕、ASR、
`process --render`、`segment export` 和 `shot export` 读取受管源视频。代理
sidecar 至少登记 Schema、源视频 ID、源哈希、策略、相对路径和代理哈希；校验失败
必须重新生成，不能静默复用。

### 提交并运行

```text
semvideo process <input-video>
  [--workspace <path>]
  [--profile <name>]
  [--render | --no-render]
  [--idempotency-key <key>]
  [--wait]
  [--json]
```

默认行为是复制或复用源视频、创建任务、启动独立无窗口 Worker，然后返回任务 ID。命令成功退出只表示任务已接受且 Worker 已启动，不表示处理已经完成。

`--wait` 持续观察同一个文件任务直到终态；观察进程被关闭不会停止 Worker。V1 不要求终端窗口一直存在。

默认使用 `--no-render`：完成视频理解、正式时间线、摘要和检索片段记录，但不批量生成子视频。`--render` 显式要求任务完成前渲染并校验全部正式片段。

### 查询与控制

```text
semvideo job list [--workspace <path>] [--state <state>] [--json]
semvideo job admission [--workspace <path>] [--json]
semvideo job status <job-id> [--workspace <path>] [--json]
semvideo job logs <job-id> [--workspace <path>] [--follow] [--quiet] [--json]
semvideo job cancel <job-id> [--workspace <path>] [--json]
semvideo job resume <job-id> [--workspace <path>] [--json]
semvideo job retry <job-id> [--workspace <path>] [--from <stage>] [--json]
```

查询和控制命令默认只作用于当前工作区。`status` 在返回前协调 Worker 身份：若状态声称正在运行但 PID 与创建时间不匹配，则原子写入 `interrupted`。`resume` 创建新的处理尝试，不复用旧 Worker 身份。

`job admission` 返回应用层当前的任务准入快照：

```json
{
  "schema_version": 1,
  "configured_limit": 2,
  "active_count": 1,
  "available_submission_slots": 1,
  "active_job_ids": ["job_001"],
  "active_jobs": [
    {
      "job_id": "job_001",
      "state": "analyzing",
      "current_stage": "analyze",
      "attempt_id": "attempt_001"
    }
  ]
}
```

该快照供编排参考；真正的硬限制由 `process`、`job resume` 和 `job retry` 在
`submit.lock` 内原子复核。容量耗尽时返回
`category=resource_transient`、`code=job_admission_capacity_reached`、
`retryable=true`、`recovery=retry_same`，且不得创建新任务或启动新 Worker。

### 检查与导出

```text
semvideo inspect <job-id> [--workspace <path>] [--open]
semvideo segment list <job-id> [--workspace <path>] [--review-only]
  [--offset <n>] [--limit <n>] [--json]
semvideo segment show <job-id> <final-segment-id> [--workspace <path>] [--json]
semvideo segment export <job-id> <final-segment-id> [--workspace <path>] --output <path>
semvideo shot list <job-id> [--workspace <path>] [--viewpoint <value>]...
  [--scale <value>]... [--motion <value>]... [--speed <value>]...
  [--keyword <value>]... [--offset <n>] [--limit <n>] [--json]
semvideo shot show <job-id> <shot-id> [--workspace <path>] [--json]
semvideo shot export <job-id> <shot-id> [--workspace <path>] --output <path>
```

`inspect` 生成或返回本地检查报告。`--open` 是人类便利功能，不能成为非交互流程的必要步骤。

`segment list --json` 返回检索片段记录，支持 `--offset <n>` 和 `--limit <n>` 分页；默认不把全量结果嵌入 `process` 的完成响应。`segment show --json` 返回单条完整记录。`process --wait --json` 返回 `segment_records.path`，下游搜索 Module 可以直接流式读取 JSONL。

正式片段 ID 的唯一性范围是任务包，因此 `show` 与 `export` 必须同时给出 `job-id`。`job logs --json` 非跟随模式向标准输出写一个包含 `lines` 的最终 JSON；跟随模式把逐行日志事件写入标准错误，结束时才向标准输出写一个最终 JSON。

`segment list --json` 响应：

```json
{
  "schema_version": 1,
  "job_id": "job_01...",
  "total": 12,
  "offset": 0,
  "limit": 50,
  "items": [
    {
      "segment_id": "segment_001",
      "start_ms": 0,
      "end_ms": 24100,
      "title": "直升机起飞并进入巡逻航线",
      "short_summary": "边防官兵搭乘直升机起飞，开始执行空中巡逻。",
      "topics": ["边防巡逻", "直升机"],
      "keywords": ["边防", "直升机", "空中巡逻"],
      "confidence": 0.93,
      "review_required": false
    }
  ]
}
```

分页列表默认省略完整 `transcript.text` 和详细 provenance，只保留转写质量等轻量状态；`segment show --json` 与 `retrieval/segments.jsonl` 返回包含完整片段转写的检索片段记录。精简字段集合必须版本化，不能静默删除下游依赖字段。

Semvideo CLI 不提供查询、embedding、相关性评分或 `--top-k` 参数。

`segment export` 按需生成指定正式片段的视频，`--output` 必须位于任务包外。
若 `process --render` 已产生且 manifest 哈希验证通过，命令可以只复制该登记产物；
否则直接渲染到用户输出路径。无论哪种情况，命令都不得创建任务包内缓存、
sidecar，或改写检索记录、checkpoint、manifest 和检查报告。

`shot list --json` 返回镜头事实、镜头语言标注以及与其时间相交的正式片段 ID；
`--viewpoint`、`--scale`、`--motion`、`--speed` 和 `--keyword` 是确定性字段过滤，
不执行相关性排名。同一个选项可以重复；重复的同类条件全部必须满足。
`shot show` 返回单条完整记录。`shot export` 按镜头的半开时间范围渲染到任务包外，
不得修改已完成任务包。

### 配置

```text
semvideo config show [--workspace <path>] [--json]
semvideo profile list [--workspace <path>] [--json]
semvideo profile validate <name> [--workspace <path>] [--json]
```

Agent 在任何会启动或改变任务的命令前，必须先运行 Skill 自带的
`scripts/load_context.py`。该门禁只通过公开 CLI 读取工作区、完整有效配置、
Profile、Doctor、任务列表与应用层任务准入快照，并返回
`admission.available_submission_slots`。该值是本轮 `process`/`resume`/`retry`
的编排上限；应用接口仍在提交锁内执行最终硬校验。状态变化后必须重新加载，禁止
将整个目录无界并发提交。

配置优先级：

1. 当前命令的显式非敏感覆盖参数。
2. 工作区 `semvideo-data/config.toml`。
3. 内置安全默认值。

V1 的模型凭据不属于上述配置值：

- API Key 只从配置指定的环境变量名读取；默认名称为 `SEMVIDEO_API_KEY`。
- `config.toml` 可以保存提供商、模型、Base URL 和 `credential_env = "SEMVIDEO_API_KEY"`，不得保存环境变量对应的值。
- Worker 继承启动它的 CLI 进程环境；凭据不得出现在命令行参数、`worker.json`、任务产物、事件或日志中。
- `doctor`、`process`、`resume` 和需要重新调用模型的 `retry` 只报告凭据“存在/缺失”，不得显示长度、前后缀或原始值。
- `job list/status/logs`、检查报告和已有片段导出不要求模型凭据。
- 新处理尝试缺少凭据时，在启动昂贵阶段前以配置错误失败，不修改已经验证的检查点。
- Agent Skill 只能检查 CLI 返回的布尔诊断结果，不得读取、回显或保存凭据。
- V1 不实现 `.env` 自动加载、凭据数据库或 Windows Credential Manager。
- V1 不配置单任务 token 或货币硬预算。模型请求数和输入规模由视频时长、证据窗口及模型上下文能力决定，与调用 Semvideo 的主 Agent token 预算无关。
- 单次请求可以设置技术性输入/输出上限，防止超过模型上下文或生成无界响应；这不是单任务费用预算。
- 每次模型运行仍记录实际输入/输出 token、请求次数和可获得的费用信息，仅用于观察、评测和人工调优。

## 7. 标准输出、标准错误和退出码

正常结果写入标准输出；进度、警告和诊断写入标准错误。

`--json` 模式：

- 标准输出只包含一个最终 JSON 值。
- `process --wait` 和 `job logs --follow` 的进度以 JSON Lines 写入标准错误，或通过 `--quiet` 关闭。
- 不输出 ANSI 颜色。
- 字段变更遵守版本策略。

退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 命令成功 |
| `2` | CLI 参数或配置无效 |
| `3` | 输入视频无效或不可访问 |
| `4` | 外部依赖缺失 |
| `5` | 模型或外部提供商失败 |
| `6` | 处理任务失败 |
| `7` | 任务已取消 |
| `8` | 存在必须人工处理的结果，且命令要求无复核完成 |
| `9` | 任务已中断，需要显式恢复 |
| `10` | 内部错误 |

## 8. 进度事件

```json
{
  "schema_version": 1,
  "event": "stage_progress",
  "job_id": "job_01...",
  "stage": "analyze",
  "completed_units": 2,
  "total_units": 5,
  "unit": "evidence_window",
  "message": "正在分析证据窗口 2/5",
  "timestamp": "2026-07-27T12:03:14Z"
}
```

CLI、Worker 和未来可能出现的 HTTP 服务使用同一事件结构。事件是观察数据，不是任务状态的唯一事实来源。

## 9. 错误模型

```json
{
  "schema_version": 1,
  "code": "model_invalid_response",
  "category": "model_response_invalid",
  "message": "模型返回未通过 SegmentMeaning 结构验证。",
  "retryable": true,
  "recovery": "retry_same",
  "stage": "analyze",
  "job_id": "job_01...",
  "attempt_id": "attempt_01...",
  "entity_id": "candidate_018",
  "details_artifact_id": "artifact_error_018",
  "diagnostic_id": "diag_01..."
}
```

`category` 与恢复策略：

| category | 示例 | recovery | Agent 行为 |
|---|---|---|---|
| `invocation_error` | 参数格式、路径写法、缺少必需选项 | `correct_and_retry` | 能确定修正方式时修正一次 |
| `config_error` | 非敏感配置字段格式或并发值非法 | `correct_and_retry` | 只修正明确字段，然后重试一次 |
| `input_error` | 视频不存在、不可访问或已损坏 | `user_action` | 停止并报告输入问题 |
| `dependency_error` | FFmpeg 缺失或版本不兼容 | `user_action` | 报告诊断，不自动安装依赖 |
| `credential_error` | 环境变量缺失或凭据被拒绝 | `user_action` | 不读取凭据，报告所需环境变量名 |
| `provider_transient` | 429、503、504、网络超时 | `retry_same` | 遵守 CLI 给出的退避和次数上限 |
| `resource_transient` | 本地文件锁在有界等待后仍不可用 | `retry_same` | 保持输入不变，稍后重试一次 |
| `model_response_invalid` | 模型 JSON、边界引用或覆盖无效 | `retry_same` | 由 CLI 执行有限结构修复；耗尽后报告 |
| `interrupted` | Worker 消失或机器重启 | `resume` | 校验检查点后恢复 |
| `cancelled` | 已接受取消请求 | `none` | 不自动恢复 |
| `internal_error` | 未处理异常、状态机错误、不变量破坏 | `report_bug` | 不重试，保留诊断并报告缺陷 |

`recovery` 枚举固定为 `correct_and_retry/retry_same/resume/user_action/report_bug/none`。

自动恢复约束：

- Agent 不从自然语言 `message` 猜测恢复策略，只读取 `category`、`retryable` 和 `recovery`。
- `correct_and_retry` 仅适用于能够确定的新命令、路径或非敏感配置；相同修正指纹最多尝试一次。
- Skill 与 CLI 参数不兼容属于版本兼容错误，不允许随机尝试其他参数。
- Agent 不直接修改任务状态、模型响应、阶段产物或程序源码。
- `provider_transient` 和 `model_response_invalid` 的请求级重试首先由 CLI 在阶段内部有界执行；Skill 只在 CLI 最终仍标记可重试时创建新的处理尝试。
- 模型提供商共享冷却锁超时使用
  `provider_cooldown_lock_timeout/provider_transient/retry_same`，不得退化为
  `internal_error/report_bug`。
- `internal_error`、领域不变量错误和未知错误一律不自动重试。

异常堆栈写入受控诊断日志，不直接进入机器可读标准输出。

## 10. 契约版本

- JSON 使用整数 `schema_version`。
- Prompt 使用独立字符串版本。
- 处理策略包含名称和版本。
- 算法记录名称、版本和有效参数。
- 读取方必须拒绝高于自身支持范围的必需 Schema。
- 改名、改变含义、改变时间单位或枚举语义属于不兼容变更。
- 新增可选字段属于兼容变更。

## 11. Agent Skill 契约

- Skill 源码保存在本仓库 `skills/semvideo/`，与 CLI 使用同一版本号。
- 开发时修改仓库内 Skill 源码；安装或发布阶段再注册到通用 Agent 的技能目录。
- Skill 执行任务前先运行随包 resolver，定位并验证 CLI 的绝对路径，再以该
  路径调用 `--version --json`；不得假定目标 Agent 保留用户级 Python Scripts
  的 PATH。
- Skill 再调用 `semvideo doctor --json`，只根据结构化诊断决定能否提交或恢复任务。
- Skill 只根据错误对象的 `category/retryable/recovery` 选择修正、重试、恢复或报告，不解析自然语言日志猜测原因。
- 对 `correct_and_retry`，同一修正指纹最多执行一次；对 `internal_error` 和 `report_bug` 不执行自动重试。
- Skill 只能调用公开 CLI，不得直接读取、写入、修复或删除 `semvideo-data/` 中的状态文件。
- Skill 不复制领域判断、状态协调、重试分类或并发控制逻辑。
- CLI 版本不兼容时，Skill 停止变更操作并返回可执行的升级说明；查询已有任务只有在 CLI 明确声明向后只读兼容时才允许继续。
