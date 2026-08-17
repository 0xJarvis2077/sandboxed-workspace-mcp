"""Bounded in-memory storage for already-public-safe large text results."""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

RESULT_TEXT_MIME = "text/plain; charset=utf-8"
RESULT_URI_PREFIX = "sandboxed-workspace://result/"
RESULT_URI_TEMPLATE = RESULT_URI_PREFIX + "{id}"
DEFAULT_INLINE_THRESHOLD_BYTES = 24 * 1024
DEFAULT_CACHE_MAX_ITEM_BYTES = 1024 * 1024
DEFAULT_CACHE_MAX_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_CACHE_MAX_ENTRIES = 64
DEFAULT_CACHE_TTL_SECONDS = 15 * 60.0
_TOKEN_BYTES = 24
_RESULT_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
_MAX_COLLISION_RETRIES = 16


class ResultCacheError(RuntimeError):
    """Raised when the cache cannot safely complete an internal operation."""


class ResultCacheMiss(LookupError):
    """Raised for invalid, expired, evicted, missing, or out-of-scope result IDs."""


@dataclass(frozen=True, slots=True)
class CachedResultRef:
    """Opaque reference returned after a successful cache insertion."""

    result_id: str
    uri: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class CachedResult:
    """One immutable cached public-safe text result."""

    content: str
    mime_type: str
    size_bytes: int


@dataclass(slots=True)
class _CacheEntry:
    content: str
    mime_type: str
    size_bytes: int
    created_at: float
    expires_at: float
    owner_scope: str | None


class ResultCache:
    """Thread-safe fixed-TTL LRU cache with byte and entry bounds."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        max_total_bytes: int = DEFAULT_CACHE_MAX_TOTAL_BYTES,
        max_item_bytes: int = DEFAULT_CACHE_MAX_ITEM_BYTES,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        if type(max_total_bytes) is not int or max_total_bytes <= 0:
            raise ValueError("max_total_bytes must be a positive integer")
        if type(max_item_bytes) is not int or max_item_bytes <= 0:
            raise ValueError("max_item_bytes must be a positive integer")
        if max_item_bytes > max_total_bytes:
            raise ValueError("max_item_bytes must not exceed max_total_bytes")
        if not isinstance(ttl_seconds, int | float) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self.max_entries = max_entries
        self.max_total_bytes = max_total_bytes
        self.max_item_bytes = max_item_bytes
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory or self._new_result_id
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _new_result_id() -> str:
        return secrets.token_urlsafe(_TOKEN_BYTES)

    @staticmethod
    def valid_result_id(result_id: str) -> bool:
        """Validate only the opaque token shape; never interpret it as a path."""

        return isinstance(result_id, str) and bool(_RESULT_ID.fullmatch(result_id))

    def put_text(
        self,
        content: str,
        *,
        owner_scope: str | None = None,
    ) -> CachedResultRef | None:
        """Store one safe text value, or return ``None`` when it exceeds item bounds."""

        encoded = content.encode("utf-8")
        size_bytes = len(encoded)
        if size_bytes > self.max_item_bytes or size_bytes > self.max_total_bytes:
            return None
        now = self._clock()
        expires_at = now + self.ttl_seconds
        with self._lock:
            self._evict_expired_locked(now)
            while (
                len(self._entries) >= self.max_entries
                or self._total_bytes + size_bytes > self.max_total_bytes
            ):
                self._evict_lru_locked()
            result_id = self._unique_result_id_locked()
            self._entries[result_id] = _CacheEntry(
                content=content,
                mime_type=RESULT_TEXT_MIME,
                size_bytes=size_bytes,
                created_at=now,
                expires_at=expires_at,
                owner_scope=owner_scope,
            )
            self._total_bytes += size_bytes
        return CachedResultRef(
            result_id=result_id,
            uri=RESULT_URI_PREFIX + result_id,
            size_bytes=size_bytes,
            mime_type=RESULT_TEXT_MIME,
        )

    def get(self, result_id: str, *, owner_scope: str | None = None) -> CachedResult:
        """Read without extending fixed TTL; successful reads refresh LRU order."""

        if not self.valid_result_id(result_id):
            raise ResultCacheMiss("result not found")
        now = self._clock()
        with self._lock:
            self._evict_expired_locked(now)
            entry = self._entries.get(result_id)
            if entry is None or entry.owner_scope != owner_scope:
                raise ResultCacheMiss("result not found")
            self._entries.move_to_end(result_id)
            return CachedResult(
                content=entry.content,
                mime_type=entry.mime_type,
                size_bytes=entry.size_bytes,
            )

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def accounted_bytes(self) -> int:
        """Return recomputed bytes for invariant tests without exposing MCP metrics."""

        with self._lock:
            return sum(entry.size_bytes for entry in self._entries.values())

    def _unique_result_id_locked(self) -> str:
        for _ in range(_MAX_COLLISION_RETRIES):
            result_id = self._token_factory()
            if not self.valid_result_id(result_id):
                raise ResultCacheError("result ID generator returned an invalid token")
            if result_id not in self._entries:
                return result_id
        raise ResultCacheError("result ID collision retry limit exceeded")

    def _evict_expired_locked(self, now: float) -> None:
        expired = [
            result_id
            for result_id, entry in self._entries.items()
            if now >= entry.expires_at
        ]
        for result_id in expired:
            self._remove_locked(result_id)

    def _evict_lru_locked(self) -> None:
        if not self._entries:
            return
        result_id = next(iter(self._entries))
        self._remove_locked(result_id)

    def _remove_locked(self, result_id: str) -> None:
        entry = self._entries.pop(result_id)
        self._total_bytes -= entry.size_bytes
