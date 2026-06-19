"""Authored-agent prompt rendering and validation."""

from jinja2 import Environment, meta

from agents.events import EventEnvelope
from agents.spec import AgentSpec


class TemplateError(ValueError):
    """Raised when an authored prompt template is invalid or cannot render."""


def render_prompt(spec: AgentSpec, envelope: EventEnvelope) -> str:
    """Render an authored prompt with one root variable, ``event``."""
    try:
        template = Environment(autoescape=False).from_string(spec.instructions)
        return template.render(event=envelope.to_template_context())
    except Exception as exc:
        raise TemplateError(
            f"template render error in agent {spec.slug!r}: {exc}"
        ) from exc


def validate_prompt(spec: AgentSpec) -> None:
    """Reject templates that reference roots other than ``event``."""
    environment = Environment(autoescape=False)
    try:
        parsed = environment.parse(spec.instructions)
    except Exception as exc:
        raise TemplateError(
            f"template syntax error in agent {spec.slug!r}: {exc}"
        ) from exc
    for name in meta.find_undeclared_variables(parsed):
        if name != "event":
            raise TemplateError(
                f"agent {spec.slug!r} template references unknown variable {name!r}"
            )
