"""RT-950 PRO operations: identify and a full read into a backup.

The backup holds the image as one file (``image.bin``, addresses 0x0000 up to
the end of READ_RANGES, decrypted) plus ``aprs.bin``, with SHA-256 hashes in the
manifest like DM-32UV backups.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from radiosmith import backup
from radiosmith.dm32.ops import Context, ReadFailed, _port_record, _read_state, _write_state
from radiosmith.rt950 import protocol as p
from radiosmith.rt950.session import RT950Session
from radiosmith.transport import TrafficLog

MIN_SESSION_GAP = 2.0
STATE_KEY = "rt950_last_session_end"


@contextmanager
def open_session(ctx: Context, log: TrafficLog) -> Iterator[RT950Session]:
    if ctx.enforce_gap:
        last = _read_state(ctx).get(STATE_KEY)
        if last is not None and (wait := MIN_SESSION_GAP - (time.time() - last)) > 0:
            ctx.sleep(wait)
    link = ctx.link_factory(ctx.port.device, ctx.lines, log)
    try:
        ctx.sleep(ctx.settle)
        yield RT950Session(link, sleep=ctx.sleep)
    finally:
        link.close()
        _write_state(ctx, **{STATE_KEY: time.time()})


def identify(ctx: Context, log: TrafficLog) -> dict:
    with open_session(ctx, log) as s:
        ident = s.identify()
        s.end()
        return {"identity": ident.to_dict(), "port": _port_record(ctx), "link_stats": dict(s.stats)}


def read_image(ctx: Context, *, copies: int = 3) -> tuple[Path, dict]:
    """Read the whole image into a new backup directory. Returns (backup dir, summary)."""
    root = ctx.data_dir / "backups" / "rt950"
    staging = ctx.data_dir / "backups" / ".incoming"
    staging.mkdir(parents=True, exist_ok=True)
    log_path = staging / f"traffic-rt950-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"
    log = TrafficLog.to_file(log_path)
    image = bytearray()
    aprs = b""
    ident: dict = {}
    stats: dict = {}
    error: Exception | None = None
    session: RT950Session | None = None
    end = max(e for _, e in p.READ_RANGES)
    try:
        with open_session(ctx, log) as s:
            session = s
            ident = s.identify().to_dict()
            ctx.say(f"Radio {ident['model']}; reading {end:#06x} bytes...")
            s.start_reads()
            for addr in range(0, end, p.BLOCK):
                image += s.read_block(p.read_frame(addr), copies=copies)
                if addr % 0x1000 == 0:
                    ctx.say(f"  {addr:#06x}")
            aprs = s.read_block(p.APRS_READ, copies=copies)
            s.end()
            stats = dict(s.stats)
    except Exception as e:  # saved as an incomplete backup, then re-raised
        error = e
    finally:
        log.close()
    if not ident:
        assert error is not None
        raise ReadFailed(f"{error} (traffic log: {log_path})", None) from error
    out = backup.new_backup_dir(root, ident["model"], None)
    log_path.replace(out / "traffic.jsonl")
    extra = {"image.bin": bytes(image)}
    if aprs:
        extra["aprs.bin"] = aprs
    backup.write_backup(
        out,
        radio_driver="rt950",
        identity=ident,
        port=_port_record(ctx),
        line_state=ctx.lines,
        page_tags={},
        pages={},
        extra_files=extra,
        link_stats=stats or (dict(session.stats) if session else {}),
        complete=error is None,
        notes=[] if error is None else [f"read failed at stage {session.stage if session else 'open'}: {error}"],
    )
    if error is not None:
        raise ReadFailed(str(error), out) from error
    return out, {"identity": ident, "bytes": len(image), "aprs_bytes": len(aprs), "link_stats": stats}


# -- guarded sector writes ------------------------------------------------------------


class WriteAborted(Exception):
    """A pre-write check failed; no write frame was sent."""


def run_sector_writes(
    ctx: Context,
    *,
    label: str,
    plan,
    really_write: bool,
    copies: int = 3,
    rewrite: tuple[int, ...] = (),
    expect_identity: dict | None = None,
) -> tuple[Path, dict]:
    """Back up, plan, check, write, read back, back up again and compare.

    ``plan`` takes the image from a fresh backup and returns the new image.
    Every 4 KB sector with a change is written whole (the radio erases a sector
    when its first block is written); each must be a writable sector and must
    still hold what the backup holds when the write session starts.
    ``rewrite`` names sectors to write even if unchanged (the no-op test).
    """
    import json

    from radiosmith.rt950 import codeplug
    from radiosmith.rt950.write import SECTOR, RT950WriteSession, UnsafeWrite, WriteUncertain, sector_of, writable

    out = backup.new_timestamped_dir(ctx.data_dir / "writes")
    report: dict = {"radio": "rt950", "label": label, "really_write": really_write, "steps": []}

    def save() -> None:
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def step(name: str, **info) -> None:
        report["steps"].append({"step": name, **info})
        save()

    ctx.say("Step 1: full backup before writing")
    before_dir, _ = read_image(ctx, copies=copies)
    before_backup = backup.load_backup(before_dir)
    if not before_backup.ok:
        raise WriteAborted(f"fresh backup has problems: {before_backup.problems}")
    image = (before_dir / "image.bin").read_bytes()
    step("backup_before", backup=before_dir.as_posix())
    model = before_backup.manifest["identity"].get("model")
    if expect_identity is not None and expect_identity.get("model") != model:
        raise WriteAborted(f"radio model {model} differs from the draft's {expect_identity.get('model')}")
    new_image = plan(image)
    if len(new_image) != len(image):
        raise WriteAborted("the plan changed the image size")
    changed = {sector_of(i) for i in range(len(image)) if new_image[i] != image[i]}
    sectors = sorted(changed | set(rewrite))
    if not sectors:
        raise WriteAborted("nothing to write")
    for s in sectors:
        if not writable(s):
            raise WriteAborted(f"sector {s:#06x} is not writable (writable: 0x0000-0x6FFF)")
    old = {c.number: c.__dict__ for c in codeplug.decode_channels(image)}
    new = {c.number: c.__dict__ for c in codeplug.decode_channels(new_image)}
    report["preview"] = {
        "sectors": [f"{s:04x}" for s in sectors],
        "channels_added": [new[n] for n in sorted(set(new) - set(old))],
        "channels_changed": [{"before": old[n], "after": new[n]} for n in sorted(set(new) & set(old)) if new[n] != old[n]],
        "channels_removed": [old[n] for n in sorted(set(old) - set(new))],
    }
    step("plan", sectors=report["preview"]["sectors"])

    ctx.say(f"Step 2: write session for {len(sectors)} sector(s)")
    log = TrafficLog.to_file(out / "traffic.jsonl")
    results: list[dict] = []
    try:
        with open_session(ctx, log) as s:
            ws = RT950WriteSession(s.link, sleep=ctx.sleep)
            ident = ws.identify().to_dict()
            if ident != before_backup.manifest["identity"]:
                raise WriteAborted(f"radio identity {ident} differs from the backup's")
            ws.start_reads()
            for sec in sectors:
                for a in range(sec, sec + SECTOR, p.BLOCK):
                    if ws.read_block(p.read_frame(a), copies=copies) != image[a : a + p.BLOCK]:
                        raise WriteAborted(f"block {a:#06x} on the radio differs from the backup")
            step("pre_write_checks", identity_match=True, sectors_match=True)
            if not really_write:
                ws.end()
                step("dry_run", note="checks passed; no W frame sent")
                return out, report
            for sec in sectors:
                r = ws.write_sector(sec, new_image[sec : sec + SECTOR], copies=copies)
                results.append(r)
                step("write", **r)
                if not r["verified"]:
                    raise WriteUncertain(f"sector {sec:04x} read back differently after the write")
            ws.end()
            step("end_session")
    except (WriteAborted, WriteUncertain, UnsafeWrite, p.ProtocolError) as e:
        step("error", error=f"{type(e).__name__}: {e}", sectors_written=[r["sector"] for r in results])
        raise
    finally:
        log.close()

    ctx.say("Step 3: full backup after writing, compared with what was planned")
    after_dir, _ = read_image(ctx, copies=copies)
    after = (after_dir / "image.bin").read_bytes()
    differ = [f"{a:04x}" for a in range(0, len(after), p.BLOCK) if after[a : a + p.BLOCK] != new_image[a : a + p.BLOCK]]
    aprs_same = (after_dir / "aprs.bin").read_bytes() == (before_dir / "aprs.bin").read_bytes()
    step("backup_after", backup=after_dir.as_posix(), matches_plan=not differ, blocks_differ=differ, aprs_unchanged=aprs_same)
    report["passed"] = bool(all(r["verified"] for r in results) and not differ and aprs_same)
    save()
    return out, report


def noop_write_test(ctx: Context, *, sector: int, really_write: bool, copies: int = 3) -> tuple[Path, dict]:
    """Rewrite one whole sector with its own contents (changes nothing)."""
    return run_sector_writes(
        ctx,
        label=f"noop rewrite of sector {sector:04x}",
        plan=lambda image: image,
        really_write=really_write,
        copies=copies,
        rewrite=(sector,),
    )


def restore_sectors(ctx: Context, source: Path, sectors: list[int], *, really_write: bool, copies: int = 3) -> tuple[Path, dict]:
    """Write ``sectors`` back as they are in the backup at ``source``."""
    from radiosmith.rt950.write import SECTOR

    b = backup.load_backup(source)
    if not b.ok or b.manifest.get("radio_driver") != "rt950":
        raise WriteAborted(f"{source} is not a good RT-950 PRO backup: {b.problems}")
    old = (source / "image.bin").read_bytes()

    def plan(image: bytes) -> bytes:
        out = bytearray(image)
        for s in sectors:
            out[s : s + SECTOR] = old[s : s + SECTOR]
        return bytes(out)

    label = f"restore sectors {', '.join(f'{s:04x}' for s in sectors)} from {source.name}"
    return run_sector_writes(ctx, label=label, plan=plan, really_write=really_write, copies=copies, rewrite=tuple(sectors))


def load_profile(path: Path) -> dict:
    """Read a channel profile (JSON) and check its channels as RT-950 PRO channels."""
    import json

    from radiosmith.rt950.draftops import profile_channels

    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["rt950_channels"] = profile_channels(profile["channels"])
    return profile


def apply_profile(
    ctx: Context, profile: dict, *, really_write: bool, copies: int = 3, at: int | None = None
) -> tuple[Path, dict]:
    """Add a profile's channels (from load_profile) into empty slots from ``at``, or after the last used channel."""
    from radiosmith.rt950.draftops import apply_operation

    op = {"op": "add_channels", "channels": profile["rt950_channels"]} | ({"at": at} if at is not None else {})

    def plan(image: bytes) -> bytes:
        return apply_operation(image, op)[0]

    return run_sector_writes(ctx, label=f"add {profile['name']!r} channels", plan=plan, really_write=really_write, copies=copies)


