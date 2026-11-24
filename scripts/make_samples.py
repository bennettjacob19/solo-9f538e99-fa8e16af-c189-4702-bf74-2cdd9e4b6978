#!/usr/bin/env python3
"""生成三套样例带卷（每套 12 个 4 KiB 镜像块、最多两盘磁带）。

用法::

    python -I scripts/make_samples.py [--root data/tapes]

生成内容：

* sample-ordered   ：块号与带上位置一致，顺序读完即可，无需暂存
* sample-scrambled ：块在两盘带上交错排布，恢复需要多次换带并使用暂存
* sample-bad       ：布局同乱序样例，但块 11 的内容被故意损坏（摘要不符）

注意：坏块样例的清单仍记录正确摘要，损坏发生在带卷数据上，
用于验证“写入前核对 SHA-256、遇坏块报错且保留上次成品”。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage import BLOCK_SIZE, MAX_BLOCKS_PER_SET  # noqa: E402


def block_content(set_name: str, index: int) -> bytes:
    """确定性的 4 KiB 块内容：头部说明 + 按块号生成的填充模式。"""

    header = f"DRILL-BLOCK set={set_name} index={index}\n".encode("utf-8")
    fill = bytes(((b + index * 7) & 0xFF) for b in range(BLOCK_SIZE - len(header)))
    return header + fill


def write_set(root: str, name: str, layout: list[tuple[int, int, int]]) -> None:
    """layout: [(block_index, tape_no, tape_position), ...]"""

    set_dir = os.path.join(root, name)
    os.makedirs(set_dir, exist_ok=True)
    frames: dict[int, list[tuple[int, int, bytes]]] = {}
    manifest_blocks = []
    for index, tape, position in layout:
        data = block_content(name, index)
        assert len(data) == BLOCK_SIZE
        frames.setdefault(tape, []).append((position, index, data))
        manifest_blocks.append(
            {
                "index": index,
                "tape": tape,
                "position": position,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

    for tape, items in frames.items():
        items.sort()
        assert [p for p, _, _ in items] == list(range(len(items))), "帧位置必须连续"
        with open(os.path.join(set_dir, f"tape{tape}.bin"), "wb") as fh:
            for _, _, data in items:
                fh.write(data)

    manifest_blocks.sort(key=lambda b: b["index"])
    with open(os.path.join(set_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": name, "blocks": manifest_blocks}, fh, indent=2)
        fh.write("\n")
    print(f"[ok] {name}: {len(layout)} 块，{len(frames)} 盘磁带 -> {set_dir}")


def corrupt_frame(root: str, name: str, tape: int, position: int) -> None:
    """在清单写好之后翻转某个帧的一个字节，制造坏块。"""

    path = os.path.join(root, name, f"tape{tape}.bin")
    with open(path, "r+b") as fh:
        fh.seek(position * BLOCK_SIZE + 17)
        original = fh.read(1)
        fh.seek(position * BLOCK_SIZE + 17)
        fh.write(bytes([original[0] ^ 0xFF]))
    print(f"[ok] {name}: 已损坏 tape{tape}.bin 位置 {position} 的帧")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成灾备演练样例带卷")
    parser.add_argument("--root", default="data/tapes", help="带卷根目录")
    args = parser.parse_args()

    n = MAX_BLOCKS_PER_SET  # 12

    # 1) 顺序样例：块 0 在带头，块号沿顺读方向递增，一次顺读连续追加，
    #    无需暂存、无需回卷
    ordered = [(i, 0, i) for i in range(6)] + [(6 + i, 1, i) for i in range(6)]
    write_set(args.root, "sample-ordered", ordered)

    # 2) 乱序样例：每盘带沿顺读方向块号递减，一次顺读即可把该带上
    #    未来块全部收入暂存；无需回卷，但暂存峰值达到 10 块。
    #    tape0 (6 帧): 10,8,6,4,2,0    tape1 (6 帧): 11,9,7,5,3,1
    scrambled = [
        (10, 0, 0), (8, 0, 1), (6, 0, 2), (4, 0, 3), (2, 0, 4), (0, 0, 5),
        (11, 1, 0), (9, 1, 1), (7, 1, 2), (5, 1, 3), (3, 1, 4), (1, 1, 5),
    ]
    write_set(args.root, "sample-scrambled", scrambled)

    # 3) 坏块样例：同样布局，块 11（tape1 位置 0）内容损坏
    write_set(args.root, "sample-bad", scrambled)
    corrupt_frame(args.root, "sample-bad", tape=1, position=0)

    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
