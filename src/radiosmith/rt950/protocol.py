"""Radtel RT-950 PRO programming protocol: constants, frames, XOR and the read-only guard.

Facts come from these sources, used as references only (no code copied):

- Hertzz58/Radtel-RT950-Pro-Firmware tools/README.md (GPL-3.0), reverse
  engineered from the vendor CPS v1.3.0, and the vendor's own protocol config
  in that repository (binary/rt950_server_config.json).
- NathanBarguss/Chirp_Radtel-RT-950-Pro notes (no license; facts only), used
  on real radios with CPS v1.1.0 layouts.
- nivingoonesekera/Radtel-RT-950Pro-BLE-bridge-for-CHIRP-and-CPS (MIT).
- The same protocol family as CHIRP's Baofeng UV-17Pro driver.

Wire format summary (115200 8N1, no checksums):

    "PROGRAMBT9000U"         -> 06
    F                        -> 16 bytes (firmware info; not decoded)
    M                        -> 12 bytes ("RT-950" padded; same on the PRO)
    "SEND" + selector ...    -> 06    (25 bytes; picks the XOR key)
    R ah al 80               -> R ah al 80 + 128 bytes XORed with the key
    T 00 00 80               -> T 00 00 80 + 128 bytes (the APRS block)
    E                        -> (ends the session)

Never sent by this project: W and X (block writes), "UPDATE" (resets into the
bootloader), D (boot picture upload, erases flash), and AA- or A5-framed
packets (bootloader and picture protocols).
"""

from __future__ import annotations

import struct

BAUD = 115200
BLOCK = 0x80

MAGIC = b"PROGRAMBT9000U"
FIRMWARE_INFO = b"F"
MODEL_INFO = b"M"
END = b"E"
ACK = b"\x06"
MODEL_PREFIX = b"RT-950"

# A key-exchange frame that has been used on real radios (CHIRP's UV-17Pro
# frame). Selector 0x21 points at the frame byte holding key index 1.
SEND_FRAME = bytes.fromhex("53454e44 21 050d0101010411 08050d0d01110f0912091004 00".replace(" ", ""))

KEYS = [
    b"BHT ", b"CO 7", b"A ES", b" EIY", b"M PQ", b"XN Y", b"RVB ", b" HQP", b"W RC", b"MS N",
    b" SAT", b"K DH", b"ZO R", b"C SL", b"6RB ", b" JCG", b"PN V", b"J PK", b"EK L", b"I LZ",
]  # fmt: skip

# Areas read for a backup: channels and the VFO/settings/extended areas. Both
# CPS v1.1.0 and v1.3.0 layouts fit inside these.
READ_RANGES = ((0x0000, 0x8000), (0x8000, 0xD300))
APRS_READ = b"T\x00\x00\x80"


class ProtocolError(Exception):
    """The radio did not answer as the protocol says it should."""


class ReadOnlyViolation(Exception):
    """Code tried to send a frame that is not on the read-only allowlist."""


def key_index(frame: bytes) -> int:
    """The XOR key index a SEND frame selects."""
    if len(frame) != 25 or frame[:4] != b"SEND":
        raise ValueError("not a 25-byte SEND frame")
    s = frame[4]
    n = (s - 0x20) * 2 + 1 if s & 0x20 else (s - 0x10) * 2
    index = frame[4 + n + 1]
    if not 0 <= index < len(KEYS):
        raise ValueError(f"SEND frame selects key {index}, outside the table")
    return index


def xor(data: bytes, key: bytes) -> bytes:
    """The radio's data obfuscation; the same function encrypts and decrypts."""
    out = bytearray(data)
    for i, b in enumerate(data):
        k = key[i % 4]
        if k == 0x20 or b in (0x00, 0xFF, k, k ^ 0xFF):
            continue
        out[i] = b ^ k
    return bytes(out)


def read_frame(addr: int) -> bytes:
    if not 0 <= addr <= 0xFFFF - BLOCK + 1 or addr % BLOCK:
        raise ValueError(f"address {addr:#x} is not a 128-byte block in 0x0000-0xFFFF")
    return b"R" + struct.pack(">H", addr) + bytes([BLOCK])


_SAFE_EXACT = frozenset({MAGIC, FIRMWARE_INFO, MODEL_INFO, END, SEND_FRAME, APRS_READ})


def check_read_only(frame: bytes) -> None:
    """Raise ReadOnlyViolation unless ``frame`` is a known read-only command."""
    if frame in _SAFE_EXACT:
        return
    if len(frame) == 4 and frame[0] == ord("R") and frame[3] == BLOCK and struct.unpack(">H", frame[1:3])[0] % BLOCK == 0:
        return
    raise ReadOnlyViolation(f"refusing to send non-read-only frame {frame[:8].hex(' ')}")


def decode_text(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
