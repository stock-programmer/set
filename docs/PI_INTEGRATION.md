# Pi integration

The workbench uses the real Pi SDK (`@earendil-works/pi-coding-agent`, pinned in
`pi-runtime/package-lock.json`). Production Bailian script/video decisions use
Pi native tool calling. Existing injected historical JSON-action unit fixtures
have an explicitly labelled offline compatibility path; they do not prove a live
Pi exchange.

## Ownership

- `pi-runtime/src/main.mjs`: Pi AgentSession, SessionManager, queued steering,
  automatic context compaction and native tool scheduling. This is an ESM host
  for the SDK, with no separate framework implementation or frontend build.
  New and resumed sessions use the role prompt and tools supplied by the host.
  Pi's automatic discovery of contributor instruction files is disabled
  (`noContextFiles`). Story history and collaboration receipts retain their session scope.
- `pi_bridge/runtime.py`: process lifecycle, exclusive session execution, frozen
  generation scope, budget reservations, completion checks, and compatibility
  projections for existing recovery tools. It does not choose model actions.
- `pi_bridge/model_gateway.py`: private loopback model proxy. Every Pi request,
  including compaction, passes through the Python Trace SDK before the actual
  upstream request. Verified TLS and disabled automatic provider retries are
  explicit. Actual upstream responses are recorded before normalization to Pi.
- `pi_bridge/repository.py`: story/role identity and durable inbox state in
  SQLite. Pi JSONL remains authoritative for conversation history. This database
  is business delivery state, never an alternative tracing system.
- `pi_bridge/service.py`: API-facing conversation operations, detached task
  contexts, delivery to running sessions, and existing collaboration bus receipts.
- `pi_bridge/script_tools.py`: restore real script/spec artifacts; expose draft,
  publication, patching and validation tools. Retain original unconfirmed calls.
- `pi_bridge/requirements.py`: checks explicit count clauses with their source
  quotations, including total shot count, at the resume/publication boundary.
- `pi_bridge/context.py`: bounded current-state observations with full-result
  hashes, concrete failed checks and original review/Trace references. Complete
  receipts remain in Trace; agents can request detailed individual reviews.
- `pi_bridge/verify.py`: read-only audit of actual continuation, session identity,
  pinned collaboration, retained uncertain operations and Trace integrity.
- `web/agent-chat.js` and `web/agent-chat.css`: story-scoped conversations and
  controls, backed by the real APIs and tasks.
- `contracts/pi/bridge.schema.json`: private protocol envelope. Tool schemas are
  exported from the actual Python tools, not independently copied to JavaScript.

`pianchang/script_collab_agent.py`, `pianchang/video_collab_agent.py`, and `pianchang/collab_negotiation.py`
retain their domain workspaces but select the Pi runtime for production model
clients. Existing video acceptance, recovery, reference approval and exports
continue to own their gates. A model reply or a Pi settled event cannot mark
media accepted. Script/spec publication is explicitly a different scope from
video acceptance; missing references remain visible and are handled by the asset
workflow before video submission.

## Runtime data

- `outputs/pi/registry.sqlite3`: identities, inbox delivery and execution links.
- `outputs/pi/sessions/<story_id>/<role>/`: Pi conversation files. Early imported
  sessions may retain their original `outputs/pi/runs/.../sessions/` path; the
  registry points to the exact existing file without rewriting history.
- `outputs/pi/runs/<job_id>/`: execution checkpoints and original failure evidence.
- `outputs/pi/workspaces/<story_id>/`: current script/spec candidates and versions.
- Existing `outputs/collaborations/`, `outputs/web/`, configured media output,
  and `outputs/traces/` retain their roles.

## Recovery

`pianchang/reference_inputs.py` compiles the unchanged `required_refs` into provider inputs.
`identity_first` preserves separate identity images where possible; the alternate
`balanced_boards` changes grouping and panel resolution. FFmpeg creates uncropped
reference boards and verifies decoded panel pixels. A separate vision review
compares the actual sources and boards. Every required comparison must pass.
This is conditioning preparation, never video acceptance. `pi_bridge/input_tools.py`
exposes the same persisted compiler to the ongoing conversation and video worker.
The video stage displays source counts, actual slot counts, boards and review Trace.

