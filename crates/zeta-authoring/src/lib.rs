//! Defines authored declarations for Zeta agent systems.
//!
//! Callers can parse exact Markdown bytes with an explicit slug or load a
//! Markdown file whose filename supplies the slug. Both paths return the same
//! validated declaration value with a stable content identity.

mod error;
mod parse;
mod spec;

pub use error::{SpecError, SpecErrorKind};
pub use parse::{load_agent, parse_agent};
pub use spec::{
    matches, scheduled_event_type, AgentSpec, EgressBinding, ExecutorSpec, IngressBinding,
    ModelSpec, RetrySpec, ScheduleEntry,
};
