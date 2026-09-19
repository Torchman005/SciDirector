"""受控子进程执行器：超时强杀 + 内存限制。

这是"渲染 LLM 生成代码"的唯一执行出口，因此**必须**能回答两个问题：
    1. 死循环怎么办？   -> 超时到点杀掉**整棵进程树**（不是只杀父进程）
    2. 吃爆内存怎么办？ -> 内核级限制 + 监控线程兜底，两条路都要有

## 内存限制为什么要做两套

| 平台 | 机制 | 谁在执行 |
| --- | --- | --- |
| Linux / macOS | ``setrlimit(RLIMIT_DATA)`` | 内核（子进程继承，覆盖整棵树） |
| Windows | **Job Object** ``JOB_OBJECT_LIMIT_JOB_MEMORY`` | 内核（覆盖加入 Job 的全部进程） |
| 任意平台兜底 | 监控线程采样 + 主动 kill | 本模块 |

> **不用 RLIMIT_AS**：它限制的是*虚拟地址空间*，而非常驻内存 ——
> 映射进来的共享库、每个线程的栈、编解码器预留的缓冲都算在里面，
> 通常比实际内存用量大一个数量级（实测 ffmpeg 抽一帧：RSS 56MB、地址空间约 2GB）。
> 把内存上限直接当 AS 上限，正常进程会在远未触及内存上限时被杀，
> 而 ffmpeg 那种情况**退出码仍是 0、产物为空**，一路静默降级到「审查不可用」。
> RLIMIT_AS 现仅作为防跑飞的兜底，留有余量（见 ``_AS_HEADROOM_FACTOR``）。

只做监控线程是不够的：采样总有间隔，一段"在两次采样之间瞬间吃满内存"的代码
可以在此之前把机器打爆。反过来只做内核限制也不够：Windows 上 Job Object 赋值
存在极短的竞态窗口，而且它只会让分配失败（子进程抛 MemoryError），
我们仍需要一个统一的"超限即杀"出口来给出确定的失败语义。

**限制是否真的生效必须如实上报**（``ExecResult.memory_limit_enforced_by``）：
在缺少任何机制的环境里谎报"已限制"是安全代码里最危险的错误。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..logging import get_logger
from .isolation import (
    Isolator,
    ReadOnlyMode,
    ReadOnlyUnavailable,
    SeccompMode,
    SeccompUnavailable,
)
from .netns import IsolationMode, NetworkIsolationUnavailable

logger = get_logger(__name__)

POSIX = os.name == "posix"
WINDOWS = os.name == "nt"

#: 单条命令的输出上限（字节）。渲染工具会刷大量进度条，
#: 不限制会让单个任务把内存吃光。
MAX_OUTPUT_BYTES = 256 * 1024

#: 超时后等待进程真正退出的宽限期。SIGKILL 之后仍需一点时间做内核回收；
#: 若这段时间内进程仍未退出，说明它卡在不可中断状态（例如磁盘 IO）。
KILL_GRACE_SEC = 5.0

#: 「CPU 时间预算耗尽」的终止信号。
#:
#: 用 getattr 而不是直接引用：``signal.SIGXCPU`` 在 Windows 上不存在，
#: 写成模块级常量会让整个模块在 Windows 上导入失败。
_RESOURCE_KILL_SIGNALS = frozenset(
    sig for sig in (getattr(signal, "SIGXCPU", None),) if sig is not None
)

#: seccomp 违规导致的终止信号。
#:
#: 我们的过滤器默认返回 EPERM（不杀进程），因此正常不会走到这里。但把它单独
#: 归类仍然必要：一旦**真的**出现，`returncode` 会是 -31，而"信号 31 是什么"
#: 没人能一眼认出来 —— 现象会变成"渲染莫名失败"。归类成 "seccomp" 之后，
#: 上层与日志能直接指出"是沙盒系统调用策略拦下了它"。
_SECCOMP_KILL_SIGNALS = frozenset(
    sig for sig in (getattr(signal, "SIGSYS", None),) if sig is not None
)


def _classify_resource_exit(returncode: int | None) -> str:
    """把「进程被资源限制杀死」归一化成 ``killed_reason``。

    POSIX 上有**两套**资源限制可能在同一个瞬间开火：

    * ``RLIMIT_CPU``（来自 ``max_cpu_sec``）：到点发 SIGXCPU；
    * 墙钟兜底：截止时间是「超时 + 宽限期」。

    manim 侧把 CPU 上限设为超时的 2 倍，而宽限期恰好也是 5 秒 ——
    两者在小超时下会精确撞在一起（实测 ``timeout_sec=5`` 时：CPU 上限 10s、
    墙钟截止 10s），谁先到取决于调度。**走 CPU 这条路径时监控线程不会设置
    ``kill_flag``**，于是 ``killed_reason`` 是空串，上层把「超时」读成了
    「未知失败」：可复现的现象是 ``test_dead_loop_is_killed_and_reported``
    拿到 ``killed_reason=''``（进程实际是 ``returncode=-24`` 即 SIGXCPU）。

    对上层而言这两条路是同一件事 —— 「资源预算耗尽、这次尝试没跑完」，
    因此归一化成同一个可观测结果（``Agent.md`` §9 的同一条原则：
    跨平台/跨机制差异必须收敛在沙盒边界内）。

    只认 SIGXCPU，不认 SIGKILL：后者来源太多（OOM killer、外部 kill），
    凭它推断原因会掩盖真正的问题。SIGXCPU 只可能来自我们自己设的 RLIMIT_CPU，
    因此不存在误判。
    """
    if returncode is None or returncode >= 0:
        return ""
    if -returncode in _SECCOMP_KILL_SIGNALS:
        return "seccomp"
    if -returncode not in _RESOURCE_KILL_SIGNALS:
        return ""
    return "timeout"

#: RLIMIT_AS 相对内存上限的余量系数，以及它的**下限**（字节）。
#:
#: RLIMIT_AS 限制的是虚拟地址空间，通常比常驻内存大一个数量级
#: （实测 ffmpeg 抽一帧：RSS 56MB / 地址空间约 2GB），因此不能把它当内存上限用。
#: 下限取 2GB 是因为实测低于这个值正常 ffmpeg 就会失败；它只是防跑飞的兜底，
#: 真正生效的内存限制是 RLIMIT_DATA（见 ``_rlimit_hook``）。
_AS_HEADROOM_FACTOR = 4

#: RLIMIT_AS 兜底的**下限**（字节）。
#:
#: 这个值由实测决定，不能凭感觉取小：**Chromium 会预留极大的地址空间**。
#: 逐项二分的结果（本机、Playwright + chromium-1208）：
#:
#:     AS = 32GB  → 启动即 SIGTRAP（exitCode=null）
#:     AS = 64GB  → 正常
#:
#: 原先取 2GB（"够 ffmpeg 抽帧就行"），于是**任何经过 runner 的浏览器进程都会崩**，
#: 而现象是 `signal=SIGTRAP`、没有任何可读的错误 —— 看起来像"浏览器坏了"，
#: 实际是资源限制。取 128GB 给实测阈值留一倍余量，同时它仍然拦得住
#: "疯狂预留 TB 级地址空间"这种真正病态的行为（这才是这个兜底的本意）。
#:
#: 若将来某个浏览器仍在此限制下崩溃：先按上面的方法重新二分出阈值，再调这里。
_AS_FLOOR_BYTES = 128 * 1024**3


# ===========================================================================
# 资源约束
# ===========================================================================


@dataclass(frozen=True)
class ResourceLimits:
    """一次执行允许消耗的资源上限。"""

    #: 墙钟超时。到点即杀整棵进程树。
    timeout_sec: float = 30.0
    #: 内存硬上限（MB）。由内核机制执行（POSIX 走 RLIMIT_DATA，Windows 走 Job Object）。
    max_memory_mb: int = 2048
    #: CPU 时间上限（秒，仅 POSIX 生效）。防止"不吃内存但吃满 CPU"的活锁。
    max_cpu_sec: int | None = None
    #: 进程/线程数上限（仅 POSIX 生效）。防止 fork 炸弹。
    max_processes: int | None = 256
    #: 监控线程的采样间隔。越短越安全、开销越大；0.25s 是经验值。
    poll_interval_sec: float = 0.25

    def __post_init__(self) -> None:
        if self.timeout_sec <= 0:
            raise ValueError("timeout_sec 必须为正数")
        if self.max_memory_mb <= 0:
            raise ValueError("max_memory_mb 必须为正数")


@dataclass
class ExecResult:
    """一次沙盒执行的结果。"""

    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = 0.0

    #: 是否因超时被强制终止。
    timed_out: bool = False
    #: 被强制终止的原因：""（正常结束）/ "timeout" / "memory"。
    #: 用字符串而不是布尔，是因为"为什么被杀"直接决定上层该怎么处理：
    #: 超时通常值得重试（可能是偶发慢），内存超限往往说明代码本身有问题。
    killed_reason: str = ""
    #: 峰值内存（MB）。探测不可用时为 None —— 如实为空，不编造数字。
    peak_memory_mb: float | None = None
    #: 内存限制实际由谁执行：rlimit / job-object / monitor / none。
    memory_limit_enforced_by: str = "none"
    #: 网络隔离实际由谁执行：netns / none。**如实上报** ——
    #: 与内存那项同理，谎报"已隔离"比没有隔离更危险（见 netns.py 的说明）。
    network_isolation: str = "none"
    #: 只读根是否**确实生效**。
    #:
    #: 注意这与 network_isolation 的语义不同：网络隔离是"配置 + 能力"决定的，
    #: 而只读是**逐次执行**才知道结果的（某个挂载点重挂失败就会让这一层失效）。
    #: 因此它的取值来自子进程写回的状态报告，而不是配置。
    read_only_enforced: bool = False
    #: 没能变成只读的挂载点（含原因）。空列表 + enforced=true 才算真的只读。
    read_only_gaps: list[str] = field(default_factory=list)
    #: seccomp 过滤器是否**确实装载成功**（同样取自子进程写回的报告）。
    seccomp_enforced: bool = False
    #: 实际拦下的系统调用条数。
    seccomp_denied: int = 0
    #: 名单里**没能装上**的条目（例如内核不认识该系统调用）。
    #: 只报"拦了 39 条"而不报未解析项，会让人以为名单里每一条都生效了。
    seccomp_unresolved: list[str] = field(default_factory=list)
    #: 使用的超时值（便于错误信息里给出确切数字）。
    timeout_sec: float = 0.0

    @property
    def killed(self) -> bool:
        return bool(self.killed_reason)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.killed

    def tail(self, n: int = 2000) -> str:
        """stderr 尾部。渲染失败时这是**最有价值**的诊断信息，
        它会被回灌给编码智能体（见 agents/coder.py 的修复回路）。"""
        text = (self.stderr or self.stdout or "").strip()
        return text if len(text) <= n else "…" + text[-n:]

    def summary(self) -> str:
        """一行式摘要，用于日志与事件消息。"""
        if self.killed_reason == "timeout":
            return f"超时（>{self.timeout_sec:g}s）被强制终止"
        if self.killed_reason == "seccomp":
            return "被沙盒的系统调用策略终止（SIGSYS）"
        if self.killed_reason == "memory":
            limit = f"{self.peak_memory_mb:.0f}MB" if self.peak_memory_mb else "超限"
            return f"内存超限（{limit}）被强制终止"
        if self.returncode != 0:
            return f"退出码 {self.returncode}"
        return "执行成功"


# ===========================================================================
# 内存探测
# ===========================================================================


class MemoryProbe(Protocol):
    """采样一个进程树的峰值内存。返回 MB；不可用返回 None。"""

    def peak_mb(self) -> float | None:
        ...

    @property
    def source(self) -> str:
        ...


class NullMemoryProbe:
    """探测不可用时的占位实现。

    返回 None 而不是 0：0 会被误读成"内存占用极低"，而 None 明确表示
    "测不到" —— 这两者在排查问题时含义完全相反。
    """

    def peak_mb(self) -> float | None:
        return None

    @property
    def source(self) -> str:
        return "none"


class ProcMemoryProbe:
    """Linux：读 /proc/<pid>/status 的 VmHWM（峰值常驻内存）。

    注意它只覆盖**直接子进程**，不含孙进程（例如 Manim 派生的 LaTeX）。
    因此它只作为补充观测，硬限制仍由 RLIMIT_AS 承担 —— 后者会被子进程继承，
    天然覆盖整棵树。
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._path = Path(f"/proc/{pid}/status")

    def peak_mb(self) -> float | None:
        try:
            with self._path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        # 形如 "VmHWM:     123456 kB"
                        return int(line.split()[1]) / 1024.0
        except (OSError, ValueError, IndexError):
            return None
        return None

    @property
    def source(self) -> str:
        return "proc"


