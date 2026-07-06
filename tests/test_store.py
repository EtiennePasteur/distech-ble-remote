import json
import os

import pytest

import store
import distech_ble as core


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CONFIG_DIR", tmp_path / "distech-ble-remote")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "distech-ble-remote" / "devices.json")
    monkeypatch.setattr(store, "CONFIG_PATH", tmp_path / "distech-ble-remote" / "config.json")
    yield


def _write_config(obj):
    store.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    store.CONFIG_PATH.write_text(json.dumps(obj))


def test_missing_file_returns_empty():
    assert store.load() == {}


def test_round_trip_and_mode():
    d = {}
    store.set_nickname(d, "AA:BB", "Desk")
    store.set_passkey(d, "AA:BB", 123456)
    assert store.get_nickname(d, "AA:BB") == "Desk"
    assert store.get_passkey(d, "AA:BB") == 123456
    reloaded = store.load()
    assert reloaded["AA:BB"]["nickname"] == "Desk"
    assert reloaded["AA:BB"]["passkey"] == 123456
    # passkeys are secret-ish -> file must be 0600 (POSIX perms; not enforced on Windows)
    if os.name == "posix":
        assert (store.STORE_PATH.stat().st_mode & 0o777) == 0o600


def test_clear_nickname():
    d = {}
    store.set_nickname(d, "AA:BB", "Desk")
    store.set_nickname(d, "AA:BB", "")
    assert store.get_nickname(d, "AA:BB") is None


def test_corrupt_file_returns_empty():
    store.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    store.STORE_PATH.write_text("{not json")
    assert store.load() == {}


def test_atomic_no_tmp_left():
    store.save({"AA:BB": {"nickname": "x"}})
    leftovers = list(store.CONFIG_DIR.glob("*.tmp"))
    assert leftovers == []
    assert json.loads(store.STORE_PATH.read_text())["devices"]["AA:BB"]["nickname"] == "x"


def test_get_config_missing_and_corrupt():
    assert store.get_config() == {}          # no file
    assert store.get_zone() is None
    _write_config("not-a-dict")
    assert store.get_config() == {}          # non-dict payload
    store.CONFIG_PATH.write_text("{not json")
    assert store.get_config() == {}          # corrupt


def test_get_zone_from_config():
    _write_config({"zone": "NIV1_A1"})
    assert store.get_zone() == "NIV1_A1"
    _write_config({"zone": ""})              # blank counts as unset
    assert store.get_zone() is None
    _write_config({})                        # key absent
    assert store.get_zone() is None


def test_resolve_zone_env_beats_config(monkeypatch):
    monkeypatch.setenv("DISTECH_ZONE", "ENVZONE")
    _write_config({"zone": "CFGZONE"})
    assert core.resolve_zone() == "ENVZONE"


def test_resolve_zone_config_beats_default(monkeypatch):
    monkeypatch.delenv("DISTECH_ZONE", raising=False)
    _write_config({"zone": "CFGZONE"})
    assert core.resolve_zone() == "CFGZONE"


def test_resolve_zone_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("DISTECH_ZONE", raising=False)
    assert core.resolve_zone() == core.DEFAULT_ZONE
