# 多供应商模型网关改造方案

**当前实现。** Pi 继续管理两个 Agent 的会话和工具循环，片场的 Python 层按用途选择连接和模型。新供应商优先通过进程内的 **LiteLLM Python SDK** 接入。现有百炼调用沿用原生客户端。用途、连接和模型别名由 [配置加载器](../pianchang/model_routes.py) 解析。

单机工作台继续使用本地 `drama-gateway`，第一版暂缓部署 LiteLLM Proxy。百炼原生路径可直接运行。启用 SDK 路由时，在启动工作台的 Python 环境、仓库根目录执行 `python -m pip install ".[models]"`，安装锁定的 LiteLLM 1.109.1。[现有 Python 依赖](../pyproject.toml#L9) · [LiteLLM SDK 与 Proxy](https://docs.litellm.ai/docs/)

**每项用途独立选模。** 剧本对话读取 `routes["script.decision"]`，视频制作对话读取 `routes["video.decision"]`，参考图生成读取 `routes["reference.image"]`，普通视频和带参考的视频各有自己的路由。多个用途可以共用模型，也可以分别使用不同供应商。拍摄委托确认时固定实际路由，已经提交的镜头按原供应商和原账户恢复。

## 片场、Pi 和 SDK 各自负责什么

| 工作内容 | 片场与 Pi 负责 | LiteLLM SDK 负责 |
| --- | --- | --- |
| 连接、账户与用途 | 片场以 `config/models.json` 为唯一路由配置，服务端解析用途、账户、端点、模型和凭据，执行权限与额度检查。 | 使用片场为本次调用显式传入的连接和模型，处理供应商鉴权与协议差异。 |
| Agent 对话与辅助文本 | Pi 维护每个故事和角色的会话、工具调用 ID、结果回填与上下文压缩。片场装配工具白名单，为对话、分镜编译、提示词和修复策略分别选模。 | 转换消息和工具参数，调用指定上游，将回答、工具调用及错误转换成片场可处理的格式。 |
| 参考图、声音和视频 | 片场固定剧本与素材版本、参考用途、镜头范围和预算。片场管理候选、选用、任务恢复和交付。 | 对已验证的能力转换媒体输入，调用生图或视频接口，按原任务标识查询并取得结果。 |
| 证据与验收 | 片场用独立 Trace SDK 在发送前持久化最终上游请求的脱敏副本，保存实际响应、错误、回执和媒体哈希。片场执行镜头与衔接必过项检查。 | 提供能够取得的原始响应、供应商请求或任务标识、用量和媒体结果，供片场关联证据。供应商未返回的字段保留未知。 |

Agent 对话经过 **Pi → 本地网关 → 片场选定的 SDK 或原生客户端 → 供应商**。后台工具从 Python 层进入同一套选模逻辑。供应商密钥按调用传给选定客户端，Pi 只取得本地网关令牌。并发使用两个账户时，无需改写进程级环境变量或 SDK 全局密钥。

现有入口可从 [Pi provider 注册](../pi-runtime/src/main.mjs#L25)、[Python 对话网关](../pi_bridge/model_gateway.py#L29)、[参考图生成](../pianchang/studio_assets.py#L404)、[声音模型](../pianchang/asset_voice.py#L18)、[视频模型校验](../pianchang/bailian_video.py#L68) 和 [声画验收](../pianchang/content_review.py#L294) 定位。

## 配置多个 Key、Base URL 和模型用途

`.env` 保存本机凭据。每个变量名代表一份可以独立轮换的密钥。以下值均为占位符。

```dotenv
DASHSCOPE_API_KEY=...
GEMINI_API_KEY=...
OPENAI_API_KEY=...
ARK_VIDEO_API_KEY=...
```

把 [config/models.example.json](../config/models.example.json) 复制为 `config/models.json`，将示例模型 ID 换成账户已开通的型号或接入点。配置分三层。**连接**指定真实供应商、账户、Base URL 和密钥环境变量名。**模型**指定连接、模型 ID 和执行方式。`adapter: "litellm"` 表示使用 Python SDK，`sdk_provider` 指定 SDK 使用的协议实现。**用途**引用模型别名。下例仅演示一种搭配：Gemini 用于剧本对话、OpenAI 用于生图、Seedance 用于带参考的视频。部署时可选择其他满足对应用途要求的模型，并填入账户已开通、经过验证的模型或接入点 ID。

```json
{
  "version": 1,
  "extends": "bailian",
  "connections": {
    "google-main": {
      "provider": "google",
      "account": "studio-google",
      "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
      "api_key_env": "GEMINI_API_KEY"
    },
    "openai-art": {
      "provider": "openai",
      "account": "studio-openai",
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY"
    },
    "ark-production": {
      "provider": "volcengine-ark",
      "account": "studio-ark",
      "base_url": "https://ark.cn-beijing.volces.com/api/v3",
      "api_key_env": "ARK_VIDEO_API_KEY"
    }
  },
  "models": {
    "gemini-writer": {
      "connection": "google-main",
      "adapter": "litellm",
      "sdk_provider": "openai",
      "model_id": "<已开通且支持工具调用的 Gemini 模型 ID>"
    },
    "gpt-reference-image": {
      "connection": "openai-art",
      "adapter": "litellm",
      "sdk_provider": "openai",
      "model_id": "<已开通的 GPT Image 模型 ID>"
    },
    "seedance-reference-video": {
      "connection": "ark-production",
      "adapter": "litellm",
      "sdk_provider": "volcengine",
      "model_id": "<已开通的 Seedance 模型 ID 或方舟接入点 ID>"
    }
  },
  "routes": {
    "script.decision": "gemini-writer",
    "video.decision": "bailian.text",
    "reference.image": "gpt-reference-image",
    "video.text_to_video": "bailian.video",
    "video.reference_to_video": "seedance-reference-video"
  }
}
```

- **更换某一项用途。** 例如，把示例中的 `script.decision` 从 `gemini-writer` 改为 `bailian.text`，配置生效后的新剧本对话就用百炼文本模型。要更换生图模型，只需将 `reference.image` 指向另一个已配置并验证过的图像模型别名。
- **继承和覆盖。** `extends: "bailian"` 从现有 `.env` 生成百炼原生连接、模型和默认用途，提供示例里的 `bailian.text` 和 `bailian.video`。未显式覆盖的参考板检查、镜头视觉验收、声画衔接、语音生成与审核继续走原实现。缺少整个新配置文件时也沿用旧行为。显式配置引用不存在的连接或模型时，报告具体用途和错误。
- **协议与账户。** 示例中 Gemini 使用 Google 的 OpenAI 兼容聊天端点，所以 `sdk_provider` 为 `openai`，请求仍使用 `google-main` 的地址和 Google Key。方舟使用 SDK 的 `volcengine` 实现。调用层按 `sdk_provider` 和 `model_id` 构造 SDK 模型标识，业务路由只引用模型别名。
- **同一把 Key 可以配多个模型与地址。** 百炼兼容聊天接口和原生媒体接口可各设一个连接，共用 `DASHSCOPE_API_KEY`。如果方舟有测试和生产两个账户，则建两个连接，各自引用对应的环境变量。稳定的 `account` 标识用于追踪账户，轮换同账户的密钥只更新密钥值。
- **只校验已启用用途所需的凭据。** 启动时解析用途、连接和模型，指出哪项缺 Key 或缺适配器。用非百炼供应商覆盖所有启用用途的安装，可以在没有百炼 Key 时工作。
- **能力按用途核对。** Agent 决策需要工具调用和工具结果回填。结构化文本任务需要约定的输出格式。镜头生成须满足参考类型、数量及模型输入限制。视听验收须能处理本项要求的视频和音频证据。配置错误或缺少能力时，返回阻碍，不静默改用另一付费模型。

示例所用接口见 [Gemini 兼容聊天接口](https://ai.google.dev/gemini-api/docs/openai)、[OpenAI 图像生成接口](https://developers.openai.com/api/docs/guides/image-generation) 和 [LiteLLM 火山方舟视频适配](https://docs.litellm.ai/docs/providers/volcano#video-generation)。

### 可配置的用途

工作台从项目根目录读取 `config/models.json`。CLI 显式传入 `--env-file` 时，以该环境文件所在目录为根读取对应的 `config/models.json`，便于为不同部署准备独立配置。

| 用途 | 执行入口 | 当前可选协议 |
| --- | --- | --- |
| `script.decision`、`video.decision` | 两个 Pi Agent 分别对话、调用工具和压缩会话 | 百炼原生或 SDK 的 `openai` 兼容聊天 |
| `story.compile`、`reference.prompt`、`voice.casting`、`video.repair_strategy` | 分镜编译、参考图提示、选声与重拍策略 | 百炼原生或 SDK 的 `openai` 兼容聊天 |
| `reference.image` | 人物、道具、场景等资产的参考图 | 百炼原生或 SDK 的 `openai` 图像生成 |
| `video.text_to_video`、`video.reference_to_video` | 普通镜头与带固定参考的镜头 | 百炼原生或 SDK 的 `volcengine` 视频 |
| `voice.synthesis`、`voice.review`、`vision.review`、`continuity.review` | 语音合成、声音审查、视觉审查、相邻镜头声画验收 | 已有百炼原生适配器 |

替换以上已接协议的型号时，更新模型别名或用途即可。新增 SDK 协议时，先在 [SDK 调用边界](../pianchang/sdk_transport.py) 验证实际请求、输入输出、单次发送和恢复，再在配置加载器中为对应用途开放。配置文件只保存密钥变量名；同名系统环境变量优先于项目 `.env`。调整配置后重新启动工作台，让新会话读取同一份配置。在账户轮换密钥时保留原连接名、账户和端点，以便继续查询旧任务。

## 分阶段迁移

当前的结构化文本输出上限和请求超时沿用 `BAILIAN_TEXT_MAX_TOKENS`、`BAILIAN_TEXT_REQUEST_TIMEOUT`，Pi 上下文窗口沿用现有会话设置。试用新型号时，请核对这些参数是否在供应商支持范围内。模型配置里的 ID 只标识选定模型；账户是否已开通该型号，需要在真实联调时确认。

先保证旧百炼工程兼容，再依次接入对话、生图和视频能力。每一阶段都从本次部署的 `routes` 读取待验证模型。实施顺序只决定先验证哪类能力，供应商可以独立选择。

| 步骤 | 代码改动 | 通过标准 |
| --- | --- | --- |
| 1. 接住旧工程和百炼配置 | 引入统一的连接、模型和用途配置。将现有文本、普通及参考视频、生图、语音和视听检查映射到原生百炼客户端。 | 只有原 `.env` 且未安装 LiteLLM 的环境可以启动。模型选择、已保存剧本、镜头、回执和报告可按旧路径读取。 |
| 2. 引入锁定版本的 SDK，接通对话 | 在 Python 网关接入 SDK 客户端。Pi 本地 provider、会话和工具回调继续使用现有入口。按两个角色各自的决策路由选模，上下文压缩继承本角色连接并单列 `compaction`。 | 模拟上游验证发送前 Trace 和双账户凭据隔离。两个角色可分别配置不同模型，实际请求与各自路由一致。工具结果按原调用 ID 回填后能继续作答，只读委派沿用原权限。 |
| 3. 接通参考图生成，收拢后台直调入口 | 资产任务按 `reference.image` 选择模型和客户端，取回图像并绑定到候选资产及独立检查。分镜编译、参考图提示、重拍策略、选声、字幕、参考板和视听检查逐项改为按用途取客户端。 | 工作台、后台与 CLI 对同一用途选择同一连接。更换生图路由后，新图的实际来源与新配置一致，既有资产版本仍可追溯。候选检查失败时保留原选用版本。 |
| 4. 接通视频生成并验证恢复 | 按普通视频和带参考视频的路由解析模型，将参考输入预检改为按能力判断。核对所选客户端的提交、按 ID 查询、下载和参考角色映射。对付费创建关闭自动重试、换部署和跨供应商降级。 | 通过下文的最终请求比对、两类中断试验和真实参考素材联调，再开放对应视频用途。达不到要求的镜头类型采用已验证的原生实现，或明确显示当前不支持。 |
| 5. 启用通过验证的用途 | 启动器按已选用途准备依赖、检查凭据，界面展示实际供应商和模型。更新首次启动与使用说明。锁定 SDK 版本升级前复测受影响用途。 | 在受影响系统用全新解压包验证百炼与混合配置。默认视频模型切换后旧付费任务按原账户恢复，已有候选、额度和交付门槛均可核对。 |

迁移时应覆盖 [现有运行时网关](../pi_bridge/runtime.py#L291)、[剧本编译](../pianchang/story_agent.py#L283)、[参考图提示词](../pianchang/studio_assets.py#L321)、[重拍策略](../pianchang/video_autonomy.py#L569)、[选声](../pianchang/asset_voice.py#L115)、[参考输入预检](../pianchang/reference_inputs.py#L77)、[制作启动预检](../pianchang/video_collab_agent.py#L967) 和 [双击启动器](../tools/launch_workbench.py#L129)。

### 保护已确认和已付费的任务

1. **确认前固定路由。** 在计算拍摄计划的 `plan_id` 前，将每个待拍镜头的供应商、稳定账户、端点、模型 ID、SDK 或原生执行方式及对应版本写入计划。新委托读取新配置，既有委托沿用已确认的范围和额度。[计划摘要](../pianchang/studio_jobs.py#L245)
2. **在实际出网处记录证据。** 登记凭据，在 SDK 完成请求转换之后、供应商收到请求之前，持久化最终请求的脱敏副本。收到实际响应或错误时立即记录并关联委托，保存参考版本、供应商回执和可用的用量。发送前发生 `TraceError` 时阻断发送，发送后的记录失败则停止后续业务动作并保留未知状态。仅有 SDK 入参或成功回调日志不足以满足该契约。[现有发送边界](../pi_bridge/model_gateway.py#L53) · [Trace 接入契约](TRACE.md#new-program-contract) · [LiteLLM 原始请求与响应日志](https://docs.litellm.ai/docs/observability/raw_request_response)
3. **恢复只查原任务。** 保存 SDK 返回的查询标识、可取得的供应商原始任务 ID 和两者对应关系，并保留原账户与连接。已有可靠标识时按原路径查单、下载，可使用同账户轮换后的 Key。上游可能已受理但无可靠 ID 时，保留 `SUBMISSION_UNKNOWN` 和已预留额度。此时严禁重新创建或改用原生客户端再提交。[现有未知状态处理](../pianchang/video_agent.py#L422) · [原任务恢复](../pianchang/video_agent.py#L454) · [LiteLLM 重试与降级行为](https://docs.litellm.ai/docs/router/learn/reliability)
4. **保留旧版校验方式。** 旧记录继续按原有 `base_url + request + metadata` 校验哈希。新格式单独记版本，将实际路由加入指纹和缓存键。恢复历史百炼任务时核对旧记录中的模型和端点，沿用原回执，不重写历史记录。[旧回执校验](../pianchang/video_agent.py#L160) · [现有供应商任务记录](../pianchang/video_agent.py#L388)

备份或迁移本地视频任务时，保留整个视频输出目录，包括候选的 `job.json` 和目录下的 `.video-submissions.sqlite3`。后者记录创作输入摘要和对应候选，供中断恢复时识别已经占用的提交，避免切换模型后为同一份结果未知的请求再次付费。

## 接新模型时怎样判断改动范围

以**供应商、用途和锁定 SDK 版本**为验证单位。协议文档列出模型支持后，还要验证片场真正使用的输入、响应、上游 Trace 和任务恢复路径，再决定该用途的执行方式。

| 接入情形 | 改动范围 | 判断依据 |
| --- | --- | --- |
| 同供应商、同协议，替换一个已覆盖能力的型号 | **只改模型配置** | 更新 `model_id` 或模型别名，沿用现有适配器，验证该用途的输入、输出和限制。 |
| 增加第二把 Key、另一个账户或区域端点 | **只改连接配置** | 若认证方式、协议和模型能力相同，新建连接并绑定对应账户及密钥变量。原账户轮换 Key 时只更新变量值。 |
| 在已经接入的模型之间切换，例如剧本从千问换到 Gemini | **只改用途路由** | 两个模型均已验证可完成同一用途。新路由只用于后续新任务，已提交视频仍走原连接。 |
| 新供应商声明兼容现有聊天协议 | **协议测试通过后只加配置** | 用一轮实际消息序列验证系统提示、工具参数、调用 ID、结果回填及结构化输出。参数或返回格式有差异且现有适配器无法表达时，扩展适配器。 |
| 供应商协议不同，但锁定的 LiteLLM SDK 已覆盖所需操作 | **优先复用 SDK** | 配置连接、SDK provider 和模型，验证实际工作流。例如带参考视频要走通提交、查单、下载及固定参考比对，验证通过即可复用该适配。 |
| SDK 缺少所需能力，或不能满足证据与恢复要求 | **仅为该供应商的该项能力保留或编写原生适配器** | 典型情形包括必需参考的角色、数量或音频无法表达，最终请求无法在发出前记录，`TraceError` 无法阻断发送，或无法按原账户恢复任务及阻止重复创建。原生路径须通过相同验收。 |
| 新模型带来当前工作台尚无的业务动作，例如上传已有视频并指定区域做局部编辑 | **扩展业务流程，按 SDK 覆盖情况决定适配改动** | 定义新的输入、用户授权范围、工具参数和验收标准。SDK 已覆盖编辑操作时直接复用，缺少必要输入或证据时为该操作补充原生路径。 |

原生实现的选择发生在**新调用发出前**。例如某个模型的多参考镜头只有原生适配器能满足要求，就为这类新委托配置该适配器，其他用途仍读取各自的路由。既有百炼委托和历史回执继续沿用原生路径。

## 验证与交付

本次已经用锁定版本的 LiteLLM 和模拟 HTTP 上游验证对话、生图、双账户并发，以及方舟视频的创建、按原任务 ID 查询和下载。Windows 已用全新解压的工作台验证首次启动和重启。部署前的真实账户联调仍须核对该账户的模型权限、输入限制和实际返回的媒体；下表保留这些验收项。

本次方舟视频适配覆盖文本镜头和固定版本的图片、角色声音参考。提交前按当前适配范围检查时长 4–15 秒、最多 9 张图片和 3 段声音、声音合计不超过 15 秒，并逐项核对发送给供应商的参考角色。需要视频参考或方舟素材库标识的镜头，需要先扩展输入和回执的适配。[片场视频输入校验](../pianchang/bailian_video.py) · [方舟视频生成说明](https://docs.volcengine.com/docs/ark/video-generation-tutorial?lang=zh)

| 验证方式 | 场景 | 可观察的通过标准 |
| --- | --- | --- |
| 离线模拟 | SDK 转换请求、并发使用同商的两个账户 | 模拟上游收到的端点、模型、请求体和参考角色与发送前持久化的脱敏副本一致。测试端收到各自的测试 Key，Trace 中不含明文。注入发送前 `TraceError` 后，上游收到 **0 个请求**。 |
| 离线模拟 | 只改用途路由，分别替换一个对话模型和一个生图模型 | 对比切换前后的模拟请求。只改 `script.decision` 时，剧本对话发往新模型，视频对话和生图仍按原配置发送。随后只改 `reference.image` 时，只有新生图请求发往替换后的模型。 |
| 离线模拟 | 模拟供应商已受理视频，但创建响应在途中丢失，本地没有可靠任务 ID | 模拟端共收到 **1 次创建 POST**。重启、刷新或切换默认模型后，**新增创建 POST 为 0**。本地保留未知状态、原证据与额度。 |
| 离线模拟及历史回放 | 本地已保存可靠任务 ID，下载前中断，再更换默认路由 | 按原账户和任务标识查询并下载，**恢复期间创建 POST 为 0**。旧百炼任务沿用旧哈希校验，已有镜头、固定参考和预留额度可核对。 |
| 真实联调 | 配置的对话模型执行一次 Pi 工具调用 | 请求使用该角色配置的模型，工具结果按原调用 ID 回填，角色能继续作答。 |
| 真实联调 | 配置的生图模型生成一张实际参考图 | 参考图可打开，候选记录可追溯到本次实际使用的供应商、模型、媒体哈希和可获得的回执。 |
| 真实联调 | 配置的视频模型完成提交、查单、下载及验收 | 对每个启用的视频用途核对冻结模型和实际回执。带参考的镜头还须区分首帧与贯穿全片的参考，逐项核对资产版本、提交角色和供应商受理证据。必需项无法表达时该输入组合不提交。下载后对实际视频及相邻衔接执行原必过项。 |

供应商字段的核对随所选适配器执行。例如，选用 LiteLLM 的火山方舟适配器时，`input_reference` 表示首帧，贯穿全片的参考按支持类型和角色放入 `content`。其他视频适配器按各自的协议核对相同的业务要求。[LiteLLM 火山视频输入说明](https://docs.litellm.ai/docs/providers/volcano#video-generation)

单元测试使用模拟上游。真实联调只在已有供应商授权和额度内执行，交付记录分别列出离线模拟、历史回放、真实调用以及仍未验证的用途。新增公开执行入口时登记 Trace 契约，并运行 `python -m tools.check_trace_contract`。该命令只检查登记标记，上表验证实际行为。
