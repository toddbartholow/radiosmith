"""Offline drafts: a verified backup plus a list of edit operations, for either radio.

A draft never stores edited memory as its source of truth. It stores the
backup it started from (by path and manifest hash), which radio it is for, and
the operations, and replays them to get the edited memory. Writing a draft
replays the same operations on a fresh read of the radio, so harmless
differences since the backup (the radio remembers the selected channel, for
example) do not block it, while any difference in what the edit would change
does: see ``preview_id``.

Each radio has a driver that knows how to load its backup, apply its
operations (dm32.edit, rt950.draftops), decode it and list the memory units a
write would touch (DM-32UV 4 KB pages, RT-950 PRO 4 KB sectors).

Layout of a draft directory (data/drafts/<id>/)::

    draft.json         radio, base backup, its manifest hash, name, operations
    channels.csv       export of the edited channels (written on save)
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radiosmith import backup
from radiosmith.dm32 import codeplug
from radiosmith.dm32.edit import EditError, changed_pages
from radiosmith.dm32.edit import apply_operations as dm32_apply

FORMAT = "radiosmith-draft/1"
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
CSV_FIELDS = [
    "number",
    "name",
    "rx_mhz",
    "tx_mhz",
    "mode",
    "forbid_tx",
    "power",
    "bandwidth_khz",
    "rx_tone",
    "tx_tone",
    "scan_list",
]
RT950_CSV_FIELDS = [
    "number",
    "name",
    "rx_mhz",
    "tx_mhz",
    "power",
    "bandwidth_khz",
    "rx_tone",
    "tx_tone",
    "scan_add",
    "busy_lock",
    "rx_am",
]
RADIOS = {"dm32": "Baofeng DM-32UV", "rt950": "Radtel RT-950 PRO"}


class DraftError(Exception):
    """A draft cannot be created, loaded or changed."""


def drafts_root(data_dir: Path) -> Path:
    return data_dir / "drafts"


def manifest_hash(backup_dir: Path) -> str:
    return hashlib.sha256((backup_dir / "manifest.json").read_bytes()).hexdigest()


def _load_base(data_dir: Path, rel: str, radio: str | None = None) -> backup.Backup:
    path = (data_dir / rel).resolve()
    if not (path / "manifest.json").is_file():
        raise DraftError(f"backup {rel} not found")
    b = backup.load_backup(path)
    if not b.ok:
        raise DraftError(f"backup {rel} has problems: {b.problems}")
    driver = b.manifest.get("radio_driver")
    if driver not in DRIVERS:
        raise DraftError(f"backup {rel} is from an unsupported radio ({driver})")
    if radio is not None and driver != radio:
        raise DraftError(f"backup {rel} is a {RADIOS[driver]} backup, not a {RADIOS[radio]} one")
    return b


def decoded(pages: dict[int, bytes]) -> dict:
    """A DM-32UV codeplug as plain data."""
    cp = codeplug.decode(pages)
    return {
        "channels": [dataclasses.asdict(c) for c in cp.channels],
        "zones": [dataclasses.asdict(z) for z in cp.zones],
        "scan_lists": [dataclasses.asdict(s) for s in cp.scan_lists],
        "problems": cp.problems,
    }


def diff_decoded(a: dict, b: dict) -> dict:
    """Decoded differences between two codeplugs: what a write would change."""
    out: dict = {}
    for kind in ("channels", "zones", "scan_lists"):
        old = {x["number"]: x for x in a[kind]}
        new = {x["number"]: x for x in b[kind]}
        out[f"{kind}_added"] = [new[n] for n in sorted(set(new) - set(old))]
        out[f"{kind}_removed"] = [old[n] for n in sorted(set(old) - set(new))]
        out[f"{kind}_changed"] = [
            {"number": n, "before": old[n], "after": new[n]} for n in sorted(set(old) & set(new)) if old[n] != new[n]
        ]
    return out


def semantic_changes(before: dict[int, bytes], after: dict[int, bytes]) -> dict:
    """DM-32UV page sets: decoded differences."""
    return diff_decoded(decoded(before), decoded(after))


def preview_id(identity: dict, changes: dict) -> str:
    """A short hash naming exactly what a write would change, on which radio model and firmware."""
    key = {"model": identity.get("model"), "firmware": identity.get("firmware"), "changes": changes}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


# -- radio drivers ---------------------------------------------------------------------


class _Dm32:
    radio = "dm32"
    csv_fields = CSV_FIELDS

    def load(self, b: backup.Backup) -> dict[int, bytes]:
        return dict(b.pages)

    def apply(self, state: dict[int, bytes], ops: list[dict]) -> tuple[dict[int, bytes], list[dict]]:
        return dm32_apply(state, ops)

    def decoded(self, state: dict[int, bytes]) -> dict:
        return decoded(state)

    def units(self, before: dict[int, bytes], after: dict[int, bytes]) -> list[dict]:
        return [
            {
                "address": f"{a:06x}",
                "tag": f"{d[-1]:02x}",
                "bytes_changed": sum(x != y for x, y in zip(before[a], d, strict=True)),
            }
            for a, d in sorted(changed_pages(before, after).items())
        ]


class _Rt950:
    radio = "rt950"
    csv_fields = RT950_CSV_FIELDS

    def load(self, b: backup.Backup) -> bytes:
        return (b.path / "image.bin").read_bytes()

    def apply(self, state: bytes, ops: list[dict]) -> tuple[bytes, list[dict]]:
        from radiosmith.rt950.draftops import apply_operations

        return apply_operations(state, ops)

    def decoded(self, state: bytes) -> dict:
        from radiosmith.rt950 import codeplug as rt_codeplug

        return rt_codeplug.decoded(state)

    def units(self, before: bytes, after: bytes) -> list[dict]:
        from radiosmith.rt950.write import SECTOR

        out = []
        for s in range(0, len(after), SECTOR):
            n = sum(x != y for x, y in zip(before[s : s + SECTOR], after[s : s + SECTOR], strict=True))
            if n:
                out.append({"address": f"{s:04x}", "tag": "sector", "bytes_changed": n})
        return out


DRIVERS: dict[str, Any] = {"dm32": _Dm32(), "rt950": _Rt950()}


@dataclass
class Draft:
    id: str
    name: str
    base_backup: str  # relative to the data directory
    base_manifest_sha256: str
    identity: dict
    radio: str = "dm32"
    operations: list[dict] = field(default_factory=list)
    created_utc: str = ""
    updated_utc: str = ""
    # filled by load/replay, not saved: the radio's memory (DM-32UV pages, RT-950 PRO image)
    base: Any = field(default=None, repr=False)
    state: Any = field(default=None, repr=False)
    results: list[dict] = field(default_factory=list, repr=False)

    @property
    def driver(self):
        return DRIVERS[self.radio]

    # -- views ----------------------------------------------------------------

    def changes(self) -> dict:
        return diff_decoded(self.driver.decoded(self.base), self.driver.decoded(self.state))

    def preview(self) -> dict:
        changes = self.changes()
        units = self.driver.units(self.base, self.state)
        return {
            "draft": self.id,
            "radio": self.radio,
            "base_backup": self.base_backup,
            "identity": {k: self.identity.get(k) for k in ("model", "firmware")},
            "preview_id": preview_id(self.identity, changes),
            "pages": units,
            "changes": changes,
            "nothing_to_write": not units,
        }

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "radio": self.radio,
            "base_backup": self.base_backup,
            "identity": {k: self.identity.get(k) for k in ("model", "firmware")},
            "operations": len(self.operations),
            "pages_changed": len(self.driver.units(self.base, self.state)),
            "created_utc": self.created_utc,
            "updated_utc": self.updated_utc,
        }

    def codeplug(self) -> dict:
        return self.driver.decoded(self.state)

    def channels_csv(self) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=self.driver.csv_fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for ch in self.codeplug()["channels"]:
            w.writerow({k: ("" if v is None else v) for k, v in ch.items()})
        return buf.getvalue()

    # -- editing --------------------------------------------------------------

    def replay(self) -> None:
        self.state, self.results = self.driver.apply(self.base, self.operations)

    def apply(self, ops: list[dict]) -> list[dict]:
        """Validate and append operations. On any error nothing is added."""
        if not isinstance(ops, list) or not ops:
            raise DraftError("no operations given")
        state, results = self.driver.apply(self.state, ops)
        self.operations.extend(ops)
        self.state = state
        self.results.extend(results)
        return results

    def undo(self, count: int = 1) -> None:
        if not 1 <= count <= len(self.operations):
            raise DraftError(f"the draft has {len(self.operations)} operations")
        self.operations = self.operations[:-count]
        self.replay()

    # -- storage ----------------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "format": FORMAT,
            "id": self.id,
            "name": self.name,
            "radio": self.radio,
            "base_backup": self.base_backup,
            "base_manifest_sha256": self.base_manifest_sha256,
            "identity": self.identity,
            "operations": self.operations,
            "created_utc": self.created_utc,
            "updated_utc": self.updated_utc,
        }


class DraftStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.root = drafts_root(data_dir)

    def _dir(self, draft_id: str) -> Path:
        if not _ID.match(draft_id):
            raise DraftError(f"bad draft id {draft_id!r}")
        return self.root / draft_id

    def create(self, backup_dir: Path, name: str = "") -> Draft:
        backup_dir = backup_dir.resolve()
        try:
            rel = backup_dir.relative_to(self.data_dir.resolve()).as_posix()
        except ValueError as e:
            raise DraftError("the backup must be inside the data directory") from e
        b = _load_base(self.data_dir, rel)
        radio = b.manifest["radio_driver"]
        now = datetime.now(UTC).isoformat(timespec="seconds")
        out = backup.new_timestamped_dir(self.root)
        state = DRIVERS[radio].load(b)
        d = Draft(
            id=out.name,
            name=name or f"Draft of {backup_dir.name}",
            base_backup=rel,
            base_manifest_sha256=manifest_hash(b.path),
            identity=b.manifest.get("identity", {}),
            radio=radio,
            created_utc=now,
            updated_utc=now,
            base=state,
            state=state,
        )
        self.save(d)
        return d

    def save(self, d: Draft) -> None:
        d.updated_utc = datetime.now(UTC).isoformat(timespec="seconds")
        path = self._dir(d.id)
        path.mkdir(parents=True, exist_ok=True)
        tmp = path / "draft.json.tmp"
        tmp.write_text(json.dumps(d.to_json(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(path / "draft.json")
        (path / "channels.csv").write_text(d.channels_csv(), encoding="utf-8")

    def load(self, draft_id: str) -> Draft:
        path = self._dir(draft_id) / "draft.json"
        if not path.is_file():
            raise DraftError(f"no draft {draft_id!r}")
        j = json.loads(path.read_text(encoding="utf-8"))
        if j.get("format") != FORMAT:
            raise DraftError(f"draft {draft_id}: unknown format {j.get('format')!r}")
        radio = j.get("radio", "dm32")  # drafts made before RT-950 PRO support are DM-32UV drafts
        b = _load_base(self.data_dir, j["base_backup"], radio)
        if manifest_hash(b.path) != j["base_manifest_sha256"]:
            raise DraftError(f"draft {draft_id}: its base backup changed since the draft was made")
        d = Draft(
            **{k: j[k] for k in ("id", "name", "base_backup", "base_manifest_sha256", "identity", "operations")}, radio=radio
        )
        d.created_utc, d.updated_utc = j.get("created_utc", ""), j.get("updated_utc", "")
        d.base = DRIVERS[radio].load(b)
        try:
            d.replay()
        except EditError as e:
            raise DraftError(f"draft {draft_id} no longer applies to its backup: {e}") from e
        return d

    def list(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.iterdir(), reverse=True):
            if (p / "draft.json").is_file():
                try:
                    out.append(self.load(p.name).summary())
                except (DraftError, OSError, json.JSONDecodeError, KeyError) as e:
                    out.append({"id": p.name, "error": str(e)})
        return out

    def delete(self, draft_id: str) -> None:
        path = self._dir(draft_id)
        if not (path / "draft.json").is_file():
            raise DraftError(f"no draft {draft_id!r}")
        for f in path.iterdir():
            f.unlink()
        path.rmdir()


# -- CSV import ------------------------------------------------------------------------


def operations_from_csv(text: str, current: dict, radio: str = "dm32") -> list[dict]:
    """Turn an edited channels CSV (as exported) into operations against ``current`` (a decoded codeplug).

    Rows with a number update that channel's changed fields; rows without one
    are added; channels missing from the CSV are deleted. DM-32UV: mode cannot
    change by CSV except to replace a channel with an analog one by setting
    mode to analog. RT-950 PRO: a row whose number is an empty slot puts a new
    channel there.
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise DraftError("the CSV has no rows")
    missing = {"name", "rx_mhz"} - set(rows[0])
    if missing:
        raise DraftError(f"the CSV needs columns {sorted(missing)}")
    try:
        if radio == "rt950":
            return _rt950_ops_from_csv(rows, current)
        return _dm32_ops_from_csv(rows, current)
    except ValueError as e:
        raise DraftError(f"bad number in the CSV: {e}") from e


