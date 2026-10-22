"""Read-only DM-32UV programming session.

Every frame goes through ``protocol.check_read_only`` before it is sent, and
this module has no write path at all. Reads are repeated and merged because
the usual CH340 cable corrupts about one received byte in a thousand.
"""

from __future__ import annotations

import collections
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from radiosmith.dm32 import protocol as p
from radiosmith.transport import Link

PRE_SEND_DELAY = 0.01  # the CPS waits 10 ms before each send
REPLY_TIMEOUT = 0.5  # CPS default receive timeout
BLOCK_TIMEOUT = 5.0  # CPS timeout for 4 KB transfers
PSEARCH_TRIES = 5  # the CPS retries PSEARCH 5 times


@dataclass
class RadioIdentity:
    model: str
    psearch_reply: str
    write_password_set: bool
    read_password_set: bool
    firmware: str | None = None
    build_date: str | None = None
    dsp_version: str | None = None
    codeplug_range: tuple[int, int] | None = None
    contacts_range: tuple[int, int] | None = None
    v_replies: dict[int, str] = field(default_factory=dict)
    v_info_blob: str | None = None

    def to_dict(self) -> dict:
        def rng(r):
            return None if r is None else [f"{r[0]:#08x}", f"{r[1]:#08x}"]

        return {
            "model": self.model,
            "firmware": self.firmware,
            "build_date": self.build_date,
            "dsp_version": self.dsp_version,
            "write_password_set": self.write_password_set,
            "read_password_set": self.read_password_set,
            "codeplug_range": rng(self.codeplug_range),
            "contacts_range": rng(self.contacts_range),
            "psearch_reply": self.psearch_reply,
            "v_replies": {str(k): v for k, v in self.v_replies.items()},
            "v_info_blob": self.v_info_blob,
        }


