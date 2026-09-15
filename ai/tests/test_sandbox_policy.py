"""沙盒静态策略的单元测试。

这一组测试的价值远高于普通业务测试：它们是**安全边界**的回归测试。
任何一次「为了兼容某个写法而放宽策略」的改动，都必须在这里留下痕迹。
"""

from __future__ import annotations

import pytest

from scidirector_ai.sandbox.policy import PolicyReport, check_source, is_safe


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

    def test_multiple_violations_collected(self) -> None:
        """一次性收集全部违规：回灌信息越完整，模型一次改对的概率越高。"""
        report = check_source("import os\nimport socket\neval('1')\n")
        assert len(report.violations) >= 3


def test_report_ok_property() -> None:
    report = PolicyReport()
    assert report.ok
