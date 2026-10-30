"""章节工作流入口：提供视频内容，自动生成脚本、旁白与动画并出片。

    python scripts/make-chapter.py --content-file 内容.md --output .data/chapter-xxx --target-sec 90
    python scripts/make-chapter.py --content "讲讲注意力机制……" --no-tts      # 无声预览（估算时间轴）
    python scripts/make-chapter.py --output .data/chapter-xxx --reuse-script   # 沿用已生成的脚本，只重做旁白与画面
"""
import argparse
import logging
from pathlib import Path

from scidirector_ai.chapter.references import ReferenceLibrary, reference_session
from scidirector_ai.chapter.workflow import make_chapter
from scidirector_ai.chapter.writer import ChapterScript
from scidirector_ai.config import get_settings
from scidirector_ai.llm import LLMClient
from scidirector_ai.tts.factory import build_tts_provider


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--content", default="")
    parser.add_argument("--content-file", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--target-sec", type=int, default=90)
    parser.add_argument("--style", default="")
    parser.add_argument("--reference", default="", help="成品源码目录或 import-chapter-reference.py 导出的参考包；仅只读参考")
    parser.add_argument("--no-tts", action="store_true")
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--reuse-script", action="store_true")
    parser.add_argument("--reuse-audio", action="store_true", help="复用输出目录里已合成的旁白（需与脚本一致）")
    parser.add_argument("--chrome", default="")
    parser.add_argument("--code-model", default="", help="场景代码模型，默认 SCID_CHAPTER_CODE_MODEL")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    out = Path(args.output)
    library = ReferenceLibrary.load(args.reference) if args.reference else None
    if args.reference and Path(args.reference).is_dir() and out.resolve().is_relative_to(Path(args.reference).resolve()):
        parser.error("输出目录不能位于所选参考成品目录内")
    script = None
    if args.reuse_script:
        script = ChapterScript.model_validate_json((out / "script.json").read_text(encoding="utf-8"))
    content = Path(args.content_file).read_text(encoding="utf-8") if args.content_file else args.content
    if script is None and not content.strip():
        raise SystemExit("需要 --content、--content-file 或 --reuse-script")

    settings = get_settings()
    tts = None if args.no_tts else build_tts_provider(settings)
    code_settings = settings
    if settings.chapter_code_provider or args.code_model:
        code_settings = settings.model_copy(update={
            "llm_provider": settings.chapter_code_provider or settings.llm_provider,
            "llm_model": args.code_model or settings.chapter_code_model or settings.llm_model})
    with reference_session(library, output=out):
        result = make_chapter(LLMClient(settings), content, out, tts=tts, code_llm=LLMClient(code_settings), target_sec=args.target_sec,
                              style=args.style, executable=args.chrome, render=not args.no_video,
                              script=script, tts_attempts=settings.tts_max_attempts, reuse_audio=args.reuse_audio)
    lines = sum(len(s.lines) for s in result.script.scenes)
    print(f"《{result.script.title}》{len(result.script.scenes)} 个场景、{lines} 句、{result.timing['dur']:.1f} 秒")
    for warning in result.warnings:
        print("警告：", warning)
    print(result.video or result.html)


if __name__ == "__main__":
    main()
