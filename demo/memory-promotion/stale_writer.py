"""Race two promotions through zeta's own promote_all to show the CAS reject a stale write."""

import sys
from pathlib import Path

from zeta.context.transforms import (
    ContentConflict,
    ContentNode,
    ContentPromotion,
    ContentWorkspace,
    content_node_from_object,
    content_revision_from_object,
    put_content_node,
)
from zeta.journal.sqlite import zeta_sqlite_path
from zeta.substrate import SqliteObjectStore

KEY = "conclusions/latency"


def main() -> None:
    state_dir = Path(sys.argv[1])
    store = SqliteObjectStore(zeta_sqlite_path(state_dir))
    ref = next(
        r
        for r in store.refs()
        if r.name.startswith("agent/") and r.name.endswith("/content/head")
    )
    owner = ref.name[len("agent/") : -len("/content/head")]
    h1 = ref.object_id
    o1 = content_revision_from_object(store.get_object(h1)).nodes[KEY]
    print(f"durable agent head for owner {owner!r}:")
    print(f"  head H1 = {h1}")
    print(f"  {KEY} -> {o1}")
    print()
    print("Two attempts both read H1, compute, then try to promote a new conclusion.")
    print()

    def promotion(object_id: str, reason: str) -> ContentPromotion:
        return ContentPromotion(
            scope="agent",
            key=KEY,
            object_id=object_id,
            expected_head=h1,
            expected_object_id=o1,
            source_head=h1,
            reason=reason,
        )

    workspace_a = ContentWorkspace(
        store, run_id="cas-attempt-a", session_id="cas-attempt-a", owner=owner
    )
    o2 = put_content_node(
        store,
        ContentNode(
            KEY,
            "memory",
            "Newer conclusion: checkout p99 regressed further to 2.4s after "
            "the 2026-08-11 deploy.",
            title="Latency conclusion",
        ),
    )
    result = workspace_a.promote_all(
        (promotion(o2, "Newer computation commits first."),)
    )
    h2 = result[0].new_head
    print(f"attempt A: promote against expected_head=H1 -> ACCEPTED, head is now {h2}")
    print()

    workspace_b = ContentWorkspace(
        store, run_id="cas-attempt-b", session_id="cas-attempt-b", owner=owner
    )
    o3 = put_content_node(
        store,
        ContentNode(
            KEY,
            "memory",
            "STALE conclusion computed from pre-deploy data.",
            title="Latency conclusion",
        ),
    )
    try:
        workspace_b.promote_all((promotion(o3, "Old computation finishes late."),))
    except ContentConflict as error:
        print("attempt B: promote against the SAME expected_head=H1 -> REJECTED")
        print(f"  ContentConflict: {error}")
    else:
        print("ERROR: the stale promotion was accepted")
        store.close()
        sys.exit(1)

    final_ref = store.get_ref(ref.name)
    assert final_ref is not None
    final_revision = content_revision_from_object(store.get_object(final_ref.object_id))
    final_node = content_node_from_object(store.get_object(final_revision.nodes[KEY]))
    print()
    print(f"final durable head: {final_ref.object_id}")
    print(f"final {KEY} content: \"{final_node.content}\"")
    store.close()


if __name__ == "__main__":
    main()
