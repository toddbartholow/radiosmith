import json
from pathlib import Path

import pytest

from radiosmith.dm32 import codeplug, ops
from radiosmith.dm32 import protocol as p
from radiosmith.dm32.edit import AnalogChannel, EditError, add_zone_with_channels, analog_record, encode_bcd_freq, encode_ctcss
from tests.fake_dm32 import sample_pages
from tests.test_ops import Bench

PROFILE = Path(__file__).parent.parent / "profiles" / "cda-listen.json"

# Channel 10 of the owner's radio as read on 2026-10-02: plain analog, 25 kHz, high power.
FACTORY_CH10 = bytes.fromhex("4368616e6e656c2031300000000000000025114300251143048000003001000000ffffffff0000000000000000000000")


def test_bcd_encoding_matches_factory_records():
    assert encode_bcd_freq(431.125) == bytes.fromhex("00251143")
    assert encode_bcd_freq(144.0225) == bytes.fromhex("50224014")
    assert encode_bcd_freq(162.4) == bytes.fromhex("00002416")
    assert encode_bcd_freq(None) == b"\xff" * 4
    assert codeplug.decode_bcd_freq(encode_bcd_freq(443.975)) == pytest.approx(443.975)


def test_ctcss_encoding():
    assert encode_ctcss("88.5") == b"\x85\x08"
    assert encode_ctcss("127.3") == b"\x73\x12"
    assert encode_ctcss(None) == b"\xff\xff"
    assert codeplug.decode_tone(encode_ctcss("136.5")) == "136.5"
    with pytest.raises(EditError):
        encode_ctcss("abc")


def test_receive_only_record_from_factory_template():
    rec = analog_record(AnalogChannel("Spokane WX", 162.4), FACTORY_CH10)
    ch = codeplug.decode_channel(26, rec)
    assert ch.name == "Spokane WX" and ch.rx_mhz == pytest.approx(162.4) and ch.tx_mhz is None
    assert ch.forbid_tx and ch.power == "low" and ch.mode == "analog" and ch.bandwidth_khz == 25.0
    assert rec[0x18] == 0x08 and rec[0x19] == 0x80
    # everything outside name, frequencies, flags and tones is the template's
    for i in range(0x30):
        if not (i < 0x1A or 0x21 <= i < 0x25):
            assert rec[i] == FACTORY_CH10[i], hex(i)


@pytest.mark.parametrize(
    "kwargs, why",
    [
        ({"name": "", "rx_mhz": 146.98}, "name"),
        ({"name": "x" * 17, "rx_mhz": 146.98}, "name"),
        ({"name": "Café", "rx_mhz": 146.98}, "name"),
        ({"name": "Air", "rx_mhz": 121.5}, "outside"),
        ({"name": "Odd", "rx_mhz": 146.980005}, "10 Hz"),
        ({"name": "NoTx", "rx_mhz": 146.98, "tx_mhz": None, "forbid_tx": False}, "must forbid TX"),
        ({"name": "Pwr", "rx_mhz": 146.98, "power": "max"}, "power"),
        ({"name": "Bw", "rx_mhz": 146.98, "bandwidth_khz": 20}, "bandwidth"),
    ],
)
def test_channel_validation(kwargs, why):
    with pytest.raises(EditError, match=why):
        AnalogChannel(**kwargs).validate()


def test_add_zone_changes_only_intended_bytes():
    pages = sample_pages()
    # sample_pages already holds the CDA names, so use others here
    chans = [AnalogChannel("Test A", 146.52), AnalogChannel("Test B", 446.0)]
    new = add_zone_with_channels(pages, "Test Zone", chans)
    assert set(new) == {0x007000, 0x013000}
    after = dict(pages) | new
    cp = codeplug.decode(after)
    assert cp.channel_count == 7
    assert [c.name for c in cp.channels[-2:]] == ["Test A", "Test B"]
    assert all(c.forbid_tx and c.tx_mhz is None for c in cp.channels[-2:])
    assert cp.zones[-1].name == "Test Zone" and cp.zones[-1].members == [6, 7]
    assert [z.name for z in cp.zones[:2]] == ["CDA Listen", "Repeaters"]

    old_ch, new_ch = pages[0x007000], new[0x007000]
    changed = [i for i in range(p.PAGE) if old_ch[i] != new_ch[i]]
    assert changed and min(changed) >= 0 and all(i < 2 or 0x10 + 0x30 * 5 <= i < 0x10 + 0x30 * 7 for i in changed)
    old_z, new_z = pages[0x013000], new[0x013000]
    changed = [i for i in range(p.PAGE) if old_z[i] != new_z[i]]
    assert all(i == 0 or 0x10 + 0x91 * 2 <= i < 0x10 + 0x91 * 3 for i in changed)
    assert new_z[0x10 + 0x91 * 2 : 0x10 + 0x91 * 2 + 16] == b"Test Zone\x00" + b"\xff" * 6


