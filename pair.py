#!/usr/bin/env python3
"""
Pair with a Distech AC controller using its passkey (thin CLI over the platform
pairing backend). Works on Linux (BlueZ), Windows (WinRT) and macOS (CoreBluetooth).

On macOS the code cannot be injected from Python — the OS shows its own passkey
dialog; type the code there when it appears.

Usage:
    python pair.py [ADDRESS] [PASSKEY]
    python pair.py AA:BB:CC:DD:EE:FF 0364    # address + the pairing code printed for your unit
"""
from __future__ import annotations

import asyncio
import sys

from bleak import BleakClient

import distech_ble as core
import pairing
from distech_ble import BASE  # noqa: F401  (kept for callers that historically imported it here)

u = lambda x: x + BASE  # noqa: E731


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    addr = sys.argv[1]
    passkey = int(sys.argv[2])

    backend = pairing.get_pairing_backend()
    await backend.open()
    try:
        if not backend.supports_passkey_injection:
            print(f"[*] macOS: enter {passkey:04d} in the system dialog when it appears ...")
        else:
            print(f"[*] pairing {addr} with passkey {passkey:04d} ...")
        ok = await backend.pair(addr, passkey)
        print(f"[{'+' if ok else '!'}] pair {'succeeded — bonded' if ok else 'FAILED (check the code)'}")
        if ok:
            async with BleakClient(addr, timeout=core.CONNECT_TIMEOUT) as client:
                state = await core.read_state(client)
                print(f"    offset = {core.offset_of(state):+.2f} C   fan = {core.describe_fan(core.fan_of(state))}")
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
