"""Read-only tracing CLI. API keys and application dependencies are not needed."""
import argparse
import json
import sys
from pathlib import Path

from . import TraceError, Tracer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, help="Trace storage directory; defaults to AGENT_TRACE_DIR or outputs/traces")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--kind")
    listing.add_argument("--status", choices=["OK", "ERROR", "UNKNOWN"])
    listing.add_argument("--search")
    showing = sub.add_parser("show")
    showing.add_argument("trace_id")
    showing.add_argument("--after", type=int, default=0)
    showing.add_argument("--limit", type=int, default=1000)
    checking = sub.add_parser("verify")
    checking.add_argument("trace_id")
    exporting = sub.add_parser("export")
    exporting.add_argument("trace_id")
    exporting.add_argument("--output", type=Path, help="New JSON file; existing files are never overwritten")
    artifact = sub.add_parser("artifact")
    artifact.add_argument("sha256")
    args = parser.parse_args(argv)
    try:
        tracer = Tracer(args.directory)
        if args.command == "list":
            result = tracer.list_traces(limit=args.limit, offset=args.offset, kind=args.kind, status=args.status, search=args.search)
        elif args.command == "show":
            result = tracer.get_trace(args.trace_id, after=args.after, limit=args.limit)
        elif args.command == "verify":
            result = tracer.verify(args.trace_id)
        elif args.command == "artifact":
            result = tracer.get_artifact(args.sha256)
        else:
            result = tracer.export_trace(args.trace_id)
            if args.output:
                with args.output.open("x", encoding="utf-8") as output:
                    json.dump(result, output, ensure_ascii=False, indent=2)
                result = {"output": str(args.output.resolve()), "trace_id": args.trace_id, "integrity": result["integrity"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "verify" and not result["ok"] else 0
    except (TraceError, KeyError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
