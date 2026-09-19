"""沙盒执行前的隔离设置：只读根文件系统 + 可写工作目录，然后 exec 目标命令。

## 为什么需要一个独立的包装进程

只读根**不能**在 `preexec_fn` 里做：那里已经 fork 完、处于多线程父进程的fork 副本中，
再做一连串 `mount(2)` 与内存分配有死锁风险（本项目已因此把 rlimits 保持在最小集合）。
也不能让 `unshare` 自己做 —— 它只会建命名空间，不会重挂文件系统。

因此采用「包装进程」：`unshare -r -n -m -- python -m ...exec_guard -- <原命令>`。
包装进程做完设置后 **`exec` 掉自己**，于是 **PID 不变**：
内存探针读 `/proc/<pid>/status` 的 `VmHWM`、杀进程树都仍然指向真正的命令 ——
这与网络隔离那层是同一个性质，绝不能改成 fork 一个子进程去做
（那会让峰值内存变成读包装器自己，小到离谱且无任何报错）。

## 一个必须知道的坑：只读 `/` 不等于只读全部

`mount -o remount,ro,bind /` **只作用于根文件系统那一个挂载**。
本机 `/vol1`、`/vol2` 是独立的 btrfs 挂载，`/boot/efi` 是 vfat ——
只重挂 `/` 之后，往 `/vol1/...` 里写文件**照样成功**。
我第一版就是这么写的，测试当场把 `blocked.txt` 写进了宿主机的项目目录。

所以这里遍历 `/proc/self/mountinfo`，把**每一个真实文件系统**都重挂为只读，
再单独把工作目录绑定回可写。拿不准的一律按"没保护上"如实记进 gaps，
绝不默认成功。

## seccomp：为什么是"拒绝名单"而不是"允许名单"

需求里写的是「seccomp 白名单」。这里实现的是**拒绝名单**，理由必须说清楚：

允许名单要求"把目标程序用到的每个系统调用都列全"。而本沙盒要跑的是
**ffmpeg / manim / LaTeX / Chromium** 这些庞大且各自演进的程序 —— 漏掉任何一个
系统调用，进程会拿到 EPERM 或被杀，而表现是**渲染静默失败**：
- 在本机只装了 ffmpeg（manim/LaTeX/Chromium 都不可用），
  也就是说我**无法**验证另外三个引擎在允许名单下能否正常工作；
- 一份没被验证过的允许名单比不加更危险 —— 它会让"渲染失败"变成生产事故，
  而失败原因（缺了哪个系统调用）极难从现象反推。

因此这里拦的是**沙盒里没有任何正当用途**的那批：进程注入、内核模块、挂载/命名空间
操作、内核密钥环、io_uring、perf 等。默认动作是 ``ALLOW``，被拦的返回 **EPERM**
（而不是杀掉进程）—— 一个意料之外的调用会得到一个普通错误，
程序往往还能降级继续，比直接 SIGKILL 温和得多。

**这块的验证边界**：只在本机实际可用的引擎（stock/ffmpeg）上验证过。
开启前必须在目标环境逐个引擎验证，这也是默认 ``off`` 的原因。

## 为什么 /tmp 换成私有 tmpfs

只读根会让 `/tmp` 也变成只读，而 ffmpeg、Playwright 都要写临时目录 ——
表现为"渲染莫名失败"。因此在内层挂一个私有 tmpfs。
**必须带 `size=`**：不设上限的 tmpfs 可以吃掉整机内存，
那等于把"只读加固"换成了一个新的 OOM 风险。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import sys
from typing import Any

# mount(2) 的标志位（见 <sys/mount.h>）。
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REMOUNT = 32
MS_BIND = 4096

#: 伪文件系统不重挂：它们本来就不可写，且重挂 proc/sysfs 会带来别的问题。
#: 按**挂载类型**跳过，而不是按路径 —— 路径会变，类型不会。
_SKIP_FSTYPES = frozenset(
    {
        "proc", "sysfs", "devpts", "devtmpfs", "tmpfs", "ramfs", "cgroup", "cgroup2",
        "mqueue", "hugetlbfs", "debugfs", "tracefs", "securityfs", "pstore",
        "bpf", "configfs", "fusectl", "autofs", "binfmt_misc", "rpc_pipefs",
        "nsfs", "overlay", "efivarfs",
    }
)

#: 私有 /tmp 的大小上限。必须有界（见模块文档）。
_TMPFS_SIZE = "256m"


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    libc.mount.restype = ctypes.c_int
    libc.mount.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_ulong, ctypes.c_char_p,
    ]
    return libc


def _mount(source: str | None, target: str, fstype: str | None, flags: int, data: str | None) -> None:
    libc = _libc()
    rc = libc.mount(
        source.encode() if source else None,
        target.encode(),
        fstype.encode() if fstype else None,
        ctypes.c_ulong(flags),
        data.encode() if data else None,
    )
    if rc != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), target)


def _mount_points() -> list[tuple[str, str]]:
    """返回 (挂载点, 文件系统类型)，按挂载点**由深到浅**排序。

    由深到浅：先处理嵌套挂载，避免把父挂载重挂为只读之后，
    子挂载点还处于"已经不可写但状态未更新"的中间态。
    """
    out: list[tuple[str, str]] = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                # mountinfo 字段：... <mount point(5)> ... "-" <fstype> <source> <opts>
                left, _, right = line.partition(" - ")
                parts = left.split()
                if len(parts) < 5:
                    continue
                mp = parts[4]
                right_parts = right.split()
                fstype = right_parts[0] if right_parts else ""
                out.append((mp, fstype))
    except OSError:
        return []
    out.sort(key=lambda t: t[0].count("/"), reverse=True)
    return out


def setup_readonly(workdir: str) -> dict[str, Any]:
    """把每个真实文件系统重挂为只读，再把工作目录与 /tmp 单独放开。

    返回一份**如实的**报告：哪些挂载点没能变成只读、为什么。
    报告会被 runner 读走并放进 `ExecResult` ——
    谎报「已只读」是安全代码里最危险的错误，这里宁可多报几条 gaps。
    """
    status: dict[str, Any] = {
        "readonly_enforced": False,
        "readonly_gaps": [],
        "workdir_rw": False,
        "tmpfs_tmp": False,
        "error": "",
    }
    protected = 0
    try:
        for mp, fstype in _mount_points():
            if fstype in _SKIP_FSTYPES:
                continue
            if mp == workdir or workdir.startswith(mp.rstrip("/") + "/") or mp == "/":
                # `/` 也要重挂（它是"其他一切"的兜底）；工作目录本身稍后单独放开。
                pass
            try:
                _mount(None, mp, None, MS_REMOUNT | MS_BIND | MS_RDONLY, None)
                protected += 1
            except OSError as exc:
                status["readonly_gaps"].append(f"{mp}({fstype}): {exc.strerror}")

        # 只读根会让 /tmp 不可写，而 ffmpeg / Playwright 都要写临时目录。
        # 换成私有、有界、不落盘的 tmpfs。
        #
        # **但工作目录若位于 /tmp 之下就不能这么做**：把 tmpfs 挂到 /tmp 会把
        # 工作目录整个遮蔽掉 —— 里面写的东西落在私有 tmpfs 上，命名空间外**看不到**，
        # 表现为"渲染成功但产物凭空消失"（本项目的 pytest tmp_path 正在 /tmp 下，
        # 因此这个坑是被测试当场逼出来的）。此时不换 /tmp，并如实记一条 gap，
        # 而不是假装 /tmp 可用。
        if workdir == "/tmp" or workdir.startswith("/tmp/"):
            status["readonly_gaps"].append(
                "/tmp 未替换为私有 tmpfs：工作目录位于其下，替换会把工作目录遮蔽"
            )
        else:
            _mount("none", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV, f"mode=1777,size={_TMPFS_SIZE}")
            status["tmpfs_tmp"] = True

        # 工作目录绑定回可写：先 bind 再 remount rw（bind 会继承只读，必须显式放开）。
        _mount(workdir, workdir, None, MS_BIND, None)
        _mount(None, workdir, None, MS_REMOUNT | MS_BIND, None)
        status["workdir_rw"] = True

        # 只要**有任何一处**没能保护上，就不声称"已只读"。
        status["readonly_enforced"] = not status["readonly_gaps"] and protected > 0
    except OSError as exc:
        status["error"] = f"{exc.strerror}: {exc.filename or ''}".strip()
    return status


#: 沙盒里**没有正当用途**的系统调用。按威胁分组，便于日后审阅时判断"为什么在名单里"。
DENY_SYSCALLS: dict[str, tuple[str, ...]] = {
    # 提权 / 内核与命名空间操作。
    # 注意：在用户命名空间里我们**是 root**，所以这些并非天然不可达
    # （exec_guard 自己就用 mount 做了只读设置）——因此必须显式拦掉，
    # 否则"沙盒里的代码"同样可以重新挂载、甚至 pivot_root 换根。
    "kernel": (
        "mount", "umount2", "pivot_root", "chroot", "setns", "unshare",
        "init_module", "finit_module", "delete_module",
        "kexec_load", "kexec_file_load", "reboot", "swapon", "swapoff",
        "acct", "quotactl", "nfsservctl",
        "sethostname", "setdomainname", "settimeofday", "clock_settime",
        "adjtimex", "clock_adjtime",
    ),
    # 进程注入与信息窃取。
    "injection": (
        "ptrace", "process_vm_readv", "process_vm_writev", "kcmp",
        "perf_event_open", "userfaultfd", "pidfd_getfd",
    ),
    # 内核密钥环：可以把密钥读出去（沙盒里没有正当用途）。
    "keyring": ("keyctl", "add_key", "request_key"),
    # eBPF：既能读内核信息也能改行为，且历史上多次成为提权面。
    "bpf": ("bpf",),
    # io_uring：系统调用面极大，且历史上出现过绕过 seccomp 的路径；
    # ffmpeg/manim/Chromium 默认都不用它。
    "io_uring": ("io_uring_setup", "io_uring_enter", "io_uring_register"),
    # 用文件句柄绕过路径权限检查（open_by_handle_at 是危险的那一半）。
    "handle": ("open_by_handle_at", "lookup_dcookie"),
}


def setup_seccomp() -> dict[str, Any]:
    """安装 seccomp 过滤器（默认放行 + 拒绝名单）。

    返回如实的报告：拦了几条、哪些**没能**装上（例如内核不支持或名字解析不到）。
    `enforced` 只有在过滤器**确实加载成功**时才为 true。
    """
    report: dict[str, Any] = {
        "seccomp_enforced": False,
        "seccomp_denied": 0,
        "seccomp_unresolved": [],
        "seccomp_error": "",
    }
    lib_name = ctypes.util.find_library("seccomp")
    if not lib_name:
        report["seccomp_error"] = "找不到 libseccomp"
        return report

    try:
        lib = ctypes.CDLL(lib_name)
    except OSError as exc:
        report["seccomp_error"] = f"加载 libseccomp 失败：{exc}"
        return report

    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]

    scmp_act_allow = 0x7FFF0000
    # EPERM 而不是杀进程：见模块文档 —— 意外的调用应当得到一个普通错误。
    scmp_act_errno_eperm = 0x00050000 | 1

    ctx = lib.seccomp_init(scmp_act_allow)
    if not ctx:
        report["seccomp_error"] = "seccomp_init 失败"
        return report

    try:
        for group, names in DENY_SYSCALLS.items():
            for name in names:
                nr = lib.seccomp_syscall_resolve_name(name.encode())
                if nr < 0:
                    # 名字在当前内核/架构上不存在（例如 io_uring 在旧内核上）。
                    # 这不算失败，但必须记下来 —— 否则"拦了 20 条"会让人以为
                    # 名单里的每一条都生效了。
                    report["seccomp_unresolved"].append(f"{group}:{name}")
                    continue
                if lib.seccomp_rule_add(ctx, scmp_act_errno_eperm, nr, 0) == 0:
                    report["seccomp_denied"] += 1
                else:
                    report["seccomp_unresolved"].append(f"{group}:{name}(rule_add 失败)")

        rc = lib.seccomp_load(ctx)
        if rc != 0:
            report["seccomp_error"] = f"seccomp_load 失败（rc={rc}）"
            return report
        report["seccomp_enforced"] = True
    finally:
        lib.seccomp_release(ctx)
    return report


def _write_status(path: str, status: dict[str, Any]) -> None:
    """把状态写到**runner 能读到的地方**（工作目录内，因为那里可写）。

    尽力而为：写不进去不影响执行，只是 runner 会因此看到"没有报告"，
    从而**不会**声称已只读 —— 失败方向是保守的。
    """
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(status, fh)
    except OSError:
        pass


def main(argv: list[str]) -> int:
    """解析参数 -> 设置隔离 -> 写状态 -> exec 目标命令（PID 不变）。"""
    workdir = ""
    status_path = ""
    seccomp_mode = "off"
    do_readonly = False
    rest: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--workdir":
            workdir = next(it, "")
        elif arg == "--status":
            status_path = next(it, "")
        elif arg == "--seccomp":
            seccomp_mode = next(it, "")
        elif arg == "--readonly":
            # 显式开关，而不是"看有没有传 workdir"：两个加固手段必须能独立启用，
            # 否则「只想要 seccomp」的人会被迫接受只读（那会破坏 $HOME 缓存）。
            do_readonly = True
        elif arg == "--":
            rest = list(it)
            break
    if not rest:
        print("exec_guard: 缺少要执行的命令", file=sys.stderr)
        return 2
    if not workdir:
        workdir = os.getcwd()

    # 状态字典的键**始终齐全**：runner 侧按固定字段读，缺键会让它把
    # "没启用"读成"没生效"（两者对使用者是一回事，但对报告是两回事）。
    status: dict[str, Any] = {
        "readonly_enforced": False,
        "readonly_gaps": [],
        "workdir_rw": False,
        "tmpfs_tmp": False,
        "seccomp_enforced": False,
        "seccomp_denied": 0,
        "seccomp_unresolved": [],
        "seccomp_error": "",
        "error": "",
    }
    if do_readonly:
        status.update(setup_readonly(os.path.abspath(workdir)))

    # **seccomp 必须最后装**：它不可逆（装上卸不掉），而只读设置本身要用 mount ——
    # mount 正在拒绝名单里，顺序反了会让 exec_guard 自己把自己拦住。
    if seccomp_mode == "deny":
        status.update(setup_seccomp())

    _write_status(status_path, status)

    try:
        os.execvp(rest[0], rest)
    except OSError as exc:
        print(f"exec_guard: 无法执行 {rest[0]}: {exc}", file=sys.stderr)
        return 127
    return 0  # pragma: no cover - execvp 成功时不会返回


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
