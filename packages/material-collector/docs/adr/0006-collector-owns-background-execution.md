# Collector 持有后台执行语义

状态：accepted

后台启动、进程身份、日志重定向、单执行器约束和控制记录由 Material Collector 内部模块统一持有，Skill 中的 PowerShell 只保留薄调用入口。该选择用一个可测试的结构化接口同时服务 Windows PowerShell 5.1、PowerShell 7 和其他宿主，避免 Shell 脚本成为第二套运行时实现。
