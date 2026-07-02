#!/usr/bin/env python3
"""
distech_ble — shared BLE/protocol layer for Distech Controls AC units.

Single source of truth for: the command frame, the register<->state-byte-offset
convention, offset/fan controls, passkey pairing (BlueZ Agent1), scanning, and
bond-state discovery. The CLI scripts and the Textual TUI both import from here.

Protocol (reverse-engineered from myPersonify, see NOTES.md):
    write  LE16(cmdId) ++ LE16(reg) ++ LE32(float)  ->  char 00000004
    reg == byte offset of that value in the live-state blob (char 00020001)
    value = IEEE-754 float32 ; NaN (0x7fc00000) = "leave unchanged"
"""
from __future__ import annotations

import asyncio
import os
import struct
import warnings

from bleak import BleakClient, BleakScanner
from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

# bleak 3.0 nags about the `adapter=` kwarg; it still works and is what we use.
warnings.filterwarnings("ignore", message=".*adapter.*keyword argument is deprecated.*")

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
BASE = "-0000-0000-0000-00137e5a8eef"          # Distech vendor UUID suffix
WRITE_CHAR = "00000004" + BASE                  # WriteServiceResponselessRequest (only writable char)
STATE_CHAR = "00020001" + BASE                  # live-state blob (82 bytes)
ACK_CHAR = "00000005" + BASE                    # ack/nack notify (may be absent)
READ_CHARS = ["00010001", "00010002", "00010003", "00010004"]
STATE_CHARS = ["00020001", "00020002", "00020003"]

ADAPTER = "hci0"
NAME_PREFIX_BROAD = "NIV"                       # ingest family
DEFAULT_ZONE = "NIV"                            # code default when the zone is unset everywhere


def resolve_zone() -> str:
    """Narrow-view zone prefix. Precedence: DISTECH_ZONE env > ~/.config config.json > code default."""
    env = os.environ.get("DISTECH_ZONE")
    if env:
        return env
    try:
        import store  # soft, lazy dep: keep the BLE core usable without the config layer
        zone = store.get_zone()
        if zone:
            return zone
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_ZONE


NAME_PREFIX_NARROW = resolve_zone()            # narrow view; set DISTECH_ZONE=NIV1_A1 or "zone" in config.json

CMD_ID = 0x0200                                 # "global" device controls
REG_TEMP = 0x000A                               # room temp (read-only) -> state byte [10]
REG_OFFSET = 0x000E                             # setpoint offset  -> state byte [14]
REG_FAN = 0x0012                                # fan              -> state byte [18]

OFFSET_MIN, OFFSET_MAX, OFFSET_STEP = -3.0, 3.0, 0.5

FAN_SETTING_TO_VALUE = {"auto": 1.0, "0": 2.0, "1": 3.0, "2": 4.0, "3": 5.0}
FAN_VALUE_TO_SETTING = {v: k for k, v in FAN_SETTING_TO_VALUE.items()}
FAN_ORDER = ["auto", "0", "1", "2", "3"]

NAN_SENTINEL = bytes.fromhex("0000c07f")        # 0x7fc00000 LE = "no value / unchanged"

AGENT_PATH = "/distech_ble/agent"
CAPABILITY = "KeyboardDisplay"

CONNECT_TIMEOUT = 25.0
READ_TIMEOUT = 8.0


# --------------------------------------------------------------------------- #
# generic protocol (register == byte offset invariant)
# --------------------------------------------------------------------------- #
def build_frame(reg: int, value: float, cmd_id: int = CMD_ID) -> bytes:
    """LE16(cmd_id) ++ LE16(reg) ++ LE32(float value)."""
    return struct.pack("<HHf", cmd_id, reg, value)


def read_register(state: bytes, reg: int) -> float:
    """Decode the float32 at byte offset `reg` of the live-state blob."""
    return struct.unpack("<f", state[reg:reg + 4])[0]


async def read_state(client: BleakClient) -> bytes:
    return bytes(await asyncio.wait_for(client.read_gatt_char(STATE_CHAR), timeout=READ_TIMEOUT))


async def write_register(client: BleakClient, reg: int, value: float, cmd_id: int = CMD_ID) -> None:
    """Write a control frame. App uses write-without-response; fall back to with-response."""
    frame = build_frame(reg, value, cmd_id)
    try:
        await client.write_gatt_char(WRITE_CHAR, frame, response=False)
    except Exception:  # noqa: BLE001  (char may not advertise write-without-response)
        await client.write_gatt_char(WRITE_CHAR, frame, response=True)


# --------------------------------------------------------------------------- #
# typed controls
# --------------------------------------------------------------------------- #
def clamp_offset(v: float) -> float:
    """Clamp to ±3 and snap to the 0.5 step."""
    v = max(OFFSET_MIN, min(OFFSET_MAX, v))
    return round(v / OFFSET_STEP) * OFFSET_STEP


async def set_offset(client: BleakClient, value: float) -> None:
    await write_register(client, REG_OFFSET, clamp_offset(value))


def offset_of(state: bytes) -> float:
    return read_register(state, REG_OFFSET)


def temp_of(state: bytes) -> float | None:
    """Live room temperature (°C) at byte [10], or None if NaN/unreported."""
    v = read_register(state, REG_TEMP)
    return None if v != v else v  # NaN -> None


