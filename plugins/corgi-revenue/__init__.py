from __future__ import annotations

import os
import subprocess
import sys


SCRIPT = "/opt/hermes-cloud/scripts/revenue_summary.py"


def _handle_revenue(raw_args: str) -> str:
    if raw_args.strip():
        return "Usage: /revenue"

    env = os.environ.copy()
    # This command is explicitly today's live view; historical dates stay in the ledger tools.
    env.pop("REPORT_DATE", None)
    try:
        result = subprocess.run(
            [sys.executable, SCRIPT, "--text-only"],
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Revenue is temporarily unavailable; the Square check timed out."

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return f"Revenue is temporarily unavailable. {detail[:300]}" if detail else "Revenue is temporarily unavailable."
    return result.stdout.strip() or "Revenue is temporarily unavailable."


def register(ctx):
    ctx.register_command(
        "revenue",
        handler=_handle_revenue,
        description="Show today's live Corgi Cafe revenue",
    )
