"""Tiny name -> class registry shared by adapters and policies."""
from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")


class Registry:
    def __init__(self, kind: str):
        self.kind = kind
        self._items: dict[str, type] = {}

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        def decorate(cls: type[T]) -> type[T]:
            if name in self._items:
                raise ValueError(f"duplicate {self.kind} {name!r}")
            cls.name = name
            self._items[name] = cls
            return cls
        return decorate

    def create(self, name: str, params: dict, **context):
        try:
            cls = self._items[name]
        except KeyError:
            known = ", ".join(sorted(self._items)) or "none"
            raise KeyError(f"unknown {self.kind} {name!r}; registered: {known}") from None
        return cls(params=params, **context)

    def names(self) -> list[str]:
        return sorted(self._items)
