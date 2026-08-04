# 旁白主题分段驱动短视频素材检索：可行性与推荐方案

## 结论

可行，而且对当前产品边界而言，比“上游直接规定每个镜头”更合适。

但需要区分两个层级：

- **主题段是采集任务和充分性判断的边界**：一个主题段说明这段旁白要表达什么，系统围绕它收集一组可供后续剪辑选择的素材。
- **具体视觉概念是检索执行的粒度**：每个主题段不应只生成一条宽泛查询，而应生成一组彼此互补的查询，包括主题查询、实体/动作/场景查询和必要的隐喻性视觉查询。

因此，推荐“推迟镜头编排”，但不推荐“推迟所有视觉拆解”。上游不决定镜头顺序、切点、景别和最终采用哪条素材；检索层仍需把主题段拆成若干可见、可搜、可验证的视觉面向，否则宽泛主题词容易召回大量语义相关但不可剪的内容。

没有找到直接比较“按旁白主题段采集后再编镜头”和“先编镜头再采集”两条完整生产流水线的原始研究。上述结论是基于 B-roll 编辑研究、视频—文本检索研究、视频时刻检索研究和官方素材搜索接口能力所作的工程综合判断，不能冒充已有论文的直接实验结论。

## 为什么这个边界成立

### 1. 旁白文本确实可以驱动 B-roll 内容发现

CHI 2019 的 B-Script 研究直接研究了以口播/旁白文本辅助 B-roll 编辑的场景。作者分析了热门 vlog，并让 115 名有经验的编辑者为口播视频插入 B-roll。研究观察到：

- B-roll 内容与旁白中的概念存在联系；
- 编辑者选择 B-roll 的位置与旁白关键词密切相关；
- 系统可以从 transcript 生成 B-roll 查询词和推荐；
- 实际编辑仍由用户在候选素材中选择、移动和调整时长。

这支持“先依据旁白意义发现候选素材，后续再决定具体镜头”的基本分工。论文也显示编辑者之间并不完全一致：不同专家编辑结果的平均插入位置 Jaccard 系数为 15.2%，说明即使存在较好的插入区域，最终镜头安排仍有明显多样性；过早固定唯一镜头会损失这种编辑空间。

