"""章节模式端到端原型：整章旁白 → 逐字时间轴 → 章节 HTML → 预检 → 成片。

    python scripts/render-attention-chapter.py            # 用已配置的 TTS（需逐字时间戳，如 Fish）
    python scripts/render-attention-chapter.py --estimate  # 不调用 TTS，按估算时间轴出无声预览
"""
import argparse
import json
from pathlib import Path

from scidirector_ai.chapter import compile_chapter, estimate_timing, preflight_chapter, render_chapter, synthesize_chapter
from scidirector_ai.chapter.examples import attention
from scidirector_ai.config import get_settings
from scidirector_ai.tts.factory import build_tts_provider


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".data/chapter-attention")
    parser.add_argument("--estimate", action="store_true", help="不合成旁白，用估算时间轴")
    parser.add_argument("--reuse-audio", action="store_true", help="复用输出目录里已合成的旁白与时间轴")
    parser.add_argument("--chrome", default="")
    parser.add_argument("--no-video", action="store_true", help="只编译与预检，不逐帧渲染")
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    audio = None
    if args.estimate:
        timing = estimate_timing(attention.LINES)
    elif args.reuse_audio and (out / "timing.json").is_file():
        timing, audio = json.loads((out / "timing.json").read_text(encoding="utf-8")), out / "narration.mp3"
    else:
        settings = get_settings()
        provider = build_tts_provider(settings)
        if provider is None:
            raise SystemExit("未配置 TTS；可加 --estimate 出无声预览")
        audio, timing = synthesize_chapter(provider, attention.LINES, out,
                                           attempts=settings.tts_max_attempts, backoff_sec=settings.tts_retry_backoff_sec)
    html = out / "index.html"
    html.write_text(compile_chapter(attention.director_js(), timing, series=attention.SERIES,
                                    title=attention.TITLE, highlights=attention.HIGHLIGHTS), encoding="utf-8")
    problems = preflight_chapter(html, timing, executable=args.chrome)
    if problems:
        raise SystemExit("预检失败：\n" + "\n".join(problems[:10]))
    print(f"预检通过：{len(timing['lines'])} 句，{timing['dur']:.1f} 秒 -> {html}")
    if not args.no_video:
        print(render_chapter(html, timing, out, audio=audio, executable=args.chrome))


if __name__ == "__main__":
    main()
