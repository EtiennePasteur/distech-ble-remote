#!/usr/bin/env python3
"""
Read + decode the live state of a bonded Distech controller (over distech_ble).

Usage:
    python state.py [ADDRESS]
"""
from __future__ import annotations

import asyncio
import struct
import sys

from bleak import BleakClient

import distech_ble as core
from distech_ble import BASE, STATE_CHAR  # re-export

u = lambda x: x + BASE  # noqa: E731
CHARS = core.READ_CHARS + core.STATE_CHARS
NAN_SENTINEL = core.NAN_SENTINEL


def decode_floats(b: bytes) -> list[str]:
    out = []
    for off in range(0, len(b) - 3):
        val = struct.unpack("<f", b[off:off + 4])[0]
        if val != val:  # NaN
            continue
        if val == 0.0 or 0.05 <= abs(val) <= 120.0:
            out.append(f"@{off}={val:g}")
    return out


def decode_state_82(b: bytes) -> None:
    if len(b) < 26:
        return
    f = lambda o: struct.unpack("<f", b[o:o + 4])[0]  # noqa: E731
    print("        [structured]")
    print(f"          [10] TEMP now   = {f(10):.2f} °C")
    print(f"          [14] OFFSET     = {core.offset_of(b):+.2f} °C")
    print(f"          [18] FAN        = {core.describe_fan(core.fan_of(b))}")


def hexdump(b: bytes) -> str:
    ascii_ = "".join(chr(c) if 32 <= c < 127 else "." for c in b)
    return f"({len(b):>3}B) {b.hex(' '):<48} |{ascii_}|"


async def main() -> None:
    addr = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:FF"
    print(f"[*] connecting {addr} ...")
    async with BleakClient(addr, timeout=core.CONNECT_TIMEOUT) as client:
        print(f"[+] connected: {client.is_connected}\n")
        for s in CHARS:
            try:
                v = bytes(await asyncio.wait_for(client.read_gatt_char(u(s)), timeout=8))
            except Exception as e:  # noqa: BLE001
                print(f"{s}: <err {type(e).__name__}: {e}>")
                continue
            print(f"{s}: {hexdump(v)}")
            if s == "00020001":
                decode_state_82(v)


if __name__ == "__main__":
    asyncio.run(main())
