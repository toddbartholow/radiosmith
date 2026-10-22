"""Guarded page writes for the DM-32UV.

The firmware's W command erases the 4 KB sector at a 4 KB-aligned address and
programs the data, with no checksum and no read-back of its own, and if the data
crosses into the next sector that sector is erased too. So the only write this
module can send is one whole page at an aligned address inside the codeplug
area, onto a page whose current tag is known, keeping that tag, and never onto
calibration (0x02, 0x69) or unidentified pages. Every write is read back.

The writable tags are the ones chaffed/CHiRP-DM32UV-Support's driver uploads
(GPL-3.0), which that author verified on a real radio.
"""

from __future__ import annotations

import struct

from radiosmith.dm32 import protocol as p
from radiosmith.dm32.session import BLOCK_TIMEOUT, DM32Session

NEVER_WRITE = frozenset({0x02, 0x69})
WRITABLE_TAGS = frozenset(
    [0x03, 0x04, 0x06, 0x0B, 0x0F, 0x11, 0x42, 0x43, 0x67]
    + list(range(0x12, 0x42))  # channels
    + list(range(0x44, 0x49))  # contact records
    + list(range(0x5C, 0x65))  # zones
)


class UnsafeWrite(Exception):
    """A requested write failed a safety check; nothing was sent."""


class WriteUncertain(Exception):
    """A write frame was sent but its result is unknown: the page may be partly written."""


def page_write_frame(addr: int, data: bytes, codeplug_range: tuple[int, int], current_tag: int) -> bytes:
    """Build the W frame for one whole page, or raise UnsafeWrite."""
    start, end = codeplug_range
    if len(data) != p.PAGE:
        raise UnsafeWrite(f"page data is {len(data)} bytes, not {p.PAGE}")
    if addr % p.PAGE:
        raise UnsafeWrite(f"address {addr:#08x} is not 4 KB aligned")
    if addr < start or addr + p.PAGE - 1 > end:
        raise UnsafeWrite(f"page {addr:#08x} is outside the codeplug area {start:#08x}-{end:#08x}")
    tag = data[-1]
    if tag != current_tag:
        raise UnsafeWrite(f"new data has tag {tag:#04x} but the page on the radio has tag {current_tag:#04x}")
    if tag in NEVER_WRITE:
        raise UnsafeWrite(f"tag {tag:#04x} is calibration data")
    if tag not in WRITABLE_TAGS:
        raise UnsafeWrite(f"tag {tag:#04x} is not a known writable page")
    return b"W" + struct.pack("<I", addr)[:3] + struct.pack("<H", p.PAGE) + bytes(data)


class DM32WriteSession(DM32Session):
    """A session that may also send W frames, but only through ``write_page``."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._approved: bytes | None = None
        self.page_tags: dict[int, int] | None = None
        self.frames_written = 0

    def send(self, frame: bytes) -> None:
        if frame and frame[0] == ord("W"):
            if frame != self._approved:
                raise p.ReadOnlyViolation("W frames can only be sent by write_page")
            self._approved = None
            self.sleep(0.01)
            self.link.reset_input()
            self.link.write(frame)
            return
        super().send(frame)

    def scan_page_tags(self, **kwargs) -> dict[int, int]:
        self.page_tags = super().scan_page_tags(**kwargs)
        return self.page_tags

    def write_page(self, addr: int, data: bytes, *, copies: int = 3) -> dict:
        """Write one page, then read it back. Raises WriteUncertain if the radio does not acknowledge."""
        if not self.in_program_mode:
            raise UnsafeWrite("not in programming mode")
        if self.identity is None or self.identity.codeplug_range is None:
            raise UnsafeWrite("radio identity unknown")
        if self.page_tags is None or addr not in self.page_tags:
            raise UnsafeWrite("page tags must be scanned in this session before writing")
        frame = page_write_frame(addr, data, self.identity.codeplug_range, self.page_tags[addr])

        self.stage = f"write page {addr:06x}"
        self._approved = frame
        self.send(frame)
        self.frames_written += 1
        ack = self.recv(1, BLOCK_TIMEOUT)
        if len(ack) != 1:
            # The radio may still be waiting for data; it ends the session after
            # 2 s. The page may be erased or partly programmed.
            self.in_program_mode = False
            raise WriteUncertain(f"no acknowledgement for W {addr:06x}; the page may be erased or partly written")
        if not p.marker_ok(ack[0], 0x06):
            self.in_program_mode = False
            raise WriteUncertain(f"unexpected reply {ack.hex()} to W {addr:06x}")

        self.stage = f"verify page {addr:06x}"
        readback = self.read_block(addr, p.PAGE, copies=copies)
        differ = [i for i in range(p.PAGE) if readback[i] != data[i]]
        return {
            "address": f"{addr:06x}",
            "tag": f"{data[-1]:02x}",
            "ack": ack.hex(),
            "verified": not differ,
            "bytes_differ": len(differ),
            "first_offsets": [f"{i:03x}" for i in differ[:16]],
        }