class WindowsJobMemoryProbe:
    """Windows：查询 Job Object 的 PeakJobMemoryUsed（覆盖 Job 内全部进程）。

    比逐进程采样准确得多：Manim 真正的内存大头是它派生的 LaTeX 进程，
    只盯父进程会严重低估。
    """

    def __init__(self, job_handle: int) -> None:
        self._job = job_handle

    def peak_mb(self) -> float | None:
        info = _windows_query_job_memory(self._job)
        if info is None:
            return None
        _peak_process, peak_job = info
        return peak_job / (1024.0 * 1024.0)

    @property
    def source(self) -> str:
        return "job-object"


# ===========================================================================
# Windows Job Object
# ===========================================================================
#
# 用 ctypes 直接调 Win32：这是 Windows 上唯一**内核级**的进程树内存限制手段。
# 全部包在 try/except 里并在失败时降级 —— 宁可少一层保护并如实上报，
# 也不要在受限环境里因为一个 ctypes 调用把整个渲染流程炸掉。

#: JOB_OBJECT_LIMIT_* 标志位（见 Windows SDK 的 winnt.h）。
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: JOBOBJECTINFOCLASS 枚举值。
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

if WINDOWS:  # pragma: no cover - 平台相关，Windows 上由测试覆盖

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_void_p),  # ULONG_PTR
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def _windows_create_job(max_memory_mb: int, max_processes: int | None) -> int | None:
    """创建带内存/进程数限制的 Job Object。

    返回 job handle；任何一步失败返回 None（调用方据此降级并如实上报）。
    """
    if not WINDOWS:  # pragma: no cover
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):  # pragma: no cover
        return None

    try:
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning("CreateJobObject 失败，内存限制将退化为监控线程")
            return None

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = _JOB_OBJECT_LIMIT_JOB_MEMORY | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = flags
        info.JobMemoryLimit = max_memory_mb * 1024 * 1024
        if max_processes:
            info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = max_processes

        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
        ]
        ok = kernel32.SetInformationJobObject(
            ctypes.c_void_p(job),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            logger.warning(
                "SetInformationJobObject 失败，内存限制将退化为监控线程",
                extra={"last_error": ctypes.get_last_error()},
            )
            _windows_close_handle(job)
            return None
        return job
    except Exception:  # noqa: BLE001 - ctypes 调用可能抛各种异常
        logger.warning("创建 Job Object 时发生异常，降级为监控线程", exc_info=True)
        return None


def _windows_assign_job(job: int, process_handle: int) -> bool:
    """把进程加入 Job Object。

    注意这里存在一个**极短的竞态窗口**：进程在 Popen 之后就已在运行，
    而赋值发生在之后。监控线程正是为了兜住这个窗口而存在。
    （更严格的做法是用 CREATE_SUSPENDED 创建再 ResumeThread，
    但那需要拿到主线程句柄，subprocess 不暴露，代价大于收益。）
    """
    if not WINDOWS or not job:  # pragma: no cover
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return bool(
            kernel32.AssignProcessToJobObject(
                ctypes.c_void_p(job), ctypes.c_void_p(process_handle)
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("AssignProcessToJobObject 失败", exc_info=True)
        return False


def _windows_query_job_memory(job: int) -> tuple[int, int] | None:
    """查询 (PeakProcessMemoryUsed, PeakJobMemoryUsed)，单位字节。"""
    if not WINDOWS or not job:  # pragma: no cover
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        kernel32.QueryInformationJobObject.restype = ctypes.c_int
        kernel32.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
        ]
        ok = kernel32.QueryInformationJobObject(
            ctypes.c_void_p(job),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        if not ok:
            return None
        return int(info.PeakProcessMemoryUsed), int(info.PeakJobMemoryUsed)
    except Exception:  # noqa: BLE001
        return None


def _windows_close_handle(handle: int | None) -> None:
    if not WINDOWS or not handle:  # pragma: no cover
        return
    try:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(handle))
    except Exception:  # noqa: BLE001
        logger.debug("CloseHandle 失败", exc_info=True)


# ===========================================================================
# 沙盒执行器
# ===========================================================================


@dataclass
class _KillFlag:
    """监控线程与主线程之间的终止原因传递。"""

    reason: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, reason: str) -> bool:
        """设置终止原因。返回 True 表示本次是第一个设置者（应当执行 kill）。"""
        with self.lock:
            if self.reason:
                return False
            self.reason = reason
            return True

    def get(self) -> str:
        with self.lock:
            return self.reason


class SandboxRunner:
    """受控子进程执行器。无共享可变状态，可被多线程共用。"""

    def __init__(
        self,
        network_isolation: IsolationMode = "auto",
        read_only: ReadOnlyMode = "off",
        seccomp: SeccompMode = "off",
    ) -> None:
        #: 网络隔离默认 auto：能用就用，不能用则如实上报而不是假装隔离。
        #: 只读默认 **off**：它会让 $HOME 下的缓存不可写（matplotlib/LaTeX 依赖），
        #: 开启前必须逐个引擎验证，不能替使用者默认打开。
        self.isolator = Isolator(network_isolation, read_only, seccomp)

    #: 允许子进程继承的环境变量白名单。
    #: 用白名单而不是黑名单：黑名单永远会漏（新增一个 *_KEY 就泄露了）。
    ENV_ALLOWLIST: tuple[str, ...] = (
        "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
        "SYSTEMROOT", "WINDIR", "PATHEXT", "NUMBER_OF_PROCESSORS",  # Windows 必需
        "SCID_RENDER_WIDTH", "SCID_RENDER_HEIGHT", "SCID_RENDER_FPS",
        # Playwright 的浏览器目录。它是一个**路径**配置而不是密钥，白名单里没有它
        # 会让沙盒里的浏览器找不到自己 —— 表现为截图全部失败，
        # 而原因（环境变量被裁掉了）从错误信息里完全看不出来。
        "PLAYWRIGHT_BROWSERS_PATH",
    )

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | Path,
        limits: ResourceLimits | None = None,
        env_extra: dict[str, str] | None = None,
        memory_probe: MemoryProbe | None = None,
    ) -> ExecResult:
        """执行命令并等待结束。

        ``argv`` 必须是**列表**而不是字符串：列表形式不经过 shell，
        从根本上杜绝了命令注入（沙盒里尤其不能容忍）。

        ``memory_probe`` 仅供测试注入；生产路径由平台自动选择。
        """
        limits = limits or ResourceLimits()
        env = self._build_env(env_extra)

        # 先确认可执行文件真的能找到，**再**考虑网络隔离包裹。
        #
        # 为什么必须前置：一旦命令被包进 `unshare`，找不到的就是 unshare 的子命令，
        # 于是报错变成 `unshare: failed to execute X: No such file or directory`、
        # 退出码 127 —— 而本方法对「可执行文件不存在」的既有约定是**返回 -1 与
        # 一句中文说明**（上层据此把「部署缺工具链」与「渲染真的失败」区分开）。
        # 不前置的话，这个约定会在开启隔离后**静默改变**：退出码从 -1 变成 127，
        # 依赖它的判断全部失灵。用子进程将要看到的 PATH 去解析，结果才与真实一致。
        if not self._executable_available(argv[0], env.get("PATH")):
            return ExecResult(
                command=argv,
                returncode=-1,
                stderr=f"找不到可执行文件：{argv[0]}",
                duration_sec=0.0,
                timeout_sec=limits.timeout_sec,
                memory_limit_enforced_by="none",
                network_isolation="none",
            )

        # 网络隔离在最外层包裹：先建命名空间，再在里面跑真正的命令。
        # 顺序很重要 —— rlimit 由 preexec_fn 在 fork 后设置，命名空间由 unshare
        # 在 exec 前建立，两者作用于同一个进程（以及它的后代），互不干扰。
        try:
            mechanism = self.isolator.network_mechanism()
            spawn_argv, status_path = self.isolator.wrap(argv, str(cwd))
        except (NetworkIsolationUnavailable, ReadOnlyUnavailable, SeccompUnavailable) as exc:
            # 这是**部署配置**问题而不是渲染问题，但仍返回结果而不是抛异常：
            # 与本方法对「找不到可执行文件」的处理保持一致 —— 让上层统一按渲染失败
            # 处理并把原因写进事件流。抛异常会穿透到图外层，把单个镜头的失败升级成
            # 整个任务崩掉（本项目已经因为这类穿透踩过一次）。
            # 真正的「快速失败」由启动期检查负责（见 service 的健康报告）。
            logger.error("沙盒网络隔离不可用，拒绝执行：%s", exc)
            return ExecResult(
                command=argv,
                returncode=-1,
                stderr=str(exc),
                duration_sec=0.0,
                timeout_sec=limits.timeout_sec,
                memory_limit_enforced_by="none",
                network_isolation="none",
            )

        job = None
        if WINDOWS:
            job = _windows_create_job(limits.max_memory_mb, limits.max_processes)

        popen_kwargs: dict[str, object] = {
            "cwd": str(cwd),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            # 新会话/新进程组：这样才能一次性杀掉整棵进程树。
            # Manim 会派生 LaTeX 子进程，只杀父进程会留下孤儿持续吃 CPU。
            "start_new_session": POSIX,
        }
        if not POSIX:  # pragma: no cover - Windows 分支
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if POSIX and (limits.max_memory_mb or limits.max_cpu_sec or limits.max_processes):
            popen_kwargs["preexec_fn"] = self._rlimit_hook(limits)

        started = time.monotonic()
        try:
            proc = subprocess.Popen(spawn_argv, **popen_kwargs)  # type: ignore[arg-type]
        except FileNotFoundError as exc:
            # 可执行文件不存在属于部署问题：返回结果而不是抛异常，
            # 让上层统一按"渲染失败"处理并记入 attempt。
            _windows_close_handle(job)
            return ExecResult(
                command=argv,
                returncode=-1,
                stderr=f"找不到可执行文件：{exc}",
                duration_sec=time.monotonic() - started,
                timeout_sec=limits.timeout_sec,
                memory_limit_enforced_by="none",
                network_isolation="none",
            )

        # --- 建立内存限制与探测 -------------------------------------------
        enforced_by = "none"
        probe: MemoryProbe = memory_probe or NullMemoryProbe()

        if WINDOWS:
            if job and _windows_assign_job(job, proc._handle):  # type: ignore[attr-defined]
                enforced_by = "job-object"
                if memory_probe is None:
                    probe = WindowsJobMemoryProbe(job)
        elif POSIX and limits.max_memory_mb:
            enforced_by = "rlimit"
            if memory_probe is None:
                probe = ProcMemoryProbe(proc.pid)

        kill_flag = _KillFlag()
        monitor = threading.Thread(
            target=self._monitor,
            args=(proc, limits, probe, kill_flag),
            name=f"sandbox-monitor-{proc.pid}",
            daemon=True,
        )
        monitor.start()

        # --- 等待结束 -------------------------------------------------------
        # 主线程也持有一个超时兜底：万一监控线程没能启动或本身出错，
        # 这里仍能保证进程一定会被终止（绝不允许挂死）。
        try:
            stdout_b, stderr_b = proc.communicate(timeout=limits.timeout_sec + KILL_GRACE_SEC)
        except subprocess.TimeoutExpired:
            if kill_flag.set("timeout"):
                self._kill_tree(proc)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=KILL_GRACE_SEC)
            except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
                stdout_b, stderr_b = b"", b""

        duration = time.monotonic() - started
        monitor.join(timeout=2.0)

        peak = probe.peak_mb()
        reason = kill_flag.get() or _classify_resource_exit(proc.returncode)

        result = ExecResult(
            command=argv,
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=_decode(stdout_b),
            stderr=_decode(stderr_b),
            duration_sec=duration,
            timed_out=(reason == "timeout"),
            killed_reason=reason,
            peak_memory_mb=peak,
            memory_limit_enforced_by=enforced_by,
            network_isolation=mechanism,
            timeout_sec=limits.timeout_sec,
        )
        # 只读报告由子进程写回：逐次执行才知道某个挂载点有没有保护上。
        if status_path != "":
            report = read_isolation_report(status_path)
            result.read_only_enforced = report["read_only_enforced"]
            result.read_only_gaps = report["read_only_gaps"]
            result.seccomp_enforced = report["seccomp_enforced"]
            result.seccomp_denied = report["seccomp_denied"]
            result.seccomp_unresolved = report["seccomp_unresolved"]
            # **必须让人看见**：配置要求了加固、而某一层没生效，这是
            # "以为加固了其实没有"的典型场景。不打日志的话，
            # 它只会体现在一个没人看的字段里。
            if self.isolator.read_only_mechanism() != "none" and not result.read_only_enforced:
                logger.warning(
                    "沙盒只读未生效",
                    extra={"command": argv[0], "gaps": result.read_only_gaps, "workdir": str(cwd)},
                )
            if self.isolator.seccomp_mechanism() != "none" and not result.seccomp_enforced:
                logger.warning(
                    "沙盒 seccomp 未生效",
                    extra={"command": argv[0], "gaps": result.read_only_gaps, "workdir": str(cwd)},
                )

        if job:
            # KILL_ON_JOB_CLOSE 保证句柄关闭时残留子进程一并被清理。
            _windows_close_handle(job)

        if result.killed:
            logger.warning(
                "沙盒执行被强制终止",
                extra={
                    "reason": reason,
                    "command": argv[0],
                    "duration_sec": round(duration, 2),
                    "peak_memory_mb": round(peak, 1) if peak else None,
                    "timeout_sec": limits.timeout_sec,
                    "max_memory_mb": limits.max_memory_mb,
                },
            )
        return result

    def run_python(
        self,
        script_path: str | Path,
        *,
        cwd: str | Path,
        limits: ResourceLimits | None = None,
        args: list[str] | None = None,
        env_extra: dict[str, str] | None = None,
        python_bin: str | None = None,
        memory_probe: MemoryProbe | None = None,
    ) -> ExecResult:
        """用指定解释器执行一个 Python 脚本。

        统一走这里而不是让各调用点自己拼 ``[sys.executable, path]``：
        解释器是可配置的（生产可能换成受限解释器），沙盒化的入口应当只有一个。
        """
        argv = [python_bin or sys.executable, str(script_path), *(args or [])]
        return self.run(
            argv, cwd=cwd, limits=limits, env_extra=env_extra, memory_probe=memory_probe
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _executable_available(program: str, path: str | None) -> bool:
        """按子进程将要看到的 PATH 判断可执行文件能否找到。

        带路径分隔符的写法按路径直接判断（不查 PATH）——
        与 `execvp` 的语义一致：含 `/` 时不搜索 PATH。
        """
        if os.sep in program or (os.altsep and os.altsep in program):
            return os.path.isfile(program) and os.access(program, os.X_OK)
        return shutil.which(program, path=path) is not None

    def _build_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        """构造子进程环境：白名单继承 + 显式追加。

        关键点：**绝不透传 OPENAI_API_KEY 之类的密钥**。
        渲染代码是我们无法完全信任的，给它密钥等于把钥匙交给陌生人。
        """
        env: dict[str, str] = {}
        for key in self.ENV_ALLOWLIST:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value

        # 让子进程输出不带缓冲，否则被超时杀掉时日志会全部丢失。
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # **强制子进程用 UTF-8 输出**。
        #
        # 不加这两项会踩一个很隐蔽的坑：Windows 下 Python 子进程默认按系统
        # 代码页（中文环境是 GBK）编码 stderr，而父进程按 UTF-8 解码，
        # 于是所有中文报错都变成乱码。后果不是"日志不好看"，而是
        # **回灌给编码智能体的错误信息全部不可读** —— 重试回路直接失效。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # matplotlib（Manim 依赖）在无显示环境下必须用 Agg 后端。
        env.setdefault("MPLBACKEND", "Agg")

        if extra:
            env.update(extra)
        return env

    def _rlimit_hook(self, limits: ResourceLimits):  # pragma: no cover - 仅 POSIX
        """返回设置资源上限的 preexec_fn。

        用 RLIMIT 而不是纯轮询监控：**内核级强制执行**，脚本无法绕过
        （轮询总有"在两次采样之间吃满内存"的窗口）。
        子进程会继承这些限制，因此天然覆盖 Manim 派生的 LaTeX 等孙进程。
        """
        mem_bytes = limits.max_memory_mb * 1024 * 1024
        cpu_sec = limits.max_cpu_sec
        max_procs = limits.max_processes

        def _apply() -> None:
            import resource

            # 内存上限交给 RLIMIT_DATA，**不是** RLIMIT_AS。
            #
            # RLIMIT_AS 限制的是虚拟地址空间：映射进来的共享库、每个线程的栈、
            # 编解码器预留的缓冲全算在里面，与「进程实际用了多少内存」差着数量级。
            # 实测本机 ffmpeg 抽一帧：RSS 只有 56MB，却需要约 2GB 地址空间
            # （1.75GB 时建不起 swscale 图，2GB 正常）。
            #
            # 把 max_memory_mb 直接当 AS 上限，后果不是「限制得比较严」，而是
            # **正常进程在远未触及内存上限时就被杀掉**；而 ffmpeg 在这种情况下的
            # 退出码仍然是 0、产物却是空的 —— 表现成「抽帧全部失败」，
            # 一路静默降级到「VLM 审查不可用」，没有任何一处会报错。
            #
            # RLIMIT_DATA（Linux 4.7+ 覆盖 brk 与私有匿名映射）的语义与 Windows
            # Job Object 的「提交内存」一致，这才是这个配置项想表达的东西。
            try:
                resource.setrlimit(resource.RLIMIT_DATA, (mem_bytes, mem_bytes))
            except (ValueError, OSError):
                # 某些容器/内核不允许设置；不阻断执行，由监控线程兜底。
                pass

            # RLIMIT_AS 降级为**兜底**，且必须留出地址空间余量：
            # 它拦住的是「疯狂预留地址空间」的进程，而不是内存用量本身。
            as_bytes = max(mem_bytes * _AS_HEADROOM_FACTOR, _AS_FLOOR_BYTES)
            try:
                resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
            except (ValueError, OSError):
                pass
            if cpu_sec:
                try:
                    # 软限制到点发 SIGXCPU，硬限制稍后 SIGKILL —— 给进程留出
                    # 打印堆栈的机会，便于排查"是哪一步耗尽了 CPU"。
                    resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec + 10))
                except (ValueError, OSError):
                    pass
            if max_procs:
                try:
                    resource.setrlimit(resource.RLIMIT_NPROC, (max_procs, max_procs))
                except (ValueError, OSError):
                    pass

        return _apply

    @staticmethod
    def _monitor(
        proc: subprocess.Popen,
        limits: ResourceLimits,
        probe: MemoryProbe,
        kill_flag: _KillFlag,
    ) -> None:
        """监控线程：采样内存占用，超限即杀。

        它在两个方向上提供保护：
        * 内存：内核限制之外的**第二道防线**，同时兜住 Windows Job Object
          "赋值前"的竞态窗口；
        * 超时：即使主线程的 communicate 因某种原因没能返回，
          它也保证进程会被终止（``SandboxRunner`` 绝不允许挂死）。
        """
        deadline = time.monotonic() + limits.timeout_sec + KILL_GRACE_SEC
        limit_mb = float(limits.max_memory_mb)

        while proc.poll() is None:
            if time.monotonic() > deadline:
                if kill_flag.set("timeout"):
                    SandboxRunner._kill_tree(proc)
                return

            peak = probe.peak_mb()
            if peak is not None and peak > limit_mb:
                if kill_flag.set("memory"):
                    logger.warning(
                        "内存超限，强制终止进程树",
                        extra={
                            "peak_memory_mb": round(peak, 1),
                            "limit_mb": limits.max_memory_mb,
                            "probe": probe.source,
                        },
                    )
                    SandboxRunner._kill_tree(proc)
                return

            time.sleep(limits.poll_interval_sec)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """杀掉整个进程组。

        只 ``proc.kill()`` 是不够的：Manim 会派生 LaTeX / dvisvgm，
        那些子进程不在我们直接持有的句柄上，会变成孤儿继续吃满 CPU ——
        这正是"防止死循环导致系统崩溃"里最容易被忽略的一环。
        """
        if proc.poll() is not None:
            return
        try:
            if POSIX:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:  # pragma: no cover - Windows 分支
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            # 进程已经自己退出，或权限不足：退回到单进程 kill 兜底。
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                logger.debug("kill 进程失败", exc_info=True)


