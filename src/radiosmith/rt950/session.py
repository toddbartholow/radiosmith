"""A read-only RT-950 PRO session: identify, key exchange and block reads."""

from __future__ import annotations

import collections
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from radiosmith.dm32.protocol import LINK_FAULT, merge_copies
from radiosmith.rt950 import protocol as p
from radiosmith.transport import Link

REPLY_TIMEOUT = 1.5
BLOCK_TIMEOUT = 1.5
PRE_SEND_DELAY = 0.01


@dataclass
class RT950Identity:
    model: str
    model_raw: str  # hex of the M reply
    firmware_info: str  # hex of the F reply; its layout is not documented

    def to_dict(self) -> dict:
        return asdict(self)


def _is_ack(b: int) -> bool:
    return b in (0x06, 0x06 | LINK_FAULT)


class RT950Session:
    def __init__(self, link: Link, *, sleep: Callable[[float], None] = time.sleep):
        self.link = link
        self.sleep = sleep
        self.stats: collections.Counter = collections.Counter()
        self.identity: RT950Identity | None = None
        self.key: bytes | None = None
        self.stage = "connected"

    def send(self, frame: bytes) -> None:
        p.check_read_only(frame)
        self.sleep(PRE_SEND_DELAY)
        self.link.reset_input()
        self.link.write(frame)

    def recv(self, n: int, timeout: float = REPLY_TIMEOUT) -> bytes:
        return self.link.read(n, timeout)

    def _expect_ack(self, what: str) -> None:
        resp = self.recv(1)
        if len(resp) != 1 or not _is_ack(resp[0]):
            raise p.ProtocolError(f"{what}: expected 06, got {resp.hex(' ') or 'no reply'}")

    def _reply(self, n: int, what: str) -> bytes:
        """Read an n-byte reply, skipping a stray leading 06 (seen over Bluetooth)."""
        resp = self.recv(n)
        while resp[:1] and _is_ack(resp[0]) and len(resp) == n:
            extra = self.recv(1, 0.2)
            if not extra:
                break
            resp = resp[1:] + extra
            self.stats["stray 06 skipped"] += 1
        if len(resp) != n:
            raise p.ProtocolError(f"{what}: expected {n} bytes, got {len(resp)}: {resp.hex(' ') or 'no reply'}")
        return resp

    def identify(self) -> RT950Identity:
        """Magic string, then the F and M info queries. Reads nothing else."""
        self.stage = "handshake"
        self.send(p.MAGIC)
        self._expect_ack("handshake")
        self.stage = "firmware info"
        self.send(p.FIRMWARE_INFO)
        info = self._reply(16, "F")
        self.stage = "model"
        self.send(p.MODEL_INFO)
        model = self._reply(12, "M")
        cleaned = bytes(b & 0x7F for b in model)
        if not cleaned.startswith(p.MODEL_PREFIX):
            raise p.ProtocolError(f"not an RT-950: model reply {model.hex(' ')}")
        self.identity = RT950Identity(model=p.decode_text(cleaned), model_raw=model.hex(), firmware_info=info.hex())
        return self.identity

    def start_reads(self) -> None:
        self.stage = "key exchange"
        self.send(p.SEND_FRAME)
        self._expect_ack("key exchange")
        self.key = p.KEYS[p.key_index(p.SEND_FRAME)]

    def _read_raw(self, frame: bytes) -> bytes | None:
        self.send(frame)
        resp = self.recv(4 + p.BLOCK, BLOCK_TIMEOUT)
        if len(resp) != 4 + p.BLOCK:
            self.stats["short replies"] += 1
            self.sleep(0.1)
            self.link.reset_input()
            return None
        if any(got not in (want, want | LINK_FAULT) for got, want in zip(resp[:4], frame, strict=True)):
            self.stats["bad headers"] += 1
            return None
        return resp[4:]

    def read_block(self, frame: bytes, *, copies: int = 3, tries: int = 10) -> bytes:
        """Read one 128-byte block several times, merge, and remove the XOR."""
        if self.key is None:
            raise p.ProtocolError("key exchange not done")
        got: list[bytes] = []
        for _ in range(tries + copies):
            raw = self._read_raw(frame)
            if raw is not None:
                got.append(raw)
            if len(got) >= copies:
                merged = merge_copies(got, self.stats)
                if merged is not None:
                    return p.xor(merged, self.key)
        raise p.ProtocolError(f"block {frame.hex(' ')}: no consistent reply after {len(got)} copies")

    def end(self) -> None:
        self.stage = "end"
        self.send(p.END)
        self.sleep(0.2)
