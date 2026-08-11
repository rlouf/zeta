//! Defines authored declarations for Zeta agent systems.
//!
//! Callers provide exact Markdown bytes and a logical source path. The crate
//! returns validated declaration values with a stable content identity.

mod error;
mod parse;
mod spec;

pub use error::{SpecError, SpecErrorKind};
pub use parse::parse_agent;
pub use spec::{
    matches, scheduled_event_type, AgentSpec, EgressBinding, ExecutorSpec, IngressBinding,
    ModelSpec, RetrySpec, ScheduleEntry,
};
