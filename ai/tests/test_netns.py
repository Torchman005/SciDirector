"""沙盒网络隔离（阶段五·沙盒加固）。

验收标准是「沙盒在无网络条件下**仍能完成渲染**（证明没有隐式外联）」。
这句话有两个方向，缺一个都算没验：

1. **外联确实被挡住** —— 否则「渲染成功」什么都证明不了，
   因为代码本来就能一边外联一边渲染；
2. **渲染确实还能成功** —— 否则只是把功能弄坏了，同样不算加固。

因此下面的用例成对出现，且第 1 条必须带**反向对照**：同一个动作在不隔离时
必须成功。少了它，一个「什么都连不上」的环境（比如 CI 本来就断网）
会让「外联被挡住」永远为真 —— 那是假阳性，不是证据。

**跳过而不是失败**：不支持用户命名空间的机器（容器、`unprivileged_userns_clone=0`、
非 Linux）上这些用例无意义。但跳过原因里必须写清**跳过了什么**，
让人一眼看出覆盖缺口 —— 与项目里其他条件跳过保持同一约定。

## 覆盖边界（不要把它读成「全部引擎都已隔离」）

本模块隔离的是**经 `SandboxRunner` 跑的子进程**：manim 渲染、ffmpeg/ffprobe 编码与探测。

**HTML 引擎（d3 / echarts / code_anim）不在其中**：`HtmlRenderer._capture` 是在
AI 服务进程里直接 `sync_playwright()` 起 Chromium，不经过 runner，因此这里加的网络
命名空间**覆盖不到它**。这是当前的真实缺口，已记入 `docs/ROADMAP.md` 与 `Agent.md` §9，
不要误以为「跑绿了就等于所有渲染都隔离了」。

（Chromium 本身**能**在无网络命名空间里正常渲染 —— 实测截图与隔离前逐字节相同，
因为它用 `--remote-debugging-pipe` 而非 TCP。真正拦住「把 Chromium 也放进 runner」
的是另一件事：runner 的 `RLIMIT_AS` 兜底，见 §9。）
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from scidirector_ai.sandbox.isolation import Isolator
from scidirector_ai.sandbox.netns import (
    NetworkIsolationUnavailable,
    network_isolation_available,
    reset_probe_cache,
)
from scidirector_ai.sandbox.runner import ResourceLimits, SandboxRunner

requires_netns = pytest.mark.skipif(
    not network_isolation_available(),
    reason="本机不支持非特权用户命名空间（unshare -rn），跳过网络隔离验证",
)


@pytest.fixture(autouse=True)
def _fresh_probe() -> None:
    """每个用例都重新探测：探测结果是进程级缓存的，
    而这里的用例会**故意**在探测期间制造不同的环境（如 off/require 模式）。"""
    reset_probe_cache()
    yield
    reset_probe_cache()


# ---------------------------------------------------------------------------
# 1) 外联确实被挡住（带反向对照）
# ---------------------------------------------------------------------------


#: 尝试外联的最小脚本。用**真实 connect** 而不是「查一下有没有网卡」：
#: 后者只能说明环境的样子，说明不了「这段代码能不能把数据发出去」。
_EGRESS_PROBE = textwrap.dedent(
    """
    import socket, sys
    try:
        s = socket.create_connection(("8.8.8.8", 53), timeout=5)
        s.close()
        print("REACHABLE")
    except Exception as exc:
        print("BLOCKED", type(exc).__name__)
    """
)


def _run_egress_probe(runner: SandboxRunner, tmp_path):
    return runner.run(
        [sys.executable, "-c", _EGRESS_PROBE],
        cwd=tmp_path,
        limits=ResourceLimits(timeout_sec=30),
    )


@requires_netns
def test_sandbox_blocks_outbound_connections(tmp_path) -> None:
    res = _run_egress_probe(SandboxRunner("auto"), tmp_path)

    assert res.network_isolation == "netns", "隔离未生效，这条用例就没有意义"
    assert res.returncode == 0, res.tail()
    assert "BLOCKED" in res.stdout, f"外联未被挡住：{res.stdout!r}{res.tail()}"


@requires_netns
def test_outbound_connection_succeeds_without_isolation(tmp_path) -> None:
    """**反向对照。**

    没有它，「外联被挡住」在一个本来就上不了网的机器上会永远为真 ——
    那时用例是绿的，却什么都没验证（我们只是证明了这台机器没网）。
    这条用例要求：同一段代码、同一台机器，只是不隔离，就必须能连出去。
    """
    res = _run_egress_probe(SandboxRunner("off"), tmp_path)

    assert res.network_isolation == "none"
    assert res.returncode == 0, res.tail()
    if "REACHABLE" not in res.stdout:
        pytest.skip(
            "本机到 8.8.8.8:53 本来就不可达，无法构成反向对照；"
            "上一条「外联被挡住」的用例因此在本次运行中不构成证据"
        )


@requires_netns
def test_sandbox_blocks_dns_resolution(tmp_path) -> None:
    """DNS 也要挡住：能解析域名就意味着能按名字外联，等于隔离只挡了一半。"""
    script = textwrap.dedent(
        """
        import socket
        try:
            print("RESOLVED", socket.gethostbyname("example.com"))
        except Exception as exc:
            print("BLOCKED", type(exc).__name__)
        """
    )
    res = SandboxRunner("auto").run(
        [sys.executable, "-c", script], cwd=tmp_path, limits=ResourceLimits(timeout_sec=30)
    )

    assert res.network_isolation == "netns"
    assert "BLOCKED" in res.stdout, f"DNS 未被挡住：{res.stdout!r}"


# ---------------------------------------------------------------------------
# 2) 渲染仍然能完成
# ---------------------------------------------------------------------------


@requires_netns
def test_render_still_works_under_isolation(tmp_path) -> None:
    """真实跑一次 ffmpeg 渲染（stock 引擎走的就是它），隔离下必须照常出片。"""
    from scidirector_ai.media import _binary

    out = tmp_path / "shot.mp4"
    res = SandboxRunner("auto").run(
        [
            _binary("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=navy:s=320x240:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ],
        cwd=tmp_path,
        limits=ResourceLimits(timeout_sec=90),
    )

    assert res.network_isolation == "netns"
    assert res.returncode == 0, res.tail()
    assert out.is_file() and out.stat().st_size > 0, "隔离下渲染没有产出文件"


@requires_netns
def test_loopback_state_inside_namespace_is_documented(tmp_path) -> None:
    """把「新命名空间里 loopback 是 DOWN 的」这个事实钉住。

    实测结论是**不影响任何现有引擎**：Playwright 用 `--remote-debugging-pipe`
    （管道而非 TCP）与 Chromium 通信，隔离前后截图逐字节相同。
    因此 `netns.py` 刻意**不**去折腾 loopback —— 多一步就多一处会坏的地方。

    这条用例的价值在未来：如果哪天某个引擎真的需要 loopback，
    它会在这里给出一个**指向原因**的失败，而不是让人对着
    「Connection refused / Network is unreachable」猜半天。
    """
    script = textwrap.dedent(
        """
        import socket
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0)); s.listen(1)
            c = socket.create_connection(s.getsockname(), timeout=3); c.close()
            print("LOOPBACK_UP")
        except Exception as exc:
            print("LOOPBACK_DOWN", type(exc).__name__)
        finally:
            s.close()
        """
    )
    res = SandboxRunner("auto").run(
        [sys.executable, "-c", script], cwd=tmp_path, limits=ResourceLimits(timeout_sec=30)
    )

    assert res.network_isolation == "netns"
    assert "LOOPBACK_DOWN" in res.stdout, (
        "loopback 在隔离命名空间里竟然是可用的 —— 说明 unshare 行为与预期不同，"
        "请重新确认 netns.py 里关于 loopback 的结论是否还成立"
    )


# ---------------------------------------------------------------------------
# 3) 诚实汇报与 fail closed
# ---------------------------------------------------------------------------


def test_mechanism_reports_none_when_disabled() -> None:
    iso = Isolator("off", "off")
    assert iso.network_mechanism() == "none"
    # 关闭时不加任何包装：多一层包装就多一处会坏的地方。
    wrapped, status = iso.wrap(["echo", "hi"], "/tmp/wd")
    assert wrapped == ["echo", "hi"]
    assert status == ""


@requires_netns
def test_mechanism_reports_netns_when_available() -> None:
    iso = Isolator("auto", "off")
    assert iso.network_mechanism() == "netns"
    wrapped, _ = iso.wrap(["echo", "hi"], "/tmp/wd")
    assert wrapped[-2:] == ["echo", "hi"]
    assert "-n" in wrapped and "-r" in wrapped
    # "--" 必须存在：否则参数会被 unshare 自己的选项解析吃掉。
    assert "--" in wrapped


def test_require_mode_fails_closed_when_unavailable(monkeypatch) -> None:
    """`require` 拿不到隔离时必须**拒绝执行**，绝不能静默降级成不隔离。

    这是本模块最重要的一条不变式：静默降级会让整个安全假设在无人察觉时失效，
    而它偏偏"一切正常"——渲染成功、任务完成，只是代码能随便外联。
    """
    monkeypatch.setattr(
        "scidirector_ai.sandbox.isolation.network_isolation_available", lambda *a, **k: False
    )
    iso = Isolator("require", "off")

    assert iso.network_mechanism() == "none"
    with pytest.raises(NetworkIsolationUnavailable):
        iso.wrap(["echo", "hi"], "/tmp/wd")


def test_require_mode_runner_returns_failure_instead_of_raising(monkeypatch, tmp_path) -> None:
    """runner 层把「拒绝执行」变成一条**失败的执行结果**，而不是抛异常。

    与本方法对「找不到可执行文件」的处理一致：抛异常会穿透到图外层，
    把单个镜头的失败升级成整个任务崩掉（本项目已经因为这类穿透踩过一次）。
    上层据此把原因写进事件流，用户看到的是一句能读懂的话。
    """
    monkeypatch.setattr(
        "scidirector_ai.sandbox.isolation.network_isolation_available", lambda *a, **k: False
    )
    runner = SandboxRunner("require")
    res = runner.run(["echo", "should-not-run"], cwd=tmp_path)

    assert res.returncode != 0
    assert not res.ok
    assert res.network_isolation == "none"
    assert "网络隔离" in res.stderr
    assert "should-not-run" not in res.stdout, "命令不该被执行"


def test_auto_mode_degrades_honestly_when_unavailable(monkeypatch, tmp_path) -> None:
    """`auto` 允许降级，但必须**如实上报** —— 而不是假装隔离了。"""
    monkeypatch.setattr(
        "scidirector_ai.sandbox.isolation.network_isolation_available", lambda *a, **k: False
    )
    res = SandboxRunner("auto").run(["echo", "hi"], cwd=tmp_path)

    assert res.returncode == 0
    assert res.network_isolation == "none", "降级时必须如实上报为未隔离"



# ---------------------------------------------------------------------------
# 5) 隔离不得改变 runner 的既有契约
# ---------------------------------------------------------------------------


def test_missing_executable_contract_holds_with_isolation(tmp_path) -> None:
    """开启隔离后，「可执行文件不存在」仍必须是 `-1` + 中文说明。

    这是我自己踩出来的回归：命令一旦被包进 `unshare`，找不到的就成了 unshare 的
    子命令 —— 报错变成 `unshare: failed to execute X: ...`、退出码 **127**，
    而既有约定是 **-1**。依赖退出码区分「部署缺工具链」与「渲染真的失败」的地方
    会因此静默失灵，所以 runner 里改为**先确认可执行文件再包裹**。

    两种模式都要断言：只测其中一种的话，恰恰是「另一种模式下契约变了」漏掉。
    """
    for mode in ("auto", "off"):
        res = SandboxRunner(mode).run(
            ["definitely-not-a-real-binary-xyz"], cwd=tmp_path
        )
        assert not res.ok, f"mode={mode} 竟然成功了"
        assert res.returncode == -1, (
            f"mode={mode} 的退出码应为 -1（部署问题），实际 {res.returncode}；"
            "127 说明命令被包进 unshare 后才失败"
        )
        assert "找不到可执行文件" in res.stderr, f"mode={mode}: {res.stderr!r}"
        assert "unshare" not in res.stderr, (
            f"mode={mode} 泄漏了 unshare 的实现细节，调用方不该看到它"
        )


# ---------------------------------------------------------------------------
# 6) 只读根文件系统（阶段五·沙盒加固的另一半）
# ---------------------------------------------------------------------------
#
# 需求里的「容器级 --read-only」在命名空间模型下的等价物：
# 把**每一个真实文件系统**重挂为只读，再单独把工作目录绑定回可写、
# 给 /tmp 挂一个有界的私有 tmpfs。
#
# 两条最容易搞错、也最值得钉住的事实：
#   1. `mount -o remount,ro,bind /` **只作用于根那一个挂载**。本机 /vol1、/vol2
#      是独立的 btrfs 挂载 —— 只重挂 / 之后，往 /vol1/... 写文件照样成功。
#      我第一版就是这么写的，测试当场把 blocked.txt 写进了宿主机的项目目录。
#   2. 只读根会让 /tmp 也不可写，而 ffmpeg / Playwright 都要写临时目录，
#      表现为「渲染莫名失败」。因此必须挂私有 tmpfs，且**必须带 size=**
#      （不设上限的 tmpfs 能吃掉整机内存，等于用一个新的 OOM 风险换掉只读加固）。

from scidirector_ai.sandbox.isolation import read_only_available as _ro_available


@pytest.fixture()
def sandbox_workdir(tmp_path_factory):
    """只读用例使用的工作目录。

    **刻意不用 pytest 的 tmp_path**：它在 /tmp 之下，而只读隔离会把 /tmp 换成
    私有 tmpfs —— 那会把工作目录整个遮蔽，写在里面的产物在命名空间外看不到。
    代码已经会把这种情况如实记成 gap，但用例本身要用**生产路径形状**
    （独立于 /tmp 的数据目录）才能真正验证正常工作时的行为。
    """
    base = Path(__file__).resolve().parents[2] / ".data" / "test-sandbox"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"case-{tmp_path_factory.getbasetemp().name}"
    if d.exists():
        import shutil as _sh

        _sh.rmtree(d, ignore_errors=True)
    (d / "work").mkdir(parents=True)
    yield d
    import shutil as _sh

    _sh.rmtree(d, ignore_errors=True)

requires_ro = pytest.mark.skipif(
    not _ro_available(),
    reason="本机不支持非特权挂载命名空间（unshare -r -m），跳过只读隔离验证",
)


def _write_probe(tmp_path):
    """返回一段脚本：分别尝试往工作目录、工作目录的**父目录**、/etc、/tmp 写文件。"""
    return textwrap.dedent(
        f"""
        import socket

        def try_write(p):
            try:
                with open(p, "w") as fh:
                    fh.write("x")
                return "OK"
            except OSError as exc:
                return "BLOCKED:" + (exc.strerror or "")

        work = {str(tmp_path / "work")!r}
        print("workdir", try_write(work + "/a.txt"))
        print("parent", try_write({str(tmp_path)!r} + "/blocked.txt"))
        print("etc", try_write("/etc/passwd"))
        print("tmp", try_write("/tmp/scid_ro_probe"))
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            print("egress REACHABLE")
        except Exception as exc:
            print("egress BLOCKED", type(exc).__name__)
        """
    )


def _probe(tmp_path, mode: str):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    runner = SandboxRunner("off" if mode == "off" else "auto", mode)
    res = runner.run(
        [sys.executable, "-c", _write_probe(tmp_path)],
        cwd=work,
        limits=ResourceLimits(timeout_sec=60),
    )
    got = {}
    for line in res.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            got[parts[0]] = parts[1]
    return res, got


@requires_ro
def test_read_only_blocks_writes_outside_workdir(sandbox_workdir) -> None:
    res, got = _probe(sandbox_workdir, "auto")

    assert res.read_only_enforced, f"只读未生效，gaps={res.read_only_gaps}"
    assert res.read_only_gaps == [], f"不该有保护不上的挂载点：{res.read_only_gaps}"
    assert res.returncode == 0, res.tail()

    # 工作目录必须可写 —— 否则渲染根本出不了产物。
    assert got["workdir"] == "OK", got
    # 工作目录的**父目录**也必须被挡住：这正是"只重挂 / 不够"的那个坑。
    assert got["parent"].startswith("BLOCKED"), got
    assert got["etc"].startswith("BLOCKED"), got
    # /tmp 必须可写（ffmpeg / Playwright 依赖它），且是私有的。
    assert got["tmp"] == "OK", got
    # 网络那半同时也在生效。
    assert got["egress"].startswith("BLOCKED"), got


@requires_ro
def test_writes_succeed_without_read_only(sandbox_workdir) -> None:
    """**反向对照。**

    没有它，「写父目录被挡住」在一个本来就没有写权限的机器上会永远为真 ——
    那时用例是绿的，却只是证明了这台机器不给写，而不是只读生效。
    """
    res, got = _probe(sandbox_workdir, "off")

    assert not res.read_only_enforced, "关闭时不该声称已只读"
    assert got["workdir"] == "OK", got
    if not got["parent"].startswith("OK"):
        pytest.skip(
            "本机对测试目录本来就没有写权限，无法构成反向对照；"
            "上一条用例因此在本次运行中不构成证据"
        )
    assert got["etc"].startswith("BLOCKED"), got  # /etc 本来就不该给普通用户写


@requires_ro
def test_read_only_report_is_removed_from_workdir(sandbox_workdir) -> None:
    """状态文件读完即删：它写在工作目录里，留着会污染渲染产物目录。"""
    _probe(sandbox_workdir, "auto")
    leftovers = list((sandbox_workdir / "work").glob(".scid_sandbox_status.json"))
    assert leftovers == [], f"状态文件未清理：{leftovers}"


@requires_ro
def test_read_only_render_still_works(sandbox_workdir) -> None:
    """只读下真实跑一次 ffmpeg 渲染，必须照常出片。

    这条是「不把功能弄坏」的那一半证据：只阻断不该做的写，
    不该影响该做的写（工作目录）。
    """
    from scidirector_ai.media import _binary

    work = sandbox_workdir / "work"
    work.mkdir(exist_ok=True)
    out = work / "shot.mp4"
    res = SandboxRunner("off", "auto").run(
        [
            _binary("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=teal:s=320x240:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ],
        cwd=work,
        limits=ResourceLimits(timeout_sec=120),
    )

    assert res.read_only_enforced, f"只读未生效：{res.read_only_gaps}"
    assert res.returncode == 0, res.tail()
    assert out.is_file() and out.stat().st_size > 0, "只读下渲染没有产出文件"


def test_read_only_require_fails_closed_when_unavailable(monkeypatch, tmp_path) -> None:
    """`require` 拿不到只读时必须拒绝执行，绝不能静默降级。"""
    monkeypatch.setattr(
        "scidirector_ai.sandbox.isolation.read_only_available", lambda: False
    )
    res = SandboxRunner("off", "require").run(["echo", "should-not-run"], cwd=tmp_path)

    assert not res.ok
    assert "只读" in res.stderr
    assert "should-not-run" not in res.stdout, "命令不该被执行"


def test_read_only_off_means_no_wrapper(tmp_path) -> None:
    """关闭时**不加任何包装**：多一层包装就多一处会坏的地方，也有成本。"""
    iso = SandboxRunner("off", "off").isolator
    wrapped, status = iso.wrap(["echo", "hi"], str(tmp_path))
    assert wrapped == ["echo", "hi"]
    assert status == ""


# ---------------------------------------------------------------------------
# 7) seccomp 系统调用过滤（阶段五·沙盒加固的第三块）
# ---------------------------------------------------------------------------
#
# 需求里写的是「seccomp 白名单」，这里实现的是**拒绝名单**，理由见
# exec_guard.py 的模块文档：允许名单要求把目标程序的每个系统调用都列全，
# 而本机只装得起 ffmpeg（manim/LaTeX/Chromium 都不可用），那份名单**无法被验证**——
# 没验证过的允许名单比不加更危险。
#
# 判定依据是真实调用一个被拦的系统调用（ptrace），而不是去读配置：
# "配置说开着了"和"真的拦住了"是两件事。

_ptrace_probe = textwrap.dedent(
    """
    import ctypes
    import sys

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    ctypes.set_errno(0)
    # PTRACE_TRACEME：正常环境返回 0；被 seccomp 拦下时返回 -1 / EPERM。
    rc = libc.ptrace(0, 0, None, None)
    errno = ctypes.get_errno()
    print("ptrace", rc, errno)
    # 反向确认：无关的调用不受影响（过滤器不能把什么都拦掉）。
    import socket

    s = socket.socket()
    s.close()
    print("socket OK")
    """
)


def _run_ptrace_probe(seccomp_mode: str, tmp_path):
    runner = SandboxRunner("off", "off", seccomp_mode)
    return runner.run(
        [sys.executable, "-c", _ptrace_probe],
        cwd=tmp_path,
        limits=ResourceLimits(timeout_sec=30),
    )


def test_seccomp_blocks_denied_syscall(tmp_path) -> None:
    res = _run_ptrace_probe("deny", tmp_path)

    assert res.seccomp_enforced, "seccomp 未生效"
    assert res.seccomp_denied > 20, f"拦下的条数明显偏少：{res.seccomp_denied}"
    assert res.returncode == 0, res.tail()
    assert "ptrace 0 0" not in res.stdout, f"ptrace 没有被拦住：{res.stdout!r}"
    assert "ptrace -1 1" in res.stdout, f"应当是 -1/EPERM，实际 {res.stdout!r}"
    # 过滤器不能把无关调用也拦掉 —— 否则它就是把功能弄坏了。
    assert "socket OK" in res.stdout, res.stdout


def test_syscall_succeeds_without_seccomp(tmp_path) -> None:
    """**反向对照。**

    没有它，「ptrace 被拦住」在一个本来就禁止 ptrace 的环境（某些容器默认策略）
    里会永远为真 —— 那时用例是绿的，却只是证明了环境如此，而不是我们的过滤器生效。
    """
    res = _run_ptrace_probe("off", tmp_path)

    assert not res.seccomp_enforced, "关闭时不该声称已启用"
    if "ptrace 0 0" not in res.stdout:
        pytest.skip(
            "本机环境本身就禁止 ptrace（容器默认策略），无法构成反向对照；"
            "上一条用例因此在本次运行中不构成证据"
        )
    assert "socket OK" in res.stdout


def test_seccomp_does_not_break_rendering(tmp_path) -> None:
    """seccomp 下真实跑一次 ffmpeg：过滤不能把正常渲染弄坏。

    这是"加固没有变成功能回归"的那一半证据。也是为什么默认关：
    另外三个引擎在本机装不起来，因此**无法**验证它们不会用到被拦的调用。
    """
    from scidirector_ai.media import _binary

    out = tmp_path / "shot.mp4"
    res = SandboxRunner("off", "off", "deny").run(
        [
            _binary("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=purple:s=320x240:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ],
        cwd=tmp_path,
        limits=ResourceLimits(timeout_sec=120),
    )

    assert res.seccomp_enforced, "seccomp 未生效"
    assert res.returncode == 0, res.tail()
    assert out.is_file() and out.stat().st_size > 0, "seccomp 下渲染没有产出文件"


def test_seccomp_and_network_and_readonly_compose(sandbox_workdir) -> None:
    """三层同时开启：都必须如实上报已生效，且命令照常跑完。

    用 `sandbox_workdir` 而不是 `tmp_path`：后者在 /tmp 之下，
    而只读隔离会把 /tmp 换成私有 tmpfs（那会遮蔽工作目录）——
    代码会正确地记为一条 gap 并**拒绝声称已只读**，于是这里测的就不是
    "三层都生效"，而是那个边界情况。
    """
    work = sandbox_workdir / "work"
    work.mkdir(exist_ok=True)
    res = SandboxRunner("auto", "auto", "deny").run(
        [sys.executable, "-c", "print('all layers')"],
        cwd=work,
        limits=ResourceLimits(timeout_sec=60),
    )

    assert res.network_isolation == "netns"
    assert res.read_only_enforced, f"只读未生效：{res.read_only_gaps}"
    assert res.seccomp_enforced
    assert res.returncode == 0, res.tail()
    assert "all layers" in res.stdout


def test_seccomp_require_fails_closed_when_unavailable(monkeypatch, tmp_path) -> None:
    """`require` 拿不到 seccomp 时必须拒绝执行，绝不静默降级。"""
    monkeypatch.setattr(
        "scidirector_ai.sandbox.isolation.Isolator.seccomp_mechanism", lambda self: "none"
    )
    res = SandboxRunner("off", "off", "require").run(["echo", "should-not-run"], cwd=tmp_path)

    assert not res.ok
    assert "seccomp" in res.stderr
    assert "should-not-run" not in res.stdout, "命令不该被执行"


def test_seccomp_report_marks_unresolved_syscalls() -> None:
    """名单里解析不到的条目必须被记下来，而不是只报"拦了 N 条"。

    只报条数会让人以为名单里每一条都生效了 —— 而某个系统调用在当前内核上
    不存在（例如旧内核没有 io_uring）时，那条规则其实**根本没装上**。
    """
    from scidirector_ai.sandbox.exec_guard import DENY_SYSCALLS, setup_seccomp

    report = setup_seccomp()
    total_listed = sum(len(v) for v in DENY_SYSCALLS.values())
    assert report["seccomp_enforced"] is True
    # 装上 + 未解析 = 名单总数：不允许有"既没装上也没记录"的条目。
    assert report["seccomp_denied"] + len(report["seccomp_unresolved"]) == total_listed, report