class DM32Session:
    """One connection to the radio: identify, enter programming mode, read pages."""

    def __init__(self, link: Link, *, sleep: Callable[[float], None] = time.sleep):
        self.link = link
        self.sleep = sleep
        self.stats: collections.Counter = collections.Counter()
        self.identity: RadioIdentity | None = None
        self.in_program_mode = False
        self.stage = "connected"

    # -- low level -----------------------------------------------------------

    def send(self, frame: bytes) -> None:
        p.check_read_only(frame)
        self.sleep(PRE_SEND_DELAY)
        self.link.reset_input()
        self.link.write(frame)

    def recv(self, n: int, timeout: float = REPLY_TIMEOUT) -> bytes:
        return self.link.read(n, timeout)

    def xfer(self, frame: bytes, n: int, what: str, timeout: float = REPLY_TIMEOUT) -> bytes:
        self.send(frame)
        resp = self.recv(n, timeout)
        if len(resp) != n:
            raise p.ProtocolError(f"{what}: expected {n} bytes, got {len(resp)}: {resp.hex(' ') or 'no reply'}")
        return resp

    def drain(self) -> None:
        """Let the late bytes of a bad reply arrive, then discard them."""
        self.sleep(0.1)
        self.link.reset_input()

    # -- handshake -------------------------------------------------------------

    def identify(self, *, v_agree: int = 3) -> RadioIdentity:
        """PSEARCH, PASSSTA, SYSINFO and the V info queries. Does not enter programming mode."""
        self.stage = "PSEARCH"
        resp = b""
        for _ in range(PSEARCH_TRIES):
            self.send(p.PSEARCH)
            resp = self.recv(8)
            if len(resp) == 8 and p.marker_ok(resp[0], 0x06):
                break
            self.stats["PSEARCH retries"] += 1
            self.drain()
        else:
            raise p.ProtocolError(f"no reply to PSEARCH after {PSEARCH_TRIES} tries (last: {resp.hex(' ') or 'nothing'})")
        model = p.clear_ascii(resp[1:8])
        if model != p.MODEL_ID:
            raise p.ProtocolError(f"unexpected model id {model!r}; expected {p.MODEL_ID!r}")

        self.stage = "PASSSTA"
        pw = self.xfer(p.PASSSTA, 3, "PASSSTA")
        if not p.marker_ok(pw[0], ord("P")):
            raise p.ProtocolError(f"bad PASSSTA reply {pw.hex(' ')}")

        self.stage = "SYSINFO"
        ack = self.xfer(p.SYSINFO, 1, "SYSINFO")
        if not p.marker_ok(ack[0], 0x06):
            raise p.ProtocolError(f"SYSINFO not acknowledged: {ack.hex(' ')}")

        ident = RadioIdentity(
            model=model.decode("ascii"),
            psearch_reply=resp.hex(" "),
            write_password_set=pw[1] == p.PASSWORD_SET,
            read_password_set=pw[2] == p.PASSWORD_SET,
        )

        self.stage = "V queries"
        hdr, body = self.query_v(p.V_INFO_BLOB, agree=v_agree)
        ident.v_info_blob = (hdr + body).hex(" ")
        bodies: dict[int, bytes] = {}
        for i in p.V_INDEXES:
            hdr, body = self.query_v(p.v_query(i), agree=v_agree)
            bodies[i] = body
            ident.v_replies[i] = (hdr + body).hex(" ")
        ident.firmware = p.decode_ascii(bodies[p.V_FIRMWARE])
        ident.build_date = p.decode_ascii(bodies[p.V_BUILD_DATE])
        ident.dsp_version = p.decode_ascii(bodies[p.V_DSP_VERSION])
        ident.codeplug_range = _checked_range(bodies[p.V_CODEPLUG_RANGE], p.V_CODEPLUG_RANGE)
        ident.contacts_range = _checked_range(bodies[p.V_CONTACTS_RANGE], p.V_CONTACTS_RANGE)
        self.identity = ident
        self.stage = "identified"
        return ident

    def query_v(self, frame: bytes, *, agree: int = 3, tries: int = 10) -> tuple[bytes, bytes]:
        """Repeat a V query until ``agree`` identical replies have been seen."""
        seen: collections.Counter = collections.Counter()
        for _ in range(tries):
            self.send(frame)
            hdr = self.recv(3)
            n = hdr[2] if len(hdr) == 3 else 0
            body = self.recv(n) if n else b""
            if len(hdr) != 3 or len(body) != n or not p.marker_ok(hdr[0], ord("V")):
                self.stats["bad V replies"] += 1
                self.drain()
                continue
            seen[hdr + body] += 1
            reply, count = seen.most_common(1)[0]
            if count >= agree:
                if len(seen) > 1:
                    self.stats["V replies corrected"] += 1
                return reply[:3], reply[3:]
        raise p.ProtocolError(f"no {agree} matching replies to {frame.hex(' ')}")

    def read_g_block(self) -> bytes:
        """The 256-byte block the CPS reads before entering programming mode."""
        self.stage = "G read"
        resp = self.xfer(p.G_READ, 0x106, "G")
        if not p.marker_ok(resp[0], ord("S")):
            raise p.ProtocolError(f"bad G reply header {resp[:6].hex(' ')}")
        return resp[6:]

    def enter_program_mode(self, *, read_g: bool = True) -> bytes:
        """G read, FF FF FF FF 0C, PROGRAM, 02, 06. Returns the G block.

        A CPS write session skips the G read (it sends S instead, which this
        driver never does), so ``read_g=False`` reproduces a write session's
        handshake up to its first write frame.

        After this the radio ends the session by itself once it has received
        nothing for 2 s, so callers must keep requests flowing or stop.
        """
        if self.identity is None:
            raise p.ProtocolError("identify() must succeed before entering programming mode")
        g_block = self.read_g_block() if read_g else b""
        self.stage = "PROGRAM"
        self.send(p.PRE_PROGRAM)
        ack = self.xfer(p.PROGRAM, 1, "PROGRAM")
        if not p.marker_ok(ack[0], 0x06):
            raise p.ProtocolError(f"PROGRAM not acknowledged: {ack.hex(' ')}")
        self.stage = "02"
        self.xfer(p.IDENT, 8, "02")
        self.stage = "06"
        ack = self.xfer(p.ACK, 1, "06")
        if not p.marker_ok(ack[0], 0x06):
            raise p.ProtocolError(f"06 not acknowledged: {ack.hex(' ')}")
        self.in_program_mode = True
        self.stage = "program mode"
        return g_block

    # -- memory reads ----------------------------------------------------------

    def read_copy(self, addr: int, length: int, timeout: float) -> bytes | None:
        """One R transfer; None if the reply is short or its header is wrong."""
        frame = p.read_frame(addr, length)
        self.send(frame)
        resp = self.recv(6 + length, timeout)
        if len(resp) == 6 + length and resp[:6] == b"W" + frame[1:6]:
            return resp[6:]
        self.stats["bad R replies"] += 1
        self.drain()
        return None

    def read_block(self, addr: int, length: int, *, timeout: float = BLOCK_TIMEOUT, copies: int = 3, tries: int = 10) -> bytes:
        """Read a block at least ``copies`` times and merge the copies."""
        if not self.in_program_mode:
            raise p.ProtocolError("not in programming mode")
        good: list[bytes] = []
        for _ in range(tries):
            data = self.read_copy(addr, length, timeout)
            if data is None:
                continue
            good.append(data)
            if len(good) >= copies:
                merged = p.merge_copies(good, self.stats)
                if merged is not None:
                    return merged
        raise p.ProtocolError(f"R {addr:06x}: no reliable copy after {tries} tries ({len(good)} good replies)")

    def scan_page_tags(self, *, copies: int = 3, progress: Callable[[int, int], None] | None = None) -> dict[int, int]:
        """Read the tag byte (offset 0xFFF) of every page in the codeplug area."""
        assert self.identity is not None and self.identity.codeplug_range is not None
        start, end = self.identity.codeplug_range
        addrs = list(range(start, end + 1 - 0xFFF, p.PAGE))
        tags: dict[int, int] = {}
        self.stage = "page scan"
        for i, addr in enumerate(addrs):
            tags[addr] = self.read_block(addr + 0xFFF, 1, timeout=REPLY_TIMEOUT, copies=copies)[0]
            if progress:
                progress(i + 1, len(addrs))
        return tags

    def read_page(self, addr: int, *, copies: int = 3) -> bytes:
        self.stage = f"read page {addr:06x}"
        return self.read_block(addr, p.PAGE, copies=copies)

    def end(self) -> None:
        """Stop talking. The radio leaves programming mode after 2 s of silence."""
        self.in_program_mode = False
        self.stage = "ended"


def _checked_range(body: bytes, index: int) -> tuple[int, int] | None:
    rng = p.parse_range(body)
    if rng is None:
        return None
    start, end = rng
    if (start | end) >> 24 or start > end:
        raise p.ProtocolError(f"V{index} range {start:#x} - {end:#x} is not a valid 24-bit range")
    return rng
