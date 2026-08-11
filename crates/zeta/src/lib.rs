//! Defines zeta's backend-neutral substrate and journal domains.
//!
//! [`substrate`] owns canonical content identity, content-addressed values,
//! and blob primitives. [`journal`] owns event ordering, chain identity,
//! verification, and its in-memory conformance reference. Journal may depend
//! on substrate's public API; substrate never depends on journal.

pub mod journal;
pub mod substrate;
