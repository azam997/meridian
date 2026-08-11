"""The headless capture-analysis digest (scripts/analyze_capture.py)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_capture.export import responses_to_wire, serialize_ndjson  # noqa: E402

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "analyze_capture.py"
_RECORDING = Path(__file__).resolve().parent / "fixtures" / "local_capture" / "samurai_full_stream.recording.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("analyze_capture", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unsupported_job_yields_error_digest(tmp_path: Path) -> None:
    capture = tmp_path / "bluemage.ndjson"
    capture.write_text("\n".join([
        json.dumps({"kind": "meta", "contractVersion": 1, "captureId": "test"}),
        json.dumps({"kind": "summary", "fights": [{"id": 1, "kill": True, "startTime": 0, "endTime": 1000,
                                                   "friendlyPlayers": [1]}],
                    "masterData": {"actors": [{"id": 1, "name": "Blue Tester", "type": "Player",
                                               "subType": "BlueMage"}], "abilities": []}}),
        json.dumps({"kind": "end", "endTime": 1000, "outcome": "kill"}),
    ]) + "\n", encoding="utf-8")

    module = _load_module()
    digest, exit_code = module.build_digest(capture, None, 8, None)
    assert exit_code == 2
    assert "BlueMage" in digest["error"]
    assert digest["player"] == "Blue Tester"


@pytest.mark.slow
def test_full_digest_from_recording_fixture(tmp_path: Path) -> None:
    fixture = json.loads(_RECORDING.read_text(encoding="utf-8"))
    capture = tmp_path / "samurai.ndjson"
    capture.write_text(serialize_ndjson(responses_to_wire(fixture["recording"])), encoding="utf-8")

    module = _load_module()
    digest, exit_code = module.build_digest(capture, fixture["player_name"], 8, None)
    assert exit_code == 0
    assert digest["job"] == "Samurai"
    assert digest["outcome"] == "kill"
    assert digest["efficiencyPct"] and digest["efficiencyPct"] > 0
    assert digest["idealizedPotency"] > digest["deliveredPotency"] > 0
    assert digest["recoverablePotency"] >= 0
    assert isinstance(digest["improvements"], list)
    for card in digest["improvements"]:
        assert card["severity"] in ("bad", "warn", "info")
    assert digest["durationSec"] > 0


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_unsupported_job_yields_error_digest(Path(tmp))
        test_full_digest_from_recording_fixture(Path(tmp))
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
