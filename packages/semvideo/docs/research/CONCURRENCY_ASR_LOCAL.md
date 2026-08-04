# 本机 ASR 并发上限验证

日期：2026-07-28

## 结论

当前机器的 ASR 只能使用 CPU。针对当前已缓存并在原型中使用过的
`Systran/faster-whisper-base`、`int8` 和每 Worker 4 个 CPU 线程：

| 口径 | 结论 |
|---|---|
| V1 安全默认值 | `asr = 1` |
| 当前配置的建议操作上限 | `asr = 2` |
| 当前配置的本机已验证运行上限 | `asr = 4` |
| 未验证范围 | 大于 4；`small`、`medium`、`large` 等更大模型；GPU ASR；长视频持续压测 |

这里的“已验证运行上限 4”表示 4 个独立 Worker 能在有限测试中同时完成，
不是 faster-whisper 的绝对上限，也不表示 4 比 2 更快。30 秒样例中，2 并发
已经达到吞吐甜点；4 并发的总吞吐没有继续增长，单任务平均时间约增加一倍。

因此正式配置建议是：

```yaml
concurrency:
  asr: 1

asr:
  device: cpu
  compute_type: int8
  cpu_threads_per_worker: 4
  recommended_concurrency_ceiling: 2
  validated_concurrency_ceiling: 4
```

`validated_concurrency_ceiling` 应被视为当前机器和当前 ASR Profile 的能力记录，
不能直接复制到其他机器或模型。正式代码若允许配置超过 4，应要求显式覆盖告警，
而不是把它当作已支持组合。

## 本机能力

只读诊断结果：

- CPU：AMD Ryzen 9 7940H，8 核、16 逻辑处理器。
- 内存：约 16 GB。
- GPU：AMD Radeon 780M；`nvidia-smi` 不存在，
  `ctranslate2.get_cuda_device_count()` 返回 `0`。
- Python：3.14.6 x64。
- faster-whisper：1.2.1。
- CTranslate2：4.8.1。
- PyAV：18.0.0。
- CTranslate2 CPU 计算类型：`int8`、`int8_float32`、`float32`。
- 本地已有 `tiny` 与 `base` 模型缓存；本次没有下载模型。

