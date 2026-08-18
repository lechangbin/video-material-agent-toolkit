# Collector 使用本机浏览器通道

状态：accepted

Material Collector 继续通过自身的本机浏览器通道完成认证和平台访问，不依赖 Agent 浏览器连接插件。原生 Windows 默认使用 `auto`，在新会话通道选择阶段按 Microsoft Edge、Google Chrome 的顺序尝试；显式选择 `edge` 或 `chrome` 时不跨通道回退，全部候选失败时返回逐通道的非敏感错误报告。选定通道后由会话冻结，恢复执行不能跨通道回退；Docker 部署显式配置容器内已有的 Chrome。人工登录始终显示浏览器，平台检索默认在后台运行并允许用户显式要求显示；用户关闭显式显示的检索窗口时返回结构化中断，不会静默转入后台。浏览器 Profile 不会被复制或跨通道混用。
