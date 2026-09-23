"""`scripts/load-env.sh` 的解析语义（供 `make dev-*` 使用）。

## 为什么要给一个 shell 脚本写单测

因为它守着一条**很容易静默失效**的链路：根目录 `.env` 只在 `docker compose`
插值时生效，本地跑进程时没人读它 —— 于是"照着 `.env.example` 配好、`make dev-ai`
起服务"会静默进 mock 模式（内容全是占位、没有任何报错）。

而它自己踩过一次真实的坑：`.env.example` 里写了行内注释
（`SCID_SANDBOX_MANIM_QUALITY=l   # l=480p 草稿…`），**bash / python-dotenv /
docker compose 都会把注释剥掉，只有我们这条路径没有** —— 值被整行读进去，
pydantic 报 `literal_error`，服务直接起不来。同一个文件三个消费者两种语义，
属于最难查的一类问题，因此这里把规则逐条钉住。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LOADER = REPO / "scripts" / "load-env.sh"


def load(env_file: Path, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """在子 shell 里 source 加载器，把结果以 KEY=VALUE 打印回来。"""
    script = (
        f". {LOADER}; "
        "for k in $(grep -oE '^[A-Z_]+' " + str(env_file) + " | sort -u); do "
        'eval "v=\\${$k-<unset>}"; printf "%s=%s\\n" "$k" "$v"; done'
    )
    import os

    env = dict(os.environ)
    env.pop("SCID_ENV_FILE", None)
    env["SCID_ENV_FILE"] = str(env_file)
    for key in ("SCID_T_INLINE", "SCID_T_HASH", "SCID_T_QUOTED", "SCID_T_TRAIL"):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)

    out = subprocess.run(
        ["sh", "-c", script], cwd=REPO, env=env, capture_output=True, text=True, check=True
    ).stdout
    return dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)


@pytest.fixture()
def env_file(tmp_path: Path) -> Path:
    f = tmp_path / "test.env"
    f.write_text(
        "\n".join(
            [
                "# 整行注释",
                "SCID_T_INLINE=l   # l=480p 草稿, m=720p, h=1080p",
                "SCID_T_HASH=ab#cd",
                "SCID_T_QUOTED=\"a # b\"",
                "SCID_T_TRAIL=value   ",
                "",
                "裸词不该被当成变量",
            ]
        ),
        encoding="utf-8",
    )
    return f


def test_strips_inline_comment_like_dotenv_does(env_file: Path) -> None:
    """**行内注释必须剥掉** —— 这正是让服务起不来的那条。

    `.env.example` 里 `SCID_SANDBOX_MANIM_QUALITY=l   # l=480p…` 这种写法，
    bash/dotenv/compose 都会剥注释；不剥就会把整行当值，pydantic 直接报
    literal_error。这条用例锁住它。
    """
    assert load(env_file)["SCID_T_INLINE"] == "l"


def test_keeps_hash_inside_value_when_not_preceded_by_space(env_file: Path) -> None:
    """密码里带 `#` 很常见：`#` 前没有空白时不能当注释。"""
    assert load(env_file)["SCID_T_HASH"] == "ab#cd"


def test_quoted_value_keeps_hash(env_file: Path) -> None:
    """引号内的 `#` 是值的一部分，且引号本身要去掉。"""
    assert load(env_file)["SCID_T_QUOTED"] == "a # b"


def test_trims_trailing_whitespace(env_file: Path) -> None:
    assert load(env_file)["SCID_T_TRAIL"] == "value"


def test_explicit_environment_wins_over_file(env_file: Path) -> None:
    """**显式环境变量优先于文件**（dotenv 的 override=False 语义）。

    这条是 `make dev-ai SCID_AI_HTTP_PORT=18000` 能生效的前提：
    直接 `. ./.env` 会把命令行覆盖冲掉，表现为"端口改了不生效、仍去绑旧端口"，
    报 address already in use，看起来像有残留进程。
    """
    got = load(env_file, extra_env={"SCID_T_INLINE": "from-env"})
    assert got["SCID_T_INLINE"] == "from-env"


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """文件不存在时安静返回：CI 里没有 .env，不该因此让 dev 目标失败。"""
    missing = tmp_path / "nope.env"
    out = subprocess.run(
        ["sh", "-c", f". {LOADER} && echo OK"],
        cwd=REPO,
        env={"SCID_ENV_FILE": str(missing), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_real_env_example_parses_without_leaking_comments() -> None:
    """拿**真实的 .env.example** 跑一遍：任何值都不该把注释带进去。

    比逐条构造用例更狠：模板里将来新增一行带行内注释的变量，这里会立刻发现。
    """
    example = REPO / ".env.example"
    if not example.is_file():
        pytest.skip(".env.example 不存在")
    got = load(example)
    assert got, "应当解析出变量"
    for key, value in got.items():
        assert " # " not in value, f"{key} 的值里混进了注释：{value!r}"
        assert not value.startswith("#"), f"{key} 的值是注释：{value!r}"


def test_env_example_actually_builds_a_valid_settings() -> None:
    """.env.example 里填的值必须能构造出合法的 `Settings`。

    这是"照着模板配就能起来"的**可执行版本**。它当场抓过一个真实缺陷：
    `SCID_SANDBOX_MANIM_QUALITY=l   # l=480p 草稿…` 的行内注释被当成值的一部分，
    pydantic 报 `literal_error`，**服务直接起不来**（而 compose/dotenv 读同一个文件
    是好的）。只断言"没有注释残留"不够 —— 值本身也可能不是合法枚举，这里直接构造。
    """
    from scidirector_ai.config import Settings

    example = REPO / ".env.example"
    if not example.is_file():
        pytest.skip(".env.example 不存在")

    raw = load(example)
    kwargs = {
        key.removeprefix("SCID_").lower(): value
        for key, value in raw.items()
        if key.startswith("SCID_") and value != ""
    }
    # 未配置项一律留空，避免模板里的示例值（如某个假密钥）影响解析。
    for k in ("llm_provider", "vlm_provider", "tts_provider"):
        if k in kwargs and kwargs[k] == "":
            del kwargs[k]

    settings = Settings(**kwargs)
    # 顺带确认服务商解析真的走通了（模板默认是 openai）。
    assert settings.text_target().provider in {"openai", "deepseek", "bailian", "mock"}
