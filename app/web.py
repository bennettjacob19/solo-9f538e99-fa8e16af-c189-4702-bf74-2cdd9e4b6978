"""Flask 灾备恢复演练 Web 界面。

页面能力：

* 列出本地带卷目录中的全部合法带卷，并给出贪心策略下的额度预估；
* 选择一套带卷，填写读带块数上限与暂存块数上限后启动恢复演练；
* 恢复在后台线程执行，页面轮询展示实际读带块数、暂存峰值与事件流；
* 恢复中仍可下载上一次成品；成功后原子替换成品，失败（坏块/额度不足）
  则报错并保留上一次成品不变。
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
from .storage import ManifestError, discover_sets, load_set

ARTIFACT_NAME = "image.bin"
MAX_EVENTS = 200


@dataclass
class JobState:
    running: bool = False
    set_name: str | None = None
    read_quota: int = 0
    staging_quota: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    result: dict[str, Any] | None = None
    events: list[str] = field(default_factory=list)
    error: str | None = None


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

    lock = threading.Lock()
    state = JobState()

    def artifact_path(set_name: str) -> str:
        return os.path.join(output_root, set_name, ARTIFACT_NAME)

    def artifact_exists(set_name: str) -> bool:
        return os.path.isfile(artifact_path(set_name))

    def artifact_size(set_name: str) -> int:
        path = artifact_path(set_name)
        return os.path.getsize(path) if os.path.isfile(path) else 0

    def run_recovery(set_name: str, read_quota: int, staging_quota: int) -> None:
        nonlocal state
        out_dir = os.path.join(output_root, set_name)
        os.makedirs(out_dir, exist_ok=True)
        tmp_path = os.path.join(out_dir, ARTIFACT_NAME + ".part")
        try:
            tape_set = load_set(tape_root, set_name)
            sink = FileImageSink(tmp_path)

            def on_sleep(_delay: float) -> None:
                time.sleep(_delay)

            result: RecoveryResult = simulate_recovery(
                tape_set,
                read_quota,
                staging_quota,
                sink=sink,
                read_delay=read_delay,
                sleep=on_sleep,
            )
            with lock:
                state.events = list(result.metrics.events[-MAX_EVENTS:])
                if result.success:
                    # 全部块核对通过后原子发布；上次成品被一次性替换。
                    os.replace(tmp_path, artifact_path(set_name))
                    result.artifact_path = artifact_path(set_name)
                else:
                    # 失败：丢弃半成品，上次成品原样保留。
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                state.result = _result_to_dict(result)
                state.error = result.error
        except Exception as exc:  # noqa: BLE001 - 线程内兜底，错误回传页面
            with lock:
                state.error = f"恢复任务异常: {exc}"
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        finally:
            with lock:
                state.running = False
                state.finished_at = time.time()

    def _result_to_dict(result: RecoveryResult) -> dict[str, Any]:
        return {
            "success": result.success,
            "set_name": result.set_name,
            "error": result.error,
            "blocks_written": result.blocks_written,
            "metrics": asdict(result.metrics) | {"events": []},
            "artifact_path": result.artifact_path,
        }

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
                "set_name": state.set_name,
                "read_quota": state.read_quota,
                "staging_quota": state.staging_quota,
                "result": state.result,
                "error": state.error,
                "events": list(state.events),
            }
        return render_template("index.html", sets=sets, job=job)

    @app.post("/recover")
    def recover():  # type: ignore[override]
        set_name = request.form.get("set_name", "").strip()
        try:
            read_quota = int(request.form.get("read_quota", ""))
            staging_quota = int(request.form.get("staging_quota", ""))
        except (TypeError, ValueError):
            abort(400, "额度必须是整数")
        if read_quota <= 0 or staging_quota < 0:
            abort(400, "读带额度必须为正整数，暂存额度必须为非负整数")
        try:
            tape_set = load_set(tape_root, set_name)
        except ManifestError as exc:
            abort(400, str(exc))

        with lock:
            if state.running:
                abort(409, "已有恢复任务在执行，请等待完成")
            state.running = True
            state.set_name = tape_set.name
            state.read_quota = read_quota
            state.staging_quota = staging_quota
            state.started_at = time.time()
            state.finished_at = 0.0
            state.result = None
            state.events = ["任务已提交，开始恢复演练…"]
            state.error = None

        thread = threading.Thread(
            target=run_recovery,
            args=(tape_set.name, read_quota, staging_quota),
            daemon=True,
        )
        thread.start()
        return redirect(url_for("index"))

    @app.get("/status")
    def status():  # type: ignore[override]
        with lock:
            return {
                "running": state.running,
                "set_name": state.set_name,
                "read_quota": state.read_quota,
                "staging_quota": state.staging_quota,
                "result": state.result,
                "error": state.error,
                "events": list(state.events),
            }

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
