"""A simulated DM-32UV for tests, following the firmware behaviour in the protocol notes.

It answers the handshake, V and G queries, programming mode and R reads from a
sparse page store, and records every frame it receives so tests can assert
that nothing but read-only frames ever reached it. It can inject the CH340
bit-7 receive fault and go silent after a number of frames.
"""

from __future__ import annotations

import random
import struct

from radiosmith.dm32 import protocol as p

PAGE = 0x1000
CP_START, CP_END = 0x001000, 0x0C8FFF
ERASED = bytes([0xFF]) * PAGE


class FakeDM32:
    def __init__(
        self,
        pages: dict[int, bytes] | None = None,
        *,
        model: bytes = b"DP570UV",
        firmware: bytes = b"DM32.NRF.01.049",
        fault_rate: float = 0.0,
        seed: int = 1,
        silent: bool = False,
        silent_after: int | None = None,
        passwords: tuple[int, int] = (0x00, 0x00),
        ack_writes: bool = True,
    ):
        # Not copied: a test bench shares one page store between sessions so writes persist.
        self.pages = pages if pages is not None else {}
        self.model = model
        self.firmware = firmware
        self.fault_rate = fault_rate
        self.rng = random.Random(seed)
        self.silent = silent
        self.silent_after = silent_after
        self.passwords = passwords
        self.ack_writes = ack_writes
        self.writes: list[int] = []
        self.received: list[bytes] = []
        self.out = bytearray()
        self.program_mode = False
        self.closed = False
        self.flips = 0

    # -- Link interface ---------------------------------------------------------

    def write(self, data: bytes) -> None:
        self.received.append(bytes(data))
        if self.silent or (self.silent_after is not None and len(self.received) > self.silent_after):
            return
        self.out += self._reply(bytes(data))

    def read(self, n: int, timeout: float) -> bytes:
        chunk = bytes(self.out[:n])
        del self.out[:n]
        if self.fault_rate:
            noisy = bytearray(chunk)
            for i, b in enumerate(noisy):
                if not b & 0x80 and self.rng.random() < self.fault_rate:
                    noisy[i] = b | 0x80
                    self.flips += 1
            chunk = bytes(noisy)
        return chunk

    def reset_input(self) -> None:
        self.out.clear()

    def close(self) -> None:
        self.closed = True

    # -- radio behaviour ----------------------------------------------------------

    def memory(self, addr: int, length: int) -> bytes:
        out = bytearray()
        for a in range(addr, addr + length):
            page = self.pages.get(a & ~0xFFF)
            out.append(page[a & 0xFFF] if page else 0xFF)
        return bytes(out)

    def v_body(self, i: int) -> bytes:
        return {
            1: self.firmware,
            2: bytes.fromhex("000000000000 09b6 0000 09b6".replace(" ", "")),
            3: b"2022-06-27",
            4: b"D1.01.01.004",
            5: b"R1.00.01.001",
            10: struct.pack("<II", CP_START, CP_END),
            11: b"C1.00.01.001",
            15: struct.pack("<II", 0x278000, 0x6DBFFF),
            16: b"\x50\xc3\x00",
        }.get(i, b"")

    def _reply(self, f: bytes) -> bytes:
        if self.program_mode:
            if f == p.IDENT:
                return b"\xff" * 8
            if f == p.ACK:
                return b"\x06"
            if len(f) == 6 and f[0] == ord("R"):
                addr = int.from_bytes(f[1:4], "little")
                length = int.from_bytes(f[4:6], "little")
                return b"W" + f[1:6] + self.memory(addr, length)
            if f[0] == ord("W") and len(f) >= 6:
                addr = int.from_bytes(f[1:4], "little")
                length = int.from_bytes(f[4:6], "little")
                data = f[6:]
                if len(data) != length:
                    return b""  # firmware waits for the rest, then gives up
                if addr % PAGE == 0:
                    self.pages[addr] = ERASED  # erase on an aligned address
                page = bytearray(self.pages.get(addr & ~0xFFF, ERASED))
                page[addr & 0xFFF : (addr & 0xFFF) + length] = data
                self.pages[addr & ~0xFFF] = bytes(page)
                self.writes.append(addr)
                return p.ACK if self.ack_writes else b""
            return b""
        if f == p.PSEARCH:
            return b"\x06" + self.model
        if f == p.PASSSTA:
            return b"P" + bytes(self.passwords)
        if f == p.SYSINFO:
            return b"\x06"
        if f == p.V_INFO_BLOB:
            return b"V\x0d\x40" + b"\x03\x4e\x2d" + b"\x00" * 61
        if len(f) == 5 and f[0] == ord("V"):
            body = self.v_body(f[4])
            return b"V" + bytes([f[4], len(body)]) + body
        if f == p.G_READ:
            return b"S\x00\x00\x00\x00\x01" + b"\xff" * 256
        if f == p.PROGRAM:
            self.program_mode = True
            return b"\x06"
        return b""


