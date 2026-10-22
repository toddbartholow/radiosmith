"""Edit DM-32UV codeplug pages: channels, zones and scan lists.

Edits start from the pages in a verified backup and change only the bytes they
mean to change. A new or replaced channel record is a copy of an existing plain
analog channel (no tones, no signalling) with its name, frequencies, power,
bandwidth and transmit inhibition set, so fields this project does not decode
keep the factory's values. Layouts are documented in codeplug.py.

Receive-only channels are inhibited twice: the Forbid TX bit is set and no
transmit frequency is stored (ff ff ff ff, which the vendor CPS also writes for
a blank TX frequency).

Operations are plain dicts (see ``apply_operation``) so a draft can store them
as JSON and replay them on a newer read of the same radio.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, fields

from radiosmith.dm32 import codeplug
from radiosmith.dm32 import protocol as p

# Bands this radio's channels are known to work in (the radio transmits on
# 136-174 and 400-480 MHz; it receives more widely, but other bands are untested here).
CHANNEL_BANDS_MHZ = ((136.0, 174.0), (400.0, 480.0))
POWER_CODES = {"low": 0, "middle": 1, "high": 2}
NAME_MAX = 16
ZONE_NAME_MAX = 16
SCAN_LIST_NAME_MAX = 11
SCAN_LISTS_MAX = 32
EMPTY_CHANNEL = b"\xff" * codeplug.CHANNEL_SIZE
EMPTY_ZONE = b"\xff" * 16 + b"\x00" * (codeplug.ZONE_SIZE - 16)


class EditError(Exception):
    """An edit cannot be applied safely to this codeplug."""


def _check_name(kind: str, name: object, limit: int) -> str:
    if not isinstance(name, str) or not name or len(name) > limit or not name.isascii() or not name.isprintable():
        raise EditError(f"{kind} name {name!r} must be 1-{limit} printable ASCII characters")
    return name


def _check_freq(label: str, f: float) -> None:
    if not isinstance(f, int | float) or isinstance(f, bool):
        raise EditError(f"{label} must be a number of MHz, not {f!r}")
    if not any(lo <= f <= hi for lo, hi in CHANNEL_BANDS_MHZ):
        raise EditError(f"{label} {f} MHz is outside {CHANNEL_BANDS_MHZ}")
    if abs(f * 100_000 - round(f * 100_000)) > 1e-6:
        raise EditError(f"{label} {f} MHz is not a multiple of 10 Hz")


@dataclass(frozen=True)
class AnalogChannel:
    name: str
    rx_mhz: float
    tx_mhz: float | None = None  # None: no transmit frequency
    forbid_tx: bool = True
    power: str = "low"
    bandwidth_khz: float = 25.0
    rx_tone: str | None = None  # CTCSS in Hz, e.g. "127.3"
    tx_tone: str | None = None
    scan_list: int = 0  # 0: none

    @classmethod
    def from_dict(cls, d: dict) -> AnalogChannel:
        if d.get("rx_am"):
            raise EditError(f"{d.get('name')}: the DM-32UV cannot receive AM; rx_am is for the RT-950 PRO")
        d = {k: v for k, v in d.items() if k != "rx_am"}  # profiles may say rx_am: false
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise EditError(f"unknown channel fields {sorted(unknown)}; allowed: {sorted(known)}")
        if "name" not in d or "rx_mhz" not in d:
            raise EditError("a channel needs at least name and rx_mhz")
        return cls(**d)

    def validate(self) -> None:
        _check_name(f"channel {self.name!r}:", self.name, NAME_MAX)
        _check_freq(f"{self.name}: rx", self.rx_mhz)
        if self.tx_mhz is not None:
            _check_freq(f"{self.name}: tx", self.tx_mhz)
        if self.power not in POWER_CODES:
            raise EditError(f"{self.name}: power must be one of {list(POWER_CODES)}")
        if self.bandwidth_khz not in (12.5, 25.0):
            raise EditError(f"{self.name}: bandwidth must be 12.5 or 25 kHz")
        if not isinstance(self.forbid_tx, bool):
            raise EditError(f"{self.name}: forbid_tx must be true or false")
        if self.tx_mhz is None and not self.forbid_tx:
            raise EditError(f"{self.name}: a channel without a TX frequency must forbid TX")
        for tone in (self.rx_tone, self.tx_tone):
            if tone is not None:
                encode_ctcss(tone)
        if not isinstance(self.scan_list, int) or not 0 <= self.scan_list <= SCAN_LISTS_MAX:
            raise EditError(f"{self.name}: scan_list must be 0 (none) or 1-{SCAN_LISTS_MAX}")


def encode_bcd_freq(mhz: float | None) -> bytes:
    """8-digit BCD, little-endian, 10 Hz units; None -> ff ff ff ff."""
    if mhz is None:
        return b"\xff" * 4
    digits = f"{round(mhz * 100_000):08d}"
    if len(digits) != 8:
        raise EditError(f"{mhz} MHz does not fit in 8 BCD digits")
    return bytes(int(digits[i : i + 2], 16) for i in range(6, -1, -2))


def encode_ctcss(tone: str | None) -> bytes:
    """CTCSS in tenths of Hz as 4 BCD digits, little-endian; None -> ff ff."""
    if tone is None:
        return b"\xff\xff"
    try:
        tenths = round(float(tone) * 10)
    except (TypeError, ValueError) as e:
        raise EditError(f"CTCSS tone {tone!r} is not a number of Hz") from e
    if not 600 <= tenths <= 2600:
        raise EditError(f"CTCSS tone {tone} Hz is out of range")
    digits = f"{tenths:04d}"
    return bytes([int(digits[2:4], 16), int(digits[0:2], 16)])


def is_plain_analog(rec: bytes) -> bool:
    """An analog channel with no tones and no signalling: safe to copy for new channels."""
    return (
        rec[:16] != b"\xff" * 16
        and rec[0x18] >> 4 == 0
        and rec[0x21:0x25] == b"\xff" * 4
        and (rec[0x26] >> 1) & 0x07 == 0
        and rec[0x27] == 0
    )


def analog_record(ch: AnalogChannel, template: bytes) -> bytes:
    ch.validate()
    rec = bytearray(template)
    rec[0:16] = ch.name.encode("ascii").ljust(16, b"\x00")
    rec[0x10:0x14] = encode_bcd_freq(ch.rx_mhz)
    rec[0x14:0x18] = encode_bcd_freq(ch.tx_mhz)
    # 0x18: type analog (0), Forbid TX bit 3, power bits 2-1, lone work bit 0 off
    rec[0x18] = (0x08 if ch.forbid_tx else 0) | (POWER_CODES[ch.power] << 1)
    # 0x19: bandwidth bit 7, auto scan off, scan list bits 5-0
    rec[0x19] = (0x80 if ch.bandwidth_khz == 25.0 else 0) | ch.scan_list
    rec[0x21:0x23] = encode_ctcss(ch.rx_tone)
    rec[0x23:0x25] = encode_ctcss(ch.tx_tone)
    return bytes(rec)


def _locate(pages: dict[int, bytes]) -> dict[int, int]:
    """tag -> page address for the live (non-stale, non-free) pages; duplicate tags refused."""
    where: dict[int, int] = {}
    for addr in sorted(pages):
        tag = pages[addr][-1]
        if tag in (p.TAG_STALE, p.TAG_FREE):
            continue
        if tag in where:
            raise EditError(f"tag {tag:#04x} is on two pages; refusing to edit")
        where[tag] = addr
    return where


class Editor:
    """Mutable working copies of the channel, zone and scan list pages."""

    def __init__(self, pages: dict[int, bytes]):
        self.pages = dict(pages)
        self.where = _locate(pages)
        self._work: dict[int, bytearray] = {}
        cp = codeplug.decode(pages)
        if cp.problems:
            raise EditError(f"codeplug has problems: {cp.problems}")

    def page(self, tag: int) -> bytearray:
        if tag not in self._work:
            if tag not in self.where:
                raise EditError(f"page with tag {tag:#04x} does not exist on this radio")
            self._work[tag] = bytearray(self.pages[self.where[tag]])
        return self._work[tag]

    def result(self) -> dict[int, bytes]:
        """All pages, with the edited ones replaced."""
        out = dict(self.pages)
        for tag, data in self._work.items():
            if len(data) != p.PAGE or data[-1] != tag:
                raise AssertionError("page size or tag changed")
            out[self.where[tag]] = bytes(data)
        return out

    # -- channels ----------------------------------------------------------------

    @property
    def channel_count(self) -> int:
        return struct.unpack_from("<H", self.page(p.TAG_CHANNELS_FIRST), 0)[0]

    @channel_count.setter
    def channel_count(self, n: int) -> None:
        struct.pack_into("<H", self.page(p.TAG_CHANNELS_FIRST), 0, n)

    def _channel_slot(self, n: int) -> tuple[bytearray, int]:
        page_index, off = codeplug.channel_location(n)
        return self.page(p.TAG_CHANNELS_FIRST + page_index), off

    def channel(self, n: int) -> bytes:
        page, off = self._channel_slot(n)
        return bytes(page[off : off + codeplug.CHANNEL_SIZE])

    def set_channel(self, n: int, rec: bytes) -> None:
        assert len(rec) == codeplug.CHANNEL_SIZE
        page, off = self._channel_slot(n)
        page[off : off + codeplug.CHANNEL_SIZE] = rec

    def channel_names(self) -> dict[str, int]:
        return {codeplug.decode_channel(n, self.channel(n)).name: n for n in range(1, self.channel_count + 1)}

    def resolve_channel(self, ref: int | str) -> int:
        if isinstance(ref, bool) or not isinstance(ref, int | str):
            raise EditError(f"channel reference {ref!r} must be a number or a name")
        if isinstance(ref, str):
            names = self.channel_names()
            if ref not in names:
                raise EditError(f"no channel named {ref!r}")
            return names[ref]
        if not 1 <= ref <= self.channel_count:
            raise EditError(f"channel {ref} does not exist (the radio has {self.channel_count})")
        return ref

    def template(self) -> bytes:
        for n in range(1, self.channel_count + 1):
            rec = self.channel(n)
            if is_plain_analog(rec):
                return rec
        raise EditError("no plain analog channel to use as a template")

    def _check_unique(self, name: str, except_number: int | None = None) -> None:
        other = self.channel_names().get(name)
        if other is not None and other != except_number:
            raise EditError(f"a channel named {name!r} already exists (channel {other})")

    def _check_scan_list(self, n: int) -> None:
        if n and n > self.scan_list_count:
            raise EditError(f"scan list {n} does not exist (the radio has {self.scan_list_count})")

    # -- zones -------------------------------------------------------------------

    @property
    def zone_count(self) -> int:
        return self.page(p.TAG_ZONES_FIRST)[0]

    def _zone_slot(self, z: int) -> tuple[bytearray, int]:
        page_index, slot = divmod(z - 1, codeplug.ZONES_PER_PAGE)
        off = codeplug.ZONE_SIZE * slot + (codeplug.ZONE_HEADER if page_index == 0 else 0)
        return self.page(p.TAG_ZONES_FIRST + page_index), off

    def zone(self, z: int) -> tuple[str, list[int]]:
        page, off = self._zone_slot(z)
        rec = page[off : off + codeplug.ZONE_SIZE]
        members = list(struct.unpack_from(f"<{min(rec[0x10], codeplug.ZONE_MEMBERS)}H", rec, 0x11))
        return codeplug._text(bytes(rec[:16])), members

    def zone_record(self, z: int) -> bytes:
        page, off = self._zone_slot(z)
        return bytes(page[off : off + codeplug.ZONE_SIZE])

    def set_zone_record(self, z: int, rec: bytes) -> None:
        assert len(rec) == codeplug.ZONE_SIZE
        page, off = self._zone_slot(z)
        page[off : off + codeplug.ZONE_SIZE] = rec

    def set_zone(self, z: int, name: str | None, members: list[int]) -> None:
        """Set a zone's members, and its name when ``name`` is given; other bytes are kept."""
        if len(members) > codeplug.ZONE_MEMBERS:
            raise EditError(f"a zone holds at most {codeplug.ZONE_MEMBERS} channels")
        rec = bytearray(self.zone_record(z))
        if name is not None:
            rec[0:16] = (name.encode("ascii") + b"\x00").ljust(16, b"\xff")[:16]
        rec[0x10] = len(members)
        rec[0x11 : 0x11 + 2 * codeplug.ZONE_MEMBERS] = b"\x00" * (2 * codeplug.ZONE_MEMBERS)
        struct.pack_into(f"<{len(members)}H", rec, 0x11, *members)
        self.set_zone_record(z, bytes(rec))

    def resolve_zone(self, ref: int | str) -> int:
        if isinstance(ref, str):
            for z in range(1, self.zone_count + 1):
                if self.zone(z)[0] == ref:
                    return z
            raise EditError(f"no zone named {ref!r}")
        if isinstance(ref, bool) or not isinstance(ref, int) or not 1 <= ref <= self.zone_count:
            raise EditError(f"zone {ref!r} does not exist (the radio has {self.zone_count})")
        return ref

    # -- scan lists ----------------------------------------------------------------

    @property
    def scan_list_count(self) -> int:
        return self.page(p.TAG_SCAN_LISTS)[0]

    def _scan_off(self, n: int) -> int:
        return codeplug.SCAN_LIST_SIZE * n - 0x38

    def scan_list(self, n: int) -> tuple[str, list[int]]:
        rec = self.page(p.TAG_SCAN_LISTS)[self._scan_off(n) : self._scan_off(n) + codeplug.SCAN_LIST_SIZE]
        members = list(struct.unpack_from(f"<{min(rec[0x0B], codeplug.SCAN_LIST_MEMBERS)}H", rec, 0x18))
        return codeplug._text(bytes(rec[0:11])), members

    def set_scan_list(self, n: int, name: str | None, members: list[int]) -> None:
        """Set a scan list's members, and its name when ``name`` is given; other bytes are kept."""
        if len(members) > codeplug.SCAN_LIST_MEMBERS:
            raise EditError(f"a scan list holds at most {codeplug.SCAN_LIST_MEMBERS} channels")
        page, off = self.page(p.TAG_SCAN_LISTS), self._scan_off(n)
        if name is not None:
            page[off : off + 11] = name.encode("ascii").ljust(11, b"\x00")
        page[off + 0x0B] = len(members)
        page[off + 0x18 : off + 0x18 + 2 * codeplug.SCAN_LIST_MEMBERS] = b"\x00" * (2 * codeplug.SCAN_LIST_MEMBERS)
        struct.pack_into(f"<{len(members)}H", page, off + 0x18, *members)

    def resolve_scan_list(self, ref: int | str) -> int:
        if isinstance(ref, str):
            for n in range(1, self.scan_list_count + 1):
                if self.scan_list(n)[0] == ref:
                    return n
            raise EditError(f"no scan list named {ref!r}")
        if isinstance(ref, bool) or not isinstance(ref, int) or not 1 <= ref <= self.scan_list_count:
            raise EditError(f"scan list {ref!r} does not exist (the radio has {self.scan_list_count})")
        return ref


