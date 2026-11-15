# 片场 · 制作与开发指南

本文的命令从仓库根目录执行。产品介绍与工作台截图见[项目首页](../README.md)。

片场为剧本、人物和镜头建立可追溯的身份与版本。工作台使用 Python 3.11 或更高版本。

剧本编译、Agent 对话、参考图和镜头生成分别读取自己的模型用途。默认配置使用千问和万相，百炼原生客户端使用 Python 标准库。启用其他供应商时，在仓库根目录运行 `python -m pip install ".[models]"`，复制 [模型配置示例](../config/models.example.json) 为 `config/models.json`，填写模型 ID，并在 `.env` 或系统环境中设置各账户的密钥。编辑 `routes` 可以只切换一个用途。保留旧连接可让已提交的视频在切换默认模型后继续查询。媒体检查仍使用 FFmpeg/ffprobe。[模型用途和适配范围](MULTI_PROVIDER_MODEL_GATEWAY.md#可配置的用途)

工作台可以从剧本生成参考图和角色试音，自动核验、修订并把通过的资产版本绑定到镜头。**参考资产通过自检，不代表成片通过验收。** `pianchang/production.py` 的 `spec_pass: true` 只检查声明的制作规格；镜头生成后还需要技术检查、媒体观察和整集复查。完整连续动作、声音一致性和精细剪辑仍有覆盖边界。

## 启动与首次配置

把下载的 ZIP 解压到你有写入权限的文件夹。在 Mac 的访达里双击解压后的 **`启动片场.command`**，在 Windows 10/11 的资源管理器里双击 **`启动片场.cmd`**。看到百炼 API Key 提示时可直接按回车，浏览器会打开本机工作台。[下载片场 ZIP](https://github.com/stock-programmer/set/archive/refs/heads/main.zip)

| 启动窗口里的提示或现象 | 接下来怎么做 |
| --- | --- |
| Mac 提示找不到 Python 3.11 或更高版本 | 在「现在打开 Python 官方下载页吗？」处按回车。下载与你的 macOS 版本兼容的 Python 3.11 或更高版本的稳定版安装包（`.pkg`），双击完成安装。在「应用程序」下对应的 Python 文件夹里双击 `Install Certificates.command` 完成 HTTPS 证书配置，然后再次双击片场启动文件。官方 universal2 安装包适用于 Apple 芯片和 Intel 芯片。[Python for macOS](https://www.python.org/downloads/macos/) · [Python 安装说明](https://docs.python.org/3/using/mac.html#installation-steps) |
| Mac 提示无法验证开发者 | 如果确认压缩包来自片场 GitHub 仓库，在尝试打开文件后进入「系统设置 → 隐私与安全性」，找到这次被阻止的启动文件，点「仍要打开」，再确认打开。[Apple 打开下载软件的说明](https://support.apple.com/zh-cn/102445) |
| Mac 提示启动文件没有执行权限 | 在终端输入 `chmod u+x`，按一次空格，将解压后的 `启动片场.command` 从访达拖到终端，按回车。回到访达再次双击文件。 |
| Windows 提示找不到 Python 3.11 或更高版本 | 如果电脑有 Windows 程序包管理器（winget），输入 `Y` 并回车即可为当前用户安装 Python 3.12 并继续启动。如果没有 winget 或安装失败，启动器会询问是否打开官方下载页。手动安装 Python 3.11 或更高版本后再次双击。[Python for Windows](https://www.python.org/downloads/windows/) |
| 缺少 Node.js 或 npm | 仍可进入工作台查看内置分镜。使用剧本和视频制作 Agent 前，安装 Node.js 22.19 或更高版本，保留安装器附带的 npm。填好 Key 后再次双击启动，即可联网下载锁定依赖。[Node.js 下载](https://nodejs.org/zh-cn/download) |
| Agent 依赖下载失败 | 检查电脑能否访问 npm registry，再重新双击。若需在终端查看 npm 的完整错误，在项目目录运行 `npm --prefix pi-runtime ci --ignore-scripts`（Windows PowerShell 请用 `npm.cmd`）。 |
| 找不到 FFmpeg 或 ffprobe | 查看剧本和分镜可以继续。要检查媒体、抽帧或导出成片，已安装 Homebrew 的 Mac 可以运行 `brew install ffmpeg`。也可以从 FFmpeg 下载页选择对应系统的构建，把两个可执行文件一起放入项目的 `.tools/ffmpeg/bin/`。Mac 文件名为 `ffmpeg` 和 `ffprobe`，Windows 文件名带 `.exe`。[Homebrew 的 FFmpeg 安装说明](https://formulae.brew.sh/formula/ffmpeg) · [FFmpeg 下载](https://ffmpeg.org/download.html) |
| 浏览器没有打开或提示端口已占用 | 手动打开启动窗口显示的完整地址。默认从 `http://127.0.0.1:8765` 开始查找，最后尝试 8774。若从终端指定的端口已被占用，换一个空闲端口或让启动器自动选择。 |

**已安装工具，工作台仍提示缺少组件。** 后台服务使用启动时的环境。如果在片场运行期间修改了 PATH 或设置了新的系统环境变量，先等正在制作的任务结束，再重启电脑并双击对应系统的启动文件，让服务读到新的设置。

**第一次配置百炼。** 登录百炼控制台，在华北 2（北京）地域创建 API Key，复制后粘贴到启动窗口并按回车。输入时密钥会被遮挡，保存位置是项目根目录的 `.env`。已设置的 `DASHSCOPE_API_KEY` 环境变量优先。[获取北京地域 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)

**先体验，稍后再填 Key。** 在密钥提示处按回车，启动器会跳过 Agent 依赖下载，直接打开工作台。选《最后一把钥匙》就能阅读随包提供的剧本和三镜分镜。准备在线创作时，再次双击对应系统的启动文件补填 Key。已安装 Node.js 和 npm 时，启动器会自动准备 Agent 依赖。本文提到的历史视频、验收报告和协作工程保存在原制作机器的 `outputs/` 中。

**从终端启动。** 在仓库根目录执行对应系统的命令。以下示例在后台启动服务，并打印工作台地址。同一份项目已经运行时会复用原服务。

在 macOS 终端运行

```sh
./启动片场.command --no-browser
# 如需更换端口
./启动片场.command --port 8766 --no-browser
```

在 Windows PowerShell 运行

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\start_web.ps1
# 如需更换端口
powershell -ExecutionPolicy Bypass -File .\tools\start_web.ps1 -Port 8766
```

需要前台调试时，在 Mac 上运行 `python3 -m pianchang.web_server --port 8765`，在 Windows 上运行 `python -m pianchang.web_server --port 8765`。按 Ctrl+C 停止该前台服务。关闭浏览器不会取消已经提交的云端视频任务。

## 独立 Agent 与协作记录

剧本 Agent 用 `pianchang/script_collab_agent.py` 独立创作、导入和改稿，交付可阅读剧本及结构化版本。视频 Agent 用 `pianchang/video_collab_agent.py` 独立接收剧本，管理分镜、媒体、检查和预览。两者各有模型历史、工具和完成检查。工作台左侧的 **Agent 协作记录**（`/collaborations`）可查看各自的 Trace、收件回执、版本影响与预览。原有工作台的「创意与剧本」入口继续使用制作规格编译工作流。

```powershell
# 单独创作剧本
python -m pianchang.script_collab_agent create --story-id my-story --goal "深夜便利店里，店员认出了失联十年的朋友" --output outputs/my-script
# 单独承接该剧本的制作
python -m pianchang.video_collab_agent outputs/my-script/published-script.json --asset-id my-story --revision-id r1 --output outputs/my-production --max-video-generations 3 --vision
```

从新协作开始时，`python -m pianchang.collaboration_workflow` 对《红毯尽头》发出真实制作委托，先生成全部三个镜头，再改 B003 并按依赖更新。每次新运行都会创建新委托。已有协作可在独立进程用 `python -m pianchang.collab_worker --run-id <协作ID> --agent script --watch` 和 `--agent video --watch` 分别收件。在恢复已有任务时使用原协作 ID。

资产记录使用稳定 ID、不可变版本、SHA-256 和带用途的依赖引用。发布剧本与投递事件在同一事务中进行，定向制作委托单独记录范围和额度。成片结果绑定剧本版本、选用的媒体和验收证据。使用 `python -m tools.collab_audit --run-id <协作ID> --beat B003` 可离线核对协作证据。

如果模型服务中断而首版素材已全部下载，`python -m pianchang.collab_continuation --run-id <协作ID> --prepare` 仅在本地核对云端任务清单和素材，导出首版预览并保存改稿委托。模型服务恢复后，使用相同 ID 和 `--resume` 执行待办改稿与局部制作。原停止记录和恢复 Trace 可分别查看。

## 固定镜头的参考素材，并查询实际提交

镜头的 `required_refs` 保存资产 ID、资产版本、资产 JSON 的 SHA-256、原文件字节的 SHA-256、主体 ID 和用途。同一剧本可以有多版制作规格。每条候选的 `job.json` 在 `reference_receipt` 中分别记录要求、准备提交的引用、已确认提交的引用、实际请求字段位置、请求摘要和供应商状态。超时或缺少历史回执时保留未知。

以下 PowerShell 示例为《红毯尽头》r2 的 S003 创建一份新制作规格。准备林薇的定妆照和 1～10 秒 WAV/MP3 音色参考，将示例中的输入路径替换成自己的文件：

```powershell
$db = "outputs/collaborations/assets.sqlite3"
$run = "139bef01621c4e25b79a20175fbd0b02"
$story = "story-$run"
$base = "outputs/collaborations/runs/$run/video-r2/project.json"
$spec = "outputs/web/stories/red-carpet-reference-spec-r1/project.json"
python -m pianchang.reference_assets --store $db import --story-id $story --asset-id "$story/portrait-lin" --subject-id char:LinWei --purpose portrait --file .\lin-portrait.png
python -m pianchang.reference_assets --store $db import --story-id $story --asset-id "$story/voice-lin" --subject-id char:LinWei --purpose voice --file .\lin-voice.wav
python -m pianchang.reference_assets --store $db bind --story-id $story --project $base --shot-id S003 --spec-asset-id "$story/spec" --reference "$story/portrait-lin" r1 --reference "$story/voice-lin" r1 --output $spec
python -m pianchang.video_agent generate --project $spec --shot S003 --reference-store $db --reference-story-id $story --dry-run
```

`--dry-run` 只检查本地资料。上面的导出路径会让工作台发现新规格，刷新首页后可在这个工程的「分镜制作」中操作。要复用已有制作的镜头，可将新规格交给协作视频 Agent。下面的命令使用原剧本和 r2 制作基线，新增视频提交额度为一次：

```powershell
python -m pianchang.collaboration_workflow --run-id $run --reference-spec "$story/spec" --spec-revision r1 --base-production r2 --max-video-generations 1
```

有固定参考的镜头按照 `.env` 中的 `BAILIAN_REFERENCE_VIDEO_MODEL=wan2.7-r2v` 选择模型，普通镜头使用 `BAILIAN_VIDEO_MODEL`。参考适配器将图片放在 `input.media[i].url`，把同一人物的音色放在该条目的 `reference_voice`。定妆照按核验过的字节传入，音色在正式执行时上传到百炼私有临时存储。模型不支持、内容哈希不符或引用无法解析时会在视频提交前停止。[百炼参考生视频接口](https://help.aliyun.com/zh/model-studio/wan-video-to-video-api-reference)

参考图片超过模型的 5 个视觉槽时，视频 Agent 可调用 `read_reference_inputs` / `prepare_reference_inputs`，选择保留独立身份图或调整分组的参考板方案。原始 `required_refs`、剧本及镜头数量保持冻结；FFmpeg 不裁切拼板，经逐区域像素核对和独立视觉核验后才允许提交。视频制作页的“模型输入编排”显示原图片数、实际槽位、合成参考板和审核证据。视觉核验占用原委托模型额度；失败后可换策略，未确认请求不自动重发。完整覆盖关系以新版提交回执记录，参考板通过不表示成片通过。详见 [Pi integration](PI_INTEGRATION.md)。

升级已有资产时，使用相同 `--asset-id` 和 `--parent-revision r1` 导入新媒体。再以先前导出的规格作 `--project`，用 `bind --parent-revision r1` 将目标镜头绑定到新资产版本，输出到新文件。在工作台「Agent 协作记录」页输入资产 ID 和版本，可以分别查看镜头要求、候选提交、供应商受理和未知记录。命令行也可查询：

```powershell
python -m pianchang.reference_usage --store $db --jobs-dir outputs/videos --asset-id "$story/portrait-lin" --revision-id r1
```

## 启动片场并制作短剧

剧本与视频制作的持续会话接入 Pi SDK。Pi 子进程由 Python 后台按会话管理。在故事页面点击「与制作 Agent 对话」可以继续原故事、补充要求或查询原因。具体职责、代码位置、恢复边界与验证方式见 [Pi 接入说明](PI_INTEGRATION.md)。

工作台已接入独立的公共 Trace 系统。点击侧栏 **Trace 调试台**，或新任务卡中的 **查看 Trace**，即可查看请求与后台任务的关联、调用树、模型上下文/响应、校验结果、工具输出和错误。查看后续事件、展开大内容、导出 JSON、核对完整性均可在页面操作。旧任务保留为“历史任务：未采集 Trace”，不会补造历史。架构、独立 SDK/CLI、后续程序接入规范见 [TRACE.md](TRACE.md)；开发接入约束见 [AGENTS.md](../AGENTS.md)。

工作台只保留一条主导航：**剧本与分镜 → 共享资产 → 视频制作 → 人工预览**。旧的镜头候选、下载、本地检查和任务执行设置归入视频制作的展开区；原来的 `shots/checks/agent/export` 及 `studio&section=...` 链接会自动进入对应阶段。当前故事的 Pi 对话、制作动态和 Trace 保持关联。

在「视频制作」或独立进度页，可以逐个预览本次委托已经生成或复用的镜头。每个片段旁显示该版本的验收状态。播放时服务端会重新核对委托、镜头和文件哈希。整片的人工预览、导出及交付仍由 `pianchang/preview_access.py` 核对当前剧本、交付版本、所有镜头及相邻衔接的必过项和媒体证据，全部通过后开放。

`python -m unittest tests.test_preview_access -v` 使用真实本地 FFmpeg 视频和明确标注的模拟语义结论，验证拒绝、通过、播放、下载范围请求及换版失效；不会调用付费 API。`python -m tools.verify_workflow --project <工程ID>` 可核对正在运行的工作台，并将真实 HTTP 与 Trace 证据保存到 `outputs/web/unified-workflow-verification-*.json`。

视频制作按镜头保存进度。中断后，拍摄计划会核验已下载镜头，以及原云端任务在后续查询中下载的素材，显示“继续制作 · 补拍剩余镜头，沿用已有镜头”。结果不明的已提交任务会阻止重复生成，不能当作未拍镜头直接重提。恢复委托有独立记录，原委托及原任务清单保留。

每个镜头生成或复用后立即自动验收。验收是视频制作 Agent 循环内部步骤：读取未通过项的实际证据，准备绑定候选和报告版本的定向修复，自主选择修改提示词并使用固定参考重拍，或调用本地视频工具切黑并添加剧本原文字幕，然后重新检查当前镜头和相邻衔接。旧候选、报告和模型回执保留，修改后的输入与组合片段使用独立目录，不能重放结果未确认的收费调用。规格和必过条件保持固定，不能通过删条件完成。

工作台默认每镜头最多自动重拍 2 次、本地编辑 2 次；模型决策另有按镜头数计算的步数和上下文上限，均记录在制作委托里，并在提交前显示修复额度。达到上限、执行不确定、缺少可补证素材或规格矛盾均属于制作未完成。只有所有镜头及相邻衔接必过项全部 PASS，才能导出并发布制作交付、进入「人工预览」。自动诊断和修复记录在「视频制作 Agent」内部可查；FAIL / UNKNOWN 不能由人工点击已阅变为通过。启动工作台的历史补充验收只恢复检查，不自动重放旧制作委托；需继续时创建有记录的新委托。

内容验收会把实际视频与该镜头固定的已批准参考图一起提交给视频理解模型，检查角色外观、场景布局、完整动作和起止状态。起止状态按相关对象的每个声明字段复核；画外对象、对视关系或人物知情状态无法确认时，报告必须写出具体原因。模型按10fps审看片段，不能据此保证原视频更短时间内没有缺陷。

已有的相邻镜头会自动生成切点两侧组合片段，并单独提取与画面同步的实际音轨。视听检查核对人物、服装道具、空间、动作和声音，提供音色参考时一并对照；同时读取剧本换场信息，允许合理的跨场与时间省略。缺少后续镜头时明确标为“素材缺失，尚未执行”，不冒充已审看。每项结论附参考素材及视频/音轨的时间区间、文件哈希和模型回执；新报告保存为独立版本，人工确认绑定具体报告。在检查进行中暂不能确认尚在更新的结论。

新建故事时只需提交一句话创意。剧本和分镜编译通过后，工作台自动生成人物、场景、服装、道具参考图及角色试音。Agent 对照剧本检查实际图片或声音；必过项全部通过后，系统自动选用这个版本并绑定到用到该对象的镜头。旧工程中已通过自检且证据仍匹配的候选会在启动时自动选用，无需再次生成。资产页可以预览、试听、查看证据；如果不满意，输入修改要求并点击“重新生成”。新候选未通过时，不会替换已选用的版本。

在「共享资产 → 需处理」中，如果你看过图片或听过试音，认为当前版本可以用于拍摄，点击「人工通过」即可选用并绑定到相关镜头，无需重新生成。人工决定只对这版素材和对应的自检报告有效。原自检结论与失败任务保留在历史 Trace 中，人工选用有独立的 Trace；没有生成出可审看的素材时，不能人工通过。

| 环节 | 在界面上做什么 | 保存的产物和后续用途 |
| --- | --- | --- |
| 剧本 Agent | 点“新建故事”输入创意和创作约束，或导入已有的 `screenplay/1` 剧本。查看发布版本、节拍及其对应镜头 | 剧本及 Trace 记录在协作运行目录。编译出的 `project.json` 包含人物、场景、道具、服装设定和镜头引用 |
| 共享资产 | Agent 生成参考图和角色试音，查看或听取实际候选并对照剧本逐项检查。通过即选用；有修改意见时可重新生成 | 每次尝试保留候选版本、媒体指纹、剧本出处、自检报告与 Trace。相关镜头固定引用已选用的具体版本。媒体文件保存在 `outputs/collaborations/reference-blobs/`，自检、选用和绑定记录保存在协作资产库 |
| 视频制作 Agent | 按拍摄计划生成或复用，自动验收后依据证据修复，再复验。内部可查看逐镜头报告和修复记录 | 制作委托记录固定输入、修复额度；旧候选、报告、服务商回执和每轮修复记录统一留存 |
| 人工预览 | 所有镜头及相邻衔接必过项通过后才交付整片供人审看 | 未通过的历史诊断可查，但不能发布为已完成制作或通过人工已阅绕过验收 |

例如选择《红毯尽头》，在“共享资产”可看到林薇的设定版本和剧本出处，在“验收与预览”可看到制作 r2 复用 S001、S002，重做 S003，以及 15 秒处的切点复查。查看历史结果只读取本地记录。点击生图或提交制作委托会使用相应的百炼服务。

下列镜头操作也可以从工作台单独使用。

1. **先看已有示例**。选左侧的《最后一把钥匙》，在「剧本与分镜」查看 S002 的分镜和来源。若本机已有该镜头的制作记录，再到「视频制作」播放候选或查看验收证据。
2. **新建工程**：点击“新建故事”，输入一句话创意。剧本 Agent 根据故事转折安排分集、场次和节拍。各集的场数、各场的节拍数可以不同。在剧本页可展开实际结构，查看对应的分镜和来源。剧本和分镜检查通过后，工作台继续制作参考资产。
3. **生成镜头**：在“分镜制作”逐个选择镜头，点击生成。后端把人物、场景、道具、起止状态编译成真实万相请求；相同输入复用候选。如等待超时，选择已有候选后点击继续查询，不必重新生成。“生成新候选”明确发起另一份生成；旧视频保留。“自定义完整提示词”会替换自动提示，需自行保留完整制作要求。
4. **观察与验收**：播放候选，点击本地检查，执行解码、分辨率、时长、来源绑定检查并抽取证据帧。视觉观察会额外调用百炼视觉模型，对抽样帧给出有限的外观证据。PASS / FAIL / UNKNOWN 分别表示通过 / 失败 / 证据不足。人物身份、连续动作、空间几何和邻镜头一致性尚未具备完整检查能力；UNKNOWN 不算交付通过。
5. **修订与选用**：发现镜头偏差时，修改完整提示词生成候选，播放比较后选用，再重新检查。当前由人在界面触发修复；没有声称已经实现自动理解全部缺陷的导演循环。
6. **导出预览片**：所有镜头都有所选视频后，进入“预览片”导出。程序按工程镜头顺序硬切，统一为 720×1280、30fps，保留音轨或为无音轨片段补静音，并检查整片解码。结果标记 PREVIEW_ONLY，不代表剧情、台词、声音连续性或人物一致性验收已通过。当前无字幕编辑、独立配音、配乐、转场和多轨时间线编辑器。

创意编剧、视频生成、视觉观察按百炼账户规则计费；页面读取、本地检查及本地导出不调用模型。费用未从账单接口读取，不显示虚构金额。Key 继续保存在项目 `.env`，浏览器只获得是否已配置；服务只监听本机，静态资源为固定白名单，不公开项目目录。不要将端口转发到公网。

任务、选用关系、Web 新工程、验收证据和导出保存在 `outputs/web/`；视频仍在配置的视频输出目录。任务串行执行，同一请求标识复用已登记任务；服务重启将未完成任务标记为 interrupted，不自动重提可能计费的请求。视频任务的恢复入口继续查询原百炼 task_id。创作中断后的冻结剧本和调用记录保留在输出目录供排查，暂未提供 Web 端文本任务恢复按钮。

本地 Web 代码为 `pianchang/web_server.py`、`pianchang/app_service.py`、`web/`；预览导出为 `pianchang/episode_export.py`。前端不需要 Node 构建或安装 npm 依赖。

2026-11-04 已通过 Web 界面真实生成示例的 S001（6 秒）、S003（8 秒），与已有 S002（8 秒）合成 22 秒预览；本地解码、编码和总时长检查通过，浏览器实际播放正常。S003 的镜头检查为 UNKNOWN，S002 保留此前 UNKNOWN 报告，S001 尚未单独做内容验收；整段并未声称通过创作与一致性验收。Web 新增 41 项边界/任务/恢复测试与预览导出 10 项测试通过。首次全量 241 项回归出现 1 项 HTTP 3 秒超时，改为 10 秒并重跑包含新增恢复用例的 41 项 Web 测试后全部通过。

当前导出会按工程所有镜头顺序生成一个预览文件；多个分集暂不自动拆成多个交付文件。先用单集短场景验证完整流程。

## 工具与镜头验收切面

完整工具清单、作用、实现边界、软件测试层级对应关系和可执行示例见 [TOOLING.md](TOOLING.md)，机器可读入口见 [contracts/tool_registry.json](../contracts/tool_registry.json)。16 项能力均明确标注已实现、部分实现或计划，清单本身不是工具调度器。

本步先以 S002 为验收切面：在生成前固定 [S002 验收契约](../fixtures/S002.acceptance.json)，再检查实际素材。`inspect` 读取元数据、执行全文件严格解码、按真实帧 PTS 抽图；`observe` 调用百炼视觉模型对这些静帧作有帧引用的外观观察；`check` 根据固定条件和证据能力判定 PASS / FAIL / UNKNOWN。

**任务完成需要所有必过项 PASS。** 缺少角色参考、连续动作观察或相邻镜头复查时，不能把 UNKNOWN 改成成功。当前固定策略还会拒绝删掉内容条件、把必过改为可选、把连续动作检查改为静帧检查，或缩小回归范围来绕过验收。当前策略只接受其规定的固定条件，额外更严格条件可以加入；改变目标应修改制作规格并建立新的契约。

报告绑定工程、镜头、契约、生成任务、视频及帧文件的哈希；验收时重算实际文件哈希。未提供匹配的 `job.json` 时，无法确认视频属于目标镜头，`candidate_binding` 为 UNKNOWN。旧素材的模型观察不能直接用于新候选。

对 S002 的修改会形成 S002/S003 的镜头复查计划和 S001→S002、S002→S003 的衔接复查计划。计划会明确 `checks_executed=false`；没有真实相邻片段和复查证据时，不能宣称回归已完成。

2026-11-04 的真实端到端验收切面已执行：固定契约后生成 S002，下载 8 秒、720×1280、含 1 条音轨的候选；完整严格解码通过，从 240 帧中抽取 9 帧，实际时间戳为 0、1、2、3、4、5、6、7、7.966667 秒。Qwen3-VL-Plus 对引用静帧中的道具和服装给出了 PASS 观察标签；该标签仅是有证据引用的模型判断，不能代替人工审看或全片内容验收。

最终结果为 **UNKNOWN，task_complete=false**：角色参考身份、场景几何、连续交接动作、完整起止状态以及两个邻镜头衔接均缺少对应证据。没有把必过项删掉，也没有继续付费重生成来掩盖缺失的验收能力。

验收工具在指定的本地输出目录保存 `report.json`、带时间戳的检查清单和模型观察，生成目录保存 `video.mp4` 和 `job.json`。公开的验收报告示例见[《湖边的倒影》S002 工作台截图](images/lake-video-acceptance.png)。运行环境按[工具说明](TOOLING.md)准备 FFmpeg 和 ffprobe。

## 第二步：剧本编译器

这一步对应 coding agent 把需求转成结构化实现并检查结果的过程。输入是一句话创意，产物是可阅读的剧本与可运行检查的制作规格。编译器用百炼 `qwen-plus` 生成文字，不会自动启动视频生成。

| 本步动作 | 为什么要做 | 解决了什么 | 尚未解决什么 |
| --- | --- | --- | --- |
| 创意扩展为大纲、设定、分集、场次和剧情节拍 | 先保存明确的创作内容，后续才有可对照的依据 | 提供可审阅的剧本与结构 | 创意是否被准确理解、故事是否好看仍需审阅 |
| 程序分配来源 ID，冻结剧本并记录哈希 | 编译与修订不能偷偷改变原始要求 | 发现旧剧本引用、原文改动、版本不符 | 哈希不能判断剧情质量 |
| 生成对象、事实、事件与镜头，每项附原文引文 | 把设定和动作映射为可追溯的制作数据 | 检查来源存在、引文精确、镜头对应的集/场 | 引文存在不证明提取含义正确或所有设定均已提取 |
| 检查所有必需剧情节拍、事件执行和状态 | 防止制作过程中漏掉段落、倒置动作或凭空改变状态 | 发现漏镜头、声明状态冲突、角色无故获知信息 | 自然语言与结构化事实之间的语义偏差仍为未知 |
| 用检查报告修订制作规格，保留每次候选 | 把失败变成有限次数的纠错任务 | 可追踪、可恢复的编译检查循环 | 这还不是观看视频后的媒体修复循环 |
| 按镜头查询相关设定、状态与来源 | 给后续 agent 提供有边界的上下文 | 不必把整份剧本文字都放入单镜头提示词 | 尚未实现长剧分块编译、倒叙和多时间线 |

**一致性进度**：人物已有身份、设定来源、状态与版本检查；场景已有剧本场次到场景实体的绑定；剧情已有顺序状态、必需节拍覆盖与原文追踪。人物视觉、实际场景几何、对白演绎、声音和动作连续性仍须在生成后观察验证。

### 运行

复用 `.env` 的 `DASHSCOPE_API_KEY`。新增文本配置为 `BAILIAN_TEXT_MODEL=qwen-plus`、`BAILIAN_TEXT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`、`BAILIAN_TEXT_MAX_TOKENS=8192`、`BAILIAN_TEXT_REQUEST_TIMEOUT=180`。它与视频接口使用不同路径；都要求 Key 与地域匹配。未指定文本地址时，代码可从 `DASHSCOPE_BASE_URL` 的同一主机派生文本地址。

先用随附的固定样例验证，无需 Key、无需网络：

```powershell
python -m pianchang.story_agent validate fixtures/story_project.json --script fixtures/story_script.json
python -m pianchang.story_agent context fixtures/story_project.json --shot S002
python -m pianchang.video_agent generate --project fixtures/story_project.json --shot S002 --dry-run
```

从创意开始真实生成文字，按百炼文本调用规则计费：

```powershell
python -m pianchang.story_agent config
python -m pianchang.story_agent build --idea "夜晚旧办公室，林岚把铜钥匙交给陈默，陈默仍不知道钥匙的来源。" --output outputs/stories/my-story
```

默认由剧本 Agent 决定集数、各集的场数及各场的节拍数。调用方有明确篇幅要求时，可单独传 `--episodes`、`--scenes-per-episode` 或 `--beats-per-scene`。当前编译器一次处理一份剧本，最多支持 4 集、全剧共 12 场、48 个节拍，每场最多 8 个节拍；镜头时长为 2–15 秒整数。

也可以分开操作，先查看或修改剧本，再编译：

```powershell
python -m pianchang.story_agent draft --idea "一句话创意" --output outputs/stories/draft-one
# 审阅 script.md；如要改稿，编辑 script.json、保留既有来源 ID，并更新 script_version
python -m pianchang.story_agent compile --script outputs/stories/draft-one/script.json --output outputs/stories/revised-one
python -m pianchang.story_agent validate outputs/stories/revised-one/project.json --script outputs/stories/draft-one/script.json
```

`script.json` 是剧本的权威来源，`script.md` 是供阅读的派生稿；只修改 Markdown 不会改变编译输入。编译结果嵌入其来源快照，视频调用会检查该快照。要发现独立的外部剧本后来被修改，需使用上面的 `validate --script` 对照最新版；嵌入快照不能自动知道外部文件变化。

通过检查后，可以直接交给已有视频入口：

```powershell
python -m pianchang.video_agent generate --project outputs/stories/my-story/project.json --shot S001 --dry-run
# 去掉 --dry-run 才会发起视频生成请求
```

### 编译过程与验收边界

输出目录保留 `run.json`、`script.json`、`script.md`、每次模型调用的 `*-call.json`、每次候选与检查报告。只有本地检查通过才输出 `project.json`；失败候选也会保留，便于定位原因。`project.json` 兼容现有 `pianchang/production.py` 和 `pianchang/video_agent.py`，增加 `source_refs`、分集/场次标识和 `script_source`。

`source_refs` 使用 `source_id` 加原文精确片段，全部引用回到冻结剧本。来源索引包含用户创意 `SRC_IDEA`、梗概 `SRC_LOGLINE`、大纲 `SRC_OUTLINE`、`SET...` 设定和 `B...` 正文节拍；场景等设定可以引用原始创意和大纲，镜头、事件仍须引用正文节拍。每个剧情节拍的 `required=true` 由程序设定，编译模型不能把它改成可选。每个事件都需要一个执行镜头，该镜头也须引用事件的来源段落。同一个剧本场次不能绑定成两个不同场景实体，空事实表也不能绕过状态检查。

`context` 除了给出镜头开始与结束状态，还通过 `state_before_origins` 标明当前状态来自初始事实还是哪个先前事件。例如 S003 中钥匙归陈默，查询结果会保留 S002 交接事件与 B002 原文，便于解释“为什么此时在他手里”。

CLI 默认 `--max-repairs 1`，可设为 0–2。草稿和制作规格分别检查和修复。默认由模型安排集、场和节拍；如果调用方显式指定某一层数量，只对那一层做精确计数校验。草稿还会检查剧本格式与容量，通过后冻结剧本，再编译制作规格。旧草稿的修复沿用原任务保存的约束。网络错误、截断或 Trace 写入失败保留原始记录。相同输入及输出目录可以复用已完成的响应。

失败的草稿可在详情页点击“自动修复这份草稿”。新任务沿用原始创意与数量要求，读取已有输出并重新校验，将错误反馈给模型。原失败记录保留，新 Trace 用 `repairs` 关联原任务；有未确认的模型调用时拒绝自动重放。

`spec_pass` 检查声明状态；`provenance_pass` 检查来源、版本、引文及场次绑定；`coverage_pass` 检查必需节拍和声明事件覆盖。三者均通过才有 `ready_for_generation=true`。**这表示可以进入生成环节，不表示提取语义完全正确或剧集已经合格**。`semantic_fidelity_pass`、`visual_pass`、`audio_pass` 保持 `null`；模型也可能遗漏尚未显式登记的事实，需要独立语义审查。

命令退出码：0 为本次操作成功，1 为校验或调用失败，2 为参数用法错误，130 为用户中断。

### 第二步的真实验收

2026-11-04 已用百炼 `qwen-plus` 完成《红钥》示例，仍采用林岚拿出钥匙、交给陈默、陈默举起钥匙的三段动作。最终一轮保持剧本不变：第一次编译因场景引文并非原文连续片段而失败，模型根据检查报告修订一次后通过。结果为 1 集、1 场、3 个镜头，计划时长分别为 8、10、12 秒；这三个时长是新制作规格，不是已生成视频的实测时长。

代码库提供可离线读取的[冻结剧本](../fixtures/story_script.json)和[制作工程](../fixtures/story_project.json)。按上文的 `validate` 和 `context` 示例，可以查看校验结果和指定镜头的因果来源。

三个镜头均通过万相请求构造检查；缓存重跑已验证不产生新文本请求。本阶段没有提交新的视频任务。最终示例自动提取了钥匙归属、固定布局和陈默是否知道来源等机器事实；日夜、人物位置与服装等文字设定仍需审阅并补足结构化状态，不能据此声称所有事实已经抽取完整。

实测中曾发现草稿擅自加入线索、场景描述自相矛盾，以及正确布局误引用人物外貌段落。随后收紧创作约束、补齐创意与大纲来源索引，再使用新目录编译。这些例子也说明：精确引用与结构检查必须和语义审阅分开，不能把自动化检查通过写成剧情质量通过。

## 百炼视频生成

### 配置与离线检查

双击启动时会依据 `.env.example` 准备本机的 `.env`。仅使用命令行的开发者也可手动复制模板，填写自己的北京地域 API Key。以下为相关配置示例。

```dotenv
DASHSCOPE_API_KEY=你的北京地域API_Key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
BAILIAN_VIDEO_MODEL=wan2.6-t2v
BAILIAN_VIDEO_RESOLUTION=720P
BAILIAN_VIDEO_RATIO=9:16
```

默认生成 720P 竖屏视频，直接传提示词时默认 5 秒。环境变量优先于 `.env`；默认按代码所在目录寻找 `.env`，不依赖运行命令时的当前目录。`.gitignore` 已排除 `.env`、其他私有环境配置和 `outputs/`，不要把 Key 写进代码或提交到版本库。

北京现有 Endpoint 仍可使用；已有业务空间 ID 时，可把地址改为 `https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/api/v1`。Key、Endpoint 和模型必须属于同一地域。这里调用的是视频 API，不应填写 `/compatible-mode/v1` 聊天兼容接口。

```powershell
# 只显示 Key 是否填写，不显示 Key 内容
python -m pianchang.video_agent config

# 不联网、不生成视频、无需 Key：检查 S001 编译后的完整请求
python -m pianchang.video_agent generate --project examples/example.json --shot S001 --dry-run

# 使用其他环境文件时，--env-file 放在子命令之前
python -m pianchang.video_agent --env-file .env.example config
```

文生视频适配 `wan2.6-t2v`、`wan2.7-t2v`、`wan2.7-t2v-2026-06-12`。带锁定参考的镜头支持 `wan2.7-r2v`、`wan2.7-r2v-2026-06-12`。2.6 文生视频使用 `size`，2.7 使用 `resolution` 与 `ratio`。当前以图片和可选音色为参考，生成时长为 2～15 秒整数。

### 真实生成与恢复

填写 Key 后，下面命令会向百炼提交生成任务，并按模型实际调用规则计费：

```powershell
# 从工程规格生成一个镜头：校验整份工程，再编译 S001 的设定和状态
python -m pianchang.video_agent generate --project examples/example.json --shot S001

# 直接使用提示词生成 5 秒视频
python -m pianchang.video_agent generate --prompt "生成单镜头视频。夜晚的旧办公室，一名穿米色风衣的成年女子缓缓拿出铜钥匙。" --duration 5

# 只提交并保存任务 ID，不等待云端完成
python -m pianchang.video_agent generate --project examples/example.json --shot S002 --no-wait
```

工程模式优先使用镜头的 `duration_s`：示例 S001 为 6 秒，S002、S003 各 8 秒；直接提示词模式使用 `.env` 中的默认时长。`--duration` 可显式覆盖生成时长，清单会记录它与目标时长是否不同，不会自动剪辑补齐。

命令先输出任务 ID 和 `job.json` 路径，默认等待完成并下载 `video.mp4`。每个任务目录位于 `outputs/videos/` 下。将下面的 `<hash>` 替换为命令实际返回的目录名：

```powershell
# 只查询一次状态
python -m pianchang.video_agent status "outputs/videos/<hash>/job.json"

# 中断、等待超时或下载失败后，继续同一个任务并下载
python -m pianchang.video_agent resume "outputs/videos/<hash>/job.json"

# 明确生成新的候选，会发起新的计费请求
python -m pianchang.video_agent generate --project examples/example.json --shot S001 --new
```

相同请求及输入元数据默认复用原任务；等待超时、退出进程或下载失败不会自动再次提交生成请求。`--new` 用于明确要求新候选。若提交请求出现网络异常而没有收到任务 ID，清单会保留 `SUBMISSION_UNKNOWN`，再次运行会阻止重复提交；先到百炼核对任务记录，确认需要重做后再使用 `--new`。云端 `FAILED`、`CANCELED`、`UNKNOWN` 状态会停止等待并保留错误证据。

去重范围包含完整工程哈希与工程文件路径：改动工程内容或移动工程文件后，再执行 `generate` 会建立新任务。继续一个已提交的任务时，请使用其 `job.json` 执行 `resume`。

任务 ID 与视频下载链接的有效期为 24 小时，应及时下载。本地默认每 10 秒查询一次、最多等待 900 秒，单次网络请求超时 60 秒，可通过 `.env` 对应配置调整。`Ctrl+C` 只停止本地等待，不取消云端任务。

`job.json` 保存请求内容与哈希、任务 ID、模型与 Endpoint、工程哈希、镜头输入版本、状态、供应商响应及用量；下载后还记录本地路径、字节数与 SHA-256。未取得的用量和费用保持未知，`cost` 为 `null`，不把估算值当成账单。Key 不写入任务清单。`media_generated: true` 仅表示云端返回生成成功；`local_video_path` 才表示已下载到本地。

可选参数包括 `--negative-prompt`、`--seed`、`--audio-url` 和 `--prompt-file`。`--audio-url` 接受模型可访问的音频 URL；不传时由模型自动生成配音或音效，本版没有普通文生视频模型的静音开关。固定 seed 不保证人物身份或结果完全复现。

视频命令退出码：0 表示本次操作成功（`--no-wait` 不代表视频已完成），1 表示配置、模型或任务错误，130 表示用户中断；命令行参数用法错误由 argparse 返回 2。

### 供 agent 直接调用

```python
from pianchang.bailian_video import load_config
from pianchang.video_agent import generate_video

result = generate_video(
    load_config(),
    "生成单镜头视频。夜晚的旧办公室，一名成年女子缓缓拿出铜钥匙。",
    duration=5,
)
print(result["local_video_path"])
```

`compile_shot(project, "S001")` 可将已校验的工程镜头编译为 `prompt`、`duration` 和 `metadata`，再传给 `generate_video`。需要自行调度时可用 `wait=False` 只提交，随后调用 `VideoRunner(config).resume(manifest_path)`。目前编译的是文字条件，不会把 `references` 槽位自动当作视觉约束传给模型。

2026-11-04 已使用本地 `.env` 的 Key 完成北京地域真实调用：`wan2.6-t2v` 提交、轮询、下载均成功。验证样片来自 S001，显式使用 `--duration 5`，原工程目标仍为 6 秒。MP4 视频轨为 5 秒、720×1280，含音频轨，文件大小 2,924,230 字节；这只验收调用与文件完整性，未验收视觉、动作或音频质量。

运行生成命令后，在本机任务目录查看 `video.mp4` 和 `job.json`。在工作台查看候选片段的方式见[镜头预览截图](images/lake-video.png)。

这些本地产物在 `outputs/` 中，已被 `.gitignore` 排除。后续普通 S001 生成命令使用 6 秒，与本次 5 秒测试不是同一请求，会产生新任务。

## 为什么从这里开始

Coding agent 有代码仓库、明确的文件和对象、工具调用结果，以及可运行的检查。短剧 agent 也需要一个能持续读取、修改和验证的工程。只保存对话和提示词，会使“哪个角色、哪个版本、此时是什么状态、哪段成片因此需要复查”没有可靠答案。

核心对应关系是：创作简报对应产品目标；剧本与分镜规格对应需求和实现约束；角色、场景、服化道与声音资产对应领域对象和依赖资源；制作工程对应仓库；生成、观察、剪辑工具对应开发工具；质检报告对应检查反馈；镜头修复与复查对应修改和回归验证。

这是工程设计类比，并不意味着可以复制代码测试的确定性。视频的视觉、声音、动作和叙事评价存在误判，必须保存证据、评估覆盖范围与不确定性。

## 最小实验

《最后一把钥匙》只是为开发验证构造的示例，不代表已确定的创作方向。

两名成年角色、一个办公室、一把钥匙、三个镜头，计划剪辑时长共 22 秒。当前百炼适配器可分别请求这三个镜头的 6、8、8 秒视频，尚未自动拼接成片；其他工程的时长仍需满足所选模型的限制。

| 镜头 | 动作 | 开始状态 | 结束状态 |
| --- | --- | --- | --- |
| S001 | 林岚拿出钥匙 | 林岚持有 | 林岚持有 |
| S002 | 林岚把钥匙交给陈默 | 林岚持有 | 陈默持有 |
| S003 | 陈默举起钥匙 | 陈默持有 | 陈默持有 |

陈默拿到钥匙并不意味着他已经知道钥匙的来源。房门位于房间西墙，也不意味着换机位后房门仍出现在画面同一侧。我们分别记录物理状态、角色知识和场景世界坐标。

## 运行

从仓库根目录执行：

```bash
python3 -m pianchang.production validate examples/example.json
python3 -m pianchang.production impact examples/example.json --changed char:lin
python3 -m unittest discover -s tests -v
```

故意损坏的示例把 S003 的钥匙持有人改回林岚：

```bash
python3 -m pianchang.production validate examples/broken_example.json
```

这条命令应返回退出码 1，并出现 `CONTINUITY`。其他退出码：0 表示本次检查通过；2 表示输入文件或命令使用错误。

## 这一版的数据模型

| 字段 | 作用 | 解决的问题 |
| --- | --- | --- |
| `entities` | 角色、场景、道具、服装的稳定 ID、版本、规范描述及参考资产槽位 | 同一对象换名字后被误当成新对象；镜头引用过期版本 |
| `facts` | 事实初始值、允许值、能否随剧情改变 | 区分固定设定和剧情状态 |
| `events` | 有前置条件、原因和状态变更的剧情事件 | 道具易主、秘密揭示等变化有明确来源 |
| `shots` | 镜头目标、前置状态、事件、预期结束状态、依赖版本 | 把文字要求变成可执行检查 |
| `continuity_from` | 该镜头依赖的先前镜头 | 修改输入后定位直接和后续复查范围 |
| `state_trace` | 检查时计算出的逐镜头声明状态轨迹 | 定位哪一个镜头首次发生矛盾 |

`canonical` 保存文字设定，机器检查的事实登记在 `facts`。旧实体的 `references` 属于声明。用于制作的具体版本绑定在镜头的 `required_refs`，候选的实际提交证据保存在生成任务和 Trace 中；人物与声音的一致性仍需媒体观察。

镜头中的 `requires` 和 `expected_after` 是部分断言；未改变的事实从前态继承。有效事件在临时状态上执行，镜头检查失败时不提交其变更。这里的“提交”只指检查器在内存里接受声明状态，不表示该事件已在视频中正确发生，更不修改原始项目文件。

当前镜头数组必须按故事发生顺序排列。倒叙、多条时间线、跨集状态快照、隐式自然语言事实和并行写入冲突尚未实现。

## 复现检查结果

Python 测试覆盖制作规格、文本与视频请求、媒体工具、视觉观察协议和验收判定。测试使用离线模拟与本地媒体，运行 `python -m unittest discover -s tests -v` 可查看当前版本的结果。导航测试另用 `node tests/ui/test_navigation_ui.cjs` 运行。

下面的命令在本地生成三份 JSON 报告。正常示例应通过规格检查，错误示例应报告 S003 的钥匙持有人冲突并以状态码 1 退出。影响报告列出林岚设定变化涉及的镜头。

```powershell
New-Item -ItemType Directory -Force outputs | Out-Null
python -m pianchang.production validate examples/example.json | Set-Content -Encoding utf8 outputs/validation-report.json
python -m pianchang.production validate examples/broken_example.json | Set-Content -Encoding utf8 outputs/broken-report.json
python -m pianchang.production impact examples/example.json --changed char:lin | Set-Content -Encoding utf8 outputs/impact-report.json
```

影响报告只给出保守复查范围，**不会自动判定所有下游镜头都要重生成**。后续需区分文字、视觉参考、状态和剪辑等依赖类型，并根据实际差异决定修复范围。

## 后续实现顺序

以下是完整路线，不能视为全部已经具备的功能。第一步已有可运行原型；第二步已有创意到短篇剧本、制作规格、来源与覆盖检查的编译循环；第四步已有文字镜头编译、百炼文生视频调用与持久化任务清单。长剧分块、参考媒体条件、完整成本追踪和其他步骤仍需逐步实现并用真实样片验收。

| 步骤 | 具体产物 | 为什么做、涉及哪个点 | 主要解决什么 | 如何验收、仍有什么边界 |
| --- | --- | --- | --- | --- |
| 1. 制作工程与状态模型 | 本包中的对象、事实、事件、镜头、版本、检查器 | 先给 agent 一个可观察和修改的环境 | 设定引用和显式状态连续性 | 矛盾能定位到镜头和事实；不证明画面一致 |
| 2. 剧本编译与上下文构建 | 从创意简报到全剧大纲、分集、场次、事件与镜头规格；事实来源链接 | 将长文本转成可查询的结构，保留创作意图与因果链 | 长剧本的时间线、人物知识、伏笔、道具归属及遗漏 | 所有关键事件有覆盖；固定评测故事中的矛盾与缺漏可识别；抽取和创作仍可能出错 |
| 3. 参考资产建立与版本锁定 | 角色多角度参考、服装与妆发版本、场景平面关系和机位参考、道具与声音参考 | 给每个镜头同一套可消费的身份和空间约束 | 人物外观、场景、服化道、声音的一致性基础 | 同一角色跨角度和同一场景正反打的小样通过审看；文字描述本身不保证模型遵守 |
| 4. 镜头编译与模型适配 | 由镜头规格构造上下文、参考图、起止帧、声音计划；异步任务和资产清单 | 将“要什么”转成所选模型实际支持的输入；跟踪生成成本和状态 | 生成条件丢失、模型能力差异、任务超时与重复计费风险 | 不支持的能力明确报错或改用受控替代方案；保存任务 ID、版本、输入哈希、成本和候选；seed 不代表跨镜头身份锁 |
| 5. 媒体观察与分层质检 | 带时间戳的帧、短片、音频观察及问题清单 | agent 必须看到输出；一次完整播放和关键动作片段都很重要 | 变脸、场景漂移、动作断裂、台词错误、口型与音画不同步 | 注入已知缺陷，测检出与误报；每条问题有证据和不确定性；稀疏抽帧可能漏掉短暂缺陷 |
| 6. 有界修复闭环 | 读取状态、选工具、生成候选、评估、局部修复、回归检查 | 将质检反馈转成动作；保持验收标准稳定 | 自动发现并修复镜头缺陷，控制返工和成本 | 能修复指定错误并保住已通过镜头；设置重试、费用、时间上限和无法判断的退出状态 |
| 7. 成片构建 | 编辑时间线、对白、环境声、音乐、字幕、混音和导出 | 单镜头通过后仍需验证整段观看体验 | 节奏、镜头衔接、声音连续性、字幕与最终文件问题 | 全片播放、音轨/字幕/时长检查与整体审看；节奏和表演仍需人工判断 |
| 8. 扩展到完整剧集 | 分集状态快照、故事时间与播放时间、版本依赖图、断点恢复 | 规模扩大后跨集引用、修改传播和任务恢复成为独立问题 | 跨集叙事与资产一致性、长任务失忆、局部改稿引起的连锁变化 | 第 N 集的关键事实可追溯至来源；修改前集后正确复查依赖，不全量重做；仍需真实多集回归样片 |

每一阶段沿用同一验收样例，再逐渐增加换装、换场、反打、日夜变化、道具损坏、长间隔回场和跨集秘密揭示。不能用增加镜头数替代验证复杂度。

## 计划中的 agent loop

先用一个决策循环和一组清晰的工具接口即可。任务调度、文件验证、预算检查和合成渲染由普通程序执行；LLM 负责需要推理的拆解和修复选择。是否拆成多个 agent 应由之后的实际并发和协调需求决定。

```text
读取当前目标、受影响镜头及证据
构建最小但完整的上下文：固定设定 + 当前故事状态 + 必要前后事件 + 媒体参考
选择下一步工具动作
执行工具，保存任务、候选、输入版本和真实输出
运行规格检查、媒体质检及相邻镜头回归
通过：保存候选选择和证据，继续下一任务
失败且有预算：定位原因，修复最小必要部分
无法判断或预算耗尽：保留已有成果和待处理问题，结束当前循环
```

视频制作统一使用当前故事的 Pi 持久会话。确认拍摄计划会把授权范围和执行限制冻结到制作委托，并把确认消息加入同一个视频 Agent 会话。后续补充要求、答疑、继续制作和修复都从这个会话进入。视频阶段的“执行限制”只读展示当前委托的额度、已预留量及剩余额度；不会另建任务目标，也不能重置恢复次数。

旧“高级执行设置 / Agent 执行”表单已退役；旧 `/api/projects/<id>/agent` 启动接口返回 HTTP 410，并指向当前视频会话。历史任务、报告、停止操作和 Trace 继续可查。旧书签跳转到视频阶段的对话。当前委托恢复会核对原委托 ID、剧本与规格版本，沿用原检查点；已完成或新版委托不会回退到更早的失败任务。

“停止后续动作”在当前调用返回后停止，不撤销已提交的云端任务。只读答疑完成与视频交付分别记录；所有镜头及衔接必过项通过后才开放人工预览。历史底层循环协议见 [AGENT_LOOP.md](AGENT_LOOP.md)，当前会话接入见 [PI_INTEGRATION.md](PI_INTEGRATION.md)，模型、工具和验收证据统一写入 [Trace 系统](TRACE.md)。

验收结果至少要区分 `pass`、`fail`、`unknown`。模型自己的“看起来不错”不能作为唯一证据。硬规则和审美偏好分开；不能通过删除失败项或降低门槛伪造修复成功。

## 如何理解“一致性解决到哪一步”

| 问题 | 本版已做 | 仍需实现 |
| --- | --- | --- |
| 人物设定一致 | 稳定 ID、版本和显式状态规则 | 自然语言语义检查与多集角色发展验证 |
| 人物视觉一致 | 仅预留参考资产字段 | 参考资产制作、条件生成、跨角度与时序观察、修复 |
| 场景一致 | 显式世界坐标事实检查 | 多机位空间参考、生成控制与画面几何关系判断 |
| 服化道一致 | 示例覆盖服装引用和道具归属 | 服装、妆发、材质、破损和持物动作的画面验证 |
| 长剧本一致 | 短篇剧本编译、来源引用、节拍覆盖、顺序事件与角色知识字段、按镜头查询 | 独立语义审核、长剧分块编译、倒叙、多集真实验收与因果回归 |
| 镜头内动作与时间连续性 | 未实现 | 观察短片和音频、时序指标与人工复核 |
| 声音与口型 | 未实现 | 固定声音条件、对白核对、时间对齐和口型检查 |
| 返工范围 | 显式依赖的保守复查列表 | 媒体级依赖、内容哈希、任务缓存、按证据局部重建 |

## 参考依据

2026-11-03 查阅。以下只支持通用机制与可用工具方向；本包架构和实现是设计选择，不是来源所证明的完整短剧方案。

- OpenAI, [Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/)：模型、工具、结果反馈及上下文管理的循环。
- Google Cloud, [Generate videos from references](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/video/generate-videos-from-references)：参考媒体可成为生成条件，具体支持范围依模型而异。此文作为参考媒体方向的依据，本包未接入该供应商或执行其示例代码。
- VBench 作者仓库，[Vchitect/VBench](https://github.com/Vchitect/VBench)：主体一致性、背景一致性、运动平滑等可以分别评估。这些维度不能直接充当整部剧的剧情或角色身份验收。

2026-11-04 核查的百炼接入依据：

- 阿里云，[万相文生视频 API（2.1–2.6）](https://help.aliyun.com/zh/model-studio/legacy-wan-text-to-video-api-reference)：默认 `wan2.6-t2v` 的参数、异步提交与任务查询、地域 Endpoint、下载链接有效期。
- 阿里云，[万相 2.7 文生视频 API](https://help.aliyun.com/zh/model-studio/text-to-video-api-reference)：2.7 的 `resolution` / `ratio` 协议及通过提示词控制镜头结构。
- 阿里云，[结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)、[文本兼容 API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)：文本编译器使用 Qwen 非思考模式和 JSON Object 输出，应用层仍自行校验业务结构与来源。


## 首页读取性能

侧栏列出已编译故事时，一并读取工程文件中的简介、剧本对象、设定和分镜。点击故事即可浏览这些内容，
选择人物或场景能看到它关联的镜头。对应工程的制作详情在后台读取，读完后保留选中的对象；
读取失败可以重试。这里的打开耗时从侧栏点击算到该故事的实际内容可见，与下方整页启动、
素材和会话全部就绪的时间分别测量。

首页通过 `/api/workspace` 一次读取当前工程、资产、该故事的制作动态和两个 Pi 会话。
现有工程在服务启动时建立只读展示快照；输入未变时复用，并在 Trace 中链接原读取上下文。
工程/资产库、任务状态、参考或证据文件、当前故事的会话变化均会使快照失效。
预览已开放的结果不进入此缓存，媒体播放和所有制作、修改接口仍执行原门禁与真实校验。

静态 CSS/JS 合并为两个请求。Trace 连接在服务生命周期内复用；纯读取校验事件在响应前
同步提交，不持有全局状态锁或 SQLite 写锁来扫描媒体。模型调用和持久修改仍逐次先落盘。
启动恢复扫描在锁外读取文件，提交任务前重新核对取消、当前委托和会话占用。

`python -m tools.verify_startup --project 887c283c256e0fca --repeat 3` 测量真实首屏资源及完整快照接口，
记录响应哈希、服务端 Trace 和耗时。浏览器的 `data-startup-sidebar-ms`、`data-startup-ready-ms`
从页面导航开始计时；完整就绪要求真实工程、制作动态和 Agent 对话已读取，不能用空壳代替。
这些验证不调用付费生成接口。