def _bool(v: str) -> bool:
    return v.strip().lower() in ("true", "1", "yes", "y")


def _dm32_ops_from_csv(rows: list[dict], current: dict) -> list[dict]:
    by_number = {c["number"]: c for c in current["channels"]}

    def parse(row: dict) -> dict:
        out: dict = {}
        for k, v in row.items():
            if k not in CSV_FIELDS or k in ("number", "mode"):
                continue
            v = (v or "").strip()
            if k in ("rx_mhz", "tx_mhz", "bandwidth_khz"):
                out[k] = float(v) if v else None
            elif k == "forbid_tx":
                out[k] = _bool(v)
            elif k == "scan_list":
                out[k] = int(v) if v else 0
            else:
                out[k] = v or None
        return out

    ops: list[dict] = []
    adds: list[dict] = []
    seen: set[int] = set()
    for row in rows:
        num = (row.get("number") or "").strip()
        fields_ = parse(row)
        if not num:
            adds.append({k: v for k, v in fields_.items() if v is not None or k == "tx_mhz"})
            continue
        n = int(num)
        if n not in by_number:
            raise DraftError(f"row for channel {n}: no such channel (leave number empty to add one)")
        seen.add(n)
        cur = by_number[n]
        if cur["mode"] != "analog" and (row.get("mode") or "").strip() == "analog":
            ops.append(
                {
                    "op": "replace_channel",
                    "channel": n,
                    "with": {k: v for k, v in fields_.items() if v is not None or k == "tx_mhz"},
                }
            )
            continue
        changes = {
            k: v
            for k, v in fields_.items()
            if k in cur and cur[k] != v and not (k in ("rx_tone", "tx_tone") and cur["mode"] != "analog")
        }
        if changes:
            ops.append({"op": "update_channel", "channel": n, "changes": changes})
    gone = sorted(set(by_number) - seen)
    if adds:
        ops.append({"op": "add_channels", "channels": adds})
    if gone:
        ops.append({"op": "delete_channels", "channels": gone})
    return ops


