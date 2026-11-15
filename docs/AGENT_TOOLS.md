# 剧本 Agent 与视频制作 Agent 的工具清单

工作台按故事、角色和当前任务装配工具。表中的英文名是模型实际调用的工具名，代码位置指向执行函数。常规对话和只读答疑使用同一角色会话，由服务端决定本轮开放哪些工具。[工作台会话入口](../pi_bridge/service.py#L372) · [Pi 工具注入](../pi-runtime/src/main.mjs#L40)

| 角色 | 工作台常规对话 | 只读答疑 `inspect` | 专项任务 | 跨阶段不同工具名 |
| --- | --- | --- | --- | --- |
| 剧本 Agent | 10 项剧本与分镜工具 + 4 项共用工具 | 5 项读取或校验工具 + 4 项共用工具 | 处理改稿请求时增加 2 个工具名，执行定向改稿时增加 1 个 | 17 |
| 视频制作 Agent | 7 项制作对话工具 + 4 项共用工具 | 5 项查询工具 + 4 项共用工具 | 已授权的制作循环最多 19 项，其中 2 项参考输入工具和 `finish_task` 也出现在对话中 | 27 |

## 两个 Agent 共用的工具

两种工作台对话模式都开放以下 4 项。进入专项改稿或拍摄循环后，仍由 `finish_task` 请求结束该任务。正式改稿的投递与回应使用各角色的改稿工具。

| 工具 | 用途 | 实现方式 | 代码位置 |
| --- | --- | --- | --- |
| `ask_other_agent` | 向同一故事的另一位 Agent 提问或委派协作。 | 把问题和当前可用的剧本、制作规格的资产 ID、版本及哈希写入协作库，再投递到对方会话。只读请求会把 `inspect` 传给对方。 | [PiSessions.ask](../pi_bridge/service.py#L217) |
| `read_collaboration_requests` | 查看本角色待答的请求及已收到的答复，恢复会话后可继续处理。 | 按故事和收件角色查收件箱，对每条待答请求加载它固定的资产版本，校验哈希后返回该版内容。 | [PiSessions.collaboration_inbox](../pi_bridge/service.py#L248) |
| `reply_collaboration_request` | 对指定协作请求作答。 | 用 `request_id` 核对本故事待办和当前执行任务，保存关联回复、登记收件回执并通知原会话。 | [PiSessions.answer_collaboration](../pi_bridge/service.py#L266) · [回复投递](../pi_bridge/service.py#L335) |
| `finish_task` | 结束答疑、报告任务完成，或说明为何需要外部输入。 | Pi 为每轮注入此工具。`responded` 结束答复并保留任务状态，`completed` 须通过本阶段的独立完成检查，`needs_input` 须满足停止条件。 | [工具注册](../pi_bridge/runtime.py#L298) · [PiLoop.execute](../pi_bridge/runtime.py#L155) |

## 剧本 Agent

### 工作台对话中的剧本编写与分镜发布

以下 10 项均在常规对话中注册。标有“可用”的 5 项也开放给只读答疑。校验工具会保存报告。写入操作还取决于候选的状态，例如已发布剧本须先开启修订才能写新稿。[剧本工作区注册](../pianchang/script_collab_agent.py#L94) · [工作台工具装配](../pi_bridge/script_tools.py#L78) · [只读过滤](../pi_bridge/service.py#L376)

| 工具 | 只读答疑 | 用途 | 实现方式 | 代码位置 |
| --- | --- | --- | --- | --- |
| `read_script` | 可用 | 看当前正在编写或核对的剧本。 | 返回候选稿的副本、任务约束、基础版本和目标节拍 ID。 | [ScriptWorkspace.read_script](../pianchang/script_collab_agent.py#L125) |
| `write_draft` | — | 写入一份完整候选稿。 | 接收标题、梗概、大纲、铺垫和分集内容，规范化为带稳定 ID 的剧本，写入 `candidate.json` 并清除旧校验结果。 | [ScriptWorkspace.write_draft](../pianchang/script_collab_agent.py#L138) |
| `check_script` | 可用 | 检查当前稿是否满足可验证的创作要求。 | 校验剧本结构、明确的数量约束和必含词。导入时比对原稿哈希，定向改稿时检查变更是否限于授权节拍。报告绑定候选哈希，语义忠实度单列为 `UNREVIEWED`。 | [ScriptWorkspace.check_script](../pianchang/script_collab_agent.py#L190) |
| `publish_script` | — | 固定可供分镜和改稿引用的剧本版本。 | 新稿须有与当前候选哈希一致的 `PASS` 报告，随后发布不可变剧本资产并保存回执。继续已有发布时返回其版本引用。 | [工作台发布入口](../pi_bridge/script_tools.py#L191) · [候选核验](../pianchang/script_collab_agent.py#L201) · [版本发布](../pianchang/script_collab_agent.py#L262) |
| `begin_script_revision` | — | 按用户的改稿要求，从当前版本开始修订。 | 保存基础版本、用户消息和改稿理由，重置校验状态并清空分镜候选。单纯的“继续运行”等指令会被拒绝。 | [StoryTools.begin_revision](../pi_bridge/script_tools.py#L196) |
| `read_story_context` | 可用 | 查看原始创意、故事约束和中断位置。 | 汇总已发布剧本或候选、版本引用、数量要求、分镜格式及旧调用的中断证据。 | [StoryTools.read_context](../pi_bridge/script_tools.py#L106) |
| `read_production_spec` | 可用 | 看已保存的分镜候选及其问题。 | 读取候选，有候选时执行即时校验，连同剧本引用、既有发布和中断记录一起返回。 | [StoryTools.read_spec](../pi_bridge/script_tools.py#L114) |
| `patch_production_spec` | — | 定点修改分镜中的镜头、状态或来源引用。 | 将路径和值组成的 JSON 补丁应用到候选副本，禁止写入冻结的 `script_source`。按哈希留存旧候选，保存新候选并重验。 | [StoryTools.patch_spec](../pi_bridge/script_tools.py#L119) |
| `check_production_spec` | 可用 | 查分镜能否追溯到原文、有无漏拍或状态矛盾。 | 嵌入冻结剧本，校验来源引用、节拍覆盖、顺序、状态及明确数量，分别报告结构问题、缺失参考和能否进入生成。 | [StoryTools.check_spec](../pi_bridge/script_tools.py#L144) |
| `publish_production_spec` | — | 把结构合格的分镜发布为制作工程。 | 重验并发布绑定剧本版本的 `source-project`，写入工作台记录，按需调度参考资产任务。同内容可复用既有发布。缺参考时可先发布工程，拍摄仍须等参考就绪。 | [StoryTools.publish_spec](../pi_bridge/script_tools.py#L163) |

### 改稿请求的审理与定向改稿

视频方提交正式请求后，剧本 Agent 先检查证据，再决定是否接受。接受会产生独立改稿任务。该任务开放 `read_script`、`replace_beat`、`check_script`、`publish_script` 和 `finish_task`。视频方可凭发布回执取得新版引用。[协商任务装配](../pianchang/collab_negotiation.py#L229) · [改稿任务执行](../pianchang/collab_negotiation.py#L297)

| 工具 | 阶段 | 用途 | 实现方式 | 代码位置 |
| --- | --- | --- | --- | --- |
| `inspect_revision_request` | 审理请求 | 查清视频方想改哪一段，以及依据是什么。 | 按请求固定的版本读取剧本和证据，校验目标节拍及原文，返回证据内容和当前最新版本。 | [ScriptNegotiation.inspect](../pianchang/collab_negotiation.py#L191) |
| `decide_revision_request` | 审理请求 | 选择保留原稿、提供替代拍法或接受改稿。 | 检查证据后记录 `retain`、`alternative` 或 `accept`。接受前验证基础版仍是最新版，再创建改稿任务。仅供核对验收规格的请求不能授权改稿。 | [ScriptNegotiation.decide](../pianchang/collab_negotiation.py#L202) |
| `replace_beat` | 执行改稿 | 替换委托指定节拍的文本。 | 复制冻结剧本，按稳定的 `beat_id` 替换授权节拍并递增版本号，保存为待校验候选。 | [ScriptWorkspace.replace_beat](../pianchang/script_collab_agent.py#L145) |

## 视频制作 Agent

### 工作台对话中的状态查询与委托接续

常规对话开放以下 7 项，前 5 项也用于只读答疑。查询失败原因可以直接读取任务证据。用户要求继续拍摄时，服务端另行核对原委托的版本、范围及可恢复状态。[视频对话工具装配](../pi_bridge/service.py#L419)

| 工具 | 只读答疑 | 用途 | 实现方式 | 代码位置 |
| --- | --- | --- | --- | --- |
| `read_video_state` | 可用 | 看整个工程的制作进度和当前阻碍。 | 调用 `studio_flow` 聚合工作台状态和资产待办。故事尚无制作工程时返回原因。 | [工作台状态入口](../pi_bridge/service.py#L394) · [studio_flow](../pianchang/studio_flow.py#L228) |
| `read_reference_issue` | 可用 | 查某个角色、场景或道具参考出了什么问题。 | 以 `subject_id` 和 `purpose` 定位资产待办中的具体参考项，返回其检查、错误和证据引用。 | [read_issue](../pi_bridge/service.py#L414) |
| `read_current_commission` | 可用 | 确认当前获准拍哪一版、哪些镜头、还剩多少额度。 | 读取当前制作委托的冻结范围、限制、已预留用量、最近执行状态和恢复阻碍。 | [current_commission](../pianchang/video_execution.py#L32) |
| `read_production_diagnostics` | 可用 | 定位失败发生在供应商任务还是镜头输入预检。 | 从冻结委托和实际任务记录中提取失败证据，逐镜头列出参考版本、图片数量和模型兼容性检查结果。 | [production_diagnostics](../pi_bridge/diagnostics.py#L8) |
| `read_reference_inputs` | 可用 | 查某个镜头准备向模型提交哪些参考。 | 按 `shot_id` 读取固定来源、编排策略、已审核参考板、失败证据和可提交状态。授权拍摄时也使用同一实现。 | [ReferenceInputs.read](../pianchang/reference_inputs.py#L187) |
| `prepare_reference_inputs` | — | 参考图超过模型输入上限时，准备可提交的参考板。 | 用 `identity_first` 或 `balanced_boards` 编排图像，记录各区域的来源版本。独立视觉核验通过后发布输入包，保留声音参考。 | [ReferenceInputs.prepare](../pianchang/reference_inputs.py#L208) |
| `continue_authorized_production` | — | 用户要求继续或修复时，接续当前拍摄委托。 | 按 `commission_id` 检查可恢复性、并发任务和输入预检，先返回等待交接。本轮对话落盘后再次核对，再调用 `resume_video_job` 接续原任务及额度。 | [接续检查](../pi_bridge/service.py#L400) · [执行交接](../pi_bridge/service.py#L441) |

### 授权拍摄的计划、生成、复用与交付

制作循环装载 4 项拍摄工具和 4 项修复工具。有参考库和故事上下文时，还会装载上表的 `read_reference_inputs` 和 `prepare_reference_inputs`。挂载改稿协商器时增加 3 项协商工具，计划启用自动修复时增加 5 项自动修复与恢复工具。加上 `finish_task`，完整清单为 19 项。[制作工具装配](../pianchang/video_collab_agent.py#L365) · [按委托组合工具](../pianchang/video_collab_agent.py#L981)

| 工具 | 用途 | 实现方式 | 代码位置 |
| --- | --- | --- | --- |
| `plan_production` | 确定本版要拍、可复用及须复查衔接的镜头。 | 读取冻结剧本和指定制作规格，按需适配前版或编译分镜，比对原文、状态与资产依赖，保存工程和影响分析。 | [VideoWorkspace.plan](../pianchang/video_collab_agent.py#L410) |
| `generate_shot` | 为授权范围内的一个镜头生成视频。 | 核验输入并预留额度，编译提示和元数据，解析固定参考，调用 `VideoRunner` 提交或接续任务，下载候选视频后触发镜头验收。 | [额度与镜头范围检查](../pi_bridge/runtime.py#L182) · [VideoWorkspace.generate](../pianchang/video_collab_agent.py#L490) |
| `reuse_shot` | 将前一版已拍镜头用于当前版本。 | 核对源任务、媒体文件哈希、新旧镜头依赖和固定参考，再把旧媒体绑定到当前工程并验收。 | [VideoWorkspace.reuse](../pianchang/video_collab_agent.py#L586) |
| `review_and_export` | 验收全部镜头及衔接，导出整片预览。 | 按当前工程逐镜验收，核对媒体、报告和相邻镜头衔接。全部必过项为 `PASS` 才按剧情顺序导出，遇到 `FAIL` 或 `UNKNOWN` 则返回具体问题。 | [VideoWorkspace.review_and_export](../pianchang/video_collab_agent.py#L770) · [交付检查](../pianchang/video_collab_agent.py#L815) |

### 拍摄中的验收与定向修复

| 工具 | 用途 | 实现方式 | 代码位置 |
| --- | --- | --- | --- |
| `read_acceptance` | 查一个镜头或全片的必过失败项和证据缺口。 | 读取当前报告中的原要求、实际观察、音视频时间范围及报告路径，同时返回固定参考、供应商失败和修复历史。 | [VideoRepair.read](../pianchang/video_repair.py#L221) |
| `prepare_shot_repair` | 将具体问题变成可执行的修复方案。 | 核对当前报告中的未通过项，或读取供应商终态失败回执。把问题、候选或失败任务、报告及修复指令绑定为 `repair_id`。 | [VideoRepair.prepare](../pianchang/video_repair.py#L258) |
| `regenerate_shot` | 对准备好方案的镜头定向重拍。 | 校验 `repair_id` 和原委托的修复额度，以冻结参考和修复提示生成新候选，保留旧候选，重新验收并比较结果。 | [VideoRepair.regenerate](../pianchang/video_repair.py#L517) |
| `edit_shot_asset` | 对已有镜头做切黑或字幕修复。 | 用本地 FFmpeg 从指定时刻切黑，可按冻结剧本原文加字幕。输出等时长的新候选，保留来源并重新验收。 | [VideoRepair.edit](../pianchang/video_repair.py#L535) |

### 拍摄中的正式改稿协商

| 工具 | 用途 | 实现方式 | 代码位置 |
| --- | --- | --- | --- |
| `read_negotiation_context` | 核对本委托可引用的剧本、证据和已发请求。 | 按制作委托的消息 ID 查询改稿记录，返回冻结剧本引用、证据引用及已请求的节拍。 | [VideoNegotiation.context](../pianchang/collab_negotiation.py#L97) |
| `request_script_revision` | 把具体剧情矛盾、证据和修改建议发给剧本 Agent。 | 核对冻结剧本版本、目标节拍及证据归属，以“制作委托 + 节拍”生成请求 ID，持久投递并防止重复开单。 | [VideoNegotiation.request_script_revision](../pianchang/collab_negotiation.py#L103) |
| `read_script_response` | 查剧本方是否答复、有无改稿任务及实际发布的新版。 | 按请求 ID 查询关联回复、改稿工作项和发布回执。有回复时登记收件并返回实际版本引用，尚在等待时返回 `pending`。 | [VideoNegotiation.read_script_response](../pianchang/collab_negotiation.py#L133) |

### 自动修复与中断恢复

| 工具 | 用途 | 实现方式 | 代码位置 |
| --- | --- | --- | --- |
| `design_repair_strategy` | 对明确失败的镜头设计新的拍法。 | 将失败观察、历史尝试和邻镜头证据交给文本模型，检查方案有实质变化后调用修复准备逻辑，返回 `repair_id`。 | [VideoAutonomy.design](../pianchang/video_autonomy.py#L527) |
| `coordinate_agents` | 推进本次制作委托尚未处理的改稿协商。 | 找到本委托发出的请求，调度剧本方核对与决策。若剧本方接受改稿，再执行独立改稿任务并读取回执。 | [VideoAutonomy.coordinate](../pianchang/video_autonomy.py#L468) |
| `review_existing_shot` | 已有视频时，补做缺失或中断的验收。 | 在现有候选上恢复镜头验收，保存尝试次数、结果和执行错误。 | [VideoAutonomy.review_existing](../pianchang/video_autonomy.py#L424) |
| `record_specification_blocker` | 登记剧本原文与镜头起止状态要求之间已核实的矛盾。 | 核对本委托的协商答复、当前镜头、报告、字段及原文引句，登记该字段受阻。原必过项及 `FAIL` 或 `UNKNOWN` 的结论一并保留。 | [VideoAutonomy.record_specification_blocker](../pianchang/video_autonomy.py#L319) |
| `resume_pending_generation` | 找回已提交但本地尚未取得最终结果的视频。 | 用原 `task_id` 和任务记录核对绑定，查询供应商状态，完成后下载并验收。此路径沿用已预留额度，只查询原任务。 | [VideoAutonomy.observe_pending](../pianchang/video_autonomy.py#L384) |
