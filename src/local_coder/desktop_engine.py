"""Loopback-only Electron sidecar entry point; readiness reveals no credentials."""
from __future__ import annotations
import json
import os
import signal
import threading
from pathlib import Path
from local_coder.remote import RemoteControlServer


def main():
    token = os.environ.get("LOCAL_CODER_REMOTE_TOKEN")
    project = os.environ.get("LOCAL_CODER_DESKTOP_PROJECT")
    if not token or not project or not Path(project).is_dir():
        raise SystemExit("Desktop engine requires a workspace and an ephemeral access token")
    control = RemoteControlServer(project, token=token)
    server = control.make_http_server("127.0.0.1", 0)
    def stop(_signal, _frame):
        def drain():
            control.drain_jobs()
            server.shutdown()
        threading.Thread(target=drain, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({"port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    finally:
        control.drain_jobs()
        server.server_close()


if __name__ == "__main__":
    main()
