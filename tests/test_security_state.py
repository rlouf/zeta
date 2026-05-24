from __future__ import annotations

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from sigil.commands import previous
from sigil.pi_stream import stream_events
from sigil.security import (
    SecurityViolation,
    inherit_security,
    make_security,
    normalize_security,
    reject_promotion,
)
from sigil.state import append_event, append_jsonl, read_jsonl, write_json, write_jsonl


class SecurityTests(unittest.TestCase):
    def test_legacy_record_is_low_trust(self) -> None:
        record = normalize_security({"type": "old"})
        self.assertEqual(record["integrity"], "unknown")
        self.assertEqual(record["taint"], ["legacy"])
        self.assertEqual(record["capability"], "none")

    def test_continuation_descends_to_lowest_integrity_and_keeps_inputs(self) -> None:
        inherited = inherit_security(
            glyph="??",
            input_records=[
                {
                    "event_id": "question-1",
                    "integrity": "web",
                    "capability": "read",
                    "taint": ["web"],
                },
                {"event_id": "legacy-1"},
            ],
            capability="read",
        )
        self.assertEqual(inherited["integrity"], "unknown")
        self.assertEqual(inherited["inputs"], ["question-1", "legacy-1"])
        self.assertEqual(inherited["taint"], ["legacy", "web"])

    def test_integrity_promotion_requires_fresh_human_input(self) -> None:
        with self.assertRaises(SecurityViolation):
            make_security(
                glyph="??",
                integrity="local_model",
                capability="propose",
                taint=["model"],
                input_records=[{"integrity": "web", "taint": ["web"]}],
            )

        security = make_security(
            glyph=",",
            integrity="local_model",
            capability="propose",
            taint=["model"],
            fresh_human=True,
        )
        self.assertEqual(security["integrity"], "local_model")

    def test_reject_promotion_mutation_without_fresh_human(self) -> None:
        with self.assertRaises(SecurityViolation):
            reject_promotion({"integrity": "web"}, {"integrity": "local_model"})


