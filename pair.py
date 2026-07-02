#!/usr/bin/env python3
"""
Pair with a Distech AC controller using its passkey (thin CLI over distech_ble).

Usage:
    python pair.py [ADDRESS] [PASSKEY]
    python pair.py AA:BB:CC:DD:EE:FF 000000    # address + the pairing code printed for your unit
"""
from __future__ import annotations

import asyncio
import sys

from bleak import BleakClient

import distech_ble as core
# re-export the names the rest of the toolkit historically imported from pair.py
from distech_ble import (  # noqa: F401
    AGENT_PATH,
    BASE,
    CAPABILITY,
    PairingAgent,
    get_system_bus,
    pair_device,
    register_agent,
)

u = lambda x: x + BASE  # noqa: E731


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    addr = sys.argv[1]
    passkey = int(sys.argv[2])

    bus = await get_system_bus()
    print(f"[*] pairing {addr} with passkey {passkey:06d} ...")
    ok = await pair_device(addr, passkey, bus=bus)
    print(f"[{'+' if ok else '!'}] pair {'succeeded — bonded + trusted' if ok else 'FAILED (check the code)'}")
    if ok:
        async with BleakClient(addr, timeout=core.CONNECT_TIMEOUT) as client:
            state = await core.read_state(client)
            print(f"    offset = {core.offset_of(state):+.2f} C   fan = {core.describe_fan(core.fan_of(state))}")
    bus.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
