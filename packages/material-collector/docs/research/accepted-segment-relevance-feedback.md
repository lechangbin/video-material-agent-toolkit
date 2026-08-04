# 用“实际有效入库片段”驱动下一轮素材检索：依据、风险与推荐闭环

## 结论

可行，而且相比“每轮只读取搜索结果标题，再从标题盲目抽词递归”，这是更可靠的闭环：

```text
主题段与原始查询计划
  → 多平台搜索一批候选
  → 下载、切分、内容理解、质量/权利检查
  → 落库或拒绝
  → 汇总实际入库片段与拒绝原因
  → 判断主题面向是否已覆盖
  → 仅针对缺口生成下一轮查询
  → 达到充分性或预算后停止
```

理论上，它可被看作“显式相关性反馈（explicit relevance feedback）+ 主动搜索（active search）”；实际入库片段相当于经下游验证的正例，语义不相关的拒绝片段相当于负例。视频时刻检索研究也直接支持以自然语言查询定位和评价视频中的时间片段，而不只依赖整条视频的标题。

但没有找到原始论文直接实验验证“跨公开平台搜索 → 下载 → 切分落库 → 用有效片段摘要和拒绝原因递归补搜”这一整条产品流水线。因此：

- “相关性反馈能改善后续检索”“自然语言可以检索视频片段”“片段可以生成内容级结构化信息”属于直接证据；
- “将落库结果定义成反馈协议”“如何分离拒绝原因”“充分性指标和默认阈值”属于基于这些证据的工程推断，需要用真实剪辑任务校准。

最关键的设计约束是：

> 只有“语义相关/不相关”才能直接改变查询语义；下载失败、低清晰度、重复、带水印、权利不明等拒绝原因只能调整平台、过滤器和采集策略，不能作为“这个主题词不相关”的负反馈。

## 一手证据

### 1. 相关性反馈支持“用已验证结果改写下一轮查询”

相关性反馈的基本做法是根据已判定的相关项和不相关项调整查询表示。Wang、Fang 与 Zhai 对负相关反馈进行了系统研究：当首轮结果很差时，负例可用于改进后续未见结果的排序；但负例往往不是一个单一聚类，而是以不同方式偏离查询，因此使用多个负模型比把全部负例揉成一个模型更有效。

