import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import (  # noqa: E402
    MemorySink,
    baseline,
    simulate_recovery,
)
from app.storage import BLOCK_SIZE, discover_sets, load_set  # noqa: E402

from helpers import ORDERED_LAYOUT, SCRAMBLED_LAYOUT, build_set, make_block  # noqa: E402


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        build_set(self.tmp, "ordered", ORDERED_LAYOUT)
        build_set(self.tmp, "scrambled", SCRAMBLED_LAYOUT)
        build_set(self.tmp, "bad", SCRAMBLED_LAYOUT, corrupt=11)

    def test_ordered_baseline_and_success(self):
        tape_set = load_set(self.tmp, "ordered")
        est = baseline(tape_set)
        self.assertEqual(est["blocks_read"], 12)
        self.assertEqual(est["staging_peak"], 0)
        self.assertEqual(est["tape_switches"], 1)
        self.assertEqual(est["rewinds"], 0)

        sink = MemorySink()
        result = simulate_recovery(tape_set, 12, 0, sink=sink)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metrics.blocks_read, 12)
        self.assertEqual(result.metrics.staging_peak, 0)
        self.assertEqual(result.metrics.rewinds, 0)
        self.assertEqual(len(sink.chunks), 12)
        self.assertEqual(sink.chunks[0], make_block(0))
        self.assertEqual(sink.chunks[11], make_block(11))
        self.assertEqual(
            b"".join(sink.chunks), b"".join(make_block(i) for i in range(12))
        )

    def test_scrambled_baseline_metrics(self):
        tape_set = load_set(self.tmp, "scrambled")
        est = baseline(tape_set)
        sink = MemorySink()
        result = simulate_recovery(tape_set, 100, 12, sink=sink)
        self.assertTrue(result.success, result.error)
        # 预估与真实模拟必须一致（同源校验），并锁定该布局的确定指标。
        self.assertEqual(est["blocks_read"], result.metrics.blocks_read)
        self.assertEqual(est["staging_peak"], result.metrics.staging_peak)
        self.assertEqual(result.metrics.blocks_read, 12)
        self.assertEqual(result.metrics.staging_peak, 10)
        self.assertEqual(result.metrics.tape_switches, 1)
        self.assertEqual(result.metrics.rewinds, 0)
        self.assertEqual(
            b"".join(sink.chunks), b"".join(make_block(i) for i in range(12))
        )

    def test_read_quota_boundary_and_shortfall(self):
        tape_set = load_set(self.tmp, "ordered")
        ok = simulate_recovery(tape_set, 12, 0)
        self.assertTrue(ok.success)
        short = simulate_recovery(tape_set, 11, 0)
        self.assertFalse(short.success)
        self.assertIsInstance(short.error, str)
        self.assertIn("读带额度不足", short.error)
        self.assertEqual(short.metrics.blocks_read, 11)

        tape_set2 = load_set(self.tmp, "scrambled")
        need = baseline(tape_set2)["blocks_read"]
        self.assertFalse(simulate_recovery(tape_set2, need - 1, 12).success)
        self.assertTrue(simulate_recovery(tape_set2, need, 12).success)

    def test_staging_quota_shortfall(self):
        tape_set = load_set(self.tmp, "scrambled")
        # 乱序样例暂存峰值为 10；给 9 必然失败，给 10 成功。
        fail = simulate_recovery(tape_set, 100, 9)
        self.assertFalse(fail.success)
        self.assertIn("暂存额度不足", fail.error)
        ok = simulate_recovery(tape_set, 100, 10)
        self.assertTrue(ok.success)
        self.assertEqual(ok.metrics.staging_peak, 10)

    def test_bad_block_fails_and_reports_index(self):
        tape_set = load_set(self.tmp, "bad")
        result = simulate_recovery(tape_set, 100, 12)
        self.assertFalse(result.success)
        self.assertEqual(result.metrics.bad_block, 11)
        self.assertIn("坏块", result.error)
        # 块 11 是换带后顺读的第一帧，命中坏块前块 0 已写出。
        self.assertGreaterEqual(result.blocks_written, 1)

    def test_discard_policy_forces_rewind(self):
        # 两帧同带：位置 0 放块 1，位置 1 放块 0。
        # 不暂存未来块时，第一次读到块 1 只能丢弃；继续顺读到块 0 写出，
        # 之后必须回卷一次重读带头，才能取回块 1。
        layout = [(1, 0, 0), (0, 0, 1)]
        build_set(self.tmp, "rewind", layout)
        tape_set = load_set(self.tmp, "rewind")

        keep = simulate_recovery(tape_set, 10, 1)
        self.assertTrue(keep.success)
        self.assertEqual(keep.metrics.blocks_read, 2)
        self.assertEqual(keep.metrics.rewinds, 0)
        self.assertEqual(keep.metrics.staging_peak, 1)

        sink = MemorySink()
        discard = simulate_recovery(
            tape_set, 10, 0, sink=sink, discard_future_blocks=True
        )
        self.assertTrue(discard.success, discard.error)
        self.assertEqual(discard.metrics.rewinds, 1)
        # 首次顺读 2 帧 + 回卷后再读 1 帧
        self.assertEqual(discard.metrics.blocks_read, 3)
        self.assertEqual(discard.metrics.staging_peak, 0)
        self.assertEqual(sink.chunks, [make_block(0), make_block(1)])

    def test_skip_frames_count_as_reads(self):
        # 块 0 在位置 3：顺读必须经过 0、1、2 三帧，读带量为 4。
        layout = [(1, 0, 0), (2, 0, 1), (3, 0, 2), (0, 0, 3)]
        build_set(self.tmp, "skip", layout)
        tape_set = load_set(self.tmp, "skip")
        result = simulate_recovery(tape_set, 10, 3)
        self.assertTrue(result.success)
        self.assertEqual(result.metrics.blocks_read, 4)

    def test_repeated_reads_count(self):
        # 两带各一帧，块 0 在 tape1：挂 tape1 读 1 块即可完成块 0；
        # 块 1 在 tape0，换带再读 1 块。随后无更多块。
        layout = [(1, 0, 0), (0, 1, 0)]
        build_set(self.tmp, "multi", layout)
        tape_set = load_set(self.tmp, "multi")
        result = simulate_recovery(tape_set, 10, 1)
        self.assertTrue(result.success)
        self.assertEqual(result.metrics.blocks_read, 2)
        self.assertEqual(result.metrics.tape_switches, 1)

    def test_quota_error_types(self):
        tape_set = load_set(self.tmp, "ordered")
        with self.assertRaises(ValueError):
            simulate_recovery(tape_set, 0, 0)
        with self.assertRaises(ValueError):
            simulate_recovery(tape_set, 1, -1)

    def test_discover_ignores_unknown_dirs(self):
        os.makedirs(os.path.join(self.tmp, "not-a-set"))
        found = {s.name for s in discover_sets(self.tmp)}
        self.assertEqual(found, {"ordered", "scrambled", "bad"})


class BadManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_duplicate_block_rejected(self):
        build_set(self.tmp, "dup", [(0, 0, 0), (0, 0, 1)])
        with self.assertRaises(Exception):
            load_set(self.tmp, "dup")

    def test_too_many_blocks_rejected(self):
        layout = [(i, 0, i) for i in range(13)]
        build_set(self.tmp, "big", layout)
        with self.assertRaisesRegex(Exception, "最多 12"):
            load_set(self.tmp, "big")

    def test_truncated_tape_rejected(self):
        build_set(self.tmp, "trunc", [(0, 0, 0), (1, 0, 1)])
        path = os.path.join(self.tmp, "trunc", "tape0.bin")
        os.truncate(path, BLOCK_SIZE + 10)
        with self.assertRaisesRegex(Exception, "大小"):
            load_set(self.tmp, "trunc")

    def test_path_traversal_rejected(self):
        with self.assertRaises(Exception):
            load_set(self.tmp, "../escape")


if __name__ == "__main__":
    unittest.main(verbosity=2)
