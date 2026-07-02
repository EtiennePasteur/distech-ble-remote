from types import SimpleNamespace as NS

import pytest
from textual.widgets import DataTable, Input

import store
import tui
import distech_ble as core
from tui import PasskeyModal, RenameModal, DistechRemoteApp


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CONFIG_DIR", tmp_path / "distech-ble-remote")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "distech-ble-remote" / "devices.json")
    monkeypatch.setattr(store, "CONFIG_PATH", tmp_path / "distech-ble-remote" / "config.json")
    monkeypatch.setattr(core, "NAME_PREFIX_NARROW", "NIVA_C1")
    yield


class FakeScanner:
    def __init__(self, cb):
        self.cb = cb

    async def start(self):
        pass

    async def stop(self):
        pass


class Fake:
    def __init__(self, bonds=None):
        self.calls = []
        self._bonds = bonds or {}

    def make_scanner(self, cb):
        return FakeScanner(cb)

    async def get_bus(self):
        return object()

    async def bond_state(self, bus):
        return self._bonds

    async def pair(self, addr, passkey, bus):
        self.calls.append(("pair", addr, passkey))
        return passkey == 123456

    async def transact(self, addr, *, offset=None, fan=None):
        self.calls.append(("transact", addr, offset, fan))
        return (offset if offset is not None else -0.5, str(fan) if fan is not None else "1", 21.5)


def dev(addr, name):
    return NS(address=addr, name=name, details=None)


def adv(name, rssi):
    return NS(local_name=name, rssi=rssi)


async def _wait_bus(app, pilot):
    for _ in range(10):
        if app._bus is not None:
            return
        await pilot.pause()


async def test_ingest_and_filter():
    app = DistechRemoteApp(backend=Fake())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._on_advert(dev("AA:02", "NIVB_C2_T20"), adv("NIVB_C2_T20", -80))
        app._on_advert(dev("AA:03", "Some Speaker"), adv("Some Speaker", -60))  # ignored
        app._redraw()
        table = app.query_one(DataTable)
        assert table.row_count == 1                 # narrow zone (NIVA_C1) only
        await pilot.press("w")
        app._redraw()
        assert table.row_count == 2                 # all NIV*


async def test_select_and_staging():
    app = DistechRemoteApp(backend=Fake())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._redraw()
        await pilot.press("space")
        assert app.registry["AA:01"].selected is True
        await pilot.press("minus")
        await pilot.press("minus")
        assert app.staged_offset == -1.0
        await pilot.press("2")
        assert app.staged_fan == "2"


