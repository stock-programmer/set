# 独立 Trace 系统：架构与接入

这是一套统一的本地记录系统。它独立于某次请求、任务目录、模型和短剧业务；
网页只是其中一个查看入口，SDK 和命令行也可以单独使用。

- **查看一次任务**：打开工作台的「Trace 调试台」，或点击任务卡上的「查看 Trace」。
- **看实际提示词**：选择 `bailian.text.complete_json`，展开「此调用的事件」→
  `http.request` → `body` → `messages`；这里是实际发送的 system/user 上下文。
- **看模型原始返回**：同一调用下展开 `http.response` → `body_text`。
  `body` 是解析后的响应；请求头 ID、响应体 ID、用量和截断标记分别保存。
  所有展示内容都已先脱敏，不保留认证凭证明文。
- **看数量为什么不符**：选择 `story.validate_counts`，查看输出中的
  `path / expected / actual / status`。更后面的制作规格问题在
  `story.validate_compilation` 输出及 `agent.decision` 事件中。
- **交给后续程序使用**：安装本项目的 SDK 包，设置相同的 `AGENT_TRACE_DIR`，
  使用下方 `trace / traced / event` 接口；新增入口同时登记 `contracts/trace_contract.json`。
  新程序需要显式接入，系统不会自动观察任意未接入的代码。

`执行完成` 表示函数调用正常返回；验收是否通过还要看输出报告。
`未结束` 表示没有结束事件，不能直接认定仍在运行。旧任务不补造历史 Trace。

## Shared agent trace system

This system is independent of any creative request, task directory, Web page or
model provider. `trace_system/` uses Python's standard library and imports no
drama application code. Existing tools and new programs write to the same store.
The Web workbench is one viewer; the SDK and CLI work without the Web server.

## Architecture

```text
Web requests / CLI programs / background jobs
                 |
        trace_system public SDK
       trace -> spans -> ordered events
                 |
       centralized redaction + serialization
                 |
       SQLite WAL, append-only event API
       per-trace sequence + integrity hashes
       content-addressed large JSON payloads
                 |
       query / verify / export
            |             |
       Web Trace UI    independent CLI
```

The shared store defaults to `outputs/traces/`. Set `AGENT_TRACE_DIR` before
starting any process to use a different shared directory. Keep this directory
separate from any single job's outputs. SQLite supports concurrent local processes;
this implementation is not a distributed telemetry collector or network filesystem
deployment. The package has no runtime third-party dependencies.

Each HTTP request has its own trace. A submitted background job gets a durable
task trace with a `submitted_by` link to the request span. Its context is persisted
before queue execution; the worker restores that context so every nested tool and
model call belongs to the correct task. Reused jobs keep their original task trace
and the new request records a reuse link. HTTP `X-Trace-Id` identifies the request;
the returned job's `trace_id` identifies the background task.

Events are appended instead of replacing previous states. An event contains trace,
span and parent IDs, UTC timestamp, global `seq`, per-trace `trace_seq`, name,
kind, data and integrity-chain fields. Event pagination uses **trace_seq** as its
opaque `after` cursor. Span inputs, outputs, source-file/loaded-code hashes,
process identity, timing and exceptions are available for debugging.

## Recorded boundaries

- HTTP requests: method, target, safe headers, JSON body, status and response.
  File responses record path/range/size rather than duplicating media bytes.
  Trace-viewer responses record query identity, digest and explicit omission
  reason so viewing traces does not recursively capture its own trace history.
- Agent: job queued/running/result/error/interruption, cache reuse, compile
  iterations, repair decisions, artifact writes and chosen candidate operations.
- Models: actual model/endpoint/request payload, safe request headers, captured
  response text and parsed envelope, HTTP status, header request ID and body IDs
  separately, finish reason, usage and errors. Captured wire hashes refer to
  pre-redaction bytes; stored payload hashes refer to sanitized JSON. They are
  different checksums with different meanings.
- Media tools: FFmpeg/ffprobe executable/arguments, return code, stdout/stderr,
  timeout/error, source/result paths, hashes and inspection/export results.
