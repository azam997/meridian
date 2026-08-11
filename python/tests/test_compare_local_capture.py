"""The parity comparator's self-test, wired into the suite.

compare_local_capture.py is the Phase 1 capture-parity gate for the Meridian
Companion collector. Its --self-test validates the comparator itself against
the committed recording fixture (export oracle, identical-sides parity,
known-gap classification, dropped-event detection) — fully offline.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "compare_local_capture.py"


def test_comparator_self_test() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--self-test"],
        capture_output=True,
        text=True,
        cwd=str(_SCRIPT.parent.parent),
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SELF-TEST OK" in result.stdout


def main() -> int:
    test_comparator_self_test()
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
