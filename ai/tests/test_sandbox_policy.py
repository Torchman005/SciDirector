"""沙盒静态策略的单元测试。

这一组测试的价值远高于普通业务测试：它们是**安全边界**的回归测试。
任何一次「为了兼容某个写法而放宽策略」的改动，都必须在这里留下痕迹。
"""

from __future__ import annotations

import pytest

from scidirector_ai.sandbox.policy import PolicyReport, PolicyViolation, check_source, is_safe


class TestAllowedCode:
    """合法渲染代码必须通过，避免误杀导致正常镜头渲染不出来。"""

    @pytest.mark.parametrize(
        "source",
        [
            "from manim import *\n\nclass S(Scene):\n    def construct(self):\n        self.play(Write(Text('hi')))\n",
            "import numpy as np\nx = np.linspace(0, 1, 10)\n",
            "import math\nprint(math.pi)\n",
            "import json\npayload = json.loads('{\"a\": 1}')\n",
            "from collections import Counter\nc = Counter('aab')\n",
            # np.random 是允许的（在 numpy 白名单内），且属性名不在黑名单里。
            "import numpy as np\nnp.random.seed(0)\n",
        ],
    )
    def test_allows_render_code(self, source: str) -> None:
        report = check_source(source)
        errors = [v for v in report.violations if v.severity == "error"]
        assert not errors, f"合法代码被误判：{errors}"


class TestBlockedImports:
    """危险模块必须被拦截。"""

    @pytest.mark.parametrize(
        "source",
        [
            "import os\nos.system('rm -rf /')\n",
            "import subprocess\nsubprocess.run(['ls'])\n",
            "import socket\n",
            "import requests\n",
            "import shutil\n",
            "import importlib\n",
            "from os import path\n",
            "import urllib.request\n",
            "import ctypes\n",
            "import pickle\n",
        ],
    )
    def test_blocks_dangerous_imports(self, source: str) -> None:
        assert not is_safe(source), f"危险导入未被拦截：{source!r}"

    def test_blocks_unknown_module(self) -> None:
        """不在白名单内的模块一律拦截（默认拒绝，而不是默认允许）。"""
        report = check_source("import some_random_package\n")
        assert not report.ok
        assert "白名单" in report.violations[0].reason

    def test_blocks_relative_import(self) -> None:
        assert not is_safe("from . import secret\n")


class TestBlockedCalls:
    """危险调用与内省必须被拦截。"""

    @pytest.mark.parametrize(
        "source",
        [
            "eval('1+1')\n",
            "exec('a=1')\n",
            "__import__('os')\n",
            "x = getattr(object, 'x')\n",
            "open('/etc/passwd', 'w')\n",
            "compile('a', '<s>', 'eval')\n",
            "globals()\n",
        ],
    )
    def test_blocks_dangerous_calls(self, source: str) -> None:
        assert not is_safe(source), f"危险调用未被拦截：{source!r}"

    @pytest.mark.parametrize(
        "source",
        [
            "(1).__class__.__bases__\n",
            "object.__subclasses__()\n",
            "func.__globals__\n",
        ],
    )
    def test_blocks_sandbox_escape_attributes(self, source: str) -> None:
        """经典的沙盒逃逸链必须被拦截。"""
        assert not is_safe(source), f"逃逸手法未被拦截：{source!r}"

    def test_blocks_while_true_as_warning(self) -> None:
        """`while True` 记为 warning 而非 error：不阻断执行，但必须被观测到。"""
        report = check_source("while True:\n    pass\n")
        assert report.ok, "warning 不应阻断执行"
        assert len(report.violations) == 1
        assert report.violations[0].severity == "warning"


class TestLatexEscaping:
    """LaTeX 反斜杠转义检查（正确性，不是安全）。

    真实事故：模型生成了 ``MathTex(r"F", r"=", r"m", r"\\\\times", r"a")``。
    raw string 里 ``\\\\`` 是两个真实反斜杠，LaTeX 当成换行，画面成了
    "F = m ⏎ times a"，VLM 读成 "timesa" 并连续三轮给出同一条建议 ——
    三轮渲染全部浪费。这条规则让编码智能体的自修复循环在**渲染之前**抓住它。
    """

    def test_blocks_double_backslash_command(self) -> None:
        source = 'from manim import *\neq = MathTex(r"F", r"\\\\times", r"a")\n'
        report = check_source(source)
        assert not report.ok, "双反斜杠命令未被拦截"
        assert any("双反斜杠" in v.reason for v in report.violations)

    def test_blocks_tab_from_non_raw_latex(self) -> None:
        """``"\\times"`` 里的 ``\\t`` 是制表符 —— 模型的本意显然不是制表符。"""
        source = 'from manim import *\neq = MathTex("F = m \\times a")\n'
        report = check_source(source)
        assert not report.ok, "被转义成控制字符的 LaTeX 命令未被拦截"
        assert any("控制字符" in v.reason for v in report.violations)

    def test_allows_correct_single_backslash(self) -> None:
        source = r'''
from manim import *
eq = MathTex(r"F", r"=", r"m", r"\times", r"a")
'''
        assert check_source(source).ok, "正确的单反斜杠写法被误判"

    def test_allows_latex_line_break(self) -> None:
        """``\\\\`` 作为 LaTeX 换行是合法的，不能一刀切。

        这条是防"误报"的反向控制：限定成"双反斜杠 + 已知命令名"之后，
        带空格的换行写法必须仍然通过。
        """
        source = r'''
from manim import *
eq = MathTex(r"a = b \\ c = d")
'''
        assert check_source(source).ok, "合法的 LaTeX 换行被误判为转义错误"


