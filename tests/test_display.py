"""Terminal rendering tests."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from _zeta_helpers import (
    TtyBuffer,
    visible_terminal_text,
)
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

import sigil.display.render as display_render
import sigil.display.summarize as display_summarize
from sigil.display.tty import IRIS, ITALIC, MUTED, RESET
from sigil.protocols import (
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
)
from zeta import trace as zeta_trace


def test_sigil_display_summarizes_tool_results() -> None:
    assert display_summarize.tool_result_summary(
        "bash",
        {
            "ok": True,
            "effect": {
                "kind": "command",
                "status": "proposed",
                "command": "uv run pytest",
            },
        },
    ) == ["staged"]
    assert display_summarize.tool_result_summary(
        "bash",
        {
            "ok": True,
            "metadata": {"mode": "direct", "status": 0},
        },
    ) == ["succeeded"]
    assert display_summarize.tool_result_summary(
        "bash",
        {
            "ok": False,
            "metadata": {"mode": "direct", "status": 2},
        },
    ) == ["failed · exit 2"]
    assert display_summarize.tool_result_summary(
        "read",
        {"ok": True, "content": [{"type": "text", "text": "a\nb\n"}]},
    ) == ["2 lines"]
    assert display_summarize.tool_result_summary(
        "read",
        {
            "ok": False,
            "error": {
                "code": "read-failed",
                "message": "[Errno 2] No such file or directory: 'missing.md'",
            },
        },
    ) == ["read-failed: [Errno 2] No such file or directory: 'missing.md'"]
    assert display_summarize.tool_result_summary(
        "write",
        {
            "ok": True,
            "metadata": {"mode": "direct", "path": "notes.txt"},
        },
    ) == ["wrote · notes.txt"]
    assert display_summarize.tool_result_summary(
        "grep",
        {"ok": True, "content": [{"type": "text", "text": "a.py:1:x\nb.py:2:y\n"}]},
    ) == ["2 matches · 2 files"]
    assert display_summarize.tool_result_summary(
        "grep",
        {
            "ok": True,
            "content": [{"type": "text", "text": "a.py:1:x\n"}],
            "metadata": {"matches": 10, "files": 3, "truncated": True},
        },
    ) == ["10 matches · 3 files · truncated"]


def test_sigil_display_classifies_progress_events() -> None:
    event = display_render.progress_event_for_tool_result(
        "read",
        {"ok": True, "metadata": {"path": "src/sigil/agent_io.py"}},
        {"path": "src/sigil/agent_io.py"},
    )
    assert event is not None
    assert event.kind == "read"
    assert event.phase == "Mapping repo"
    assert event.line == "✓ read src/sigil/agent_io.py · ok"

    event = display_render.progress_event_for_tool_result(
        "ls",
        {"ok": True, "metadata": {"path": "src/sigil", "entries": 3}},
        {"path": "src/sigil"},
    )
    assert event is not None
    assert event.kind == "list"
    assert event.phase == "Mapping repo"
    assert event.line == "✓ listed src/sigil · 3 entries"

    event = display_render.progress_event_for_tool_result(
        "ls",
        {
            "ok": True,
            "metadata": {
                "path": "src/sigil",
                "entries": 30,
                "recursive": True,
                "limit": 50,
            },
        },
        {"path": "src/sigil", "recursive": True, "limit": 50},
    )
    assert event is not None
    assert event.line == "✓ listed src/sigil (recursive) · 30 entries"

    event = display_render.progress_event_for_tool_result(
        "write",
        {"ok": True, "metadata": {"mode": "direct", "path": "notes.md"}},
        {"path": "notes.md"},
    )
    assert event is not None
    assert event.kind == "mutation"
    assert event.phase == "Applying changes"
    assert event.line == "+ notes.md"

    event = display_render.progress_event_for_tool_result(
        "bash",
        {"ok": False, "metadata": {"mode": "direct", "status": 2}},
        {"command": "uv run pytest"},
    )
    assert event is not None
    assert event.kind == "failure"
    assert event.phase == "Validating"
    assert event.line == "✗ uv run pytest · failed · exit 2"


def test_sigil_display_terminal_digest_keeps_short_turns_compact() -> None:
    output = StringIO()
    renderer = display_render.TerminalDigestRenderer(output, clock=lambda: 0.0)

    renderer.observe_tool_call("read", {"path": "README.md"})
    renderer.observe_tool_result(
        "read",
        {"ok": True, "content": [{"type": "text", "text": "one\n"}]},
    )

    assert output.getvalue() == "✓ read README.md · 1 lines\n"
    assert renderer.status_detail() == "mapping repo · 1 events · last: README.md"


def test_sigil_display_terminal_digest_quotes_web_search_query() -> None:
    output = StringIO()
    renderer = display_render.TerminalDigestRenderer(output, clock=lambda: 0.0)

    renderer.observe_tool_call(
        "web_search",
        {"query": "Remi Louf .txt AI founder Outlines author"},
    )
    renderer.observe_tool_result(
        "web_search",
        {"ok": True, "metadata": {"result_count": 4}},
    )

    assert output.getvalue().splitlines() == [
        '→ web_search "Remi Louf .txt AI founder Outlines author"',
        '✓ web_search "Remi Louf .txt AI founder Outlines author" · 4 results',
    ]


def test_sigil_display_terminal_digest_has_no_empty_status_detail() -> None:
    output = StringIO()
    renderer = display_render.TerminalDigestRenderer(output)

    assert renderer.status_detail() == ""


def test_sigil_display_terminal_digest_switches_to_chapters() -> None:
    output = StringIO()
    now = 0.0
    renderer = display_render.TerminalDigestRenderer(
        output,
        clock=lambda: now,
        chapter_event_threshold=2,
    )

    renderer.observe_tool_call("read", {"path": "README.md"})
    renderer.observe_tool_result("read", {"ok": True})
    renderer.observe_tool_call("grep", {"pattern": "PromptBuilder"})
    renderer.observe_tool_result("grep", {"ok": True})
    now = 31.0
    renderer.observe_tool_call("write", {"path": "slides/story.md"})
    renderer.observe_tool_result(
        "write",
        {"ok": True, "metadata": {"mode": "direct", "path": "slides/story.md"}},
    )

    assert output.getvalue().splitlines() == [
        "✓ read README.md · ok",
        "✓ searched PromptBuilder · ok",
        "",
        "[00:31] Applying changes",
        "  + slides/story.md",
    ]


def test_sigil_display_terminal_digest_bounds_repeated_chapter_lines() -> None:
    output = StringIO()
    renderer = display_render.TerminalDigestRenderer(
        output,
        clock=lambda: 0.0,
        chapter_event_threshold=1,
        max_chapter_lines=2,
    )

    for index in range(4):
        path = f"src/file_{index}.py"
        renderer.observe_tool_call("read", {"path": path})
        renderer.observe_tool_result("read", {"ok": True})

    assert output.getvalue().splitlines() == [
        "✓ read src/file_0.py · ok",
        "",
        "[00:00] Mapping repo",
        "  ✓ read src/file_1.py · ok",
        "  ✓ read src/file_2.py · ok",
    ]


def test_sigil_display_terminal_digest_quiet_keeps_failures_and_final_digest() -> None:
    output = StringIO()
    renderer = display_render.TerminalDigestRenderer(output, mode="quiet")

    renderer.observe_tool_call("read", {"path": "README.md"})
    renderer.observe_tool_result("read", {"ok": True})
    renderer.observe_tool_call("bash", {"command": "uv run pytest"})
    renderer.observe_tool_result(
        "bash",
        {"ok": False, "metadata": {"mode": "direct", "status": 1}},
    )
    renderer.finalize(
        {
            "turn_id": "16717bb7-aaaa",
            "cost": {"wall_ms": 1520},
            "outcome": "failed",
        }
    )

    assert output.getvalue().splitlines() == [
        "✗ uv run pytest · failed · exit 1",
        "",
        "Done in 1s · 1 command · 1 failure · log 16717bb7",
    ]


def test_sigil_display_terminal_digest_final_receipt_summarizes_effects() -> None:
    output = StringIO()
    renderer = display_render.TerminalDigestRenderer(output, mode="compact")
    renderer.observe_tool_call("bash", {"command": "uv run pytest"})
    renderer.observe_tool_result(
        "bash",
        {"ok": True, "metadata": {"mode": "direct", "status": 0}},
    )

    renderer.finalize(
        {
            "turn_id": "16717bb7-aaaa",
            "outcome": "executed",
            "cost": {"wall_ms": 152_000},
            "effects": [
                {"kind": "file_write", "path": "notes.md"},
                {"kind": "command", "command": "uv run pytest"},
            ],
        }
    )

    assert output.getvalue().splitlines()[-1] == (
        "Done in 2m32s · 1 file · 1 command · log 16717bb7"
    )


def test_sigil_display_terminal_digest_uses_reasoning_for_phase() -> None:
    output = StringIO()
    renderer = display_render.TerminalDigestRenderer(
        output,
        clock=lambda: 12.0,
        chapter_event_threshold=1,
    )

    renderer.observe_tool_call("read", {"path": "README.md"})
    renderer.observe_tool_result("read", {"ok": True})
    renderer.observe_reasoning_delta("Now I'll inspect prompt assembly before editing.")
    renderer.observe_tool_call("read", {"path": "src/sigil/agent_io.py"})
    renderer.observe_tool_result("read", {"ok": True})

    assert renderer.current_phase == "Inspect prompt assembly before editing"
    assert renderer.status_detail().startswith("inspect prompt assembly")
    assert "[00:00] Inspect prompt assembly before editing" in output.getvalue()


def test_sigil_display_terminal_digest_ignores_generic_reasoning_phase() -> None:
    output = StringIO()
    renderer = display_render.TerminalDigestRenderer(
        output,
        clock=lambda: 12.0,
        chapter_event_threshold=1,
    )

    renderer.observe_tool_call("read", {"path": "README.md"})
    renderer.observe_tool_result("read", {"ok": True})
    renderer.observe_reasoning_delta("Checking.")
    renderer.observe_tool_call("read", {"path": "src/sigil/agent_io.py"})
    renderer.observe_tool_result("read", {"ok": True})

    assert renderer.current_phase == "Mapping repo"
    assert "[00:00] Mapping repo" in output.getvalue()
    assert "[00:00] Checking" not in output.getvalue()


def test_sigil_display_terminal_digest_keeps_specific_reasoning_phase() -> None:
    assert display_render.reasoning_phase("Checking model configuration.") == (
        "Checking model configuration"
    )


def test_sigil_display_thinking_status_forwards_reasoning_to_progress(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_THINKING_TRACE", "0")
    seen: list[str] = []

    with display_render.ThinkingStatus(
        StringIO(),
        enabled=False,
        reasoning_observer=seen.append,
    ) as status:
        status.reasoning_delta("I need to understand the workflow path.")

    assert seen == ["I need to understand the workflow path."]


def test_sigil_display_renders_tool_paths_relative_to_cwd(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    inside = str(Path.cwd() / "docs" / "notes.md")

    assert display_summarize.summarize("read", {"path": inside}) == "docs/notes.md"
    assert display_summarize.summarize("ls", {"path": str(Path.cwd())}) == "."
    assert display_summarize.summarize("read", {"path": "/etc/hosts"}) == "/etc/hosts"
    assert display_summarize.summarize("grep", {"pattern": "^#"}) == "^#"
    assert (
        display_summarize.summarize(
            "web_search",
            {
                "query": "Remi Louf public profile",
            },
        )
        == '"Remi Louf public profile"'
    )
    assert display_summarize.tool_result_summary(
        "write",
        {"ok": True, "metadata": {"mode": "direct", "path": inside}},
    ) == ["wrote · docs/notes.md"]
    assert display_summarize.tool_result_summary(
        "web_search",
        {"ok": True, "metadata": {"result_count": 8}},
    ) == ["8 results"]


def test_sigil_display_summarizes_current_context_estimate() -> None:
    line = display_render.context_usage_line(
        {
            "usage": {
                "prompt_tokens": 18_432,
                "completion_tokens": 391,
                "total_tokens": 18_823,
            },
            "model_context_tokens": 262_144,
        }
    )

    assert line == "context  [█░░░░░░░░░░░░░░░░░░░] 7%"
    assert (
        display_render.context_usage_line(
            {"usage": {"prompt_tokens": 18_432, "completion_tokens": 391}}
        )
        == ""
    )
    assert (
        display_render.context_usage_line(
            {"estimated_context_tokens": 200, "model_context_tokens": 1_000}
        )
        == "context  [████░░░░░░░░░░░░░░░░] 20% est."
    )


def test_sigil_display_summarizes_context_from_total_tokens() -> None:
    line = display_render.context_usage_line(
        {
            "usage": {"total_tokens": 18_823},
            "model_context_tokens": 262_144,
        }
    )

    assert line == "context  [█░░░░░░░░░░░░░░░░░░░] 7%"


def test_sigil_display_context_usage_footer_estimates_tool_result_tokens() -> None:
    output = StringIO()
    footer = display_render.ContextUsageFooter(output)
    base_telemetry = {
        "usage": {"prompt_tokens": 100, "completion_tokens": 0},
        "model_context_tokens": 1_000,
    }
    result = {"ok": True, "content": [{"type": "text", "text": "x" * 200}]}

    footer.update(base_telemetry)
    footer.update_for_tool_result(None, result)

    estimated_tokens = 100 + display_render.estimated_tool_result_context_tokens(result)
    assert footer.current_line() == display_render.context_usage_line(
        {
            "estimated_context_tokens": estimated_tokens,
            "model_context_tokens": 1_000,
        }
    )
    assert footer.current_line().endswith(" est.")
    assert output.getvalue() == ""

    real_telemetry = {
        "usage": {"prompt_tokens": 250, "completion_tokens": 10},
        "model_context_tokens": 1_000,
    }
    footer.finalize(real_telemetry)

    assert output.getvalue() == "context  [█████░░░░░░░░░░░░░░░] 26%\n"


def test_sigil_display_tool_result_telemetry_replaces_stale_estimates() -> None:
    footer = display_render.ContextUsageFooter(StringIO())
    stale_result = {"ok": True, "content": [{"type": "text", "text": "x" * 400}]}
    fresh_result = {"ok": True, "content": [{"type": "text", "text": "y" * 40}]}
    fresh_telemetry = {
        "usage": {"prompt_tokens": 400, "completion_tokens": 20},
        "model_context_tokens": 1_000,
    }

    footer.update(
        {
            "usage": {"prompt_tokens": 100, "completion_tokens": 0},
            "model_context_tokens": 1_000,
        }
    )
    footer.update_for_tool_result(None, stale_result)
    footer.update_for_tool_result(fresh_telemetry, fresh_result)

    expected_tokens = 420 + display_render.estimated_tool_result_context_tokens(
        fresh_result
    )
    assert footer.current_line() == display_render.context_usage_line(
        {
            "estimated_context_tokens": expected_tokens,
            "model_context_tokens": 1_000,
        }
    )
    assert display_render.context_usage_line({"model_context_tokens": 262_144}) == ""
    assert (
        display_render.context_usage_line(
            {
                "usage": {"prompt_tokens": 18_432},
                "model_context_tokens": 262_144,
            }
        )
        == ""
    )


def test_sigil_display_context_usage_footer_is_ephemeral_for_tty(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    telemetry = {
        "usage": {"prompt_tokens": 18_432, "completion_tokens": 391},
        "model_context_tokens": 262_144,
    }
    output = TtyBuffer()
    footer = display_render.ContextUsageFooter(output)

    assert footer.update(telemetry)
    assert not output.getvalue().endswith("\n")
    assert output.getvalue() == "\r\x1b[2Kcontext  [█░░░░░░░░░░░░░░░░░░░] 7%"

    footer.clear()
    assert output.getvalue().endswith("\r\x1b[2K")
    assert footer.finalize(telemetry)
    assert output.getvalue().endswith("context  [█░░░░░░░░░░░░░░░░░░░] 7%\n")


def test_sigil_display_context_usage_footer_prints_final_only_for_non_tty() -> None:
    telemetry = {
        "usage": {"prompt_tokens": 18_432, "completion_tokens": 391},
        "model_context_tokens": 262_144,
    }
    output = StringIO()
    footer = display_render.ContextUsageFooter(output)

    assert not footer.update(telemetry)
    assert output.getvalue() == ""
    assert footer.finalize()
    assert output.getvalue() == "context  [█░░░░░░░░░░░░░░░░░░░] 7%\n"


def test_sigil_display_stream_renderer_factory_selects_output_mode() -> None:
    assert isinstance(
        display_render.create_stream_renderer(StringIO()),
        display_render.TerminalStreamRenderer,
    )
    assert isinstance(
        display_render.create_stream_renderer(TtyBuffer()),
        display_render.RichStreamRenderer,
    )


def test_sigil_display_rich_stream_renderer_renders_markdown() -> None:
    output = TtyBuffer()
    renderer = display_render.RichStreamRenderer(output, refresh_interval=0)

    renderer.content_delta("Hello ")
    renderer.content_delta("**world**")
    renderer.finish()

    text = visible_terminal_text(output.getvalue())
    assert "Hello world" in text
    assert "**world**" not in text


def test_sigil_display_rich_stream_renderer_wraps_with_left_padding() -> None:
    output = TtyBuffer()
    renderer = display_render.RichStreamRenderer(
        output,
        width=24,
        refresh_interval=0,
    )

    renderer.content_delta("alpha beta gamma delta epsilon")
    renderer.finish()

    lines = [
        line.rstrip()
        for line in visible_terminal_text(output.getvalue()).splitlines()
        if line.strip()
    ]
    assert "  alpha beta gamma delta" in lines
    assert "  epsilon" in lines


def test_sigil_display_rich_stream_renderer_finalizes_trace_boundaries() -> None:
    output = TtyBuffer()
    renderer = display_render.RichStreamRenderer(output, refresh_interval=0)

    renderer.content_delta("First")
    renderer.ensure_trace_boundary()
    assert renderer.live is None
    assert renderer.buffer == []
    assert renderer.wrote_text is False

    renderer.content_delta("Second")
    renderer.finish()

    text = visible_terminal_text(output.getvalue())
    assert "First" in text
    assert "Second" in text
    assert renderer.live is None


def test_sigil_display_thinking_status_updates_and_clears(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    now = 0.0

    def clock() -> float:
        return now

    with display_render.ThinkingStatus(output, interval=60, clock=clock) as status:
        now = 10.4
        status.refresh()

    text = output.getvalue()
    assert "\n\r\x1b[2K  prefill 0s" in text
    assert "\n\r\x1b[2K  prefill 10s" in text
    assert text.endswith("\r\x1b[2K\x1b[1A\r\x1b[2K")


def test_sigil_display_thinking_status_is_muted(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TtyBuffer()

    with display_render.ThinkingStatus(output, interval=60):
        pass

    assert f"{MUTED}  prefill 0s{RESET}" in (output.getvalue())


def test_sigil_display_thinking_status_includes_context_detail(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with display_render.ThinkingStatus(
        output,
        interval=60,
        detail=lambda: "context  [█░░░░░░░░░░░░░░░░░░░] 7%",
    ):
        pass

    assert (
        "\n\r\x1b[2K  context  [█░░░░░░░░░░░░░░░░░░░] 7%\n  prefill 0s"
        in output.getvalue()
    )
    assert output.getvalue().endswith("\r\x1b[2K\x1b[1A\r\x1b[2K\x1b[1A\r\x1b[2K")


def test_sigil_display_thinking_status_skips_non_tty() -> None:
    output = StringIO()

    with display_render.ThinkingStatus(output):
        pass

    assert output.getvalue() == ""


def test_sigil_display_thinking_status_renders_reasoning_tail(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with display_render.ThinkingStatus(output, interval=60, clock=lambda: 0.0) as s:
        s.reasoning_delta("the user wants a summary\nso check ")
        s.reasoning_delta("the recent diff")
        s.refresh()

    text = output.getvalue()
    assert "  the user wants a summary\n" in text
    assert "  so check the recent diff\n\n  thinking 0s" in text


def test_sigil_display_thinking_status_shows_prefill_before_reasoning(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with display_render.ThinkingStatus(output, interval=60, clock=lambda: 0.0) as s:
        assert "  prefill 0s" in output.getvalue()
        assert "thinking 0s" not in output.getvalue()
        s.reasoning_delta("checking the request")
        s.refresh()

    text = output.getvalue()
    assert "  checking the request\n\n  thinking 0s" in text


def test_sigil_display_thinking_status_tail_keeps_last_lines(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with display_render.ThinkingStatus(
        output, interval=60, clock=lambda: 0.0, reasoning_lines=3
    ) as s:
        s.reasoning_delta("\n".join(f"step-{i}" for i in range(1, 9)))
        s.refresh()

    text = output.getvalue()
    assert "step-1" not in text
    assert "step-5" not in text
    assert "  step-6\n  step-7\n  step-8\n\n  thinking 0s" in text


def test_sigil_display_thinking_status_truncates_long_reasoning_lines(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with display_render.ThinkingStatus(
        output, interval=60, clock=lambda: 0.0, width=20
    ) as s:
        s.reasoning_delta("a reasoning line far longer than the terminal width")
        s.refresh()

    rendered = [
        line
        for line in output.getvalue().split("\n")
        if "a reasoning" in line or "a reasonin" in line
    ]
    assert rendered
    assert all(len(line.replace("\r\x1b[2K", "")) < 20 for line in rendered)


def test_sigil_display_thinking_tail_renders_iris_italic(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TtyBuffer()

    with display_render.ThinkingStatus(output, interval=60, clock=lambda: 0.0) as s:
        s.reasoning_delta("pondering")
        s.refresh()

    assert f"{ITALIC}{IRIS}  pondering{RESET}" in output.getvalue()


def test_sigil_display_thinking_status_repaints_on_new_reasoning(monkeypatch) -> None:
    # New reasoning within the same second must repaint; the seconds
    # short-circuit alone would hold the tail frozen for the interval.
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with display_render.ThinkingStatus(output, interval=60, clock=lambda: 0.0) as s:
        s.reasoning_delta("first thought")
        s.refresh()
        s.reasoning_delta("\nsecond thought")
        s.refresh()

    text = output.getvalue()
    assert "first thought" in text
    assert "second thought" in text


def test_sigil_display_thinking_status_erases_reasoning_without_summary(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    now = 0.0

    with display_render.ThinkingStatus(output, interval=60, clock=lambda: now) as s:
        s.reasoning_delta("hmm")
        now = 12.3
        s.refresh()

    text = output.getvalue()
    assert "hmm" in text
    assert "thought for" not in text
    assert text.endswith("\r\x1b[2K\x1b[1A\r\x1b[2K\x1b[1A\r\x1b[2K")


def test_sigil_display_thinking_status_no_summary_without_reasoning(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with display_render.ThinkingStatus(output, interval=60):
        pass

    assert "thought for" not in output.getvalue()


def test_sigil_display_thinking_status_no_summary_on_error_exit(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with pytest.raises(RuntimeError):
        with display_render.ThinkingStatus(output, interval=60) as s:
            s.reasoning_delta("hmm")
            raise RuntimeError("aborted")

    text = output.getvalue()
    assert "thought for" not in text
    assert text.endswith("\x1b[2K")


def test_sigil_display_thinking_trace_opt_out(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("SIGIL_THINKING_TRACE", "0")
    output = TtyBuffer()
    now = 0.0

    with display_render.ThinkingStatus(output, interval=60, clock=lambda: now) as s:
        s.reasoning_delta("secret reasoning")
        now = 5.0
        s.refresh()

    text = output.getvalue()
    assert "secret reasoning" not in text
    assert "thought for" not in text
    assert "thinking 5s" in text


def test_sigil_display_summarizes_shell_results() -> None:
    assert display_summarize.shell_result_summary(
        {
            "type": "tool_result",
            "result": {
                "outcome": SHELL_HANDOFF_OUTCOME_EXECUTED,
                "executed_command": "uv run pytest",
                "status": 0,
                "shell_turns": [{"command": "uv run pytest"}],
            },
        }
    ) == ["❯ shell  captured", "  uv run pytest", "  exit 0 · 1 shell turn"]
    assert display_summarize.shell_result_summary(
        {
            "type": "tool_result",
            "result": {
                "outcome": SHELL_HANDOFF_OUTCOME_CANCELLED,
                "expected_command": "uv run pytest",
                "actual_command": "uv run pytest -q",
            },
        }
    ) == [
        "❯ shell  changed",
        "  expected: uv run pytest",
        "  ran:      uv run pytest -q",
    ]


def transcript_console() -> tuple[StringIO, Console]:
    output = StringIO()
    return output, Console(file=output, force_terminal=False, width=80)


def test_transcript_renders_conversation_blocks() -> None:
    output, console = transcript_console()
    events = [
        {"type": "user_message", "content": "what is sigil?"},
        {
            "type": "model",
            "content": "It is a **shell assistant**.",
            "prompt_trace": {"prompt_object_id": "sha256:abcdef1234567890"},
        },
        {"type": "tool_call", "name": "read", "input": {"path": "README.md"}},
        {
            "type": "tool_result",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "line one\nline two"}],
            },
        },
        {"type": "turn_aborted", "content": "(turn aborted: model down)"},
    ]

    display_render.render_transcript(events, console=console)
    text = output.getvalue()

    assert "you" in text
    assert "what is sigil?" in text
    assert "sigil" in text
    assert "abcdef12" in text
    assert "shell assistant" in text
    assert "**" not in text
    assert "read README.md" in text
    assert "2 lines" in text
    assert "(turn aborted: model down)" in text
    assert "╭" in text


def test_transcript_joins_results_to_their_calls() -> None:
    output, console = transcript_console()
    events = [
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "grep",
            "input": {"pattern": "todo"},
        },
        {
            "type": "tool_call",
            "id": "call-2",
            "name": "ls",
            "input": {"path": "src"},
        },
        {
            "type": "tool_result",
            "tool_call_id": "call-2",
            "result": {"ok": True, "metadata": {"entries": 4}},
        },
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "result": {"ok": True, "metadata": {"matches": 0}},
        },
    ]

    display_render.render_transcript(events, console=console)
    text = output.getvalue()

    assert "grep todo — 0 matches" in text
    assert "ls src — 4 entries" in text
    assert text.count("0 matches") == 1
    assert text.count("4 entries") == 1


def test_transcript_drops_failed_results_to_marked_lines() -> None:
    output, console = transcript_console()
    events = [
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "read",
            "input": {"path": "skills/voice"},
        },
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "result": {"ok": False, "message": "[Errno 21] Is a directory"},
        },
    ]

    display_render.render_transcript(events, console=console)
    text = output.getvalue()

    assert "→ read skills/voice" in text
    assert "✗ [Errno 21] Is a directory" in text
    assert "—" not in text


def test_transcript_strips_redundant_failure_prefix_when_joined() -> None:
    output, console = transcript_console()
    events = [
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "read",
            "input": {"path": "skills/voice"},
        },
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "result": {
                "ok": False,
                "error": {
                    "code": "read-failed",
                    "message": "[Errno 21] Is a directory",
                },
            },
        },
    ]

    display_render.render_transcript(events, console=console)
    text = output.getvalue()

    assert "✗ [Errno 21] Is a directory" in text
    assert "read-failed" not in text


def test_transcript_keeps_tool_exchanges_contiguous() -> None:
    output, console = transcript_console()
    events = [
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "grep",
            "input": {"pattern": "todo"},
        },
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "result": {"ok": True, "metadata": {"matches": 0}},
        },
        {
            "type": "tool_call",
            "id": "call-2",
            "name": "ls",
            "input": {"path": "src"},
        },
        {
            "type": "tool_result",
            "tool_call_id": "call-2",
            "result": {"ok": True, "metadata": {"entries": 4}},
        },
        {"type": "model", "content": "done"},
    ]

    display_render.render_transcript(events, console=console)
    text = output.getvalue()

    assert "→ grep todo — 0 matches\n→ ls src — 4 entries\n\n" in text


def test_transcript_renders_unmatched_results_standalone() -> None:
    output, console = transcript_console()
    events = [
        {
            "type": "tool_result",
            "tool_call_id": "call-9",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "line one\nline two"}],
            },
        },
    ]

    display_render.render_transcript(events, console=console)

    assert "2 lines" in output.getvalue()


def test_transcript_renders_tool_calls_embedded_in_assistant_messages() -> None:
    output, console = transcript_console()
    events = [
        {
            "type": "model",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "grep",
                        "arguments": '{"pattern": "todo"}',
                    },
                }
            ],
        },
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "grep",
            "input": {"pattern": "todo"},
        },
        {
            "type": "tool_call",
            "id": "call-2",
            "name": "ls",
            "input": {"path": "src"},
        },
    ]

    display_render.render_transcript(events, console=console)
    text = output.getvalue()

    assert text.count("grep todo") == 1
    assert "ls src" in text


def test_transcript_skips_noise_and_empty_events() -> None:
    output, console = transcript_console()
    events = [
        {"type": "model_usage", "usage": {"total_tokens": 999}},
        {"type": "model", "content": ""},
        {"role": "user", "content": "prior question"},
    ]

    display_render.render_transcript(events, console=console)
    text = output.getvalue()

    assert "999" not in text
    assert "prior question" in text


def test_transcript_renders_assistant_without_prompt_trace() -> None:
    output, console = transcript_console()

    display_render.render_transcript(
        [{"type": "model", "content": "plain answer"}],
        console=console,
    )

    assert "plain answer" in output.getvalue()


def test_transcript_renders_reasoning_before_answer() -> None:
    output, console = transcript_console()

    display_render.render_transcript(
        [
            {
                "type": "model",
                "reasoning": "the user wants the short version",
                "content": "Here it is.",
            }
        ],
        console=console,
    )
    text = output.getvalue()

    assert "the user wants the short version" in text
    assert text.index("the user wants the short version") < text.index("Here it is.")


def test_transcript_reasoning_renders_markdown_in_italic_magenta() -> None:
    blocks = display_render.transcript_assistant_block(
        {
            "type": "model",
            "reasoning": "weighing **options**",
            "content": "done",
        },
        set(),
        {},
    )

    reasoning = blocks[0]
    assert not isinstance(reasoning, Panel)
    assert isinstance(reasoning, Markdown)
    # Named magenta lands on iris under a Rose Pine terminal, matching the
    # live thinking tail: one color for sigil's thinking voice.
    assert reasoning.style == "italic magenta"


def test_transcript_dims_user_scaffolding_sections() -> None:
    content = (
        "Recent shell activity:\n  git branch (exit 0)\n\n"
        "Question:\nwhatever\n\n"
        "cwd:\n/Users/remilouf/projects/sigil"
    )

    body = display_render.user_message_text(content)

    assert body.plain == content
    dimmed = "".join(
        body.plain[span.start : span.end] for span in body.spans if span.style == "dim"
    )
    assert "Recent shell activity:" in dimmed
    assert "git branch (exit 0)" in dimmed
    assert "cwd:" in dimmed
    assert "/Users/remilouf/projects/sigil" in dimmed
    assert "Question:" in dimmed
    assert "whatever" not in dimmed


def test_transcript_keeps_plain_user_message_undimmed() -> None:
    body = display_render.user_message_text("summarize notes.md")

    assert body.plain == "summarize notes.md"
    assert not body.spans


def test_transcript_skips_empty_reasoning_panel() -> None:
    blocks = display_render.transcript_assistant_block(
        {"type": "model", "reasoning": "", "content": "done"},
        set(),
        {},
    )

    assert len(blocks) == 1


def trace_object(kind: str, data: dict, links: tuple = ()) -> zeta_trace.Object:
    return zeta_trace.Object(
        kind=kind, schema=f"zeta.{kind}.v1", data=data, links=links
    )


def test_trace_summary_shortens_content_addressed_ids() -> None:
    assert (
        display_summarize.short_trace_id("sha256:" + "ab12cd34" + "0" * 56)
        == "ab12cd34"
    )
    assert display_summarize.short_trace_id("ab12cd34ef") == "ab12cd34"


def test_trace_summary_counts_prompt_components_and_tokens() -> None:
    store = zeta_trace.InMemoryStore()
    component = trace_object(
        "user_message", {"message": {"role": "user", "content": "fix the test"}}
    )
    component_id = store.put_object(component)
    prompt = trace_object("prompt", {"payload_sha256": "sha256:feed"}, (component_id,))

    summary = display_summarize.trace_object_summary(
        prompt, get_object=store.get_object
    )

    assert summary.startswith("1 component")
    assert "tok" in summary


def test_trace_summary_heads_assistant_text_and_tool_calls() -> None:
    answered = trace_object(
        "assistant_message",
        {"message": {"role": "assistant", "content": "first line\nsecond line"}},
    )
    calling = trace_object(
        "assistant_message",
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read"}}],
            }
        },
    )

    assert display_summarize.trace_object_summary(answered) == "first line"
    assert "read" in display_summarize.trace_object_summary(calling)


def test_trace_summary_labels_tool_calls_and_results() -> None:
    call = trace_object(
        "tool_call", {"name": "bash", "input": {"command": "uv run pytest"}}
    )
    result = trace_object(
        "tool_result",
        {
            "name": "bash",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "3 passed"}],
            },
        },
    )
    failed = trace_object(
        "tool_result",
        {
            "name": "read",
            "result": {
                "ok": False,
                "error": {"code": "not-found", "message": "no file"},
            },
        },
    )

    assert display_summarize.trace_object_summary(call) == "bash uv run pytest"
    assert display_summarize.trace_object_summary(result) == "bash · ok · 3 passed"
    assert "not-found" in display_summarize.trace_object_summary(failed)


def test_trace_summary_reads_run_event_type_and_component_messages() -> None:
    event = trace_object(
        "run_event", {"event": {"type": "user_message"}, "previous_event_object_id": ""}
    )
    component = trace_object(
        "system_prompt", {"message": {"role": "system", "content": "You are Zeta."}}
    )
    opaque = trace_object("tool_descriptor_set", {"representation": "tools"})

    assert display_summarize.trace_object_summary(event) == "user_message"
    assert display_summarize.trace_object_summary(component) == "You are Zeta."
    assert (
        display_summarize.trace_object_summary(opaque) == "zeta.tool_descriptor_set.v1"
    )
