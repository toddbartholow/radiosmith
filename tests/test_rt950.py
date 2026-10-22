import pytest

from radiosmith import backup
from radiosmith.dm32 import ops as dm32_ops
from radiosmith.rt950 import ops
from radiosmith.rt950 import protocol as p
from radiosmith.rt950.session import RT950Session
from radiosmith.transport import PortInfo

PORT = PortInfo(device="FAKE4", description="USB-SERIAL CH340 (fake)", vid=0x1A86, pid=0x7523, manufacturer=None, hwid="")
F_REPLY = bytes.fromhex("01360174040005200200026001033004")


class FakeRT950:
    """Answers the handshake, info queries, key exchange and R/T reads; records every frame."""

    def __init__(self, image: bytes, *, flip_every: int = 0):
        self.image = image
        self.aprs = bytes(range(128))
        self.received: list[bytes] = []
        self.out = bytearray()
        self.key: bytes | None = None
        self.flip_every = flip_every
        self.reads = 0
        self.writes: list[int] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.received.append(bytes(data))
        if data == p.MAGIC or data == p.SEND_FRAME:
            if data == p.SEND_FRAME:
                self.key = p.KEYS[p.key_index(data)]
            self.out += b"\x06"
        elif data == p.FIRMWARE_INFO:
            self.out += F_REPLY
        elif data == p.MODEL_INFO:
            self.out += b"RT-950      "
        elif len(data) == 4 and data[:1] in (b"R", b"T") and self.key:
            addr = int.from_bytes(data[1:3], "big")
            plain = self.aprs if data[:1] == b"T" else self.image[addr : addr + 128]
            body = bytearray(p.xor(plain, self.key))
            self.reads += 1
            if self.flip_every and self.reads % self.flip_every == 0:
                body[5] |= 0x80 if not body[5] & 0x80 else 0
            self.out += data + bytes(body)
        elif data[:1] == b"W" and len(data) == 4 + 128 and self.key:
            addr = int.from_bytes(data[1:3], "big")
            if addr % 0x1000 == 0:  # as on the real radio: the first block of a sector erases it
                self.image[addr : addr + 0x1000] = bytes([0xFF]) * 0x1000
            self.image[addr : addr + 128] = p.xor(data[4:], self.key)
            self.writes.append(addr)
            self.out += b"\x06"

    def read(self, n: int, timeout: float) -> bytes:
        got, self.out = bytes(self.out[:n]), self.out[n:]
        return got

    def reset_input(self) -> None:
        self.out.clear()

    def close(self) -> None:
        self.closed = True


def sample_image() -> bytes:
    img = bytearray(b"\xff" * 0xD300)
    rec = bytearray(b"\xff" * 32)
    rec[0:8] = bytes.fromhex("00251343 00251343")
    rec[8:16] = bytes.fromhex("00000000 00000006")
    img[0:32] = rec
    img[0x8000:0x8008] = bytes([4, 3, 4, 9, 9, 2, 5, 0])
    return bytes(img)


def context(tmp_path, radios, image: bytearray | None = None):
    shared = image if image is not None else bytearray(sample_image())

    def factory(device, lines, log):
        radios.append(FakeRT950(shared, flip_every=7))
        return radios[-1]

    return dm32_ops.Context(port=PORT, data_dir=tmp_path, link_factory=factory, sleep=lambda _s: None, say=lambda _m: None)


def test_xor_is_its_own_inverse_and_skips_special_bytes():
    key = b"CO 7"
    data = bytes(range(256)) * 2
    assert p.xor(p.xor(data, key), key) == data
    assert p.xor(b"\x00\xff\x00\xff", key) == b"\x00\xff\x00\xff"
    assert p.xor(b"AAAA", key)[2] == ord("A")  # key byte 0x20 leaves data alone


def test_send_frame_selects_key_one():
    assert p.key_index(p.SEND_FRAME) == 1 and p.KEYS[1] == b"CO 7"