# -- operations ---------------------------------------------------------------------
#
# Each takes an Editor and the operation's arguments and returns a small dict
# describing what it did (for example the numbers given to new channels).


def _members(ed: Editor, refs: object, limit: int, kind: str) -> list[int]:
    if not isinstance(refs, list) or not refs:
        raise EditError(f"{kind} members must be a non-empty list of channel numbers or names")
    numbers = [ed.resolve_channel(r) for r in refs]
    if len(set(numbers)) != len(numbers):
        raise EditError(f"{kind} lists a channel twice")
    if len(numbers) > limit:
        raise EditError(f"a {kind} holds at most {limit} channels")
    return numbers


def op_add_channels(ed: Editor, channels: list[dict]) -> dict:
    if not isinstance(channels, list) or not channels:
        raise EditError("no channels to add")
    chans = [AnalogChannel.from_dict(c) for c in channels]
    names = [c.name for c in chans]
    if len(set(names)) != len(names):
        raise EditError("two new channels have the same name")
    template = ed.template()
    first = ed.channel_count + 1
    for i, ch in enumerate(chans):
        ch.validate()
        ed._check_unique(ch.name)
        ed._check_scan_list(ch.scan_list)
        ed.set_channel(first + i, analog_record(ch, template))
    ed.channel_count = first + len(chans) - 1
    return {"numbers": list(range(first, first + len(chans)))}


