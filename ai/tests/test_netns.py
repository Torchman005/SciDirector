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

import pytest

from scidirector_ai.sandbox.netns import (
    NetworkIsolationUnavailable,
    NetworkIsolator,
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
    assert NetworkIsolator("off").mechanism() == "none"
    assert NetworkIsolator("off").wrap(["echo", "hi"]) == ["echo", "hi"]


@requires_netns
def test_mechanism_reports_netns_when_available() -> None:
    iso = NetworkIsolator("auto")
    assert iso.mechanism() == "netns"
    wrapped = iso.wrap(["echo", "hi"])
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
        "scidirector_ai.sandbox.netns.network_isolation_available", lambda *a, **k: False
    )
    iso = NetworkIsolator("require")

    assert iso.mechanism() == "none"
    with pytest.raises(NetworkIsolationUnavailable):
        iso.wrap(["echo", "hi"])


def test_require_mode_runner_returns_failure_instead_of_raising(monkeypatch, tmp_path) -> None:
    """runner 层把「拒绝执行」变成一条**失败的执行结果**，而不是抛异常。

    与本方法对「找不到可执行文件」的处理一致：抛异常会穿透到图外层，
    把单个镜头的失败升级成整个任务崩掉（本项目已经因为这类穿透踩过一次）。
    上层据此把原因写进事件流，用户看到的是一句能读懂的话。
    """
    monkeypatch.setattr(
        "scidirector_ai.sandbox.netns.network_isolation_available", lambda *a, **k: False
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
        "scidirector_ai.sandbox.netns.network_isolation_available", lambda *a, **k: False
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
