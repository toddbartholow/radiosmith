"""Edit operations on an RT-950 PRO image, for drafts (like dm32.edit's, as plain dicts).

Channels are positional slots (32 bytes each, channel n at (n-1)*32); zones
are fixed ranges of slots, so channels are never renumbered here: deleting a
channel empties its slot. Only encodings confirmed against the vendor CPS are
written (see codeplug.py), and updates change only the bytes of the fields
named, so bytes this project does not decode (signalling, scramble, FHSS, byte
15 bit 1) keep their values.

    {"op": "add_channels", "channels": [{...}], "at": 30}      # empty slots from `at`, else after the last channel
    {"op": "update_channel", "channel": 21 | "Name", "changes": {...}}
    {"op": "replace_channel", "channel": 21 | "Name", "with": {...}}
    {"op": "delete_channels", "channels": [21, "Name"]}

A channel: name (0-12 ASCII; the radio allows unnamed channels), rx_mhz, tx_mhz (null = receive only), power
(low/middle/high), bandwidth_khz (12.5/25), rx_tone/tx_tone (CTCSS Hz or
null), scan_add (default true), busy_lock (default false), rx_am (AM receive,
e.g. for airband; default false).
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from radiosmith.dm32.edit import EditError, encode_bcd_freq
from radiosmith.rt950 import codeplug
from radiosmith.rt950.edit import EMPTY, NAME_MAX, POWER_CODES, encode_ctcss

SIZE = codeplug.CHANNEL_SIZE
# RX range the vendor CPS accepts; TX only in the bands the radio reports (F reply).
RX_RANGE_MHZ = (18.0, 1000.0)
TX_BANDS_MHZ = ((136.0, 174.0), (200.0, 260.0), (400.0, 520.0))


def _freq(label: str, f: object, ok) -> None:
    if not isinstance(f, int | float) or isinstance(f, bool):
        raise EditError(f"{label} must be a number of MHz, not {f!r}")
    if not ok(f):
        raise EditError(f"{label} {f} MHz is out of range")
    if abs(f * 100_000 - round(f * 100_000)) > 1e-6:
        raise EditError(f"{label} {f} MHz is not a multiple of 10 Hz")


@dataclass(frozen=True)
class Rt950Channel:
    name: str
    rx_mhz: float
    tx_mhz: float | None = None
    power: str = "low"
    bandwidth_khz: float = 25.0
    rx_tone: str | None = None
    tx_tone: str | None = None
    scan_add: bool = True
    busy_lock: bool = False
    rx_am: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> Rt950Channel:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise EditError(f"unknown channel fields {sorted(unknown)}; allowed: {sorted(known)}")
        if "name" not in d or "rx_mhz" not in d:
            raise EditError("a channel needs at least name and rx_mhz")
        return cls(**d)

    def validate(self, *, skip_tones: bool = False) -> None:
        n = self.name
        if not isinstance(n, str) or len(n) > NAME_MAX or not n.isascii() or not n.isprintable():
            raise EditError(f"channel name {n!r} must be up to {NAME_MAX} printable ASCII characters")
        n = n or "unnamed channel"
        _freq(f"{n}: rx", self.rx_mhz, lambda f: RX_RANGE_MHZ[0] <= f <= RX_RANGE_MHZ[1])
        if self.tx_mhz is not None:
            _freq(f"{n}: tx", self.tx_mhz, lambda f: any(lo <= f <= hi for lo, hi in TX_BANDS_MHZ))
        if self.power not in POWER_CODES:
            raise EditError(f"{n}: power must be one of {list(POWER_CODES)}")
        if self.bandwidth_khz not in (12.5, 25.0):
            raise EditError(f"{n}: bandwidth must be 12.5 or 25 kHz")
        for flag in ("scan_add", "busy_lock", "rx_am"):
            if not isinstance(getattr(self, flag), bool):
                raise EditError(f"{n}: {flag} must be true or false")
        if not skip_tones:
            encode_ctcss(self.rx_tone)
            encode_ctcss(self.tx_tone)

    def record(self, base: bytes = EMPTY, only: set[str] | None = None) -> bytes:
        """The 32-byte record. Fields not written keep ``base``'s bytes; ``only`` limits which fields are written."""
        self.validate(skip_tones=only is not None and not ({"rx_tone", "tx_tone"} & only))
        want = only if only is not None else {f.name for f in fields(self)}
        rec = bytearray(base)
        if base == EMPTY:
            rec[8:16] = bytes(8)  # no tones, no signalling, PTT-ID off, scramble off, flags clear
        if "rx_mhz" in want:
            rec[0:4] = encode_bcd_freq(self.rx_mhz)
        if "tx_mhz" in want:
            rec[4:8] = encode_bcd_freq(self.tx_mhz)
        if "rx_tone" in want:
            rec[8:10] = encode_ctcss(self.rx_tone)
        if "tx_tone" in want:
            rec[10:12] = encode_ctcss(self.tx_tone)
        if "power" in want:
            rec[14] = (rec[14] & 0xF0) | POWER_CODES[self.power]
        for name, bit, on in (
            ("rx_am", 0x01, self.rx_am),
            ("scan_add", 0x04, self.scan_add),
            ("busy_lock", 0x08, self.busy_lock),
            ("bandwidth_khz", 0x40, self.bandwidth_khz == 12.5),
        ):
            if name in want:
                rec[15] = (rec[15] & ~bit & 0xFF) | (bit if on else 0)
        if "name" in want:
            rec[20:32] = self.name.encode("ascii").ljust(NAME_MAX, b"\xff")
        return bytes(rec)


