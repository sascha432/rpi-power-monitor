"""LEGACY JSON-Lines protocol draft - superseded by the binary protocol.

The active wire format lives in ``shared.binary`` (fixed-size little-endian
frames, C-layout compatible; see ``docs/protocol.md``). This module is kept
for reference only and is NOT used by the current server.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

JSON_ENCODING = "utf-8"
FRAME_DELIMITER = b"\n"

PROTOCOL_VERSION = 1

# Message types (the "type" key of every frame)
MSG_REGISTRY = "registry"
MSG_SAMPLE = "sample"
MSG_ERROR = "error"


def encode(message: dict) -> bytes:
    """Serialize one message dict into a single JSON-Lines frame.

    TODO: implement with the shared models (Registry/Sample/ErrorMessage)
    once the transport is built.
    """
    raise NotImplementedError


def decode_frame(frame: bytes) -> dict:
    """Parse one JSON-Lines frame into a message dict."""
    raise NotImplementedError


def iter_frames(buffer: bytes) -> Iterator[dict]:
    """Yield complete frames from a (possibly partial) byte buffer."""
    raise NotImplementedError
