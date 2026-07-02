#!/usr/bin/env python3
"""
explore - a small BLE explorer for Distech Controls AC units
           (the ones the "myPersonify" phone app talks to).

The AC controllers advertise with names like:  NIVx_Cy_Tzz
                                                ^    ^  ^
                                                |    |  +-- unit / thermostat number
                                                |    +----- zone (e.g. C2)
                                                +---------- floor  ("niveau 0")

Workflow (see README.md for the full story):

  1.  scan            find the controllers around you, note their address + name
  2.  enum   ADDR     dump every GATT service / characteristic + read them
  3.  monitor ADDR    subscribe to notifications and watch values change live
  4.  read/write      once you know which characteristic does what, poke it

Run every command with the venv active:  . .venv/bin/activate

Nothing here is Distech-specific yet: it is a generic BLE explorer. The point is
to *discover* the protocol. As we learn what each characteristic means we encode
that knowledge into presets at the bottom of this file.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

DEFAULT_PREFIX = "NIV"          # matches NIVx_Cy_Tzz & friends; use --all to see everything
DEFAULT_ADAPTER = "hci0"        # WSL / Linux BlueZ adapter


# --------------------------------------------------------------------------- #
# pretty-printing helpers
# --------------------------------------------------------------------------- #
def hexdump(data: bytes, indent: str = "    ") -> str:
    """Classic offset / hex / ascii dump, easy to eyeball for structure."""
    if not data:
        return indent + "(empty)"
    out = []
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        hexpart = f"{hexpart:<47}"
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{indent}{off:04x}  {hexpart}  |{asciipart}|")
    return "\n".join(out)


def short_hex(data: bytes) -> str:
    return data.hex(" ") if data else "-"


def now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
async def cmd_scan(args) -> None:
    seen: dict[str, tuple] = {}
    prefix = "" if args.all else args.filter

    def match(name: str | None) -> bool:
        if not prefix:
            return True
        return bool(name) and prefix.lower() in name.lower()

    def on_detect(device, adv):
        name = adv.local_name or device.name
        if not match(name):
            return
        prev = seen.get(device.address)
        # keep the strongest RSSI / best name we have seen for this device
        if prev is None or (adv.rssi or -999) > prev[2]:
            seen[device.address] = (device, adv, adv.rssi or -999, name)

    scanner = BleakScanner(
        detection_callback=on_detect,
        scanning_mode="active",           # active => we get scan-response names
        adapter=args.adapter,
    )

    label = "everything" if args.all else f'names containing "{prefix}"'
    print(f"[*] Scanning for {label} for {args.timeout:.0f}s on {args.adapter} ...")
    await scanner.start()
    try:
        await asyncio.sleep(args.timeout)
    finally:
        await scanner.stop()

    if not seen:
        print("[!] Nothing matched. Try:  explore.py scan --all")
        return

    rows = sorted(seen.values(), key=lambda r: r[2], reverse=True)
    print(f"\n[+] {len(rows)} device(s):\n")
    print(f"    {'ADDRESS':<18} {'RSSI':>5}  {'NAME':<22} SERVICES / MFR-DATA")
    print(f"    {'-'*18} {'-'*5}  {'-'*22} {'-'*30}")
    for device, adv, rssi, name in rows:
        svc = ",".join(u[4:8] for u in adv.service_uuids) or "-"   # short 16-bit-ish form
        mfr = ";".join(f"{cid:#06x}:{d.hex()}" for cid, d in adv.manufacturer_data.items()) or "-"
        extra = svc if svc != "-" else mfr
        print(f"    {device.address:<18} {rssi:>5}  {name or '(unknown)':<22} {extra}")
    print("\n    next:  explore.py enum <ADDRESS>")


# --------------------------------------------------------------------------- #
# enumerate GATT
# --------------------------------------------------------------------------- #
async def cmd_enum(args) -> None:
    print(f"[*] Connecting to {args.address} on {args.adapter} ...")
    async with BleakClient(args.address, adapter=args.adapter, timeout=args.timeout) as client:
        print(f"[+] Connected. MTU={getattr(client, 'mtu_size', '?')}\n")
        for service in client.services:
            print(f"[service] {service.uuid}  ({service.description})")
            for ch in service.characteristics:
                props = ",".join(ch.properties)
                print(f"    [char] {ch.uuid}  handle={ch.handle}  ({props})  {ch.description}")
                if args.read and "read" in ch.properties:
                    try:
                        val = await client.read_gatt_char(ch)
                        print(f"        = {short_hex(val)}")
                        if len(val) > 16:
                            print(hexdump(val, indent="          "))
                    except Exception as e:               # noqa: BLE001
                        print(f"        (read failed: {e})")
                for d in ch.descriptors:
                    print(f"        [desc] {d.uuid}  handle={d.handle}")
        print("\n    next:  explore.py monitor <ADDRESS>   (then change something in the app)")


# --------------------------------------------------------------------------- #
# monitor notifications
# --------------------------------------------------------------------------- #
async def cmd_monitor(args) -> None:
    print(f"[*] Connecting to {args.address} on {args.adapter} ...")
    async with BleakClient(args.address, adapter=args.adapter, timeout=args.timeout) as client:
        def make_cb(uuid: str):
            def cb(ch: BleakGATTCharacteristic, data: bytearray):
                print(f"{now()}  {uuid}  ({len(data):>3}B)  {short_hex(bytes(data))}")
            return cb

        subscribed = 0
        for service in client.services:
            for ch in service.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    with contextlib.suppress(Exception):
                        await client.start_notify(ch, make_cb(ch.uuid))
                        subscribed += 1
        if not subscribed:
            print("[!] No notify/indicate characteristics on this device.")
            return
        print(f"[+] Subscribed to {subscribed} characteristic(s). "
              f"Watching... (Ctrl+C to stop)\n")
        print(f"    {'TIME':<12}  {'CHARACTERISTIC':<36}  {'LEN':>4}  VALUE")
        with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
            while True:
                await asyncio.sleep(1)


# --------------------------------------------------------------------------- #
# read / write a single characteristic
# --------------------------------------------------------------------------- #
async def cmd_read(args) -> None:
    async with BleakClient(args.address, adapter=args.adapter, timeout=args.timeout) as client:
        val = await client.read_gatt_char(args.char)
        print(f"{args.char} = {short_hex(val)}")
        print(hexdump(val))


async def cmd_write(args) -> None:
    payload = bytes.fromhex(args.data.replace(" ", ""))
    async with BleakClient(args.address, adapter=args.adapter, timeout=args.timeout) as client:
        await client.write_gatt_char(args.char, payload, response=not args.no_response)
        print(f"[+] Wrote {len(payload)} byte(s) to {args.char}: {short_hex(payload)}")
        if args.readback:
            with contextlib.suppress(Exception):
                val = await client.read_gatt_char(args.char)
                print(f"    readback = {short_hex(val)}")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="BLE toolkit for reverse-engineering Distech Controls AC units.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--adapter", default=DEFAULT_ADAPTER, help=f"BlueZ adapter (default {DEFAULT_ADAPTER})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan for nearby AC controllers")
    s.add_argument("-t", "--timeout", type=float, default=8.0, help="scan seconds (default 8)")
    s.add_argument("-f", "--filter", default=DEFAULT_PREFIX, help=f'name substring (default "{DEFAULT_PREFIX}")')
    s.add_argument("--all", action="store_true", help="show every device, no name filter")
    s.set_defaults(func=cmd_scan)

    e = sub.add_parser("enum", help="connect and dump all GATT services/characteristics")
    e.add_argument("address")
    e.add_argument("-t", "--timeout", type=float, default=20.0, help="connect timeout")
    e.add_argument("--no-read", dest="read", action="store_false", help="don't auto-read readable chars")
    e.set_defaults(func=cmd_enum, read=True)

    m = sub.add_parser("monitor", help="subscribe to notifications and watch values live")
    m.add_argument("address")
    m.add_argument("-t", "--timeout", type=float, default=20.0, help="connect timeout")
    m.set_defaults(func=cmd_monitor)

    r = sub.add_parser("read", help="read one characteristic")
    r.add_argument("address")
    r.add_argument("char", help="characteristic UUID")
    r.add_argument("-t", "--timeout", type=float, default=20.0)
    r.set_defaults(func=cmd_read)

    w = sub.add_parser("write", help="write bytes to one characteristic")
    w.add_argument("address")
    w.add_argument("char", help="characteristic UUID")
    w.add_argument("data", help='hex bytes, e.g. "01 2c" or 012c')
    w.add_argument("--no-response", action="store_true", help="write-without-response")
    w.add_argument("--readback", action="store_true", help="read the char again after writing")
    w.add_argument("-t", "--timeout", type=float, default=20.0)
    w.set_defaults(func=cmd_write)

    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(130)


if __name__ == "__main__":
    main()
