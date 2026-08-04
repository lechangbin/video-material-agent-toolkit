# Semvideo 镜头事实与细粒度片段方案

## 结论

Semvideo 不需要从零训练镜头检测模型，但需要自行实现镜头时间线、镜头语言标注、
细粒度语义片段、产物契约和确定性校验。

CRV 开源版可借鉴的主要是“场景变化感知取帧 + 去重 + 时间戳转写 + 九宫格证据”
思路。它没有提供可直接复用的稳定镜头表、镜头摘要、关键词或正式片段内的语义归并。

推荐同时保留三种不同层次的数据：

1. **镜头事实（Shot）**：由确定性算法识别的拍摄或剪辑连续区间。
2. **镜头语言标注（Cinematography Annotation）**：描述每个镜头的视点、景别和随时间
   发生的运镜变化。
3. **正式片段（Final Segment）**：描述完整叙事事件，可跨越多个镜头。
4. **细粒度片段（Fine-grained Segment，暂定名）**：正式片段内用于精细理解、检索和
   按需导出的连续内容单元；它可以包含多个相邻镜头，也可以在超长连续镜头内按动作或
   转写阶段切分。

镜头事实不是检索结果，细粒度片段也不等同于每次切镜。

## 当前交付范围

`semvideo 0.1.2` 已交付镜头事实、镜头内多联系图证据、前景抑制后的全局运动与速度
曲线、镜头语言标注，以及 `shot list/show/export`。正式片段仍可跨越多个镜头。

本文其余“细粒度片段”及 `retrieval/fine-segments.jsonl`、`unit list/show/export`
描述的是下一阶段目标，尚未在 0.1.2 实现。当前版本不得宣称完整交付本文 B/C 阶段，
也不得用 `shot` 冒充正式片段内部的细粒度语义单元。

## CRV 开源版的能力边界

核查基于 CRV 官方仓库 `master` 的 0.7.17 代码。

- CRV 使用 FFmpeg `scene` 分数阈值和固定密度下限选择关键帧。
- 自适应模式根据逐帧 scene score 和滚动均值补充关键帧。
- 后续滑窗去重和总帧数限制可能删除场景变化帧。
- `frames.json` 保存的是保留帧及时间戳，不是带 `shot_id/start/end` 的镜头表。
- Lite timeline 只把关键帧放入转写时间段，不做镜头检测或镜头语义标注。
- README 将逐镜头时长、剪辑节奏和运镜标签列为 Pro 能力。

因此，CRV 的 scene-aware 不应被解释为完整的镜头标注系统。

可借鉴：

- FFmpeg scene-score 候选；
- 源视频 PTS 时间戳保留；
- 关键帧去重；
- 时序九宫格；
- 带时间戳转写和视觉证据对齐。

不直接复用：

- CRV 运行时依赖；
- 将保留关键帧时间当作可信镜头边界；
- 将关键帧去重结果当作镜头时间线；
- 未版本化的镜头或语义输出。

CRV 将完整 shot table 和运镜标注列为 Pro 能力，因此 Semvideo 不能依赖其开源版提供
“航拍大镜头”“缓慢推进”或“逐渐拉高”等结果。这部分属于 Semvideo 的正式开发范围。

## 推荐领域模型

### 镜头事实

镜头由 `ShotTimeline` 记录，属于确定性媒体事实。

建议字段：

```json
{
  "schema_version": 1,
  "source_video_id": "video_...",
  "detector": {
    "name": "ffmpeg_scene",
    "version": "1",
    "parameters": {
      "threshold": 0.3,
      "min_shot_ms": 750
    }
  },
  "shots": [
    {
      "shot_id": "shot_0001",
      "ordinal": 0,
      "start_ms": 0,
      "end_ms": 4380,
      "right_boundary": {
        "kind": "hard_cut_candidate",
        "score": 0.71,
        "detector": "ffmpeg_scene"
      },
      "evidence_frame_ids": ["frame_0001", "frame_0002"]
    }
  ]
}
```

约束：

- 镜头必须按时间排序、无重叠并覆盖源视频。
- `max_duration` 补点不能伪装成真实镜头边界。
- 检测器只能报告自己能证明的边界类型；FFmpeg 基线默认标记
  `hard_cut_candidate`，不虚构精确的淡入淡出类型。
