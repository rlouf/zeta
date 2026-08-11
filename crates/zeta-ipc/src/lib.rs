//! Implements the Zeta IPC protocol.
//!
//! The crate provides typed JSON-RPC 2.0 messages, bounded NDJSON framing,
//! initialization roles, and one bidirectional [`Session`] state machine.
//! The session turns incoming messages and clock ticks into explicit actions.
//!
//! # Examples
//!
//! ```
//! use zeta_ipc::{Message, RequestId};
//!
//! let message = Message::parse_str(
//!     r#"{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}"#,
//! )?;
//! let Message::Request(request) = message else {
//!     panic!("expected a request");
//! };
//! assert_eq!(request.id, RequestId::from(1_u64));
//! assert_eq!(request.method, "ping");
//! # Ok::<(), zeta_ipc::IpcError>(())
//! ```

mod error;
mod frame;
mod message;
mod session;
mod validate;

pub use error::{
    ErrorObject, IpcError, Retryability, INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST,
    METHOD_NOT_FOUND, PARSE_ERROR, SERVER_ERROR,
};
pub use frame::{Frame, FrameReader, FrameWriter, Violation, MAX_FRAME_BYTES};
pub use message::{
    ErrorResponse, EventTypeDecl, InitializeParams, InitializeResult, Message, MethodDecl,
    Notification, PeerIdentity, Request, RequestId, Role, SuccessResponse, JSONRPC_VERSION,
    MAX_INLINE_PAYLOAD_BYTES, PROTOCOL_VERSION,
};
pub use session::{Action, ResolvedRequest, RuntimeConfig, Session, ShutdownDirection};
pub use validate::validate_message;
