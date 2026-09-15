"""渲染代码的静态安全策略。

**这是本系统最大的风险面**：执行由大模型生成的任意 Python 代码。
本模块用 AST 静态分析做第一层拦截，目标不是「绝对安全」，
而是把绝大多数无意的危险操作（以及低成本的恶意尝试）挡在进程启动之前。

策略分三类：
* **模块白名单**：只允许渲染必需的模块；
* **名字黑名单**：禁止 ``eval`` / ``exec`` / ``__import__`` 等动态执行入口；
* **调用检查**：禁止 ``os.system``、``subprocess.*``、``open(..., 'w')`` 等副作用调用。

为什么不用正则而用 AST？
正则匹配 ``import os`` 无法识别 ``__import__("o" + "s")``，也无法区分
``os.path.join``（无害）与 ``os.system``（危险）。AST 能给出结构化的事实。
"""

from __future__ import annotations

import ast
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


def check_source(source: str) -> PolicyReport:
    """对渲染源码做静态安全检查。

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
    return visitor.report


def is_safe(source: str) -> bool:
    """便捷判断：仅当没有任何 **error** 级违规时返回 True。

    warning 级（如 ``while True``）不阻断执行 —— 它们由运行时超时兜底，
    因为在静态阶段误杀一个合法写法，比让它超时一次的代价更高。
    """
    return check_source(source).ok