def _decode(raw: bytes | None) -> str:
    """解码子进程输出并截断到上限。

    渲染工具的输出常含非 UTF-8 字节（LaTeX 报错、Windows 代码页），
    用 ``errors="replace"`` 而不是严格解码 —— 因为一条乱码就丢掉
    全部诊断信息，代价太大了。
    """
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES] + "\n…（输出已截断）"
    return text

def read_isolation_report(status_path: str) -> dict[str, Any]:
    """读子进程写回的隔离状态报告并删除它。

    **读不到就返回"都没生效"** —— 保守失败：拿不到证据就不声称已加固。
    反过来（读不到就当成功）会让这些机制在报告链路坏掉时静默失效，而那正是
    "以为加固了其实没有"的典型场景。
    """
    import json
    from pathlib import Path

    empty = {
        "read_only_enforced": False,
        "read_only_gaps": ["未取到隔离状态报告 —— 按未生效处理"],
        "seccomp_enforced": False,
        "seccomp_denied": 0,
        "seccomp_unresolved": [],
    }
    path = Path(status_path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty
    finally:
        # 状态文件写在工作目录里，读完即删，避免污染产物目录。
        try:
            path.unlink()
        except OSError:
            pass

    gaps = [str(g) for g in (data.get("readonly_gaps") or [])]
    if err := data.get("error"):
        gaps.append(str(err))
    if msg := data.get("seccomp_error"):
        gaps.append(str(msg))
    return {
        "read_only_enforced": bool(data.get("readonly_enforced", False)),
        "read_only_gaps": gaps,
        "seccomp_enforced": bool(data.get("seccomp_enforced", False)),
        "seccomp_denied": int(data.get("seccomp_denied", 0) or 0),
        "seccomp_unresolved": [str(u) for u in (data.get("seccomp_unresolved") or [])],
    }
