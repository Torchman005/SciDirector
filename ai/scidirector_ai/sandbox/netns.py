"""网络隔离：让沙盒里的渲染代码**无法发起任何外联**。

## 为什么必须有这一层

沙盒要执行的是 **LLM 生成的代码**。静态白名单（`policy.check_source`）可以被绕过
（拼接字符串构造危险名字），因此不能只靠它 —— 必须假设「代码已经绕过了检查」，
再问一句：它此刻能做什么？

能做的事里，**外联**是最危险的一类：把工作目录里的脚本/密钥发出去、
打内网服务、把机器当作跳板。而这些都不需要写文件、不需要提权，
只需要一个 `socket.connect`。

## 机制：用户命名空间 + 网络命名空间

    unshare -r -n -- <原命令>

`-n` 建一个新的网络命名空间：里面只有一张**未启用**的 loopback，没有路由、没有 DNS、
连不出去也解析不了。`-r`（`--map-root-user`）同时建用户命名空间并把当前 uid 映射成
里面的 root —— 非 root 用户本来无权建网络命名空间，这是让普通用户也能用上的关键。

### 一个实测出来的重要事实

新的网络命名空间里 **loopback 是 DOWN 的**，`connect("127.0.0.1", ...)` 直接报
`Network is unreachable`。我原本担心这会打断 HTML 引擎（Playwright/Chromium 看起来
需要本地端口），实测结论是**不影响**：Playwright 用 `--remote-debugging-pipe`
（管道，不是 TCP）与浏览器通信，在隔离命名空间里截图结果与隔离前**逐字节相同**。
本模块因此**不**去折腾 loopback：多一步就多一处会坏的地方，而当前没有任何引擎需要它。
（`test_netns.py` 把这个事实钉住了：如果将来某个引擎真的需要 loopback，
那条用例会告诉后来者，而不是让人对着「连接被拒」猜半天。）

## 本模块的职责边界

这里**只**回答「网络命名空间能不能用、怎么包成 netns 形式」。
把网络与只读文件系统拼成一条命令、以及两者的模式开关，都在 `isolation.py` ——
拼装规则散在两处必然会出现「加了 -m 但忘了挂 guard」这类静默失效。

## 诚实的汇报，而不是想当然的「已隔离」

与 `ExecResult.memory_limit_enforced_by` 同一条原则：**谎报「已加固」是安全代码里
最危险的错误**。因此：

- 本模块**探测**机制是否真的可用（跑一次 `unshare -r -n -- true`，并缓存结果），
  而不是看到 Linux 就假定可用（容器里常见 `Operation not permitted`，
  本机 `unshare -n` 就是不可用的，只有 `unshare -rn` 可以）；
- `ExecResult.network_isolation` 如实上报实际生效的机制（`"netns"` / `"none"`）；
- 配置成 `require` 时，探测失败**直接拒绝执行**（fail closed）——
  「我要了隔离但没拿到」必须是一个显式的失败，绝不允许静默降级成不隔离。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from typing import Literal

logger = logging.getLogger(__name__)

#: 隔离模式。
#: - ``auto``    ：能用就用，不能用则如实上报 `"none"` 并继续（本地开发友好）；
#: - ``require`` ：必须可用，否则**拒绝执行**（生产环境应当用这个）；
#: - ``off``     ：明确不用（例如已经在容器里由容器提供隔离）。
IsolationMode = Literal["auto", "require", "off"]

#: 探测缓存：`unshare` 的可用性在进程生命周期内不会变化，
#: 而探测本身要 fork 一个进程 —— 每个镜头都探一次纯属浪费。
_PROBE_LOCK = threading.Lock()
_PROBE_RESULT: bool | None = None


def reset_probe_cache() -> None:
    """清空探测缓存。仅供测试使用。"""
    global _PROBE_RESULT
    with _PROBE_LOCK:
        _PROBE_RESULT = None


def _probe_unshare(executable: str) -> bool:
    """真正执行一次最小隔离命令来判断可用性。

    只检查 `unshare` 存在是不够的：本机 `unshare -n` 返回
    `Operation not permitted`（非 root 无权建网络命名空间），
    而 `unshare -rn` 可以。**必须实跑**，否则会在最需要它的机器上才发现不可用。
    """
    try:
        proc = subprocess.run(
            [executable, "-r", "-n", "--", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("网络隔离探测失败：%s", exc)
        return False
    if proc.returncode != 0:
        logger.info(
            "网络隔离不可用（unshare 退出码 %s）：%s",
            proc.returncode,
            (proc.stderr or b"").decode("utf-8", "replace").strip()[:200],
        )
        return False
    return True


def network_isolation_available(executable: str = "unshare") -> bool:
    """本机能否建立网络命名空间（结果被缓存）。"""
    global _PROBE_RESULT
    with _PROBE_LOCK:
        if _PROBE_RESULT is None:
            path = shutil.which(executable)
            _PROBE_RESULT = bool(path) and _probe_unshare(path)
            if _PROBE_RESULT:
                logger.info("沙盒网络隔离已启用（unshare -r -n）")
            else:
                logger.warning(
                    "沙盒网络隔离**不可用**：渲染代码将能自由外联。"
                    "生产环境请设置 SCID_SANDBOX_NETWORK_ISOLATION=require 以拒绝在无隔离时渲染"
                )
        return _PROBE_RESULT


class NetworkIsolationUnavailable(RuntimeError):
    """要求隔离但本机不可用。"""
