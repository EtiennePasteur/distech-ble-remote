#!/usr/bin/env python3
"""
Set the fan mode (Auto/0/1/2/3) — thin CLI over distech_ble.

Usage:
    python set_fan.py <ADDRESS> <auto|0|1|2|3> [--restore]
    python set_fan.py AA:BB:CC:DD:EE:FF 3
"""
from __future__ import annotations

import asyncio
import sys

from bleak import BleakClient

import distech_ble as core
from distech_ble import CMD_ID, STATE_CHAR, WRITE_CHAR  # re-export

REG_FAN = core.REG_FAN
FAN_POS = 18
SETTING_TO_VALUE = core.FAN_SETTING_TO_VALUE
VALUE_TO_SETTING = core.FAN_VALUE_TO_SETTING


def frame(value: float) -> bytes:
    return core.build_frame(REG_FAN, value)


def fan_value(state: bytes) -> float:
    return core.read_register(state, REG_FAN)


def describe(v: float) -> str:
    for value, setting in VALUE_TO_SETTING.items():
        if abs(v - value) < 0.05:
            return f"{v:g} (= {'Auto' if setting == 'auto' else 'speed ' + setting})"
    return f"{v:g} (unmapped)"


async def main() -> None:
    if len(sys.argv) < 3 or sys.argv[2].lower() not in SETTING_TO_VALUE:
        print(__doc__)
        return
    addr, setting = sys.argv[1], sys.argv[2].lower()
    restore = "--restore" in sys.argv

    print(f"[*] connecting {addr} ...")
    async with BleakClient(addr, timeout=core.CONNECT_TIMEOUT) as client:
        orig = fan_value(await core.read_state(client))
        print(f"[+] current fan value [18] = {describe(orig)}")
        await core.set_fan(client, setting)
        await asyncio.sleep(1.2)
        after = fan_value(await core.read_state(client))
        label = "Auto" if setting == "auto" else f"speed {setting}"
        print(f"[✓] fan set to {label}; state [18] now = {describe(after)}")
        if restore:
            await core.write_register(client, REG_FAN, orig)
            await asyncio.sleep(1.0)
            print(f"[i] restored fan to {describe(fan_value(await core.read_state(client)))}")


if __name__ == "__main__":
    asyncio.run(main())
