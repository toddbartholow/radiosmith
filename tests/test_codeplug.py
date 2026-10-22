import pytest

from radiosmith.dm32 import codeplug
from tests.fake_dm32 import CDA_LISTEN, bcd_freq, page, sample_pages


def test_bcd_frequency():
    assert bcd_freq(146.98) == bytes([0x00, 0x80, 0x69, 0x14])
    assert codeplug.decode_bcd_freq(bytes([0x00, 0x80, 0x69, 0x14])) == pytest.approx(146.98)
    assert codeplug.decode_bcd_freq(bcd_freq(443.975)) == pytest.approx(443.975)
    assert codeplug.decode_bcd_freq(b"\xff" * 4) is None
    assert codeplug.decode_bcd_freq(b"\x0a\x00\x00\x00") is None


@pytest.mark.parametrize(
    "raw, tone",
    [
        (b"\x85\x08", "88.5"),  # CTCSS, from the protocol notes
        (b"\x23\x80", "D023N"),  # DCS normal
        (b"\x54\xc7", "D754I"),  # DCS inverted
        (b"\xff\xff", None),
    ],
)
def test_tones(raw, tone):
    assert codeplug.decode_tone(raw) == tone


@pytest.mark.parametrize(
    "n, loc",
    [(1, (0, 0x10)), (84, (0, 0x10 + 0x30 * 83)), (85, (1, 0)), (169, (1, 0xFC0)), (170, (2, 0)), (4000, (47, 0x30 * 5))],
)
def test_channel_location(n, loc):
    assert codeplug.channel_location(n) == loc


def test_decode_sample():
    cp = codeplug.decode(sample_pages())
    assert cp.problems == []
    assert cp.channel_count == 5
    assert [c.name for c in cp.channels[:4]] == [n for n, _ in CDA_LISTEN]
    first = cp.channels[0]
    assert first.rx_mhz == pytest.approx(162.4)
    assert first.tx_mhz == pytest.approx(162.4)
    assert first.forbid_tx and first.mode == "analog" and first.bandwidth_khz == 25.0 and first.power == "high"
    rpt = cp.channels[4]
    assert not rpt.forbid_tx
    assert rpt.tx_mhz == pytest.approx(146.38)
    assert rpt.rx_tone == "100.0"
    assert [(z.name, z.members) for z in cp.zones] == [("CDA Listen", [1, 2, 3, 4]), ("Repeaters", [5])]
    assert [(s.name, s.members) for s in cp.scan_lists] == [("Listen", [1, 2, 3, 4])]


def test_duplicate_tags_reported_and_missing_pages_reported():
    pages = sample_pages()
    pages[0x0A0000] = page(0x12, b"\x00\x00")
    del pages[0x013000]
    cp = codeplug.decode(pages)
    assert any("tag 0x12 on pages" in prob for prob in cp.problems)
    assert any("no zone page" in prob for prob in cp.problems)
