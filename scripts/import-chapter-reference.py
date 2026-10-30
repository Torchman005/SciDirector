"""Snapshot local scene-code references without model calls or executing external JS."""
from __future__ import annotations

import argparse
from pathlib import Path

from scidirector_ai.chapter.references import ReferenceLibrary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="网页动画项目或多个项目的父目录")
    parser.add_argument("--output", required=True, help="新建只读参考包 JSON（不覆盖已有文件）")
    args = parser.parse_args()
    source, output = Path(args.source).resolve(), Path(args.output).resolve()
    if output == source or source.is_dir() and output.is_relative_to(source):
        parser.error("参考包必须保存在原成品目录之外")
    library = ReferenceLibrary.load(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(library.model_dump_json(indent=1))
    print(f"{len({s.project for s in library.scenes})} 个项目 / {len(library.scenes)} 个参考场景")
    for scene in library.scenes:
        print(f"{scene.id} | {scene.title} | {', '.join(scene.tags)}")
    for note in library.notes:
        print("未提取：", note)
    print(output)


if __name__ == "__main__":
    main()
