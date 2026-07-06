#!/usr/bin/env python3
"""
pairing — platform-specific BLE pairing backends for Distech Controls units.

The GATT layer (scan/connect/read/write) is cross-platform via ``bleak`` and lives
in ``distech_ble``. Passkey *pairing* is **not** portable — every OS exposes a
different mechanism — so it is abstracted here behind :class:`PairingBackend`:

  • **Linux**   — BlueZ ``org.bluez.Agent1`` over D-Bus (``dbus-fast``). The passkey
                  is injected programmatically via the agent's ``RequestPasskey``.
  • **Windows** — WinRT ``DeviceInformationCustomPairing`` with ``ProvidePin``. The
                  passkey is injected via the ``PairingRequested`` handler
                  (``args.accept(pin)``). Note: bleak's own ``pair()`` only does
                  "Just Works" (``ConfirmOnly``), so we drive the WinRT API directly.
  • **macOS**   — CoreBluetooth pairs *implicitly* on first access to an encrypted
                  characteristic and shows its **own** passkey dialog; the code
                  cannot be injected programmatically (Apple restriction). So
                  ``supports_passkey_injection`` is ``False`` and ``pair()`` merely
                  triggers that dialog by reading an encrypted characteristic — the
                  user types the code into the OS prompt.

``get_pairing_backend()`` returns the right backend for the current platform. Every
OS-specific import (``dbus_fast``, ``winrt.*``) is done **lazily** inside the backend
that needs it, so ``import pairing`` never fails on a foreign platform.
"""
from __future__ import annotations

import asyncio
import sys

from bleak import BleakClient

import distech_ble as core


# --------------------------------------------------------------------------- #
# base / no-op backend
# --------------------------------------------------------------------------- #
class PairingBackend:
    """Default backend: reports pairing unavailable (safe fallback)."""

    #: True when the passkey can be supplied from code (Linux/Windows); False on
    #: macOS, where the OS collects the code through its own system dialog.
    supports_passkey_injection: bool = False

    async def open(self) -> object | None:
        """Acquire whatever handle the backend needs. Returns a truthy handle when
        pairing is usable, or raises if the subsystem is unavailable."""
        return None

    async def pair(self, address: str, passkey: int) -> bool:
        return False

    async def bond_state(self) -> dict[str, tuple[bool, bool]]:
        """``{address: (bonded, connected)}`` for known devices; ``{}`` if the OS
        does not expose a queryable bond list (macOS, and best-effort on Windows)."""
        return {}

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Linux — BlueZ Agent1 over D-Bus (dbus-fast)
# --------------------------------------------------------------------------- #
def _build_agent(passkey: int):
    """Build a fresh ``org.bluez.Agent1`` object that answers with ``passkey``.

    The ``dbus_fast.service`` import (and the ``ServiceInterface`` subclass it
    requires) is done here so the class is never defined on non-Linux platforms.
    """
    from dbus_fast.service import ServiceInterface, method

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

    return PairingAgent(passkey, f"{passkey:04d}")


class BluezPairing(PairingBackend):
    """Linux/BlueZ passkey pairing via a default ``org.bluez.Agent1`` agent."""

    supports_passkey_injection = True

    AGENT_PATH = "/distech_ble/agent"
    CAPABILITY = "KeyboardDisplay"

    def __init__(self) -> None:
        self._bus = None

    async def open(self):
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return self._bus

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def _register_agent(self, passkey: int) -> None:
        """(Re)register a default pairing agent that supplies ``passkey``."""
        agent = _build_agent(passkey)
        try:
            self._bus.unexport(self.AGENT_PATH)
        except Exception:  # noqa: BLE001
            pass
        self._bus.export(self.AGENT_PATH, agent)
        intro = await self._bus.introspect("org.bluez", "/org/bluez")
        mgr = self._bus.get_proxy_object("org.bluez", "/org/bluez", intro).get_interface(
            "org.bluez.AgentManager1"
        )
        try:
            await mgr.call_unregister_agent(self.AGENT_PATH)
        except Exception:  # noqa: BLE001
            pass
        await mgr.call_register_agent(self.AGENT_PATH, self.CAPABILITY)
        await mgr.call_request_default_agent(self.AGENT_PATH)

    async def _set_trusted(self, address: str) -> None:
        from dbus_fast import Variant

        path = f"/org/bluez/{core.ADAPTER}/dev_" + address.replace(":", "_")
        intro = await self._bus.introspect("org.bluez", path)
        props = self._bus.get_proxy_object("org.bluez", path, intro).get_interface(
            "org.freedesktop.DBus.Properties"
        )
        await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))

    async def pair(self, address: str, passkey: int) -> bool:
        """Register the agent, connect+pair (BlueZ bonds), then mark trusted."""
        await self._register_agent(passkey)
        try:
            async with BleakClient(address, timeout=core.CONNECT_TIMEOUT) as client:
                await client.pair()
            await self._set_trusted(address)
            return True
        except Exception:  # noqa: BLE001  (AuthenticationFailed = wrong passkey, etc.)
            return False

    async def bond_state(self) -> dict[str, tuple[bool, bool]]:
        """``{address: (bonded, connected)}`` for every device BlueZ knows about."""
        intro = await self._bus.introspect("org.bluez", "/")
        om = self._bus.get_proxy_object("org.bluez", "/", intro).get_interface(
            "org.freedesktop.DBus.ObjectManager"
        )
        objs = await om.call_get_managed_objects()
        out: dict[str, tuple[bool, bool]] = {}
        for _path, ifaces in objs.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev or "Address" not in dev:
                continue
            addr = dev["Address"].value
            bonded_v = dev.get("Bonded") or dev.get("Paired")  # Variant objects are always truthy
            connected_v = dev.get("Connected")
            bonded = bool(bonded_v.value) if bonded_v is not None else False
            connected = bool(connected_v.value) if connected_v is not None else False
            out[addr] = (bonded, connected)
        return out


