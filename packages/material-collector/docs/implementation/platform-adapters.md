# 第一版国内平台适配器

状态：已实现离线契约；真实平台烟雾测试待执行

## 交付范围

`material_collector.infrastructure.platforms` 提供 Bilibili、抖音和小红书适配器。
每个适配器同时满足已冻结的 `SearchProvider`、`SourceResolver` 和
`MediaFetcher` 接口：

- Bilibili：关键词搜索；BV 来源解析；每个 P/CID 生成独立 `MediaUnit`；
  调用播放信息接口后下载当前可用的 progressive stream；低码率代理请求
  `qn=64`（720p），高质量请求 `qn=120`。
- 抖音：登录态搜索页 API 捕获；作品 ID、稳定 `/video/` URL 和短分享链接解析；
  下载前重新读取作品详情；根据 `video.bit_rate` 的尺寸或 `gear_name` 选择码流。
- 小红书：先访问同源 `/explore` 预热持久登录 profile，再捕获搜索页当前
  `/api/sns/web/v2/search/notes` API；笔记 ID、稳定笔记 URL 和短分享链接解析；
  下载前重新读取详情，并按低码率代理或高质量选择 H.264 流。

`LOW_PROXY` 在三个平台统一定义为“最高且不超过 720p”。横屏和竖屏均按短边判断：
`1280×720` 和 `720×1280` 都属于 720p。抖音和小红书在没有可确认尺寸且不超过
720p 的码流时返回 `media_stream_unavailable`，不会把未知或 1080p 地址当作低码率
代理。`HIGH` 保持选择平台返回的最高可用档位。该映射已经通过三平台离线响应夹具
验证；真实平台仍可能按账号、地区或版权回退到更低清晰度，受控烟雾测试需记录实际
响应档位。

适配器不会把平台响应原文或临时签名媒体 URL放进候选、媒体单元、日志或会话
产物。签名 URL 只存在于一次 `fetch()` 调用的局部变量中。

## 网络边界

`PlatformTransport` 只有三个操作：

1. 使用指定认证配置打开页面并捕获目标 JSON 响应；
2. 跟随短分享 URL，返回最终稳定页面 URL；
3. 将一个临时媒体 URL 流式写入调用方给出的绝对路径。

默认 `PlaywrightPlatformTransport` 从
`%LOCALAPPDATA%\material-collector\auth\<auth-profile>\<platform>` 读取持久浏览器
配置，使用无头 Chromium 搜索和解析。认证前置门禁及跨进程 profile 锁由更外层
应用服务负责；适配器不复制认证编排。

浏览器始终以 `--no-proxy-server` 启动；媒体 HTTP 客户端固定
`trust_env=False`。因此系统代理、`HTTP_PROXY`、`HTTPS_PROXY` 和本机
`127.0.0.1:10808` 均不会被默认采用。第一版不提供启用代理的隐式或自动回退路径。

媒体下载采用逐跳安全检查：

1. 初始地址和每个重定向目标必须是无用户信息的 HTTPS URL；
2. 域名必须属于对应平台的显式媒体/CDN 后缀白名单；
3. 每一跳请求前解析全部 DNS 地址，任一地址为环回、私网、链路本地、保留地址或
   其他非全局地址时拒绝；
4. 校验通过的地址固定到该次 HTTP 连接；TCP 直接连接该地址，而 HTTP `Host` 和
   TLS SNI 仍使用原始 CDN 主机名，消除“校验后再次 DNS 解析”的 rebinding/TOCTOU；
5. HTTP 客户端禁止自动重定向，由传输层手动处理并逐跳重复上述校验，最多五跳。

域名白名单当前覆盖 Bilibili 的 `bilivideo.com`/`hdslb.com`，抖音的
`douyinvod.com`/`douyinstatic.com`/`zjcdn.com`/`bytecdn.cn` 及平台主域，
小红书的 `xhscdn.com` 及平台主域。平台新增 CDN 域时应先加入离线夹具并审查，
不能通过放开任意 HTTPS 域来兼容。

