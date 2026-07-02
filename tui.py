#!/usr/bin/env python3
"""
Distech Controls BLE Remote — a Textual terminal UI to control Distech Controls AC units over BLE.

  • live-lists nearby controllers by zone (DISTECH_ZONE env or "zone" in config.json; w widens to all NIV*)
  • tracks bonded/paired state; pairing an unbonded unit prompts for its code
  • local nicknames (persisted), multi-select, and apply offset / fan to all selected

Run:  . .venv/bin/activate && python tui.py
Everything (scan, pair, connect, write) shares Textual's asyncio loop with bleak
and dbus-fast — no threads. All radio access is serialised by one lock that pauses
the scanner during connects/pairs (the classic BlueZ connect-while-scanning race).
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from bleak import BleakClient
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

import store
import distech_ble as core

# --------------------------------------------------------------------------- #
# backend seam (real BLE by default; a fake can be injected for tests)
# --------------------------------------------------------------------------- #
class RealBackend:
    def make_scanner(self, callback):
        return core.make_scanner(callback)

    async def get_bus(self):
        return await core.get_system_bus()

    async def bond_state(self, bus):
        return await core.bonded_and_connected(bus)

    async def pair(self, address, passkey, bus):
        return await core.pair_device(address, passkey, bus=bus)

    async def transact(self, address, *, offset=None, fan=None):
        """Connect once; optionally write offset/fan; always read back (offset, fan, temp)."""
        async with BleakClient(address, timeout=core.CONNECT_TIMEOUT) as c:
            if offset is not None:
                await core.write_register(c, core.REG_OFFSET, core.clamp_offset(offset))
            if fan is not None:
                await core.set_fan(c, fan)
            state = await core.read_state(c)
            return core.offset_of(state), core.fan_of(state), core.temp_of(state)


# --------------------------------------------------------------------------- #
# device registry model
# --------------------------------------------------------------------------- #
@dataclass
class Device:
    address: str
    name: str | None = None
    unit_label: str | None = None
    nickname: str | None = None
    rssi: int = -999
    last_seen: float = 0.0
    bonded: bool = False
    connected: bool = False
    selected: bool = False
    live_offset: float | None = None
    live_fan: str | None = None
    live_temp: float | None = None
    last_read: float | None = None
    busy: bool = False
    status: str = ""

    def label(self, full: bool = False) -> str:
        """Short unit label ('Tzz') or the full advertised name ('NIVx_Cy_Tzz')."""
        if full:
            return self.name or self.unit_label or self.address
        return self.unit_label or self.name or self.address

    def display_name(self, full: bool = False) -> str:
        base = self.label(full)
        return f"{base} - {self.nickname}" if self.nickname else base

    def rssi_bar(self) -> str:
        if self.rssi <= -998:
            return "····"
        blocks = "▁▂▃▄▅▆▇█"
        # map -100..-40 dBm -> 0..7
        idx = max(0, min(7, int((self.rssi + 100) / 60 * 7)))
        return blocks[idx] * 1 + "".join("█" if i <= idx else "·" for i in range(4))

    def is_stale(self, now: float, ttl: float = 15.0) -> bool:
        return (now - self.last_seen) > ttl


# --------------------------------------------------------------------------- #
# modals
# --------------------------------------------------------------------------- #
class PasskeyModal(ModalScreen[int | None]):
    def __init__(self, name: str, default: int | None = None):
        super().__init__()
        self._name = name
        self._default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Pairing code for [b]{self._name}[/b]")
            yield Input(
                value=str(self._default) if self._default is not None else "",
                placeholder="e.g. 000000",
                restrict=r"[0-9]*",
                id="pk",
            )
            with Horizontal(id="buttons"):
                yield Button("Pair", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#pk", Input).focus()

    def _submit(self) -> None:
        v = self.query_one("#pk", Input).value.strip()
        self.dismiss(int(v) if v.isdigit() else None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._submit() if event.button.id == "ok" else self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def key_escape(self) -> None:
        self.dismiss(None)


class RenameModal(ModalScreen[str | None]):
    def __init__(self, nickname: str, context: str):
        super().__init__()
        self._nickname = nickname
        self._ctx = context

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Nickname for [b]{self._ctx}[/b] (blank to clear)")
            yield Input(value=self._nickname, placeholder="e.g. Call Room", id="nick")
            with Horizontal(id="buttons"):
                yield Button("Save", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#nick", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#nick", Input).value.strip())
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(self.query_one("#nick", Input).value.strip())

    def key_escape(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
# (label, fixed width) — fixed so cell updates never clip (e.g. "speed 3" in fan).
# temp/offset/fan come right after the name so they stay visible on narrow terminals;
# the long address is pushed to the end.
COLSPEC = [
    ("sel", 3),
    ("unit / nickname", 27),
    ("temp", 7),
    ("offset", 7),
    ("fan", 9),
    ("bond", 9),
    ("rssi", 10),
    ("conn", 4),
    ("address", 18),
    ("status", 14),
]


class DistechRemoteApp(App):
    CSS = """
    Screen { layout: horizontal; }
    #left { width: 1fr; }
    #right { width: 42; border: round $primary; padding: 1; }
    DataTable { height: 1fr; }
    #dialog { width: 48; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    #dialog Label { margin-bottom: 1; }
    #buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    ModalScreen { align: center middle; }
    #panel { height: 1fr; }
    """

    BINDINGS = [
        Binding("space", "select", "select"),
        Binding("p", "pair", "pair"),
        Binding("n", "rename", "nickname"),
        Binding("r", "read", "read status"),
        Binding("R", "read_all", "read all"),
        Binding("minus", "offset_step(-1)", "offset −"),
        Binding("plus", "offset_step(1)", "offset +"),
        Binding("equals_sign", "offset_step(1)", "offset +", show=False),
        Binding("a", "stage_fan('auto')", "fan auto"),
        Binding("0", "stage_fan('0')", "fan 0", show=False),
        Binding("1", "stage_fan('1')", "fan 1", show=False),
        Binding("2", "stage_fan('2')", "fan 2", show=False),
        Binding("3", "stage_fan('3')", "fan 3", show=False),
        Binding("o", "apply_offset", "apply offset"),
        Binding("f", "apply_fan", "apply fan"),
        Binding("A", "apply_both", "apply both"),
        Binding("w", "toggle_filter", "widen"),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, backend=None):
        super().__init__()
        self.backend = backend or RealBackend()
        self.registry: dict[str, Device] = {}
        self.store: dict[str, dict] = {}
        self._bus = None
        self._scanner = None
        self._scanning = False
        self._radio = asyncio.Lock()
        self._ble_busy = False
        self._bond_busy = False
        self._rows: set[str] = set()
        self._colkeys: list = []
        self.widened = False
        self.staged_offset = 0.0
        self.staged_fan = "auto"
        self.status_line = "scanning…"

    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="left"):
            yield DataTable(id="devices", cursor_type="row", zebra_stripes=True)
        with Vertical(id="right"):
            yield Static(id="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Distech Controls BLE Remote"
        self.store = store.load()
        table = self.query_one(DataTable)
        self._colkeys = [table.add_column(label, width=w) for label, w in COLSPEC]
        table.focus()
        self.set_interval(0.25, self._redraw)
        self.set_interval(3.0, self._schedule_bond_refresh)
        self._startup()
        self._update_panel()

    # ---- startup / scanning ----
    @work(exclusive=True, group="startup")
    async def _startup(self) -> None:
        try:
            self._bus = await self.backend.get_bus()
        except Exception:  # noqa: BLE001
            self._bus = None
            self.notify("BlueZ dbus unavailable — pairing/bond-state off", severity="warning")
        if self._bus is not None:
            await self._refresh_bonds()
        try:
            self._scanner = self.backend.make_scanner(self._on_advert)
            await self._scanner.start()
            self._scanning = True
        except Exception as e:  # noqa: BLE001
            self.notify(f"scan start failed: {e}", severity="error")

    @work(exclusive=True, group="readall")
    async def _do_read_all(self) -> None:
        """Read the current offset/fan of every bonded unit, one at a time."""
        if self._bus is not None:
            await self._refresh_bonds()
        while self._ble_busy:                     # don't fight a user action
            await asyncio.sleep(0.4)
        targets = [d for d in self.registry.values() if d.bonded]
        if not targets:
            self.notify("No paired units to read", severity="warning")
            return
        self._ble_busy = True
        sem = asyncio.Semaphore(1)      # read one unit at a time; concurrent BLE reads collide
        for d in targets:               # mark the whole batch as waiting up front
            d.status = "queued…"

        async def read_one(d: Device) -> None:
            async with sem:
                d.busy = True
                d.status = "reading…"
                try:
                    off, fan, temp = await self.backend.transact(d.address)
                    d.live_offset, d.live_fan, d.live_temp = off, fan, temp
                    d.last_read = time.monotonic()
                    d.status = "read ✓"
                except Exception:  # noqa: BLE001
                    d.status = "read failed"
                finally:
                    d.busy = False

        try:
            async with self._radio_session():
                await asyncio.gather(*(read_one(d) for d in targets))
        finally:
            self._ble_busy = False

    def _on_advert(self, device, adv) -> None:
        """BleakScanner detection callback — fires on the loop; safe to mutate registry."""
        name = core.name_of(device, adv)
        if not name or not name.upper().startswith(core.NAME_PREFIX_BROAD):
            return
        d = self.registry.get(device.address)
        if d is None:
            d = Device(address=device.address)
            self.registry[device.address] = d
        d.name = name
        d.unit_label = core.parse_unit_label(name)
        d.nickname = store.get_nickname(self.store, device.address)
        if adv.rssi is not None:
            d.rssi = adv.rssi
        d.last_seen = time.monotonic()

    async def _pause_scan(self) -> None:
        if self._scanning and self._scanner is not None:
            try:
                await self._scanner.stop()
            except Exception:  # noqa: BLE001
                pass
            self._scanning = False

    async def _resume_scan(self) -> None:
        if not self._scanning and self._scanner is not None:
            try:
                await self._scanner.start()
                self._scanning = True
            except Exception:  # noqa: BLE001
                pass

    @asynccontextmanager
    async def _radio_session(self):
        async with self._radio:
            await self._pause_scan()
            try:
                yield
            finally:
                await self._resume_scan()

    # ---- bond-state refresh ----
    def _schedule_bond_refresh(self) -> None:
        if self._bus is not None and not self._bond_busy:
            self._run_bond_refresh()

    @work(exclusive=True, group="bonds")
    async def _run_bond_refresh(self) -> None:
        await self._refresh_bonds()

    async def _refresh_bonds(self) -> None:
        if self._bus is None:
            return
        self._bond_busy = True
        try:
            bonds = await self.backend.bond_state(self._bus)
        except Exception:  # noqa: BLE001
            return
        finally:
            self._bond_busy = False
        for addr, (bonded, connected) in bonds.items():
            d = self.registry.get(addr)
            if d is not None:
                d.bonded = bonded
                if not d.busy:
                    d.connected = connected

    # ---- rendering ----
    def _passes_filter(self, d: Device) -> bool:
        name = (d.name or "").upper()
        prefix = core.NAME_PREFIX_BROAD if self.widened else core.NAME_PREFIX_NARROW
        return name.startswith(prefix.upper())

    def _redraw(self) -> None:
        now = time.monotonic()
        for addr in list(self.registry):
            d = self.registry[addr]
            if not d.bonded and not d.selected and (now - d.last_seen) > 60:
                del self.registry[addr]
        visible = [d for d in self.registry.values() if self._passes_filter(d)]
        visible.sort(key=lambda d: (d.unit_label or d.name or d.address))
        self._sync_table(visible, now)
        self._update_panel()

    def _row_cells(self, d: Device, now: float) -> list[Text]:
        # NB: cells are Text (not str) so DataTable does NOT run Rich markup / emoji
        # substitution on them — otherwise "[x]" vanishes and ":AB:" becomes 🆎.
        rssi = "····" if d.is_stale(now) else f"{d.rssi_bar()} {d.rssi}"
        # order must match COLSPEC: sel, name, temp, offset, fan, bond, rssi, conn, address, status
        return [
            Text("✔", style="bold green") if d.selected else Text("·", style="dim"),
            Text(d.display_name(full=self.widened)),
            Text("—" if d.live_temp is None else f"{d.live_temp:.1f}°"),
            Text("—" if d.live_offset is None else f"{d.live_offset:+.1f}"),
            Text(core.describe_fan(d.live_fan) if d.live_fan is not None else "—"),
            Text("✔ bonded", style="green") if d.bonded else Text("✎ pair", style="yellow"),
            Text(rssi),
            Text("●", style="cyan") if (d.connected or d.busy) else Text(""),
            Text(d.address),
            Text(d.status),
        ]

    def _sync_table(self, visible: list[Device], now: float) -> None:
        table = self.query_one(DataTable)
        wanted = {d.address for d in visible}
        for addr in list(self._rows):
            if addr not in wanted:
                try:
                    table.remove_row(addr)
                except Exception:  # noqa: BLE001
                    pass
                self._rows.discard(addr)
        for d in visible:
            cells = self._row_cells(d, now)
            if d.address in self._rows:
                for col, val in zip(self._colkeys, cells):
                    table.update_cell(d.address, col, val)
            else:
                table.add_row(*cells, key=d.address)
                self._rows.add(d.address)

    def _update_panel(self) -> None:
        sel = [d for d in self.registry.values() if d.selected]
        bar = self._offset_bar(self.staged_offset)
        fan_row = " ".join(
            (f"[reverse] {s.upper() if s=='auto' else s} [/]" if s == self.staged_fan
             else f" {s.upper() if s=='auto' else s} ")
            for s in core.FAN_ORDER
        )
        lines = [
            "[b]Control[/b]",
            "",
            f"Selected: [b]{len(sel)}[/b] unit(s)",
            "  " + (", ".join(d.display_name(full=self.widened) for d in sel) if sel else "(none)"),
            "",
            f"Offset staged: [b]{self.staged_offset:+.1f} °C[/b]   (−/+)",
            f"  {bar}",
            f"  → press [b]o[/b] to apply to selected",
            "",
            f"Fan staged:  {fan_row}   (a / 0-3)",
            f"  → press [b]f[/b] to apply to selected",
            "",
            f"[dim]{self.status_line}[/dim]",
        ]
        try:
            self.query_one("#panel", Static).update("\n".join(lines))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _offset_bar(value: float) -> str:
        # -3..+3 over 13 half-steps
        steps = int(round((value - core.OFFSET_MIN) / core.OFFSET_STEP))
        total = int(round((core.OFFSET_MAX - core.OFFSET_MIN) / core.OFFSET_STEP))
        return "".join("●" if i == steps else "─" for i in range(total + 1))

    # ---- helpers ----
    def _highlighted_address(self) -> str | None:
        if not self._rows:
            return None
        table = self.query_one(DataTable)
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None

    # ---- sync actions (no radio) ----
    def action_select(self) -> None:
        addr = self._highlighted_address()
        d = self.registry.get(addr) if addr else None
        if d:
            d.selected = not d.selected
            self._update_panel()

    def action_offset_step(self, delta: int) -> None:
        self.staged_offset = core.clamp_offset(self.staged_offset + delta * core.OFFSET_STEP)
        self._update_panel()

    def action_stage_fan(self, setting: str) -> None:
        self.staged_fan = setting
        self._update_panel()

    def action_toggle_filter(self) -> None:
        self.widened = not self.widened
        self.notify("Showing all NIV*" if self.widened else f"Showing {core.NAME_PREFIX_NARROW} only")

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    # ---- radio actions (workers) ----
    def action_pair(self) -> None:
        addr = self._highlighted_address()
        if addr:
            self._do_pair(addr)

    def action_rename(self) -> None:
        addr = self._highlighted_address()
        if addr:
            self._do_rename(addr)

    def action_read(self) -> None:
        addr = self._highlighted_address()
        if addr:
            self._do_read(addr)

    def action_read_all(self) -> None:
        self._do_read_all()

    def action_apply_offset(self) -> None:
        self._apply_to_selected(offset=self.staged_offset)

    def action_apply_fan(self) -> None:
        self._apply_to_selected(fan=self.staged_fan)

    def action_apply_both(self) -> None:
        self._apply_to_selected(offset=self.staged_offset, fan=self.staged_fan)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        addr = event.row_key.value
        if addr:
            self._do_read(addr)

    @work(exclusive=False, group="action")
    async def _do_pair(self, addr: str) -> None:
        d = self.registry.get(addr)
        if d and d.bonded:
            self.notify("Already bonded")
            return
        if self._bus is None:
            self.notify("Pairing needs BlueZ dbus (unavailable)", severity="error")
            return
        default = store.get_passkey(self.store, addr)
        passkey = await self.push_screen_wait(PasskeyModal(d.display_name() if d else addr, default))
        if passkey is None:
            return
        if self._ble_busy:
            self.notify("Busy — another action is running")
            return
        self._ble_busy = True
        if d:
            d.busy = True
            d.status = "pairing…"
        try:
            async with self._radio_session():
                ok = await self.backend.pair(addr, passkey, self._bus)
            if ok:
                store.set_passkey(self.store, addr, passkey)
                if d:
                    d.bonded = True
                    d.status = "bonded ✓"
                self.notify(f"Paired {d.display_name() if d else addr}")
                await self._refresh_bonds()
            else:
                if d:
                    d.status = "pair failed"
                self.notify("Pairing failed — check the code", severity="error")
        finally:
            self._ble_busy = False
            if d:
                d.busy = False

    @work(exclusive=False, group="action")
    async def _do_rename(self, addr: str) -> None:
        d = self.registry.get(addr)
        if not d:
            return
        context = d.name or d.address
        new = await self.push_screen_wait(RenameModal(d.nickname or "", context))
        if new is None:
            return
        d.nickname = new or None
        store.set_nickname(self.store, addr, new or None)

    @work(exclusive=False, group="action")
    async def _do_read(self, addr: str) -> None:
        d = self.registry.get(addr)
        if not d:
            return
        if not d.bonded:
            self.notify("Pair the unit first (p)", severity="warning")
            return
        if self._ble_busy:
            self.notify("Busy — another action is running")
            return
        self._ble_busy = True
        d.busy = True
        d.status = "reading…"
        try:
            async with self._radio_session():
                off, fan, temp = await self.backend.transact(addr)
            d.live_offset, d.live_fan, d.live_temp = off, fan, temp
            d.last_read = time.monotonic()
            d.status = "read ✓"
        except Exception as e:  # noqa: BLE001
            d.status = "read failed"
            self.notify(f"Read failed: {type(e).__name__}", severity="error")
        finally:
            self._ble_busy = False
            d.busy = False

    @work(exclusive=False, group="action")
    async def _apply_to_selected(self, *, offset=None, fan=None) -> None:
        targets = [d for d in self.registry.values() if d.selected]
        bonded = [d for d in targets if d.bonded]
        skipped = [d for d in targets if not d.bonded]
        for d in skipped:
            d.status = "not bonded"
        if not bonded:
            self.notify("No bonded units selected", severity="warning")
            return
        if self._ble_busy:
            self.notify("Busy — another action is running")
            return
        self._ble_busy = True
        counters = {"ok": 0, "fail": 0}
        sem = asyncio.Semaphore(3)

        async def apply_one(d: Device) -> None:
            async with sem:
                d.busy = True
                d.status = "connecting…"
                try:
                    d.status = "writing…"
                    off, fanv, temp = await self.backend.transact(d.address, offset=offset, fan=fan)
                    d.live_offset, d.live_fan, d.live_temp = off, fanv, temp
                    d.last_read = time.monotonic()
                    good = True
                    if offset is not None and (off is None or abs(off - core.clamp_offset(offset)) >= 0.05):
                        good = False
                    if fan is not None and fanv != str(fan):
                        good = False
                    d.status = "ok ✓" if good else "applied?"
                    counters["ok"] += 1
                except Exception:  # noqa: BLE001
                    d.status = "failed"
                    counters["fail"] += 1
                finally:
                    d.busy = False

        try:
            async with self._radio_session():
                await asyncio.gather(*(apply_one(d) for d in bonded))
        finally:
            self._ble_busy = False
        parts = []
        if offset is not None:
            parts.append(f"offset {core.clamp_offset(offset):+.1f}")
        if fan is not None:
            parts.append(f"fan {fan}")
        what = " + ".join(parts) or "nothing"
        self.status_line = f"{what} → {counters['ok']} ok, {counters['fail']} fail, {len(skipped)} skipped"
        self.notify(self.status_line)


if __name__ == "__main__":
    DistechRemoteApp().run()