def op_replace_channel(ed: Editor, channel: int | str, with_: dict) -> dict:
    """Overwrite a channel (of any type) with an analog channel built from the template."""
    n = ed.resolve_channel(channel)
    ch = AnalogChannel.from_dict(with_)
    ch.validate()
    ed._check_unique(ch.name, except_number=n)
    ed._check_scan_list(ch.scan_list)
    ed.set_channel(n, analog_record(ch, ed.template()))
    return {"number": n}


UPDATABLE = {"name", "rx_mhz", "tx_mhz", "forbid_tx", "power", "bandwidth_khz", "rx_tone", "tx_tone", "scan_list"}
ANALOG_ONLY = {"bandwidth_khz", "rx_tone", "tx_tone"}


def op_update_channel(ed: Editor, channel: int | str, changes: dict) -> dict:
    """Change some fields of a channel, keeping every other byte."""
    n = ed.resolve_channel(channel)
    if not isinstance(changes, dict) or not changes:
        raise EditError("no changes given")
    unknown = set(changes) - UPDATABLE
    if unknown:
        raise EditError(f"cannot change {sorted(unknown)}; changeable fields: {sorted(UPDATABLE)}")
    rec = bytearray(ed.channel(n))
    cur = codeplug.decode_channel(n, bytes(rec))
    if cur is None:
        raise EditError(f"channel {n} is empty")
    if cur.mode != "analog" and set(changes) & ANALOG_ONLY:
        raise EditError(f"channel {n} is {cur.mode}; {sorted(set(changes) & ANALOG_ONLY)} apply to analog channels only")
    merged = {
        "name": cur.name,
        "rx_mhz": cur.rx_mhz,
        "tx_mhz": cur.tx_mhz,
        "forbid_tx": cur.forbid_tx,
        "power": cur.power,
        "bandwidth_khz": cur.bandwidth_khz,
        "rx_tone": None,
        "tx_tone": None,
        "scan_list": cur.scan_list,
    } | changes
    # Tones are only checked, and only written, when they are being changed:
    # decoded DCS tones cannot be re-encoded yet.
    check = AnalogChannel.from_dict(merged)
    check.validate()
    ed._check_unique(check.name, except_number=n)
    ed._check_scan_list(check.scan_list)

    if "name" in changes:
        rec[0:16] = check.name.encode("ascii").ljust(16, b"\x00")
    if "rx_mhz" in changes:
        rec[0x10:0x14] = encode_bcd_freq(check.rx_mhz)
    if "tx_mhz" in changes:
        rec[0x14:0x18] = encode_bcd_freq(check.tx_mhz)
    if {"forbid_tx", "power"} & set(changes):
        rec[0x18] = (rec[0x18] & ~0x0E & 0xFF) | (0x08 if check.forbid_tx else 0) | (POWER_CODES[check.power] << 1)
    if "bandwidth_khz" in changes:
        rec[0x19] = (rec[0x19] & 0x7F) | (0x80 if check.bandwidth_khz == 25.0 else 0)
    if "scan_list" in changes:
        rec[0x19] = (rec[0x19] & 0xC0) | check.scan_list
    if "rx_tone" in changes:
        rec[0x21:0x23] = encode_ctcss(check.rx_tone)
    if "tx_tone" in changes:
        rec[0x23:0x25] = encode_ctcss(check.tx_tone)
    ed.set_channel(n, bytes(rec))
    return {"number": n}


