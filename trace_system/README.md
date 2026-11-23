# Independent local trace SDK

This package uses only the Python standard library and does not import the drama
agent. Any Python program can put the repository on `PYTHONPATH`, import this SDK,
and write to the same trace store. `AGENT_TRACE_DIR` selects the store; the default
is `outputs/traces` next to this package's parent directory.

```python
from trace_system import get_tracer, traced, event, use_context

@traced("my_program.transform", kind="tool")
def transform(text):
    event("input.checked", {"characters": len(text)}, kind="validation")
    return text.upper()

tracer = get_tracer()
tracer.register_secret(my_api_key)  # before recording provider text
with tracer.trace("my_program.task", inputs={"text": "hello"}) as task:
    task.set_output(transform("hello"))
```

Use `kind="model"` for a model boundary and record sanitized request/response
events **before sending** and **immediately after receiving**. A `TraceError` is
not silently ignored: failure to persist a pre-call event must stop a billable
call. Do not log Authorization/Cookie headers yourself. Binary media and inline
data URIs become hashes and descriptors; these are not embedded media backups.
Full plain prompts and sanitized JSON/text replies are retained.

Detached queues can call `tracer.start_span(..., new_trace=True)`, persist its
JSON-serializable `.context`, and pass it to another thread/process with
`use_context(context, tracer=tracer)`. Workers end the task with
`tracer.end_span(context, status="OK", output=...)`. First end wins: a repeated
end returns the original event unchanged. Request and task can be separate traces
linked by `links=[{"trace_id": ..., "span_id": ..., "relation": "submitted_by"}]`.
`use_context(None, tracer=tracer)` binds a store without a parent span.

The decorator supports synchronous and async functions, captures named inputs
and output, records exceptions, and marks `__trace_instrumented__=True`. Python
objects such as `self` are represented by type; their attributes/repr are not
dumped. Generator functions are not supported for full-lifetime tracing; put an
explicit span inside the generator. New programs must instrument their own entry
points and boundaries: this SDK cannot automatically observe arbitrary code.

Storage is SQLite in WAL mode, with transactional append-only event inserts and
separate content-addressed sanitized JSON artifacts. Each trace has its own
sequence and hash chain; `seq` additionally provides database-wide ordering.
Queries use IDs, never client-provided file paths. Output exceeds the inline
threshold as `{"$artifact": "sha256", "bytes": n, "media_type": "application/json"}`;
resolve it with `get_artifact` or `resolve`. Event pages expose `next_after` and
`events_has_more`. Exports include all events and recursively referenced artifacts.
If user data itself has exactly the reserved artifact-reference shape, it is
stored as `{"$literal": <sanitized original>, "$literal_reason": "user_artifact_shape"}`.
This wrapper means ordinary business data, not an SDK reference: viewers must
display its `$literal` as JSON without trying to fetch the embedded `$artifact`.
`resolve(wrapper)` returns that original sanitized business object in one step;
do not recursively resolve it as an artifact. Real SDK references retain their
existing wire shape, including those nested in large input/output payloads.
Header name/value pairs, relative signed URLs, sensitive XML elements, and data
URI object keys are also sanitized; an inline-media key becomes a descriptive
string containing its hash, not a non-string JSON key.

```powershell
python -m trace_system.example
python -m trace_system list --kind task
python -m trace_system show TRACE_ID
python -m trace_system verify TRACE_ID
python -m trace_system export TRACE_ID --output trace-export.json
```

Span status is `OK`, `ERROR`, or `UNKNOWN`. A missing end event is `UNKNOWN`, with
`end_recorded=false`; it does not assert a process is still running or dead. A
supervisor that detects an interrupted task can append an explicit `UNKNOWN` end
with its recovery evidence. No past events are invented. Timing is recorded in
UTC, and in-process duration uses a monotonic clock. Detached duration uses wall
clock timestamps. Parent errors and unfinished spans affect the trace summary.

Sensitive dictionary keys, registered secret values, Bearer tokens, sk-prefixed
keys, URL credentials/signatures, JSON strings and inline image data are scrubbed
before persistence. This does not classify every conceivable private business
field; callers remain responsible for deliberate data minimization and registering
nonstandard credentials. The store and exports contain creative prompts and
provider responses and should remain local unless sharing is intended.

`verify` detects event-chain or artifact inconsistency; it is **not** a signed
provider receipt or protection against an administrator rewriting the entire
store and hashes. Source metadata records module/qualname/file and a file hash
at invocation time; this is not bytecode attestation. No provider-side receipt,
immutable remote archive, retention/deletion service, or OTLP collector is claimed.

Long-running servers can own `with tracer.storage_session():` to reuse a bounded
set of SQLite connections. Each normal event still commits with
`synchronous=FULL` before returning. Closing the last owner releases idle
connections, while in-flight connections close when their operations finish.
The SDK still has no dependency on this application's server or media modules.

Writers in the same process and database share a FIFO queue, including separate
`Tracer` instances and read-batch commits. A waiting background task gets its
turn before newer page-refresh writes, instead of repeatedly competing for
SQLite's writer lock. Database reads do not enter this queue. SQLite still
coordinates other processes with its bounded busy timeout; storage failures
remain `TraceError` and never cause a provider operation to be replayed.

`with tracer.read_batch():` is restricted to pure read computations under an
already durable root. It stages redacted observations, preserving their actual
timestamps, and commits the complete ordered batch before the scope returns.
Before acquiring the SQLite writer, the SDK prepares the redacted payloads and
artifact hashes. If a credential was registered in the meantime, it repeats
that preparation with the new credentials. The transaction commits the
artifacts and ordered hash-chain entries together. Ordinary validation
failures retain their evidence; storage failures raise `TraceError` and prevent
a result from being returned. A crash during an unfinished read leaves the
received request's root incomplete. Provider calls, domain mutations, background
dispatch and sending the HTTP response must stay outside this scope. In this
workbench, Pi session creation and all generation/mutation requests retain
immediate tracing. This is not an asynchronous queue for paid-call logs.

When SQLite rejects a read batch's `BEGIN IMMEDIATE` with `SQLITE_BUSY`,
the SDK can attach `read_batch_before_begin` to the owning span's durable
error record. It describes the staged span relationships and operation names.
This evidence requires a normally completed read body, a single trace and at
most 1,024 staged operations. The original batch still fails. A supervisor
can inspect the error and verify its trace before repeating a known read.

On Windows, SDK writers in the same desktop session acquire a kernel mutex
named for the resolved store path after joining their process-local writer
queue. The mutex covers the SQLite transaction and its synchronous commit.
Independent reads continue through WAL. If the owner exits, Windows releases
the mutex and SQLite recovers its transaction. A writer that cannot acquire
the mutex within 30 seconds raises `TraceError` before beginning a write.
