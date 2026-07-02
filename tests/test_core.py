import struct

import distech_ble as core


def test_golden_frames():
    # verified against NOTES.md / the real T17 unit
    assert core.build_frame(0x0E, -0.5).hex(" ") == "00 02 0e 00 00 00 00 bf"
    assert core.build_frame(0x0E, 1.0).hex(" ") == "00 02 0e 00 00 00 80 3f"
    assert core.build_frame(0x12, 1.0).hex(" ") == "00 02 12 00 00 00 80 3f"   # fan Auto
    assert core.build_frame(0x12, 5.0).hex(" ") == "00 02 12 00 00 00 a0 40"   # fan speed 3


def test_register_decode_and_offset():
    blob = bytearray(82)
    struct.pack_into("<f", blob, 14, -0.5)
    struct.pack_into("<f", blob, 18, 3.0)
    assert abs(core.read_register(bytes(blob), 14) - (-0.5)) < 1e-6
    assert abs(core.offset_of(bytes(blob)) - (-0.5)) < 1e-6
    assert core.fan_of(bytes(blob)) == "1"          # 3.0 -> speed 1


def test_fan_map_and_nan():
    for setting, value in core.FAN_SETTING_TO_VALUE.items():
        blob = bytearray(82)
        struct.pack_into("<f", blob, 18, value)
        assert core.fan_of(bytes(blob)) == setting
    blob = bytearray(82)
    struct.pack_into("<f", blob, 18, float("nan"))
    assert core.fan_of(bytes(blob)) is None


def test_clamp_offset():
    assert core.clamp_offset(2.3) == 2.5
    assert core.clamp_offset(-2.24) == -2.0
    assert core.clamp_offset(9) == 3.0
    assert core.clamp_offset(-9) == -3.0


def test_parsers():
    assert core.parse_unit_label("NIVA_C1_T01") == "T01"
    assert core.parse_unit_label(None) is None
    assert core.parse_paired_devices(
        "Device AA:BB:CC:DD:EE:01 NIVA_C1_T01\nDevice AA:BB:CC:DD:EE:02 NIVB\n(garbage)"
    ) == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}


def test_describe_fan():
    assert core.describe_fan("auto") == "Auto"
    assert core.describe_fan("2") == "speed 2"
    assert core.describe_fan(None) == "—"


class FakeClient:
    """Records write_gatt_char calls; first write-without-response raises (like our char)."""

    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, char, data, response=False):
        self.writes.append((char, bytes(data), response))
        if response is False:
            raise RuntimeError("char does not support write-without-response")


async def test_write_register_fallback():
    c = FakeClient()
    await core.write_register(c, core.REG_OFFSET, 1.0)
    # first attempt responseless (raised), then a with-response retry with identical bytes
    assert [w[2] for w in c.writes] == [False, True]
    assert c.writes[0][1] == c.writes[1][1] == core.build_frame(core.REG_OFFSET, 1.0)
    assert c.writes[1][0] == core.WRITE_CHAR