@pytest.mark.parametrize(
    "frame",
    [
        b"W\x00\x00\x80" + b"\x00" * 128,
        b"X\x00\x00\x80",
        b"UPDATE",
        b"D",
        b"\xaa\x42\x55",
        b"\xa5\x01",
        b"R\x00\x01\x80",
        b"R\x00\x00\x40",
    ],
)
def test_guard_refuses_writes_and_bootloader_commands(frame):
    with pytest.raises(p.ReadOnlyViolation):
        p.check_read_only(frame)


def test_identify_reads_model_and_firmware_info():
    radio = FakeRT950(sample_image())
    s = RT950Session(radio, sleep=lambda _s: None)
    ident = s.identify()
    assert ident.model == "RT-950" and ident.firmware_info == F_REPLY.hex()
    assert radio.received == [p.MAGIC, p.FIRMWARE_INFO, p.MODEL_INFO]


def test_identify_refuses_other_radios():
    radio = FakeRT950(sample_image())
    radio.write = lambda data: (
        radio.received.append(data),
        radio.out.extend(
            b"\x06" if data == p.MAGIC else b"UV-17PRO    "[: 16 if data == b"F" else 12].ljust(16 if data == b"F" else 12)
        ),
    )
    with pytest.raises(p.ProtocolError, match="not an RT-950"):
        RT950Session(radio, sleep=lambda _s: None).identify()


def test_read_image_makes_verifiable_backup_despite_bit7_faults(tmp_path):
    radios: list[FakeRT950] = []
    out, summary = ops.read_image(context(tmp_path, radios))
    b = backup.load_backup(out)
    assert b.ok, b.problems
    assert (out / "image.bin").read_bytes() == sample_image()
    assert (out / "aprs.bin").read_bytes() == bytes(range(128))
    assert summary["link_stats"].get("bit-7 errors corrected")
    assert b.manifest["radio_driver"] == "rt950"
    for frame in radios[0].received:
        p.check_read_only(frame)
    assert radios[0].received[-1] == p.END and radios[0].closed


def test_decode_channels():
    from radiosmith.rt950 import codeplug

    img = bytearray(b"\xff" * 0x8000)
    rec = bytearray(b"\xff" * 32)
    rec[0:8] = bytes.fromhex("0025134300251343")
    rec[8:12] = bytes.fromhex("f904 1700")  # CTCSS 127.3 Hz, DCS index 23
    rec[12:16] = bytes([0, 0, 0x02, 0b01000100])  # low power, TX inhibited, scan add, narrow
    rec[20:32] = b"Mica Peak".ljust(12, b"\xff")
    img[32:64] = rec
    (ch,) = codeplug.decode_channels(bytes(img))
    assert ch.number == 2 and ch.name == "Mica Peak" and ch.rx_mhz == 431.325
    assert ch.rx_tone == "127.3" and ch.tx_tone == "DCS#23"
    assert not ch.rx_only and ch.power == "low" and ch.bandwidth_khz == 12.5 and ch.scan_add


def test_nrbf_reads_classes_strings_and_arrays():
    import struct

    from radiosmith import nrbf

    def lps(s: str) -> bytes:
        return bytes([len(s)]) + s.encode()

    stream = (
        b"\x00" + struct.pack("<iiii", 1, -1, 1, 0)
        + b"\x0c" + struct.pack("<i", 2) + lps("Lib")
        + b"\x05" + struct.pack("<i", 1) + lps("KDH.Test") + struct.pack("<i", 3) + lps("num") + lps("text") + lps("list")
        + bytes([0, 1, 7]) + bytes([8]) + bytes([8]) + struct.pack("<i", 2)
        + struct.pack("<i", 42) + b"\x06" + struct.pack("<i", 3) + lps("hi") + b"\x09" + struct.pack("<i", 4)
        + b"\x0f" + struct.pack("<iiB", 4, 2, 8) + struct.pack("<ii", 7, 9)
        + b"\x0b"
    )  # fmt: skip
    assert nrbf.load(stream) == {"__class__": "KDH.Test", "num": 42, "text": "hi", "list": [7, 9]}
    with pytest.raises(nrbf.NrbfError):
        nrbf.load(b"\x00" + struct.pack("<iiii", 9, -1, 1, 0) + b"\x0b")


# -- writes ---------------------------------------------------------------------------


