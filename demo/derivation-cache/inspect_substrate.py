"""Read-only substrate queries that surface the reuse evidence for this demo.

The session-scoped `zeta traces` commands cannot traverse content-workspace
derivations (they are recorded without a session id), so this script reads the
substrate SQLite store directly and only reads.
"""

import json
import sqlite3
import sys
from pathlib import Path


def connect(db: str) -> sqlite3.Connection:
    uri = f"{Path(db).resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def content_nodes(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id, data_json FROM objects WHERE kind = 'content_node' ORDER BY id"
    ).fetchall()


def cmd_nodes(connection: sqlite3.Connection) -> None:
    for row in content_nodes(connection):
        data = json.loads(row["data_json"])
        print(f"{row['id']}  {data['kind']:<9} {data['key']}")


def cmd_node_id(connection: sqlite3.Connection, key: str) -> None:
    for row in content_nodes(connection):
        if json.loads(row["data_json"])["key"] == key:
            print(row["id"])
            return


def transform_model_calls(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT output_id, params_json FROM derivations "
        "WHERE producer = 'ModelResponse' ORDER BY created_at"
    ).fetchall()
    return [row for row in rows if "retry_ref" in json.loads(row["params_json"])]


def cmd_modelcalls(connection: sqlite3.Connection) -> None:
    print(len(transform_model_calls(connection)))


def cmd_retryrefs(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT name, object_id FROM refs "
        "WHERE name LIKE 'content-transform/retry/%' ORDER BY name"
    ).fetchall()
    for row in rows:
        print(f"{row['name']}  ->  {row['object_id']}")


def cmd_refcount(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM refs WHERE name LIKE 'content-transform/retry/%'"
    ).fetchone()
    print(row["n"])


def get_object(connection: sqlite3.Connection, object_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, kind, schema, data_json, links_json FROM objects WHERE id = ?",
        (object_id,),
    ).fetchone()


def describe(connection: sqlite3.Connection, object_id: str) -> str:
    obj = get_object(connection, object_id)
    if obj is None:
        return f"{object_id} (missing)"
    data = json.loads(obj["data_json"])
    if obj["kind"] == "content_node":
        return f"content_node {data['kind']} key={data['key']}  {object_id}"
    return f"{obj['kind']}  {object_id}"


def cmd_tree(connection: sqlite3.Connection, key: str) -> None:
    """Print the derivation chain of one derived content node, newest first."""
    target = None
    for row in content_nodes(connection):
        if json.loads(row["data_json"])["key"] == key:
            target = row
    if target is None:
        print(f"no content node with key {key}")
        return
    print(describe(connection, target["id"]))
    seen = {target["id"]}
    stack = [(target["id"], 1)]
    while stack:
        object_id, depth = stack.pop()
        derivations = connection.execute(
            "SELECT producer, input_ids_json FROM derivations WHERE output_id = ?",
            (object_id,),
        ).fetchall()
        for derivation in derivations:
            for input_id in json.loads(derivation["input_ids_json"]):
                label = describe(connection, input_id)
                print(f"{'  ' * depth}<- [{derivation['producer']}] {label}")
                obj = get_object(connection, input_id)
                keep = obj is not None and obj["kind"] in {
                    "content_node",
                    "assistant_message",
                    "prompt",
                }
                if keep and input_id not in seen and depth < 4:
                    seen.add(input_id)
                    stack.append((input_id, depth + 1))


def cmd_summary_links(connection: sqlite3.Connection, key: str) -> None:
    """Print the assistant-message object id a summary node links to."""
    target = None
    for row in content_nodes(connection):
        if json.loads(row["data_json"])["key"] == key:
            target = row
    if target is None:
        return
    obj = get_object(connection, target["id"])
    for link in json.loads(obj["links_json"]):
        linked = get_object(connection, link)
        if linked is not None and linked["kind"] == "assistant_message":
            print(link)


def main() -> int:
    db, command, *args = sys.argv[1:]
    connection = connect(db)
    commands = {
        "nodes": cmd_nodes,
        "node-id": cmd_node_id,
        "modelcalls": cmd_modelcalls,
        "retryrefs": cmd_retryrefs,
        "refcount": cmd_refcount,
        "tree": cmd_tree,
        "summary-links": cmd_summary_links,
    }
    commands[command](connection, *args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
