# MediaCrawler 一手资料研究

## 研究范围

- 研究对象：[NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)。
- 证据固定在提交 [`17f66121e0fcc40fc23958b995bec873d422667d`](https://github.com/NanmiCoder/MediaCrawler/commit/17f66121e0fcc40fc23958b995bec873d422667d)（提交时间 2026-07-25），避免 `main` 后续变化使结论失效。
- 仅使用该仓库的 README、许可证、源码和仓库内官方文档。下文标为“判断”或“建议”的内容是基于源码证据作出的工程推断，不是项目作者的原话。

## 结论摘要

MediaCrawler 可以作为“平台采集适配器”的设计参考，但不应直接当作“旁白主题段 → 查询句 → 候选素材 → 充分性判断 → 递归扩展”的完整工作流引擎。它的强项是七个平台的登录、关键词分页检索、详情补全、评论采集、平台字段落盘，以及部分平台的媒体文件下载；它没有主题段模型、统一候选素材协议、跨平台聚合返回、语义相关性评分、素材充分性规则、递归查询图或切分落库接口。各平台 `search()` 方法主要以写存储的副作用结束，而不是返回统一的候选集合；抽象存储接口也是 `store_content` / `store_comment` / `store_creator`。[爬虫抽象接口](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/base/base_crawler.py#L26-L40) [存储抽象接口](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/base/base_crawler.py#L86-L100)

对本项目最合适的借鉴方式是：把类似 MediaCrawler 的实现放在 `SearchProvider` / `MediaFetcher` 基础设施适配层；在其上另建独立的主题段查询编排器、统一 `CandidateContent`、充分性评估器、递归预算与发现链路。若计划商业发布或用于商业短视频，不能直接复制或合并其代码，除非取得版权所有者书面许可，因为其许可证仅授权非商业学习用途。[许可证授权与限制](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/LICENSE#L42-L48)

## 支持平台与检索模式

项目列出的七个平台是小红书、抖音、快手、B 站、微博、百度贴吧和知乎；README 声明七者均支持关键词搜索、指定帖子 ID、二级评论和指定创作者主页。[平台能力表](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/README.md#L50-L70) 程序级通用模式为：

- `search`：关键词检索；
- `detail`：指定帖子或内容详情；
- `creator`：指定创作者主页内容。

这些模式直接定义在配置和 CLI 枚举中。[通用模式配置](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/config/base_config.py#L20-L32) [CLI 模式枚举](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/cmd_arg/arg.py#L60-L65)

平台搜索还有以下可借鉴的筛选维度：

| 平台 | 当前源码中的检索行为与筛选 |
| --- | --- |
| 小红书 | 支持综合、最热、最新排序，客户端参数还支持全部、仅视频、仅图文；当前核心搜索显式传排序，内容类型使用客户端默认的“全部”。[枚举](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/xhs/field.py#L55-L72) [客户端参数](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/xhs/client.py#L280-L310) [核心调用](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/xhs/core.py#L129-L179) |
| 抖音 | 客户端定义综合、视频、用户、直播频道，以及综合、最多赞、最新排序和发布时间范围；当前核心搜索使用综合频道/综合排序，并从配置传发布时间范围。[筛选枚举](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/douyin/field.py#L24-L43) [请求参数](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/douyin/client.py#L169-L209) [核心调用](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/douyin/core.py#L127-L181) |
| 快手 | 关键词分页搜索视频，使用平台返回的 `searchSessionId` 延续会话；当前未暴露排序或时间筛选。[搜索循环](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/kuaishou/core.py#L130-L183) |
| B 站 | 支持普通搜索、时间范围内全部搜索、时间范围内逐日限量三种模式；客户端支持综合、最多播放、最新、最多弹幕、最多收藏，并可传发布时间区间。[搜索模式](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/bilibili/core.py#L136-L148) [客户端参数](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/bilibili/client.py#L157-L186) |
| 微博 | 支持综合、实时、热门、视频四种搜索类型。[类型枚举](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/weibo/field.py#L28-L39) [核心选择与分页](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/weibo/core.py#L137-L190) |
| 贴吧 | 客户端枚举按时间升降序或相关性排序，以及只看主题帖/帖子与回复混合；当前核心固定使用时间倒序和混合模式。[筛选枚举](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/tieba/field.py#L24-L38) [核心调用](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/tieba/core.py#L148-L208) |
| 知乎 | 关键词分页搜索内容，内容模型覆盖回答、文章和视频；当前核心未提供额外排序/时间筛选。[搜索循环](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/zhihu/core.py#L150-L199) [内容类型模型](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/model/m_zhihu.py#L25-L42) |

## 输入查询的实际粒度

CLI 的 `--keywords` 是一个字符串，多个查询只用英文逗号分隔。[CLI 参数](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/cmd_arg/arg.py#L193-L200) 各平台核心实现均以 `config.KEYWORDS.split(",")` 得到单个字符串，并把它原样传给平台搜索接口；例如小红书和抖音都没有在本地进行分词、主题理解或查询改写。[小红书循环](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/xhs/core.py#L129-L156) [抖音循环](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/douyin/core.py#L127-L151)

因此：

- **事实**：MediaCrawler 的最小查询单位是平台可接受的原始关键词字符串，不是“旁白主题段”对象，也不是镜头需求对象。
- **判断**：平台技术上可能接受较长句子，但源码没有证明长旁白句能获得稳定结果。对本工作流，更稳妥的是由上游把一个主题段生成多个短而独立的平台检索表达，例如“事件/主体 + 动作/现象 + 地点/年代”等组合，而不是把整段旁白原样提交。
- **建议**：主题段保留为编排层的语义与充分性评估单位；查询句只是该主题段下的一组搜索表达。每个候选项至少记录 `segment_id`、`query_id`、`query_text`、`round`、`parent_candidate_id` 和 `provider`。MediaCrawler 已有的 `source_keyword` 可借鉴为最小溯源字段，但它只是单个上下文字符串，无法表达递归发现链。[关键词上下文变量](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/var.py#L21-L31)

## 采集到的结构化内容与元数据

MediaCrawler 会把平台原始结果转换为平台专属结构，并普遍附带 `source_keyword`。它没有统一的跨平台 `CandidateContent` 返回类型。

| 平台 | 主要内容字段 |
| --- | --- |
| 小红书 | 内容 ID、类型、标题、描述、视频 URL、图片 URL、话题标签、发布时间、点赞/收藏/评论/分享、落地页、来源关键词。[转换代码](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/xhs/__init__.py#L88-L131) |
| 抖音 | 作品 ID/类型、标题/描述、发布时间、互动计数、作品页、封面、视频/音乐/图文下载 URL、来源关键词。[转换代码](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/douyin/__init__.py#L122-L183) |
| 快手 | 视频 ID/类型、标题/描述、发布时间、点赞/播放、内容页、封面、播放 URL、来源关键词。[转换代码](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/kuaishou/__init__.py#L55-L79) |
| B 站 | 视频 ID、标题/描述、发布时间、点赞/播放/收藏/分享/投币/弹幕/评论、内容页、封面、来源关键词。[转换代码](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/bilibili/__init__.py#L55-L82) |
| 微博 | 正文、发布时间、点赞/评论/转发、内容页、来源关键词；创作者 ID 被哈希、昵称被脱敏。[转换代码](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/weibo/__init__.py#L70-L107) |
| 贴吧 | 帖子 ID、标题、摘要、链接、发布时间、贴吧名称/链接、回复数/页数、来源关键词。[模型](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/model/m_baidu_tieba.py#L27-L42) |
| 知乎 | 内容 ID/类型、正文、落地页、问题 ID、标题/摘要、创建/更新时间、赞同/评论、来源关键词。[模型](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/model/m_zhihu.py#L25-L42) |

可落盘为 CSV、JSON、JSONL、Excel、SQLite、MySQL 等；但这属于存储输出，不等同于可供 Agent 直接消费的稳定跨平台响应协议。[README 数据保存](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/README.md#L282-L286) [配置中的存储与数量控制](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/config/base_config.py#L89-L105)

**建议的统一候选层**：从上述字段归一化出 `candidate_id`、`provider`、`platform_content_id`、`canonical_url`、`media_type`、`title`、`description`、`published_at`、`duration`、`width`、`height`、`preview_url`、`download_locator`、`engagement`、`query_trace`、`rights_status`、`raw_metadata`。其中时长、分辨率、权利状态等关键剪辑字段在 MediaCrawler 的统一设计中并不存在，需要本项目自行补足或在下载后探测。

## 下载能力

全局 `ENABLE_GET_MEIDAS` 默认关闭。[媒体开关](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/config/base_config.py#L101-L108) 当前源码发现的实际二进制下载能力如下：

- **小红书**：下载图文图片和视频，并按内容 ID 写入本地目录。[下载流程](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/xhs/core.py#L463-L522) [本地文件布局](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/xhs/xhs_store_media.py#L34-L64)
- **抖音**：自动区分图文与短视频，下载图片或 MP4；元数据还暴露音乐 URL，但下载流程明确暂不单独提取音频。[下载流程](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/douyin/core.py#L402-L470)
- **B 站**：从播放地址列表选最大 `size` 项，下载并保存为 `video.mp4`。[下载流程](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/bilibili/core.py#L573-L608)
- **微博**：下载帖子图片；当前核心没有微博视频下载流程。[图片下载流程](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/weibo/core.py#L272-L301)
- **快手**：结构化记录中有播放 URL，但当前核心未实现与前三者相同的本地媒体下载调用。[快手字段](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/kuaishou/__init__.py#L55-L79)
- **贴吧、知乎**：当前源码未发现媒体二进制下载实现。

这些下载器保存的是完整原始文件字节，没有镜头切分、转码、统一容器、技术质量探测或素材库入库协议。因此可复用的是 `MediaFetcher` 边界和“按平台内容 ID 隔离文件”的思路，切分与落库仍应由后续独立工具承担。

## 对“旁白主题段 → 查询句 → 候选内容”工作流可借鉴的设计

1. **平台适配器工厂**
   主入口以平台代号创建对应 crawler，七个平台实现彼此隔离。[CrawlerFactory](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/main.py#L39-L67) 本项目可沿用这种可替换边界，但应让适配器返回统一候选项，而非直接决定存储格式。

2. **列表检索后再补详情**
   小红书先检索列表，再并发取详情，随后结构化保存、下载媒体、取评论。[小红书搜索管线](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/xhs/core.py#L147-L179) 这适合拆成低成本 `discover` 和高成本 `hydrate/download` 两阶段：充分性判断前只补齐评分所必需的字段，入选后再下载。

3. **来源关键词可追踪**
   各平台存储时保留 `source_keyword`，例如小红书、抖音和 B 站均如此。[小红书](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/xhs/__init__.py#L109-L129) [抖音](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/douyin/__init__.py#L158-L183) [B 站](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/store/bilibili/__init__.py#L55-L82) 本项目应把它扩展为不可丢失的查询/递归发现图。

4. **平台能力驱动的查询计划**
   视频剪辑素材应优先路由到视频过滤能力较强的平台与模式：小红书仅视频、微博视频、B 站视频搜索、抖音视频频道；图像型补充再走图文模式。不要把所有查询以同一参数广播到所有平台。

5. **明确预算而不是无限递归**
   MediaCrawler 已有页数、最大内容数、并发数和请求间隔配置。[预算配置](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/config/base_config.py#L98-L105) 但部分平台在配置值小于单页固定值时会主动把最大数量抬高，例如小红书至少 20 条、抖音至少 10 条。[小红书最小批量](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/xhs/core.py#L129-L141) [抖音最小批量](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/douyin/core.py#L127-L140) 本项目不能直接照搬这种全局计数语义，应按主题段设置 `max_rounds`、`max_queries`、`max_candidates`、`max_download_bytes` 和每平台预算。

6. **充分性与扩词必须置于采集器之外**
   当前各平台搜索循环的停止条件是页数/数量、空结果或异常，不是“对旁白主题是否足够”。[知乎停止条件示例](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/zhihu/core.py#L150-L199) 建议编排层使用主题覆盖、候选数量、可下载率、媒体类型多样性、去重后数量、技术质量和权利状态共同判定；不足时从高相关候选的标题/标签/描述提取新词，并记录父子关系。

## 限制、合规与工程风险

1. **许可证与商业用途冲突是首要阻断项**
   `NON-COMMERCIAL LEARNING LICENSE 1.1` 只授权非商业学习用途，禁止未经书面同意的商业使用，也禁止大规模爬取或干扰平台运营。[LICENSE](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/LICENSE#L12-L22) 若素材库或短视频用于变现，建议只研究其接口思想，重新实现合规的数据源适配器；复制代码前必须取得许可。

2. **平台条款、robots、风控与账号风险**
   项目文件头要求遵守目标平台条款和 robots.txt、控制频率；README 采用 Playwright 登录态和浏览器上下文获取签名参数，并推荐 CDP 复用真实浏览器 Cookie 以降低风控。[源码使用约束](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/config/base_config.py#L10-L18) [技术原理](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/README.md#L54-L58) [CDP 与 Cookie](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/README.md#L142-L154) “降低风控”不等于平台授权；真实账号、Cookie 和浏览器状态应隔离，不能成为服务端共享凭据。

3. **素材版权与可剪辑权利未被建模**
   仓库免责声明明确不得侵犯知识产权或其他合法权益。[免责声明](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/README.md#L399-L415) 从上述各平台内容结构看，没有许可证、授权范围、肖像/音乐权利、署名要求或允许二创状态字段。**判断**：下载成功不能推出可用于发布或商业剪辑；本项目应在下载前后保留来源、作者归属、许可证据和人工复核状态，并允许因权利不明拒绝入库。

4. **非官方接口稳定性有限**
   实现依赖浏览器登录态、内部 Web 接口、签名参数和平台特定安全令牌；例如小红书详情 URL 需要 `xsec_token`，抖音检索需构造内部查询参数。[小红书指定内容配置](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/config/xhs_config.py#L23-L36) [抖音内部搜索请求](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/douyin/client.py#L169-L209) 平台改版、验证码、地区差异或账号状态都可能让适配器失效；需配置熔断、退避、限流和逐平台健康状态。

5. **输出协议不统一，无法直接驱动 Agent**
   各平台独立构造字段，搜索方法返回 `None` 并直接落盘。**判断**：如果直接嵌入，会把编排层耦合到全局配置、上下文变量和存储副作用。应包在端口之后，并在进入充分性评估前做统一校验、规范化、去重和错误分类。

## 面向当前方案的最终判断

“按旁白的主题意义分段，再围绕每个主题段生成查询句，镜头编排留到后续剪辑阶段”是可行的，而且比“每次查询对应一个镜头需求”更符合当前系统边界。MediaCrawler 的证据支持“一个查询表达对应一次平台关键词检索”，但不支持把整段旁白直接作为稳定查询，也不负责把结果解释为镜头。

建议采用两层粒度：

- **工作流与停止判断单位：主题段**。充分性是“这一段旁白可用的候选素材池是否足够”，不是某个镜头是否已确定。
- **平台检索单位：短查询表达**。每个主题段先生成多个不同检索意图的短语；结果不足时，从高相关候选的标题、标签和描述扩展下一轮查询。

这样既保留后续 AI 剪辑对镜头长度、顺序和节奏的自由，也避免单条宽泛查询造成结果同质化。实现时必须把“搜索发现”与“下载/切分/落库”分阶段：先用结构化元数据做主题相关性、覆盖度、媒体可用性、重复度和权利状态评估，达到阈值后才下载入选内容并交给切分落库工具。