def _rt950_ops_from_csv(rows: list[dict], current: dict) -> list[dict]:
    by_number = {c["number"]: c for c in current["channels"]}

    def parse(row: dict) -> dict:
        out: dict = {}
        for k in RT950_CSV_FIELDS:
            if k == "number" or k not in row:
                continue
            v = (row[k] or "").strip()
            if k in ("rx_mhz", "tx_mhz", "bandwidth_khz"):
                out[k] = float(v) if v else None
            elif k in ("scan_add", "busy_lock", "rx_am"):
                out[k] = _bool(v)
            elif k == "name":
                out[k] = v
            else:
                out[k] = v or None
        return out

    ops: list[dict] = []
    adds: list[dict] = []
    seen: set[int] = set()
    for row in rows:
        num = (row.get("number") or "").strip()
        fields_ = parse(row)
        new = {k: v for k, v in fields_.items() if v is not None or k in ("tx_mhz", "rx_tone", "tx_tone")}
        if not num:
            adds.append(new)
            continue
        n = int(num)
        seen.add(n)
        if n not in by_number:
            ops.append({"op": "add_channels", "channels": [new], "at": n})
            continue
        cur = by_number[n]
        changes = {k: v for k, v in fields_.items() if k in cur and cur[k] != v}
        if changes:
            ops.append({"op": "update_channel", "channel": n, "changes": changes})
    gone = sorted(set(by_number) - seen)
    if gone:
        ops.insert(0, {"op": "delete_channels", "channels": gone})
    if adds:
        ops.append({"op": "add_channels", "channels": adds})
    return ops
