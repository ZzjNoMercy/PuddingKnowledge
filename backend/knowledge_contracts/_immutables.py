"""Small immutable mapping helper for frozen boundary DTOs."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any


class FrozenMap(Mapping[str, Any]):
    """A recursively immutable mapping with no mutable dict base to bypass."""

    __slots__ = ("_data",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_data", MappingProxyType(dict(values)))

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __deepcopy__(self, _memo: dict[int, object]) -> FrozenMap:
        return FrozenMap({key: deepcopy(value) for key, value in self._data.items()})


def freeze_mapping(value: Mapping[str, Any]) -> FrozenMap:
    return FrozenMap({str(key): freeze_value(item) for key, item in value.items()})


def freeze_value(value: Any) -> Any:
    """Recursively freeze JSON-shaped boundary values."""

    if isinstance(value, Mapping):
        return freeze_mapping(value)
    if isinstance(value, list):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"boundary mapping contains a non-JSON value: {type(value).__name__}")


def thaw_value(value: Any) -> Any:
    """Convert immutable boundary values into JSON-compatible containers."""

    if isinstance(value, Mapping):
        return {str(key): thaw_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [thaw_value(item) for item in value]
    return value
