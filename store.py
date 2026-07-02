#!/usr/bin/env python3
"""
Persistent per-device store for Distech Controls BLE Remote.

Keeps local nicknames, the (per-unit) pairing passkey, and the last-seen label at
~/.config/distech-ble-remote/devices.json. The passkey is written 0600. `load()` never
raises — a missing or corrupt file yields an empty store.

The store is just a dict {address: {"nickname":..., "passkey":..., "label":...}}
that callers mutate through the set_* helpers (which persist immediately).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "distech-ble-remote"
STORE_PATH = CONFIG_DIR / "devices.json"
VERSION = 1


def load() -> dict[str, dict]:
    """Return {address: entry}. Never raises; {} on missing/corrupt file."""
    try:
        data = json.loads(STORE_PATH.read_text())
        devices = data.get("devices", {})
        return devices if isinstance(devices, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save(devices: dict[str, dict]) -> None:
    """Atomically write the store, 0600 (it holds passkeys)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_name(STORE_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"version": VERSION, "devices": devices}, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE_PATH)


def _entry(devices: dict[str, dict], addr: str) -> dict:
    return devices.setdefault(addr, {})


def get_nickname(devices: dict[str, dict], addr: str) -> str | None:
    return devices.get(addr, {}).get("nickname")


def set_nickname(devices: dict[str, dict], addr: str, nickname: str | None) -> None:
    if nickname:
        _entry(devices, addr)["nickname"] = nickname
    else:
        devices.get(addr, {}).pop("nickname", None)
    save(devices)


def get_passkey(devices: dict[str, dict], addr: str) -> int | None:
    v = devices.get(addr, {}).get("passkey")
    return int(v) if v is not None else None


def set_passkey(devices: dict[str, dict], addr: str, passkey: int) -> None:
    _entry(devices, addr)["passkey"] = int(passkey)
    save(devices)


def set_label(devices: dict[str, dict], addr: str, label: str | None) -> None:
    if label:
        _entry(devices, addr)["label"] = label
        save(devices)
