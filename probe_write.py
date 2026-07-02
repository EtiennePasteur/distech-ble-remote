#!/usr/bin/env python3
"""
Empirically discover the write frame for char 00000004 (the command channel).

Strategy: set the personal-comfort OFFSET to a distinctive in-range test value
(+1.5 C by default) using a curated, ordered list of candidate frames. After each
write we re-read the 82-byte state (0x00020001), diff it against the baseline, and
report exactly which bytes moved. We stop at the first frame that makes the offset
field (byte [14]) equal the target. The original offset is always restored.

Safety: every candidate carries the SAME bounded value (target in [-3,+3]), so any
misparse still lands within the allowed +/-3 C range. Char 00000004 is write-only
(write-with-response). Reversible + snapshotted.

Usage:
    python probe_write.py [ADDRESS] [TARGET_OFFSET]
    python probe_write.py AA:BB:CC:DD:EE:FF 1.5
"""
from __future__ import annotations

import asyncio
import struct
import sys

from bleak import BleakClient

ADDR = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:FF"
TARGET = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5

BASE = "-0000-0000-0000-00137e5a8eef"
u = lambda x: f"{x}{BASE}"
CMD = u("00000004")      # write
STATE = u("00020001")    # read,notify
OFFSET_POS = 14          # byte offset of the OFFSET float in the state blob


def f_at(b: bytes, pos: int) -> float:
    return struct.unpack("<f", b[pos:pos + 4])[0] if len(b) >= pos + 4 else float("nan")


def diff(base: bytes, new: bytes) -> list[str]:
    changes = []
    for i in range(min(len(base), len(new))):
        if base[i] != new[i]:
            changes.append(i)
    # group consecutive changed offsets, show float interpretation of each run
    out, i = [], 0
    while i < len(changes):
        j = i
        while j + 1 < len(changes) and changes[j + 1] == changes[j] + 1:
            j += 1
        lo, hi = changes[i], changes[j]
        span = new[lo:hi + 1]
        fv = ""
        if lo + 4 <= len(new):
            fv = f" (float@{lo}={f_at(new, lo):g})"
        out.append(f"[{lo}..{hi}] {base[lo:hi+1].hex(' ')} -> {span.hex(' ')}{fv}")
        i = j + 1
    return out


def candidate_frames(value: float) -> list[tuple[str, bytes]]:
    """Round 2: structured 'point write' hypotheses. NaN sentinel = 'keep field'."""
    V = struct.pack("<f", value)                 # +1.5 -> 00 00 c0 3f
    N = bytes.fromhex("0000c07f")                # NaN = leave-unchanged sentinel
    c: list[tuple[str, bytes]] = []
    # record-mirror of the state point 0x03: [id][temp][offset][+lim][-lim]
    c.append(("rec03 N,V,N,N", b"\x03" + N + V + N + N))
    c.append(("rec03 N,V", b"\x03" + N + V))          # id + temp(keep) + offset
    c.append(("rec03 V,N,N", b"\x03" + V + N + N))    # offset as 1st field
    c.append(("hdr01 rec03 N,V", b"\x01\x03" + N + V))# leading count then record
    c.append(("rec00 N,V,N,N", b"\x00" + N + V + N + N))
    # value + BACnet-ish priority terminator
    c.append(("V pri08", V + b"\x08"))
    c.append(("[03] V pri08", b"\x03" + V + b"\x08"))
    c.append(("[03][01] V pri08", b"\x03\x01" + V + b"\x08"))
    # integer step encodings: offset/step = 1.5/0.5 = 3
    c.append(("[03][01] i16=3", b"\x03\x01" + struct.pack("<h", 3)))
    c.append(("[03][01] i16=15", b"\x03\x01" + struct.pack("<h", 15)))
    c.append(("[03][01] i8=3", b"\x03\x01\x03"))
    # handle from config + record-mirror
    c.append(("h8801 N,V", b"\x88\x01" + N + V))
    return c


async def read_state(client) -> bytes:
    return bytes(await asyncio.wait_for(client.read_gatt_char(STATE), timeout=8))


async def main() -> None:
    print(f"[*] connecting {ADDR} ... (target offset = {TARGET:+.2f} C)")
    async with BleakClient(ADDR, timeout=25) as client:
        # notifications: the device pushing a new state = strong "it worked" signal
        pushed = {"blob": None}
        try:
            await client.start_notify(STATE, lambda ch, d: pushed.update(blob=bytes(d)))
        except Exception as e:  # noqa: BLE001
            print(f"[i] notify subscribe failed (will poll instead): {e}")

        baseline = await read_state(client)
        orig_offset = f_at(baseline, OFFSET_POS)
        print(f"[+] baseline offset [14] = {orig_offset:+.2f} C")
        print(f"[+] baseline temp   [10] = {f_at(baseline, 10):.2f} C\n")

        winner = None
        try:
            for name, frame in candidate_frames(TARGET):
                try:
                    await client.write_gatt_char(CMD, frame, response=True)
                    werr = None
                except Exception as e:  # noqa: BLE001
                    werr = f"{type(e).__name__}: {e}"
                await asyncio.sleep(0.9)
                new = await read_state(client)
                off = f_at(new, OFFSET_POS)
                changed = diff(baseline, new)
                tag = "WRITE-ERR " + werr if werr else "written   "
                hit = abs(off - TARGET) < 0.05
                mark = "  <<< OFFSET HIT" if hit else ""
                print(f"  {name:<20} {frame.hex(' '):<20} {tag[:38]:<38} off={off:+.2f}{mark}")
                for ch in changed:
                    print(f"        changed {ch}")
                if hit:
                    winner = (name, frame)
                    break
                baseline = new  # track drift so temp changes don't look like our doing
        finally:
            # ALWAYS restore the original offset if we learned how
            if winner:
                name, frame = winner
                restore = frame[:-4] + struct.pack("<f", orig_offset)
                try:
                    await client.write_gatt_char(CMD, restore, response=True)
                    await asyncio.sleep(0.8)
                    back = f_at(await read_state(client), OFFSET_POS)
                    print(f"\n[✓] FRAME FOUND: '{name}'  ->  {frame.hex(' ')}")
                    print(f"[✓] restored offset to {back:+.2f} C (was {orig_offset:+.2f})")
                except Exception as e:  # noqa: BLE001
                    print(f"[!] restore failed, set it back in the app: {e}")
            else:
                cur = f_at(await read_state(client), OFFSET_POS)
                print(f"\n[x] No candidate set the offset. Current offset = {cur:+.2f} C "
                      f"(baseline was {orig_offset:+.2f}).")
                print("    -> next: widen candidates or fall back to HCI capture.")


if __name__ == "__main__":
    asyncio.run(main())
