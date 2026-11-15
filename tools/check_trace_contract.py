"""Offline guard against removing tracing from registered production boundaries."""
import importlib
import json
from pathlib import Path


def check_contract(path=None):
    path = Path(path or Path(__file__).resolve().parents[1] / "contracts" / "trace_contract.json")
    contract = json.loads(path.read_text(encoding="utf-8"))
    issues = []
    entries = contract["decorated_callables"]
    for name in entries:
        try:
            parts=name.split(".")
            for boundary in range(len(parts),0,-1):
                try:
                    function=importlib.import_module(".".join(parts[:boundary]))
                    attributes=parts[boundary:]
                    break
                except ModuleNotFoundError:
                    if boundary==1:raise
            for attribute in attributes:
                function = getattr(function, attribute)
            if not callable(function) or not getattr(function, "__trace_instrumented__", False):
                issues.append({"entry": name, "reason": "missing SDK instrumentation"})
        except (ImportError, AttributeError) as exc:
            issues.append({"entry": name, "reason": str(exc)})
    return {"ok": not issues, "registered_boundaries": len(entries), "issues": issues,
            "manual_integrations": contract["manual_integrations"],
            "scope": "Declared entry points only; new code must explicitly register and instrument its boundaries."}


if __name__ == "__main__":
    result = check_contract()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
