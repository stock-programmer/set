"""Standalone SDK demonstration: python -m trace_system.example (no model calls)."""
import json

from . import event, get_tracer, traced


@traced("example.count_beats", kind="validation")
def count_beats(beats, expected):
    actual = len(beats)
    result = {"path": "episodes[0].scenes[0].beats", "expected": expected, "actual": actual,
              "passed": actual == expected, "source": "offline SDK example; not a historical model call"}
    event("validation.result", result, kind="validation")
    return result


def main():
    tracer = get_tracer()
    with tracer.trace("独立 Trace SDK 示例", kind="demo", attributes={"offline": True, "not_historical_replay": True}) as root:
        result = count_beats(["认出朋友", "朋友回避"], expected=3)
        root.set_output(result)
        root.set_status("ERROR" if not result["passed"] else "OK", "Example intentionally demonstrates a count mismatch")
    print(json.dumps({"trace_id": root.trace_id, "store": str(tracer.directory), "integrity": tracer.verify(root.trace_id)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
