"""Strict JSON primitives shared by versioned persistence codecs."""

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar, cast

from ..domain.errors import StorageIntegrityError
from .integrity import JsonObject

_EnumT = TypeVar("_EnumT", bound=StrEnum)


def _encode_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageIntegrityError("naive_datetime")
    return value.astimezone(UTC).isoformat()


def _encode_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else _encode_datetime(value)


def _decode_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(_str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageIntegrityError("naive_datetime")
    return parsed.astimezone(UTC)


def _decode_optional_datetime(value: object) -> datetime | None:
    return None if value is None else _decode_datetime(value)


def _enum_value(value: StrEnum | None) -> str | None:
    return None if value is None else value.value


def _enum(enum_type: type[_EnumT], value: object) -> _EnumT:
    return enum_type(_str(value))


def _optional_enum(enum_type: type[_EnumT], value: object) -> _EnumT | None:
    return None if value is None else _enum(enum_type, value)


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise StorageIntegrityError("expected_object")
    return cast(JsonObject, value)


def _string_mapping(value: object) -> Mapping[str, object]:
    return _object(value)


def _object_list(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        raise StorageIntegrityError("expected_list")
    return [_object(item) for item in value]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StorageIntegrityError("expected_string_list")
    return cast(list[str], value)


def _pair_list(value: object) -> list[list[object]]:
    if not isinstance(value, list):
        raise StorageIntegrityError("expected_pair_list")
    result: list[list[object]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise StorageIntegrityError("expected_pair")
        result.append(cast(list[object], item))
    return result


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise StorageIntegrityError("expected_string")
    return value


def _optional_str(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise StorageIntegrityError("expected_optional_string")
    return value


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StorageIntegrityError("expected_integer")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise StorageIntegrityError("expected_boolean")
    return value