来源：[B-Script: Transcript-based B-roll Video Editing with Recommendations](https://arxiv.org/abs/1902.11216)，尤其见论文第 3–5 节及页面文本第 59–115 行。

### 2. “整段主题”与“可定位视觉事件”不是同一种检索目标

自然视频通常同时包含多个事件。ActivityNet Captions 的原始论文把每个事件分别用自然语言描述，并为每个描述标注独立起止时间；这说明一条视频的整体主题不能可靠代表其中每个可剪片段。

来源：[Dense-Captioning Events in Videos](https://arxiv.org/abs/1705.00754)。

视频语料库时刻检索（VCMR）的研究进一步把问题明确拆成：

1. 从大规模语料中找出相关视频；
2. 在相关视频中定位与文本查询对应的短时刻。

Escorcia 等人的 STAL 将查询与组成候选时刻的一系列短片段和区域对齐；相较只使用整个时刻聚合特征的方法，细粒度对齐带来更好的检索表现。ReLoCLNet 同时使用视频级目标寻找候选视频、使用帧级目标突出与查询对应的局部时刻。这些工作支持“两阶段或多粒度”而不是“一种查询粒度包办全部”。

来源：

- [Temporal Localization of Moments in Video Collections with Natural Language](https://arxiv.org/abs/1907.12763)
- [Video Corpus Moment Retrieval with Contrastive Learning](https://arxiv.org/abs/2105.06247)
- [Tencent Text-Video Retrieval: Hierarchical Cross-Modal Interactions with Multi-Level Representations](https://arxiv.org/abs/2204.03382)

### 3. 后置镜头编排不等于下载完整视频后盲选

视频文本检索需要从丰富且冗余的视频帧中保留代表性内容。Wu 等人的实证研究发现，适当的帧选择可以显著提升检索效率而不牺牲检索性能。对本项目的含义是：可以把最终镜头编排后置，但候选视频进入素材库前仍应进行片段化、视觉索引、去重和查询相关性评估，不能只依赖标题。

来源：[An Empirical Study of Frame Selection for Text-to-Video Retrieval](https://aclanthology.org/2023.findings-emnlp.455/)。

## 查询粒度的召回—精度取舍

| 查询方式 | 优势 | 主要风险 | 推荐用途 |
| --- | --- | --- | --- |
| 宽泛主题查询，如“城市孤独感” | 召回范围大，保留解释与创作空间，适合发现未知表达方式 | 返回结果可能只在标题/话题上相关，视觉上不可直接使用 | 第一轮发现、跨平台召回 |
| 实体/动作/场景查询，如“夜晚独自乘地铁的人” | 更容易得到可见且可剪的内容，便于视频级和片段级重排 | 限定过多会降低召回；不同平台标题词汇不一致会漏检 | 主题段下的主要查询分支 |
| 镜头级查询，如“侧面中景、人物走入雨夜街道、慢推” | 对预定镜头精度高 | 多数平台搜索主要依赖关键词或元数据，未必能理解景别、运镜；也会过早锁死剪辑方案 | 仅在后续剪辑明确缺口时补搜，或用于视觉模型重排 |
| 单个标题扩展词 | 成本低，能发现平台内常用别称、事件名和实体名 | 头部结果若不相关会产生查询漂移，热门噪声和营销词会递归放大 | 受控的补充扩展，不能替代原始主题锚点 |

Pexels 官方视频搜索接口明确允许查询既宽泛（例如 `nature`）也更具体（例如“一群正在工作的人”），并另外提供画幅、最小尺寸和 locale 过滤。这证明“语义查询”和“媒体硬约束”应该分开表达，而不是把所有条件塞进一条自然语言查询。

来源：[Pexels API Documentation — Search for Videos](https://www.pexels.com/api/documentation/#videos-search)。

YouTube 官方 `search.list` 也把查询词 `q` 与发布时间、语言、时长、清晰度、是否可嵌入、许可类型等过滤项分开，并支持 OR 与 NOT。不同平台支持的过滤能力不同，所以统一查询计划需要由各平台适配器降级映射。

来源：[YouTube Data API — Search: list](https://developers.google.com/youtube/v3/docs/search/list)。

Pixabay 的官方视频接口同样把 `q` 与类别、最小宽高、视频类型、安全搜索、排序、分页分开，且一条查询最多返回的可访问结果有上限。这进一步说明查询计划应保留“语义”“硬约束”“分页/预算”三个独立部分。

来源：[Pixabay API Documentation — Search Videos](https://pixabay.com/api/docs/)。

## 推荐的查询计划

不要让上游只输出一个字符串。让它为每个旁白主题段输出一个可序列化的 `QueryPlan`：

```json
{
  "segment_id": "seg-003",
  "narration_text": "城市越热闹，有些人反而越感到孤独。",
  "theme_intent": "繁华环境与个人孤独的反差",
  "required_facets": [
    {
      "id": "crowded_city",
      "description": "拥挤、繁忙、明亮的城市公共空间",
      "priority": "required"
    },
    {
      "id": "isolated_person",
      "description": "在人群或城市环境中显得孤立的单个人物",
      "priority": "required"
    },
    {
      "id": "contrast",
      "description": "热闹与孤独能形成视觉反差",
      "priority": "preferred"
    }
  ],
  "query_variants": [
    {
      "kind": "broad_topic",
      "text": "城市 孤独 人群",
      "covers": ["crowded_city", "isolated_person"]
    },
    {
      "kind": "entity_action_scene",
      "text": "夜晚 地铁 人群 独自站立",
      "covers": ["isolated_person", "contrast"]
    },
    {
      "kind": "entity_action_scene",
      "text": "繁忙街头 单人 等红灯",
      "covers": ["crowded_city", "isolated_person", "contrast"]
    },
    {
      "kind": "alternative_visualization",
      "text": "公寓窗边 独处 城市灯光",
      "covers": ["isolated_person"]
    }
  ],
  "constraints": {
    "orientation": "portrait_preferred",
    "min_height": 1080,
    "safe_search": true,
    "rights_policy": "allowed_for_downstream_editing"
  },
  "exclude_terms": ["解说", "教程", "纯文字"],
  "budget": {
    "max_depth": 2,
    "max_queries": 12,
    "max_candidates": 120
  }
}
```

### 字段设计原则

- `narration_text` 永久保留，不能在递归扩展时被标题词覆盖。
- `theme_intent` 是语义锚点，用于防止扩展漂移和进行整体相关性判断。
- `required_facets` 描述“需要哪些类型的可视证据”，不是镜头表。
- `query_variants` 至少包括一条宽泛主题查询和多条实体—动作—场景查询。
- 景别、运镜和具体切点默认不进入第一轮硬约束；只有叙事确实依赖它们时才加入。
- 画幅、清晰度、语言、安全、时间范围和许可等放在 `constraints`，由平台适配器映射。
- 每条查询记录 `covers`，使充分性评估能够判断缺了什么，而不只是数结果。

## 推荐的检索与选择流程

```text
旁白主题段
  → 生成 QueryPlan（主题锚点 + 视觉面向 + 查询变体）
  → 多平台宽召回
  → 统一候选结构、规范化 URL 与内容指纹
  → 元数据初筛（标题、描述、标签、语言、许可、质量）
  → 预览帧/低码率内容的视觉—文本重排
  → 候选视频内的时刻定位或片段切分
  → 按主题面向计算覆盖、替代性、质量和多样性
  → 若不足，只针对未覆盖面向做受控查询扩展
  → 达标或达到预算后停止
  → 下载合格候选并调用后续切分落库接口
```

该流程把“发现源视频”和“找到可剪片段”分开，也把“收集可选素材”和“决定成片镜头顺序”分开。

## 如何保留后续镜头选择空间

1. **保留候选集，不提前选唯一素材。** 每个 required facet 保存多个不同内容指纹的候选。
2. **保留不同视觉表达。** 同一主题可同时保留直接表达、环境建立、人物动作、物件细节和隐喻表达。
3. **保存发现链路。** 每条素材记录 `segment_id`、`facet_id`、原始查询、扩展父查询、平台、原 URL、发布时间、作者、许可/授权状态和相关性理由。
4. **保存片段级数据。** 除源视频外，保存候选片段起止时间、关键帧、视觉描述、OCR/ASR、质量指标和内容指纹，使下游剪辑可以按节奏和旁白时间重新选择。
5. **避免用景别和运镜做首轮强过滤。** 可把这些属性记录为偏好或后验标签，供剪辑阶段排序。
6. **去重但不抹平变体。** 同一视频的搬运/转码版本应合并；真正不同的场景、构图和动作应保留。

## “素材足够”的推荐判定

“足够”不能只等于候选数量达到 N。建议先做硬门槛，再做覆盖判断。

### 硬门槛

候选素材只有同时满足以下条件才计入：

- 可访问、可预览，且在落库时仍可下载；
- 文件或流信息完整，能够安全切分；
- 达到最低分辨率、画幅和时长要求；
- 未与已有候选重复或近重复；
- 许可、授权或来源政策满足后续剪辑用途；
- 与主题段的语义相关性超过配置阈值；
- 至少存在一个与某个 `facet_id` 对应的可定位片段，而不只是标题相关。

许可必须作为硬门槛而非排序加分项。YouTube 官方 API 可按 `creativeCommon` 或标准 YouTube 许可筛选，Pexels 和 Pixabay 也各自有官方内容许可；“能够下载”不等于“能够合法再剪辑”。

来源：

- [YouTube Data API — `videoLicense`](https://developers.google.com/youtube/v3/docs/search/list#videoLicense)
- [Pexels API introduction and license reference](https://www.pexels.com/api/documentation/)
- [Pixabay API documentation and Content License reference](https://pixabay.com/api/docs/)

### 覆盖与停止条件

推荐首版采用可配置、可通过实际剪辑反馈校准的判定：

```text
sufficient =
  所有 required facet 均已覆盖
  AND 每个 required facet 至少有 min_alternatives 个独立候选
  AND 可用候选的视觉类型达到 min_visual_families
  AND 权利与质量硬门槛全部通过
  AND （已满足最低可用量 OR 连续搜索边际收益趋近于零）
```

可作为 MVP 起点而非学术定值的默认参数：

- 每个 required facet 至少 3 个独立候选；
- 每个主题段至少 2 种视觉表达；
- 去重后的可用片段总时长达到该段预计成片占用时长的 2–3 倍；
- 连续 2 个查询分支都没有新增合格候选时判定收益平台期；
- 最大递归深度 2，避免标题词链式漂移；
- 同时设置查询数、候选数、时间和网络成本上限。

这些数值没有可直接迁移到本产品的论文定值，应在真实编辑任务中以“最终被剪辑 Agent 采用的比例”“因缺素材触发补搜的比例”和“被人工判为无关的比例”校准。B-Script 的实验只说明不同编辑者会产生不同选择，不能据此推出本项目固定需要多少候选。

## 标题关键词递归扩展：可用，但必须受控

把首轮结果标题当作伪相关反馈（pseudo-relevance feedback）是合理的探索方式，但“从标题取新词后直接递归搜索”风险很高。检索研究表明，扩展可改善词汇不匹配；同时，扩展文本与原查询之间的平衡、上下文以及整合方式会显著影响效果。

来源：[Exploring the Best Practices of Query Expansion with Large Language Models](https://aclanthology.org/2024.findings-emnlp.103/)。

推荐协议：

1. 只从已通过最低相关性门槛的前若干候选中提取词，不把所有头部结果都当作相关。
2. 优先提取命名实体、具体物体、动作短语、地点、事件别名和平台常用同义词；丢弃“热门”“推荐”“完整版”等泛化词。
3. 新查询必须保留原 `theme_intent` 的一个锚点，或明确绑定一个尚未覆盖的 `facet_id`。
4. 扩展词不能替换原查询，只能组成 `原锚点 + 新词` 的新分支。
5. 对新分支记录 `parent_query_id`、`expansion_terms`、`target_facet` 和 `depth`。
6. 查询文本规范化后做 visited-set 去重；候选按规范化 URL、平台 ID 和感知哈希去重。
7. 若新结果与原主题的相似度下降、只增加重复项，或连续分支没有新增合格候选，立即剪枝。
8. 达到 `max_depth`、查询预算、时间预算或充分性条件时停止。

换句话说，下一轮应由“缺失面向”驱动，标题词只提供候选词汇；不能由标题词自行决定搜索方向。

## 两个参考项目应放在哪一层

### MediaCrawler

MediaCrawler 已经展示了按平台拆分搜索客户端的实现方式。其配置把多个关键词作为逗号分隔项，并在抖音、小红书、Bilibili 等平台的 crawler 中逐个关键词分页搜索。这适合参考：

- 平台适配器边界；
- 登录、Cookie、限流和分页；
- 关键词批量搜索；
- 详情与媒体抓取。

但其现有循环主要按关键词和数量执行，不包含本项目需要的主题面向、视觉重排、充分性判断、查询扩展树和递归停止策略，因此不应把 crawler 本身当作 Agent 编排核心。

源码：

- [关键词配置](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/config/base_config.py#L27-L44)
- [抖音关键词搜索循环](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/douyin/core.py#L127-L179)
- [小红书关键词搜索循环](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/xhs/core.py#L129-L190)
- [Bilibili 关键词搜索循环](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/bilibili/core.py#L183-L238)

### Douyin_TikTok_Download_API

该项目的混合解析和下载端点以视频 URL/分享文本为输入，更适合作为“发现之后”的解析与下载基础设施：

- 将具体平台 URL 解析为统一媒体信息；
- 获取视频/音频流；
- 下载、合并和返回文件；
- 处理部分平台的 Header、Cookie 和格式差异。

它不应承担从旁白生成查询、跨平台检索、充分性判断或递归扩展。

源码：

- [项目能力说明](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/README.md#L10-L61)
- [按 URL 解析单一视频](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/app/api/endpoints/hybrid_parsing.py#L15-L43)
- [按 URL 下载视频/图片](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/app/api/endpoints/download.py#L111-L155)

## 建议的系统边界

```text
NarrativeSegmentPlanner
  输入：主题分段 + 对应旁白
  输出：QueryPlan

SearchOrchestrator
  输入：QueryPlan
  调用：多个 SearchProvider
  输出：统一 CandidateAsset[]

CandidateEvaluator
  输入：CandidateAsset[] + QueryPlan
  输出：带 facet、相关性、质量、权利和去重信息的 EvaluatedCandidate[]

SufficiencyEvaluator
  输入：EvaluatedCandidate[] + QueryPlan + SearchBudget
  输出：sufficient | missing_facets | stop_reason

QueryExpander
  输入：原始 QueryPlan + 高置信候选标题/描述 + missing_facets
  输出：受控的新 QueryVariant[]

MediaResolver / Downloader
  输入：选中的平台 ID 或 URL
  输出：可验证的本地媒体与来源元数据

MaterialIngestPort
  输入：片段、元数据、发现链路
  输出：外部切分落库工具的结果
```

最终剪辑 Agent 消费的是一个按 `segment_id` 和 `facet_id` 组织、包含多种候选片段的素材包，而不是检索 Agent 预先排好的镜头序列。

## 实施前应做的最小验证

因为现有研究不能直接给出本产品的最佳查询粒度，建议用自己的旁白和剪辑偏好做小规模离线实验：

1. 选取 30–50 个真实主题段，由人工标注每段的 required facets。
2. 比较三组：
   - A：每段只用一条宽泛主题查询；
   - B：只用细粒度实体/动作查询；
   - C：主题查询 + 多个视觉面向查询的混合方案。
3. 在同等查询预算下评估：
   - 至少找到一个最终可用片段的段落比例；
   - required facet 覆盖率；
   - 合格候选的 Precision@K；
   - 去重后视觉表达数量；
   - 下载后才发现不可用的比例；
   - 剪辑 Agent 的采用率；
   - 每个被采用片段的搜索、预览和下载成本。
4. 以真实“被采用”和“缺素材补搜”记录校准充分性阈值，而不是长期依赖固定候选数量。

## 最终建议

采用用户设想的主题分段工作流，但把表述精确为：

> 上游按旁白的主题意义生成“素材需求段”；每个需求段生成一个多查询 `QueryPlan`。检索系统负责从主题到若干可见的实体、动作、场景和替代表达做宽召回与细筛，并返回带覆盖关系的候选素材包。镜头顺序、切点、节奏和最终素材选择由后续剪辑阶段决定。

这既保留了创作弹性，也避免了单一抽象主题查询精度不足的问题。