def write_draft(ctx: Context, draft, *, approved_preview_id: str, really_write: bool, copies: int = 3) -> tuple[Path, dict]:
    """Write an RT-950 PRO draft: replay its operations on a fresh read and write the changed sectors.

    Refuses unless the replayed changes are exactly the approved preview's.
    """
    import json

    from radiosmith.dm32.draft import diff_decoded, preview_id
    from radiosmith.dm32.edit import EditError
    from radiosmith.rt950 import codeplug
    from radiosmith.rt950.draftops import apply_operations

    if draft.radio != "rt950":
        raise WriteAborted("not an RT-950 PRO draft")
    if approved_preview_id != draft.preview()["preview_id"]:
        raise WriteAborted("the approved preview is not this draft's current preview; preview it again")

    def plan(image: bytes) -> bytes:
        try:
            after, _ = apply_operations(image, draft.operations)
        except EditError as e:
            raise WriteAborted(f"the draft no longer applies to the radio: {e}") from e
        changes = diff_decoded(codeplug.decoded(image), codeplug.decoded(after))
        if preview_id(draft.identity, changes) != approved_preview_id:
            raise WriteAborted("on the radio this draft would change something other than the approved preview")
        return after

    out, report = run_sector_writes(
        ctx,
        label=f"draft {draft.id} ({draft.name})",
        plan=plan,
        really_write=really_write,
        copies=copies,
        expect_identity=draft.identity,
    )
    report["draft"] = draft.id
    report["preview_id"] = approved_preview_id
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return out, report
