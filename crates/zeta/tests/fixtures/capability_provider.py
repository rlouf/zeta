import json
import pathlib
import subprocess
import sys
import time

marker = pathlib.Path(sys.argv[1])
run_number = int(marker.read_text()) + 1 if marker.exists() else 1
marker.write_text(str(run_number))


def send(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


send(
    {
        "jsonrpc": "2.0",
        "id": "peer-initialize",
        "method": "initialize",
        "params": {
            "protocol_versions": [0],
            "peer": {"name": "reference-provider", "version": "0.0.1"},
            "roles": ["provider"],
            "methods": [{"name": "test.deliver"}],
            "heartbeat_seconds": 10,
            "max_in_flight": 64,
        },
    }
)

initialized = json.loads(sys.stdin.readline())
assert initialized["id"] == "peer-initialize"

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "shutdown":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {}})
        raise SystemExit(0)
    params = request["params"]
    input_value = params["input"]
    if input_value.get("exit"):
        raise SystemExit(3)
    if input_value.get("slow"):
        time.sleep(1)
    if descendant_marker := input_value.get("descendant_marker"):
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib, sys, time; "
                "time.sleep(1); pathlib.Path(sys.argv[1]).write_text('survived')",
                descendant_marker,
            ]
        )
        time.sleep(5)
    if input_value.get("fail"):
        send(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {
                    "code": -32000,
                    "message": "rejected by fixture",
                    "data": {"code": "provider_rejected", "retryable": True},
                },
            }
        )
        continue
    if input_value.get("non_object"):
        send({"jsonrpc": "2.0", "id": request["id"], "result": "invalid"})
        continue
    send(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "ok": True,
                "delivered": input_value.get("text"),
                "effect_key": params.get("effect_key"),
                "base_dir": params.get("base_dir"),
            },
        }
    )