抖音和小红书短分享地址不再交给浏览器自动跳转。适配器只接受精确的初始短链
hostname（抖音 `v.douyin.com`/`iesdouyin.com`，小红书 `xhslink.com`），传输层
使用同一套直连、DNS 固定和手动重定向机制；每一跳必须保持 HTTPS，并留在本平台
短链或稳定页面域内。URL 查询或路径中出现白名单文本不构成合法 hostname，跨平台
跳转和私网解析都会返回 `short_url_invalid`。

下载目标必须是调用方拥有的绝对路径，其父目录必须已经存在，且目标不能已经
存在。适配器不会创建默认下载目录，不会选择素材工作区，也不会覆盖文件。失败
时只清理由本次调用创建的目标文件。

下载完成后在工作线程调用本地 `ffprobe`，将实际容器、时长、宽度和高度写入
`FetchResult`。`LOW_PROXY` 的实际视频短边若超过 720，或 ffprobe 无法确认视频流，
会删除本次目标并分别返回 `media_quality_exceeded` 或
`media_inspection_failed`；不能只相信平台选流参数。`FetchResult.media_unit_id`
使用带平台前缀的 `MediaUnit.stable_id`，可直接关联来源清单和内容寻址资产。

## 结构化失败

平台错误使用 `PlatformAdapterError`，并至少携带：

- `platform`
- `operation`
- `retryable`

登录失效、风控挑战、超时、HTTP 错误、平台拒绝和响应结构变化拥有不同错误码。
已登录用户的合法“零结果”可以返回空 `SearchBatch`；缺少约定字段则返回
`platform_schema_changed`，禁止伪装成零结果。

浏览器捕获把超时拆分为 `platform_navigation_timeout` 和
`platform_response_timeout`。目标接口等待超时时，抖音与小红书还会读取与认证探针
共享的最小渲染状态：可见挑战返回 `challenge_required`，可见登出界面返回
`authentication_lost`；日志不保存页面正文、查询文本、Cookie 或原始响应。
小红书预热页超时仍使用 `platform_navigation_timeout`，并额外携带
`navigation_phase=warmup`，便于区分搜索页本身的导航故障。

## 验证边界

`tests/test_platform_adapters.py` 使用可注入的离线传输覆盖：

- 三个平台搜索字段规范化；
- HTML 标题与时长转换；
- BV/P/CID 媒体单元；
- 抖音和小红书稳定 URL/ID；
- 分享链接解析；
- 临时媒体 URL 不进入稳定模型；
- 调用方目标写入、内容哈希和禁止覆盖；
- 三个平台 720p 低码率代理和最高质量选择；
- 环境代理禁用和 Chrome 直连启动；
- 非平台域名、DNS 解析到内网以及重定向到内网的 SSRF 拒绝；
- 已验证 IP 的实际连接固定以及原始 TLS SNI；
- 短链 hostname 欺骗、私网解析和跨平台重定向拒绝；
- ffprobe 落地清晰度校验、探测失败和稳定媒体单元 ID；
- 登录失效及平台 schema 漂移的结构化错误。
- 小红书持久 profile 预热顺序、v2 搜索端点和预热超时分类。

本次没有登录真实平台、没有下载真实素材，也没有把适配器标记为生产可用。正式
启用前仍需依次完成只读认证、单关键词搜索、单详情以及一个短视频低码率代理的
受控烟雾测试。平台页面请求路径可能变化；发生变化时应更新捕获规则和离线契约，
不能退回抓取任意页面文本或静默返回空结果。

## 第三方实现说明

本实现依据公开平台响应结构和两个固定参考项目的能力边界重新设计，没有逐文件或
逐函数复制 MediaCrawler 或 Douyin_TikTok_Download_API 源码，因此本次没有新增
第三方复制文件或 `THIRD_PARTY_NOTICES.md` 条目。若后续为签名或兼容性选择性移植
代码，必须按 `docs/design/upstream-code-reuse.md` 补充来源、固定提交、原路径、
本地修改和许可证声明。
