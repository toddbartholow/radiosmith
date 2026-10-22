"""Pieces for building RT-950 PRO channel records (layout in codeplug.py, confirmed against the vendor CPS).

The records themselves are built by draftops.Rt950Channel. Only encodings
confirmed on the owner's radio are produced: frequencies, CTCSS tones, power,
narrow, scan add, busy lock, AM receive and the name. DCS tones are
refused until their encoding is confirmed. A receive-only channel has no TX
frequency (ff ff ff ff), the form the vendor CPS writes for a blank TX field;
the CPS has no per-channel TX inhibit. Byte 15 bit 1 (set in factory
channels, meaning unknown) is written as the CPS writes it, 0.
"""

from __future__ import annotations

from radiosmith.dm32.edit import EditError
from radiosmith.rt950 import codeplug

NAME_MAX = 12
POWER_CODES = {"high": 0, "middle": 1, "low": 2}
EMPTY = b"\xff" * codeplug.CHANNEL_SIZE


def encode_ctcss(tone: str | None) -> bytes:
    if tone is None:
        return b"\x00\x00"
    try:
        tenths = round(float(tone) * 10)
    except (TypeError, ValueError) as e:
        raise EditError(f"tone {tone!r}: only CTCSS tones in Hz are supported on the RT-950 PRO so far") from e
    if not 600 <= tenths <= 2600:
        raise EditError(f"CTCSS tone {tone} Hz is out of range")
    return tenths.to_bytes(2, "little")
