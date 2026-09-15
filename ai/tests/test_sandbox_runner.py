"""``SandboxRunner`` 的单元测试。

这是本项目**安全关键路径**的回归测试，重点验证三件事：

1. **超时确实会杀进程**，而且是杀掉**整棵进程树**（不是只杀父进程）；
2. **内存限制确实生效**（监控线程兜底路径可被确定性触发）；
3. **不会误杀**正常进程，也**不会泄露密钥**给不可信的子进程。

这些断言都直接对应"防止死循环导致系统崩溃"这一需求 ——
需求里说的"崩溃"其实有两类（CPU 活锁与内存爆炸），因此两类都要测。
"""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from scidirector_ai.sandbox.runner import (
    ExecResult,
    NullMemoryProbe,
    ResourceLimits,
    SandboxRunner,
)


@pytest.fixture()
def runner() -> SandboxRunner:
    return SandboxRunner()


def _write(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


# ===========================================================================
# 基本执行
# ===========================================================================


class TestBasicExecution:
    def test_successful_run(self, runner: SandboxRunner, tmp_path: Path) -> None:
        script = _write(tmp_path, "ok.py", "print('hello sandbox')")
        result = runner.run_python(script, cwd=tmp_path, limits=ResourceLimits(timeout_sec=30))
        assert result.ok
        assert result.returncode == 0
        assert "hello sandbox" in result.stdout
        assert result.killed_reason == ""
        assert result.summary() == "执行成功"

    def test_nonzero_exit_is_captured_not_raised(self, runner: SandboxRunner, tmp_path: Path) -> None:
        """退出码非零不是异常，而是一个**可回灌给模型**的结果。"""
        script = _write(
            tmp_path,
            "fail.py",
            """
            import sys
            sys.stderr.write('Traceback: 业务代码报错\\n')
            sys.exit(3)
            """,
        )
        result = runner.run_python(script, cwd=tmp_path, limits=ResourceLimits(timeout_sec=30))
        assert not result.ok
        assert result.returncode == 3
        assert "业务代码报错" in result.tail()

    def test_missing_executable_returns_result(self, runner: SandboxRunner, tmp_path: Path) -> None:
        """可执行文件不存在属于部署问题，应当返回结果而不是抛异常。

        返回结果才能让上层统一按"渲染失败"处理并记入 attempt；
        抛异常会让这条路径与其它失败路径分叉，容易漏处理。
        """
        result = runner.run(["definitely-not-a-real-binary-xyz"], cwd=tmp_path)
        assert not result.ok
        assert result.returncode == -1
        assert "找不到可执行文件" in result.stderr


# ===========================================================================
# 超时：杀死整棵进程树
# ===========================================================================


class TestTimeoutKill:
    def test_dead_loop_is_killed(self, runner: SandboxRunner, tmp_path: Path) -> None:
        """死循环必须在超时点被强制终止。

        这是需求里"防止死循环导致系统崩溃"最直接的验证。
        """
        script = _write(tmp_path, "loop.py", "while True:\n    pass\n")
        started = time.monotonic()
        result = runner.run_python(
            script, cwd=tmp_path, limits=ResourceLimits(timeout_sec=2, max_memory_mb=512)
        )
        elapsed = time.monotonic() - started

        assert result.timed_out, result.summary()
        assert result.killed_reason == "timeout"
        assert not result.ok
        assert result.timeout_sec == 2
        # 必须在超时后很快返回，而不是任由它跑下去。
        # 上限给 10s 是为了容忍 Windows 上进程回收较慢的情况。
        assert elapsed < 10, f"超时后 {elapsed:.1f}s 才返回，说明没有及时终止"
        assert "超时" in result.summary()

    def test_kills_whole_process_tree(self, runner: SandboxRunner, tmp_path: Path) -> None:
        """**关键**：必须杀掉孙进程。

        只杀父进程会留下孤儿继续吃 CPU —— 表现为"沙盒报超时了，
        但机器风扇还在狂转"，这是最难排查的一类资源泄漏。
        Manim 就是这种形态（父进程派生 LaTeX/dvisvgm）。
        """
        heartbeat = tmp_path / "heartbeat.txt"
        # 子进程（模拟 LaTeX）持续写心跳；孙进程存活则文件会继续增长。
        child = _write(
            tmp_path,
            "child.py",
            f"""
            import time, pathlib
            p = pathlib.Path(r"{heartbeat}")
            while True:
                with p.open("a", encoding="utf-8") as fh:
                    fh.write("x")
                time.sleep(0.15)
            """,
        )
        parent = _write(
            tmp_path,
            "parent.py",
            f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, r"{child}"])
            while True:
                time.sleep(0.1)
            """,
        )

        result = runner.run_python(
            parent, cwd=tmp_path, limits=ResourceLimits(timeout_sec=2, max_memory_mb=512)
        )
        assert result.killed_reason == "timeout"

        # 等到心跳文件确实产生过内容，再确认它**停止增长**。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not heartbeat.exists():
            time.sleep(0.1)
        assert heartbeat.exists(), "孙进程从未写入心跳，测试本身无效"

        size_after_kill = heartbeat.stat().st_size
        time.sleep(1.5)
        size_later = heartbeat.stat().st_size
        assert size_later == size_after_kill, (
            f"孙进程仍在运行（心跳从 {size_after_kill} 增长到 {size_later}）—— "
            "进程树没有被真正杀掉"
        )


# ===========================================================================
# 内存限制
# ===========================================================================


class _ExplodingProbe:
    """总是报告超大内存占用的探测桩。

    用它来**确定性地**触发监控线程的"内存超限即杀"分支 ——
    真实探测在超限前进程可能已经因分配失败退出，无法稳定复现该分支。
    """

    def peak_mb(self) -> float | None:
        return 999_999.0

    @property
    def source(self) -> str:
        return "test-stub"


class TestMemoryLimit:
    def test_monitor_kills_on_memory_exceeded(self, runner: SandboxRunner, tmp_path: Path) -> None:
        """监控线程必须在内存超限时杀掉进程，并如实标注原因。

        这条路径是内核限制之外的第二道防线，也是 Windows 上
        Job Object 赋值竞态窗口的唯一兜底，因此必须被覆盖。
        """
        script = _write(
            tmp_path,
            "sleepy.py",
            """
            import time
            time.sleep(30)
            """,
        )
        result = runner.run_python(
            script,
            cwd=tmp_path,
            limits=ResourceLimits(timeout_sec=25, max_memory_mb=512, poll_interval_sec=0.1),
            memory_probe=_ExplodingProbe(),
        )
        assert result.killed_reason == "memory", result.summary()
        assert not result.timed_out, "内存超限不应被误标成超时"
        assert "内存超限" in result.summary()

    def test_tight_limit_prevents_memory_hog(
        self, runner: SandboxRunner, tmp_path: Path
    ) -> None:
        """真实施加内存上限时，内存吞噬脚本必须失败。

        这里不假设具体失败形态：内核机制下子进程会抛 MemoryError 后退出，
        监控机制下会被直接杀掉。两种都算"限制生效"，因此断言的是
        **结果**（没有成功吃下全部内存），而不是某一种机制。
        """
        script = _write(
            tmp_path,
            "hog.py",
            """
            chunks = []
            try:
                while True:
                    chunks.append(bytearray(8 * 1024 * 1024))  # 每次 8MB
            except MemoryError:
                import sys
                sys.stderr.write('MemoryError: 被内存上限拦住了\\n')
                sys.exit(9)
            """,
        )
        result = runner.run_python(
            script,
            cwd=tmp_path,
            limits=ResourceLimits(timeout_sec=15, max_memory_mb=256, poll_interval_sec=0.1),
        )
        assert not result.ok, "内存吞噬脚本居然成功了，说明限制没有生效"
        # 要么被监控杀掉，要么被内核限制逼出 MemoryError —— 都是预期结果。
        assert result.killed_reason == "memory" or "MemoryError" in result.stderr, (
            f"失败形态不符合预期：killed={result.killed_reason!r} "
            f"stderr={result.stderr[:200]!r}"
        )

    def test_benign_process_is_not_killed(self, runner: SandboxRunner, tmp_path: Path) -> None:
        """限制足够宽松时**绝不能**误杀 —— 否则正常镜头会被白白判为失败。"""
        script = _write(
            tmp_path,
            "small.py",
            """
            data = bytearray(4 * 1024 * 1024)  # 4MB
            print('done', len(data))
            """,
        )
        result = runner.run_python(
            script, cwd=tmp_path, limits=ResourceLimits(timeout_sec=30, max_memory_mb=1024)
        )
        assert result.ok
        assert "done" in result.stdout

    def test_reports_which_mechanism_enforced_the_limit(
        self, runner: SandboxRunner, tmp_path: Path
    ) -> None:
        """必须如实上报限制由谁执行。

        在缺少机制的环境里谎报"已限制"是安全代码里最危险的错误，
        因此这条断言守护的是**诚实性**，而不是某个具体机制。
        """
        script = _write(tmp_path, "noop.py", "pass")
        result = runner.run_python(
            script, cwd=tmp_path, limits=ResourceLimits(timeout_sec=30, max_memory_mb=512)
        )
        assert result.memory_limit_enforced_by in {"rlimit", "job-object", "monitor", "none"}
        if os.name == "nt":
            # Windows 上应当拿到 Job Object；拿不到说明 ctypes 路径退化了。
            assert result.memory_limit_enforced_by == "job-object", (
                "Windows 下未通过 Job Object 施加内存限制，已降级 —— 需要排查"
            )


class TestResourceLimits:
    def test_rejects_invalid_values(self) -> None:
        with pytest.raises(ValueError):
            ResourceLimits(timeout_sec=0)
        with pytest.raises(ValueError):
            ResourceLimits(max_memory_mb=0)

    def test_null_probe_returns_none_not_zero(self) -> None:
        """测不到内存必须返回 None。

        返回 0 会被误读成"内存占用极低"，而 None 明确表示"测不到" ——
        这两者在排查问题时含义完全相反。
        """
        assert NullMemoryProbe().peak_mb() is None


# ===========================================================================
# 环境隔离
# ===========================================================================


class TestEnvironmentIsolation:
    def test_secrets_are_not_inherited(
        self, runner: SandboxRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """密钥绝不能透传给渲染代码。

        渲染代码是我们无法完全信任的，给它密钥等于把钥匙交给陌生人。
        这里用白名单机制：未列出的变量一律不继承。
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak")
        monkeypatch.setenv("SCID_FAKE_SECRET", "top-secret")

        script = _write(
            tmp_path,
            "env.py",
            """
            import os
            print('OPENAI=' + repr(os.environ.get('OPENAI_API_KEY')))
            print('SECRET=' + repr(os.environ.get('SCID_FAKE_SECRET')))
            print('UNBUFFERED=' + repr(os.environ.get('PYTHONUNBUFFERED')))
            """,
        )
        result = runner.run_python(
            script, cwd=tmp_path, limits=ResourceLimits(timeout_sec=30, max_memory_mb=512)
        )
        assert result.ok
        assert "OPENAI=None" in result.stdout
        assert "SECRET=None" in result.stdout
        # 未缓冲是我们主动设置的，应当存在（否则超时被杀时日志会全丢）。
        assert "UNBUFFERED='1'" in result.stdout

    def test_explicit_extra_env_is_merged(
        self, runner: SandboxRunner, tmp_path: Path
    ) -> None:
        """显式追加的环境变量要被合并（供测试注入假 manim 等受信任场景）。"""
        script = _write(
            tmp_path,
            "extra.py",
            "import os; print('V=' + str(os.environ.get('MY_EXPLICIT_VAR')))",
        )
        result = runner.run_python(
            script,
            cwd=tmp_path,
            limits=ResourceLimits(timeout_sec=30, max_memory_mb=512),
            env_extra={"MY_EXPLICIT_VAR": "injected"},
        )
        assert result.ok
        assert "V=injected" in result.stdout