async def set_fan(client: BleakClient, setting: str) -> None:
    setting = str(setting).lower()
    if setting not in FAN_SETTING_TO_VALUE:
        raise ValueError(f"fan setting must be one of {list(FAN_SETTING_TO_VALUE)}")
    await write_register(client, REG_FAN, FAN_SETTING_TO_VALUE[setting])


def fan_of(state: bytes) -> str | None:
    """Map the fan value at byte [18] back to a setting, or None if unknown/NaN."""
    v = read_register(state, REG_FAN)
    if v != v:  # NaN
        return None
    for value, setting in FAN_VALUE_TO_SETTING.items():
        if abs(v - value) < 0.05:
            return setting
    return None


def describe_fan(setting: str | None) -> str:
    if setting is None:
        return "—"
    return "Auto" if setting == "auto" else f"speed {setting}"


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def name_of(device, adv) -> str | None:
    return adv.local_name or device.name


def parse_unit_label(name: str | None) -> str | None:
    """'NIVx_Cy_Tzz' -> 'Tzz' (last token). None if it doesn't look like a unit name."""
    if not name:
        return None
    parts = name.split("_")
    return parts[-1] if len(parts) >= 2 else name


def make_scanner(callback, adapter: str = ADAPTER) -> BleakScanner:
    """Active scanner (needed to receive scan-response names)."""
    return BleakScanner(detection_callback=callback, scanning_mode="active", adapter=adapter)


# --------------------------------------------------------------------------- #
# pairing (BlueZ Agent1)
# --------------------------------------------------------------------------- #
class PairingAgent(ServiceInterface):
    """Minimal org.bluez.Agent1 that answers the passkey prompt."""

    def __init__(self, passkey: int, pin: str):
        super().__init__("org.bluez.Agent1")
        self._passkey = passkey
        self._pin = pin

    @method()
    def Release(self):  # noqa: N802
        return

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802,F821
        return self._pin

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802,F821
        return self._passkey

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802,F821
        return

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802,F821
        return

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802,F821
        return

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
        return

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
        return

    @method()
    def Cancel(self):  # noqa: N802
        return


async def get_system_bus() -> MessageBus:
    return await MessageBus(bus_type=BusType.SYSTEM).connect()


async def register_agent(passkey: int, bus: MessageBus) -> PairingAgent:
    """(Re)register a default pairing agent that supplies `passkey`."""
    agent = PairingAgent(passkey, f"{passkey:04d}")
    try:
        bus.unexport(AGENT_PATH)
    except Exception:  # noqa: BLE001
        pass
    bus.export(AGENT_PATH, agent)
    intro = await bus.introspect("org.bluez", "/org/bluez")
    mgr = bus.get_proxy_object("org.bluez", "/org/bluez", intro).get_interface("org.bluez.AgentManager1")
    try:
        await mgr.call_unregister_agent(AGENT_PATH)
    except Exception:  # noqa: BLE001
        pass
    await mgr.call_register_agent(AGENT_PATH, CAPABILITY)
    await mgr.call_request_default_agent(AGENT_PATH)
    return agent


def device_path(address: str, adapter: str = ADAPTER) -> str:
    return f"/org/bluez/{adapter}/dev_" + address.replace(":", "_")


async def set_trusted(address: str, bus: MessageBus, adapter: str = ADAPTER) -> None:
    path = device_path(address, adapter)
    intro = await bus.introspect("org.bluez", path)
    props = bus.get_proxy_object("org.bluez", path, intro).get_interface(
        "org.freedesktop.DBus.Properties"
    )
    await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))


async def pair_device(address: str, passkey: int, *, bus: MessageBus, adapter: str = ADAPTER) -> bool:
    """Register the agent, connect+pair (BlueZ bonds), then mark trusted. True on success."""
    await register_agent(passkey, bus)
    try:
        async with BleakClient(address, timeout=CONNECT_TIMEOUT) as client:
            await client.pair()
        await set_trusted(address, bus, adapter)
        return True
    except Exception:  # noqa: BLE001  (AuthenticationFailed = wrong passkey, etc.)
        return False


# --------------------------------------------------------------------------- #
# bond / connection state (read-only, via BlueZ ObjectManager)
# --------------------------------------------------------------------------- #
async def bonded_and_connected(bus: MessageBus) -> dict[str, tuple[bool, bool]]:
    """{address: (bonded, connected)} for every device BlueZ knows about."""
    intro = await bus.introspect("org.bluez", "/")
    om = bus.get_proxy_object("org.bluez", "/", intro).get_interface(
        "org.freedesktop.DBus.ObjectManager"
    )
    objs = await om.call_get_managed_objects()
    out: dict[str, tuple[bool, bool]] = {}
    for _path, ifaces in objs.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev or "Address" not in dev:
            continue
        addr = dev["Address"].value
        bonded = bool(dev.get("Bonded", dev.get("Paired", Variant("b", False))).value)
        connected = bool(dev.get("Connected", Variant("b", False)).value)
        out[addr] = (bonded, connected)
    return out


def parse_paired_devices(text: str) -> set[str]:
    """Parse `bluetoothctl devices Paired` output ('Device <ADDR> <NAME>' lines)."""
    out: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            out.add(parts[1])
    return out
