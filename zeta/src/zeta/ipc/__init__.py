"""JSON-RPC messages, framing, peer support, and process supervision."""

from zeta.ipc.client import EventType, ProviderError, SourceEvent, run_peer
from zeta.ipc.framing import FrameReader, FrameViolation, decode_frame, encode_frame
from zeta.ipc.messages import (
    PROTOCOL_VERSION,
    MessageError,
    error_response,
    notification,
    request,
    success_response,
    validate_message,
)
from zeta.ipc.supervisor import (
    PeerCommand,
    ProviderCallError,
    PublishRequest,
    SubprocessPeer,
)

__all__ = [
    "EventType",
    "FrameReader",
    "FrameViolation",
    "MessageError",
    "PROTOCOL_VERSION",
    "PeerCommand",
    "ProviderCallError",
    "ProviderError",
    "PublishRequest",
    "SourceEvent",
    "SubprocessPeer",
    "decode_frame",
    "encode_frame",
    "error_response",
    "notification",
    "request",
    "run_peer",
    "success_response",
    "validate_message",
]
