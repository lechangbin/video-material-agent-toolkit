# 固定上游提交并选择性移植平台代码

状态：superseded-for-public-release

该决策记录的是公开发布前的内部使用前提。MIT 公开版本没有逐文件或逐函数复制两个
参考项目的源码；平台适配器依据公开响应行为独立实现。未来若引入第三方派生代码，
必须先确认许可证兼容性，保留来源、固定提交、版权、许可证和修改记录，并重构到
`SearchProvider`、`SourceResolver`、`MediaFetcher` 接缝之后。Python 3.11 sidecar
只允许用于短期行为验证，正式实现保持 Python 3.14。