def op_delete_channels(ed: Editor, channels: list[int | str]) -> dict:
    """Delete channels, renumber the rest, and update zone and scan list members to match.

    Refuses if a zone or scan list would be left empty. References to channel
    numbers outside zones and scan lists are not decoded by this project and are
    left as they are.
    """
    if not isinstance(channels, list) or not channels:
        raise EditError("no channels to delete")
    gone = {ed.resolve_channel(c) for c in channels}
    count = ed.channel_count
    if len(gone) >= count:
        raise EditError("cannot delete every channel")
    keep = [n for n in range(1, count + 1) if n not in gone]
    new_number = {old: new for new, old in enumerate(keep, 1)}

    zones = [ed.zone(z) for z in range(1, ed.zone_count + 1)]
    scans = [ed.scan_list(n) for n in range(1, ed.scan_list_count + 1)]
    for kind, items in (("zone", zones), ("scan list", scans)):
        for name, members in items:
            if members and not [m for m in members if m in new_number]:
                raise EditError(f"{kind} {name!r} would be left empty; change or delete it first")

    records = [ed.channel(n) for n in keep]
    for i, rec in enumerate(records, 1):
        ed.set_channel(i, rec)
    for n in range(len(records) + 1, count + 1):
        ed.set_channel(n, EMPTY_CHANNEL)
    ed.channel_count = len(records)
    for z, (_, members) in enumerate(zones, 1):
        ed.set_zone(z, None, [new_number[m] for m in members if m in new_number])
    for n, (_, members) in enumerate(scans, 1):
        ed.set_scan_list(n, None, [new_number[m] for m in members if m in new_number])
    return {"deleted": sorted(gone), "renumbered": {str(o): n for o, n in new_number.items() if o != n}}


