"""Abort checks for one turn.

A run stops when it is cancelled or passes its deadline. These checks read that
state and raise, so no stage decides on its own whether to continue.
"""

from __future__ import annotations

from zeta.journal.drafts import (
    turn_aborted_draft,
)
from zeta.loop.cancellation import (
    AgentRunAborted,
)
from zeta.loop.outcomes import (
    RunState,
)
from zeta.loop.request import (
    RunDependencies,
    record_runtime_event,
)


def check_run_abort(
    state: RunState,
    *,
    ctx: RunDependencies,
    check_deadline: bool = True,
) -> None:
    raise_if_agent_run_aborted(
        state,
        ctx=ctx,
        check_deadline=check_deadline,
    )


def raise_if_agent_run_aborted(
    state: RunState,
    *,
    ctx: RunDependencies,
    check_deadline: bool,
) -> None:
    reason = ctx.abort_reason(check_deadline=check_deadline)
    if reason is None:
        return
    state.note_step("abort_run")
    record_runtime_event(
        state.events,
        turn_aborted_draft(
            reason=reason,
            session_id=None,
            turn_id=None,
            caused_by=state.next_model_caused_by,
        ),
        ctx=ctx,
    )
    raise AgentRunAborted(
        reason,
        result=state.result(),
        event_recorded=True,
    )