# --------------------------------------------------------------------------- #
# Windows — WinRT DeviceInformationCustomPairing (ProvidePin)
# --------------------------------------------------------------------------- #
def _mac_from_uint64(addr: int) -> str:
    """0x00137e5a8eef -> 'AA:BB:CC:DD:EE:FF' (upper-case, colon-separated)."""
    b = int(addr).to_bytes(6, "big")
    return ":".join(f"{x:02X}" for x in b)


class WinRTPairing(PairingBackend):
    """Windows passkey pairing via WinRT ``DeviceInformationCustomPairing``.

    bleak's built-in ``pair()`` only performs the "Just Works" ceremony, so we
    reach into bleak's WinRT backend (``client._backend._requester``) to get the
    device's ``DeviceInformation`` and drive a ``ProvidePin`` pairing ourselves.
    """

    supports_passkey_injection = True

    async def open(self):
        return self  # no persistent handle is needed on Windows

    async def pair(self, address: str, passkey: int) -> bool:
        from winrt.windows.devices.enumeration import (
            DevicePairingKinds,
            DevicePairingResultStatus,
        )

        pin = f"{passkey:04d}"
        try:
            async with BleakClient(address, timeout=core.CONNECT_TIMEOUT) as client:
                # bleak WinRT internal: the connected BluetoothLEDevice ("requester").
                requester = client._backend._requester
                info = requester.device_information
                if info.pairing.is_paired:
                    return True
                custom = info.pairing.custom

                def handler(sender, args):
                    args.accept(pin)

                token = custom.add_pairing_requested(handler)
                try:
                    result = await custom.pair_async(DevicePairingKinds.PROVIDE_PIN)
                finally:
                    custom.remove_pairing_requested(token)

                return result.status in (
                    DevicePairingResultStatus.PAIRED,
                    DevicePairingResultStatus.ALREADY_PAIRED,
                )
        except Exception:  # noqa: BLE001
            return False

    async def bond_state(self) -> dict[str, tuple[bool, bool]]:
        """Best-effort list of already-paired BLE devices. Returns ``{}`` on any
        error — the TUI still reflects a unit as bonded right after a successful
        ``pair()``."""
        out: dict[str, tuple[bool, bool]] = {}
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEDevice
            from winrt.windows.devices.enumeration import DeviceInformation

            selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
            devices = await DeviceInformation.find_all_async(selector)
            for info in devices:
                try:
                    le = await BluetoothLEDevice.from_id_async(info.id)
                    addr = _mac_from_uint64(le.bluetooth_address)
                    out[addr] = (bool(info.pairing.is_paired), False)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            return {}
        return out


# --------------------------------------------------------------------------- #
# macOS — CoreBluetooth (implicit pairing via the OS dialog)
# --------------------------------------------------------------------------- #
class CoreBluetoothPairing(PairingBackend):
    """macOS pairing. The passkey cannot be injected — CoreBluetooth pairs
    implicitly and shows its own dialog on first access to an encrypted
    characteristic. ``pair()`` triggers that by reading the live-state blob."""

    supports_passkey_injection = False

    #: How long to wait for the encrypted read while the user types the code into
    #: the macOS system dialog (much longer than the normal read timeout).
    PAIR_READ_TIMEOUT = 90.0

    async def open(self):
        return self

    async def pair(self, address: str, passkey: int) -> bool:
        # `passkey` is unused: the user enters it into the macOS dialog. Reading an
        # encrypted characteristic triggers that dialog; success means the bond was
        # established (the OS remembers it for future connections).
        try:
            async with BleakClient(address, timeout=core.CONNECT_TIMEOUT) as client:
                await asyncio.wait_for(
                    client.read_gatt_char(core.STATE_CHAR), timeout=self.PAIR_READ_TIMEOUT
                )
            return True
        except Exception:  # noqa: BLE001
            return False


# --------------------------------------------------------------------------- #
# factory
# --------------------------------------------------------------------------- #
def get_pairing_backend() -> PairingBackend:
    """Return the pairing backend for the current platform."""
    if sys.platform.startswith("linux"):
        return BluezPairing()
    if sys.platform == "win32":
        return WinRTPairing()
    if sys.platform == "darwin":
        return CoreBluetoothPairing()
    return PairingBackend()  # unknown OS: pairing unavailable, GATT still works