def op_add_zone(ed: Editor, name: str, members: list[int | str]) -> dict:
    _check_name("zone", name, ZONE_NAME_MAX)
    if any(ed.zone(z)[0] == name for z in range(1, ed.zone_count + 1)):
        raise EditError(f"zone {name!r} already exists")
    numbers = _members(ed, members, codeplug.ZONE_MEMBERS, "zone")
    z = ed.zone_count + 1
    if z > codeplug.ZONES_PER_PAGE:
        raise EditError("adding a zone beyond the first zone page is not supported yet")
    ed.set_zone(z, name, numbers)
    ed.page(p.TAG_ZONES_FIRST)[0] = z
    return {"number": z, "members": numbers}


def op_update_zone(ed: Editor, zone: int | str, name: str | None = None, members: list[int | str] | None = None) -> dict:
    z = ed.resolve_zone(zone)
    cur_name, cur_members = ed.zone(z)
    if name is None and members is None:
        raise EditError("give a new name, new members, or both")
    if name is not None:
        _check_name("zone", name, ZONE_NAME_MAX)
        if name != cur_name and any(ed.zone(o)[0] == name for o in range(1, ed.zone_count + 1)):
            raise EditError(f"zone {name!r} already exists")
    numbers = cur_members if members is None else _members(ed, members, codeplug.ZONE_MEMBERS, "zone")
    ed.set_zone(z, name if name != cur_name else None, numbers)
    return {"number": z, "members": numbers}


