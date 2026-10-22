import collections

import pytest

from radiosmith.dm32 import protocol as p


@pytest.mark.parametrize(
    "frame",
    [
        p.PSEARCH,
        p.PASSSTA,
        p.SYSINFO,
        p.PROGRAM,
        p.PRE_PROGRAM,
        p.IDENT,
        p.ACK,
        p.G_READ,
        p.V_INFO_BLOB,
        p.v_query(10),
        p.read_frame(0x1000, 0x1000),
    ],
)
def test_read_only_frames_allowed(frame):
    p.check_read_only(frame)


@pytest.mark.parametrize(
    "frame",
    [
        b"W\x00\x10\x00\x00\x10" + b"\x00" * 0x1000,  # flash write
        b"W\x00\x10\x00\x00\x10",  # write header alone
        b"S\x00\x00\x00\x00\x01" + b"\x00" * 256,  # G-memory write
        b"PASWORD",
        b"PCTESTM",
        b"D\x00\x00\x00\x00\x02",
        b"R\x00\x10\x00\x00\x10\x00",  # R with trailing byte
        b"",
    ],
)
def test_other_frames_refused(frame):
    with pytest.raises(p.ReadOnlyViolation):
        p.check_read_only(frame)


def test_read_frame_encoding():
    assert p.read_frame(0x015000, 0x1000) == b"R\x00\x50\x01\x00\x10"
    assert p.read_frame(0x001FFF, 1) == b"R\xff\x1f\x00\x01\x00"
    with pytest.raises(ValueError):
        p.read_frame(1 << 24, 1)
    with pytest.raises(ValueError):
        p.read_frame(0, 0)


def test_marker_and_ascii_tolerate_bit7_fault():
    assert p.marker_ok(0x86, 0x06)
    assert not p.marker_ok(0x07, 0x06)
    assert p.clear_ascii(b"D\xd0570UV") == b"DP570UV"
    assert p.decode_ascii(b"DM32.NRF.01.049\x00\x00") == "DM32.NRF.01.049"


def test_merge_identical_copies():
    stats = collections.Counter()
    assert p.merge_copies([b"abc", b"abc", b"abc"], stats) == b"abc"
    assert not stats


def test_merge_corrects_bit7_flips():
    stats = collections.Counter()
    good = bytes([0x10, 0x20, 0x30])
    merged = p.merge_copies([good, bytes([0x90, 0x20, 0x30]), bytes([0x10, 0x20, 0xB0])], stats)
    assert merged == good
    assert stats["bit-7 errors corrected"] == 2


def test_merge_needs_majority_for_other_disagreements():
    stats = collections.Counter()
    assert p.merge_copies([b"\x01", b"\x02", b"\x03"], stats) is None
    assert p.merge_copies([b"\x01", b"\x01", b"\x01", b"\x02"], stats) == b"\x01"
    # two of three agreeing is not enough: at least three equal copies are required
    assert p.merge_copies([b"\x01", b"\x01", b"\x02"], collections.Counter()) is None


def test_parse_range():
    assert p.parse_range(bytes.fromhex("00100000ff8f0c00")) == (0x1000, 0x0C8FFF)
    assert p.parse_range(b"\x00") is None