def _current(rec: bytes, number: int) -> dict:
    ch = next(iter(codeplug.decode_channels(b"\xff" * (number - 1) * SIZE + rec)), None)
    if ch is None:
        raise EditError(f"channel {number} is empty")
    return {
        "name": ch.name,
        "rx_mhz": ch.rx_mhz,
        "tx_mhz": ch.tx_mhz,
        "power": ch.power,
        "bandwidth_khz": ch.bandwidth_khz,
        "rx_tone": ch.rx_tone,
        "tx_tone": ch.tx_tone,
        "scan_add": ch.scan_add,
        "busy_lock": ch.busy_lock,
        "rx_am": ch.rx_am,
    }


class Image:
    def __init__(self, image: bytes):
        self.data = bytearray(image)

    def rec(self, n: int) -> bytes:
        return bytes(self.data[(n - 1) * SIZE : n * SIZE])

    def put(self, n: int, rec: bytes) -> None:
        self.data[(n - 1) * SIZE : n * SIZE] = rec

    def used(self) -> dict[int, str]:
        return {c.number: c.name for c in codeplug.decode_channels(bytes(self.data))}

    def resolve(self, ref: int | str) -> int:
        if isinstance(ref, bool) or not isinstance(ref, int | str):
            raise EditError(f"channel reference {ref!r} must be a number or a name")
        used = self.used()
        if isinstance(ref, str):
            hits = [n for n, name in used.items() if name == ref]
            if len(hits) != 1:
                raise EditError(
                    f"no channel named {ref!r}" if not hits else f"several channels are named {ref!r}; use the number"
                )
            return hits[0]
        if not 1 <= ref <= codeplug.CHANNELS:
            raise EditError(f"channel {ref} is out of range 1-{codeplug.CHANNELS}")
        return ref

    def check_name(self, name: str, except_number: int | None = None) -> None:
        for n, existing in self.used().items():
            if existing and existing == name and n != except_number:
                raise EditError(f"a channel named {name!r} already exists (channel {n})")


def op_add_channels(img: Image, channels: list[dict], at: int | None = None) -> dict:
    if not isinstance(channels, list) or not channels:
        raise EditError("no channels to add")
    chans = [Rt950Channel.from_dict(c) for c in channels]
    names = [c.name for c in chans]
    if len(set(names)) != len(names):
        raise EditError("two new channels have the same name")
    start = at if at is not None else max(img.used(), default=0) + 1
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        raise EditError(f"at must be a channel number, not {at!r}")
    numbers = list(range(start, start + len(chans)))
    if numbers[-1] > codeplug.CHANNELS:
        raise EditError(f"the radio has {codeplug.CHANNELS} channels")
    for n, ch in zip(numbers, chans, strict=True):
        if img.rec(n) != EMPTY:
            raise EditError(f"channel {n} is in use")
        img.check_name(ch.name)
        img.put(n, ch.record())
    return {"numbers": numbers}