async def test_apply_fan_to_many():
    fake = Fake()
    app = DistechRemoteApp(backend=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._on_advert(dev("AA:02", "NIVA_C1_T02"), adv("NIVA_C1_T02", -60))
        app._redraw()
        for d in app.registry.values():
            d.selected = True
            d.bonded = True
        await pilot.press("3")
        await pilot.press("f")
        for _ in range(10):
            await pilot.pause()
        transacts = [c for c in fake.calls if c[0] == "transact"]
        assert len(transacts) == 2
        assert all(c[3] == "3" for c in transacts)


async def test_apply_skips_unbonded():
    fake = Fake()
    app = DistechRemoteApp(backend=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._on_advert(dev("AA:02", "NIVA_C1_T02"), adv("NIVA_C1_T02", -60))
        app._redraw()
        app.registry["AA:01"].selected = True
        app.registry["AA:01"].bonded = True
        app.registry["AA:02"].selected = True
        app.registry["AA:02"].bonded = False
        await pilot.press("o")
        for _ in range(10):
            await pilot.pause()
        transacts = [c for c in fake.calls if c[0] == "transact"]
        assert [c[1] for c in transacts] == ["AA:01"]
        assert app.registry["AA:02"].status == "not bonded"


async def test_pair_modal_success():
    fake = Fake()
    app = DistechRemoteApp(backend=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _wait_bus(app, pilot)
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._redraw()
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, PasskeyModal)
        app.screen.query_one("#pk", Input).value = "123456"
        await pilot.press("enter")
        for _ in range(12):
            await pilot.pause()
        d = app.registry["AA:01"]
        assert d.bonded is True, d.status
        assert ("pair", "AA:01", 123456) in fake.calls
        assert store.get_passkey(app.store, "AA:01") == 123456


async def test_pair_modal_wrong_code():
    fake = Fake()
    app = DistechRemoteApp(backend=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _wait_bus(app, pilot)
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._redraw()
        await pilot.press("p")
        await pilot.pause()
        app.screen.query_one("#pk", Input).value = "999"
        await pilot.press("enter")
        for _ in range(12):
            await pilot.pause()
        d = app.registry["AA:01"]
        assert d.bonded is False
        assert d.status == "pair failed"
        assert store.get_passkey(app.store, "AA:01") is None


async def test_display_name_keeps_unit():
    app = DistechRemoteApp(backend=Fake())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._redraw()
        d = app.registry["AA:01"]
        assert d.display_name() == "T01"
        assert d.display_name(full=True) == "NIVA_C1_T01"          # widened shows floor
        d.nickname = "Call Room"
        assert d.display_name() == "T01 - Call Room"
        assert d.display_name(full=True) == "NIVA_C1_T01 - Call Room"


async def test_apply_both_key():
    fake = Fake()
    app = DistechRemoteApp(backend=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._redraw()
        d = app.registry["AA:01"]
        d.selected = True
        d.bonded = True
        await pilot.press("2")       # stage fan 2
        await pilot.press("plus")    # stage offset +0.5
        await pilot.press("A")       # apply both
        for _ in range(10):
            await pilot.pause()
        transacts = [c for c in fake.calls if c[0] == "transact"]
        assert len(transacts) == 1
        assert transacts[0][2] == 0.5 and transacts[0][3] == "2"


async def test_rename_modal():
    app = DistechRemoteApp(backend=Fake())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._redraw()
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, RenameModal)
        app.screen.query_one("#nick", Input).value = "Etienne desk"
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert app.registry["AA:01"].nickname == "Etienne desk"
        assert store.get_nickname(app.store, "AA:01") == "Etienne desk"


async def test_read_status_key():
    fake = Fake()
    app = DistechRemoteApp(backend=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._redraw()
        d = app.registry["AA:01"]
        d.bonded = True
        await pilot.press("r")                      # read the highlighted unit
        for _ in range(10):
            await pilot.pause()
        transacts = [c for c in fake.calls if c[0] == "transact"]
        assert [c[1] for c in transacts] == ["AA:01"]
        assert transacts[0][2] is None and transacts[0][3] is None   # read-only, no write
        assert d.status == "read ✓"
        assert d.last_read is not None
        assert d.live_temp == 21.5                   # room temp captured from the read-back


async def test_read_status_requires_pairing():
    fake = Fake()
    app = DistechRemoteApp(backend=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._redraw()
        app.registry["AA:01"].bonded = False
        await pilot.press("r")
        for _ in range(6):
            await pilot.pause()
        assert [c for c in fake.calls if c[0] == "transact"] == []


def _row_order(app):
    table = app.query_one(DataTable)
    return [row.key.value for row in table.ordered_rows]


async def test_sort_is_bonded_first_then_alphabetical():
    app = DistechRemoteApp(backend=Fake())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:03", "NIVA_C1_T03"), adv("NIVA_C1_T03", -45))
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -60))
        app._on_advert(dev("AA:02", "NIVA_C1_T02"), adv("NIVA_C1_T02", -70))
        app._redraw()
        # no bonds yet: pure alphabetical by unit / nickname
        assert _row_order(app) == ["AA:01", "AA:02", "AA:03"]

        # bond state resolves after launch -> the bonded unit floats to the top
        app.registry["AA:03"].bonded = True
        app._redraw()
        assert _row_order(app) == ["AA:03", "AA:01", "AA:02"]

        # nickname is a suffix on the unit-first column value ("T01 - Zebra"),
        # so it never reorders distinct units — order stays unit-first, live.
        app.registry["AA:01"].nickname = "Zebra"
        app._redraw()
        assert _row_order(app) == ["AA:03", "AA:01", "AA:02"]


async def test_cursor_follows_device_across_resort():
    app = DistechRemoteApp(backend=Fake())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._on_advert(dev("AA:02", "NIVA_C1_T02"), adv("NIVA_C1_T02", -60))
        app._redraw()
        table = app.query_one(DataTable)
        table.move_cursor(row=1)                     # highlight T02 (AA:02)
        assert app._highlighted_address() == "AA:02"
        # AA:02 becomes bonded and jumps to the top; the cursor should ride along
        app.registry["AA:02"].bonded = True
        app._redraw()
        assert _row_order(app) == ["AA:02", "AA:01"]
        assert app._highlighted_address() == "AA:02"


async def test_read_all_reads_every_bonded_unit():
    fake = Fake()
    app = DistechRemoteApp(backend=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_advert(dev("AA:01", "NIVA_C1_T01"), adv("NIVA_C1_T01", -45))
        app._on_advert(dev("AA:02", "NIVA_C1_T02"), adv("NIVA_C1_T02", -60))
        app._on_advert(dev("AA:03", "NIVA_C1_T03"), adv("NIVA_C1_T03", -70))
        app._redraw()
        app.registry["AA:01"].bonded = True         # bonded but NOT selected
        app.registry["AA:02"].bonded = True         # bonded but NOT selected
        app.registry["AA:03"].bonded = False        # unpaired -> skipped
        await pilot.press("R")
        for _ in range(14):
            await pilot.pause()
        transacts = sorted(c[1] for c in fake.calls if c[0] == "transact")
        assert transacts == ["AA:01", "AA:02"]
        assert app.registry["AA:01"].status == "read ✓"
        assert app.registry["AA:02"].status == "read ✓"
        assert app.registry["AA:03"].status != "read ✓"
