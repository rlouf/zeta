from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def qwen_port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8080), timeout=0.25):
            return True
    except OSError:
        return False


def start_qwen_for_pi() -> bool:
    if qwen_port_open():
        return True

    home = Path.home()
    script = home / ".config" / "pi" / "run-qwen36-q8.sh"
    log_dir = home / ".pi" / "agent" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "qwen36-q8.log"

    if not script.exists():
        print(f"pi: missing local Qwen server helper at {script}", file=sys.stderr)
        return False

    print("pi: starting local Qwen3.6-27B Q8 server on 127.0.0.1:8080", file=sys.stderr)
    with log_path.open("ab") as log:
        subprocess.Popen(
            [str(script)],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )

    for _ in range(180):
        if qwen_port_open():
            return True
        time.sleep(1)

    print(f"pi: local Qwen server did not become ready; see {log_path}", file=sys.stderr)
    return False