- 镜头时间线必须在关键帧去重之前形成并单独保存。

### 镜头语言标注

镜头语言标注附着于单个镜头，不能折叠进普通 `visual_summary`。建议字段：

```json
{
  "schema_version": 1,
  "shot_id": "shot_0001",
  "viewpoint": ["aerial"],
  "shot_scale": {
    "start": "extreme_wide",
    "end": "extreme_wide"
  },
  "camera_motions": [
    {
      "type": "rise",
      "direction": "up",
      "speed": "slow",
      "temporal_profile": "gradual",
      "start_ms": 1200,
      "end_ms": 6800
    },
    {
      "type": "push_in",
      "speed": "slow",
      "temporal_profile": "gradual",
      "start_ms": 7000,
      "end_ms": 11400
    }
  ],
  "cinematography_summary": "航拍大全景缓慢升高，并逐步向巡逻区域推进。",
  "cinematography_keywords": ["航拍", "大全景", "缓慢推进", "逐渐拉高"],
  "measurement_refs": ["motion_track_0001"],
  "evidence_frame_ids": ["frame_0001", "frame_0003", "frame_0006"],
  "model_run_id": "model_run_...",
  "confidence": 0.88
}
```

最低正式词表应覆盖：

- 视点：`aerial`、`ground`、`interior`、`overhead`、`low_angle`、`high_angle`。
- 景别：`extreme_wide`、`wide`、`medium`、`close_up`、`extreme_close_up`。
- 运镜：`static`、`pan`、`tilt`、`push_in`、`pull_out`、`rise`、`fall`、
  `tracking`、`orbit`、`handheld`、`compound`、`unknown`。
- 时间特征：方向、开始/结束时间、快慢、幅度以及匀速/加速/减速/渐进。

“推进”和“变焦”仅凭稀疏关键帧往往难以可靠区分。无法证明物理摄影机运动还是光学变焦
时，应输出视觉效果级标签 `push_in/pull_out`，不能虚构 `dolly/zoom`。

镜头语言检测采用混合实现：

1. 在镜头内部按时间提取连续小帧序列，而不是只使用一张关键帧或跨镜头九宫格。
2. 本地算法估计背景主导的全局平移、缩放、旋转和速度曲线，并抑制前景物体运动。
3. 多模态模型结合帧序列、时间戳、测量特征和上下文，判断航拍视点、景别和自然语言
   运镜含义。
4. 确定性校验器验证标签词表、时间范围、证据引用和置信度。

本地测量提供运动证据，模型负责语义命名。两者都不应单独作为最终结果。

### 正式片段

沿用现有正式片段。正式片段描述完整叙事事件，边界不由切镜机械决定。

例如“官兵搭乘直升机完成一次空中巡逻”可以包含：

- 登机镜头；
- 直升机起飞的远景；
- 机舱内官兵观察镜头；
- 空中飞行和地面俯拍镜头。

这些镜头可以属于同一个正式片段。

### 细粒度片段

细粒度片段是正式片段的子级语义单元，面向 Agent 的精细理解和后续搜索。

建议字段：

```json
{
  "schema_version": 1,
  "fine_segment_id": "fine_0001_0002",
  "parent_final_segment_id": "segment_0001",
  "start_ms": 4380,
  "end_ms": 12640,
  "source_shot_ids": ["shot_0002", "shot_0003"],
  "title": "直升机离地并爬升",
  "short_summary": "直升机从停机坪起飞，随后进入爬升阶段。",
  "visual_summary": "远景展示旋翼启动、机身离地和向山地上空爬升。",
  "keywords": ["直升机起飞", "爬升", "停机坪", "边防巡逻"],
  "entities": ["直升机", "边防官兵"],
  "actions": ["起飞", "爬升"],
  "transcript_span_ids": ["transcript_0004"],
  "evidence_frame_ids": ["frame_0012", "frame_0015"],
  "model_run_id": "model_run_...",
  "confidence": 0.91
}
```

约束：

- 子片段不得跨越父正式片段。
- 同一父片段下的子片段必须连续、无重叠并完整覆盖父范围。
- 子片段内部可以跨镜头。
- 长连续镜头允许使用动作、转写或时长锚点细分。
- 摘要和关键词只能描述子片段实际范围内的证据。
- 关键词应区分本地观察所得与父片段继承项，避免所有子片段复制同一组标签。