- Validation: normalized inputs, complete check outputs, and explicit
  path/expected/actual/status for draft episode/scene/beat counts. A local failure
  remains visible even if another later span succeeds.

Binary media and image data URIs are represented by hashes and descriptors, with
the source frame/file references recorded separately. They are not media backups;
retain the production assets if the original pixels or audio must be replayed.
Provider capture limits remain explicit: a response exceeding the client's limit
is marked truncated and rejected, not represented as a complete response.

`OK` means the observed operation completed. It does not mean a video passed
creative acceptance: the acceptance report retains PASS/FAIL/UNKNOWN separately.
An unended span is UNKNOWN with `end_recorded=false`; a missing end does not prove
the process is still running. On workbench restart, the exclusive process lock
provides evidence that prior unfinished Web jobs were interrupted; the system
appends a new UNKNOWN end and never invents the original interruption time.

## Using the viewer

In the extracted project folder, double-click `启动片场.command` on macOS or `启动片场.cmd` on Windows.
The launcher opens the workbench in your browser and prints its local address. It tries ports 8765 through 8774 by default.
To select a port from a terminal, run `./启动片场.command --port 8766` on macOS or `powershell -ExecutionPolicy Bypass -File .\tools\start_web.ps1 -Port 8766` on Windows.
To view local traces without model credentials, press Enter at the API Key prompt. The launcher skips the Agent dependency download.
Use **Trace 调试台** in the sidebar, or **查看 Trace** on a new task card. The
default list shows task traces; switch to HTTP or all traces to inspect requests
and standalone tool invocations. Filter by status/name/ID, inspect the call tree,
open model input/output events, and expand large payloads on demand. The viewer
can load later event pages, verify integrity and download a full JSON export.
Old jobs without traces are labeled as historical, uninstrumented tasks. Their
old run/call files are preserved; no historical steps are manufactured.

CLI examples (PowerShell, from the repository):

```powershell
python -m trace_system.example
python -m trace_system list --kind task
python -m trace_system show TRACE_ID
python -m trace_system verify TRACE_ID
python -m trace_system export TRACE_ID --output outputs/trace-export.json
python -m tools.check_trace_contract
```

## New program contract

```python
from trace_system import get_tracer, traced, event

@traced("my_program.check", kind="tool")
def check(value):
    result = {
        "path": "count",
        "expected": 3,
        "actual": value,
        "status": "PASS" if value == 3 else "FAIL",
    }
    event("validation.result", result, kind="validation")
    return result

tracer = get_tracer()  # same AGENT_TRACE_DIR as all other programs
with tracer.trace("my_program.task", inputs={"count": 2}) as task:
    task.set_output(check(2))
```

For programs outside this repository, install the SDK package or put the repository
on `PYTHONPATH`, and set the same `AGENT_TRACE_DIR`. The SDK does not automatically
observe arbitrary code: new entry points and side-effect boundaries must use it.
The [development contract](../AGENTS.md) makes this mandatory; `contracts/trace_contract.json` and its checker prevent
registered production boundaries from silently losing instrumentation. New tools
must update the contract and add an integration test exercising their real boundary.

Use `@traced(..., kind="cli")` on a CLI main function. For work that continues the
same span across a thread or process boundary, capture `current_context()` before
dispatch and enter `use_context(context, tracer=tracer)` in the worker. For a
queued job, persist its detached task context with a link to the submitting
request before dispatch. Restore that task context when the worker starts.
Keep the SDK independent of story, media, Web and provider modules.

Use `tracer.register_secret(...)` before recording inputs. Keep credentials in
authorization headers, cookies and signed URLs out of plaintext records. Use the
SDK's binary summaries for media. Exclude hidden model reasoning. A trace write
failure stops subsequent business operations, including new billable requests.
On restart, preserve an uncertain request and reconcile it with available receipts.

Integrity verification detects inconsistent events or payloads. It is not a signed
receipt from a provider and does not prevent an administrator rewriting the whole
database and its hashes. Traces contain creative inputs and provider responses;
the viewer is local-only and no external telemetry service is enabled.

