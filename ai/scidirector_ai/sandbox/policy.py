"""渲染代码的静态安全策略。

**这是本系统最大的风险面**：执行由大模型生成的任意 Python 代码。
本模块用 AST 静态分析做第一层拦截，目标不是「绝对安全」，
而是把绝大多数无意的危险操作（以及低成本的恶意尝试）挡在进程启动之前。

策略分三类：
* **模块白名单**：只允许渲染必需的模块；
* **名字黑名单**：禁止 ``eval`` / ``exec`` / ``__import__`` 等动态执行入口；
* **调用检查**：禁止 ``os.system``、``subprocess.*``、``open(..., 'w')`` 等副作用调用。

此外还含一组 **LaTeX 转义正确性**规则。它们不是安全问题，但一样必须在进程
启动前拦住，理由见 ``_PolicyVisitor.visit_Constant``。

为什么不用正则而用 AST？
正则匹配 ``import os`` 无法识别 ``__import__("o" + "s")``，也无法区分
``os.path.join``（无害）与 ``os.system``（危险）。AST 能给出结构化的事实。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 策略表
# ---------------------------------------------------------------------------

#: 允许导入的模块白名单。
#: 只列渲染画面真正需要的库。新增条目必须同时说明用途 —— 每多一个模块
#: 就多一份被滥用的可能（例如 ``socket`` 直接意味着可以外联）。
ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        # 数学渲染
        "manim",
        "numpy",
        "math",
        "cmath",
        "random",
        "statistics",
        "fractions",
        "decimal",
        "itertools",
        "functools",
        "operator",
        "collections",
        "dataclasses",
        "enum",
        "typing",
        "abc",
        "textwrap",
        "re",
        "json",
        "datetime",
        "uuid",
        "string",
        "copy",
        "heapq",
        "bisect",
        "array",
        "sympy",  # 符号运算：Manim 的 MathTex 常与之配合
    }
)

#: 明令禁止的模块（比白名单更早触发，用于给出更清晰的错误消息）。
BLOCKED_MODULES: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "httpx",
        "ftplib",
        "smtplib",
        "telnetlib",
        "asyncio",
        "threading",
        "multiprocessing",
        "concurrent",
        "ctypes",
        "importlib",
        "pickle",
        "marshal",
        "shelve",
        "sqlite3",
        "pathlib",
        "glob",
        "tempfile",
        "webbrowser",
        "pty",
        "signal",
        "resource",
        "platform",
        "getpass",
        "pwd",
        "grp",
        "socketio",
        "builtins",
        "__builtin__",
    }
)

#: 禁止出现的内置名字（动态执行与内省入口）。
BLOCKED_NAMES: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "input",
        "breakpoint",
        "memoryview",
        "open",  # 渲染代码不应直接读写文件；产物由沙盒框架统一管理
        "exit",
        "quit",
    }
)

#: 禁止的属性调用（形如 ``obj.attr``）。键为属性名，值为说明。
BLOCKED_ATTRIBUTES: dict[str, str] = {
    "system": "调用 shell 命令",
    "popen": "启动子进程",
    "spawn": "启动子进程",
    "fork": "创建子进程",
    "execv": "执行外部程序",
    "execve": "执行外部程序",
    "remove": "删除文件",
    "unlink": "删除文件",
    "rmdir": "删除目录",
    "rmtree": "递归删除目录",
    "chmod": "修改文件权限",
    "chown": "修改文件属主",
    "kill": "发送信号",
    "killpg": "发送信号",
    "connect": "建立网络连接",
    "urlopen": "发起网络请求",
    "getenv": "读取环境变量（可能泄露密钥）",
    "putenv": "修改环境变量",
    "__dict__": "内省对象内部结构",
    "__class__": "内省对象类型（常用于绕过静态检查）",
    "__globals__": "获取全局命名空间（危险的逃逸入口）",
    "__subclasses__": "枚举子类（经典的沙盒逃逸手法）",
    "__builtins__": "访问内置命名空间",
    "__code__": "访问函数字节码",
    "__reduce__": "反序列化逃逸入口",
    "__reduce_ex__": "反序列化逃逸入口",
}

#: 允许以写模式打开的调用（此处刻意留空：渲染产物由沙盒框架管理，代码只负责画）。
ALLOWED_WRITE_TARGETS: frozenset[str] = frozenset()


@dataclass
class PolicyViolation:
    """一次策略违规。

    带 ``lineno`` 与 ``snippet`` 是刻意的：这条信息会**回灌给编码智能体**，
    让它知道"第 12 行的 os.system 不允许"，从而在重试时改对，
    而不是盲目地再生成一次同样违规的代码。
    """

    reason: str
    lineno: int = 0
    snippet: str = ""
    severity: str = "error"
    #: 直接回灌给模型的建议文本。
    #:
    #: 为空时由 :meth:`to_feedback` 按"使用了被禁止的 X"这一默认措辞生成 ——
    #: 那适用于 AST 白名单类违规（禁止某个语法）。
    #: 但渲染契约类违规（**缺少** window.__seek、**缺少** __ready）语义相反，
    #: 套用默认措辞会生成"使用了被禁止的 缺少渲染契约…"这种病句，
    #: 而它正是模型用来改错的唯一线索。这类违规应当在此给出完整建议。
    advice: str = ""

    def __str__(self) -> str:
        location = f"第 {self.lineno} 行" if self.lineno else "未知位置"
        detail = f"（{self.snippet}）" if self.snippet else ""
        return f"{location}：{self.reason}{detail}"

    def to_feedback(self) -> str:
        """把违规转成可回灌给模型的自然语言指令。"""
        if self.advice:
            return self.advice
        location = f"第 {self.lineno} 行" if self.lineno else "代码中"
        return f"{location}使用了被禁止的 {self.reason}，请改用沙盒允许的写法。"


@dataclass
class PolicyReport:
    """静态检查结果。

    语义约定（很重要，避免调用方误判）：
    * ``ok`` 只要求**没有 error 级违规**；warning 级（如 ``while True``）不阻断执行，
      因为它们由运行时超时兜底，误杀一个合法写法的代价高于让它超时一次。
    * ``has_errors`` 显式区分「有硬性问题」与「只有告警」。
    """

    violations: list[PolicyViolation] = field(default_factory=list)
    imported_modules: set[str] = field(default_factory=set)

    @property
    def errors(self) -> list[PolicyViolation]:
        """仅 error 级违规。"""
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[PolicyViolation]:
        """仅 warning 级违规。"""
        return [v for v in self.violations if v.severity != "error"]

    @property
    def ok(self) -> bool:
        """是否可以安全执行（无 error 级违规）。"""
        return not self.errors

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def summary(self) -> str:
        """生成人类/模型可读的违规摘要（用于回灌重试）。"""
        if self.ok:
            return "静态检查通过"
        return "；".join(v.to_feedback() for v in self.errors[:8])


# ---------------------------------------------------------------------------
# LaTeX 转义：反斜杠被写多了
# ---------------------------------------------------------------------------

#: 常见 LaTeX 命令名，用来判定"``\\`` 后面本该只有一条反斜杠"。
#:
#: 为什么不看它是不是 raw string：**AST 不保留 ``r`` 前缀** ——
#: ``ast.parse('r"a"').body[0].value.kind is None``，Python 认为 ``r`` 不改变语义
#: 就把它丢了。所以只能从**字符串的值**判断。好在两种情况结论一致：
#: 无论 raw 与否，LaTeX 最终都只应收到**一条**反斜杠。
#:
#: 为什么要限定成"已知命令名"而不是"``\\`` 后面跟字母"：
#: ``r"x\\y"`` 是合法的 LaTeX 换行（``\\`` 换行后接 ``y``），
#: 一刀切会把正常代码也判为违规、白白触发一轮重写。限定命令名后，
#: 误报面缩到"换行后紧跟一个同名命令"这种几乎不会出现的写法。
_LATEX_COMMANDS: frozenset[str] = frozenset(
    {
        "times", "cdot", "div", "pm", "mp", "ast", "circ", "bullet",
        "frac", "dfrac", "tfrac", "sqrt", "overline", "underline",
        "vec", "hat", "bar", "dot", "ddot", "tilde", "widehat", "widetilde",
        "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta",
        "eta", "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu",
        "xi", "pi", "varpi", "rho", "varrho", "sigma", "varsigma", "tau",
        "upsilon", "phi", "varphi", "chi", "psi", "omega",
        "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma",
        "Upsilon", "Phi", "Psi", "Omega",
        "sum", "prod", "coprod", "int", "iint", "iiint", "oint",
        "partial", "nabla", "infty", "ell", "hbar", "Re", "Im",
        "leq", "geq", "neq", "approx", "equiv", "sim", "simeq", "propto",
        "ll", "gg", "subset", "supset", "subseteq", "supseteq",
        "cup", "cap", "setminus", "emptyset", "varnothing",
        "in", "notin", "ni", "forall", "exists", "nexists",
        "left", "right", "big", "Big", "bigg", "Bigg",
        "begin", "end", "text", "textbf", "textit", "mathrm", "mathbf",
        "mathit", "mathcal", "mathbb", "mathsf", "mathtt", "operatorname",
        "rightarrow", "leftarrow", "Rightarrow", "Leftarrow",
        "leftrightarrow", "Leftrightarrow", "to", "mapsto", "implies",
        "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos",
        "arctan", "sinh", "cosh", "tanh", "log", "ln", "lg", "exp",
        "lim", "max", "min", "sup", "inf", "det", "gcd", "binom",
        "quad", "qquad", "hspace", "vspace", "over", "choose",
    }
)

#: ``\\`` + 已知 LaTeX 命令名。``(?![A-Za-z])`` 保证命令名是完整的，
#: 免得 ``\\timeseries`` 这种把前缀当成命令。
_DOUBLE_BACKSLASH_CMD = re.compile(
    r"\\\\(?:" + "|".join(sorted(_LATEX_COMMANDS)) + r")(?![A-Za-z])"
)

#: 非 raw string 里被 Python 转义成控制字符的"LaTeX 命令"：
#: ``"\times"`` 的 ``\t`` 是制表符，``"\frac"`` 的 ``\f`` 是换页。
#: 正常文本不会含有这些控制字符，所以一旦出现就是确凿的转义错误。
_LATEX_CONTROL_CHARS: dict[str, str] = {
    "\t": r"\t",
    "\x0b": r"\v",
    "\x0c": r"\f",
    "\x07": r"\a",
    "\x08": r"\b",
}


# ---------------------------------------------------------------------------
# 渲染正确性：用了允许的模块却没导入
# ---------------------------------------------------------------------------

#: 允许被"裸模块名"引用的模块 —— 它们的常规用法就是 `random.seed(...)`、
#: `math.sqrt(...)`。用来发现「引用了却没 import」，那是运行时的 NameError。
#: 只列**允许导入**的模块（见 ALLOWED_MODULES），且不含习惯上会取别名的
#: （如 `import numpy as np` 之后写 `np.xxx`，不会被这里命中）。
_BARE_MODULE_NAMES: frozenset[str] = frozenset(
    {"random", "math", "cmath", "numpy", "statistics", "fractions", "decimal", "itertools"}
)


def _check_missing_imports(tree: ast.AST, report: PolicyReport) -> None:
    """找出「引用了允许但**没导入**的模块」—— 那在渲染时必然 NameError。

    真实事故：manim 提示词的「确定性」一节写着「用 ``random`` 必须
    ``random.seed(0)``」，模型于是老老实实写下 ``random.seed(0)``，
    却**没有** ``import random`` —— ``from manim import *`` 并不导出它
    （已实测 ``'random' in globals()`` is False）。静态检查放行，渲染时在第 7 行
    抛 ``NameError``，白烧一轮渲染 + 一次模型调用；而重试的是同一份代码，
    错误一模一样，于是整条重试链全部浪费。

    这个检查之所以放在这里，和 LaTeX 转义同理：让编码智能体的自修复循环
    **在进程启动之前**就拿到这条信息。
    """
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        name = node.value.id
        if name not in _BARE_MODULE_NAMES or name in report.imported_modules:
            continue
        if name in seen:
            continue
        seen.add(name)
        report.violations.append(
            PolicyViolation(
                reason=(
                    f"使用了 `{name}` 但没有导入它，运行时会 "
                    f"`NameError: name '{name}' is not defined`。"
                    f"要么在文件顶部加 `import {name}`，要么就别用它"
                ),
                lineno=node.value.lineno,
                snippet=f"{name}.{node.attr}",
            )
        )


class _PolicyVisitor(ast.NodeVisitor):
    """遍历 AST 并收集违规项。

    刻意**不提前退出**：一次性收集所有违规比"遇到第一个就抛异常"更有价值 ——
    回灌给模型的信息越完整，它一次改对的概率越高，能省下一整轮渲染。
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.report = PolicyReport()

    # -- 辅助 ---------------------------------------------------------

    def _snippet(self, node: ast.AST) -> str:
        """取节点对应的源码片段（截断到 80 字符，避免日志被长行刷屏）。"""
        try:
            seg = ast.get_source_segment(self.source, node)
        except Exception:  # noqa: BLE001 - get_source_segment 在异常 AST 上可能出错
            seg = None
        if not seg:
            return ""
        seg = seg.strip().replace("\n", " ")
        return seg if len(seg) <= 80 else seg[:77] + "..."

    def _add(self, reason: str, node: ast.AST, severity: str = "error") -> None:
        self.report.violations.append(
            PolicyViolation(
                reason=reason,
                lineno=getattr(node, "lineno", 0),
                snippet=self._snippet(node),
                severity=severity,
            )
        )

    # -- 遍历规则 -----------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(alias.name, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # 相对导入（level > 0）在沙盒里没有意义，且可能被用于越界访问。
        if node.level and node.level > 0:
            self._add("相对导入（沙盒中不允许）", node)
        elif node.module:
            self._check_module(node.module, node)
        self.generic_visit(node)

    def _check_module(self, module: str, node: ast.AST) -> None:
        root = module.split(".")[0]
        self.report.imported_modules.add(root)
        if root in BLOCKED_MODULES:
            self._add(f"导入被禁止的模块 `{module}`", node)
            return
        if root not in ALLOWED_MODULES:
            self._add(f"导入不在白名单内的模块 `{module}`", node)

    def visit_Name(self, node: ast.Name) -> None:
        # 只在「被读取/调用」的位置检查，避免把合法的同名变量误报。
        if node.id in BLOCKED_NAMES:
            self._add(f"使用了被禁止的内置函数 `{node.id}`", node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in BLOCKED_ATTRIBUTES:
            self._add(f"{BLOCKED_ATTRIBUTES[node.attr]}（`{node.attr}`）", node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # 双重保险：即使名字检查被绕过（例如通过别名），调用点也会再查一次。
        func = node.func
        if isinstance(func, ast.Name) and func.id in BLOCKED_NAMES:
            self._add(f"调用被禁止的函数 `{func.id}`", node)
        if isinstance(func, ast.Attribute) and func.attr in BLOCKED_ATTRIBUTES:
            self._add(f"调用被禁止的方法 `{func.attr}`", node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        # `while True:` 在渲染脚本里几乎总是"卡死沙盒"的元凶。
        # 不直接禁止（有些动画循环确实需要），但标记出来便于观测。
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            self.report.violations.append(
                PolicyViolation(
                    reason="检测到 `while True`，可能导致渲染超时",
                    lineno=node.lineno,
                    snippet=self._snippet(node),
                    severity="warning",
                )
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        """LaTeX 转义检查（正确性，非安全）。

        **为什么放在这里**：编码智能体的自修复循环只消费 ``check_source`` 的报告。
        把这类缺陷并进同一份报告，模型才能在一次修复里改掉；否则要先白渲染一轮、
        再由 VLM 审查发现、再花一轮重试 —— 一次转义错误就能烧掉三轮渲染。

        真实事故：模型生成 ``MathTex(r"F", r"=", r"m", r"\\\\times", r"a")``。
        raw string 里 ``\\\\`` 是两个真实反斜杠，LaTeX 当成换行，
        画面上出现 "F = m ⏎ times a"，VLM 读成 "timesa" 并连续三轮给出同一条建议。
        """
        if isinstance(node.value, str):
            if _DOUBLE_BACKSLASH_CMD.search(node.value):
                self._add(
                    "检测到双反斜杠 + LaTeX 命令：LaTeX 会把 `\\\\` 当成换行，"
                    '`r"\\\\times"` 会渲染成"换行 + times"。'
                    '应只写一条反斜杠 `r"\\times"`（只有 JSON 里才需要双写）',
                    node,
                )
            for ch, name in _LATEX_CONTROL_CHARS.items():
                if ch in node.value:
                    self._add(
                        f"字符串里含有控制字符 {name}：多半是把 LaTeX 命令写进了"
                        '非 raw string（例如 "\\times" 里的 \\t 是制表符）。'
                        'LaTeX 一律用 r"..." 原始字符串',
                        node,
                    )
                    break
        self.generic_visit(node)


def check_source(source: str) -> PolicyReport:
    """对渲染源码做静态检查（安全 + LaTeX 转义正确性）。

    语法错误会被包装成一条 ``error`` 级违规：这比抛 SyntaxError 更好，
    因为调用方只需处理一种「失败」形态，且错误信息可以直接回灌给编码智能体。
    """
    report = PolicyReport()
    if not source or not source.strip():
        report.violations.append(PolicyViolation(reason="源码为空", severity="error"))
        return report

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        report.violations.append(
            PolicyViolation(
                reason=f"语法错误：{exc.msg}",
                lineno=exc.lineno or 0,
                snippet=(exc.text or "").strip()[:80],
            )
        )
        return report

    visitor = _PolicyVisitor(source)
    visitor.visit(tree)
    _check_missing_imports(tree, visitor.report)
    return visitor.report


def is_safe(source: str) -> bool:
    """便捷判断：仅当没有任何 **error** 级违规时返回 True。

    warning 级（如 ``while True``）不阻断执行 —— 它们由运行时超时兜底，
    因为在静态阶段误杀一个合法写法，比让它超时一次的代价更高。
    """
    return check_source(source).ok
