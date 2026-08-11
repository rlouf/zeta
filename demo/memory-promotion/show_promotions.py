"""Render runtime.content.promoted journal events as readable commit lines."""

import json
import sys


def main() -> None:
    for event in json.load(sys.stdin):
        payload = event["payload"]
        print(f"{event['id']}  scope={payload['scope']:<7} key={payload['key']}")
        print(f"    reason: {payload['reason']}")
        print(f"    head: {payload['old_head']} -> {payload['new_head']}")


if __name__ == "__main__":
    main()
