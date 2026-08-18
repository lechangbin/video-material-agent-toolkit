# 本机认证模块

第一版实现在
`src/material_collector/infrastructure/authentication.py`，实现已冻结的
`AuthenticationGateway` 端口。

## 边界

- 默认配置根目录为 `%LOCALAPPDATA%\material-collector\auth\`；测试和嵌入调用可注入
  `root_dir`，不需要修改用户真实登录态。
- 每个平台配置位于 `<root>/<auth-profile>/<browser-channel>/<platform>/`。配置标识经过白名单校验，
  不能产生路径穿越。
- 每次探针、有头登录和登出都持有 `<auth-profile> + <browser-channel> + <platform>` 粒度的跨进程文件
  句柄锁。正式等待上限为 60 秒；进程退出会释放锁，删除锁文件不能抢占仍打开的
  Windows 句柄。异步调用只执行一次非阻塞锁尝试后让出事件循环，因此取消不会等待
  后台线程耗尽 60 秒。
- `probe()` 只返回 `valid`、`invalid`、`challenge_required` 或 `probe_failed`，
  且公开结果只包含平台、配置标识、时间和规范化原因码。
- `ensure_authenticated()` 按 Bilibili、抖音、小红书的支持顺序过滤到调用方传入的冻结 `platform_scope`，只串行处理范围内平台。明确未登录或
  需要挑战时才打开有头浏览器；不可靠探针不会被误判为未登录。
- `logout()` 要求确认值与平台值完全相同，只删除该平台配置。
- 无头探针和有头登录都以 `--no-proxy-server` 启动冻结的 Edge 或 Chrome，明确绕过 Windows
  系统代理和本机代理端口；认证模块不会读取或自动采用代理环境变量。
- 无头探针和有头登录均显式设置 `chromium_sandbox=True`，不再通过
  `--no-sandbox` 降级。登录页导航后调用 `bring_to_front()`；Windows 仍可能阻止
  应用抢占系统前台，因此进度事件同时提示用户检查任务栏。
- Windows 有头登录在进入等待循环前验证调用进程处于活动控制台会话，并确认可见
  Chromium 顶层窗口属于本次 `user-data-dir`、启动时间不早于本次有头启动且进程
  非 headless；其他窗口（包括同 profile 的旧进程）不能使验证通过。
  验证失败归一到 `window_verification` 阶段。
- `ensure_authenticated()` 通过应用层进度端口输出逐平台探针开始/完成、登录窗口
  导航、打开、登录等待和平台完成事件。导航和等待阶段最多每 10 秒输出一次无凭据
  进度；等待事件携带平台、认证配置、截止时间、`actor=human` 与安全提示。
- 抖音和小红书先检查新导航页面的登录、登出和验证状态，再以自身份接口兜底。
  抖音接口的业务状态码 `8` 在页面没有给出明确状态时归一为 `invalid`；小红书
  裸接口的 HTTP 406 不会覆盖页面中已明确显示的登录状态。页面状态未知且该裸接口
  返回 406 时，归一为 `invalid/interactive_login_required`，进入有头人工登录；406
  本身永远不能证明已登录。
- 用户关闭全部登录页面时，驱动立即发出 `authentication_login_window_closed`，
  并让工作流持久化 `auth_required` 检查点；不会继续静默等待到登录超时。

## Playwright 探针

Bilibili 使用其导航身份接口的明确 `isLogin` 字段。抖音在新导航页面中检查
`HasUserLogin`、可见登录面板和验证码中间页；小红书检查可见的“我”个人资料入口、
登录容器和“请通过验证”提示。平台页面没有给出明确状态时才调用自身份接口：
抖音业务状态码 `8` 判为未登录，身份接口返回可识别身份对象时判定有效。
小红书页面未知且裸身份接口返回 HTTP 406 时判定需要交互登录，并继续等待页面出现
可靠的“我”入口；持续未知最终返回带 `interactive_login_required` 原因的
`auth_login_timeout`。其他不能识别的响应保守返回 `probe_failed`。HTTP 401 判定无效，403、412、429 判定
需要人工挑战。接口或页面变化不会被伪装为空结果。

有头登录由调用方传入等待秒数。浏览器通道错误按 `unavailable`、`launch`、
`navigation`、`desktop`、`window_verification` 分阶段；自动选择耗尽时只聚合通道、
阶段、规范化原因和所需动作。登录等待超时继续返回 `auth_login_timeout`，不会触发
跨通道回退。

## 离线测试

`tests/test_authentication.py` 使用注入式 fake driver，不访问任何平台，覆盖四态探针、
固定登录顺序、锁占用与释放、无桌面、登录超时、配置标识安全、单平台登出、
Edge/Chrome 选择和隔离、直连启动参数，以及抖音和小红书页面登录态优先级。
小红书覆盖 rendered unknown + HTTP 406 进入可见登录、登录后正证据收敛，以及持续
不明确时的结构化超时。

2026-07-30 Windows 活动桌面烟雾测试确认：无头探针和有头登录主进程均位于
`SessionId=1`，包含 `--no-proxy-server`，不包含 `--no-sandbox`。
同日真实小红书配置探测确认：页面已显示“我”而裸自身份接口返回 HTTP 406 时，
探针仍正确返回 `valid/platform_reports_logged_in`。
