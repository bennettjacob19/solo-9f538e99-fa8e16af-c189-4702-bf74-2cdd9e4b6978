"""测试辅助：在临时目录里构造带卷。"""

import hashlib
import json
import os
import sys

# 允许在无 pip/venv 的环境下，用 `python -I` 跑测试：依赖装在仓库内 .pylibs。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _extra in (_REPO, os.path.join(_REPO, ".pylibs")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from app.storage import BLOCK_SIZE


def make_block(index: int, size: int = BLOCK_SIZE) -> bytes:
    header = f"block-{index}\n".encode()
    return header + bytes((index & 0xFF) for _ in range(size - len(header)))


def build_set(root: str, name: str, layout, corrupt: int | None = None) -> str:
    """layout: [(block_index, tape, position), ...]，块号需从 0 连续。"""

    set_dir = os.path.join(root, name)
    os.makedirs(set_dir, exist_ok=True)
    per_tape: dict[int, list[tuple[int, int, bytes]]] = {}
    records = []
    for index, tape, position in layout:
        data = make_block(index)
        if corrupt == index:
            data = data[:17] + bytes([data[17] ^ 0xFF]) + data[18:]
        per_tape.setdefault(tape, []).append((position, index, data))
        records.append(
            {
                "index": index,
                "tape": tape,
                "position": position,
                "sha256": hashlib.sha256(make_block(index)).hexdigest(),
            }
        )
    for tape, frames in per_tape.items():
        frames.sort()
        with open(os.path.join(set_dir, f"tape{tape}.bin"), "wb") as fh:
            for _, _, data in frames:
                fh.write(data)
    records.sort(key=lambda r: r["index"])
    with open(os.path.join(set_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": name, "blocks": records}, fh)
    return set_dir


# 与 scripts/make_samples.py 的乱序样例保持一致的布局
SCRAMBLED_LAYOUT = [
    (10, 0, 0), (8, 0, 1), (6, 0, 2), (4, 0, 3), (2, 0, 4), (0, 0, 5),
    (11, 1, 0), (9, 1, 1), (7, 1, 2), (5, 1, 3), (3, 1, 4), (1, 1, 5),
]

# 真正的带头顺序：块 0 在带头，一次顺读连续追加，无暂存、无回卷。
ORDERED_LAYOUT = [(i, 0, i) for i in range(6)] + [
    (6 + i, 1, i) for i in range(6)
]