## Autonomous video repair and recovery

`pianchang/video_autonomy.py` runs inside the video commission executor. Its durable queue
separates actionable failures, specific evidence blockers, and exhausted repair
limits. A blocker on one shot cannot satisfy the stop guard while another shot
still has actionable work. Limits stop spending; they never make acceptance pass.
Strategy plans bind the actual candidate and report hashes and retain all earlier
instructions. New submissions must change strategy, and each completed candidate
receives content and adjacent-cut review. Regressing candidates and their failed
reports remain in history while the selected better candidate is revalidated.
Selection also protects passed mandatory endpoint states and adjacent cuts. A
historical cut PASS is comparable only if its other endpoint still matches the
current selected media; stale combinations cannot justify a rollback. Recovery
can reconcile a selected candidate that regressed a previously passed cut,
without resetting the generation budget or commissioning another video.
When both neighbors changed, the executor also searches actual older candidate
combinations whose cuts were already inspected. It preserves current mandatory
passes and requirement coverage, switches only for more known passed checks,
and revalidates the resulting selection. Unreviewed cuts are never invented as
PASS. Large pools use bounded adjacent-pair search. This work is considered
before further generation or stopping at a paid-call limit.

Cross-agent clarification requests publish versioned, report-bound evidence and
are serviced by the project, including after restart. A specification conflict
must name the original script quote and specific state field. Recording that
blocker neither changes the original verdict nor unlocks preview.

The app persists the original commission and detached trace context before
dispatch. Agent runs have an exclusive execution lock, write pending tool calls
and repair reservations before side effects, and archive their prior records
before recovery. Recovery preserves the same commission, script/specification
versions, counters and provider task IDs. It resumes polling/downloading an
identified task, never resubmits an uncertain paid operation. Missing task IDs
remain explicit blockers; other actionable shots can continue. Native review
receipts are hash-checked, and interrupted or missing content reviews are
reconciled before acceptance. A receipt whose paid result is uncertain must not
be replayed merely to recover logs. Trace persistence errors stop side effects.

Deterministic content evidence validation records its complete attempted
path/expected/actual/status list in one shared SDK event before returning. The
batch changes storage cost, not the acceptance conditions or evidence schema.
Preview export, human confirmation and completion each independently require all
mandatory checks to pass and to bind the currently selected media.

An explicit `字幕浮现：…`/`字幕显示：…` script requirement also creates a mandatory
`script_subtitle` check. A separate full-video observation transcribes the actual
on-screen text; deterministic comparison preserves the raw model verdict but
rejects missing or different text, including a model claiming text is somebody
else's postproduction work. No transcription or inadequate temporal evidence
remains specifically inconclusive. This adds a missing literal-script check;
it does not change the frozen story or waive any earlier condition. A subtitle
failure can still use the independently limited local editing tool after paid
video-generation limits are reached. Identical failed local edits are rejected
before another media operation. Restart reconciliation also detects previously
published reports that omitted the required subtitle check.
Local edits must receive the complete literal subtitle before rendering. Its
receipt is bound to output bytes and checked alongside the observed text, so a
model copying the expected caption cannot hide different actual rendering input.

Automatic recovery retains its lifetime count and counts repeated failures at
the same durable execution checkpoint separately. Three recoveries without
progress at the same checkpoint stop; a later interruption after real progress
can resume the same commission without resetting any generation/edit budget.

When the whole-video subtitle observation has an invalid response or execution
error, `pianchang/subtitle_audit.py` changes verification strategy. It extracts actual
original-resolution end frames, retains partial extraction attempts, and reads
one timestamped image using a small transcription-only model request. The target
text and story are deliberately absent from that request. Deterministic code
compares received text with the frozen caption; the receipt explicitly labels
image-only input and does not claim continuous-action or audio coverage. A valid
whole-video FAIL is never overridden. Unreadable/mismatching static evidence
does not prove absence in other video intervals. Original failed responses remain
immutable, and frame/model/normalization receipts are separately bound so a new
requirement can reuse an actual transcription without another model call.
