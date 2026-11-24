"""带卷（tape volume）的发现、清单校验与逐帧顺读。

磁盘布局::

    <tape_root>/<set_name>/manifest.json
    <tape_root>/<set_name>/tape0.bin
    <tape_root>/<set_name>/tape1.bin

带卷文件由若干 4 KiB 定长帧顺序拼接而成，没有额外文件头；
每帧对应清单中的一个镜像块，每块在整套带卷中只出现一次。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

BLOCK_SIZE = 4096
MAX_BLOCKS_PER_SET = 12
MANIFEST_NAME = "manifest.json"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class ManifestError(ValueError):
    """清单或带卷文件不合法。"""


@dataclass(frozen=True)
class BlockEntry:
    """清单中一个镜像块的记录。"""

    index: int          # 镜像块号，从 0 开始
    tape: int           # 所在物理磁带号，从 0 开始
    position: int       # 带上帧位置，从 0 开始（顺读时的帧序号）
    sha256: str         # 该块 4 KiB 内容的摘要（小写十六进制）


@dataclass(frozen=True)
class TapeSet:
    """一套带卷（最多两盘磁带、合计最多 12 个镜像块）。"""

    name: str
    path: str
    blocks: tuple[BlockEntry, ...]
    tape_files: tuple[str, ...]

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def tape_count(self) -> int:
        return len(self.tape_files)

    def entry(self, index: int) -> BlockEntry:
        return self._by_index[index]

    def __post_init__(self) -> None:
        by_index = {b.index: b for b in self.blocks}
        object.__setattr__(self, "_by_index", by_index)


def _validate_manifest(raw: object) -> tuple[list[dict], list[str]]:
    if not isinstance(raw, dict):
        raise ManifestError("manifest.json 顶层必须是对象")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError("清单缺少 name 字段")
    blocks = raw.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ManifestError("清单 blocks 必须是非空数组")
    if len(blocks) > MAX_BLOCKS_PER_SET:
        raise ManifestError(
            f"每套带卷最多 {MAX_BLOCKS_PER_SET} 个镜像块，实际 {len(blocks)} 个"
        )

    seen_index: set[int] = set()
    seen_loc: set[tuple[int, int]] = set()
    normalized: list[dict] = []
    tapes_used: set[int] = set()
    for pos, item in enumerate(blocks):
        if not isinstance(item, dict):
            raise ManifestError(f"第 {pos} 条块记录不是对象")
        try:
            index = int(item["index"])
            tape = int(item["tape"])
            position = int(item["position"])
            digest = str(item["sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"第 {pos} 条块记录字段不完整: {exc}") from exc
        if index < 0 or tape < 0 or position < 0:
            raise ManifestError(f"第 {pos} 条块记录出现负数")
        if index >= MAX_BLOCKS_PER_SET:
            raise ManifestError(f"块号 {index} 超出上限 {MAX_BLOCKS_PER_SET - 1}")
        if index in seen_index:
            raise ManifestError(f"镜像块 {index} 在清单中重复出现")
        if (tape, position) in seen_loc:
            raise ManifestError(f"磁带 {tape} 位置 {position} 上有多个块")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ManifestError(f"块 {index} 的 sha256 不是 64 位小写十六进制")
        seen_index.add(index)
        seen_loc.add((tape, position))
        tapes_used.add(tape)
        normalized.append(
            {"index": index, "tape": tape, "position": position, "sha256": digest}
        )

    if seen_index != set(range(len(blocks))):
        raise ManifestError("镜像块号必须从 0 开始连续编号")
    if tapes_used != set(range(max(tapes_used) + 1)):
        raise ManifestError("磁带号必须从 0 开始连续编号")
    if len(tapes_used) > 2:
        raise ManifestError("演练场景最多两盘磁带")
    return normalized, sorted(f"tape{t}.bin" for t in tapes_used)


def load_set(tape_root: str, name: str) -> TapeSet:
    """按名字加载并校验一套带卷。"""

    if not _SAFE_NAME.fullmatch(name):
        raise ManifestError(f"非法带卷目录名: {name!r}")
    set_dir = os.path.realpath(os.path.join(tape_root, name))
    root_real = os.path.realpath(tape_root)
    if os.path.commonpath([root_real, set_dir]) != root_real:
        raise ManifestError("带卷目录越界")
    manifest_path = os.path.join(set_dir, MANIFEST_NAME)
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise ManifestError(f"找不到清单: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"清单 JSON 解析失败: {exc}") from exc

    normalized, _ = _validate_manifest(raw)
    if str(raw.get("name")) != name:
        raise ManifestError("清单 name 与目录名不一致")

    entries = tuple(
        BlockEntry(
            index=item["index"],
            tape=item["tape"],
            position=item["position"],
            sha256=item["sha256"],
        )
        for item in normalized
    )

    # 校验磁带文件：帧数与清单一致、帧位置连续不缺帧。
    per_tape: dict[int, list[BlockEntry]] = {}
    for entry in entries:
        per_tape.setdefault(entry.tape, []).append(entry)
    tape_files: list[str] = []
    for tape in sorted(per_tape):
        tape_file = os.path.join(set_dir, f"tape{tape}.bin")
        if not os.path.isfile(tape_file):
            raise ManifestError(f"缺少磁带文件 tape{tape}.bin")
        positions = sorted(e.position for e in per_tape[tape])
        if positions != list(range(len(positions))):
            raise ManifestError(f"磁带 {tape} 的帧位置不连续")
        actual_size = os.path.getsize(tape_file)
        expected_size = len(positions) * BLOCK_SIZE
        if actual_size != expected_size:
            raise ManifestError(
                f"tape{tape}.bin 大小 {actual_size} 与清单推算 {expected_size} 不符"
            )
        tape_files.append(tape_file)

    return TapeSet(name=name, path=set_dir, blocks=entries, tape_files=tuple(tape_files))


def discover_sets(tape_root: str) -> list[TapeSet]:
    """列出本地目录下所有合法带卷，非法目录跳过（在页面上不展示）。"""

    if not os.path.isdir(tape_root):
        return []
    result: list[TapeSet] = []
    for name in sorted(os.listdir(tape_root)):
        if not _SAFE_NAME.fullmatch(name):
            continue
        set_dir = os.path.join(tape_root, name)
        if not os.path.isdir(set_dir):
            continue
        try:
            result.append(load_set(tape_root, name))
        except ManifestError:
            continue
    return result


def read_frame(tape_set: TapeSet, tape: int, position: int) -> bytes:
    """从指定磁带读取某个帧位置上的 4 KiB 原始内容。"""

    tape_file = os.path.join(tape_set.path, f"tape{tape}.bin")
    with open(tape_file, "rb") as fh:
        fh.seek(position * BLOCK_SIZE)
        data = fh.read(BLOCK_SIZE)
    if len(data) != BLOCK_SIZE:
        raise ManifestError(
            f"tape{tape}.bin 第 {position} 帧不足 {BLOCK_SIZE} 字节"
        )
    return data


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
