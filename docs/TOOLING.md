# 短剧 Agent 的环境、工具与验收约定

这份文档定义本仓库中工具能做什么、证据如何保存，以及什么时候可以报告任务完成。机器可读清单见[工具注册表](../contracts/tool_registry.json)。清单中的工具名是能力边界；只有标明的 Python 入口和 CLI 已能实际执行，清单本身不是自动调用器。

`implemented` 表示清单描述的有限功能已实现；`partial` 表示只具备其中部分能力；`planned` 表示尚无该能力的实现入口。任何状态都不等于实际素材已经通过验收。

## 工程就是可检查的制作环境

| 对象 | 当前文件或字段 | 用途 |
| --- | --- | --- |
| 冻结剧本 | `script.json`、派生阅读稿 `script.md` | JSON 为权威来源；来源 ID 与剧本版本用于追溯 |
| 制作规格 | `project.json` 的 entities / facts / events / shots | 描述对象、声明状态、事件与镜头依赖 |
| 剧本来源 | `script_source`、`source_refs` | 保存来源快照、哈希与原文片段；不代替语义审阅 |
| 生成任务 | `outputs/videos/<候选目录>/job.json`、`video.mp4` | 保存模型请求、task_id、用量、输入版本与下载哈希 |
| 验收契约 | `fixtures/S002.acceptance.json` 或新契约文件 | 生成前固定预期、必过条件、项目与镜头哈希、回归范围 |
| 媒体检查 | `inspection.json`、`F000.jpg` 等 | 保存真实媒体元数据、完整解码结果、实际帧时间及文件哈希 |
| 模型观察 | `observations.json` | 保存带帧引用的静态抽样判断和模型追踪信息 |
| 验收报告 | `acceptance-report.json` | 逐条记录 PASS / FAIL / UNKNOWN、证据及完成状态 |

工程版本、视频或图片变化后，旧证据不再自动适用于新版本。不要通过覆盖旧候选、删除必过条件或放宽预期让新结果“通过”。需要修改创作目标时，建立新的规格和契约，保留旧记录。

目前的版本能力是哈希绑定、候选保留与相同请求缓存。尚无统一资产库、候选选用表、版本分支、选用回滚或自动重建系统。`references` 字段只是引用槽位，不能视为已批准身份参考。

## 运行环境

Python 3.10+；Python 模块仅用标准库，无需先执行 `pip install`。`inspect` 另需 FFmpeg 和 ffprobe 可执行程序。

媒体工具按以下顺序寻找：进程环境变量 `FFMPEG_PATH` / `FFPROBE_PATH`，项目 `.tools/ffmpeg/bin/`，最后是系统 PATH。Windows 对应 `ffmpeg.exe` / `ffprobe.exe`。这两个覆盖变量由媒体模块读取进程环境，不会自动从 `.env` 加载。

例如已有本地安装时，可在 PowerShell 中设置：

```powershell
$env:FFMPEG_PATH = 'C:\tools\ffmpeg\bin\ffmpeg.exe'
$env:FFPROBE_PATH = 'C:\tools\ffmpeg\bin\ffprobe.exe'
```

项目本地安装的来源、版本及校验记录使用 `.tools/ffmpeg/installation.json`；只有该记录和可执行文件均存在且能运行，才可认为安装完成。每次检查还把实际运行的工具版本写入 `inspection.json.tool_versions`。未找到可执行工具、解码失败或超时都不能报告媒体检查通过。

共享百炼 Key 存在项目 `.env` 的 `DASHSCOPE_API_KEY`，或由进程环境提供。`.env` 不应提交到版本库。不同能力使用不同协议地址：

```dotenv
DASHSCOPE_API_KEY=
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
BAILIAN_TEXT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
BAILIAN_TEXT_MODEL=qwen-plus
BAILIAN_VISION_MODEL=qwen3-vl-plus
BAILIAN_VISION_MAX_TOKENS=4096
BAILIAN_VISION_REQUEST_TIMEOUT=180
BAILIAN_VIDEO_MODEL=wan2.6-t2v
BAILIAN_VIDEO_RESOLUTION=720P
BAILIAN_VIDEO_RATIO=9:16
```

