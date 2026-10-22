"""Decode RT-950 PRO channels from a backup image (read only).

Channel records are 32 bytes from address 0; an empty slot is all 0xFF.
Checked against the vendor CPS v1.3.3 on the owner's radio; "CPS" marks a
field confirmed by comparing a CPS file with a read of the same radio after
the CPS changed that field.

    0-3    RX frequency, 8-digit BCD, little-endian, 10 Hz units (CPS)
    4-7    TX frequency, same; ff ff ff ff = none, receive only (CPS)
    8-9    RX tone, 10-11 TX tone: 0000 or ffff off; second byte 00 = DCS
           index (1-based); otherwise little-endian CTCSS in 0.1 Hz (CTCSS: CPS)
    12     signalling group
    13     PTT-ID: 0 off, 1 BOT, 2 EOT, 3 both (CPS)
    14     low nibble power: 0 high, 1 middle, 2 low (CPS); high nibble scramble (CPS)
    15     flags: bit 0 AM receive (CPS), bit 2 scan add (CPS), bit 3 busy lock
           (CPS), bits 4-5 encryption, bit 6 narrow (CPS), bit 7 FHSS learn
           (CPS). Bit 1 is set in factory
           channels but the CPS has no field for it and writes it as 0: it is
           NOT a TX enable. The CPS has no per-channel TX inhibit; a channel
           without a TX frequency is the receive-only form.
    20-31  name, ASCII, padded with 0xFF (CPS)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from radiosmith.dm32.codeplug import decode_bcd_freq

CHANNEL_SIZE = 32
CHANNELS = 960
POWER = {0: "high", 1: "middle", 2: "low"}
PTT_ID = {0: "off", 1: "BOT", 2: "EOT", 3: "both"}


@dataclass
class Channel:
    number: int
    name: str
    rx_mhz: float | None
    tx_mhz: float | None
    rx_only: bool  # no TX frequency stored
    power: str
    bandwidth_khz: float
    scan_add: bool
    busy_lock: bool
    rx_tone: str | None
    tx_tone: str | None
    ptt_id: str
    scramble: int
    fhss_learn: bool
    rx_am: bool


def decode_tone(raw: bytes) -> str | None:
    if raw in (b"\x00\x00", b"\xff\xff"):
        return None
    if raw[1] == 0:
        return f"DCS#{raw[0]}"
    return f"{int.from_bytes(raw, 'little') / 10:.1f}"


def decode_channels(image: bytes) -> list[Channel]:
    out = []
    for n in range(1, CHANNELS + 1):
        rec = image[(n - 1) * CHANNEL_SIZE : n * CHANNEL_SIZE]
        if len(rec) < CHANNEL_SIZE or rec == b"\xff" * CHANNEL_SIZE:
            continue
        flags = rec[15]
        name = rec[20:32].split(b"\xff", 1)[0].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        tx = decode_bcd_freq(rec[4:8])
        out.append(
            Channel(
                number=n,
                name=name,
                rx_mhz=decode_bcd_freq(rec[0:4]),
                tx_mhz=tx,
                rx_only=tx is None,
                power=POWER.get(rec[14] & 0x0F, f"unknown({rec[14] & 0x0F})"),
                bandwidth_khz=12.5 if flags & 0x40 else 25.0,
                scan_add=bool(flags & 0x04),
                busy_lock=bool(flags & 0x08),
                rx_tone=decode_tone(rec[8:10]),
                tx_tone=decode_tone(rec[10:12]),
                ptt_id=PTT_ID.get(rec[13], f"unknown({rec[13]})"),
                scramble=rec[14] >> 4,
                fhss_learn=bool(flags & 0x80),
                rx_am=bool(flags & 0x01),
            )
        )
    return out


def decoded(image: bytes) -> dict:
    return {"channels": [asdict(c) for c in decode_channels(image)], "zones": [], "scan_lists": [], "problems": []}
