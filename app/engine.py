"""恢复演练引擎。

磁带语义（按题面实现）：

* 每盘磁带只能从头顺读；换带或回卷之后，下一次读取都从第 0 帧重新开始。
* 顺读路径上跳过的帧同样计入读带量；同一帧重复读到也重复计数。
* 提前读到、暂时不能按块号写出的块进入暂存区（内存或磁盘等价），
  暂存块数的峰值受暂存额度约束。
* 镜像只允许按块号 0、1、2…… 追加，不能回读、不能改前面的块。
* 每个块在写出前都要再核对一次 SHA-256；坏块或任一额度不足立即失败，
  失败时不产出新镜像（由上层保留上一次成品）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .storage import BLOCK_SIZE, BlockEntry, TapeSet, read_frame, sha256_hex


class RecoveryError(RuntimeError):
    """恢复失败：坏块或额度不足。"""


class QuotaExceeded(RecoveryError):
    pass


class BadBlock(RecoveryError):
    pass


class ImageSink(Protocol):
    """镜像输出端：只允许按块号顺序追加。"""

    def write_block(self, index: int, data: bytes) -> None: ...

    def close(self) -> None: ...


class FileImageSink:
    """写临时镜像文件；只有全部块写完并核对通过后才由上层原子发布。"""

    def __init__(self, tmp_path: str) -> None:
        self._tmp_path = tmp_path
        self._fh = open(tmp_path, "wb")  # noqa: SIM115 - 生命周期由 close 管理
        self._next_index = 0
        self.bytes_written = 0

    def write_block(self, index: int, data: bytes) -> None:
        if index != self._next_index:
            raise RecoveryError(
                f"镜像只允许顺序追加：期望块 {self._next_index}，收到块 {index}"
            )
        if len(data) != BLOCK_SIZE:
            raise RecoveryError(f"块 {index} 不是 {BLOCK_SIZE} 字节")
        self._fh.write(data)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._next_index += 1
        self.bytes_written += len(data)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    @property
    def next_index(self) -> int:
        return self._next_index


class MemorySink:
    """测试用内存输出端。"""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write_block(self, index: int, data: bytes) -> None:
        if index != len(self.chunks):
            raise RecoveryError(
                f"镜像只允许顺序追加：期望块 {len(self.chunks)}，收到块 {index}"
            )
        self.chunks.append(data)

    def close(self) -> None:
        return None


@dataclass
class RecoveryMetrics:
    blocks_read: int = 0           # 实际读带块数（含跳过、含重读）
    staging_peak: int = 0          # 暂存块数峰值
    tape_mounts: int = 0           # 换带次数（含第一次挂带）
    tape_switches: int = 0         # 在两盘带之间切换的次数
    rewinds: int = 0               # 同一盘带回卷次数
    bad_block: int | None = None   # 命中的坏块号（若有）
    head_position: int = 0         # 当前磁头：本盘带上“下一个待读帧”的序号
    events: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.events.append(message)


@dataclass
class RecoveryResult:
    success: bool
    set_name: str
    metrics: RecoveryMetrics
    error: str | None = None
    artifact_path: str | None = None
    blocks_written: int = 0


def _baseline_metrics(tape_set: TapeSet) -> RecoveryMetrics:
    """贪心策略（提前读到的块一律暂存）下的确定性指标，供页面预估额度。"""

    metrics = RecoveryMetrics()
    current_tape: int | None = None
    head_position = 0
    staged: dict[int, bytes] = {}
    written = 0
    n = tape_set.block_count

    for target in range(n):
        if target in staged:
            staged.pop(target, None)
            written += 1
            continue
        entry = tape_set.entry(target)
        if current_tape is None:
            current_tape = entry.tape
            head_position = 0
            metrics.tape_mounts += 1
            metrics.log(f"挂带 tape{entry.tape}")
        elif entry.tape != current_tape:
            current_tape = entry.tape
            head_position = 0
            metrics.tape_mounts += 1
            metrics.tape_switches += 1
            metrics.log(f"换带 tape{entry.tape}，从头顺读")
        elif entry.position < head_position:
            metrics.rewinds += 1
            head_position = 0
            metrics.log(f"回卷 tape{entry.tape}，从头顺读")
        # 目标帧在磁头之后：磁头自然前移，无需回卷。
        metrics.blocks_read += entry.position + 1 - head_position
        for position in range(head_position, entry.position + 1):
            frame_entry = _entry_at(tape_set, entry.tape, position)
            if (
                frame_entry.index >= written
                and frame_entry.index != target
                and frame_entry.index not in staged
            ):
                staged[frame_entry.index] = b""
        head_position = entry.position + 1
        metrics.staging_peak = max(metrics.staging_peak, len(staged))
        staged.pop(target, None)
        written += 1
    return metrics


def _entry_at(tape_set: TapeSet, tape: int, position: int) -> BlockEntry:
    for entry in tape_set.blocks:
        if entry.tape == tape and entry.position == position:
            return entry
    raise RecoveryError(f"清单缺少 tape{tape} 位置 {position} 的记录")


def baseline(tape_set: TapeSet) -> dict[str, int]:
    """返回贪心恢复所需的最小读带量与暂存峰值（坏块不影响位置推算）。"""

    metrics = _baseline_metrics(tape_set)
    return {
        "blocks_read": metrics.blocks_read,
        "staging_peak": metrics.staging_peak,
        "tape_mounts": metrics.tape_mounts,
        "tape_switches": metrics.tape_switches,
        "rewinds": metrics.rewinds,
    }


def simulate_recovery(
    tape_set: TapeSet,
    read_quota: int,
    staging_quota: int,
    sink: ImageSink | None = None,
    *,
    discard_future_blocks: bool = False,
    read_delay: float = 0.0,
    sleep: Callable[[float], None] | None = None,
) -> RecoveryResult:
    """按给定额度执行恢复演练。

    read_quota / staging_quota 为块数上限。sink 为 None 时只做可行性模拟
    （不写镜像），用于预估与单元测试。discard_future_blocks 用来模拟暂存
    资源受限、提前块被丢弃的策略，此时后续需要回卷重读。
    """

    if read_quota <= 0 or staging_quota < 0:
        raise ValueError("读带额度必须为正整数，暂存额度必须为非负整数")

    own_sink = sink is None
    if sink is None:
        sink = MemorySink()
    metrics = RecoveryMetrics()
    staged: dict[int, bytes] = {}
    current_tape: int | None = None
    head_position = 0          # 当前磁头：本盘带上“下一个待读帧”的序号
    n = tape_set.block_count
    written = 0

    def drain_staged() -> None:
        nonlocal written
        while written in staged:
            data = staged.pop(written)
            entry = tape_set.entry(written)
            if sha256_hex(data) != entry.sha256:
                raise BadBlock(f"块 {written} 写出前摘要核对失败（暂存内容损坏）")
            sink.write_block(written, data)
            metrics.log(f"块 {written} 由暂存区追加写入镜像")
            written += 1

    try:
        target = 0
        while target < n:
            # 已写出的块（可能由上一轮 drain_staged 批量推进）直接跳过。
            if target < written:
                target += 1
                continue
            # 目标块已在暂存区：核对写出，不产生读带量、不移动磁头。
            if target in staged:
                drain_staged()
                target = written
                continue

            entry = tape_set.entry(target)

            # 挂带 / 换带：磁头回到带头（位置 0）。
            if current_tape is None:
                current_tape = entry.tape
                head_position = 0
                metrics.tape_mounts += 1
                metrics.log(f"挂带 tape{entry.tape}，从头顺读")
            elif entry.tape != current_tape:
                current_tape = entry.tape
                head_position = 0
                metrics.tape_mounts += 1
                metrics.tape_switches += 1
                metrics.log(f"换带 tape{entry.tape}，从头顺读")
            elif entry.position < head_position:
                # 目标帧落在磁头之后已越过的位置：必须回卷再从头顺读。
                metrics.rewinds += 1
                head_position = 0
                metrics.log(f"回卷 tape{current_tape}，从头顺读")
            # 目标帧在磁头之后时磁头自然前移，跳过的帧同样计入读带量。
            read_from = head_position

            for position in range(read_from, entry.position + 1):
                if metrics.blocks_read >= read_quota:
                    raise QuotaExceeded(
                        f"读带额度不足：已读 {metrics.blocks_read} 块，"
                        f"上限 {read_quota} 块"
                    )
                frame_entry = _entry_at(tape_set, current_tape, position)
                data = read_frame(tape_set, current_tape, position)
                metrics.blocks_read += 1
                head_position = position + 1
                if read_delay and sleep is not None:
                    sleep(read_delay)

                # 入暂存前先核对摘要，坏块立即失败。
                if sha256_hex(data) != frame_entry.sha256:
                    metrics.bad_block = frame_entry.index
                    raise BadBlock(
                        f"坏块：镜像块 {frame_entry.index}（tape{current_tape} "
                        f"位置 {position}）SHA-256 核对失败"
                    )

                if frame_entry.index == target:
                    # 正好是当前需要的块：摘要已在上面核对，直接追加，不占暂存。
                    sink.write_block(target, data)
                    metrics.log(f"块 {target} 读带后直接追加写入镜像")
                    written += 1
                    break

                # 已经写出过的历史块（顺读/重读路径上）直接跳过，不暂存。
                # 判据是块号小于已写指针：回卷重读时块号可能仍大于 target。
                if frame_entry.index < written:
                    continue

                # 之前已暂存过的未来块（重读路径）：保留原暂存，不重复计数。
                if frame_entry.index in staged:
                    continue

                # 提前读到的未来块：进入暂存（丢弃策略下不暂存，之后回卷重读）。
                if discard_future_blocks:
                    metrics.log(
                        f"提前读到块 {frame_entry.index}，按策略丢弃，稍后回卷重读"
                    )
                    continue
                if len(staged) >= staging_quota:
                    raise QuotaExceeded(
                        f"暂存额度不足：暂存峰值将超过上限 {staging_quota} 块"
                    )
                staged[frame_entry.index] = data
                metrics.staging_peak = max(metrics.staging_peak, len(staged))
                metrics.log(
                    f"提前读到块 {frame_entry.index}，暂存（当前暂存 {len(staged)} 块）"
                )

            drain_staged()
            target = written

        if written != n:
            raise RecoveryError(f"恢复不完整：应写 {n} 块，实写 {written} 块")
        metrics.log(f"恢复完成：共读带 {metrics.blocks_read} 块，暂存峰值 "
                    f"{metrics.staging_peak} 块")
        return RecoveryResult(
            success=True,
            set_name=tape_set.name,
            metrics=metrics,
            blocks_written=written,
        )
    except RecoveryError as exc:
        metrics.log(f"恢复失败：{exc}")
        return RecoveryResult(
            success=False,
            set_name=tape_set.name,
            metrics=metrics,
            error=str(exc),
            blocks_written=written,
        )
    finally:
        if not own_sink:
            sink.close()
