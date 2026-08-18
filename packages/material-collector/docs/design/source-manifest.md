# 采集会话来源清单

状态：已确认

## 权威产物

每个采集会话维护一个 `collection-result.json`。它是该会话面向 Agent、人工用户和下游程序的权威来源清单，记录所有递归搜索轮次发现的候选内容，不因候选内容是否完成下载、切分、落库或被自动剪辑采用而省略。会话内部阶段、幂等和恢复状态以同一工作区内的 `session.sqlite3` 为准；该 JSON 是从已提交内部状态原子发布的对外投影。顶层清单契约当前为 `schema_version: "2.0"`；2.0 新增必填的冻结 `platform_scope`，不得把缺少该字段的旧 1.0 清单解释为 2.0。

表格、Markdown 报告和前端列表都只能从该文件生成，不得形成另一份可独立修改的来源状态。

## 更新规则

- 顶层记录会话冻结的 `platform_scope`；即使候选为空，也能审计允许访问的平台。
- 每个候选内容使用“平台 + 稳定内容标识”去重；同一内容被不同检索表达再次发现时，合并发现链路，不复制来源记录。
- 每个候选内容至少记录平台、稳定内容标识、规范页面地址、标题、作者、发现轮次、命中的查询计划和检索表达。
- 跨平台同作品保留各自独立的候选内容记录，并通过作品组标识、主来源标记和回退顺序建立关联。
- 记录低码率代理的本地引用、状态、分辨率和时长。
- 记录高质量媒体是否已下载；已下载时保存本地引用，未下载时标记为 `on_demand`。
- 记录完整高质量下载请求与媒体单元的幂等关系，多个已选片段必须复用同一下载任务和本地文件。
- 低码率代理和高质量媒体都记录内容哈希、文件大小及生命周期状态；跨会话或剪辑项目复用时只增加逻辑引用。
- 每个已下载资产的 `relative_path` 始终指向工作区级内容寻址资产。作品组的主来源可额外
  记录 `display_relative_path`，指向本会话按来源标题、平台、来源标识、质量和媒体单元
  标题组织的标题素材视图；回退来源必须为 `null`。
- 超过 20 分钟且未经人工批准的媒体单元记录为 `manual_review_required`；无法取得可靠时长的媒体单元记录为 `duration_unknown`。两者都保留规范页面地址和已知元数据，但没有本地代理引用。
- 记录人工复核决定、决定时间、操作者以及关联补偿批次；不得通过修改已关闭轮次来隐藏迟到批准。
- 记录下载、切分、落库和自动剪辑采用状态，但这些状态不得决定来源记录是否保留。
- 记录媒体单元是否占用查询计划的 `max_videos` 名额及首次占用轮次；任务恢复不得重复计数。
- 记录媒体资产和完整语义分段目录的首次占用查询计划及全部复用查询计划；复用不得被记成新的 `max_videos` 占用。
- 不保存 Cookie、请求头、临时签名媒体地址或其他登录凭据。
- 每次变更使用临时文件完成序列化和校验，再原子替换正式文件，避免中断后留下半写文件。
- 文件包含显式的格式版本；不兼容变更必须经过迁移，不得静默改变字段含义。

## 跨平台作品组

本地指纹比较完成后，工作流通过
`SourceManifestApplication.replace_work_groups()` 一次提交当前会话的完整作品组快照。
该调用是替换语义而不是增量追加：校验全部成员成功后，在同一 SQLite 事务内替换旧
快照，再原子发布 `collection-result.json`。重复提交同一快照是幂等操作；任一成员
未知、平台不匹配、跨组重复时，旧快照保持不变。

`collection-result.json` 顶层的 `work_groups` 使用以下版本化结构：

```json
{
  "schema_version": "1.0",
  "work_group_id": "wg_example",
  "status": "confirmed_duplicate",
  "primary_media_unit_id": "bilibili:BV1:cid1",
  "members": [
    {
      "schema_version": "1.0",
      "media_unit_id": "bilibili:BV1:cid1",
      "platform": "bilibili",
      "role": "primary",
      "fallback_order": 1
    },
    {
      "schema_version": "1.0",
      "media_unit_id": "douyin:123:123",
      "platform": "douyin",
      "role": "fallback",
      "fallback_order": 2
    }
  ]
}
```

作品组状态含义：

- `confirmed_duplicate`：可靠本地指纹确认多个来源属于同一作品。
- `independent`：指纹证据确认该媒体单元是独立作品。
- `fingerprint_unavailable`：无法取得足够的本地指纹证据；为避免误删，仍按独立来源
  保留并允许后续理解。

每个 `ManifestMediaUnit` 同步投影 `work_group_id`、`source_role`、
`fallback_order` 和 `eligible_for_understanding`。作品组内只有 `primary` 为
`eligible_for_understanding=true`，回退来源为 `false`；尚未形成作品组的旧会话
媒体保持 `true`，确保迁移不会静默阻断原有处理流程。

SQLite 中的 `work_group` 和 `work_group_member` 表由清单存储适配器按需、幂等创建。
旧会话首次导出或写入清单时自动补齐这两张表；迁移不改写既有候选、媒体、资产和
人工复核记录。

## 标题素材视图

标题素材视图位于
`materials/by-session/<session-id>/<source-title>__<platform>__<source-id>/`，质量目录为
`low-proxy` 或 `high-quality`，文件名为
`<media-unit-title>__<media-unit-id>.<container>`。标题会做 Unicode 规范化、Windows
保留名与非法字符处理；标识保留短可读前缀和稳定哈希。标题单组件以约 80 字符为上限，
Windows 上还会按完整目标路径预算缩短并保留哈希，避免依赖系统长路径开关。

视图只在本地指纹形成作品组后发布。每组只有 `primary` 的低码率代理和已取得的高质量
媒体拥有 `display_relative_path`；主来源变化时，原回退资产继续保留，但对外投影清除其
展示路径。发布优先使用硬链接，跨卷或文件系统不支持时原子复制并复核 SHA-256。发布
失败返回可重试的 `named_view_publish_failed`，会话停留在可恢复阶段，已提交的下载不会
重做。标题变化产生新路径，旧入口不自动删除；本版本不迁移旧会话，也不自动清理孤立
标题入口。

## 一致性边界

- 同一采集会话只有一个清单写入者；并发平台任务通过会话服务提交变更。
- `collection-result.json` 负责记录发现来源和处理状态，不替代素材库中的有效片段快照。
- 会话恢复时，以 `session.sqlite3` 及其引用产物的完整性校验恢复内部状态；该文件缺失或过期时，从已提交状态重新发布。素材是否充分仍以素材库返回的有效片段快照为准。
- 同一来源被多个主题段查询计划使用时，合并来源和媒体资产记录，同时分别保存各查询计划的发现链路、分段选择和落库状态。
- 同一已落库物理片段被多个查询计划引用时，记录一份片段身份和多份查询计划关联，不复制片段文件。