# -- building synthetic codeplug pages (tests only; the driver has no encoder) ----


def bcd_freq(mhz: float) -> bytes:
    digits = f"{round(mhz * 100_000):08d}"
    return bytes(int(digits[i : i + 2], 16) for i in range(0, 8, 2))[::-1]


def channel_record(
    name: str,
    rx: float,
    tx: float,
    *,
    mode: int = 0,
    forbid_tx: bool = False,
    power: int = 2,
    wide: bool = True,
    scan_list: int = 0,
    rx_tone: bytes = b"\xff\xff",
    tx_tone: bytes = b"\xff\xff",
) -> bytes:
    rec = bytearray(0x30)
    rec[0:16] = name.encode("ascii").ljust(16, b"\x00")
    rec[0x10:0x14] = bcd_freq(rx)
    rec[0x14:0x18] = bcd_freq(tx)
    rec[0x18] = (mode << 4) | (0x08 if forbid_tx else 0) | (power << 1)
    rec[0x19] = (0x80 if wide else 0) | scan_list
    rec[0x1C] = 0x30
    rec[0x21:0x23] = rx_tone
    rec[0x23:0x25] = tx_tone
    return bytes(rec)


def page(tag: int, body: bytes = b"", fill: int = 0xFF) -> bytes:
    data = bytearray([fill] * PAGE)
    data[: len(body)] = body
    data[-1] = tag
    return bytes(data)


CDA_LISTEN = [
    ("Spokane WX", 162.400),
    ("Mica Peak", 146.980),
    ("Canfield", 443.975),
    ("Blacktail", 146.780),
]


def sample_pages() -> dict[int, bytes]:
    """A small codeplug: 5 channels, 2 zones, 1 scan list, plus stale and calibration pages."""
    channels = [channel_record(n, f, f, forbid_tx=True) for n, f in CDA_LISTEN]
    channels.append(channel_record("Repeater", 146.980, 146.380, forbid_tx=False, rx_tone=b"\x00\x10", tx_tone=b"\x00\x10"))
    ch_body = struct.pack("<H", len(channels)) + b"\x00" * 14 + b"".join(channels)

    def zone(name: str, members: list[int]) -> bytes:
        rec = bytearray(0x91)
        rec[0:16] = name.encode().ljust(16, b"\x00")
        rec[0x10] = len(members)
        rec[0x11 : 0x11 + 2 * len(members)] = struct.pack(f"<{len(members)}H", *members)
        return bytes(rec)

    zone_body = bytes([2, 1, 0, 1, 0, 1, 0, 1]) + b"\x00" * 8 + zone("CDA Listen", [1, 2, 3, 4]) + zone("Repeaters", [5])

    scan = bytearray(b"\x00" * 0x39 + b"\x00")
    scan[0] = 1
    rec = bytearray(0x39)
    rec[0:11] = b"Listen".ljust(11, b"\x00")
    rec[0x0B] = 4
    rec[0x18:0x20] = struct.pack("<4H", 1, 2, 3, 4)
    scan[1 : 1 + 0x39] = rec

    return {
        0x001000: page(0x00, b"stale copy"),
        0x007000: page(0x12, ch_body),
        0x013000: page(0x5C, zone_body),
        0x02A000: page(0x11, bytes(scan)),
        0x050000: page(0x69, b"RxVL RxVM calibration"),
        0x0C8000: page(0x04, b"\x01Welcome"),
    }