class StateTests(unittest.TestCase):
    def test_writers_normalize_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_state_dir = os.environ.get("SIGIL_STATE_DIR")
            old_session_id = os.environ.get("SIGIL_SESSION_ID")
            os.environ["SIGIL_STATE_DIR"] = tmp
            os.environ["SIGIL_SESSION_ID"] = "test"
            try:
                event = append_event({"type": "legacy_shape"})
                self.assertEqual(event["integrity"], "unknown")
                self.assertEqual(event["taint"], ["legacy"])

                turn = append_jsonl(
                    "last-question.jsonl",
                    {
                        "role": "assistant",
                        "content": "answer",
                        "glyph": "?",
                        "integrity": "web",
                        "capability": "read",
                        "taint": ["web"],
                        "inputs": [event["id"]],
                        "provisional": True,
                    },
                )
                self.assertEqual(turn["inputs"], [event["id"]])
                self.assertTrue(turn["provisional"])

                written = write_jsonl("last-tools.jsonl", [{"type": "tool_start"}])
                self.assertEqual(written[0]["taint"], ["legacy"])
                self.assertEqual(
                    read_jsonl("last-tools.jsonl")[0]["integrity"], "unknown"
                )

                events_path = Path(tmp) / "events.jsonl"
                stored = json.loads(
                    events_path.read_text(encoding="utf-8").splitlines()[0]
                )
                self.assertEqual(stored["taint"], ["legacy"])
            finally:
                if old_state_dir is None:
                    os.environ.pop("SIGIL_STATE_DIR", None)
                else:
                    os.environ["SIGIL_STATE_DIR"] = old_state_dir
                if old_session_id is None:
                    os.environ.pop("SIGIL_SESSION_ID", None)
                else:
                    os.environ["SIGIL_SESSION_ID"] = old_session_id

    def test_previous_command_inherits_legacy_low_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_state_dir = os.environ.get("SIGIL_STATE_DIR")
            old_session_id = os.environ.get("SIGIL_SESSION_ID")
            os.environ["SIGIL_STATE_DIR"] = tmp
            os.environ["SIGIL_SESSION_ID"] = "test"
            try:
                write_json(
                    "last-command.json",
                    {
                        "id": "legacy-command",
                        "prompt": "status",
                        "commands": [
                            {"command": "git status --short", "note": "show changes"}
                        ],
                    },
                )
                prompt, candidates, security = previous()
                self.assertEqual(prompt, "status")
                self.assertEqual(candidates[0]["command"], "git status --short")
                self.assertEqual(security["integrity"], "unknown")
                self.assertEqual(security["taint"], ["legacy"])
                self.assertEqual(security["inputs"], ["legacy-command"])
            finally:
                if old_state_dir is None:
                    os.environ.pop("SIGIL_STATE_DIR", None)
                else:
                    os.environ["SIGIL_STATE_DIR"] = old_state_dir
                if old_session_id is None:
                    os.environ.pop("SIGIL_SESSION_ID", None)
                else:
                    os.environ["SIGIL_SESSION_ID"] = old_session_id

    def test_pi_stream_records_web_tainted_answer_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saved = {
                key: os.environ.get(key)
                for key in [
                    "SIGIL_STATE_DIR",
                    "SIGIL_SESSION_ID",
                    "SIGIL_CAPTURE_ANSWER",
                    "SIGIL_CAPTURE_TRACE",
                    "SIGIL_SECURITY_GLYPH",
                    "SIGIL_SECURITY_INTEGRITY",
                    "SIGIL_SECURITY_CAPABILITY",
                    "SIGIL_SECURITY_TAINT",
                    "SIGIL_SECURITY_PROVISIONAL",
                    "SIGIL_SECURITY_INPUTS",
                    "SIGIL_QUESTION",
                    "SIGIL_PROMPT",
                    "SIGIL_FOLLOW_UP",
                ]
            }
            os.environ["SIGIL_STATE_DIR"] = tmp
            os.environ["SIGIL_SESSION_ID"] = "test"
            os.environ["SIGIL_CAPTURE_ANSWER"] = "1"
            os.environ["SIGIL_SECURITY_GLYPH"] = "?"
            os.environ["SIGIL_SECURITY_INTEGRITY"] = "web"
            os.environ["SIGIL_SECURITY_CAPABILITY"] = "read"
            os.environ["SIGIL_SECURITY_TAINT"] = "web"
            os.environ["SIGIL_SECURITY_PROVISIONAL"] = "1"
            os.environ["SIGIL_SECURITY_INPUTS"] = "question-event"
            try:
                stdin = StringIO(
                    json.dumps(
                        {
                            "type": "message_update",
                            "assistantMessageEvent": {
                                "type": "text_delta",
                                "delta": "answer",
                            },
                        }
                    )
                    + "\n"
                )
                self.assertEqual(
                    stream_events(stdin=stdin, stdout=StringIO(), stderr=StringIO()), 0
                )
                answer = read_jsonl("last-question.jsonl")[0]
                self.assertEqual(answer["inputs"], ["question-event"])
                self.assertTrue(answer["event_id"])
                self.assertEqual(answer["integrity"], "web")
                self.assertEqual(answer["capability"], "read")
                self.assertEqual(answer["taint"], ["web"])
                self.assertTrue(answer["provisional"])
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_pi_stream_json_output_is_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saved = {
                key: os.environ.get(key)
                for key in [
                    "SIGIL_STATE_DIR",
                    "SIGIL_SESSION_ID",
                    "SIGIL_CAPTURE_ANSWER",
                    "SIGIL_CAPTURE_TRACE",
                    "SIGIL_SECURITY_GLYPH",
                    "SIGIL_SECURITY_INTEGRITY",
                    "SIGIL_SECURITY_CAPABILITY",
                    "SIGIL_SECURITY_TAINT",
                    "SIGIL_SECURITY_PROVISIONAL",
                    "SIGIL_SECURITY_INPUTS",
                    "SIGIL_QUESTION",
                    "SIGIL_PROMPT",
                    "SIGIL_FOLLOW_UP",
                ]
            }
            os.environ["SIGIL_STATE_DIR"] = tmp
            os.environ["SIGIL_SESSION_ID"] = "test"
            os.environ["SIGIL_CAPTURE_ANSWER"] = "1"
            os.environ["SIGIL_CAPTURE_TRACE"] = "1"
            os.environ["SIGIL_SECURITY_GLYPH"] = "?"
            os.environ["SIGIL_SECURITY_INTEGRITY"] = "web"
            os.environ["SIGIL_SECURITY_CAPABILITY"] = "read"
            os.environ["SIGIL_SECURITY_TAINT"] = "web"
            os.environ["SIGIL_SECURITY_PROVISIONAL"] = "1"
            os.environ["SIGIL_SECURITY_INPUTS"] = "question-event"
            os.environ["SIGIL_QUESTION"] = "what is sigil?"
            os.environ["SIGIL_PROMPT"] = "what is sigil?"
            os.environ["SIGIL_FOLLOW_UP"] = "0"
            try:
                stdin = StringIO(
                    "\n".join(
                        [
                            json.dumps(
                                {
                                    "type": "tool_execution_start",
                                    "toolName": "web_search",
                                    "args": {"query": "sigil"},
                                }
                            ),
                            json.dumps(
                                {"type": "tool_execution_end", "toolName": "web_search"}
                            ),
                            json.dumps(
                                {
                                    "type": "message_update",
                                    "assistantMessageEvent": {
                                        "type": "text_delta",
                                        "delta": "answer",
                                    },
                                }
                            ),
                        ]
                    )
                    + "\n"
                )
                stdout = StringIO()
                stderr = StringIO()
                self.assertEqual(
                    stream_events(
                        stdin=stdin, stdout=stdout, stderr=stderr, json_output=True
                    ),
                    0,
                )

                payload = json.loads(stdout.getvalue())
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["type"], "answer")
                self.assertEqual(payload["question"], "what is sigil?")
                self.assertEqual(payload["answer"], "answer")
                self.assertEqual(payload["security"]["taint"], ["web"])
                self.assertEqual(payload["tools"][0]["tool"], "web_search")
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(
                    read_jsonl("last-question.jsonl")[-1]["content"], "answer"
                )
                self.assertEqual(len(read_jsonl("last-tools.jsonl")), 2)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
