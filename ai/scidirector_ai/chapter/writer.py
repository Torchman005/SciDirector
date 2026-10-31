"""章节编剧与导演：视频内容 → 讲解脚本（含分场景视觉计划）→ 各场景动画代码。

## 为什么不再接收成稿脚本

实测纰漏多数源自脚本本身：术语未定义就使用（Query/Key/Value、softmax）、
字幕与朗读不一致、关键推理缺一步，而且画面是为某一份脚本手写的。
脚本与画面必须一起设计：编剧在写每句话时就决定画面上出现什么，
导演再按这份计划逐场景写动画，关键词必须与旁白逐字对应。

## 流程

1. ``write_script``：内容 → ``ChapterScript``（场景 → 句子，字幕 sub / 朗读 say 分离）；
   程序校验 + 一次模型自审（术语先定义后使用、推理不跳步、示意数值标注）。
2. 旁白合成与逐字对齐（narration.py）。
3. ``write_director``：每个场景单独生成 ``scene(...)`` 代码，避免单次输出过长；
   浏览器预检的报错（未知关键词、脚本异常、文字重叠/出画）回灌给模型定点修复。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from ..agents.base import load_prompt
import logging

from ..llm import LLMClient, LLMParseError, Task
from .narration import ChapterLine, validate_lines

_ID = r"^[A-Za-z][A-Za-z0-9_]{0,31}$"
#: 字幕两行以内（40px、可用宽约 1300px，每行约 32 字）。
MAX_SUB_CHARS = 64
#: 朗读文本里不该出现、TTS 会念错或念出符号名的字符。
_UNSPEAKABLE = re.compile(r"[√∑×÷=<>^_\\/|{}\[\]（）()《》【】#*~]")
logger = logging.getLogger(__name__)
_EXAMPLE = Path(__file__).with_name("examples") / "attention.js"


class ScriptLine(BaseModel):
    id: str = Field(pattern=_ID)
    sub: str = Field(min_length=2, max_length=MAX_SUB_CHARS)
    say: str = ""
    #: 这句话说出时画面上发生什么（给导演的指令）。
    visual: str = Field(min_length=4, max_length=300)
    #: 画面要卡点的关键词，必须逐字出现在朗读文本里。
    cues: list[str] = Field(default_factory=list, max_length=6)

    @property
    def spoken(self) -> str:
        return (self.say or self.sub).strip()

    @model_validator(mode="after")
    def _speakable(self) -> ScriptLine:
        if _UNSPEAKABLE.search(self.spoken):
            raise ValueError(f"{self.id} 的朗读文本含符号 {_UNSPEAKABLE.findall(self.spoken)}；"
                             "在 say 里写成口语（如“根号 d”“等于”）")
        norm = lambda s: re.sub(r"\s+", "", s).casefold()  # noqa: E731
        missing = [c for c in self.cues if norm(c) not in norm(self.spoken)]
        if missing:
            raise ValueError(f"{self.id} 的卡点词 {missing} 不在朗读文本里")
        return self


class ScriptScene(BaseModel):
    id: str = Field(pattern=_ID)
    title: str = Field(min_length=2, max_length=24)
    #: 场景的固定舞台：贯穿本场景的对象及布局（导演据此建立元素）。
    stage: str = Field(min_length=10, max_length=600)
    #: 一个舞台最多 6 句：实测 10 句的场景同屏对象堆叠，版式问题集中在那一个场景。
    lines: list[ScriptLine] = Field(min_length=1, max_length=6)


class ChapterScript(BaseModel):
    title: str = Field(min_length=2, max_length=24)
    series: str = Field(default="", max_length=40)
    #: 字幕关键词着色：词 -> #RRGGBB
    highlights: dict[str, str] = Field(default_factory=dict)
    scenes: list[ScriptScene] = Field(min_length=1, max_length=12)
    #: 全章共用的示例数据（贯穿的例子）：画面上的所有数值由运行时从这里取或计算，
    #: 模型不得在动画代码里手写结果（实测手写的 softmax 百分比与分数对不上）。
    data: dict[str, float | int | str | list[float] | list[int] | list[str]] = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def _data(cls, value: dict) -> dict:
        for key, item in value.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", key):
                raise ValueError(f"data 的键 {key!r} 必须是字母开头的标识符")
            if isinstance(item, list) and len(item) > 64:
                raise ValueError(f"data.{key} 太长（≤64 项）")
        return value

    @field_validator("highlights")
    @classmethod
    def _colors(cls, value: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in value.items() if k and re.fullmatch(r"#[0-9a-fA-F]{6}", v)}

    @model_validator(mode="after")
    def _ids(self) -> ChapterScript:
        validate_lines(self.chapter_lines())
        scene_ids = [s.id for s in self.scenes]
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("场景 id 必须唯一")
        return self

    def chapter_lines(self) -> list[ChapterLine]:
        return [ChapterLine(line.id, line.sub, line.say) for scene in self.scenes for line in scene.lines]


class ScriptReview(BaseModel):
    ok: bool
    issues: list[str] = Field(default_factory=list)


class SceneCode(BaseModel):
    code: str = Field(min_length=40)


def write_script(llm: LLMClient, content: str, *, target_sec: int = 90, style: str = "") -> ChapterScript:
    """内容 → 讲解脚本；模型自审一次，有问题则按意见重写一次。"""
    user = render(load_prompt("chapter_script_user"), content=content.strip(), target_sec=target_sec,
                  chars=int(target_sec * 4.4), style=style or "（无）")
    system = load_prompt("chapter_script")
    script = llm.chat_json(system, user, ChapterScript, task=Task.PLAN,
                           temperature=0.5, max_tokens=8192)
    assert isinstance(script, ChapterScript)
    review = llm.chat_json(load_prompt("chapter_script_review"),
                           "## 原始内容\n" + content.strip() + "\n\n## 脚本\n" + script.model_dump_json(indent=1),
                           ScriptReview, task=Task.PLAN, temperature=0, max_tokens=2048)
    assert isinstance(review, ScriptReview)
    issues = [] if review.ok else list(review.issues)
    budget = int(target_sec * 4.4)
    # 时长靠模型自觉经常超标（实测 90 秒目标写出约 150 秒），由程序按字数核对，最多修订两轮。
    for _ in range(2):
        over = _length_issue(script, budget, target_sec)
        if not issues and not over:
            return script
        try:
            revised = llm.chat_json(system,
                                    user + "\n\n## 上一版脚本\n" + script.model_dump_json(indent=1)
                                    + "\n\n## 审读意见（必须逐条解决，其余保持）\n"
                                    + "\n".join(f"- {i}" for i in issues + ([over] if over else [])),
                                    ChapterScript, task=Task.PLAN, temperature=0.3, max_tokens=8192)
        except LLMParseError as exc:
            # 修订稿不合格时保留上一稿（它已通过同样的校验），而不是让整次生成失败。
            logger.warning("脚本修订稿未通过校验，沿用上一稿", extra={"error": str(exc)[:300]})
            return script
        assert isinstance(revised, ChapterScript)
        script, issues = revised, []
    return script


def spoken_chars(script: ChapterScript) -> int:
    return sum(len(re.sub(r"\s", "", line.spoken)) for scene in script.scenes for line in scene.lines)


def _length_issue(script: ChapterScript, budget: int, target_sec: int) -> str:
    spoken = spoken_chars(script)
    if spoken <= budget * 1.2:
        return ""
    return (f"朗读总字数 {spoken} 远超目标约 {budget} 字（{target_sec} 秒）：合并重复解释、删去次要句，"
            f"压缩到 {int(budget * .9)}～{budget} 字，保留全部关键概念与推理步骤")


def render(template: str, **values: object) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def _scene_brief(script: ChapterScript, index: int, timing: dict) -> str:
    scene = script.scenes[index]
    by_id = {line["id"]: line for line in timing["lines"]}
    rows = []
    for line in scene.lines:
        t = by_id[line.id]
        rows.append({"id": line.id, "字幕": line.sub, "朗读逐字": t["chars"],
                     "时长秒": round(t["end"] - t["start"], 2), "画面": line.visual, "卡点词": line.cues})
    return json.dumps({"场景编号": f"{index + 1:02d}", "场景标题": scene.title, "舞台": scene.stage,
                       "首句id": scene.lines[0].id, "句子": rows,
                       "示例数据V": script.data}, ensure_ascii=False, indent=1)


#: 字符串里手写的计算结果（百分比、“= 23”）：必须改为由 V 计算。
_HARDCODED = re.compile(r"\d+(?:\.\d+)?\s*%|=\s*-?\d")
#: 运行时导出的名字：导演代码重新声明它们会遮蔽运行时（实测 const bars = ... 导致初始化前访问）。
_RUNTIME_NAMES = ("V", "C", "T", "TE", "K", "KE", "P", "Wn", "Ez", "S", "A", "O", "TR", "TRS", "txt", "drawPath",
                  "DP", "chip", "rowXs", "panel", "heading", "arcD", "arcPt", "flowDots", "vecRow", "matrix",
                  "bars", "pop", "scene", "softmax", "pct", "mix", "clamp", "lerp", "rng", "textW")
_DECL = re.compile(r"\b(?:const|let|var|function)\s+(" + "|".join(_RUNTIME_NAMES) + r")\b")


def _string_literals(code: str) -> list[str]:
    """按 JS 词法取出 '…' 与 "…" 字面量（处理转义；跳过注释与模板字符串）。"""
    out, i, n = [], 0, len(code)
    while i < n:
        c = code[i]
        if code.startswith("//", i):
            i = code.find("\n", i)
            i = n if i < 0 else i
        elif code.startswith("/*", i):
            i = code.find("*/", i + 2)
            i = n if i < 0 else i + 2
        elif c in "'\"`":
            j, buf = i + 1, []
            while j < n and code[j] != c:
                if code[j] == "\\":
                    j += 1
                elif c == "`" and code.startswith("${", j):
                    # 模板里的插值是计算出来的，跳过直到配对的右括号。
                    depth, j = 1, j + 2
                    while j < n and depth:
                        depth += {"{": 1, "}": -1}.get(code[j], 0)
                        j += 1
                    continue
                buf.append(code[j] if j < n else "")
                j += 1
            out.append("".join(buf))
            i = j + 1
        else:
            i += 1
    return out


_K_CALL = re.compile(r"""\bKE?\(\s*(['"])([A-Za-z][A-Za-z0-9_]*)\1\s*,\s*(['"])([^'"]+)\3""")
_T_CALL = re.compile(r"""\bTE?\(\s*(['"])([A-Za-z][A-Za-z0-9_]*)\1\s*\)""")


def cue_problems(code: str, scene_id: str, timing: dict) -> list[str]:
    """静态核对 T()/K() 引用：句子要存在，关键词要在那句里；找不到时指出它实际在哪句。"""
    lines = {line["id"]: line["chars"] for line in timing["lines"]}
    norm = lambda s: re.sub(r"[\s，。！？、；：,.!?;:…—“”‘’\"'（）()《》]+", "", s).casefold()  # noqa: E731
    problems = []
    for m in _T_CALL.finditer(code):
        if m.group(2) not in lines:
            problems.append(f"[场景 {scene_id}] T('{m.group(2)}')：没有这句旁白；可用的 id：{list(lines)}")
    for m in _K_CALL.finditer(code):
        line_id, word = m.group(2), norm(m.group(4))
        if line_id not in lines:
            problems.append(f"[场景 {scene_id}] K('{line_id}', …)：没有这句旁白")
        elif word not in lines[line_id]:
            where = [i for i, chars in lines.items() if word in chars]
            hint = f"它在 {where} 里" if where else "整章都没有念到这个词，换成该句朗读里实际出现的词"
            problems.append(f"[场景 {scene_id}] K('{line_id}', '{m.group(4)}')：这句朗读里没有该词（朗读：{lines[line_id]}）；{hint}")
    return problems


def static_problems(code: str, scene_id: str) -> list[str]:
    """代码层面的硬性检查：数值必须从脚本数据计算；不能遮蔽运行时函数。"""
    problems = []
    # '100%' 是 SVG/CSS 尺寸，不是计算结果。
    found = sorted({s[:40] for s in _string_literals(code) if _HARDCODED.search(s) and s.strip() != "100%"})
    if found:
        problems.append(f"[场景 {scene_id}] 字符串里手写了数值结果 {found[:5]}：画面上的数字必须由 V 中的数据计算"
                        "（如 pct(softmax(V.scores)[i])、`= ${...}`），不得手写")
    shadowed = sorted(set(_DECL.findall(code)))
    if shadowed:
        problems.append(f"[场景 {scene_id}] 声明了与运行时同名的变量 {shadowed}，会遮蔽运行时函数；请改名（如 barChart）")
    return problems


def write_scene(llm: LLMClient, script: ChapterScript, index: int, timing: dict, *,
                previous_code: str = "", problems: list[str] | None = None) -> str:
    """生成（或按预检问题修复）一个场景的 ``scene(...)`` 代码。"""
    user = render(load_prompt("chapter_director_user"), brief=_scene_brief(script, index, timing),
                  chapter=script.title)
    if previous_code:
        user += ("\n\n## 上一版代码\n```js\n" + previous_code + "\n```\n## 浏览器预检发现的问题（逐条修复，"
                 "保留没问题的部分）\n" + "\n".join(f"- {p}" for p in (problems or [])))
    system = render(load_prompt("chapter_director"), example=_EXAMPLE.read_text(encoding="utf-8"))
    result = llm.chat_json(system, user, SceneCode, task=Task.CODE, temperature=0.2, max_tokens=8192)
    assert isinstance(result, SceneCode)
    code = result.code.strip()
    first = script.scenes[index].lines[0].id
    if f"scene('{first}'" not in code and f'scene("{first}"' not in code:
        raise ValueError(f"场景代码必须以 scene('{first}', svg => {{ ... }}) 注册")
    return code
