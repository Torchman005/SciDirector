"""protoc 生成的 gRPC 代码。

生成命令（见仓库根目录 ``Makefile`` 的 ``proto-python`` 目标）::

    python -m grpc_tools.protoc -I proto \
        --python_out=ai/scidirector_ai/pb \
        --pyi_out=ai/scidirector_ai/pb \
        --grpc_python_out=ai/scidirector_ai/pb \
        proto/scidirector/v1/*.proto

为什么要在这里动 ``sys.path``：
``protoc`` 生成的模块内部使用 **绝对导入** ``from scidirector.v1 import common_pb2``，
它假设包根 ``scidirector`` 在导入路径上。而我们把生成物放在
``scidirector_ai/pb/`` 下（避免与顶层包名冲突），因此需要把该目录本身加入
``sys.path``，让 ``scidirector.v1`` 能作为命名空间包被解析。

这一处 shim 是必要的，且只影响本进程 —— 比到处用 ``--python_out`` 生成到仓库根
（污染顶层命名空间）要干净得多。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PB_ROOT = Path(__file__).resolve().parent
if str(_PB_ROOT) not in sys.path:
    # 插到最前面：确保解析到的是本仓库的生成物而不是环境中同名的第三方包。
    sys.path.insert(0, str(_PB_ROOT))

__all__ = ["_PB_ROOT"]