def op_delete_zone(ed: Editor, zone: int | str) -> dict:
    z = ed.resolve_zone(zone)
    count = ed.zone_count
    if count <= 1:
        raise EditError("cannot delete the only zone")
    for o in range(z + 1, count + 1):
        ed.set_zone_record(o - 1, ed.zone_record(o))
    ed.set_zone_record(count, EMPTY_ZONE)
    ed.page(p.TAG_ZONES_FIRST)[0] = count - 1
    return {"deleted": z}


def op_update_scan_list(
    ed: Editor, scan_list: int | str, name: str | None = None, members: list[int | str] | None = None
) -> dict:
    n = ed.resolve_scan_list(scan_list)
    cur_name, cur_members = ed.scan_list(n)
    if name is None and members is None:
        raise EditError("give a new name, new members, or both")
    if name is not None:
        _check_name("scan list", name, SCAN_LIST_NAME_MAX)
    numbers = cur_members if members is None else _members(ed, members, codeplug.SCAN_LIST_MEMBERS, "scan list")
    ed.set_scan_list(n, name if name != cur_name else None, numbers)
    return {"number": n, "members": numbers}


OPERATIONS = {
    "add_channels": (op_add_channels, {"channels"}),
    "replace_channel": (op_replace_channel, {"channel", "with"}),
    "update_channel": (op_update_channel, {"channel", "changes"}),
    "delete_channels": (op_delete_channels, {"channels"}),
    "add_zone": (op_add_zone, {"name", "members"}),
    "update_zone": (op_update_zone, {"zone", "name", "members"}),
    "delete_zone": (op_delete_zone, {"zone"}),
    "update_scan_list": (op_update_scan_list, {"scan_list", "name", "members"}),
}