Compiler state is under the original commission's `reference-inputs/` directory.
The original script, shot count, required pins and generation budget stay frozen.
External vision reservations consume that commission's model-call allowance;
the worker adds them to its own calls when checking its original limit. Requests
with uncertain outcomes are not replayed. If the real completed vision receipt
is present in Trace, recovery reconciles it without another paid call. A failed
strategy retains its evidence; other shots and the alternate strategy remain
available. An impossible input layout requires a pinned collaboration request,
not silently dropped sources or an unapproved shot rewrite.

Version 2 video-reference receipts distinguish original source requirements from
the effective images actually transmitted. Immutable bundle assets bind complete
source coverage, board pixels, voice identity pairing, and the independent review
receipt. Submission, receipt verification, reverse usage lookup, publication and
acceptance all retain this distinction; no source is reported as directly sent
when it only appears within a derived board. Legacy direct receipts remain valid.

New user messages use stable message IDs. Startup reconciles interrupted delivery
against actual persisted Pi entries. It never resubmits a paid operation merely
to recover a transcript. Script recovery reads published versions and completed
candidates; unresolved historical requests remain unresolved evidence. Video
recovery uses the existing immutable commission, media hashes, operation keys,
provider task IDs, original budgets and acceptance gate. New video authorization
continues to use the workbench's displayed production plan.

Conversation response completion is distinct from production completion:
`responded` ends a consultation turn with `task_complete=false`; it cannot bypass
a production workspace's remaining actionable work or open the preview gate.
Native Pi compaction has its own request purpose and still consumes the recorded
model-call budget. It may run after a valid finish; it cannot start another
production decision. A failed post-finish compaction is reported separately and
retains the original conversation and independently accepted business outcome.
Messages can explicitly select `inspect` mode. That mode removes mutation and
generation tools at the server, propagates to delegated agents, and is retained
in durable delivery state. It does not cancel already authorized background work.

## One video execution entry

The displayed production plan and the ongoing video conversation share one
story-scoped Pi session. Plan confirmation reserves that session before its
worker can start and persists the real approval with the job ID. Native Pi
receives the message ID in its initial prompt; only actual persisted Pi entries
mark a message delivered. An approval rejected before execution is retained as
`not_executed`, never replayed as a new generation instruction.

`pianchang/video_execution.py` resolves the latest commission including completed orders,
reads its immutable limits and original checkpoint, and rejects changed versions.
Conversation continuation must name that commission ID. It uses the existing
workspace and budget under the same Pi execution lock; the old failure trace is
retained. `read_current_commission` is read-only. Inspect mode omits the resume
tool. Terminal production status is saved before queued follow-ups are released.

The workbench Continue button submits the selected commission ID as a structured
user command in the existing video session. After checking the current order and
its frozen inputs, the host hands the command to the production worker. The worker
receives the original checkpoint and remaining budget. Conversation history and
earlier provider errors remain available as dated evidence. In the chat interface,
ordinary questions still use the selected conversation mode and its tool catalog.

If the first Pi decision fails before production planning creates its workspace,
an explicit Continue command may resume that same blocked order after checking
its frozen plan and untouched checkpoint. No pending tools, video reservations,
auxiliary model calls, repair/review records or other production files may be
present. The original model-call count and video budget are retained. Recovery
leaves planning pending for Pi instead of assuming `project.json` already exists;
page reads and automatic recovery do not retry this blocked initial decision.

The former generic `/api/projects/<id>/agent` start route returns HTTP 410
`AGENT_ENTRY_MERGED`. Its UI/bookmarks lead to the video conversation. Historical
reports and cancellation remain readable. Execution limits are read-only,
bound to the accepted plan, and cannot weaken the shared preview gate.

## Live execution feedback

The existing story conversation renders public assistant explanations and model,
tool, cloud-video, media-preparation and acceptance-stage events from the shared
Trace. It follows the original conversation and its detached production checkpoint;
handoff or a returned tool never counts as acceptance. No separate event log or
hidden model reasoning is exposed. The fixed preview gate remains authoritative.

`pi_bridge/live.py` projects allowlisted fields and authorizes Trace sources from
the current session, project and story. `/api/pi/events/stream` uses SSE with a
scoped multi-Trace cursor and `Last-Event-ID`; `/api/pi/events` supplies the same
cursor-based feed when streaming disconnects. Reconnection only reads committed
evidence, never resubmits a conversation or generation request. Heartbeats indicate
connection health, not task progress. Every response frame is traced before writing.

