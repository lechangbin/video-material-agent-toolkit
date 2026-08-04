# 第一阶段技术栈与交付范围

状态：部分确认；Python、CLI 范围、包管理和可恢复同步运行已确认，具体命令与输出协议待确认

## 第一阶段范围

- 只交付面向 Agent 和脚本调用的 CLI。
- 不实现 Web API、浏览器前端或桌面 GUI。
- 核心领域与应用服务仍保持独立，避免 CLI 参数解析、终端输出或具体框架进入业务逻辑。
- 后续增加人类界面时复用同一应用服务，但第一阶段不为此引入 FastAPI 或前端依赖。
- CLI 默认同步运行到终态，同时使用持久采集会话支持状态查询、取消和断点恢复。

## Python

- 使用 CPython 3.14，项目运行约束设为 `>=3.14,<3.15`。
- 应用服务、平台适配器、下载任务和状态机采用异步 Python。
- 平台浏览器自动化使用 Playwright Python；登录时有头、正常搜索时无头，并复用本机 Chrome 登录态。
- FFmpeg 和 FFprobe 作为外部媒体工具，通过基础设施适配器调用，不让核心模块依赖子进程细节。

## 包管理

- 使用 uv 管理 Python 3.14 虚拟环境、依赖和锁文件。
- `pyproject.toml` 是依赖与工具配置的权威来源，`uv.lock` 提交到版本库。
- 仓库虚拟环境固定为 `.venv`，不得把全局 site-packages 当作项目依赖来源。

## 本机验证

2026-07-28 在 Windows 环境使用 Python 3.14.6 完成：

- 安装并导入 Playwright、Pydantic、Typer、HTTPX、pytest、pytest-asyncio、SQLAlchemy、aiosqlite 和 yt-dlp。
- 使用 Playwright 1.61 启动本机 Google Chrome 无头实例并完成页面冒烟测试。
- 检测到 FFmpeg/FFprobe 8.1.2。

这些结果证明第一阶段候选依赖可以在当前 Python 3.14 环境运行，但不等于所有候选包都会成为正式依赖；正式依赖应随具体接口逐项引入。
