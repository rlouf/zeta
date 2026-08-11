"""JSON-RPC messages, framing, peer support, and process supervision."""

from zeta.ipc.connection import JsonRpcConnection, JsonRpcRouter, RpcError
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
    "FrameReader",
    "FrameViolation",
    "JsonRpcConnection",
    "JsonRpcRouter",
    "MessageError",
    "PROTOCOL_VERSION",
    "PeerCommand",
    "ProviderCallError",
    "PublishRequest",
    "RpcError",
    "SubprocessPeer",
    "decode_frame",
    "encode_frame",
    "error_response",
    "notification",
    "request",
    "success_response",
    "validate_message",
]