## 推荐流水线

```text
源视频
  → 媒体探测
  → 镜头边界候选
  → 镜头时间线（确定性）
  → 镜头内运动测量与镜头语言标注
  → 证据帧、九宫格、转写
  → 现有长上下文叙事分段
  → 正式片段
  → 父片段内细粒度语义分段
  → 子片段摘要与关键词
  → 检索记录与按需导出
```

细粒度语义分段建议在正式片段确定后执行，原因是它必须引用稳定的父片段 ID，并且
父边界能够限制模型输出范围。

模型输入按父正式片段组织，包含：

- 父正式片段标题和摘要；
- 片段内按时间排序的镜头表；
- 九宫格及每格时间戳、镜头 ID；
- 带起止时间的转写；
- 可选内部锚点；
- 明确的目标粒度和连续覆盖约束。

镜头语言标注应单独按镜头或连续镜头批次处理，输入比叙事九宫格更密集的时间序列；
随后在细粒度片段记录中按时间顺序汇总所包含镜头的摄影语言。不能让一个跨多次切镜的
父正式片段只得到一个含混的运镜标签。

模型一次输出子片段边界、摘要、视觉摘要和关键词。无需再为每个子片段单独调用一次
模型。多个很短的父片段可以在同一请求中批处理，但输出必须仍以父片段分组。

模型 Prompt 必须同时声明：

1. 不要因为机位、景别或构图变化就机械切分。
2. 当动作目标、视觉焦点、叙事功能或转写阶段发生实质变化时可以切分。
3. 同一父片段主题相同不代表所有内容都应合成一个子片段。
4. 九宫格格子只是观察点。
5. 优先选择真实镜头边界；长连续镜头可选择其他允许锚点。

## 确定性校验

模型输出不能直接落为正式产物。校验器至少验证：

- 父片段 ID 存在；
- 子片段 ID 唯一；
- 时间按顺序递增；
- 无越界、空洞、重叠；
- 完整覆盖父片段；
- 内部边界来自允许锚点；
- `source_shot_ids` 与时间范围相交；
- 摘要和关键词非空；
- 证据引用存在且位于子片段范围内；
- Schema 版本受支持。

校验失败时沿用现有模型响应修复机制，仅修复一次；再次失败则保留原始响应和修复响应，
以稳定错误类别报告，不允许静默生成不完整子时间线。

## 产物与 CLI

建议新增任务包产物：

```text
segmentation/shot-timeline.json
semantics/cinematography-annotations.jsonl
semantics/fine-segment-proposals/
retrieval/fine-segments.jsonl
model-runs/
```

现有 `retrieval/segments.jsonl` 继续保存正式片段，避免破坏既有调用方。新增
`retrieval/fine-segments.jsonl` 保存子片段，两者通过
`parent_final_segment_id` 关联。

建议应用接口先行，CLI 只做 Adapter：

```text
list_fine_segments(job_id, parent_segment_id?, page?)
get_fine_segment(job_id, fine_segment_id)
export_fine_segment(job_id, fine_segment_id, output)
```

对应 CLI 可以是：

```text
semvideo unit list <job-id> [--parent <segment-id>] --json
semvideo unit show <job-id> <unit-id> --json
semvideo unit export <job-id> <unit-id> --output <path> --json
```

子视频仍按需导出，不默认渲染全部子片段。

## 检测算法选择

### 第一阶段：现有 FFmpeg 基线

直接复用 `FfmpegAdapter.detect_scenes()`，新增独立 `ShotTimelineBuilder`，并实现首个
`CinematographyAnalyzer`，覆盖航拍视点、景别、推进/拉远、升高/降低和速度描述。

需要补足：

- 在去重前保存镜头边界；
- 对相邻高分点做最短镜头约束和非极大值抑制；
- 保留检测器、阈值和分数；
- 将真实 scene change 与 `max_duration` 锚点分开；
- 生成完整镜头区间。
- 为每个镜头提取有时间间隔的运动帧序列。
- 生成可审计的镜头语言标注和文案关键词。

这是推荐的首个实现，不增加大型依赖，也能直接用当前真实样例验收。

### 第二阶段：PySceneDetect 对照

