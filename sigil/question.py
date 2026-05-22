from __future__ import annotations

import shutil
import subprocess
import sys

from .ansi import MUTED, RESET
from .server import start_qwen_for_pi
from .state import append_event


def ask(question: str, stream_filter: str) -> int:
    if not start_qwen_for_pi():
        return 1

    append_event({"type": "question", "text": question})
    print(f"{MUTED}❯ pi · read + web{RESET}", file=sys.stderr)

    pi_cmd = [
        "pi",
        "-p",
        "--mode",
        "json",
        "--no-session",
        "--tools",
        "read,web_search",
        "--append-system-prompt",
        "Answer concisely. You are responding to a quick question typed at a shell prompt.",
        question,
    ]
    filter_cmd = [stream_filter]
    renderer_cmd = ["glow", "-s", "dark", "-"] if shutil.which("glow") else ["cat"]

    pi_proc = subprocess.Popen(pi_cmd, stdout=subprocess.PIPE)
    filter_proc = subprocess.Popen(filter_cmd, stdin=pi_proc.stdout, stdout=subprocess.PIPE)
    assert pi_proc.stdout is not None
    pi_proc.stdout.close()
    renderer_proc = subprocess.Popen(renderer_cmd, stdin=filter_proc.stdout)
    assert filter_proc.stdout is not None
    filter_proc.stdout.close()

    renderer_code = renderer_proc.wait()
    filter_code = filter_proc.wait()
    pi_code = pi_proc.wait()
    print()
    if pi_code:
        return pi_code
    if filter_code:
        return filter_code
    return renderer_code
