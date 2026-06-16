from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    expires_at: datetime


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = timedelta(seconds=ttl_seconds)
        self._values: dict[str, CacheEntry[T]] = {}
        self._lock = RLock()

    def set(self, key: str, value: T) -> None:
        with self._lock:
            self._cleanup_locked()
            self._values[key] = CacheEntry(value=value, expires_at=self._expiry())

    def get(self, key: str) -> T | None:
        with self._lock:
            entry = self._values.get(key)
            if not entry:
                return None
            if entry.expires_at <= self._now():
                self._values.pop(key, None)
                return None
            return entry.value

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)

    def touch(self, key: str) -> None:
        with self._lock:
            entry = self._values.get(key)
            if entry:
                entry.expires_at = self._expiry()

    def _cleanup_locked(self) -> None:
        now = self._now()
        expired = [key for key, entry in self._values.items() if entry.expires_at <= now]
        for key in expired:
            self._values.pop(key, None)

    def _expiry(self) -> datetime:
        return self._now() + self.ttl

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
