# Distech Controls BLE Remote

Control **Distech Controls** AC room controllers from your computer over Bluetooth LE —
a terminal UI plus a few small CLIs. These are the units the **myPersonify** app drives; this tool
speaks the same BLE protocol, reverse-engineered for interoperability.

## ⚠️ Disclaimer

- **Not affiliated with, endorsed by, or connected to Distech Controls or Acuity Brands.**
  "Distech Controls" and "myPersonify" are trademarks of their respective owners.
- This talks to **real HVAC equipment**. Only use it on units you are **authorized to control**, and
  be aware you can change the temperature/fan of a space. **Use at your own risk.**
- The myPersonify app itself is **not** included in this repository (it's proprietary). The
  reverse-engineering here was done for interoperability with equipment the author operates.
- Pairing codes are stored locally in `~/.config/distech-ble-remote/devices.json` (mode `0600`) —
  **never commit that file**.

## What it does

- 🔍 Live scan + list of nearby controllers with signal strength
- 🔑 BLE **passkey pairing** (bonds + trusts the unit)
- 🌡️ Read live state: room temperature, comfort **offset**, **fan**
- 🎛️ Set the comfort temperature **offset** (−3…+3 °C) and **fan** (Auto / 0 / 1 / 2 / 3)
- 🖥️ **Textual TUI**: multi-select units, local nicknames, apply to many at once

## Requirements

- **Linux with BlueZ** (developed on Ubuntu; also works in WSL2 with a passed-through adapter).
  Pairing and bond-state use BlueZ over D-Bus, so those parts are Linux-only.
- Python 3.10+

The BLE control itself uses [bleak](https://github.com/hbldh/bleak), which is cross-platform; on
Windows/macOS you would pair the unit through the OS first, then the read/offset/fan commands work.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Usage

### Terminal UI (recommended)

```bash
python tui.py
```

Keys: `↑/↓` or `j/k` move · `space` multi-select · `enter` connect + read · `p` pair (prompts for
the unit's code) · `n` nickname · `r` read status (highlighted unit) · `R` read all paired units ·
`-`/`+` stage offset · `a`/`0-3` stage fan · `o` apply offset · `f` apply fan · `A` apply both ·
`w` widen to all `NIV*` · `q` quit.

Reads happen **on demand** (`r` / `R` / `enter`), one unit at a time — the app no longer auto-reads
every unit at startup. While a batch (`R`) runs, units waiting their turn show `queued…`.

The list shows all `NIV*` controllers by default. Set your floor/zone prefix to narrow the default
view (press `w` to toggle back to everything). The zone is resolved by precedence
**`DISTECH_ZONE` env var → `config.json` → built-in default (`NIV`)**:

```bash
DISTECH_ZONE=NIV1_A1 python tui.py   # env var wins, for a single run
```

To set it persistently, add a `zone` to `~/.config/distech-ble-remote/config.json`:

```json
{ "zone": "NIV1_A1" }
```

### CLIs

```bash
python explore.py scan                        # find controllers nearby
python pair.py       AA:BB:CC:DD:EE:FF 000000  # bond a unit (address + its printed code)
python state.py      AA:BB:CC:DD:EE:FF         # read live state
python set_offset.py AA:BB:CC:DD:EE:FF 1.0     # comfort offset in °C
python set_fan.py    AA:BB:CC:DD:EE:FF 3       # fan: auto / 0 / 1 / 2 / 3
```

## How it works (protocol)

Reverse-engineered from the myPersonify Xamarin/.NET app. Controllers advertise as `NIVx_Cy_Tzz`
(floor / zone / unit) and expose a vendor GATT service (`…-00137e5a8eef`).

- **Pairing:** BLE passkey-entry; the passkey is the per-unit code. Reads/writes only succeed once
  the link is **bonded**.
- **Command frame** — written to the "write" characteristic, little-endian:

  ```
  LE16(cmdId = 0x0200)  LE16(reg)  LE32(float value)
  ```

  `NaN` (`0x7fc00000`) as the value means "leave this field unchanged".
- **`reg` equals the byte offset of that value inside the live-state blob**, so the current value of
  any control is read straight from the state characteristic at `[reg]`.
- Verified registers (cmdId `0x0200`):
  - temperature **offset** — `reg 0x0E`, float °C, clamped ±3
  - **fan** — `reg 0x12`; digital map: `Auto = 1.0`, numbered speed *N* = *N* + 2.0

The reverse-engineering path: Xamarin APK → [`pyxamstore`](https://github.com/jakev/pyxamstore)
(unpack `assemblies.blob`) → [`dnfile`](https://github.com/malwarefrank/dnfile) +
[`dncil`](https://github.com/mandiant/dncil) to read the .NET IL. `il.py` is the small IL browser
used for that.

## Development / tests

```bash
pip install -r requirements.txt   # includes pytest + pytest-asyncio
pytest
```

Tests cover the protocol (golden frames, decoding), the local store, and the TUI logic (via
Textual's headless `run_test()` with a fake BLE backend) — no adapter required.

## License

[MIT](LICENSE)
