import pytest

from radiosmith import backup
from radiosmith.dm32 import ops
from radiosmith.dm32 import protocol as p
from radiosmith.dm32.session import DM32Session
from radiosmith.dm32.write import DM32WriteSession, UnsafeWrite, WriteUncertain, page_write_frame
from tests.fake_dm32 import FakeDM32, page, sample_pages
from tests.test_ops import Bench

RANGE = (0x001000, 0x0C8FFF)


def no_sleep(_s):
    pass


def channel_page_13() -> dict[int, bytes]:
    pages = sample_pages()
    pages[0x015000] = page(0x13)  # empty second channel page, as on the owner's radio
    return pages


# -- frame checks ------------------------------------------------------------------


def test_page_write_frame_layout():
    data = page(0x13)
    frame = page_write_frame(0x015000, data, RANGE, 0x13)
    assert frame[:6] == b"W\x00\x50\x01\x00\x10" and frame[6:] == data


@pytest.mark.parametrize(
    "addr, data, current, why",
    [
        (0x015000, page(0x13)[:-1], 0x13, "not 4096"),
        (0x015001, page(0x13), 0x13, "not 4 KB aligned"),
        (0x000000, page(0x13), 0x13, "outside the codeplug"),
        (0x0C9000, page(0x13), 0x13, "outside the codeplug"),
        (0x015000, page(0x14), 0x13, "has tag 0x13"),
        (0x015000, page(0x69), 0x69, "calibration"),
        (0x015000, page(0x02), 0x02, "calibration"),
        (0x015000, page(0x7C), 0x7C, "not a known writable"),
        (0x015000, page(0x00), 0x00, "not a known writable"),
        (0x015000, page(0xFF), 0xFF, "not a known writable"),
    ],
)
def test_unsafe_writes_refused(addr, data, current, why):
    with pytest.raises(UnsafeWrite, match=why):
        page_write_frame(addr, data, RANGE, current)


def test_read_only_session_still_refuses_w():
    radio = FakeDM32(channel_page_13())
    s = DM32Session(radio, sleep=no_sleep)
    with pytest.raises(p.ReadOnlyViolation):
        s.send(page_write_frame(0x015000, page(0x13), RANGE, 0x13))
    assert radio.received == []


def test_write_session_only_sends_w_through_write_page():
    radio = FakeDM32(channel_page_13())
    s = DM32WriteSession(radio, sleep=no_sleep)
    with pytest.raises(p.ReadOnlyViolation):
        s.send(page_write_frame(0x015000, page(0x13), RANGE, 0x13))
    with pytest.raises(p.ReadOnlyViolation):
        s.send(b"S\x00\x00\x00\x00\x01" + b"\x00" * 256)
    assert radio.received == []


def test_write_page_requires_scan_and_program_mode():
    s = DM32WriteSession(FakeDM32(channel_page_13()), sleep=no_sleep)
    s.identify()
    with pytest.raises(UnsafeWrite, match="programming mode"):
        s.write_page(0x015000, page(0x13))
    s.enter_program_mode()
    with pytest.raises(UnsafeWrite, match="scanned"):
        s.write_page(0x015000, page(0x13))


def test_write_page_writes_and_verifies():
    pages = channel_page_13()
    radio = FakeDM32(pages)
    s = DM32WriteSession(radio, sleep=no_sleep)
    s.identify()
    s.enter_program_mode()
    s.scan_page_tags()
    new = bytearray(page(0x13))
    new[0:4] = b"TEST"
    result = s.write_page(0x015000, bytes(new))
    assert result["verified"] and result["ack"] == "06"
    assert radio.writes == [0x015000] and pages[0x015000] == bytes(new)


def test_missing_ack_is_uncertain():
    s = DM32WriteSession(FakeDM32(channel_page_13(), ack_writes=False), sleep=no_sleep)
    s.identify()
    s.enter_program_mode()
    s.scan_page_tags()
    with pytest.raises(WriteUncertain, match="partly written"):
        s.write_page(0x015000, page(0x13))
    assert not s.in_program_mode


# -- the no-op write test operation ----------------------------------------------------


def w_frames(bench: Bench) -> list[bytes]:
    return [f for r in bench.radios for f in r.received if f[:1] == b"W" and len(f) > 6]


def test_noop_write_test_passes_and_writes_one_identical_page(tmp_path):
    pages = channel_page_13()
    original = dict(pages)
    bench = Bench(pages=pages)
    out, report = ops.noop_write_test(bench.context(tmp_path), tag=0x13, really_write=True)
    assert report["passed"]
    assert [s["step"] for s in report["steps"]] == [
        "backup_before",
        "plan",
        "pre_write_checks",
        "write",
        "rescan",
        "backup_after",
    ]
    frames = w_frames(bench)
    assert len(frames) == 1 and frames[0] == b"W\x00\x50\x01\x00\x10" + original[0x015000]
    assert pages == original
    assert (out / "report.json").exists() and (out / "traffic.jsonl").exists()


def test_dry_run_sends_no_write(tmp_path):
    bench = Bench(pages=channel_page_13())
    _, report = ops.noop_write_test(bench.context(tmp_path), tag=0x13, really_write=False)
    assert report["steps"][-1]["step"] == "dry_run"
    assert w_frames(bench) == []


def test_aborts_when_radio_page_differs_from_backup(tmp_path):
    pages = channel_page_13()
    bench = Bench(pages=pages)
    real_factory = bench.factory

    def factory(device, lines, log):
        radio = real_factory(device, lines, log)
        if len(bench.radios) == 2:  # the write session: someone edited the radio since the backup
            data = bytearray(pages[0x015000])
            data[0] = 0x41
            pages[0x015000] = bytes(data)
        return radio

    ctx = bench.context(tmp_path)
    ctx.link_factory = factory
    with pytest.raises(ops.WriteTestAborted, match="differs from the backup"):
        ops.noop_write_test(ctx, tag=0x13, really_write=True)
    assert w_frames(bench) == []


def test_aborts_on_unknown_or_calibration_tag(tmp_path):
    with pytest.raises(UnsafeWrite):
        ops.noop_write_test(Bench(pages=channel_page_13()).context(tmp_path), tag=0x69, really_write=True)
    with pytest.raises(ops.WriteTestAborted, match="0 pages with tag"):
        ops.noop_write_test(Bench(pages=channel_page_13()).context(tmp_path), tag=0x30, really_write=True)


def test_missing_ack_reported_and_no_after_backup(tmp_path):
    bench = Bench(pages=channel_page_13(), ack_writes=False)
    with pytest.raises(WriteUncertain):
        ops.noop_write_test(bench.context(tmp_path), tag=0x13, really_write=True)
    reports = list((tmp_path / "writes").glob("*/report.json"))
    assert len(reports) == 1 and "WriteUncertain" in reports[0].read_text()
    assert len(list((tmp_path / "backups" / "dm32").iterdir())) == 1
    assert backup.load_backup(next((tmp_path / "backups" / "dm32").iterdir())).ok
