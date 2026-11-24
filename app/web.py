"""Flask 灾备演练 Web 界面：带卷巡检 + 镜像恢复。

页面能力：

* 列出本地带卷目录中的全部合法带卷；
* 选择一套带卷，填写**巡检最多读帧数**后启动巡检。巡检按带号顺读、
  逐帧用清单中的 SHA-256 核对；
* 巡检与恢复共用一台带机。巡检运行期间提交的恢复会作为**一次插队**排队：
  巡检读完当前帧后最多再读两帧就让位，恢复按自己提交的额度执行；
* 恢复实读并核验通过的帧计入同套带卷的巡检，恢复中途失败也算；恢复发现
  的坏块同步报给巡检；
* 恢复结束后，巡检对仍需补查的磁带从带头顺读，经过的已核验帧也计巡检读量；
* 页面列出巡检实读帧数、由恢复补齐的帧、坏块位置或额度不足原因。

普通恢复（无巡检运行时）保持原有语义：后台线程执行，成功后原子发布
`image.bin`，失败保留上次成品。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .engine import FileImageSink, RecoveryResult, baseline, simulate_recovery
from .inspect import RecoveryOutcome, inspect_volume, outcome_from_recovery
from .storage import ManifestError, discover_sets, load_set

ARTIFACT_NAME = "image.bin"
MAX_EVENTS = 200


@dataclass
class JobState:
    running: bool = False
    kind: str | None = None            # "recovery" | "inspection"
    set_name: str | None = None
    read_quota: int = 0
    staging_quota: int = 0
    inspection_quota: int = 0
    inspection_progress: int = 0       # 巡检运行中已实读帧数
    started_at: float = 0.0
    finished_at: float = 0.0
    result: dict[str, Any] | None = None       # 最近一次恢复结果
    inspection: dict[str, Any] | None = None   # 最近一次巡检结果
    error: str | None = None
    events: list[str] = field(default_factory=list)
    pending_recovery: dict[str, Any] | None = None
    interjection_taken: bool = False


def create_app(
    tape_root: str | None = None,
    output_root: str | None = None,
    read_delay: float | None = None,
) -> Flask:
    app = Flask(__name__)
    tape_root = tape_root or os.environ.get("TAPE_ROOT", "data/tapes")
    output_root = output_root or os.environ.get("OUTPUT_ROOT", "data/output")
    if read_delay is None:
        read_delay = float(os.environ.get("READ_DELAY", "0.05"))
    os.makedirs(output_root, exist_ok=True)

    # 可重入锁：路由在持锁状态校验后直接调用 start_*，这些函数内部也会加锁。
    lock = threading.RLock()
    state = JobState()

    def artifact_path(set_name: str) -> str:
        return os.path.join(output_root, set_name, ARTIFACT_NAME)

    def artifact_exists(set_name: str) -> bool:
        return os.path.isfile(artifact_path(set_name))

    def artifact_size(set_name: str) -> int:
        path = artifact_path(set_name)
        return os.path.getsize(path) if os.path.isfile(path) else 0

    def _append_events_locked(messages: list[str]) -> None:
        state.events.extend(messages)
        if len(state.events) > MAX_EVENTS:
            del state.events[: len(state.events) - MAX_EVENTS]

    def append_events(messages: list[str]) -> None:
        with lock:
            _append_events_locked(messages)

    def _result_to_dict(result: RecoveryResult) -> dict[str, Any]:
        return {
            "success": result.success,
            "set_name": result.set_name,
            "error": result.error,
            "blocks_written": result.blocks_written,
            "metrics": asdict(result.metrics) | {"events": []},
            "artifact_path": result.artifact_path,
        }

    def execute_recovery(
        set_name: str, read_quota: int, staging_quota: int
    ) -> RecoveryResult | None:
        """执行一次恢复（普通任务或巡检插队），返回引擎结果。"""

        out_dir = os.path.join(output_root, set_name)
        os.makedirs(out_dir, exist_ok=True)
        tmp_path = os.path.join(out_dir, ARTIFACT_NAME + ".part")
        try:
            tape_set = load_set(tape_root, set_name)
            sink = FileImageSink(tmp_path)
            result = simulate_recovery(
                tape_set,
                read_quota,
                staging_quota,
                sink=sink,
                read_delay=read_delay,
                sleep=time.sleep,
            )
            with lock:
                _append_events_locked(result.metrics.events[-MAX_EVENTS:])
                if result.success:
                    # 全部块核对通过后原子发布；上次成品被一次性替换。
                    os.replace(tmp_path, artifact_path(set_name))
                    result.artifact_path = artifact_path(set_name)
                    _append_events_locked(
                        [f"成品已发布：{artifact_path(set_name)}"]
                    )
                else:
                    # 失败：丢弃半成品，上次成品原样保留。
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    _append_events_locked(
                        [f"恢复失败，上次成品已保留：{result.error}"]
                    )
                state.result = _result_to_dict(result)
                state.error = result.error
            return result
        except Exception as exc:  # noqa: BLE001 - 线程内兜底，错误回传页面
            with lock:
                state.error = f"恢复任务异常: {exc}"
                _append_events_locked([state.error])
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            return None

    # ---- 恢复任务 ---------------------------------------------------------

    def _reset_for_recovery_locked(
        set_name: str, read_quota: int, staging_quota: int, *, keep_inspection: bool
    ) -> None:
        state.running = True
        state.kind = "recovery"
        state.set_name = set_name
        state.read_quota = read_quota
        state.staging_quota = staging_quota
        state.inspection_quota = 0
        state.inspection_progress = 0
        state.started_at = time.time()
        state.finished_at = 0.0
        state.result = None
        state.error = None
        if not keep_inspection:
            state.inspection = None
        state.interjection_taken = False
        state.pending_recovery = None
        _append_events_locked(
            [
                f"恢复任务已提交：{set_name}，读带上限 {read_quota} 块，"
                f"暂存上限 {staging_quota} 块"
            ]
        )

    def run_recovery_worker(
        set_name: str, read_quota: int, staging_quota: int
    ) -> None:
        try:
            execute_recovery(set_name, read_quota, staging_quota)
        finally:
            with lock:
                state.running = False
                state.kind = None
                state.finished_at = time.time()

    def start_recovery(
        set_name: str,
        read_quota: int,
        staging_quota: int,
        *,
        keep_inspection: bool = False,
    ) -> None:
        with lock:
            _reset_for_recovery_locked(
                set_name, read_quota, staging_quota, keep_inspection=keep_inspection
            )
        thread = threading.Thread(
            target=run_recovery_worker,
            args=(set_name, read_quota, staging_quota),
            daemon=True,
        )
        thread.start()

    # ---- 巡检任务 ---------------------------------------------------------

    def _poll_pending_recovery() -> dict[str, Any] | None:
        with lock:
            if state.pending_recovery is not None and not state.interjection_taken:
                return dict(state.pending_recovery)
            return None

    def _run_recovery_interjection(
        req: dict[str, Any],
    ) -> RecoveryOutcome:
        with lock:
            state.pending_recovery = None
            state.interjection_taken = True
            state.kind = "recovery"
            state.error = None
            state.result = None
            _append_events_locked(
                [
                    "恢复插队开始："
                    f"{req['set_name']}，读带上限 {req['read_quota']} 块，"
                    f"暂存上限 {req['staging_quota']} 块（照原额度执行）"
                ]
            )
        result = execute_recovery(
            req["set_name"], req["read_quota"], req["staging_quota"]
        )
        with lock:
            state.kind = "inspection"
            _append_events_locked(["恢复插队结束，巡检继续"])
        if result is None:
            return RecoveryOutcome(
                set_name=req["set_name"],
                info={
                    "set_name": req["set_name"],
                    "success": False,
                    "error": state.error,
                },
            )
        return outcome_from_recovery(result)

    def _on_inspection_event(message: str) -> None:
        append_events([message])

    def _on_inspection_progress(frames_read: int) -> None:
        with lock:
            state.inspection_progress = frames_read

    def run_inspection_worker(set_name: str, quota: int) -> None:
        result = None
        try:
            tape_set = load_set(tape_root, set_name)
            result = inspect_volume(
                tape_set,
                quota,
                read_delay=read_delay,
                sleep=time.sleep,
                poll_recovery=_poll_pending_recovery,
                run_recovery=_run_recovery_interjection,
                on_event=_on_inspection_event,
                on_progress=_on_inspection_progress,
            )
        except Exception as exc:  # noqa: BLE001 - 线程内兜底，错误回传页面
            with lock:
                state.error = f"巡检任务异常: {exc}"
                _append_events_locked([state.error])

        handoff: dict[str, Any] | None = None
        with lock:
            if result is not None:
                state.inspection = result.to_dict()
                state.inspection_progress = result.frames_read
            state.finished_at = time.time()
            if (
                state.pending_recovery is not None
                and not state.interjection_taken
            ):
                # 巡检靠自己查完了，未让位；把已排队的恢复作为普通任务接着跑。
                handoff = dict(state.pending_recovery)
                state.pending_recovery = None
                state.result = None
                state.error = None
                state.read_quota = handoff["read_quota"]
                state.staging_quota = handoff["staging_quota"]
                state.set_name = handoff["set_name"]
                state.started_at = time.time()
                state.kind = "recovery"
                _append_events_locked(
                    ["巡检结束，开始执行此前排队的恢复请求（未占用插队机会）"]
                )
            else:
                state.running = False
                state.kind = None

        if handoff is not None:
            # state.running 仍为 True，直到恢复结束。
            run_recovery_worker(
                handoff["set_name"],
                handoff["read_quota"],
                handoff["staging_quota"],
            )

    def start_inspection(set_name: str, quota: int) -> None:
        with lock:
            state.running = True
            state.kind = "inspection"
            state.set_name = set_name
            state.read_quota = 0
            state.staging_quota = 0
            state.inspection_quota = quota
            state.inspection_progress = 0
            state.started_at = time.time()
            state.finished_at = 0.0
            state.result = None
            state.inspection = None
            state.error = None
            state.pending_recovery = None
            state.interjection_taken = False
            state.events = [
                f"巡检任务已提交：{set_name}，最多读 {quota} 帧；按带号顺读"
            ]
        thread = threading.Thread(
            target=run_inspection_worker,
            args=(set_name, quota),
            daemon=True,
        )
        thread.start()

    # ---- 入参解析 ---------------------------------------------------------

    def _form_data() -> dict[str, Any]:
        payload: dict[str, Any] = {}
        json_body = request.get_json(silent=True)
        if isinstance(json_body, dict):
            payload.update(json_body)
        payload.update(request.form.to_dict())
        return payload

    def _int_field(data: dict[str, Any], *names: str) -> int | None:
        for name in names:
            if name in data and data[name] not in (None, ""):
                return int(data[name])
        return None

    def _validate_set(set_name: str):
        try:
            return load_set(tape_root, set_name)
        except ManifestError as exc:
            abort(400, str(exc))

    # ---- 路由 -------------------------------------------------------------

    @app.get("/")
    def index():  # type: ignore[override]
        sets = []
        for tape_set in discover_sets(tape_root):
            est = baseline(tape_set)
            sets.append(
                {
                    "name": tape_set.name,
                    "block_count": tape_set.block_count,
                    "tape_count": tape_set.tape_count,
                    "estimate": est,
                    "has_artifact": artifact_exists(tape_set.name),
                    "artifact_size": artifact_size(tape_set.name),
                }
            )
        with lock:
            job = {
                "running": state.running,
                "kind": state.kind,
                "set_name": state.set_name,
                "read_quota": state.read_quota,
                "staging_quota": state.staging_quota,
                "inspection_quota": state.inspection_quota,
                "inspection_progress": state.inspection_progress,
                "result": state.result,
                "inspection": state.inspection,
                "error": state.error,
                "events": list(state.events),
                "pending_recovery": state.pending_recovery is not None,
            }
        return render_template("index.html", sets=sets, job=job)

    @app.post("/inspect")
    @app.post("/inspection")
    def inspect_view():  # type: ignore[override]
        data = _form_data()
        set_name = str(data.get("set_name", "")).strip()
        try:
            quota = _int_field(
                data,
                "inspection_quota",
                "inspection_frames",
                "read_frames",
                "frame_quota",
                "max_frames",
                "quota",
            )
        except (TypeError, ValueError):
            abort(400, "巡检帧额度必须是整数")
        if quota is None:
            abort(400, "请填写巡检最多读帧数")
        if quota <= 0:
            abort(400, "巡检帧额度必须为正整数")
        tape_set = _validate_set(set_name)

        with lock:
            if state.running:
                abort(409, "带机忙，已有任务在执行，请等待完成")
            start_inspection(tape_set.name, quota)
        return redirect(url_for("index"))

    @app.post("/recover")
    def recover():  # type: ignore[override]
        data = _form_data()
        set_name = str(data.get("set_name", "")).strip()
        try:
            read_quota = _int_field(data, "read_quota", "recovery_read_quota")
            staging_quota = _int_field(
                data, "staging_quota", "recovery_staging_quota"
            )
        except (TypeError, ValueError):
            abort(400, "额度必须是整数")
        if read_quota is None or staging_quota is None:
            abort(400, "请填写读带与暂存额度")
        if read_quota <= 0 or staging_quota < 0:
            abort(400, "读带额度必须为正整数，暂存额度必须为非负整数")
        tape_set = _validate_set(set_name)

        with lock:
            if state.running:
                if state.kind != "inspection":
                    abort(409, "已有任务在执行，请等待完成")
                if state.interjection_taken or state.pending_recovery is not None:
                    abort(409, "本轮巡检只接受一次恢复插队")
                # 队列中的恢复请求由巡检在帧边界取走；巡检未取走则巡检结束后执行。
                state.pending_recovery = {
                    "set_name": tape_set.name,
                    "read_quota": read_quota,
                    "staging_quota": staging_quota,
                }
                state.error = None
                _append_events_locked(
                    [
                        "恢复插队请求已排队："
                        f"{tape_set.name}，读带上限 {read_quota} 块，"
                        f"暂存上限 {staging_quota} 块"
                    ]
                )
                return redirect(url_for("index"))
            start_recovery(tape_set.name, read_quota, staging_quota)
        return redirect(url_for("index"))

    @app.get("/status")
    @app.get("/inspection/status")
    def status():  # type: ignore[override]
        with lock:
            inspection = state.inspection
            payload = {
                "running": state.running,
                "kind": state.kind,
                "set_name": state.set_name,
                "read_quota": state.read_quota,
                "staging_quota": state.staging_quota,
                "inspection_quota": state.inspection_quota,
                "inspection_progress": state.inspection_progress,
                "result": state.result,
                "inspection": inspection,
                "error": state.error,
                "events": list(state.events),
                "pending_recovery": state.pending_recovery is not None,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
            }
            # 便于脚本直接读取的扁平字段。
            payload["inspection_frames_read"] = (
                inspection["frames_read"]
                if inspection
                else state.inspection_progress
            )
            payload["inspection_supplied_frames"] = (
                inspection["supplied_count"] if inspection else 0
            )
            payload["inspection_complete"] = bool(
                inspection and inspection["complete"]
            )
            payload["inspection_status"] = (
                inspection["status"] if inspection else None
            )
            return payload

    @app.get("/download/<set_name>")
    def download(set_name: str):  # type: ignore[override]
        from .storage import _SAFE_NAME

        if not _SAFE_NAME.fullmatch(set_name):
            abort(404)
        path = artifact_path(set_name)
        if not os.path.isfile(path):
            abort(404, "该带卷尚无已交付的镜像成品")
        # 恢复线程写的是 .part 并以 os.replace 原子发布，下载不受恢复影响。
        return send_file(
            os.path.abspath(path),
            as_attachment=True,
            download_name=f"{set_name}-image.bin",
        )

    return app