此代码块是空白配置说明，不要用它覆盖已有私有配置。视频、文本和视觉模型复用 Key，但 Key、模型与 Endpoint 必须属于同一地域。视觉配置复用文本的 Key 和兼容接口地址。未设置 `BAILIAN_TEXT_BASE_URL` 时，文本模块从视频地址的同一域名推导 `/compatible-mode/v1`；显式设置文本地址后，应在更换视频地域时一起核对。已有业务空间域名时只替换对应域名，保留各自协议路径。

环境变量优先于 `.env`。配置展示命令只显示 Key 是否存在，不显示内容。任务与报告不得保存 Authorization 头或原始 Key；图像只在视觉请求中编码为 Data URI，观察结果不包含 Base64。

## 工具调用与调度边界

剧本入口是 `StoryRunner.build/compile`；`query_context` 查询镜头相关设定、开始/结束状态和来源。`state_before_origins` 还说明当前状态由初始事实还是之前的哪次事件产生。编译循环只修订制作规格，保持冻结剧本及全部必需节拍不变，修订次数有明确上限。

视频入口是 `compile_shot`、`generate_video`、`VideoRunner.submit/resume` 和 `BailianVideoClient.get_task`。当前仅文生视频；没有把角色参考图、首尾帧或声音身份自动作为生成条件传入的能力。任务管理实现提交、查询、恢复和下载；没有取消、服务端调度或统一预算控制。`Ctrl+C` 只停止本地等待，不取消云端生成。

相同视频请求默认复用原 task_id；`--new` 明确创建新候选并产生新请求。网络提交结果不明时保留 `SUBMISSION_UNKNOWN`，不得用自动重新 POST 猜测是否成功。文本模型已完成响应可缓存；失败或中断请求不默认重放。视觉观察一次调用只请求一次；对同一候选重新发起观察仍可能产生费用，不应把网络错误当作“没有计费”。

`create_contract` 建立验收契约，`regression_plan` 给出声明依赖的复查范围，`run_checks` 检查证据是否匹配并汇总判定。回归计划本身没有执行任何回归测试。当前没有完成自动观察、决策、局部修复、重新验收的通用 agent 循环。

| 工具名 | 可用状态 | 实际入口或尚缺能力 |
| --- | --- | --- |
| `read_project` | implemented | 读取本地 JSON，`pianchang.production.validate` / `validate_compilation`；无同名调度入口 |
| `query_story_state` | implemented | `pianchang.story_agent.query_context` / `context`，包含相关状态的因果来源 |
| `patch_spec` | partial | 显式改稿后校验、`StoryRunner.compile` 有界规格修复；无通用补丁接口 |
| `generate_image` | planned | 尚无图片或角色参考资产生成接口 |
| `edit_image` | planned | 尚无局部重绘或参考图修复接口 |
| `generate_video` | implemented | `compile_shot`、`generate_video` / `generate`，仅文生视频 |
| `edit_video` | planned | 尚无通用视频编辑、局部替换、续写接口 |
| `generate_audio` | planned | 尚无独立配音、音效、音乐生成接口 |
| `align_dialogue` | planned | 尚无转写、说话人、强制对齐或口型核对 |
| `edit_timeline` | planned | 尚无可编辑的剪辑时间线与音轨编排 |
| `render_episode` | partial | `pianchang.episode_export.export_preview` 按所选顺序硬切、统一编码并检查完整解码；始终是 PREVIEW_ONLY，尚无多轨剪辑、独立配音、字幕、配乐和转场 |
| `inspect_media` | implemented | `pianchang.media_tools.inspect_media` / `python -m pianchang.acceptance inspect` |
| `analyze_media` | partial | `BailianObserver.complete_observations` / `observe`，只作静帧抽样观察 |
| `run_checks` | partial | `create_contract`、`regression_plan`、`run_checks`；未具备的证据能力保留 UNKNOWN |
| `manage_jobs` | partial | submit / get / resume / download；无取消与统一调度 |
| `manage_versions` | partial | 哈希、候选与缓存；无选用、回滚和分支合并 |

