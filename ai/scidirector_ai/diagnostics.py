"""Keep actionable exception lines ahead of bounded Rich/ANSI tracebacks."""
from __future__ import annotations

import re


def error_evidence(message: str, detail: str = "", limit: int = 2000) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", detail)
    lines = [line.strip(" │╰╭─╯╮\r") for line in text.splitlines()]
    causes = [i for i, line in enumerate(lines)
              if re.match(r"(?:[\w.]+(?:Error|Exception)|Error):", line)]
    cause = "\n".join(lines[causes[-1]:causes[-1]+5]) if causes else ""
    head = message.strip()
    if cause and cause not in head:
        head += "\n" + cause
    if text and text.strip() != head:
        remaining = max(0, limit - len(head) - 12)
        # Retain both contextual start (e.g. LaTeX diagnostics) and final cause.
        context = text if len(text) <= remaining else text[:remaining//2] + "\n…\n" + text[-remaining//2:]
        head += "\n" + context if remaining else ""
    return head[:limit]
