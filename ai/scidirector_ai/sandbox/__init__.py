"""沙盒：安全执行 LLM 生成的渲染代码。

分层防御（见 docs/DESIGN.md §5.4）：
    1. 静态白名单（本包 policy.py）—— AST 扫描，禁止危险导入与调用；
    2. 进程隔离（runner.py，阶段二）—— 独立子进程 + 资源限制；
    3. 超时熔断 —— 到点 SIGKILL，判定该次渲染失败；
    4. 容器加固（生产）—— --network=none --read-only 非 root。

**必须清醒认识到：这是纵深防御，不是绝对安全边界。**
静态检查可以被绕过（例如通过 getattr 拼接出危险名字），
因此生产环境必须叠加容器级隔离。
"""

from .policy import PolicyReport, PolicyViolation, check_source, is_safe

__all__ = ["PolicyReport", "PolicyViolation", "check_source", "is_safe"]
