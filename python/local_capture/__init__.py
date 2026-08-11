"""Local-capture ingest: the wire-format parser, the `LocalCaptureClient`
FFLogs-client shim, and the FFLogs→wire export/verify tooling.

Normative wire contract: WIRE_CONTRACT.md (repo root; mirrored in the
Meridian Companion repo). Oracle: tests/test_local_capture_replay.py.
"""
from .capture import (
    CONTRACT_VERSION,
    CaptureError,
    CaptureIncompleteError,
    LocalCapture,
    parse_capture_lines,
    parse_capture_text,
)
from .client import LocalCaptureClient
from .export import (
    RecordingClient,
    ReplayClient,
    responses_to_wire,
    serialize_ndjson,
    verify,
)

__all__ = [
    "CONTRACT_VERSION",
    "CaptureError",
    "CaptureIncompleteError",
    "LocalCapture",
    "LocalCaptureClient",
    "RecordingClient",
    "ReplayClient",
    "parse_capture_lines",
    "parse_capture_text",
    "responses_to_wire",
    "serialize_ndjson",
    "verify",
]