## S002 的操作示例

以下命令在项目目录执行。使用 `fixtures/story_project.json` 的 S002；该镜头目标为 8 秒。契约固定 720×1280，生成配置也应使用 720P、9:16。命令示例不表示 S002 已通过验收。

先查询规格和来源，在生成前固定契约与回归范围：

```powershell
python -m pianchang.story_agent validate fixtures/story_project.json
python -m pianchang.story_agent context fixtures/story_project.json --shot S002
python -m pianchang.acceptance contract --project fixtures/story_project.json --shot S002 --width 720 --height 1280 --output fixtures/S002.acceptance.json
python -m pianchang.acceptance regression --project fixtures/story_project.json --shot S002
python -m pianchang.video_agent generate --project fixtures/story_project.json --shot S002 --dry-run
```

契约文件已存在且内容不同时命令拒绝覆盖。改变规格或目标后，应为新契约选择新路径。

下面的 `generate` 会调用百炼并产生视频生成费用。它输出 JSON，下载完成后其中包含实际素材路径：

```powershell
$videoJob = python -m pianchang.video_agent generate --project fixtures/story_project.json --shot S002 | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw '视频生成未完成，请按任务记录处理。' }
python -m pianchang.acceptance inspect $videoJob.local_video_path --output outputs/reviews/S002/candidate-01 --interval 1 --max-frames 9
```

中断或下载失败后，使用生成命令实际返回的 `job.json` 恢复：`python -m pianchang.video_agent resume "outputs/videos/<实际候选目录>/job.json"`。`status` 只查一次状态。只有明确要新候选时才对 `generate` 加 `--new`。

`inspect` 是本地操作，写出 `outputs/reviews/S002/candidate-01/inspection.json` 及 JPEG 帧。帧以解码索引选择，记录实际源 PTS 与相对媒体起点的 `time_s`；请求的抽样时刻不冒充实际取到的帧时刻。抽帧被数量上限限制、某帧提取失败、全文件解码超时等信息保存在采样与检查记录中。

下面的 `observe` 会调用百炼视觉模型并产生调用费用。只对契约中 `sampled_visual` 条目作辅助观察：

```powershell
python -m pianchang.acceptance observe --contract fixtures/S002.acceptance.json --inspection outputs/reviews/S002/candidate-01/inspection.json --output outputs/reviews/S002/candidate-01/observations.json
python -m pianchang.acceptance check --project fixtures/story_project.json --contract fixtures/S002.acceptance.json --inspection outputs/reviews/S002/candidate-01/inspection.json --observations outputs/reviews/S002/candidate-01/observations.json --job $videoJob.manifest_path --output outputs/reviews/S002/candidate-01/acceptance-report.json
```

`--job` 指向生成命令返回的实际 `job.json`，用于将素材哈希与目标镜头、工程版本和生成输入绑定；不能用另一个成功任务替代。未提供任务记录时，必过项 `candidate_binding` 为 UNKNOWN；记录与实际候选不匹配时为 FAIL。恢复已有任务后，使用其 `manifest_path` 或实际任务文件路径。

也可不传 `--observations`，先执行纯本地技术检查。新观察、新候选或修改后的报告使用新输出路径，保留此前证据。`check` 的退出码是 0=PASS、1=FAIL、3=UNKNOWN、2=输入或执行错误；其他操作返回成功只表示该操作完成。

## 观察和验收的能力边界

`pianchang.media_tools.inspect_media` 可检查视频元数据、全文件严格解码并提取带实际时间戳的帧。解码整个文件不等于已经看完并理解整个片段，检测到音轨不等于已经核对声音内容。

