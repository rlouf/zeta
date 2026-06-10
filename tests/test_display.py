"""Terminal rendering tests."""

from __future__ import annotations

from io import StringIO

from _zeta_helpers import (
    TtyBuffer,
    visible_terminal_text,
)

from sigil import display as sigil_display
from sigil.protocols import (
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
    SHELL_PROMPT_HANDOFF_TYPE,
)


def test_sigil_display_summarizes_tool_results() -> None:
    assert sigil_display.tool_result_summary(
        "bash",
        {
            "ok": True,
            "handoff": {
                "type": SHELL_PROMPT_HANDOFF_TYPE,
                "command": "uv run pytest",
            },
        },
    ) == ["staged"]
    assert sigil_display.tool_result_summary(
        "bash",
        {
            "ok": True,
            "metadata": {"mode": "direct", "status": 0},
        },
    ) == ["succeeded"]
    assert sigil_display.tool_result_summary(
        "bash",
        {
            "ok": False,
            "metadata": {"mode": "direct", "status": 2},
        },
    ) == ["failed · exit 2"]
    assert sigil_display.tool_result_summary(
        "read",
        {"ok": True, "content": [{"type": "text", "text": "a\nb\n"}]},
    ) == ["2 lines"]
    assert sigil_display.tool_result_summary(
        "read",
        {
            "ok": False,
            "error": {
                "code": "read-failed",
                "message": "[Errno 2] No such file or directory: 'missing.md'",
            },
        },
    ) == ["read-failed: [Errno 2] No such file or directory: 'missing.md'"]
    assert sigil_display.tool_result_summary(
        "write",
        {
            "ok": True,
            "metadata": {"mode": "direct", "path": "notes.txt"},
        },
    ) == ["wrote · notes.txt"]
    assert sigil_display.tool_result_summary(
        "grep",
        {"ok": True, "content": [{"type": "text", "text": "a.py:1:x\nb.py:2:y\n"}]},
    ) == ["2 matches · 2 files"]
    assert sigil_display.tool_result_summary(
        "grep",
        {
            "ok": True,
            "content": [{"type": "text", "text": "a.py:1:x\n"}],
            "metadata": {"matches": 10, "files": 3, "truncated": True},
        },
    ) == ["10 matches · 3 files · truncated"]


def test_sigil_display_summarizes_current_context_estimate() -> None:
    line = sigil_display.context_usage_line(
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
        sigil_display.context_usage_line(
            {"usage": {"prompt_tokens": 18_432, "completion_tokens": 391}}
        )
        == ""
    )
    assert (
        sigil_display.context_usage_line(
            {"estimated_context_tokens": 200, "model_context_tokens": 1_000}
        )
        == "context  [████░░░░░░░░░░░░░░░░] 20% est."
    )


def test_sigil_display_context_usage_footer_estimates_tool_result_tokens() -> None:
    output = StringIO()
    footer = sigil_display.ContextUsageFooter(output)
    base_telemetry = {
        "usage": {"prompt_tokens": 100, "completion_tokens": 0},
        "model_context_tokens": 1_000,
    }
    result = {"ok": True, "content": [{"type": "text", "text": "x" * 200}]}

    footer.update(base_telemetry)
    footer.update_for_tool_result(None, result)

    estimated_tokens = 100 + sigil_display.estimated_tool_result_context_tokens(result)
    assert footer.current_line() == sigil_display.context_usage_line(
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
    footer = sigil_display.ContextUsageFooter(StringIO())
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

    expected_tokens = 420 + sigil_display.estimated_tool_result_context_tokens(
        fresh_result
    )
    assert footer.current_line() == sigil_display.context_usage_line(
        {
            "estimated_context_tokens": expected_tokens,
            "model_context_tokens": 1_000,
        }
    )
    assert sigil_display.context_usage_line({"model_context_tokens": 262_144}) == ""
    assert (
        sigil_display.context_usage_line(
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
    footer = sigil_display.ContextUsageFooter(output)

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
    footer = sigil_display.ContextUsageFooter(output)

    assert not footer.update(telemetry)
    assert output.getvalue() == ""
    assert footer.finalize()
    assert output.getvalue() == "context  [█░░░░░░░░░░░░░░░░░░░] 7%\n"


def test_sigil_display_stream_renderer_factory_selects_output_mode() -> None:
    assert isinstance(
        sigil_display.create_stream_renderer(StringIO()),
        sigil_display.TerminalStreamRenderer,
    )
    assert sigil_display.create_stream_renderer(StringIO(), json_output=True) is None
    assert isinstance(
        sigil_display.create_stream_renderer(TtyBuffer()),
        sigil_display.RichStreamRenderer,
    )


def test_sigil_display_rich_stream_renderer_renders_markdown() -> None:
    output = TtyBuffer()
    renderer = sigil_display.RichStreamRenderer(output, refresh_interval=0)

    renderer.content_delta("Hello ")
    renderer.content_delta("**world**")
    renderer.finish()

    text = visible_terminal_text(output.getvalue())
    assert "Hello world" in text
    assert "**world**" not in text


def test_sigil_display_rich_stream_renderer_wraps_with_left_padding() -> None:
    output = TtyBuffer()
    renderer = sigil_display.RichStreamRenderer(
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
    renderer = sigil_display.RichStreamRenderer(output, refresh_interval=0)

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

    with sigil_display.ThinkingStatus(output, interval=60, clock=clock) as status:
        now = 10.4
        status.refresh()

    text = output.getvalue()
    assert "\n\r\x1b[2K  thinking 0s" in text
    assert "\n\r\x1b[2K  thinking 10s" in text
    assert text.endswith("\r\x1b[2K\x1b[1A\r\x1b[2K")


def test_sigil_display_thinking_status_is_muted(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TtyBuffer()

    with sigil_display.ThinkingStatus(output, interval=60):
        pass

    assert f"{sigil_display.MUTED}  thinking 0s{sigil_display.RESET}" in (
        output.getvalue()
    )


def test_sigil_display_thinking_status_includes_context_detail(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with sigil_display.ThinkingStatus(
        output,
        interval=60,
        detail=lambda: "context  [█░░░░░░░░░░░░░░░░░░░] 7%",
    ):
        pass

    assert (
        "\n\r\x1b[2K  context  [█░░░░░░░░░░░░░░░░░░░] 7%\n  thinking 0s"
        in output.getvalue()
    )
    assert output.getvalue().endswith("\r\x1b[2K\x1b[1A\r\x1b[2K\x1b[1A\r\x1b[2K")


def test_sigil_display_thinking_status_skips_non_tty() -> None:
    output = StringIO()

    with sigil_display.ThinkingStatus(output):
        pass

    assert output.getvalue() == ""


def test_sigil_display_summarizes_shell_results() -> None:
    assert sigil_display.shell_result_summary(
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
    assert sigil_display.shell_result_summary(
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