当真实样例证明 FFmpeg 固定阈值存在明显误报或漏检时，在统一 `ShotDetector` 接口后加入
PySceneDetect 实验实现。

可评估：

- `ContentDetector`：相邻帧 HSV 内容变化，适合快速切镜；
- `AdaptiveDetector`：使用滚动平均，较能抑制快速运镜导致的误报；
- `ThresholdDetector`：补充淡入淡出。

PySceneDetect 官方文档提示 API 仍在开发，应固定到下一主版本之前，并且只能在对照指标
胜出后成为默认实现。

### 第三阶段：TransNetV2（可选）

TransNetV2 是专用镜头边界神经网络，官方提供预训练推理代码。它会增加模型权重、运行时
依赖、安装和跨平台验证成本，因此不应作为第一版默认依赖。

只有在以下情况同时成立时再引入：

- FFmpeg/PySceneDetect 在目标视频集上不能达到人工验收指标；
- 渐变转场或复杂剪辑是主要视频类型；
- 本地运行成本可以接受；
- 能固定模型、运行时和可复现输出。

## 验收计划

自动化测试不调用云端模型。

确定性测试素材至少包含：

- 明显硬切；
- 淡入淡出；
- 快速摇摄但没有切镜；
- 单个超长连续镜头；
- 快速蒙太奇；
- 可变帧率视频。

测试内容：

- 镜头时间线连续覆盖；
- scene change 与强制时长锚点不混淆；
- 检测器失败的降级和错误分类；
- 子片段校验器拒绝空洞、重叠、越界和未知 Schema；
- 模型修复响应被审计；
- 恢复后不会重复已验证阶段。

真实视频验收使用现有直升机样例及一组不同剪辑风格视频，人工关注：

- 镜头切换召回和误报；
- 正式片段边界是否保持不变；
- 同一次连续动作是否没有按切镜破碎；
- 过长正式片段是否得到可检索的子片段；
- 子片段摘要和关键词是否只描述自身内容；
- Agent 能否仅凭 JSON 找到并按需导出目标范围。

## 分阶段开发建议

### A. 契约与镜头事实

- 在 `CONTEXT.md` 确认细粒度片段正式名称。
- 更新架构、契约和任务目录。
- 抽出 `ShotDetector` 接口。
- 基于现有 FFmpeg scene points 生成 `ShotTimeline`。
- 添加确定性测试和检查点。

### B. 细粒度语义

- 定义镜头语言 Schema、受控词表和 `CinematographyAnalyzer` Interface。
- 实现全局运动测量与多模态标注的混合流程。
- 为“航拍大镜头、缓慢推进、逐渐拉高”等文案条件添加查询友好的结构化字段。
- 定义子片段 Schema 和校验器。
- 新增父片段内的模型 Prompt。
- 复用九宫格、转写和模型审计。
- 生成 `retrieval/fine-segments.jsonl`。
- 添加假模型契约测试。

### C. CLI 与导出

- 增加应用接口。
- 增加 `unit list/show/export` CLI Adapter。
- 按需导出且同步任务包完整性记录。
- 更新 Semvideo Skill，只编排 CLI。

### D. 真实验收与检测器升级决策

- 使用真实视频跑 FFmpeg 基线。
- 人工标注少量镜头边界作为对照。
- 记录误报、漏检、子片段粒度和摘要质量。
- 只有指标显示必要时才引入 PySceneDetect 或 TransNetV2。

## 参考来源

- CRV README：
  <https://github.com/HUANGCHIHHUNGLeo/claude-real-video>
- CRV 核心取帧与去重：
  <https://github.com/HUANGCHIHHUNGLeo/claude-real-video/blob/1e0a29f6202cd2a28397b0c70f8fa11ab0f0ba26/src/claude_real_video/core.py>
- CRV Lite timeline：
  <https://github.com/HUANGCHIHHUNGLeo/claude-real-video/blob/1e0a29f6202cd2a28397b0c70f8fa11ab0f0ba26/src/claude_real_video/timeline_lite.py>
- FFmpeg Filters Documentation：
  <https://ffmpeg.org/ffmpeg-filters.html>
- PySceneDetect 官方文档：
  <https://www.scenedetect.com/docs/latest/>
- TransNetV2 官方仓库：
  <https://github.com/soCzech/TransNetV2>
