"""Guarded sector writes for the RT-950 PRO.

The radio's W command writes one 128-byte block (address, then the data XORed
with the session key) and answers 06. Found on the owner's radio on
2026-10-03: a W to the first block of a 4 KB flash sector erases the whole
sector first, so a single block written alone wipes the rest of its sector. The
vendor CPS never notices because it writes everything in order.

So writes here are whole sectors only: every block of a 4 KB sector, from its
first block upwards, in one session. ``RT950WriteSession`` enforces the order,
and only sectors entirely inside channels 1-896 (0x0000-0x6FFF) can be
written, so settings, VFOs, the APRS block, the 0x7000 sector (which holds
0x7800-0x7FFF) and 0xE000-0xFFFF (calibration) can never be erased. Every
block is read back after it is written.
"""

from __future__ import annotations

import struct

from radiosmith.rt950 import protocol as p
from radiosmith.rt950.session import BLOCK_TIMEOUT, RT950Session, _is_ack

SECTOR = 0x1000
BLOCKS_PER_SECTOR = SECTOR // p.BLOCK
# Whole sectors holding nothing but channels 1-896.
WRITABLE_SECTORS = range(0x0000, 0x7000, SECTOR)


class UnsafeWrite(Exception):
    """A requested write failed a safety check; nothing was sent."""


class WriteUncertain(Exception):
    """A write frame was sent but its result is unknown: the sector may be erased or partly written."""


def sector_of(addr: int) -> int:
    return addr - addr % SECTOR


def writable(addr: int) -> bool:
    return addr % p.BLOCK == 0 and sector_of(addr) in WRITABLE_SECTORS


def write_frame(addr: int, data: bytes, key: bytes) -> bytes:
    """Build the W frame for one block, or raise UnsafeWrite."""
    if len(data) != p.BLOCK:
        raise UnsafeWrite(f"block data is {len(data)} bytes, not {p.BLOCK}")
    if not writable(addr):
        raise UnsafeWrite(f"address {addr:#06x} is not in a writable sector (writable: 0x0000-0x6FFF, channels 1-896)")
    return b"W" + struct.pack(">H", addr) + bytes([p.BLOCK]) + p.xor(data, key)


class RT950WriteSession(RT950Session):
    """A session that may also send W frames, only through ``write_sector``."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._approved: bytes | None = None
        self.frames_written = 0

    def send(self, frame: bytes) -> None:
        if frame[:1] == b"W":
            if frame != self._approved:
                raise p.ReadOnlyViolation("W frames can only be sent by write_sector")
            self._approved = None
            self.sleep(0.01)
            self.link.reset_input()
            self.link.write(frame)
            return
        super().send(frame)

    def _write_block(self, addr: int, data: bytes) -> str:
        assert self.key is not None
        frame = write_frame(addr, data, self.key)
        self.stage = f"write block {addr:04x}"
        self._approved = frame
        self.send(frame)
        self.frames_written += 1
        ack = self.recv(1, BLOCK_TIMEOUT)
        if len(ack) != 1 or not _is_ack(ack[0]):
            raise WriteUncertain(
                f"no acknowledgement for W {addr:04x} (got {ack.hex() or 'nothing'}); "
                f"sector {sector_of(addr):04x} may be erased or partly written"
            )
        return ack.hex()

    def write_sector(self, start: int, data: bytes, *, copies: int = 3) -> dict:
        """Write one whole 4 KB sector, block by block from its start, then read every block back."""
        if self.key is None:
            raise UnsafeWrite("key exchange not done")
        if start % SECTOR or start not in WRITABLE_SECTORS:
            raise UnsafeWrite(f"{start:#06x} is not the start of a writable sector")
        if len(data) != SECTOR:
            raise UnsafeWrite(f"sector data is {len(data)} bytes, not {SECTOR}")
        for i in range(BLOCKS_PER_SECTOR):
            self._write_block(start + i * p.BLOCK, data[i * p.BLOCK : (i + 1) * p.BLOCK])
        self.stage = f"verify sector {start:04x}"
        back = b"".join(self.read_block(p.read_frame(start + i * p.BLOCK), copies=copies) for i in range(BLOCKS_PER_SECTOR))
        differ = [i for i in range(SECTOR) if back[i] != data[i]]
        return {"sector": f"{start:04x}", "blocks": BLOCKS_PER_SECTOR, "verified": not differ, "bytes_differ": len(differ)}
