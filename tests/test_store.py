import json

import pytest

import store


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CONFIG_DIR", tmp_path / "distech-ble-remote")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "distech-ble-remote" / "devices.json")
    yield


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
    # passkeys are secret-ish -> file must be 0600
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
