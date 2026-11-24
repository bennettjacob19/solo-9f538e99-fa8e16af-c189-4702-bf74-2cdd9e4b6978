import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import simulate_recovery  # noqa: E402
from app.inspect import inspect_volume, outcome_from_recovery  # noqa: E402
from app.storage import load_set  # noqa: E402

from helpers import ORDERED_LAYOUT, SCRAMBLED_LAYOUT, build_set  # noqa: E402


def _recovery_runner(tape_set, read_quota, staging_quota=0):
    """返回一个确定性的恢复入口：照给定额度真实执行一次恢复。"""

    def run(_request):
        result = simulate_recovery(tape_set, read_quota, staging_quota)
        return outcome_from_recovery(result)

    return run


def _request_once(request, after_polls=0):
    """第 after_polls 次轮询之后开始返回同一个恢复插队请求。"""

    state = {"polls": 0}

    def poll():
        state["polls"] += 1
        return request if state["polls"] > after_polls else None

    return poll


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        build_set(self.tmp, "ordered", ORDERED_LAYOUT)
        build_set(self.tmp, "scrambled", SCRAMBLED_LAYOUT)
        build_set(self.tmp, "bad", SCRAMBLED_LAYOUT, corrupt=11)
        # 块 0 在 tape1，其余在 tape0：用于验证“恢复补齐后从带头补查”。
        build_set(
            self.tmp,
            "cross",
            [(1, 0, 0), (2, 0, 1), (0, 1, 0), (3, 1, 1)],
        )

    # ---- 纯巡检 ---------------------------------------------------------

    def test_clean_set_complete_with_exact_quota(self):
        tape_set = load_set(self.tmp, "ordered")
        result = inspect_volume(tape_set, 12)
        self.assertTrue(result.complete)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.frames_read, 12)
        self.assertEqual(result.supplied_by_recovery, [])
        self.assertEqual(result.total_verified, 12)
        self.assertEqual(sorted(result.verified_by_inspection), list(range(12)))
        self.assertEqual(result.jumps, [])

    def test_quota_one_short_reports_shortfall(self):
        tape_set = load_set(self.tmp, "ordered")
        result = inspect_volume(tape_set, 11)
        self.assertFalse(result.complete)
        self.assertEqual(result.status, "quota_exceeded")
        self.assertIn("巡检额度不足", result.error)
        self.assertEqual(result.frames_read, 11)
        self.assertEqual(result.total_verified, 11)

    def test_reads_tape_number_order(self):
        # 巡检按带号顺读：tape0 的帧读完才换 tape1。
        tape_set = load_set(self.tmp, "cross")
        result = inspect_volume(tape_set, 4)
        self.assertTrue(result.complete)
        self.assertEqual(result.frames_read, 4)
        self.assertEqual(result.verified_by_inspection, [1, 2, 0, 3])

    def test_bad_block_reports_location(self):
        tape_set = load_set(self.tmp, "bad")
        result = inspect_volume(tape_set, 100)
        self.assertFalse(result.complete)
        self.assertEqual(result.status, "bad_block")
        self.assertIsNotNone(result.bad_block)
        # sample 布局中块 11 位于 tape1 位置 0。
        self.assertEqual(result.bad_block.index, 11)
        self.assertEqual(result.bad_block.tape, 1)
        self.assertEqual(result.bad_block.position, 0)
        self.assertIn("坏块", result.error)
        self.assertIn("tape1 位置 0", result.error)

    def test_invalid_arguments(self):
        tape_set = load_set(self.tmp, "ordered")
        with self.assertRaises(ValueError):
            inspect_volume(tape_set, 0)
        with self.assertRaises(ValueError):
            inspect_volume(tape_set, 1, yield_allowance=-1)

    # ---- 恢复插队 -------------------------------------------------------

    def test_recovery_mid_failure_counts_verified_frames(self):
        # 恢复读带上限 6：写完块 0..5 后，在读取块 6 前额度耗尽而失败；
        # 它实读并核验通过的块必须计入巡检。
        tape_set = load_set(self.tmp, "ordered")
        request = {"set_name": "ordered", "read_quota": 6, "staging_quota": 0}
        result = inspect_volume(
            tape_set,
            8,  # 刚好够用：让位前巡检读 2 帧，恢复结束后从带头补查 tape1 6 帧
            poll_recovery=_request_once(request),
            run_recovery=_recovery_runner(tape_set, 6, 0),
        )
        self.assertTrue(result.complete, result.error)
        self.assertEqual(result.frames_read, 8)
        self.assertEqual(result.supplied_by_recovery, [2, 3, 4, 5])
        self.assertEqual(len(result.jumps), 1)
        jump = result.jumps[0]
        self.assertLessEqual(
            jump["extra_frames_after_request"], 2,
            "收到请求后最多再读两帧就必须让位",
        )
        # 恢复中途失败的事实也如实记录。
        self.assertFalse(jump["recovery"]["success"])
        self.assertEqual(jump["recovery"]["bad_block"], None)
        self.assertEqual(result.status, "complete")

    def test_quota_one_short_after_interjection(self):
        tape_set = load_set(self.tmp, "ordered")
        request = {"set_name": "ordered", "read_quota": 6, "staging_quota": 0}
        result = inspect_volume(
            tape_set,
            7,
            poll_recovery=_request_once(request),
            run_recovery=_recovery_runner(tape_set, 6, 0),
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.status, "quota_exceeded")
        self.assertEqual(result.frames_read, 7)
        self.assertEqual(result.supplied_by_recovery, [2, 3, 4, 5])
        self.assertEqual(result.total_verified, 11)

    def test_reread_after_recovery_counts_passed_frames(self):
        # tape0 在让位前两帧已全部核验，仍需补查的只剩 tape1：
        #   tape1 位置 0（块 0，已由恢复核验，顺读经过也计 1 帧）+ 位置 1（块 3）
        # 巡检实读量 = 让位前 2 帧 + 补查 2 帧 = 4。
        tape_set = load_set(self.tmp, "cross")
        request = {"set_name": "cross", "read_quota": 1, "staging_quota": 0}
        result = inspect_volume(
            tape_set,
            4,
            poll_recovery=_request_once(request),
            run_recovery=_recovery_runner(tape_set, 1, 0),
        )
        self.assertTrue(result.complete, result.error)
        self.assertEqual(result.supplied_by_recovery, [0])
        self.assertEqual(result.frames_read, 4)
        # 已核验帧（由恢复补齐）被顺读经过时仍计入实读量。
        self.assertNotIn(0, result.verified_by_inspection)
        self.assertIn("计入实读量", "\n".join(result.events))
        # 补查也从带头开始：不用 4 帧以外的读量，额度 3 必然不足。
        short = inspect_volume(
            tape_set,
            3,
            poll_recovery=_request_once(request),
            run_recovery=_recovery_runner(tape_set, 1, 0),
        )
        self.assertFalse(short.complete)
        self.assertEqual(short.status, "quota_exceeded")

    def test_recovery_bad_block_is_reported_by_inspection(self):
        tape_set = load_set(self.tmp, "bad")
        request = {"set_name": "bad", "read_quota": 100, "staging_quota": 12}
        result = inspect_volume(
            tape_set,
            100,
            poll_recovery=_request_once(request),
            run_recovery=_recovery_runner(tape_set, 100, 12),
        )
        self.assertEqual(result.status, "bad_block")
        self.assertIsNotNone(result.bad_block)
        self.assertEqual(result.bad_block.index, 11)
        self.assertEqual((result.bad_block.tape, result.bad_block.position), (1, 0))
        self.assertIn("恢复发现坏块", "\n".join(result.events))

    def test_interjection_only_once_per_inspection(self):
        tape_set = load_set(self.tmp, "ordered")
        requests = [
            {"set_name": "ordered", "read_quota": 6, "staging_quota": 0},
            {"set_name": "ordered", "read_quota": 12, "staging_quota": 0},
        ]
        state = {"polls": 0, "runs": 0}

        def poll():
            # 一直是“有待处理请求”，但引擎每轮巡检只应让位一次。
            state["polls"] += 1
            return requests[0]

        def run(_request):
            state["runs"] += 1
            return outcome_from_recovery(
                simulate_recovery(tape_set, 6, 0)
            )

        result = inspect_volume(
            tape_set, 12, poll_recovery=poll, run_recovery=run
        )
        self.assertEqual(state["runs"], 1)
        self.assertEqual(len(result.jumps), 1)
        self.assertTrue(result.complete)

    def test_finishes_by_itself_when_request_arrives_late(self):
        # 请求到达时只剩 1 帧：单靠巡检在读带与让位上限内能查完，必须查完。
        tape_set = load_set(self.tmp, "ordered")
        request = {"set_name": "ordered", "read_quota": 3, "staging_quota": 0}
        runs = []

        def run(_request):
            runs.append(1)
            return outcome_from_recovery(simulate_recovery(tape_set, 3, 0))

        result = inspect_volume(
            tape_set,
            12,
            poll_recovery=_request_once(request, after_polls=12),
            run_recovery=run,
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.frames_read, 12)
        self.assertEqual(result.jumps, [])
        self.assertEqual(runs, [])

    def test_different_set_recovery_not_counted(self):
        tape_set = load_set(self.tmp, "ordered")
        other = load_set(self.tmp, "scrambled")
        request = {"set_name": "scrambled", "read_quota": 12, "staging_quota": 10}
        result = inspect_volume(
            tape_set,
            14,  # 让位前 2 帧 + 补查 12 帧（其中 2 帧已核验，仍计读量）
            poll_recovery=_request_once(request),
            run_recovery=_recovery_runner(other, 12, 10),
        )
        # 仍要按共用带机让位，但别的带卷核验的帧不能算进本套巡检。
        self.assertEqual(len(result.jumps), 1)
        self.assertEqual(result.supplied_by_recovery, [])
        self.assertTrue(result.complete)
        self.assertEqual(result.frames_read, 14)
        self.assertEqual(result.verified_by_inspection, list(range(12)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
