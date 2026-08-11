"""Show session vs agent content heads so scope isolation is visible in the store."""

import json
import sys
from pathlib import Path

from zeta.context.transforms import (
    content_node_from_object,
    content_revision_from_object,
)
from zeta.journal.sqlite import zeta_sqlite_path
from zeta.substrate import SqliteObjectStore


def preview(value: object, limit: int = 110) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def main() -> None:
    state_dir = Path(sys.argv[1])
    store = SqliteObjectStore(zeta_sqlite_path(state_dir), read_only=True)
    heads = sorted(
        (ref for ref in store.refs() if ref.name.endswith("/content/head")),
        key=lambda ref: ref.name,
    )
    for ref in heads:
        scope = ref.name.split("/", 1)[0]
        print(f"{ref.name}")
        if scope == "run":
            print("    (per-run workspace head; discarded from prompts after the run)")
            continue
        revision = content_revision_from_object(store.get_object(ref.object_id))
        for key in revision.projection_order:
            node = content_node_from_object(store.get_object(revision.nodes[key]))
            print(
                f"    {key} [kind={node.kind}, "
                f"source_scope={revision.source_scopes[key]}]"
            )
            print(f'        "{preview(node.content)}"')
    store.close()


if __name__ == "__main__":
    main()
