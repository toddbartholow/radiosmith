"""DM-32UV programming protocol: constants, frames and the read-only guard.

Protocol facts come from two independent sources, both checked against real
radios:

- chaffed/CHiRP-DM32UV-Support docs/PROTOCOL.md (GPL-3.0), reverse engineered
  from the vendor CPS v1.60 and the radio firmware, tested on DM32.01.01.047.
- infamy/DM32-Protocol-Spec (MIT).

Wire format summary (115200 8N1, no checksums anywhere):

    PSEARCH                -> 06 + 7-byte model id ("DP570UV")
    PASSSTA                -> 'P' write_flag read_flag   (0xA5 = password set)
    SYSINFO                -> 06
    V 00 00 00 i           -> 'V' i n + n bytes          (radio info, i = 1..16)
    V 00 00 40 0D          -> 'V' 0D 40 + 64 bytes
    G 00 00 00 00 01       -> 'S' + 5-byte header + 256 bytes
    FF FF FF FF 0C         -> (no reply)
    PROGRAM                -> 06                          (enter programming mode)
    02                     -> 8 bytes (ff x 8 on a real radio)
    06                     -> 06
    R a0 a1 a2 l0 l1       -> 'W' a0 a1 a2 l0 l1 + l bytes (24-bit LE addr, 16-bit LE len)

In programming mode the firmware waits up to 2 s for each 6-byte frame and
ends the session by itself when nothing arrives, so no end command exists.

This module deliberately has no way to build a write frame. ``check_read_only``
is applied to every frame the session sends.
"""

from __future__ import annotations

import collections
import struct
from collections.abc import Sequence

BAUD = 115200
PAGE = 0x1000
MODEL_ID = b"DP570UV"

# Known link fault on CH340 cables: received bytes sometimes have bit 7 set
# when the radio sent it clear (about 1 byte in 1000). Never the other way.
LINK_FAULT = 0x80

PSEARCH = b"PSEARCH"
PASSSTA = b"PASSSTA"
SYSINFO = b"SYSINFO"
PROGRAM = b"PROGRAM"
PRE_PROGRAM = b"\xff\xff\xff\xff\x0c"
IDENT = b"\x02"
ACK = b"\x06"
G_READ = b"G\x00\x00\x00\x00\x01"
V_INFO_BLOB = b"V\x00\x00\x40\x0d"

PASSWORD_SET = 0xA5

# V query indexes the CPS sends (12 is skipped by the CPS).
V_INDEXES = tuple(i for i in range(1, 17) if i != 12)
V_FIRMWARE = 1
V_BUILD_DATE = 3
V_DSP_VERSION = 4
V_CODEPLUG_RANGE = 10
V_CONTACTS_RANGE = 15

# Page tags (last byte of each 4 KB page in the codeplug area).
TAG_FREE = 0xFF
TAG_STALE = 0x00
TAG_CALIBRATION = 0x69
TAG_SETTINGS = 0x04
TAG_SCAN_LISTS = 0x11
TAG_CHANNELS_FIRST = 0x12
TAG_CHANNELS_LAST = 0x41
TAG_ZONES_FIRST = 0x5C
TAG_ZONES_LAST = 0x64

# Fixed command strings that are safe to send: none of them changes radio memory.
_SAFE_EXACT = frozenset({PSEARCH, PASSSTA, SYSINFO, PROGRAM, PRE_PROGRAM, IDENT, ACK, G_READ})


class ProtocolError(Exception):
    """The radio did not answer as the protocol says it should."""


class ReadOnlyViolation(Exception):
    """Code tried to send a frame that is not on the read-only allowlist."""


def check_read_only(frame: bytes) -> None:
    """Raise ReadOnlyViolation unless ``frame`` is a known read-only command.

    Allowed: the fixed handshake strings, 5-byte V queries, the G read of the
    256-byte block, and 6-byte R block reads. Everything else, in particular
    W (flash write, erases the sector), S (writes the G memory) and PASWORD,
    is refused.
    """
    if frame in _SAFE_EXACT:
        return
    if len(frame) == 5 and frame[0] == ord("V"):
        return
    if len(frame) == 6 and frame[0] == ord("R"):
        return
    raise ReadOnlyViolation(f"refusing to send non-read-only frame {frame[:8].hex(' ')}")


def read_frame(addr: int, length: int) -> bytes:
    if not 0 <= addr < 1 << 24:
        raise ValueError(f"address {addr:#x} does not fit in 24 bits")
    if not 0 < length <= 0xFFFF:
        raise ValueError(f"length {length} out of range")
    return b"R" + struct.pack("<I", addr)[:3] + struct.pack("<H", length)


def v_query(index: int) -> bytes:
    return b"V\x00\x00\x00" + bytes([index])


def marker_ok(got: int, want: int) -> bool:
    """Check a one-byte reply marker, allowing for the known link fault."""
    return got in (want, want | LINK_FAULT)


def clear_ascii(data: bytes) -> bytes:
    """Undo the bit-7 link fault in data known to be 7-bit ASCII."""
    return bytes(b & 0x7F for b in data)


def decode_ascii(data: bytes) -> str:
    return clear_ascii(data).split(b"\x00", 1)[0].decode("ascii", errors="replace").rstrip("\x7f ")


def parse_range(body: bytes) -> tuple[int, int] | None:
    """Parse a V flash range reply: two u32 LE values, start and end (inclusive)."""
    if len(body) < 8:
        return None
    start, end = struct.unpack("<II", body[:8])
    return start, end


def merge_copies(copies: Sequence[bytes], stats: collections.Counter) -> bytes | None:
    """Merge copies of the same block byte by byte, or return None if more are needed.

    Where copies differ only in bit 7, the value with bit 7 clear is taken,
    since the link fault only ever sets it. Any other disagreement needs a
    strict majority of at least 3 copies. Stats are only counted for a block
    that merges.
    """
    if len(set(copies)) == 1:
        return bytes(copies[0])
    quorum = max(3, len(copies) // 2 + 1)
    merged = bytearray(len(copies[0]))
    corrected = disagreements = 0
    for i, column in enumerate(zip(*copies, strict=True)):
        distinct = set(column)
        if len(distinct) == 1:
            merged[i] = column[0]
        elif len({b & ~LINK_FAULT for b in distinct}) == 1:
            merged[i] = column[0] & ~LINK_FAULT
            corrected += 1
        else:
            value = max(distinct, key=column.count)
            if column.count(value) < quorum:
                return None
            merged[i] = value
            disagreements += 1
    stats.update({k: n for k, n in (("bit-7 errors corrected", corrected), ("other disagreements", disagreements)) if n})
    return bytes(merged)
