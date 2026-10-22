"""Decode the parts of a DM-32UV codeplug needed to check a read.

The codeplug is a set of tagged 4 KB pages scattered over the codeplug area;
the last byte of each page says what it holds. Layouts are from
chaffed/CHiRP-DM32UV-Support docs/PROTOCOL.md, where each field is marked with
how it was confirmed. This module only decodes; it never builds pages.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from radiosmith.dm32 import protocol as p

CHANNEL_SIZE = 0x30
CHANNEL_HEADER = 0x10
CHANNELS_ON_FIRST_PAGE = 84
CHANNELS_PER_PAGE = 85
MAX_CHANNELS = 4000

ZONE_SIZE = 0x91
ZONE_HEADER = 0x10
ZONES_PER_PAGE = 28
ZONE_MEMBERS = 64

SCAN_LIST_SIZE = 0x39
SCAN_LIST_MEMBERS = 16

CHANNEL_TYPES = {0: "analog", 1: "digital", 2: "fixed analog", 3: "fixed digital"}
POWER_LEVELS = {0: "low", 1: "middle", 2: "high"}


@dataclass
class Channel:
    number: int
    name: str
    rx_mhz: float | None
    tx_mhz: float | None
    mode: str
    forbid_tx: bool
    power: str
    bandwidth_khz: float
    scan_list: int
    rx_tone: str | None
    tx_tone: str | None
    color_code: int | None = None
    time_slot: int | None = None


@dataclass
class Zone:
    number: int
    name: str
    members: list[int]


@dataclass
class ScanList:
    number: int
    name: str
    members: list[int]


@dataclass
class Codeplug:
    channels: list[Channel] = field(default_factory=list)
    channel_count: int = 0
    zones: list[Zone] = field(default_factory=list)
    scan_lists: list[ScanList] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def pages_by_tag(pages: dict[int, bytes]) -> tuple[dict[int, bytes], list[str]]:
    """Map tag -> page data. Stale (0x00) and free (0xFF) pages are left out."""
    by_tag: dict[int, bytes] = {}
    where: dict[int, int] = {}
    problems: list[str] = []
    for addr in sorted(pages):
        data = pages[addr]
        tag = data[-1]
        if tag in (p.TAG_STALE, p.TAG_FREE):
            continue
        if tag in by_tag:
            problems.append(f"tag {tag:#04x} on pages {where[tag]:#08x} and {addr:#08x}; using the first")
            continue
        by_tag[tag] = data
        where[tag] = addr
    return by_tag, problems


def decode_bcd_freq(raw: bytes) -> float | None:
    """8-digit BCD, little-endian, in 10 Hz units. ff x 4 = empty."""
    if raw == b"\xff" * 4:
        return None
    digits = "".join(f"{b:02x}" for b in reversed(raw))
    if not digits.isdigit():
        return None
    return int(digits) * 10 / 1_000_000


def decode_tone(raw: bytes) -> str | None:
    """CTCSS as tenths of Hz in 4 BCD digits LE; DCS with flags in the second byte."""
    lo, hi = raw[0], raw[1]
    if lo == 0xFF and hi == 0xFF:
        return None
    if hi & 0x80:
        polarity = "I" if hi & 0x40 else "N"
        return f"D{hi & 0x07}{lo >> 4}{lo & 0x0F}{polarity}"
    digits = f"{hi:02x}{lo:02x}"
    if not digits.isdigit():
        return f"?{raw.hex()}"
    return f"{int(digits) / 10:.1f}"


def _text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].split(b"\xff", 1)[0].decode("ascii", errors="replace")


def channel_location(n: int) -> tuple[int, int]:
    """(page index within the channel pages, offset in page) for channel n (1-based)."""
    if not 1 <= n <= MAX_CHANNELS:
        raise ValueError(f"channel {n} out of range")
    if n <= CHANNELS_ON_FIRST_PAGE:
        return 0, CHANNEL_HEADER + CHANNEL_SIZE * (n - 1)
    return n // CHANNELS_PER_PAGE, CHANNEL_SIZE * (n % CHANNELS_PER_PAGE)


def decode_channel(n: int, rec: bytes) -> Channel | None:
    if rec[:0x18] == b"\xff" * 0x18:
        return None
    b18, b19 = rec[0x18], rec[0x19]
    mode_code = b18 >> 4
    mode = CHANNEL_TYPES.get(mode_code, f"unknown({mode_code})")
    digital = mode_code in (1, 3)
    return Channel(
        number=n,
        name=_text(rec[0:16]),
        rx_mhz=decode_bcd_freq(rec[0x10:0x14]),
        tx_mhz=decode_bcd_freq(rec[0x14:0x18]),
        mode=mode,
        forbid_tx=bool(b18 & 0x08),
        power=POWER_LEVELS.get((b18 >> 1) & 0x03, "unknown"),
        bandwidth_khz=25.0 if b19 & 0x80 else 12.5,
        scan_list=b19 & 0x3F,
        rx_tone=None if digital else decode_tone(rec[0x21:0x23]),
        tx_tone=None if digital else decode_tone(rec[0x23:0x25]),
        color_code=rec[0x1D] & 0x0F if digital else None,
        time_slot=((rec[0x1D] >> 4) & 1) + 1 if digital else None,
    )


def decode(pages: dict[int, bytes]) -> Codeplug:
    """Decode channels, zones and scan lists from {page address: 4 KB page}."""
    by_tag, problems = pages_by_tag(pages)
    cp = Codeplug(problems=problems)

    first = by_tag.get(p.TAG_CHANNELS_FIRST)
    if first is None:
        cp.problems.append("no channel page (tag 0x12)")
    else:
        cp.channel_count = struct.unpack_from("<H", first, 0)[0]
        if cp.channel_count > MAX_CHANNELS:
            cp.problems.append(f"channel count {cp.channel_count} exceeds {MAX_CHANNELS}")
            cp.channel_count = 0
        for n in range(1, cp.channel_count + 1):
            page_index, off = channel_location(n)
            page = by_tag.get(p.TAG_CHANNELS_FIRST + page_index)
            if page is None:
                cp.problems.append(f"channel {n}: page tag {p.TAG_CHANNELS_FIRST + page_index:#04x} missing")
                continue
            ch = decode_channel(n, page[off : off + CHANNEL_SIZE])
            if ch is not None:
                cp.channels.append(ch)

    zone_page0 = by_tag.get(p.TAG_ZONES_FIRST)
    if zone_page0 is None:
        cp.problems.append("no zone page (tag 0x5c)")
    else:
        count = zone_page0[0]
        for z in range(1, count + 1):
            page_index, slot = divmod(z - 1, ZONES_PER_PAGE)
            page = by_tag.get(p.TAG_ZONES_FIRST + page_index)
            if page is None:
                cp.problems.append(f"zone {z}: page tag {p.TAG_ZONES_FIRST + page_index:#04x} missing")
                continue
            off = ZONE_SIZE * slot + (ZONE_HEADER if page_index == 0 else 0)
            rec = page[off : off + ZONE_SIZE]
            n_members = min(rec[0x10], ZONE_MEMBERS)
            members = list(struct.unpack_from(f"<{n_members}H", rec, 0x11))
            cp.zones.append(Zone(number=z, name=_text(rec[0:16]), members=members))

    scan = by_tag.get(p.TAG_SCAN_LISTS)
    if scan is None:
        cp.problems.append("no scan list page (tag 0x11)")
    else:
        count = scan[0]
        for n in range(1, min(count, 32) + 1):
            off = SCAN_LIST_SIZE * n - 0x38
            rec = scan[off : off + SCAN_LIST_SIZE]
            n_members = min(rec[0x0B], SCAN_LIST_MEMBERS)
            members = list(struct.unpack_from(f"<{n_members}H", rec, 0x18))
            cp.scan_lists.append(ScanList(number=n, name=_text(rec[0:11]), members=members))

    return cp
