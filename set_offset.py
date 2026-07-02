#!/usr/bin/env python3
"""
Set the personal-comfort temperature offset (thin CLI over distech_ble).

Usage:
    python set_offset.py <ADDRESS> <offset_celsius> [--restore-to <v>]
    python set_offset.py AA:BB:CC:DD:EE:FF 1.0
"""
from __future__ import annotations

import asyncio
import sys

from bleak import BleakClient

import distech_ble as core
from distech_ble import ACK_CHAR, CMD_ID, STATE_CHAR, WRITE_CHAR  # re-export

REG = core.REG_OFFSET
OFFSET_POS = 14


def build_frame(value: float) -> bytes:
    return core.build_frame(REG, value)


def offset_of(state: bytes) -> float:
    return core.offset_of(state)


async def read_offset(client: BleakClient) -> float:
    return core.offset_of(await core.read_state(client))


async def write_offset(client: BleakClient, value: float) -> None:
    await core.write_register(client, REG, value)


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    addr, target = sys.argv[1], float(sys.argv[2])
    restore_to = float(sys.argv[sys.argv.index("--restore-to") + 1]) if "--restore-to" in sys.argv else None

    print(f"[*] connecting {addr} ...")
    async with BleakClient(addr, timeout=core.CONNECT_TIMEOUT) as client:
        print(f"[+] current offset = {await read_offset(client):+.2f} C")
        await write_offset(client, target)
        await asyncio.sleep(1.2)
        after = await read_offset(client)
        ok = abs(after - target) < 0.05
        print(f"[{'✓' if ok else 'x'}] offset now = {after:+.2f} C")
        if restore_to is not None:
            await write_offset(client, restore_to)
            await asyncio.sleep(1.2)
            print(f"[i] restored offset = {await read_offset(client):+.2f} C")


if __name__ == "__main__":
    asyncio.run(main())