def op_update_channel(img: Image, channel: int | str, changes: dict) -> dict:
    n = img.resolve(channel)
    if not isinstance(changes, dict) or not changes:
        raise EditError("no changes given")
    cur = _current(img.rec(n), n)
    unknown = set(changes) - set(cur)
    if unknown:
        raise EditError(f"cannot change {sorted(unknown)}; changeable fields: {sorted(cur)}")
    ch = Rt950Channel(**(cur | changes))
    img.check_name(ch.name, except_number=n)
    img.put(n, ch.record(img.rec(n), only=set(changes)))
    return {"number": n}


def op_replace_channel(img: Image, channel: int | str, with_: dict) -> dict:
    n = img.resolve(channel)
    ch = Rt950Channel.from_dict(with_)
    img.check_name(ch.name, except_number=n)
    img.put(n, ch.record())
    return {"number": n}


def op_delete_channels(img: Image, channels: list[int | str]) -> dict:
    if not isinstance(channels, list) or not channels:
        raise EditError("no channels to delete")
    gone = sorted({img.resolve(c) for c in channels})
    for n in gone:
        if img.rec(n) == EMPTY:
            raise EditError(f"channel {n} is already empty")
        img.put(n, EMPTY)
    return {"deleted": gone}


# Profile channel fields (profiles/*.json are shared with the DM-32UV) and the ones the RT-950 PRO stores.
PROFILE_FIELDS = {"name", "rx_mhz", "tx_mhz", "forbid_tx", "power", "bandwidth_khz", "rx_tone", "tx_tone", "scan_list", "rx_am"}
PROFILE_KEEP = ("name", "rx_mhz", "tx_mhz", "power", "bandwidth_khz", "rx_tone", "tx_tone", "rx_am")


def profile_channels(channels: list[dict]) -> list[dict]:
    """A profile's channels as RT-950 PRO channels, checked.

    There is no zone list on the RT-950 PRO (zones are fixed ranges of
    channels) and no TX inhibit: a receive-only profile channel is one without
    a TX frequency, and one that has a TX frequency but forbids TX is refused.
    scan_list is a DM-32UV setting and is left out.
    """
    out, blocked = [], []
    for c in channels:
        unknown = set(c) - PROFILE_FIELDS
        if unknown:
            raise EditError(f"unknown profile channel fields {sorted(unknown)}; allowed: {sorted(PROFILE_FIELDS)}")
        if c.get("forbid_tx", True) and c.get("tx_mhz") is not None:
            blocked.append(c.get("name"))
        ch = {k: c[k] for k in PROFILE_KEEP if k in c}
        Rt950Channel.from_dict(ch).validate()
        out.append(ch)
    if blocked:
        raise EditError(
            f"{blocked}: these profile channels have a TX frequency but forbid TX, which the RT-950 PRO "
            "cannot store (it has no TX inhibit); remove their tx_mhz for receive only"
        )
    return out


OPERATIONS = {
    "add_channels": (op_add_channels, {"channels", "at"}),
    "update_channel": (op_update_channel, {"channel", "changes"}),
    "replace_channel": (op_replace_channel, {"channel", "with"}),
    "delete_channels": (op_delete_channels, {"channels"}),
}


def apply_operation(image: bytes, op: dict) -> tuple[bytes, dict]:
    if not isinstance(op, dict) or op.get("op") not in OPERATIONS:
        name = op.get("op") if isinstance(op, dict) else op
        raise EditError(f"unknown operation {name!r} for the RT-950 PRO; known: {sorted(OPERATIONS)}")
    func, allowed = OPERATIONS[op["op"]]
    args = {k: v for k, v in op.items() if k != "op"}
    unknown = set(args) - allowed
    if unknown:
        raise EditError(f"{op['op']}: unknown arguments {sorted(unknown)}; allowed: {sorted(allowed)}")
    if "with" in args:
        args["with_"] = args.pop("with")
    img = Image(image)
    try:
        info = func(img, **args)
    except TypeError as e:
        raise EditError(f"{op['op']}: {e}") from e
    return bytes(img.data), info


def apply_operations(image: bytes, ops: list[dict]) -> tuple[bytes, list[dict]]:
    infos = []
    for i, op in enumerate(ops, 1):
        try:
            image, info = apply_operation(image, op)
        except EditError as e:
            raise EditError(f"operation {i} ({op.get('op') if isinstance(op, dict) else op}): {e}") from e
        infos.append(info)
    return image, infos