@pytest.mark.parametrize("addr", [0x0001, 0x7000, 0x7800, 0x8000, 0x9000, 0xF000])
def test_write_frame_refuses_outside_channel_sectors(addr):
    from radiosmith.rt950.write import UnsafeWrite, write_frame

    with pytest.raises(UnsafeWrite):
        write_frame(addr, b"\xff" * 128, b"CO 7")
    with pytest.raises(UnsafeWrite, match="not 128"):
        write_frame(0, b"\xff" * 127, b"CO 7")


def test_read_only_session_refuses_w():
    from radiosmith.rt950.write import write_frame

    radio = FakeRT950(bytearray(sample_image()))
    with pytest.raises(p.ReadOnlyViolation):
        RT950Session(radio, sleep=lambda _s: None).send(write_frame(0, b"\xff" * 128, b"CO 7"))
    assert radio.received == []


def test_simulator_erases_a_sector_like_the_radio():
    """What happened on the owner's radio: one block written alone wiped the rest of its sector."""
    radio = FakeRT950(bytearray(b"\x11" * 0xD300))
    s = RT950Session(radio, sleep=lambda _s: None)
    s.identify()
    s.start_reads()
    from radiosmith.rt950.write import write_frame

    radio.write(write_frame(0, b"\x22" * 128, s.key))
    assert radio.image[:128] == b"\x22" * 128 and radio.image[128:0x1000] == b"\xff" * (0x1000 - 128)
    assert radio.image[0x1000:0x1080] == b"\x11" * 128


def test_write_session_only_writes_whole_sectors():
    from radiosmith.rt950.write import RT950WriteSession, UnsafeWrite

    radio = FakeRT950(bytearray(sample_image()))
    ws = RT950WriteSession(radio, sleep=lambda _s: None)
    ws.identify()
    ws.start_reads()
    with pytest.raises(UnsafeWrite, match="start of a writable sector"):
        ws.write_sector(0x0080, b"\xff" * 0x1000)
    with pytest.raises(UnsafeWrite, match="start of a writable sector"):
        ws.write_sector(0x7000, b"\xff" * 0x1000)
    with pytest.raises(UnsafeWrite, match="not 4096"):
        ws.write_sector(0x0000, b"\xff" * 128)
    with pytest.raises(p.ReadOnlyViolation):
        ws.send(b"W\x00\x00\x80" + b"\xff" * 128)
    assert radio.writes == []


def test_channel_record_encoding_and_refusals():
    from radiosmith.dm32.edit import EditError
    from radiosmith.rt950 import codeplug
    from radiosmith.rt950.draftops import Rt950Channel, profile_channels

    rec = Rt950Channel("Mica Peak", 146.98, rx_tone="127.3", bandwidth_khz=12.5).record()
    assert rec[4:8] == b"\xff" * 4 and rec[8:10] == bytes.fromhex("f904") and rec[14] == 2 and rec[15] == 0x44
    assert rec[12:14] == b"\x00\x00" and rec[16:20] == b"\xff" * 4
    img = bytearray(b"\xff" * 0x8000)
    img[0:32] = rec
    (ch,) = codeplug.decode_channels(bytes(img))
    assert ch.name == "Mica Peak" and ch.rx_only and ch.rx_tone == "127.3" and ch.bandwidth_khz == 12.5 and ch.scan_add
    assert not ch.rx_am
    with pytest.raises(EditError, match="no TX inhibit"):
        profile_channels([{"name": "Simplex", "rx_mhz": 146.52, "tx_mhz": 146.52, "forbid_tx": True}])
    with pytest.raises(EditError, match="12 printable"):
        Rt950Channel("Thirteen chars", 146.52).record()
    with pytest.raises(EditError, match="CTCSS"):
        Rt950Channel("Dcs", 146.52, rx_tone="D023N").record()


def test_am_receive_sets_byte_15_bit_0():
    from radiosmith.rt950 import codeplug
    from radiosmith.rt950.draftops import Rt950Channel

    rec = Rt950Channel("GEG Tower", 118.3, rx_am=True).record()
    assert rec[15] == 0x05  # AM receive (bit 0) and scan add (bit 2)
    img = bytearray(b"\xff" * 0x8000)
    img[0:32] = rec
    (ch,) = codeplug.decode_channels(bytes(img))
    assert ch.rx_am and ch.rx_mhz == 118.3 and ch.rx_only


