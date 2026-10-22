"""Backup store: raw radio pages plus a manifest with identity and hashes.

Layout of one backup directory::

    manifest.json      identity, port, page map, SHA-256 of every file
    g_block.bin        the 256-byte G block (DM-32UV)
    pages/<addr>_tag<tag>.bin
    traffic.jsonl      the session's traffic log

Backups hold personal data (DMR IDs, contacts, any passwords in clear text),
so they stay local and out of version control.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from radiosmith import __version__

FORMAT = "radiosmith-backup/1"
# Backups made before the project was renamed from radio-mcp; same layout.
READABLE_FORMATS = {FORMAT, "radio-mcp-backup/1"}
_PAGE_NAME = re.compile(r"^([0-9a-f]{6})_tag([0-9a-f]{2})\.bin$")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_timestamped_dir(root: Path, suffix: str = "") -> Path:
    """Create root/<UTC time><suffix>, adding -2, -3... if that name is taken."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        path = root / f"{stamp}{'' if n == 1 else f'-{n}'}{suffix}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            n += 1


def new_backup_dir(root: Path, model: str, firmware: str | None) -> Path:
    label = re.sub(r"[^A-Za-z0-9.]+", "-", f"{model}_{firmware or 'unknown'}")
    path = new_timestamped_dir(root, f"_{label}")
    (path / "pages").mkdir()
    return path


def write_backup(
    path: Path,
    *,
    radio_driver: str,
    identity: dict,
    port: dict,
    line_state: str,
    page_tags: dict[int, int],
    pages: dict[int, bytes],
    extra_files: dict[str, bytes],
    link_stats: dict,
    complete: bool,
    notes: list[str],
) -> dict:
    """Write pages and the manifest. Called also for partial reads (complete=False)."""
    files = {}
    for addr, data in sorted(pages.items()):
        name = f"pages/{addr:06x}_tag{data[-1]:02x}.bin"
        (path / name).write_bytes(data)
        files[name] = sha256(data)
    for name, data in extra_files.items():
        (path / name).write_bytes(data)
        files[name] = sha256(data)
    manifest = {
        "format": FORMAT,
        "tool_version": __version__,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "radio_driver": radio_driver,
        "complete": complete,
        "identity": identity,
        "port": port,
        "line_state": line_state,
        "page_tags": {f"{a:06x}": f"{t:02x}" for a, t in sorted(page_tags.items())},
        "pages_saved": len(pages),
        "files": files,
        "link_stats": link_stats,
        "notes": notes,
    }
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


@dataclass
class Backup:
    path: Path
    manifest: dict
    pages: dict[int, bytes]
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def diff_pages(old: dict[int, bytes], new: dict[int, bytes]) -> dict:
    """Compare two backups by page tag, since the radio moves pages around.

    Stale (0x00) and free pages are compared by count only: their placement
    changes whenever the radio reorganises its flash.
    """

    def by_tag(pages: dict[int, bytes]) -> dict[int, tuple[int, bytes]]:
        return {d[-1]: (a, d) for a, d in sorted(pages.items()) if d[-1] not in (0x00, 0xFF)}

    a, b = by_tag(old), by_tag(new)
    changed = {}
    for tag in sorted(set(a) & set(b)):
        (addr_a, da), (addr_b, db) = a[tag], b[tag]
        if da != db:
            offsets = [i for i in range(len(da)) if da[i] != db[i]]
            changed[f"{tag:02x}"] = {
                "old_addr": f"{addr_a:06x}",
                "new_addr": f"{addr_b:06x}",
                "bytes_differ": len(offsets),
                "first_offsets": [f"{o:03x}" for o in offsets[:16]],
            }
    moved = {f"{t:02x}": [f"{a[t][0]:06x}", f"{b[t][0]:06x}"] for t in sorted(set(a) & set(b)) if a[t][0] != b[t][0]}
    return {
        "identical": not changed and set(a) == set(b),
        "changed": changed,
        "only_in_old": [f"{t:02x}" for t in sorted(set(a) - set(b))],
        "only_in_new": [f"{t:02x}" for t in sorted(set(b) - set(a))],
        "moved": moved,
        "stale_pages": [sum(d[-1] == 0x00 for d in old.values()), sum(d[-1] == 0x00 for d in new.values())],
    }


def load_backup(path: Path) -> Backup:
    """Load a backup and check every file against the manifest's hashes."""
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    if manifest.get("format") not in READABLE_FORMATS:
        problems.append(f"unknown format {manifest.get('format')!r}")
    if not manifest.get("complete"):
        problems.append("backup is marked incomplete (the read did not finish)")
    pages: dict[int, bytes] = {}
    for name, digest in manifest.get("files", {}).items():
        file = path / name
        if not file.is_file():
            problems.append(f"missing file {name}")
            continue
        data = file.read_bytes()
        if sha256(data) != digest:
            problems.append(f"hash mismatch in {name}")
            continue
        m = _PAGE_NAME.match(Path(name).name)
        if name.startswith("pages/") and m:
            addr, tag = int(m.group(1), 16), int(m.group(2), 16)
            if data[-1] != tag:
                problems.append(f"{name}: tag byte {data[-1]:#04x} does not match file name")
            pages[addr] = data
    for addr_hex, tag_hex in manifest.get("page_tags", {}).items():
        if tag_hex != "ff" and int(addr_hex, 16) not in pages:
            problems.append(f"page {addr_hex} (tag {tag_hex}) is in use but was not saved")
    return Backup(path=path, manifest=manifest, pages=pages, problems=problems)