def apply_operation(pages: dict[int, bytes], op: dict) -> tuple[dict[int, bytes], dict]:
    """Apply one operation, e.g. {"op": "add_zone", "name": "Hiking", "members": [1, "Mica Peak"]}.

    Returns (all pages after the edit, what the operation did). Raises EditError
    and leaves ``pages`` untouched if the operation is invalid.
    """
    if not isinstance(op, dict) or op.get("op") not in OPERATIONS:
        raise EditError(f"unknown operation {op.get('op') if isinstance(op, dict) else op!r}; known: {sorted(OPERATIONS)}")
    func, allowed = OPERATIONS[op["op"]]
    args = {k: v for k, v in op.items() if k != "op"}
    unknown = set(args) - allowed
    if unknown:
        raise EditError(f"{op['op']}: unknown arguments {sorted(unknown)}; allowed: {sorted(allowed)}")
    if "with" in args:
        args["with_"] = args.pop("with")
    ed = Editor(pages)
    try:
        info = func(ed, **args)
    except TypeError as e:
        raise EditError(f"{op['op']}: {e}") from e
    after = ed.result()
    problems = codeplug.decode(after).problems
    if problems:
        raise EditError(f"{op['op']} would leave problems: {problems}")
    return after, info


def apply_operations(pages: dict[int, bytes], ops: list[dict]) -> tuple[dict[int, bytes], list[dict]]:
    infos = []
    for i, op in enumerate(ops, 1):
        try:
            pages, info = apply_operation(pages, op)
        except EditError as e:
            raise EditError(f"operation {i} ({op.get('op') if isinstance(op, dict) else op}): {e}") from e
        infos.append(info)
    return pages, infos


def changed_pages(before: dict[int, bytes], after: dict[int, bytes]) -> dict[int, bytes]:
    return {a: d for a, d in after.items() if before.get(a) != d}


def add_zone_with_channels(pages: dict[int, bytes], zone_name: str, channels: list[AnalogChannel]) -> dict[int, bytes]:
    """Append ``channels`` after the last channel and a new zone holding them.

    Returns {page address: new page} for the pages that change. Refuses if a
    zone of that name or a channel of any of those names already exists.
    """
    if not channels:
        raise EditError("no channels to add")
    ed = Editor(pages)
    if any(ed.zone(z)[0] == zone_name for z in range(1, ed.zone_count + 1)):
        raise EditError(f"zone {zone_name!r} already exists")
    existing = ed.channel_names()
    clash = [c.name for c in channels if c.name in existing]
    if clash:
        raise EditError(f"channels already exist: {clash}")
    if len(channels) > codeplug.ZONE_MEMBERS:
        raise EditError(f"a zone holds at most {codeplug.ZONE_MEMBERS} channels")
    after, infos = apply_operations(
        pages,
        [
            {"op": "add_channels", "channels": [c.__dict__ for c in channels]},
            {"op": "add_zone", "name": zone_name, "members": [c.name for c in channels]},
        ],
    )
    return changed_pages(pages, after)
