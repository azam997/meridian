"""Test the legacy config-dir migration in config.py.

When the analyzer was renamed from fflogs-mch-compare to
fflogs-efficiency-analyzer, the dotfile config dir moved from
~/.fflogs_mch_compare/ to ~/.fflogs_efficiency_analyzer/. A first-run
migration in config.py renames the old dir to the new path so users
don't lose their credentials.

The migration is:
  - skipped when the new dir already exists (idempotent)
  - skipped when neither dir exists (clean install)
  - executed once when the legacy dir exists and the new one doesn't

We monkeypatch CONFIG_DIR and _LEGACY_DIR onto a tmp scratch space so
the test doesn't touch the user's real home directory.

Run from python/:  python tests/test_config_migration.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config


_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  [OK  ] {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
        raise AssertionError(f"{name}  {detail}".rstrip())


def _with_scratch(test):
    """Run `test(scratch_dir)` with config's dir constants pointed at a
    temp scratch dir; restore originals on exit so other tests aren't
    affected."""
    saved_config = config.CONFIG_DIR
    saved_config_path = config.CONFIG_PATH
    saved_legacy = config._LEGACY_DIR
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        config.CONFIG_DIR = scratch_path / "new"
        config.CONFIG_PATH = config.CONFIG_DIR / "config.json"
        config._LEGACY_DIR = scratch_path / "old"
        try:
            test(scratch_path)
        finally:
            config.CONFIG_DIR = saved_config
            config.CONFIG_PATH = saved_config_path
            config._LEGACY_DIR = saved_legacy


def test_legacy_present_migrates() -> None:
    """Legacy dir exists, new dir doesn't → rename old to new."""
    print()
    print("Test: legacy dir is renamed when new dir is absent")

    def body(scratch: Path) -> None:
        legacy = scratch / "old"
        legacy.mkdir()
        (legacy / "config.json").write_text(
            json.dumps({"client_id": "abc", "client_secret": "xyz"}),
            encoding="utf-8",
        )
        (legacy / "ability_metadata.json").write_text("{}", encoding="utf-8")

        config.ensure_config_dir_migrated()

        _check("legacy dir is gone after migration",
               not legacy.exists())
        _check("new dir exists after migration",
               config.CONFIG_DIR.exists())
        _check("config.json carried over",
               (config.CONFIG_DIR / "config.json").exists())
        _check("ability_metadata.json carried over",
               (config.CONFIG_DIR / "ability_metadata.json").exists())
        cfg = json.loads((config.CONFIG_DIR / "config.json").read_text())
        _check("credential payload intact",
               cfg.get("client_id") == "abc"
               and cfg.get("client_secret") == "xyz")

    _with_scratch(body)


def test_no_legacy_no_migration() -> None:
    """Clean install: neither dir exists → no-op."""
    print()
    print("Test: clean install — migration is a no-op")

    def body(_scratch: Path) -> None:
        config.ensure_config_dir_migrated()
        _check("new dir still absent (no creation without source)",
               not config.CONFIG_DIR.exists())

    _with_scratch(body)


def test_new_already_present_legacy_untouched() -> None:
    """Idempotent: when the new dir already exists, the legacy dir is
    left untouched (no clobbering of fresh data)."""
    print()
    print("Test: idempotent — existing new dir wins, legacy left alone")

    def body(scratch: Path) -> None:
        legacy = scratch / "old"
        legacy.mkdir()
        (legacy / "config.json").write_text(
            json.dumps({"client_id": "OLD"}),
            encoding="utf-8",
        )

        config.CONFIG_DIR.mkdir()
        config.CONFIG_PATH.write_text(
            json.dumps({"client_id": "NEW"}),
            encoding="utf-8",
        )

        config.ensure_config_dir_migrated()

        _check("legacy dir still exists", legacy.exists())
        _check("legacy config still has OLD payload",
               json.loads((legacy / "config.json").read_text()).get("client_id") == "OLD")
        _check("new config still has NEW payload",
               json.loads(config.CONFIG_PATH.read_text()).get("client_id") == "NEW")

    _with_scratch(body)


def test_load_config_triggers_migration() -> None:
    """load_config() is the public entry point that runs at sidecar
    startup; it must trigger the migration."""
    print()
    print("Test: load_config triggers migration on first call")

    def body(scratch: Path) -> None:
        legacy = scratch / "old"
        legacy.mkdir()
        (legacy / "config.json").write_text(
            json.dumps({"client_id": "migrated"}),
            encoding="utf-8",
        )

        cfg = config.load_config()

        _check("load_config returns migrated data",
               cfg.get("client_id") == "migrated",
               f"got {cfg}")
        _check("legacy dir gone after load_config",
               not legacy.exists())

    _with_scratch(body)


def main() -> int:
    test_legacy_present_migrates()
    test_no_legacy_no_migration()
    test_new_already_present_legacy_untouched()
    test_load_config_triggers_migration()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}    {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
