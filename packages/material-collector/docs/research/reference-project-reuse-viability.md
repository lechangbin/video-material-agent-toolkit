# 两个参考项目的代码复用可行性

状态：截至 2026-07-29 的定点核查
范围：仅核查官方 GitHub 仓库的源码、README、许可证和依赖声明；未登录任何平台，未下载素材，也未验证真实搜索结果。

## 结论

不能把“个人、非商业、不分发”直接等同于“两个项目都可整库无条件复用”。

- **MediaCrawler：有较高的功能参考和选择性代码复用价值，但不适合原样作为本项目的 Python 库依赖。**它覆盖第一阶段需要的 Bilibili、抖音和小红书关键词搜索、详情、创作者、登录态缓存及媒体抓取。不过其许可证把授权目的进一步限制为“非商业学习/研究”，个人非商业剪辑并不天然满足这一目的限制。若本工具实际用途主要是生产个人剪辑素材，而不是学习或研究，直接复制代码仍存在授权不确定性；长期使用前宜取得作者书面许可。技术上应固定上游提交，选择性移植平台客户端、签名和登录逻辑，再适配成本项目接口。
- **Douyin_TikTok_Download_API：Apache-2.0 下可以选择性复用，但它不是本项目的多平台搜索引擎。**它的核心价值是已知抖音/TikTok/Bilibili URL 或 ID 的解析、详情字段提取和下载逻辑，不提供小红书，也未提供本项目所需的多平台关键词搜索。适合复用少量 URL 解析和下载算法，不建议把整个 FastAPI/PyWebIO 应用嵌入主 CLI。
- **两个项目均不能按官方锁定依赖原样进入本项目的 Windows/Python 3.14 运行时。**MediaCrawler 原锁在 `lxml==6.0.0` 处失败；下载 API 原 requirements 在 `pydantic-core==2.18.1` 处失败。为了维持本项目已确定的 Python 3.14，推荐“选择性移植并升级依赖”，而不是整库安装。Python 3.11 sidecar 只适合作为短期兼容或行为比对方案。

## 核查基线

