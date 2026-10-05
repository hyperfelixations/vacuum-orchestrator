"""Rich room and robot projections for optional clients and configuration views."""

from dataclasses import asdict
from datetime import datetime
from typing import Any

from ..domain.rooms import Room
from ..domain.types import OperationKind
from ..infrastructure.room_codec import encode_room


def present_room(room: Room, now: datetime) -> dict[str, Any]:
    """Expose policy, provenance, effective grant and derived due state."""
    result = dict(encode_room(room))
    result["released"] = room.released(now)
    result["due"] = {
        operation.value: {
            **asdict(report),
            "state": report.state.value,
            "quality": report.quality.value if report.quality else None,
            "due_at": report.due_at.isoformat() if report.due_at else None,
        }
        for operation in (OperationKind.VACUUM, OperationKind.MOP)
        for report in (room.due(operation, now),)
    }
    return result