`BailianObserver.complete_observations` 给 Qwen VL 的输入是各张图片与时间戳标签，不是完整视频。一次最多 24 张、单图最多 10 MiB，整个请求保守限制为 16 MiB。`inspect` 允许的抽帧数更高；传给视觉模型前仍须遵守视觉接口限制。

| 判断内容 | 当前可用证据与结果边界 |
| --- | --- |
| 文件解码、分辨率、时长 | 本地工具与固定契约比较，可给出限定范围的 PASS / FAIL；缺失或超时为 UNKNOWN |
| 引用帧中的道具、服装外观 | 模型可给出带帧引用的 PASS / FAIL / UNKNOWN；仅适用于引用的静态画面，模型仍可能判断错误 |
| 角色身份 | 无已批准参考及校准的身份比较证据时 UNKNOWN；外貌相似不能证明同一角色 |
| 全程动作、交接顺序、完整起止状态 | 稀疏帧不能证明连续过程，UNKNOWN |
| 场景世界坐标与机位几何 | 缺少场景参考与机位验证，UNKNOWN；画面左右不能直接替代东西南北 |
| 音轨对白、说话人、口型和音画同步 | 未实现真实音轨转写与对齐证据，UNKNOWN |
| S001→S002、S002→S003 的衔接 | 未绑定相邻候选、组合预览与审看证据，UNKNOWN |
| 表演、节奏、叙事和整体创作质量 | 保持独立创作审阅，不从模型观察标签推导通过 |

`run_checks` 要求生成任务、观察、图片、视频、工程和契约版本匹配；捏造帧 ID、无依据的条目、重复或缺失观察不能当作有效通过。检查器还依据当前项目重建固定验收策略：删除或降级必过项、将证据能力改成更弱类型、缩小 scope 或回归范围，都会导致证据完整性检查失败。模型输出的 PASS 是候选观察标签，只有检查器在对应证据能力范围内才能采用。不能把静帧观察拿去满足连续视频、音频或参考身份比较的条件。

任何 required 条件为 FAIL 或 UNKNOWN，`task_complete` 都必须为 false。当前默认契约包含尚未具备证据能力的必过条件，因此运行所有已实现命令也可能正确地停在 UNKNOWN。停止重试、得到文件、API 返回成功、规格检查通过，都不是素材验收通过。

## 测试层级与回归

| 层级 | 检查什么 | 不能证明什么 |
| --- | --- | --- |
| 单元测试 | 请求构造、错误处理、状态流转、哈希和条件判定；模型响应通常是离线模拟 | 不证明远端服务可用，也不证明生成内容正确 |
| 集成测试 | 本地 HTTP、真实 FFmpeg/ffprobe、文件落盘和模块间接口协作 | 不等于真实生成视频已经满足创作要求 |
| 端到端测试 | 固定规格与契约，经真实生成、下载、检查、观察形成完整报告 | 流程跑通不能把尚缺证据的必过条件改为 PASS |
| 回归检查 | 修改一个镜头后，复查它、声明依赖它的后续镜头及相邻衔接 | 影响范围不等于所有受影响镜头必须重新生成；计划也不等于检查已经执行 |

运行 `python -m unittest discover -s tests -v` 检查代码行为；测试结果以本次实际输出为准，不在本文件固定数量。真实模型端到端调用与离线测试分开记录。修复 S002 后至少依据 `regression_plan` 复查 S002、S003，以及 S001→S002、S002→S003 的衔接；缺少邻片媒体时，应保留 UNKNOWN 和所缺证据，不能报告回归已通过。

## 参考接口

- [百炼 OpenAI 兼容 Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [Qwen 结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)
- [图像与视频理解及输入限制](https://help.aliyun.com/zh/model-studio/vision)
- [FFmpeg](https://ffmpeg.org/ffmpeg.html)、[ffprobe](https://ffmpeg.org/ffprobe.html)
