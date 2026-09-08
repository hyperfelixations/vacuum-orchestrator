"""Canonical integrity envelope for critical orchestration state."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, TypeAlias, cast

from ..domain.errors import StorageIntegrityError

JsonObject: TypeAlias = dict[str, Any]


def _digest(payload: JsonObject) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(encoded).hexdigest()


def seal_snapshot(payload: JsonObject) -> JsonObject:
    """Return a detached payload with a canonical SHA-256 integrity digest."""
    detached = cast(JsonObject, json.loads(json.dumps(payload, allow_nan=False)))
    return {"payload": detached, "integrity_sha256": _digest(detached)}


def verify_snapshot(envelope: JsonObject) -> JsonObject:
    """Verify and return a detached snapshot payload."""
    payload = envelope.get("payload")
    digest = envelope.get("integrity_sha256")
    if not isinstance(payload, dict) or not isinstance(digest, str):
        raise StorageIntegrityError("invalid_snapshot_envelope")
    typed_payload = cast(JsonObject, payload)
    if _digest(typed_payload) != digest:
        raise StorageIntegrityError("snapshot_digest_mismatch")
    return cast(JsonObject, json.loads(json.dumps(typed_payload, allow_nan=False)))