def test_noop_write_rewrites_the_whole_sector(tmp_path):
    radios: list[FakeRT950] = []
    image = bytearray(sample_image())
    image[0x200:0x220] = bytes(range(32))  # something later in the sector that must survive
    before = bytes(image)
    _, report = ops.noop_write_test(context(tmp_path, radios, image), sector=0, really_write=True)
    assert report["passed"], report["steps"]
    assert [a for r in radios for a in r.writes] == list(range(0, 0x1000, 0x80))
    assert bytes(image) == before


def test_restore_sectors_from_backup(tmp_path):
    radios: list[FakeRT950] = []
    image = bytearray(sample_image())
    image[0x40:0x60] = bytes(range(32))
    ctx = context(tmp_path, radios, image)
    good, _ = ops.read_image(ctx)
    image[0x80:0x1000] = b"\xff" * (0x1000 - 0x80)  # the damage
    _, report = ops.restore_sectors(ctx, good, [0], really_write=True)
    assert report["passed"], report["steps"]
    assert bytes(image) == (good / "image.bin").read_bytes()


def test_apply_profile_writes_the_channel_sector(tmp_path):
    from pathlib import Path

    from radiosmith.rt950 import codeplug

    profile = ops.load_profile(Path(__file__).parent.parent / "profiles" / "cda-listen.json")
    radios: list[FakeRT950] = []
    image = bytearray(sample_image())
    before = bytes(image)
    _, report = ops.apply_profile(context(tmp_path, radios, image), profile, really_write=False)
    assert report["steps"][-1]["step"] == "dry_run" and not any(r.writes for r in radios)
    assert [c["number"] for c in report["preview"]["channels_added"]] == [2, 3, 4, 5]

    _, report = ops.apply_profile(context(tmp_path, radios, image), profile, really_write=True)
    assert report["passed"], report["steps"]
    assert [a for r in radios for a in r.writes] == list(range(0, 0x1000, 0x80))
    assert bytes(image)[0x1000:] == before[0x1000:]
    chans = codeplug.decode_channels(bytes(image))
    assert [c.name for c in chans] == ["", "Spokane WX", "Mica Peak", "Canfield", "Blacktail"]
    assert all(c.rx_only and c.power == "low" for c in chans[1:])


def test_airband_profile_loads_for_the_rt950_only(tmp_path):
    from pathlib import Path

    from radiosmith.dm32 import ops as dm32_ops_
    from radiosmith.dm32.edit import EditError
    from radiosmith.rt950 import codeplug

    path = Path(__file__).parent.parent / "profiles" / "cda-airband.json"
    with pytest.raises(EditError, match="cannot receive AM"):
        dm32_ops_.load_profile(path)
    profile = ops.load_profile(path)
    radios: list[FakeRT950] = []
    image = bytearray(sample_image())
    _, report = ops.apply_profile(context(tmp_path, radios, image), profile, really_write=True, at=100)
    assert report["passed"], report["steps"]
    added = codeplug.decode_channels(bytes(image))[1:]
    assert [c.number for c in added] == list(range(100, 100 + len(profile["channels"])))
    assert all(c.rx_am and c.rx_only for c in added)


def test_write_aborts_if_radio_changed_since_backup(tmp_path):
    from radiosmith.rt950.ops import WriteAborted

    radios: list[FakeRT950] = []
    image = bytearray(sample_image())
    ctx = context(tmp_path, radios, image)
    real = ctx.link_factory
    calls = {"n": 0}

    def factory(device, lines, log):
        calls["n"] += 1
        if calls["n"] == 2:  # between the backup and the write session
            image[0x800] = 0x99
        return real(device, lines, log)

    ctx.link_factory = factory
    with pytest.raises(WriteAborted, match="differs from the backup"):
        ops.noop_write_test(ctx, sector=0, really_write=True)
    assert not any(r.writes for r in radios)