| 项目 | 官方 `main` HEAD | 最近提交时间 | 许可证 |
|---|---|---:|---|
| MediaCrawler | [`17f66121e0fcc40fc23958b995bec873d422667d`](https://github.com/NanmiCoder/MediaCrawler/commit/17f66121e0fcc40fc23958b995bec873d422667d) | 2026-07-25 | [NON-COMMERCIAL LEARNING LICENSE 1.1](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/LICENSE) |
| Douyin_TikTok_Download_API | [`42784ffc83a72a516bfe952153ad7e2a3998d16c`](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/commit/42784ffc83a72a516bfe952153ad7e2a3998d16c) | 2025-10-12 | [Apache License 2.0](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/LICENSE) |

以上是本次核查的可复现基线，不代表平台接口在以后仍然可用。

## MediaCrawler

### 许可边界

[许可证](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/LICENSE)明确授予使用、复制、修改和合并代码的权利，但同时要求：

- 用途必须是“非商业学习”或“学习和研究”；
- 在软件及副本的合理显著位置保留版权声明和许可证；
- 不得大规模爬取、干扰平台运行、商业使用或对第三方造成不当影响。

因此，不分发和不收费解决了“商业/分发”方面的大部分顾虑，却没有消除“必须用于学习或研究”的目的限制。个人剪辑是否属于获准目的不能仅由技术设计判断。本结论不是法律意见；若要把复制来的代码长期用于实际剪辑生产，应向作者确认，或只参考其公开行为后独立实现。

### 能力匹配

官方 [README 功能表](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/README.md)列出 Bilibili、抖音和小红书的关键词搜索、指定帖子、创作者主页及登录态缓存。代码中三个独立适配器分别位于：

- [Bilibili crawler/client](https://github.com/NanmiCoder/MediaCrawler/tree/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/bilibili)
- [Douyin crawler/client](https://github.com/NanmiCoder/MediaCrawler/tree/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/douyin)
- [Xiaohongshu crawler/client](https://github.com/NanmiCoder/MediaCrawler/tree/17f66121e0fcc40fc23958b995bec873d422667d/media_platform/xhs)

这使它成为第一阶段搜索能力最有价值的上游来源。但真实平台可用性、风控触发率以及搜索结果完整性本次均未登录验证，不能把“源码存在”当成“当前稳定可用”。

### 为什么不宜整库嵌入

- [入口](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/main.py)是独立应用，没有稳定的 `[project.scripts]` 或面向第三方的库 API；创建 crawler 后直接执行完整抓取生命周期。
- 平台 crawler 读取全局 [`config`](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/config/base_config.py)，并直接调用 [`store`](https://github.com/NanmiCoder/MediaCrawler/tree/17f66121e0fcc40fc23958b995bec873d422667d/store) 写数据或媒体，而不是返回本项目需要的规范化候选 DTO。
- 浏览器持久目录、数据目录和若干资源路径以当前工作目录为基准；默认 CDP 模式还会连接/启动真实 Chrome。这与本项目“显式素材工作区、独立会话、认证资料单独管理”的边界不一致。
- 默认输出支持 JSON/JSONL/SQLite 等应用级存储，但没有本项目要求的单个最终 JSON、JSONL 进度、幂等检查点和 `collection-result.json` 契约。
- 上游媒体抓取不提供本项目的 20 分钟门禁、低码率代理、BV/P/CID 计数语义、跨平台作品组、内容寻址资产或恢复幂等。

选择性复用至少需要把“平台请求与登录”从全局配置、浏览器生命周期和 store 回调中拆出来，包装成 `SearchProvider`、`SourceResolver` 和 `MediaFetcher`。

### Windows 与 Python 3.14 验证

官方 [`pyproject.toml`](https://github.com/NanmiCoder/MediaCrawler/blob/17f66121e0fcc40fc23958b995bec873d422667d/pyproject.toml)声明 `requires-python >=3.11`，但本次在 Windows、CPython 3.14.6 上执行官方锁文件的 `uv sync --no-install-project` 时失败：

1. 官方 `uv.lock` 选择 `lxml==6.0.0`；
2. 该环境没有可用的 CPython 3.14 wheel，转为源码构建；
3. Windows 构建缺少 `libxml2` 头文件而失败。

在临时 clone 中仅把 `lxml` 升到 `6.1.1` 后，依赖最终能够安装，但锁定的 `numpy==2.3.1`、`pandas==2.2.3` 等仍触发本地源码构建，耗时约三分钟。这证明“经升级和本机构建可以安装”，不等于官方锁可原样使用或具备稳定的 3.14 CI 可重复性。未进行平台联网调用和登录测试。

## Douyin_TikTok_Download_API

### 许可边界

官方 [Apache-2.0 许可证](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/LICENSE)允许使用、复制、修改和分发。即使当前不分发，复制代码时仍应保留相应版权和许可证说明；以后若分发修改版本，还需要遵守 Apache-2.0 的 NOTICE、变更声明和专利等条款。

### 能力匹配

官方 [README](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/README.md)和 [hybrid crawler](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/crawlers/hybrid/hybrid_crawler.py)面向给定分享文本、URL 或作品 ID 的解析；下载端点覆盖抖音、TikTok 和 Bilibili，并在 Bilibili 情况下调用 FFmpeg 合并音视频流。[下载实现](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/app/api/endpoints/download.py)

它不提供：

- Bilibili、抖音和小红书的统一关键词搜索；
- 小红书解析或下载；
- 本项目的浏览器登录态复用流程；
- 多轮查询、会话恢复、候选清单和规范化素材状态。

因此它只能补充 `SourceResolver`/`MediaFetcher`，不能充当 `SearchProvider`。

### 嵌入、副作用与安全

- 项目是完整的 [FastAPI/PyWebIO 应用](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/app/main.py)，并非可安装的库；根目录没有 `pyproject.toml` 或稳定库入口。
- 多个模块在 import 时按源码相对路径读取 YAML；下载端点按根配置写入默认 `./download`，并直接打印 FFmpeg 诊断。这些行为会绕过本项目的工作区、日志和原子产物边界。
- 官方 README 要求用户把登录 Cookie 写入 crawler 的 YAML。部分 API 还把 Cookie 设计成查询参数，例如 [TikTok endpoint](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/app/api/endpoints/tiktok_web.py)。查询参数容易进入访问日志、浏览器历史或错误记录，不能原样复用。
- 返回结构是项目自有的 FastAPI response model 或临时字典，并非本项目版本化的 `SourceCandidate`/`MediaUnit` 契约。

### Windows 与 Python 3.14 验证

官方 [`requirements.txt`](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/requirements.txt)固定 `pydantic==2.7.0` 和 `pydantic-core==2.18.1`。在 Windows、CPython 3.14.6 上原样安装失败：`pydantic-core` 使用的 PyO3 0.21.1 最高识别 Python 3.12，并且其 schema 生成脚本与 Python 3.14 的 `ForwardRef` API 不兼容。

本次没有继续通过大范围升级依赖来验证，因为升级 Pydantic/FastAPI/PyWebIO 后已不再代表官方代码的原样可用性。Python 3.11 sidecar 的完整安装和最小导入也未完成验证；若以后需要 sidecar，应另开兼容性任务并冻结独立锁文件。

## 推荐复用策略

| 上游 | 整库嵌入 Python 3.14 | 选择性复用 | 独立 Python 3.11 sidecar |
|---|---|---|---|
| MediaCrawler | 不推荐；官方锁原样失败，且全局状态/存储耦合较重 | **推荐，但先确认许可证用途**；优先移植三平台 client、签名、登录检查和字段解析 | 一般不推荐；会令浏览器会话、取消、日志和恢复跨进程复杂化 |
| Douyin_TikTok_Download_API | 不推荐；原 requirements 失败且产品形态是服务 | **推荐少量 Apache-2.0 代码**；限 URL/ID 解析、字段映射、Bilibili 音视频下载合并 | 仅作短期兼容/行为对照；正式采用前必须单独验证 |

落地时建议：

1. 在来源清单中记录上游仓库、固定 commit、复制文件和后续本地修改。
2. 保留每个复制文件适用的版权头；仓库内同时保存对应许可证文本。
3. 不直接合并两套全局配置、store、日志、Web API 或下载目录。
4. 先为本项目定义稳定的 `SearchProvider`、`SourceResolver`、`MediaFetcher` 和 DTO，再把经审计的上游片段放到基础设施适配器后面。
5. 对三个平台各建立不下载素材的契约测试和受控登录烟雾测试；平台可用性通过后，才把该适配器标记为可生产使用。
6. 任何 Cookie 只保存在本地认证工作区，不写入源码/YAML、URL、stdout、stderr 或来源清单。

## 尚未验证

- 三个平台在 2026-07-29 的真实登录、搜索、详情和下载成功率；
- MediaCrawler 对 Bilibili 多 P/CID 的完整处理是否符合本项目计数和资产边界；
- 两个项目升级依赖后的全部测试结果；
- 下载 API 在 Python 3.11 sidecar 中的完整启动与最小调用；
- 各平台服务条款、素材版权与下载/再剪辑行为本身的法律边界。
