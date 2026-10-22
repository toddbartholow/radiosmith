"""Serial transport: port enumeration, line-state control and traffic logging.

Radio drivers talk to a ``Link`` rather than to pyserial directly, so tests can
substitute a simulated radio and every byte on the wire goes through one log.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Protocol

import serial
from serial.tools import list_ports

# CH340 USB-serial bridge, used by the usual DM-32UV programming cable.
CH340_VID_PID = (0x1A86, 0x7523)

# Named RTS/DTR states to open the port with. pyserial applies them as the port
# opens, so the radio never sees another state first.
#
# - cps: DTR and RTS asserted. What the vendor CPS v1.60 sets (CHIRP driver
#   reverse engineering) and what the CHIRP read tool used on a real radio.
# - rts-only: RTS asserted, DTR clear. dm32uv-linux reports this is required
#   on firmware DM32.NRF.01.049; not independently confirmed.
# - dtr-only: DTR asserted, RTS clear. What qdmr uses.
# - none: both clear.
LINE_PRESETS: dict[str, tuple[bool, bool]] = {
    "cps": (True, True),
    "rts-only": (False, True),
    "dtr-only": (True, False),
    "none": (False, False),
}

# Replies longer than this are logged as a length, header and hash rather than
# in full: page contents go to the backup, not the log.
LOG_FULL_MAX = 64


@dataclass
class PortInfo:
    device: str
    description: str
    vid: int | None
    pid: int | None
    manufacturer: str | None
    hwid: str

    @property
    def is_ch340(self) -> bool:
        return (self.vid, self.pid) == CH340_VID_PID

    def to_dict(self) -> dict:
        d = asdict(self)
        d["vid"] = f"{self.vid:04X}" if self.vid is not None else None
        d["pid"] = f"{self.pid:04X}" if self.pid is not None else None
        d["is_ch340"] = self.is_ch340
        return d


def list_serial_ports() -> list[PortInfo]:
    return [
        PortInfo(
            device=p.device,
            description=p.description,
            vid=p.vid,
            pid=p.pid,
            manufacturer=p.manufacturer,
            hwid=p.hwid,
        )
        for p in sorted(list_ports.comports(), key=lambda p: p.device)
    ]


def choose_port(requested: str | None) -> PortInfo:
    """Return the requested port, or the only programming cable if none was named."""
    ports = list_serial_ports()
    if requested:
        for p in ports:
            if p.device.lower() == requested.lower():
                return p
        listing = ", ".join(p.device for p in ports) or "none"
        raise LookupError(f"port {requested} not found (present: {listing})")
    cables = [p for p in ports if p.is_ch340]
    if len(cables) == 1:
        return cables[0]
    if not cables and len(ports) == 1:
        return ports[0]
    listing = "; ".join(f"{p.device} ({p.description})" for p in ports) or "none"
    raise LookupError(f"cannot choose a port automatically, use --port (present: {listing})")


class TrafficLog:
    """JSON-lines log of port events and every frame sent and received.

    Times are seconds since the log was opened (monotonic clock), so gaps
    between sessions and reply latencies can be read straight off the log.
    """

    def __init__(self, stream: IO[str] | None):
        self._stream = stream
        self._t0 = time.monotonic()

    @classmethod
    def to_file(cls, path: Path) -> TrafficLog:
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(open(path, "a", encoding="utf-8"))

    @classmethod
    def null(cls) -> TrafficLog:
        return cls(None)

    def now(self) -> float:
        return round(time.monotonic() - self._t0, 4)

    def event(self, kind: str, **fields) -> None:
        self._emit({"t": self.now(), "ev": kind, **fields})

    def tx(self, data: bytes) -> None:
        self._emit({"t": self.now(), "ev": "tx", **_describe(data)})

    def rx(self, data: bytes, wanted: int, elapsed: float) -> None:
        self._emit({"t": self.now(), "ev": "rx", "wanted": wanted, "ms": round(elapsed * 1000, 1), **_describe(data)})

    def _emit(self, record: dict) -> None:
        if self._stream is not None:
            self._stream.write(json.dumps(record) + "\n")
            self._stream.flush()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None


def _describe(data: bytes) -> dict:
    if len(data) <= LOG_FULL_MAX:
        return {"n": len(data), "hex": data.hex(" ")}
    return {"n": len(data), "head": data[:8].hex(" "), "sha256": hashlib.sha256(data).hexdigest()[:16]}


class Link(Protocol):
    """Byte link to a radio. ``read`` returns fewer bytes than asked on timeout."""

    def write(self, data: bytes) -> None: ...
    def read(self, n: int, timeout: float) -> bytes: ...
    def reset_input(self) -> None: ...
    def close(self) -> None: ...


class SerialLink:
    """A pyserial port opened with explicit line states, logging all traffic."""

    def __init__(self, device: str, *, baud: int, lines: str, log: TrafficLog):
        if lines not in LINE_PRESETS:
            raise ValueError(f"unknown line preset {lines!r}; choose from {', '.join(LINE_PRESETS)}")
        dtr, rts = LINE_PRESETS[lines]
        self.device = device
        self.lines = lines
        self.log = log
        ser = serial.Serial()
        ser.port = device
        ser.baudrate = baud
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        ser.xonxoff = False
        ser.rtscts = False
        ser.dsrdtr = False
        ser.dtr = dtr
        ser.rts = rts
        ser.timeout = 0.5
        ser.write_timeout = 2.0
        if os.name == "posix":
            ser.exclusive = True
        log.event("open", port=device, baud=baud, lines=lines, dtr=dtr, rts=rts)
        try:
            ser.open()
        except serial.SerialException as e:
            log.event("open_failed", error=str(e))
            raise
        self._ser = ser
        log.event("opened")

    def write(self, data: bytes) -> None:
        self.log.tx(data)
        self._ser.write(data)
        self._ser.flush()

    def read(self, n: int, timeout: float) -> bytes:
        self._ser.timeout = timeout
        start = time.monotonic()
        data = self._ser.read(n)
        self.log.rx(data, n, time.monotonic() - start)
        return data

    def reset_input(self) -> None:
        waiting = self._ser.in_waiting
        if waiting:
            stale = self._ser.read(waiting)
            self.log.event("discard", n=len(stale), hex=stale[:LOG_FULL_MAX].hex(" "))
        self._ser.reset_input_buffer()

    def close(self) -> None:
        if self._ser.is_open:
            self._ser.close()
            self.log.event("closed")
