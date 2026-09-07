"""backlinks-api.json holds API keys; loading it must restrict it to the user."""

from __future__ import annotations

import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import backlinks_auth  # noqa: E402


def _write_config(tmp_path, monkeypatch, mode=None):
    target = tmp_path / "backlinks-api.json"
    target.write_text(json.dumps({"moz_api_key": "moz-key"}), encoding="utf-8")
    if mode is not None:
        os.chmod(target, mode)
    monkeypatch.setattr(backlinks_auth, "CONFIG_PATH", str(target))
    for name in ("MOZ_API_KEY", "BING_WEBMASTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return target


def test_load_config_hardens_the_key_file(tmp_path, monkeypatch) -> None:
    target = _write_config(tmp_path, monkeypatch)
    hardened = []
    monkeypatch.setattr(backlinks_auth, "harden_credential_file", lambda p: hardened.append(p) or True)

    config = backlinks_auth.load_config()

    assert config["moz_api_key"] == "moz-key"
    assert hardened == [str(target)]


def test_load_config_does_not_touch_a_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backlinks_auth, "CONFIG_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(backlinks_auth, "harden_credential_file", lambda p: pytest.fail(f"hardened {p}"))
    monkeypatch.setenv("MOZ_API_KEY", "from-env")

    assert backlinks_auth.load_config()["moz_api_key"] == "from-env"


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX mode bits")
def test_load_config_remediates_a_world_readable_key_file(tmp_path, monkeypatch) -> None:
    target = _write_config(tmp_path, monkeypatch, mode=0o644)
    assert target.stat().st_mode & 0o777 == 0o644

    backlinks_auth.load_config()

    assert target.stat().st_mode & 0o777 == 0o600
