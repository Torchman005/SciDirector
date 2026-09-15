"""pytest 全局配置。

作用：把 ``ai/`` 目录加入 ``sys.path``，使测试可以直接 ``import scidirector_ai``，
而不要求先执行 ``pip install -e .``。

显式插入而不是依赖 pytest 的隐式行为，是为了让「在仓库任意目录下运行 pytest」
都能得到一致结果 —— 隐式的 sys.path 行为在不同 pytest 版本间有差异。
"""

from __future__ import annotations

import sys
from pathlib import Path

_AI_ROOT = Path(__file__).resolve().parent
if str(_AI_ROOT) not in sys.path:
    sys.path.insert(0, str(_AI_ROOT))

# 让测试默认使用 mock LLM，避免 CI 因缺少密钥而失败或产生真实费用。
import os  # noqa: E402

os.environ.setdefault("SCID_LLM_PROVIDER", "mock")
os.environ.setdefault("SCID_ENV", "test")
