from __future__ import annotations

import threading
import unittest

from workspace_guard_mcp.result_cache import ResultCache, ResultCacheMiss


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ResultCacheTests(unittest.TestCase):
    def test_put_get_and_utf8_byte_accounting(self) -> None:
        cache = ResultCache(max_item_bytes=64, max_total_bytes=128)
        ref = cache.put_text("中文🙂")

        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.size_bytes, len("中文🙂".encode()))
        self.assertEqual(cache.get(ref.result_id).content, "中文🙂")
        self.assertEqual(cache.total_bytes, ref.size_bytes)
        self.assertEqual(cache.accounted_bytes, cache.total_bytes)

    def test_entry_limit_uses_lru_without_sliding_ttl(self) -> None:
        clock = _FakeClock()
        cache = ResultCache(
            max_entries=2,
            max_item_bytes=32,
            max_total_bytes=64,
            ttl_seconds=10,
            clock=clock,
        )
        first = cache.put_text("first")
        second = cache.put_text("second")
        assert first is not None and second is not None

        cache.get(first.result_id)
        third = cache.put_text("third")
        assert third is not None

        with self.assertRaises(ResultCacheMiss):
            cache.get(second.result_id)
        self.assertEqual(cache.get(first.result_id).content, "first")
        clock.advance(10)
        with self.assertRaises(ResultCacheMiss):
            cache.get(first.result_id)
        with self.assertRaises(ResultCacheMiss):
            cache.get(third.result_id)

    def test_aggregate_byte_limit_evicts_lru(self) -> None:
        cache = ResultCache(max_entries=8, max_item_bytes=8, max_total_bytes=10)
        first = cache.put_text("123456")
        second = cache.put_text("abcdef")
        assert first is not None and second is not None

        with self.assertRaises(ResultCacheMiss):
            cache.get(first.result_id)
        self.assertEqual(cache.get(second.result_id).content, "abcdef")
        self.assertLessEqual(cache.total_bytes, 10)
        self.assertEqual(cache.accounted_bytes, cache.total_bytes)

    def test_max_item_is_not_cached(self) -> None:
        cache = ResultCache(max_item_bytes=4, max_total_bytes=8)

        self.assertIsNone(cache.put_text("12345"))
        self.assertEqual(cache.entry_count, 0)
        self.assertEqual(cache.total_bytes, 0)

    def test_owner_scope_is_required_when_entry_is_scoped(self) -> None:
        cache = ResultCache(max_item_bytes=32, max_total_bytes=64)
        ref = cache.put_text("private", owner_scope="subject:a\x1fclient:c")
        assert ref is not None

        with self.assertRaises(ResultCacheMiss):
            cache.get(ref.result_id)
        with self.assertRaises(ResultCacheMiss):
            cache.get(ref.result_id, owner_scope="subject:b\x1fclient:c")
        self.assertEqual(
            cache.get(ref.result_id, owner_scope="subject:a\x1fclient:c").content,
            "private",
        )

    def test_invalid_ids_fail_without_path_interpretation(self) -> None:
        cache = ResultCache()
        invalid = (
            "",
            "../secret",
            "%2e%2e",
            "foo/bar",
            "foo\\bar",
            "a" * 33,
            "a\x00b",
            "a∕b",
        )
        for result_id in invalid:
            with self.subTest(result_id=result_id):
                with self.assertRaises(ResultCacheMiss):
                    cache.get(result_id)

    def test_collision_regenerates_without_overwrite(self) -> None:
        tokens = iter(("a" * 32, "a" * 32, "b" * 32))
        cache = ResultCache(token_factory=lambda: next(tokens))
        first = cache.put_text("first")
        second = cache.put_text("second")
        assert first is not None and second is not None

        self.assertEqual(first.result_id, "a" * 32)
        self.assertEqual(second.result_id, "b" * 32)
        self.assertEqual(cache.get(first.result_id).content, "first")

    def test_concurrent_put_get_preserves_invariants(self) -> None:
        cache = ResultCache(max_entries=16, max_item_bytes=64, max_total_bytes=512)
        barrier = threading.Barrier(8)
        failures: list[BaseException] = []
        failures_lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                barrier.wait()
                for iteration in range(40):
                    ref = cache.put_text(f"{index}:{iteration}:" + "x" * 16)
                    if ref is not None:
                        try:
                            cache.get(ref.result_id)
                        except ResultCacheMiss:
                            pass
            except BaseException as exc:  # pragma: no cover - assertion aid
                with failures_lock:
                    failures.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertLessEqual(cache.entry_count, 16)
        self.assertLessEqual(cache.total_bytes, 512)
        self.assertGreaterEqual(cache.total_bytes, 0)
        self.assertEqual(cache.accounted_bytes, cache.total_bytes)


if __name__ == "__main__":
    unittest.main()
