import pytest

from radiosmith.dm32 import protocol as p
from radiosmith.dm32.session import DM32Session
from tests.fake_dm32 import FakeDM32, sample_pages


def no_sleep(_seconds):
    pass


def assert_only_read_frames(radio: FakeDM32):
    for frame in radio.received:
        p.check_read_only(frame)  # raises if anything else reached the radio


def test_identify():
    radio = FakeDM32()
    ident = DM32Session(radio, sleep=no_sleep).identify()
    assert ident.model == "DP570UV"
    assert ident.firmware == "DM32.NRF.01.049"
    assert ident.build_date == "2022-06-27"
    assert ident.dsp_version == "D1.01.01.004"
    assert ident.codeplug_range == (0x001000, 0x0C8FFF)
    assert not ident.read_password_set and not ident.write_password_set
    assert not radio.program_mode
    assert_only_read_frames(radio)


def test_identify_reports_password_flags():
    ident = DM32Session(FakeDM32(passwords=(0xA5, 0x00)), sleep=no_sleep).identify()
    assert ident.write_password_set and not ident.read_password_set


def test_silent_radio_fails_after_psearch_retries():
    radio = FakeDM32(silent=True)
    s = DM32Session(radio, sleep=no_sleep)
    with pytest.raises(p.ProtocolError, match="no reply to PSEARCH"):
        s.identify()
    assert radio.received == [p.PSEARCH] * 5
    assert s.stage == "PSEARCH"


def test_wrong_model_refused():
    with pytest.raises(p.ProtocolError, match="unexpected model"):
        DM32Session(FakeDM32(model=b"XX999ZZ"), sleep=no_sleep).identify()


def test_program_mode_and_page_reads():
    pages = sample_pages()
    radio = FakeDM32(pages)
    s = DM32Session(radio, sleep=no_sleep)
    s.identify()
    g = s.enter_program_mode()
    assert g == b"\xff" * 256 and radio.program_mode
    tags = s.scan_page_tags()
    assert len(tags) == 200
    assert {a: t for a, t in tags.items() if t != 0xFF} == {a: d[-1] for a, d in pages.items()}
    for addr, data in pages.items():
        assert s.read_page(addr) == data
    assert_only_read_frames(radio)


def test_reads_survive_bit7_link_faults():
    pages = sample_pages()
    radio = FakeDM32(pages, fault_rate=0.002, seed=7)
    s = DM32Session(radio, sleep=no_sleep)
    s.identify()
    s.enter_program_mode()
    for addr, data in pages.items():
        assert s.read_page(addr) == data
    assert radio.flips > 0
    assert s.stats["bit-7 errors corrected"] + s.stats["bad R replies"] + s.stats["V replies corrected"] > 0


def test_write_prep_skips_g_read():
    radio = FakeDM32()
    s = DM32Session(radio, sleep=no_sleep)
    s.identify()
    assert s.enter_program_mode(read_g=False) == b""
    assert p.G_READ not in radio.received
    assert radio.program_mode


def test_reads_require_program_mode():
    s = DM32Session(FakeDM32(sample_pages()), sleep=no_sleep)
    s.identify()
    with pytest.raises(p.ProtocolError, match="not in programming mode"):
        s.read_page(0x007000)


def test_guard_blocks_write_frames_before_they_reach_the_radio():
    radio = FakeDM32()
    s = DM32Session(radio, sleep=no_sleep)
    with pytest.raises(p.ReadOnlyViolation):
        s.send(b"W\x00\x70\x00\x00\x10" + b"\x00" * 0x1000)
    with pytest.raises(p.ReadOnlyViolation):
        s.send(b"S\x00\x00\x00\x00\x01" + b"\x00" * 256)
    assert radio.received == []