faster-whisper 官方说明 GPU 执行依赖 NVIDIA CUDA 12 的 cuBLAS 和 cuDNN 9；
本机既没有 NVIDIA GPU，也没有被 CTranslate2 识别出的 CUDA 设备，因此 Radeon
780M 不能作为当前 faster-whisper Adapter 的 GPU 并发资源
([faster-whisper v1.2.1 README](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/README.md#gpu))。

## 官方并发语义

CTranslate2 把 CPU 并行分成两层：

- `intra_threads`：单个 Worker 内模型运算使用的线程数，官方默认值为 4。
- `inter_threads`：同一模型实例可并行执行的 Worker 数。

同一设备上的多个 CTranslate2 Worker 可以共享模型权重，但必须并发提交多个批次
才能发生数据并行；Python 计算方法会释放 GIL
([CTranslate2 4.8 并行文档](https://opennmt.net/CTranslate2/parallel.html))。
Whisper API 也明确把 `inter_threads` 定义为并行批次 Worker 数，把
`intra_threads` 定义为每 Worker 的 OpenMP 线程数
([CTranslate2 4.8 Whisper API](https://opennmt.net/CTranslate2/python/ctranslate2.models.Whisper.html))。

faster-whisper 1.2.1 的实现直接进行如下映射：

```text
cpu_threads -> intra_threads
num_workers -> inter_threads
```

它同时说明，`num_workers` 只有在多个 Python 线程并发调用 `transcribe()` 时才产生
真正的并行，并以更多内存换取总吞吐
([faster-whisper 1.2.1 `WhisperModel`](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/transcribe.py))。

这对当前架构有一个重要结论：V1 是“每任务一个独立进程”，不是一个共享 ASR
服务。因此每个进程应保持 `num_workers = 1`，由项目的 ASR 文件锁槽位控制跨任务
并发。否则把“多个任务进程”和“单进程多个模型 Worker”叠加，会产生
`进程数 × num_workers × cpu_threads` 的隐含并行度。

独立进程之间也不能利用 CTranslate2 所说的同实例权重共享；每个任务进程都会加载
自己的模型。因此本项目测量的是独立进程并发，而不是单实例 `num_workers` 性能。

## 有限实测

### 方法

- 输入：项目样例视频中截取的 30 秒、16 kHz、单声道 PCM 音频。
- 模型：本地缓存 `Systran/faster-whisper-base`，模型目录约 141 MiB。
- 模式：CPU `int8`。
- 每进程：`cpu_threads=4`、`num_workers=1`。
- 解码：中文、`beam_size=1`、关闭 VAD、固定温度，确保测试短且有界。
- 进程：Windows `CREATE_NO_WINDOW`，分别启动 1、2、4 个完全独立的 Python
  Worker。
- 网络与费用：未联网下载模型，未调用任何云端模型。

代表性缓存热态结果：

| 并发进程 | 总音频时长 | 墙钟时间 | 聚合处理倍速 | 各进程峰值工作集之和 | 子进程平均完成时间 |
|---:|---:|---:|---:|---:|---:|
| 1 | 30 秒 | 2.128 秒 | 14.10× | 0.314 GiB | 1.759 秒 |
| 2 | 60 秒 | 2.952 秒 | 20.32× | 0.628 GiB | 2.597 秒 |
| 4 | 120 秒 | 5.942 秒 | 20.20× | 1.255 GiB | 5.503 秒 |

三个档位均正常退出并生成 5 个转写段。2 到 4 的聚合吞吐从 20.32× 变为
20.20×，没有收益；平均单任务时间从 2.597 秒增加到 5.503 秒。因此：

- 4 是当前轻量 Profile 的“功能已验证上限”。
- 2 是当前轻量 Profile 的“性能建议上限”。
- 1 仍是 V1 默认值，可减少 ASR 与媒体分析、渲染争抢同一 CPU 的风险。

重复的有限运行出现过明显冷启动和共享机器负载波动，所以这些秒数不能作为正式
性能承诺。它们足以证明 4 个 Worker 可运行，并识别 2 到 4 已无吞吐提升。正式
冻结参数前仍需在机器空闲时用多种真实视频重复基准。

## 理论与资源上限

在固定 `cpu_threads_per_worker = 4` 时，16 个逻辑处理器可容纳的整数组合是：

```text
floor(16 / 4) = 4 个 ASR Worker
```

这是避免明显线程超额订阅的资源上界，不是吞吐最优值。CTranslate2 官方文档也以
“4 Worker、每 Worker 1 线程”说明两层并行应共同预算
([CTranslate2 4.8 并行文档](https://opennmt.net/CTranslate2/parallel.html))。

内存在 `base/int8` 短样例中不是先到达的瓶颈：4 个独立进程峰值工作集之和约
1.255 GiB。但该数值不能外推到更大模型、批处理、长音频或词级时间戳。官方 CPU
基准也显示模型、精度和 batch size 会显著改变 RAM 使用，例如 `small/int8`
从非批量的 1477 MB 增加到 `batch_size=8` 的 3608 MB
([faster-whisper v1.2.1 CPU benchmark](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/README.md#small-model-on-cpu))。

因此模型 Profile 变更时必须重新认证，并至少记录：

1. 模型与计算精度。
2. `cpu_threads`、`num_workers` 和 batch size。
3. 单 Worker 峰值工作集。
4. 1/2/4 并发的吞吐与 P95 单任务延迟。
5. 与 FFmpeg 媒体、渲染阶段同时运行时的系统负载。

## 对正式实现的要求

- V1 默认锁槽数保持 `asr = 1`。
- 当前机器允许用户配置到 2，作为推荐操作上限。
- 允许配置到 4，但在 `doctor --json` 中标记为“已验证运行、非吞吐推荐”。
- 每任务 Worker 内固定 `num_workers = 1`，避免隐藏的二次并发。
- 显式配置 `cpu_threads_per_worker = 4`，不要依赖环境变量或库默认值。
- ASR Profile 记录模型、精度和本机认证上限；更换模型后上限回退为 1，直到重新
  基准。
- `doctor` 输出逻辑处理器数、可用内存、CUDA 设备数、CTranslate2 支持的计算类型
  以及最终推导的线程预算。
- 未来若改成共享 ASR 常驻服务，才重新评估单实例 `num_workers` 和权重共享；该
  结论不适用于当前 V1 的独立 Worker 架构。
