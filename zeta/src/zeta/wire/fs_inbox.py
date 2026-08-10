"""The filesystem inbox connector as a wire-v0 source child.

Run as ``python -m zeta.wire.fs_inbox '<config json>'``. The config
carries the watches (dir, optional glob), the poll interval, and an
optional debounce override:

```json
{"watches": [{"dir": "/p/inbox", "glob": "*"}], "poll_interval": 1.0}
```

The polling itself is `connectors.filesystem.collect_file_created`,
unchanged, so the subprocess emits the same event type and payloads as
the in-process connector.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

from connectors import IngressBinding
from connectors.filesystem import FILE_CREATED, collect_file_created

from zeta._version import __version__
from zeta.wire.plugin import EventType, SourceEvent, run_source

DEFAULT_POLL_INTERVAL_SECS = 1.0


async def watch_events(
    watches: list[dict[str, Any]],
    *,
    poll_interval: float,
    debounce_seconds: float,
) -> AsyncIterator[SourceEvent]:
    state: dict[str, dict[str, Any]] = {}
    bindings = [IngressBinding(FILE_CREATED, filter=dict(watch)) for watch in watches]
    while True:
        now = time.time()
        for binding in bindings:
            for draft in collect_file_created(
                binding, state, now=now, debounce_seconds=debounce_seconds
            ):
                yield SourceEvent(draft.event_type, dict(draft.payload))
        await asyncio.sleep(poll_interval)


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        raise SystemExit("usage: python -m zeta.wire.fs_inbox '<config json>'")
    config = json.loads(argv[0])
    debounce = config.get("debounce")
    if debounce is None:
        debounce = float(os.environ.get("FILESYSTEM_DEBOUNCE_SECONDS", "2"))
    run_source(
        watch_events(
            config["watches"],
            poll_interval=float(
                config.get("poll_interval", DEFAULT_POLL_INTERVAL_SECS)
            ),
            debounce_seconds=float(debounce),
        ),
        name="fs-inbox",
        plugin_version=__version__,
        event_types=[EventType(FILE_CREATED, f"{FILE_CREATED}@1")],
    )


if __name__ == "__main__":
    main(sys.argv[1:])
