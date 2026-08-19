# 本地媒体探测与同作品指纹

`material_collector.infrastructure.media_inspection` 提供不依赖平台的本地媒体检查，
用于下载后补全结构信息，并在执行平台优先级规则前保守确认两个媒体是否属于同一作品。

## 接口

- `inspect_media(path)`：先通过 `ffprobe` 取得时长、容器、首个视频流的宽高和流数量，
  再以 FFmpeg 严格解码完整主视频及可选主音频；只有完整时间线可解码时才返回结构事实。
- `fingerprint_media(path)`：生成两种独立证据：
  - 最长前 120 秒、单声道 11025 Hz 音频的 Chromaprint 原始哈希序列；
  - 每 2 秒采样一次、最多 60 帧的 9×8 灰度图像 dHash 序列。
- `compare_fingerprints(left, right)`：返回音频相似度、视频相似度、时长差、证据可靠性
  和 `same_work` 结论。

所有 FFmpeg/ffprobe 调用都使用参数数组、禁用 shell、关闭标准输入并设置超时。完整性
解码使用严格错误退出，超时至少覆盖已声明媒体时长及固定收尾余量。外部媒体路径只作为
单独的 `-i` 参数传递，不拼接到命令文本中。指纹生成复用内部元数据探测，不为同一媒体
增加第二次完整解码。

## 保守判定

默认只有以下条件同时成立才返回 `same_work=true`：

1. 两侧都有至少三个可用 Chromaprint 哈希；
2. 两侧都有至少三个视频帧 dHash；
3. 音频相似度不低于 0.88；
4. 视频相似度不低于 0.90；
5. 时长差不超过 2 秒或较长媒体时长的 5%（取两者较大值）。

序列比较允许最多四个采样位置的轻微对齐偏差，但不会仅凭标题、作者、单一模态或近似
时长确认同作品。无音轨时 `audio_status=no_audio`；无视频、媒体过短导致证据不足或时长
未知时，同作品判断均为 `false`。调用方可继续保留两个候选，不应在证据不足时执行平台
优先级去重。

## 运行要求与限制

- 运行环境必须提供带 `chromaprint` muxer 的 FFmpeg 和 ffprobe。
- 指纹适合确认完整作品的跨平台转码副本，不负责识别二创、拼接、裁切或局部片段。
- dHash 对缩放和常规有损转码稳定，但不是密码学标识；它必须与 Chromaprint 和时长共同
  使用。
- 当前实现同步执行，应用服务在批量处理时应放入工作线程，并使用自身的取消和进度接口。
- 可读取 MP4 文件头不代表媒体完整；快速启动 MP4 尾部截断、损坏包或中途中断编码必须在
  资产提交前由完整性解码拒绝。

验证命令：

```powershell
uv run pytest tests/test_media_inspection.py
uv run ruff check src/material_collector/infrastructure/media_inspection.py tests/test_media_inspection.py
uv run mypy src/material_collector/infrastructure/media_inspection.py
```
