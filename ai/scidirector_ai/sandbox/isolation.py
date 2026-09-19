"""沙盒隔离的组合层：把「网络」与「只读文件系统」两个关注点拼成一条命令。

## 为什么要有这一层

两个关注点最终必须落在**同一次 `unshare`** 上（`-n` 建网络命名空间、`-m` 建挂载命名空间），
而且只读设置必须在 `unshare` **之后**、目标命令**之前**执行 —— 那需要一个包装进程
（见 `exec_guard.py`）。把这些拼装规则散在 runner 里，很容易出现
「加了 `-m` 但忘了挂 guard」这类**静默失效**：命令照跑，只是没有任何只读保护。

## PID 不变这一条必须守住

链路是：`unshare`（exec 目标）→ `python -m exec_guard`（exec 目标）→ 真正的命令。
每一步都用 exec 换像，因此**全程同一个 PID**。这不是巧合，是要求：
runner 用 `Popen.pid` 去读 `/proc/<pid>/status` 的 `VmHWM` 做内存探针，
一旦中间某一步改成 fork 子进程，峰值内存就会变成读那个中间进程 ——
数字小到离谱，且没有任何报错。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Literal

from .netns import NetworkIsolationUnavailable, network_isolation_available

#: 只读开关的模式。
#: - ``off``（缺省）：不启用。**为什么默认关**：只读根会让 ``$HOME`` 下的缓存
#:   （matplotlib 字体缓存、LaTeX 缓存）也不可写，而 manim/LaTeX 依赖它们 ——
#:   开启前必须逐个引擎验证，不能替使用者默认打开。
#: - ``auto``   ：能用就用，不能用则如实上报。
#: - ``require``：必须生效，否则拒绝执行（fail closed）。
ReadOnlyMode = Literal["off", "auto", "require"]

#: 工作目录内的状态文件名。runner 读完即删，避免污染产物目录。
STATUS_FILE_NAME = ".scid_sandbox_status.json"


def read_only_available() -> bool:
    """只读隔离是否可用。

    只检查 ``unshare -r -m`` 能不能用（本机实测可用）；真正的"有没有保护上"由
    每次执行的**状态报告**回答 —— 因为某些挂载点可能重挂失败，
    那是逐次执行的属性，不是"本机支不支持"的属性。
    """
    exe = shutil.which("unshare")
    if not exe:
        return False
    import subprocess

    try:
        proc = subprocess.run(
            [exe, "-r", "-m", "--", "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


class Isolator:
    """按配置把命令包装成隔离形式。"""

    def __init__(
        self,
        network_mode: str = "auto",
        read_only_mode: ReadOnlyMode = "off",
        unshare_bin: str = "unshare",
    ) -> None:
        self.network_mode = network_mode
        self.read_only_mode = read_only_mode
        self.unshare_bin = unshare_bin

    # ------------------------------------------------------------------
    # 机制汇报（如实，不猜）
    # ------------------------------------------------------------------

    def network_mechanism(self) -> str:
        if self.network_mode == "off":
            return "none"
        return "netns" if network_isolation_available(self.unshare_bin) else "none"

    def read_only_mechanism(self) -> str:
        """配置上打算用哪种只读机制。

        注意：**这不等于"已经只读了"**。逐次执行的真实结果由状态报告给出
        （见 `exec_guard.setup_readonly` 的 gaps）—— 因为某个挂载点重挂失败
        会让这一层实际失效，而那是执行期才知道的。
        """
        if self.read_only_mode == "off":
            return "none"
        return "mountns" if read_only_available() else "none"

    # ------------------------------------------------------------------
    # 包装
    # ------------------------------------------------------------------

    def wrap(self, argv: list[str], workdir: str) -> list[str]:
        """返回真正要执行的 argv，以及状态文件路径（未启用只读时为空）。

        抛 ``NetworkIsolationUnavailable`` 表示配置要求隔离但拿不到。
        """
        want_net = self.network_mode != "off" and network_isolation_available(self.unshare_bin)
        if self.network_mode == "require" and not want_net:
            raise NetworkIsolationUnavailable(
                "沙盒网络隔离被要求（SCID_SANDBOX_NETWORK_ISOLATION=require）"
                "但本机不可用：需要 unshare 且允许非特权用户命名空间"
            )

        want_ro = self.read_only_mode != "off" and read_only_available()
        if self.read_only_mode == "require" and not want_ro:
            raise ReadOnlyUnavailable(
                "沙盒只读隔离被要求（SCID_SANDBOX_READ_ONLY=require）"
                "但本机不可用：需要 unshare -r -m（非特权挂载命名空间）"
            )

        if not want_net and not want_ro:
            return argv, ""

        exe = shutil.which(self.unshare_bin) or self.unshare_bin
        flags = ["-r"]
        if want_net:
            flags.append("-n")
        if want_ro:
            flags.append("-m")

        if not want_ro:
            # 只要网络隔离：不需要包装进程，unshare 直接 exec 目标（PID 不变）。
            return [exe, *flags, "--", *argv], ""

        # 需要只读：挂一层包装进程做 mount 设置，做完 exec 掉自己（PID 不变）。
        #
        # 用**绝对脚本路径**而不是 `-m scidirector_ai.sandbox.exec_guard`：
        # runner 会把子进程环境裁剪到白名单（不含 PYTHONPATH），子进程的 cwd 又是
        # 工作目录，因此 `-m` 找不到包 —— 表现为包装器直接 ModuleNotFoundError 退出、
        # 命令根本没跑，而状态报告缺失只会让 read_only_enforced 为 false，
        # **看起来就像"只读没生效"而不是"整个包装器没起来"**。
        # exec_guard 只依赖标准库，因此按脚本路径执行是最稳的。
        status_path = f"{workdir.rstrip('/')}/{STATUS_FILE_NAME}"
        guard_script = str(Path(__file__).with_name("exec_guard.py"))
        guard = [
            sys.executable, guard_script,
            "--workdir", workdir, "--status", status_path, "--",
        ]
        return [exe, *flags, "--", *guard, *argv], status_path


class ReadOnlyUnavailable(RuntimeError):
    """要求只读隔离但本机不可用。"""
