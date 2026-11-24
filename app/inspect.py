"""带卷巡检引擎。

巡检与恢复共用一台带机，规则（按题面实现）：

* 巡检按**带号顺读**：先从小号磁带带头读到带尾，再换下一盘；每帧都用
  清单中的 SHA-256 核对。
* 巡检有独立的**读帧额度**（不是恢复的读带额度）；顺读经过的每一帧都计入
  巡检实读量。
* 恢复可以插队，但**每轮巡检只接一次**恢复插队。收到请求后，巡检读完当前
  帧，最多再读两帧就必须让位；恢复按它自己的额度执行。
* 恢复**实读并核验通过**的帧计入同一套带卷的巡检，恢复中途失败也照算；
  恢复若发现坏块，同一套带卷的巡检也要报出该坏块的位置。
* 恢复结束后，巡检对仍需补查的磁带**从带头**重新顺读；经过的已核验帧同样
  计入巡检实读量。
* 带卷完好时，只要单靠巡检能在剩余读帧额度与“最多再读两帧”的让位限制内
  查完，巡检就必须查完（不得为了等恢复而无谓让位）。
* 全部帧核验通过才算查完；遇到坏块报位置；额度用尽仍未查完报额度不足。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .storage import TapeSet, read_frame, sha256_hex

YIELD_ALLOWANCE = 2  # 收到恢复插队请求后，巡检最多再读的帧数


@dataclass(frozen=True)
class BadBlockLocation:
    """坏块位置：镜像块号 + 所在磁带与帧位置。"""

    index: int
    tape: int
    position: int

    def describe(self) -> str:
        return (
            f"坏块：镜像块 {self.index}（tape{self.tape} 位置 {self.position}）"
        )

    def to_dict(self) -> dict[str, int]:
        return {"index": self.index, "tape": self.tape, "position": self.position}


@dataclass
class RecoveryOutcome:
    """一次恢复插队的结果（供巡检记账）。"""

    set_name: str
    verified_blocks: tuple[int, ...] = ()
    bad_block: BadBlockLocation | None = None
    info: dict[str, Any] = field(default_factory=dict)


class RecoveryRunner(Protocol):
    def __call__(self, request: Any) -> RecoveryOutcome: ...


@dataclass
class InspectionResult:
    set_name: str
    quota: int
    frames_read: int = 0                       # 巡检自己实读的帧数
    complete: bool = False                     # 全部帧核验通过
    status: str = "quota_exceeded"             # complete/bad_block/quota_exceeded/error
    error: str | None = None
    bad_block: BadBlockLocation | None = None
    verified_by_inspection: list[int] = field(default_factory=list)
    supplied_by_recovery: list[int] = field(default_factory=list)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    jumps: list[dict[str, Any]] = field(default_factory=list)
    total_verified: int = 0
    block_count: int = 0
    events: list[str] = field(default_factory=list)

    @property
    def supplied_count(self) -> int:
        return len(self.supplied_by_recovery)

    def to_dict(self) -> dict[str, Any]:
        return {
            "set_name": self.set_name,
            "quota": self.quota,
            "frames_read": self.frames_read,
            "complete": self.complete,
            "status": self.status,
            "error": self.error,
            "bad_block": self.bad_block.to_dict() if self.bad_block else None,
            "bad_block_text": (
                self.bad_block.describe() if self.bad_block else None
            ),
            "verified_by_inspection": list(self.verified_by_inspection),
            "supplied_by_recovery": list(self.supplied_by_recovery),
            "supplied_count": self.supplied_count,
            "recoveries": list(self.recoveries),
            "jumps": list(self.jumps),
            "total_verified": self.total_verified,
            "block_count": self.block_count,
            "events": list(self.events),
        }


def outcome_from_recovery(result) -> RecoveryOutcome:
    """把引擎的 RecoveryResult 转成巡检记账所需的帧集合。"""

    metrics = result.metrics
    bad = None
    if metrics.bad_block is not None:
        bad = BadBlockLocation(
            index=metrics.bad_block,
            tape=metrics.bad_block_tape if metrics.bad_block_tape is not None else -1,
            position=(
                metrics.bad_block_position
                if metrics.bad_block_position is not None
                else -1
            ),
        )
    return RecoveryOutcome(
        set_name=result.set_name,
        verified_blocks=tuple(metrics.verified_blocks),
        bad_block=bad,
        info={
            "set_name": result.set_name,
            "success": result.success,
            "error": result.error,
            "blocks_written": result.blocks_written,
            "bad_block": bad.to_dict() if bad else None,
            "metrics": {
                "blocks_read": metrics.blocks_read,
                "staging_peak": metrics.staging_peak,
                "tape_switches": metrics.tape_switches,
                "rewinds": metrics.rewinds,
                "verified_count": len(metrics.verified_blocks),
            },
        },
    )


def inspect_volume(
    tape_set: TapeSet,
    quota: int,
    *,
    read_delay: float = 0.0,
    sleep: Callable[[float], None] | None = None,
    poll_recovery: Callable[[], Any | None] | None = None,
    run_recovery: RecoveryRunner | None = None,
    on_event: Callable[[str], None] | None = None,
    on_progress: Callable[[int], None] | None = None,
    yield_allowance: int = YIELD_ALLOWANCE,
) -> InspectionResult:
    """按巡检额度扫描整套带卷，期间最多接受一次恢复插队。

    ``poll_recovery`` 在每个帧边界被调用；返回非 ``None`` 表示有一笔已受理的
    恢复插队请求。``run_recovery`` 在巡检让位时执行该请求并返回实际核验通过
    的帧集合。两者都由调用方（Web 层）提供，便于单测注入确定性行为。
    """

    if quota <= 0:
        raise ValueError("巡检帧额度必须为正整数")
    if yield_allowance < 0:
        raise ValueError("让位上限不能为负")

    result = InspectionResult(
        set_name=tape_set.name,
        quota=quota,
        block_count=tape_set.block_count,
    )
    verified: set[int] = set()

    def log(message: str) -> None:
        result.events.append(message)
        if on_event is not None:
            on_event(message)

    def positions_of(tape: int) -> list[int]:
        return sorted(
            entry.position for entry in tape_set.blocks if entry.tape == tape
        )

    def build_queue() -> list[tuple[int, int]]:
        """仍需补查的磁带从带头顺读，读到该带最后一个未核验帧为止。"""

        queue: list[tuple[int, int]] = []
        for tape in range(tape_set.tape_count):
            unverified = [
                position
                for position in positions_of(tape)
                if tape_set.entry_at(tape, position).index not in verified
            ]
            if not unverified:
                continue
            last = max(unverified)
            queue.extend((tape, position) for position in range(last + 1))
        return queue

    def apply_recovery(outcome: RecoveryOutcome) -> None:
        if outcome.set_name != tape_set.name:
            log(
                f"恢复插队针对带卷 {outcome.set_name}，不计入本套带卷 {tape_set.name}"
                " 的巡检"
            )
            return
        newly: list[int] = []
        for index in outcome.verified_blocks:
            if index not in verified and 0 <= index < tape_set.block_count:
                verified.add(index)
                newly.append(index)
        # 保持块号顺序，页面展示稳定。
        result.supplied_by_recovery.extend(sorted(newly))
        if newly:
            log(
                "恢复实读并核验通过 "
                f"{len(newly)} 个此前未巡检的帧，计入巡检：块 {sorted(newly)}"
            )
        if outcome.bad_block is not None:
            result.bad_block = outcome.bad_block
            log(f"恢复发现坏块，巡检同步上报：{outcome.bad_block.describe()}")

    queue = build_queue()
    log(
        f"开始巡检带卷 {tape_set.name}：{tape_set.block_count} 帧，"
        f"帧额度 {quota}，按带号顺读"
    )

    request_active = False
    request_payload: Any | None = None
    frames_since_request = 0
    request_frames = 0
    interjection_used = False

    # 请求在帧边界被检测到：此刻“当前帧”已读完，之后最多再读
    # yield_allowance 帧就必须让位。
    post_request_budget = yield_allowance

    def yield_to_recovery() -> None:
        """让位给已受理的恢复插队，并把恢复核验通过的帧计入巡检。"""

        nonlocal queue, request_active, request_payload
        nonlocal frames_since_request, interjection_used

        assert request_payload is not None
        log(
            f"巡检让位：请求后已再读 {frames_since_request} 帧，"
            f"待查 {len(queue)} 帧，交由恢复插队"
        )
        outcome = run_recovery(request_payload)  # type: ignore[misc]
        interjection_used = True
        result.recoveries.append(outcome.info)
        result.jumps.append(
            {
                "frames_at_request": request_frames,
                "frames_at_yield": result.frames_read,
                "extra_frames_after_request": result.frames_read - request_frames,
                "recovery": outcome.info,
            }
        )
        apply_recovery(outcome)
        # 恢复结束后，仍需补查的磁带从带头顺读。
        queue = build_queue()
        log(
            "恢复结束，巡检继续：剩余待查磁带从头顺读，"
            f"当前已实读 {result.frames_read} 帧"
        )
        request_active = False
        request_payload = None
        frames_since_request = 0

    while True:
        result.total_verified = len(verified)
        # 坏块优先上报：恢复发现的坏块也要让本轮巡检报出来。
        if result.bad_block is not None:
            result.status = "bad_block"
            result.error = result.bad_block.describe()
            log(f"巡检中止：{result.error}")
            break

        if len(verified) == tape_set.block_count:
            result.complete = True
            result.status = "complete"
            log(
                f"巡检完成：全部 {tape_set.block_count} 帧核验通过，"
                f"巡检实读 {result.frames_read} 帧，"
                f"恢复补齐 {len(result.supplied_by_recovery)} 帧"
            )
            break

        # 帧边界检测恢复插队请求；每轮巡检只接一次。
        pending = poll_recovery() if poll_recovery is not None else None
        if pending is not None and not request_active and not interjection_used:
            request_active = True
            request_payload = pending
            frames_since_request = 0
            request_frames = result.frames_read
            log(
                f"收到恢复插队请求（巡检已实读 {result.frames_read} 帧），"
                f"读完当前帧后最多再读 {yield_allowance} 帧即让位"
            )

        if request_active:
            remaining = len(queue)
            allowance_left = max(0, post_request_budget - frames_since_request)
            budget_left = quota - result.frames_read
            # 带卷完好且单靠巡检能在让位与读帧上限内查完时，必须查完。
            can_finish_by_self = (
                remaining <= allowance_left and remaining <= budget_left
            )
            must_yield = not can_finish_by_self and (
                frames_since_request >= post_request_budget or budget_left <= 0
            )
            if must_yield:
                if run_recovery is None:
                    result.status = "error"
                    result.error = "有恢复插队请求，但没有可执行的恢复入口"
                    break
                yield_to_recovery()
                continue

        if not queue:
            # 仍未有全部核验通过，但已无可读帧：清单/逻辑异常兜底。
            result.status = "error"
            result.error = "巡检计划为空但仍有帧未核验"
            log(result.error)
            break

        if result.frames_read >= quota:
            if request_active and run_recovery is not None:
                # 额度已尽：先让位给已受理的恢复，恢复可能补齐剩余帧。
                log(
                    f"巡检读帧额度已用尽（{result.frames_read}/{quota}），"
                    "先让位给恢复插队"
                )
                yield_to_recovery()
                continue
            result.status = "quota_exceeded"
            result.error = (
                f"巡检额度不足：已实读 {result.frames_read} 帧（上限 {quota}），"
                f"仍有 {tape_set.block_count - len(verified)} 帧未核验"
            )
            log(result.error)
            break

        tape, position = queue.pop(0)
        entry = tape_set.entry_at(tape, position)
        data = read_frame(tape_set, tape, position)
        result.frames_read += 1
        if request_active:
            frames_since_request += 1
        if read_delay and sleep is not None:
            sleep(read_delay)
        if on_progress is not None:
            on_progress(result.frames_read)

        if sha256_hex(data) != entry.sha256:
            result.bad_block = BadBlockLocation(
                index=entry.index, tape=tape, position=position
            )
            log(f"巡检发现{result.bad_block.describe()}")
            continue

        if entry.index not in verified:
            verified.add(entry.index)
            result.verified_by_inspection.append(entry.index)
            log(
                f"巡检通过：镜像块 {entry.index}"
                f"（tape{tape} 位置 {position}）"
            )
        else:
            log(
                f"巡检经过已核验帧：镜像块 {entry.index}"
                f"（tape{tape} 位置 {position}，计入实读量）"
            )

    result.total_verified = len(verified)
    return result