`web/agent-chat.js` deduplicates events, preserves conversation drafts and scroll,
and closes a prior story/role stream. When an already-running server does not yet
advertise `live_events`, the UI uses the existing indexed, paged Trace API and the
already loaded studio snapshot's repair receipt for the exact commission. It
avoids global Trace lists, collaboration scans and unrelated preview validation.
Coarse sequence probes locate a recent event window (up to two pages of 1,000 raw
events), avoiding replay of a long validation history. Older evidence
remains in Trace. This compatibility path permits a frontend refresh without
restarting an in-flight paid operation; SSE activates on the next safe server start.

Pi SDK public text updates are forwarded as they become available. The current
model gateway buffers a non-streaming upstream response, so this integration does
not promise provider token-by-token output. Model start, tool transitions and
long-running media/cloud stages are independent visible events.

Run `python -m unittest tests.test_pi_live -q` for explicitly offline real HTTP/SSE
transport, cursor reconnect, story isolation and no-dispatch checks, and
`node tests/ui/test_pi_chat_ui.cjs` for UI delivery, deduplication and fallback behavior.

## Interruption and production recovery

Pi process shutdown escalates from protocol close to terminate to kill with
bounded waits, always revokes the model gateway, and retains cleanup failures
separately from the original exception. The public execution wrapper also catches
failure to persist the SDK span end. A fail-closed atomic domain checkpoint marks
the run interrupted and retains pending operations, original limits, reservations
and the previous checkpoint. When Trace is unavailable this uses SDK redaction
without pretending to have recorded an event; no tool/model dispatch follows.

The project recovery scheduler reconciles a historical `running` checkpoint only
when its actual owner is terminal and a durable ERROR end exists in its original
Trace. A healthy storage fence is required to requeue a Trace-failed commission.
Recovery retains the same frozen request, Pi session, candidate media and budgets.
A persisted background recovery context permits a 15-second health watchdog to
recover approved interrupted commissions without an external operator. Repeated
stalls at the same checkpoint remain bounded. Cancellation is respected.

An unconfirmed strategy-model request remains unconfirmed, with its real request
event and payload digest retained and its usage reserved. Recovery never repeats
it to reconstruct logs. A new production strategy may use the remaining allowance
and must receive the retired design context. Generation recovery still reconciles
the existing provider task and cannot duplicate an uncertain video POST. Reference
receipt/transport validation uses pure read batches, committed before subsequent
effects; no required check or failed result is removed.

Oversized continuous review input gets a bounded-bitrate transport derivative.
The original candidate and earlier review input remain intact. The derivative
retains the full interval and the existing 10fps review timeline, with exact
timestamp, duration, source/parent/output hash and wire-size checks. Partial
encoding attempts remain evidence and use a new attempt directory. This changes
transport bytes, never the acceptance criteria or the original production video.

The live feed uses the active owner or most recently updated execution, and marks
a stale worker checkpoint interrupted when its owner has stopped. A prior failed
conversation cannot override a newer resumed production job. Tests:
`python -m unittest tests.test_pi_interruption tests.test_trace_storage_session tests.test_pi_runtime -q`.

## Validation

Run `python -m unittest tests.test_pi_runtime tests.test_pi_sessions tests.test_pi_context tests.test_pi_service -v`
for explicitly offline Pi SDK, compaction, durable delivery and collaboration
checks. `python -m unittest tests.test_video_repair.RepairTests.test_native_pi_sdk_repairs_and_respects_the_same_export_gate -v`
exercises native Pi repair and the unchanged export gate with labelled mock
model verdicts and local FFmpeg fixtures. It does not prove real video acceptance.
UI checks are `node tests/ui/test_activity_ui.cjs`, `node tests/ui/test_story_drafts_ui.cjs`, and
`node tests/ui/test_pi_chat_ui.cjs`. Register new production boundaries in
`contracts/trace_contract.json` and run `python -m tools.check_trace_contract`. Do not use live
provider calls as a unit-test dependency.

For a completed live continuation, `python -m pi_bridge.verify --story <story-id>`
audits original script hashes, explicit counts, persisted real Pi identities,
actual collaboration receipts and Trace integrity without submitting any model
or media operation. Its report separates integration verification from video
production acceptance.
