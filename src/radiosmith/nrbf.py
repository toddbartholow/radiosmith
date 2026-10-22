"""A minimal reader for .NET Binary Format (MS-NRBF) streams.

Vendor CPS programs written in .NET (for example the Radtel RT-950 PRO CPS)
save their files with BinaryFormatter. This turns such a stream into plain
Python values: classes become dicts (with the class name under "__class__"),
arrays become lists, primitives become ints, floats, bools and strings.

Written from the published MS-NRBF specification; read-only.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any


class NrbfError(Exception):
    """The stream is not valid MS-NRBF, or uses a record this reader does not handle."""


# PrimitiveTypeEnumeration -> struct format (None: special handling)
_PRIM = {
    1: "?",
    2: "B",
    3: None,
    5: None,
    6: "<d",
    7: "<h",
    8: "<i",
    9: "<q",
    10: "b",
    11: "<f",
    12: "<q",
    13: "<q",
    14: "<H",
    15: "<I",
    16: "<Q",
    17: None,
    18: None,
}


@dataclass
class _Ref:
    id: int


@dataclass
class _ClassMeta:
    name: str
    members: list[str]
    types: list[tuple[int, Any]] | None  # (BinaryTypeEnum, additional info) per member


@dataclass
class _Reader:
    data: bytes
    pos: int = 0
    objects: dict[int, Any] = field(default_factory=dict)
    metas: dict[int, _ClassMeta] = field(default_factory=dict)
    root_id: int | None = None

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise NrbfError(f"unexpected end of stream at {self.pos}")
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def u8(self) -> int:
        return self.take(1)[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def lps(self) -> str:
        n = shift = 0
        while True:
            b = self.u8()
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
            if shift > 35:
                raise NrbfError("bad string length")
        return self.take(n).decode("utf-8", errors="replace")

    def primitive(self, ptype: int) -> Any:
        if ptype == 3:  # Char: one UTF-8 character
            first = self.data[self.pos]
            n = 1 if first < 0x80 else 2 if first < 0xE0 else 3 if first < 0xF0 else 4
            return self.take(n).decode("utf-8", errors="replace")
        if ptype in (5, 18):  # Decimal, String
            return self.lps()
        if ptype == 17:
            return None
        fmt = _PRIM.get(ptype)
        if fmt is None:
            raise NrbfError(f"unknown primitive type {ptype}")
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    # -- records --------------------------------------------------------------------

    def class_info(self) -> tuple[int, str, list[str]]:
        oid, name, count = self.i32(), self.lps(), self.i32()
        return oid, name, [self.lps() for _ in range(count)]

    def member_types(self, count: int) -> list[tuple[int, Any]]:
        kinds = [self.u8() for _ in range(count)]
        out = []
        for k in kinds:
            if k in (0, 7):
                out.append((k, self.u8()))
            elif k == 3:
                out.append((k, self.lps()))
            elif k == 4:
                out.append((k, (self.lps(), self.i32())))
            else:
                out.append((k, None))
        return out

    def values(self, meta: _ClassMeta) -> dict:
        obj: dict = {"__class__": meta.name}
        for i, name in enumerate(meta.members):
            kind, info = meta.types[i] if meta.types else (2, None)
            obj[name] = self.primitive(info) if kind == 0 else self.record_value()
        return obj

    def record_value(self) -> Any:
        """Read one record that stands for a value (skipping library records)."""
        while True:
            rtype = self.u8()
            if rtype == 12:  # BinaryLibrary
                self.i32()
                self.lps()
                continue
            return self.record(rtype)

    def record(self, rtype: int) -> Any:
        if rtype in (4, 5, 2, 3):  # classes with members (and types)
            oid, name, members = self.class_info()
            types = self.member_types(len(members)) if rtype in (4, 5) else None
            if rtype in (3, 5):
                self.i32()  # library id
            meta = _ClassMeta(name, members, types)
            self.metas[oid] = meta
            obj = self.values(meta)
            self.objects[oid] = obj
            return obj
        if rtype == 1:  # ClassWithId
            oid, meta_id = self.i32(), self.i32()
            if meta_id not in self.metas:
                raise NrbfError(f"class metadata {meta_id} not seen")
            obj = self.values(self.metas[meta_id])
            self.objects[oid] = obj
            return obj
        if rtype == 6:  # BinaryObjectString
            oid, s = self.i32(), self.lps()
            self.objects[oid] = s
            return s
        if rtype == 8:  # MemberPrimitiveTyped
            return self.primitive(self.u8())
        if rtype == 9:  # MemberReference
            return _Ref(self.i32())
        if rtype == 10:
            return None
        if rtype in (13, 14):  # ObjectNullMultiple(256)
            return _Nulls(self.u8() if rtype == 13 else self.i32())
        if rtype == 15:  # ArraySinglePrimitive
            oid, n, ptype = self.i32(), self.i32(), self.u8()
            arr = self.take(n) if ptype == 2 else [self.primitive(ptype) for _ in range(n)]
            self.objects[oid] = arr
            return arr
        if rtype in (16, 17):  # ArraySingleObject / ArraySingleString
            oid, n = self.i32(), self.i32()
            arr = self.elements(n, None)
            self.objects[oid] = arr
            return arr
        if rtype == 7:  # BinaryArray
            oid, atype, rank = self.i32(), self.u8(), self.i32()
            lengths = [self.i32() for _ in range(rank)]
            if atype in (3, 4, 5):
                [self.i32() for _ in range(rank)]  # lower bounds
            ((kind, info),) = self.member_types(1)
            total = 1
            for n in lengths:
                total *= n
            if kind == 0:
                arr: Any = [self.primitive(info) for _ in range(total)]
            else:
                arr = self.elements(total, None)
            self.objects[oid] = arr
            return arr
        if rtype == 0:  # SerializedStreamHeader
            self.root_id = self.i32()
            self.i32(), self.i32(), self.i32()
            return None
        raise NrbfError(f"unsupported record type {rtype} at {self.pos - 1}")

    def elements(self, n: int, _: Any) -> list:
        out: list = []
        while len(out) < n:
            v = self.record_value()
            if isinstance(v, _Nulls):
                out.extend([None] * v.count)
            else:
                out.append(v)
        return out


@dataclass
class _Nulls:
    count: int


def _resolve(value: Any, objects: dict[int, Any], seen: set[int]) -> Any:
    if isinstance(value, _Ref):
        if value.id in seen:
            return {"__ref__": value.id}
        target = objects.get(value.id)
        return _resolve(target, objects, seen | {value.id})
    if isinstance(value, dict):
        for k, v in value.items():
            value[k] = _resolve(v, objects, seen)
        return value
    if isinstance(value, list):
        for i, v in enumerate(value):
            value[i] = _resolve(v, objects, seen)
        return value
    return value


def load(data: bytes) -> Any:
    """Parse an MS-NRBF stream and return its root object."""
    r = _Reader(data)
    while r.pos < len(data):
        rtype = r.u8()
        if rtype == 11:  # MessageEnd
            break
        if rtype == 12:
            r.i32()
            r.lps()
            continue
        r.record(rtype)
    if r.root_id is None or r.root_id not in r.objects:
        raise NrbfError("no root object")
    return _resolve(r.objects[r.root_id], r.objects, set())
