"""Serve existing video candidates without starting a production worker."""
import argparse
import json
import os
import threading
from pathlib import Path

from trace_system import event, get_tracer, traced


class ProgressSource:
    """Read the workbench's atomically replaced job index without taking its process lock."""

    def __init__(self, root, *, state_dir=None, video_root=None, tracer=None):
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir or self.root / "outputs" / "web")
        self.video_root = Path(video_root or self.root / "outputs" / "videos").resolve()
        self.tracer = tracer or get_tracer(os.environ.get("AGENT_TRACE_DIR") or self.root / "outputs" / "traces")
        self._scan_lock = threading.Lock()
        self._version = None
        self._jobs = {}

    @traced("studio.progress.job", kind="tool", capture_output=False)
    def job(self, project_id, job_id):
        state = self.state_dir / "state.json"
        with self._scan_lock:
            stat = state.stat()
            version = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if version != self._version:
                records = json.loads(state.read_bytes())["jobs"]
                self._jobs = {(row["project_id"], row["id"]): row for row in records if row["kind"] == "studio_video"}
                self._version = version
            row = self._jobs[(project_id, job_id)]
        event("studio.progress.job_read", {"project_id": project_id, "job_id": job_id, "status": row["status"],
                                          "order_id": row.get("message_id")})
        return row


@traced("studio.progress.serve", kind="cli", capture_inputs=False)
def main():
    from pianchang.web_server import ROOT, create_server
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--videos-dir", type=Path)
    args = parser.parse_args()
    source = ProgressSource(ROOT, state_dir=args.state_dir, video_root=args.videos_dir)
    with create_server(source, port=args.port, progress_only=True) as server:
        print(f"Video progress: http://127.0.0.1:{server.server_address[1]}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