def test_add_zone_refuses_duplicates():
    pages = sample_pages()
    with pytest.raises(EditError, match="already exists"):
        add_zone_with_channels(pages, "CDA Listen", [AnalogChannel("New", 146.52)])
    with pytest.raises(EditError, match="channels already exist"):
        add_zone_with_channels(pages, "Other", [AnalogChannel("Mica Peak", 146.98)])


def test_add_zone_needs_a_plain_analog_template():
    pages = sample_pages()
    body = bytearray(pages[0x007000])
    for n in range(1, 6):  # give every channel a tone, so none is plain
        off = 0x10 + 0x30 * (n - 1)
        body[off + 0x21 : off + 0x23] = b"\x85\x08"
    pages[0x007000] = bytes(body)
    with pytest.raises(EditError, match="template"):
        add_zone_with_channels(pages, "Z", [AnalogChannel("New", 146.52)])


def factory_like_pages() -> dict[int, bytes]:
    """sample_pages with factory-style names, so the CDA profile can be added."""
    pages = sample_pages()
    body = bytearray(pages[0x007000])
    for n, name in enumerate(["Channel 1", "Channel 2", "Channel 3", "Channel 4", "Channel 5"], 1):
        off = 0x10 + 0x30 * (n - 1)
        body[off : off + 16] = name.encode().ljust(16, b"\x00")
    pages[0x007000] = bytes(body)
    zones = bytearray(pages[0x013000])
    zones[0x10 : 0x10 + 16] = b"Zone 1".ljust(16, b"\x00")
    pages[0x013000] = bytes(zones)
    return pages


def test_profile_file_is_valid():
    profile = ops.load_profile(PROFILE)
    assert profile["zone"] == "CDA Listen"
    assert [c.name for c in profile["channel_objects"]] == ["Spokane WX", "Mica Peak", "Canfield", "Blacktail"]
    assert all(c.forbid_tx and c.tx_mhz is None for c in profile["channel_objects"])
    json.loads(PROFILE.read_text())  # plain JSON


def test_apply_profile_end_to_end(tmp_path):
    pages = factory_like_pages()
    original = dict(pages)
    bench = Bench(pages=pages)
    out, report = ops.apply_profile(bench.context(tmp_path), ops.load_profile(PROFILE), really_write=True)
    assert report["passed"], report["steps"][-1]
    added = report["preview"]["channels_added"]
    assert [c["name"] for c in added] == ["Spokane WX", "Mica Peak", "Canfield", "Blacktail"]
    assert report["preview"]["zones_added"][0]["members"] == [6, 7, 8, 9]
    assert not report["preview"]["channels_changed"] and not report["preview"]["zones_removed"]
    writes = [f for r in bench.radios for f in r.received if f[:1] == b"W" and len(f) > 6]
    assert sorted(f[1:4] for f in writes) == [b"\x00\x30\x01", b"\x00\x70\x00"]
    for addr in original:
        if addr not in (0x007000, 0x013000):
            assert pages[addr] == original[addr]
    assert [z["name"] for z in report["after"]["zones"]][-1] == "CDA Listen"


def test_apply_profile_dry_run_writes_nothing(tmp_path):
    bench = Bench(pages=factory_like_pages())
    _, report = ops.apply_profile(bench.context(tmp_path), ops.load_profile(PROFILE), really_write=False)
    assert report["steps"][-1]["step"] == "dry_run"
    assert len(report["preview"]["channels_added"]) == 4
    assert not any(f[:1] == b"W" for r in bench.radios for f in r.received)