来源：[A Study of Methods for Negative Relevance Feedback, SIGIR 2008](https://www.eecis.udel.edu/~hfang/pubs/sigir08.pdf)，尤其见摘要与第 1 节。

这直接支持：

- 将实际入库的、主题相关片段作为正反馈；
- 将“语义偏离”按原因或偏离方向分类，而不是合并成一个模糊的负面词表；
- 下一轮同时保留原查询、强化正反馈、抑制明确的语义负反馈。

它不直接证明：

- 自动落库判定一定等同于可靠的人类相关性判断；
- 技术质量拒绝可以作为语义负反馈；
- 关键词字符串是唯一或最佳的反馈表示。

### 2. 多轮视觉检索已经验证了正负反馈和内容摘要的价值

WACV 2026 的一项视觉—语言检索研究把经典相关性反馈扩展到 VLM 检索，比较了伪相关反馈、由候选图像生成描述的生成式反馈、融合图像与文本细粒度特征的反馈摘要，以及显式反馈。实验显示，生成式反馈、反馈摘要和显式反馈相对无反馈均提升了检索；在多轮检索中，显式反馈持续改进，而较弱的生成式反馈在第三轮后可能退化；融合多模态细粒度信息的反馈摘要在第 3–5 轮趋于收敛并减轻查询漂移。

来源：

- [A Little More Like This: Text-to-Image Retrieval with Vision-Language Models Using Relevance Feedback, WACV 2026](https://openaccess.thecvf.com/content/WACV2026/html/Khaertdinov_A_Little_More_Like_This_Text-to-Image_Retrieval_with_Vision-Language_Models_WACV_2026_paper.html)
- [论文 PDF](https://openaccess.thecvf.com/content/WACV2026/papers/Khaertdinov_A_Little_More_Like_This_Text-to-Image_Retrieval_with_Vision-Language_Models_WACV_2026_paper.pdf)

该工作研究的是图像检索而不是跨平台短视频采集，但它是与你的方案最接近的直接多模态证据：以已检索视觉项及其内容描述形成反馈，可以改善多轮视觉搜索；只依赖自动生成或头部结果则可能在若干轮后漂移。

另一项 CLIP 图像检索研究也以用户二元反馈更新检索，并在多种偏好设定中优于无反馈检索。

来源：[Revisiting Relevance Feedback for CLIP-based Interactive Image Retrieval](https://arxiv.org/abs/2404.16398)。

### 3. “伪相关反馈”有效，但把头部结果自动当相关会产生查询漂移

伪相关反馈（PRF）把首轮排名靠前的结果假定为相关，并从中扩展或重加权查询。选择性反馈研究指出，PRF 在大量查询上平均可以改善效果，但也会把查询从原始信息需求带偏，并伤害部分查询，因此反馈不应无条件应用。

来源：[A Deep Learning Approach for Selective Relevance Feedback, ECIR 2024](https://eprints.gla.ac.uk/312863/)。

这说明“入库后再反馈”比“搜索标题即反馈”更有理论优势：前者至少经过内容、质量和用途校验，更接近显式相关反馈；后者只是 PRF，容易把热门话题、营销词、搬运标题或偶然实体递归放大。

不过，“已入库”仍不是天然正确的语义标签：

- 分割器可能误切；
- 内容理解模型可能误描述；
- 权利或技术过滤会造成选择偏差；
- 只反馈可下载平台的内容，会把搜索逐渐偏向“容易下载”而非“最适合主题”。

因此每一轮都必须使用原始 `theme_intent` 和 `required_facets` 重新校验反馈，不允许上一轮结果取代原始需求。

### 4. 视频研究支持“以片段而不是整条视频作为相关性单位”

DiDeMo 的原始工作明确把任务定义为：给定自然语言描述，在视频中检索特定时间片段。该数据集包含局部视频片段及其自然语言指代表达，证明“整条视频相关”与“其中某个时刻相关”是不同问题。

来源：[Localizing Moments in Video With Natural Language, ICCV 2017](https://openaccess.thecvf.com/content_iccv_2017/html/Hendricks_Localizing_Moments_in_ICCV_2017_paper.html)。

视频语料库时刻检索（VCMR）进一步把目标定义为：从大型视频语料中检索与自然语言查询最相关的视频时刻，而非只返回整条视频。MPGN 还从候选时间片段的视觉与文本信息生成伪查询，说明“从片段内容反向形成可检索文本表示”在研究上具有直接先例。

来源：[Modal-specific Pseudo Query Generation for Video Corpus Moment Retrieval, EMNLP 2022](https://aclanthology.org/2022.emnlp-main.530/)。

QVHighlights 方向的研究同时执行视频时刻检索和查询相关高光检测，目标包括定位时间区间并估计每个片段与文本查询的一致性/显著性。QD-DETR 通过构造不相关的视频—查询对并要求其获得低显著性分数，提升了查询与视频内容的一致性判断。

来源：[Query-Dependent Video Representation for Moment Retrieval and Highlight Detection, CVPR 2023](https://openaccess.thecvf.com/content/CVPR2023/html/Moon_Query-Dependent_Video_Representation_for_Moment_Retrieval_and_Highlight_Detection_CVPR_2023_paper.html)。

这直接支持：

- 反馈和充分性评估以“片段”为单位，而不是以“下载成功的视频数”为单位；
- 每个片段应保存起止时间、内容描述和与主题面向的对应关系；
- 标题仅作为发现元数据，不能证明视频内部存在可用片段。

QD-DETR 的负对训练属于离线模型训练证据，并不直接证明把生产环境拒绝项写入查询就会改善搜索；将它用于在线补搜仍是工程推断。

### 5. 现有官方工具证明片段级结构化反馈在工程上可获得

Azure AI Video Indexer 官方文档列出的内容洞察包括场景、镜头、关键帧、关键词、命名实体、主题、OCR、对象和带精确时间范围的人物信息；它还能按关键词搜索媒体库并跳转到视频中的具体时刻。

来源：

- [Azure AI Video Indexer insights overview](https://learn.microsoft.com/en-us/azure/azure-video-indexer/insights-overview)
- [Search for exact moments in videos](https://learn.microsoft.com/en-us/azure/azure-video-indexer/video-indexer-search)
- [Scene, shot, and keyframe detection](https://learn.microsoft.com/en-us/azure/azure-video-indexer/scene-shot-keyframe-detection-insight)

Gemini 官方视频理解文档说明模型可以描述、分段、提取视频信息并引用具体时间戳，但也明确提示默认按 1 FPS 采样，快速动作或快速切换可能遗漏细节。

来源：[Gemini API — Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)。

Amazon Rekognition Video 官方文档说明其异步接口可返回镜头和技术提示的起止时间、持续时间、置信度及 JSON 结果；镜头结果可以用于为最终编辑识别候选镜头。

来源：[Amazon Rekognition — Detecting video segments](https://docs.aws.amazon.com/rekognition/latest/dg/segments.html)。

这些官方能力证明“切分后返回摘要、关键词、实体、镜头时间和技术质量信息”在工程上可实施，但不证明任何一个服务的输出足以自动决定素材是否满足你的剪辑需求。

### 6. “从正例提取代表词再搜”是成熟检索能力，但应允许反例与人工控制

Elasticsearch 官方 `more_like_this` 查询从输入文档中选择代表性词项形成新查询，并支持 `like` 和 `unlike` 输入；官方文档还建议在需要更强控制时使用 Term Vectors 展示候选主题词，再由调用者选择用于深入搜索的词。

来源：[Elasticsearch — more_like_this query](https://www.elastic.co/guide/en/elasticsearch/reference/current/search-more-like-this.html)。

它直接证明“从已确认内容提取代表性词汇进行相似搜索”是成熟的工程模式。但其目标是索引内的相似文档检索，不能保证抽出的词能被抖音、小红书、Bilibili、YouTube 等平台的外部搜索正确理解；跨平台查询仍需适配、测试和降级。

## 正反馈与负反馈应如何分流

推荐把反馈分成至少四类，不能只存 `accepted: true/false`：

| 反馈类别 | 示例 | 是否影响查询语义 | 下一轮主要动作 |
| --- | --- | --- | --- |
| `semantic_positive` | 片段确实表现“拥挤城市中的个人孤独” | 是，强化已验证实体、动作、地点和视觉表达 | 生成“more like this”查询，同时保持原主题锚点 |
| `semantic_negative` | 标题写“城市孤独”，画面却是纯口播或游戏录屏 | 是，抑制具体错误内容；按偏离类型分别建模 | 加否定词、改写视觉表达、降低对应来源/标题模式权重 |
| `utility_rejection` | 低清、横竖屏不合、带水印、片段过短、黑帧 | 否 | 调整质量过滤、平台选择、下载格式和切分参数 |
| `rights_or_access_rejection` | 权利不明、禁止再利用、需登录、下载失败 | 否 | 调整来源白名单、授权策略、解析器和重试策略 |
| `redundancy_rejection` | 搬运、镜像、同一画面近重复 | 通常否 | 提高去重、降低重复来源权重，搜索不同视觉家族 |

这是工程推断，但它与负相关反馈研究的发现一致：负例可能以完全不同的方式偏离，合并为单一负模型会丢失信息。

尤其要避免：

```text
低清视频含有“地铁”
  → 被技术过滤拒绝
  → 系统把“地铁”加入排除词
  → 后续错过大量语义正确的高清地铁素材
```

正确做法是：

```text
语义：地铁相关，记作可用的主题证据
技术：分辨率不足，记作质量拒绝
后续：继续搜索“地铁 + 原主题锚点”，并提高最低分辨率或换平台
```

## 推荐反馈载荷

落库工具后续提供接口时，建议至少返回：

```json
{
  "round_id": "round-002",
  "query_plan_id": "qp-seg-003",
  "source_candidate_id": "douyin:734...",
  "source_query_id": "q-isolated-person-02",
  "ingest_status": "accepted",
  "segments": [
    {
      "segment_id": "mat-9842",
      "start_ms": 12300,
      "end_ms": 19700,
      "summary": "夜间地铁站内，一名乘客与快速经过的人群形成对比",
      "visual_keywords": ["夜间", "地铁站", "单人", "人群", "孤立"],
      "entities": ["地铁站"],
      "actions": ["站立", "人群经过"],
      "ocr_terms": [],
      "asr_terms": [],
      "facet_matches": [
        {
          "facet_id": "isolated_person",
          "score": 0.91,
          "evidence": "单个人物静止，周围多人移动"
        }
      ],
      "semantic_relevance": 0.88,
      "technical_quality": {
        "resolution": "1080x1920",
        "motion_quality": 0.82,
        "watermark": false
      },
      "rights_status": "allowed",
      "content_fingerprint": "..."
    }
  ],
  "rejections": []
}
```

拒绝项使用结构化原因：

```json
{
  "ingest_status": "rejected",
  "rejections": [
    {
      "category": "semantic_negative",
      "code": "VISUAL_CONTENT_MISMATCH",
      "summary": "标题提到地铁，实际画面为主持人口播",
      "negative_concepts": ["主持人口播"],
      "do_not_suppress": ["地铁"],
      "confidence": 0.93
    }
  ]
}
```

反馈汇总器不应把所有片段标题、关键词直接拼接成下一轮查询，而应输出：

```json
{
  "covered_facets": ["crowded_city"],
  "missing_facets": ["isolated_person", "contrast"],
  "positive_visual_patterns": ["夜晚地铁站", "快速移动人群"],
  "semantic_negative_patterns": ["主持人口播", "纯文字卡"],
  "technical_failures": {
    "low_resolution": 8,
    "watermark": 3
  },
  "novel_accepted_segments": 4,
  "duplicate_segments": 6,
  "accepted_duration_ms": 28400
}
```

## 下一轮查询应如何生成

推荐把下一轮查询看作“缺口驱动的受控相关性反馈”，而不是简单递归抽词：

```text
next_query =
  原主题锚点
  + 当前缺失 facet 的可见描述
  + 已入库正例中与该 facet 有关的实体/动作/地点/同义表达
  - 明确的语义负例
  + 平台可表达的技术过滤
```

例如：

```text
原主题：繁华环境与个人孤独的反差
已覆盖：城市繁华
缺失：人群中的孤立人物
正例词：夜间、地铁站、通勤人群
语义负例：口播、纯文字

下一轮：
  “夜间 地铁站 通勤人群 独自站立 -口播 -纯文字”
```

每条扩展查询必须：

1. 绑定一个 `target_missing_facet`；
2. 保留至少一个原主题锚点；
3. 记录从哪些入库片段和反馈证据派生；
4. 不重复已访问的规范化查询；
5. 在执行前重新检查是否与原始旁白和主题一致；
6. 把平台不支持的 NOT、时长、画幅和许可条件交给适配器降级处理。

查询表示不应只依赖标题。优先级建议为：

```text
片段视觉/音频内容证据
  > 经验证的片段摘要和实体/动作
  > 原视频描述和标签
  > 原视频标题
```

标题仍可用于发现平台常用叫法，但只能作为弱信号。

## 查询漂移与选择偏差

### 主要漂移路径

1. **头部偏差**：热门结果中的高频实体被误认为主题核心。
2. **可下载性偏差**：系统只从容易下载的平台获得正例，逐步忽略更相关但难访问的平台。
3. **切分器偏差**：切分模型偏爱静态、清晰或有字幕的片段，使下一轮检索越来越像模型易处理内容。
4. **单一视觉家族坍缩**：正反馈不断搜索相似场景，虽然数量增加，但后续剪辑缺少视觉变化。
5. **摘要幻觉或遗漏**：自动描述加入画面不存在的实体，或漏掉快速事件。
6. **错误负反馈**：把低清、权利不明、重复等非语义拒绝当成主题不相关。

### 防护措施

- 永久保留并每轮重用 `narration_text`、`theme_intent` 与 `required_facets`；
- 反馈项进入扩展前必须与原始主题重新计算相关性；
- 正例词只能服务于一个明确的缺失面向；
- 每轮同时保留一部分原始查询和探索查询，不能全量替换成“类似已入库内容”；
- 对每个视觉家族设置上限，优先补足新家族而非增加重复；
- 语义负例按偏离原因分类，技术拒绝绝不进入负语义词表；
- 限制递归深度、查询数、下载数、时间和成本；
- 保存完整 `parent_query_id → candidate_id → segment_id → feedback` 证据链，便于复盘。

WACV 2026 的多轮视觉检索结果尤其提醒：生成式反馈可能在第三轮以后退化，因此不能以“还能生成新关键词”为继续搜索的理由；必须同时观察新增有效片段、覆盖增量和漂移指标。

## 停止条件

### 研究能直接说明什么

技术辅助审阅（TAR）把检索建模为迭代相关性反馈流程，并强调停止点需要在目标召回与成本之间取舍。Yang、Lewis 与 Frieder 提出的停止规则可针对用户指定的召回目标，并显示置信区间方法能减少过早停止。

来源：[Heuristic Stopping Rules for Technology-Assisted Review, DocEng 2021](https://arxiv.org/abs/2106.09871)。

Callaghan 与 Müller-Hansen 使用统计检验，在给定置信度下判断是否已达到召回目标；论文也指出，连续出现不相关结果可以作为“剩余相关项比例较低”的代理，但单纯启发式停止缺少可靠性保证。

来源：[Statistical stopping criteria for automated screening in systematic reviews](https://link.springer.com/article/10.1186/s13643-020-01521-4)。

主动搜索研究通常在有限预算下最大化发现的目标数；多类别主动搜索还显式建模发现多样性和边际收益递减。

来源：

- [Efficient Nonmyopic Active Search, ICML 2017](https://proceedings.mlr.press/v70/jiang17d.html)
- [Nonmyopic Multiclass Active Search with Diminishing Returns for Diverse Discovery](https://arxiv.org/abs/2202.03593)

### 不能直接照搬的地方

公开平台不是固定、完整、可枚举的封闭语料库，系统不知道互联网上全部相关素材的总数，因此通常无法计算真实 recall，也不能严谨声称“已经找全”。TAR 的统计停止规则需要抽样框或可定义的剩余集合；跨平台动态搜索不天然具备这些条件。

因此本产品应停止于“已经满足当前剪辑用途”，而不是宣称“所有相关素材都已找到”。

### 推荐的复合停止规则

工程上建议每轮落库后同时检查四类条件：

```text
success =
  所有 required facet 已由实际入库片段覆盖
  AND 每个 required facet 的独立片段数达到最低值
  AND 片段总可用时长达到配置目标
  AND 视觉家族/来源多样性达到目标

plateau =
  连续 N 轮新增独立有效片段低于阈值
  OR 连续 N 轮 required facet 覆盖没有增加
  OR 每个新增有效片段的成本超过阈值

hard_stop =
  达到最大轮数、查询数、下载量、时间、费用
  OR 平台不可用/权利策略不允许继续
  OR 漂移风险超过阈值

stop =
  success
  OR hard_stop
  OR (plateau AND 已达到最低可用标准)
```

默认值只能作为 MVP 假设，而不是论文结论。可从以下保守值开始验证：

- 最大递归深度 2–3；
- 连续 2 轮未新增缺失面向即进入平台期；
- 每个 required facet 至少 2–3 个内容指纹不同的片段；
- 可用片段总时长达到预计成片占用时长的 2–3 倍；
- 每轮至少保留一个与已入库内容不同的探索性查询；
- 达到硬预算时返回 `stopped_with_gaps`，明确列出缺失面向，不能伪装为“充分”。

由于这些数值没有可直接迁移的研究定值，应使用真实剪辑数据校准：

- 入库片段被下游 AI 剪辑采用的比例；
- 达成“充分”后仍触发人工补搜的比例；
- 每个主题段的视觉重复率；
- 每新增一个最终被采用片段的搜索、下载、解析和存储成本；
- 漂移查询比例；
- 因错误拒绝而漏掉可用片段的比例。

## 推荐状态机

```text
PLANNED
  → SEARCHING
  → BATCH_FOUND
  → DOWNLOADING
  → INGESTING
  → FEEDBACK_READY
       ├─ sufficient → COMPLETED
       ├─ missing facets + budget remains → EXPANDING → SEARCHING
       ├─ plateau + minimum viable → COMPLETED_PLATEAU
       ├─ hard budget + gaps → STOPPED_WITH_GAPS
       └─ recoverable failure → RETRYING
```

状态边界必须是“落库反馈已经返回”，而不是“下载调用已经发出”。否则下一轮会在不知道实际片段质量的情况下继续扩展。

## 对系统边界的建议

```text
SearchOrchestrator
  负责：查询轮次、平台预算、幂等、取消、恢复

MaterialIngestPort
  输入：下载媒体 + QueryPlan/来源/发现链路
  输出：AcceptedSegment[] + Rejection[]

FeedbackAggregator
  负责：区分语义、技术、权利、重复反馈
  输出：FacetCoverage + PositiveEvidence + NegativeEvidence + YieldMetrics

SufficiencyEvaluator
  负责：根据实际入库片段判断满足、缺口、平台期或硬停止

QueryExpander
  输入：原始主题锚点 + 缺失面向 + 已验证正负证据
  输出：下一轮受控 QueryVariant[]
```

`MaterialIngestPort` 必须是应用层端口，具体落库工具后续按接口文档接入；检索编排器不应知道切分器或数据库内部实现。

## 最小验证实验

在全面开发前，建议使用 30–50 个真实旁白主题段比较三种策略，并保持查询和下载预算一致：

1. **标题 PRF**：从首轮高排名标题抽词递归；
2. **正例反馈**：只从实际有效入库片段摘要/关键词扩展；
3. **分流反馈**：正例扩展 + 语义负例抑制 + 技术/权利拒绝单独调整策略。

测量：

- required facet 的实际入库覆盖率；
- 每轮新增独立有效片段数；
- 每轮查询漂移率；
- 下载后拒绝率及拒绝原因；
- 视觉家族多样性；
- 达标轮数与总成本；
- 最终被剪辑 Agent 采用的片段比例；
- 停止后人工认为仍缺素材的主题段比例。

若“分流反馈”没有显著优于标题 PRF 或正例反馈，优先检查反馈标签可靠性、摘要质量和充分性定义，不要先增加递归深度。

## 最终建议

采用用户提出的闭环，但将其正式定义为：

> 每轮多平台搜索得到的候选先下载并调用外部切分落库工具；只有在落库工具返回实际有效片段、内容摘要、片段关键词、主题面向匹配和结构化拒绝原因后，本轮才算完成。系统以实际入库片段作为正相关反馈，以语义不匹配作为分类负反馈，以技术、权利和重复拒绝调整非语义策略；随后围绕尚未覆盖的主题面向生成下一轮查询。达到实际片段充分性、收益平台期或硬预算时停止。

这比标题递归更符合检索理论和视频片段检索的研究方向，也更接近下游剪辑真正关心的“可用片段”。它仍需要真实工作流实验来确定反馈可靠性、充分性阈值和成本边界。
