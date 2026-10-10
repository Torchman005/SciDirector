"""Bounded child process for offline Cubism rendering, invoked by SandboxRunner."""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if str(Path(p or ".").resolve()) != _HERE]

import array
import json
import math
import mimetypes
import subprocess
import wave
from urllib.parse import unquote, urlsplit

ENVELOPE_HZ = 50
NOISE_FLOOR = .008


def mouth_envelope(wav_path: Path, gain: float = 1) -> list[float]:
    levels = []
    with wave.open(str(wav_path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError("口型分析需要单声道 16-bit PCM")
        block = max(1, audio.getframerate() // ENVELOPE_HZ)
        while data := audio.readframes(block):
            samples = array.array("h", data)
            if sys.byteorder != "little": samples.byteswap()
            levels.append(math.sqrt(sum((v / 32768) ** 2 for v in samples) / max(1, len(samples))))
    voiced = sorted(v for v in levels if v > NOISE_FLOOR)
    reference = max(.06, voiced[min(len(voiced)-1, int(len(voiced)*.95))] if voiced else .06)
    value, output = 0., []
    for level in levels:
        target = min(1., max(0., (level-NOISE_FLOOR)/(reference-NOISE_FLOOR))*gain)
        value += (target-value) * (.75 if target>value else .45)
        output.append(round(value if value>.015 else 0.,4))
    return output


def capture(config_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    config = json.loads(config_path.read_text(encoding="utf-8"))
    root = config_path.parent
    wav = root / "narration.wav"
    subprocess.run(["ffmpeg","-v","error","-nostdin","-y","-i",config["audio"],"-t",str(config["duration"]),
                    "-ac","1","-ar","16000","-c:a","pcm_s16le",str(wav)], check=True, timeout=120)
    envelope = mouth_envelope(wav,config["gain"])
    (root/"mouth.json").write_text(json.dumps({"hz":ENVELOPE_HZ,"values":envelope}),encoding="utf-8")
    wav.unlink()
    model_path = Path(config["model"])
    manifest = json.loads(model_path.read_text(encoding="utf-8"))
    refs = manifest["FileReferences"]
    # A second allowlist at the renderer boundary prevents even a forged internal request
    # from using the browser as a filesystem or network proxy.
    safe_manifest = {"Version":3,"FileReferences":{"Moc":refs["Moc"],"Textures":refs["Textures"]},
                     "Groups":manifest.get("Groups",[]),"Layout":manifest.get("Layout",{})}
    allowed = {}
    for name in [refs["Moc"],*refs["Textures"]]:
        candidate = (model_path.parent/name).resolve()
        if not candidate.is_relative_to(model_path.parent.resolve()) or not candidate.is_file():
            raise ValueError("模型引用超出导入目录或文件不存在")
        allowed["/"+name] = candidate
    def route(request):
        url = urlsplit(request.request.url)
        if url.netloc != "scid.local": request.abort(); return
        name = unquote(url.path)
        if name == "/":
            request.fulfill(content_type="text/html",body='<html><body style="margin:0;background:transparent"><canvas id="avatar"></canvas></body></html>')
        elif name == "/model.model3.json":
            request.fulfill(content_type="application/json",body=json.dumps(safe_manifest))
        elif name in allowed:
            file=allowed[name]
            request.fulfill(content_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream",body=file.read_bytes())
        else: request.abort()
    with sync_playwright() as pw:
        launch={"args":["--no-sandbox","--use-gl=angle","--use-angle=swiftshader","--enable-unsafe-swiftshader"]}
        if config.get("chrome"): launch["executable_path"]=config["chrome"]
        browser=pw.chromium.launch(**launch)
        try:
            page=browser.new_page(viewport={"width":config["width"],"height":config["height"]},device_scale_factor=1)
            page.route("**/*",route)
            page.goto("https://scid.local/",timeout=15000)
            for library in ("core","pixi","display"):
                page.add_script_tag(content=Path(config["libraries"][library]).read_text(encoding="utf-8"))
            page.add_script_tag(content=Path(__file__).with_name("presenter_runtime.js").read_text(encoding="utf-8"))
            page.evaluate("config => window.setupPresenter(config)", {**config,"envelope":envelope,"hz":ENVELOPE_HZ})
            # Catch SDK/model state that depends on playback history before encoding.
            sample = min(.37, config["duration"] / 2)
            page.evaluate("t => window.seekPresenter(t)", sample)
            expected = page.screenshot(type="png", omit_background=True)
            for moment in (config["duration"], 0, sample):
                page.evaluate("t => window.seekPresenter(t)", moment)
            if page.screenshot(type="png", omit_background=True) != expected:
                raise RuntimeError("Live2D 模型倒序 seek 不确定，停止渲染以避免口型漂移")
            count=math.ceil(config["duration"]*config["fps"])
            # Stream alpha PNGs into ffmpeg: disk usage is bounded by the encoded layer,
            # not duration * uncompressed RGBA frames.
            log=(root/"encode.log").open("wb")
            encoder=subprocess.Popen(["ffmpeg","-hide_banner","-loglevel","error","-nostdin","-y",
                "-f","image2pipe","-vcodec","png","-framerate",str(config["fps"]),"-i","pipe:0",
                "-an","-c:v","libvpx-vp9","-pix_fmt","yuva420p","-auto-alt-ref","0","-b:v","0",
                "-crf","24","-deadline","realtime","-cpu-used","6",str(root/"presenter.webm")],stdin=subprocess.PIPE,stderr=log)
            try:
                for index in range(count):
                    page.evaluate("t => window.seekPresenter(t)",index/config["fps"])
                    encoder.stdin.write(page.screenshot(type="png",omit_background=True))
                encoder.stdin.close()
                if encoder.wait(timeout=120): raise RuntimeError("Live2D alpha 编码失败")
            finally:
                if encoder.poll() is None: encoder.kill(); encoder.wait()
                log.close()
        finally:
            browser.close()


if __name__ == "__main__":
    capture(Path(sys.argv[1]).resolve())