class TestMissingImports:
    """引用了**允许但没导入**的模块 —— 运行时必然 NameError。

    真实事故：manim 提示词的「确定性」一节要求「用 `random` 必须
    `random.seed(0)`」，模型于是**无条件**写下 `random.seed(0)`，却没有
    `import random`（`from manim import *` 并不导出它 —— 已实测
    `'random' in globals()` 为 False）。静态检查放行，渲染时第 7 行抛
    `NameError`，白烧一轮渲染 + 一次模型调用；而重试的是同一份代码，
    错误一模一样，整条重试链全部浪费。
    """

    def test_blocks_module_use_without_import(self) -> None:
        source = (
            "from manim import *\n"
            "\n"
            "\n"
            "class SciShotScene(Scene):\n"
            "    def construct(self):\n"
            "        random.seed(0)\n"
        )
        report = check_source(source)
        assert not report.ok, "用了 random 却连 import 都没有，应当被拦下"
        assert any("没有导入" in v.reason for v in report.violations)
        # 报错必须给出**可直接照做**的修法，否则模型只能猜。
        assert any("import random" in v.reason for v in report.violations)

    def test_allows_module_use_with_import(self) -> None:
        source = (
            "import random\n"
            "from manim import *\n"
            "\n"
            "\n"
            "class SciShotScene(Scene):\n"
            "    def construct(self):\n"
            "        random.seed(0)\n"
        )
        assert check_source(source).ok, "正常 import 之后不该被误判"

    def test_allows_aliased_numpy(self) -> None:
        """`import numpy as np` 之后写 `np.xxx` 是常规写法，不能被误报。"""
        source = "import numpy as np\nvalue = np.array([1, 2, 3])\n"
        assert check_source(source).ok, "取别名的常规写法被误判为缺 import"

    def test_reports_each_missing_module_once(self) -> None:
        """同一个缺失模块用多次，只报一条 —— 刷屏会淹没真正的问题。"""
        source = "from manim import *\nmath.sqrt(2)\nmath.pi\n"
        report = check_source(source)
        missing = [v for v in report.violations if "没有导入" in v.reason]
        assert len(missing) == 1, f"同一模块应只报一次，实际 {len(missing)} 条"


class TestErrorReporting:
    """违规信息必须可用于回灌给编码智能体（含行号与可执行指令）。"""

    def test_reports_lineno_and_snippet(self) -> None:
        source = "from manim import *\nimport os\n"
        report = check_source(source)
        assert not report.ok
        violation = report.violations[0]
        assert violation.lineno == 2
        assert "os" in violation.snippet
        assert "第 2 行" in violation.to_feedback()

    def test_syntax_error_becomes_violation(self) -> None:
        """语法错误包装成违规，而不是抛 SyntaxError。

        这样调用方只需处理一种失败形态，且错误信息可直接回灌给模型。
        """
        report = check_source("def broken(:\n")
        assert not report.ok
        assert "语法错误" in report.violations[0].reason

    def test_empty_source_is_violation(self) -> None:
        report = check_source("   \n")
        assert not report.ok

    def test_summary_is_actionable(self) -> None:
        report = check_source("import os\nimport socket\n")
        summary = report.summary()
        assert "禁止" in summary

    def test_default_advice_wording_for_ast_violations(self) -> None:
        """AST 白名单类违规（**使用了**被禁止的东西）用默认措辞。

        与渲染契约类违规（**缺少**某物）区分开：后者自带 advice，
        因为套用"使用了被禁止的 X"会生成病句。
        """
        report = check_source("import os\n")
        feedback = report.violations[0].to_feedback()
        assert "使用了被禁止的" in feedback
        assert "第 1 行" in feedback

    def test_explicit_advice_overrides_default_wording(self) -> None:
        """带 advice 的违规必须原样输出建议，而不是被套进默认句式。"""
        violation = PolicyViolation(
            reason="缺少渲染契约 window.__seek(t)",
            severity="error",
            advice="请定义 window.__seek = (t) => {...} 并设置 window.__ready = true。",
        )
        assert violation.to_feedback() == violation.advice
        assert "使用了被禁止的" not in violation.to_feedback()

    def test_violation_str_is_for_humans(self) -> None:
        """``__str__`` 给人看（简短），``to_feedback`` 给模型看（可执行）。"""
        violation = PolicyViolation(reason="导入被禁止的模块 `os`", lineno=3, snippet="import os")
        assert "第 3 行" in str(violation)
        assert "import os" in str(violation)

    def test_multiple_violations_collected(self) -> None:
        """一次性收集全部违规：回灌信息越完整，模型一次改对的概率越高。"""
        report = check_source("import os\nimport socket\neval('1')\n")
        assert len(report.violations) >= 3


def test_report_ok_property() -> None:
    report = PolicyReport()
    assert report.ok