# ===========================================================================
# 结果对象
# ===========================================================================


class TestExecResult:
    def test_ok_requires_zero_exit_and_no_kill(self) -> None:
        assert ExecResult(command=["x"], returncode=0).ok
        assert not ExecResult(command=["x"], returncode=1).ok
        assert not ExecResult(command=["x"], returncode=0, killed_reason="timeout").ok

    def test_tail_falls_back_to_stdout(self) -> None:
        result = ExecResult(command=["x"], returncode=1, stdout="只有 stdout 的内容")
        assert "只有 stdout" in result.tail()

    def test_tail_truncates_from_the_front(self) -> None:
        """截断要保留**尾部** —— 报错的最后几行才是有用的。"""
        long_text = "A" * 5000 + "关键错误在最后"
        result = ExecResult(command=["x"], returncode=1, stderr=long_text)
        tail = result.tail(100)
        assert "关键错误在最后" in tail
        assert len(tail) <= 101

    def test_output_is_capped(self, runner: SandboxRunner, tmp_path: Path) -> None:
        """超长输出必须被截断，否则单个任务就能把内存吃光。"""
        script = _write(
            tmp_path,
            "loud.py",
            """
            for i in range(20000):
                print('这是一行非常啰嗦的渲染日志 ' * 8)
            """,
        )
        result = runner.run_python(
            script, cwd=tmp_path, limits=ResourceLimits(timeout_sec=60, max_memory_mb=1024)
        )
        assert result.ok
        assert "输出已截断" in result.stdout
        assert len(result.stdout) < 300_000
