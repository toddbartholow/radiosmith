"""DM-32UV operations: identify, full read to a backup, and connection diagnostics.

These are the entry points the CLI uses now and the service and MCP adapter
will use later. Each opens the port, runs one or more sessions and always
closes the port again.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from radiosmith import backup
from radiosmith.dm32 import codeplug
from radiosmith.dm32 import protocol as p
from radiosmith.dm32.session import DM32Session
from radiosmith.transport import Link, PortInfo, SerialLink, TrafficLog

SETTLE = 0.5  # seconds after opening the port before the first command
# The radio leaves programming mode 2 s after its last request, and the CHIRP
# work found 5 s between sessions to be enough to avoid a radio that stops
# answering. Sessions started sooner than this wait first.
MIN_SESSION_GAP = 5.0
NEONPLUG_REOPEN_DELAY = 0.4  # NeonPlug's CONNECTION.REOPEN_DELAY

LinkFactory = Callable[[str, str, TrafficLog], Link]


def serial_link_factory(device: str, lines: str, log: TrafficLog) -> Link:
    return SerialLink(device, baud=p.BAUD, lines=lines, log=log)


@dataclass
class Context:
    """Where files go and how to reach the radio."""

    port: PortInfo
    data_dir: Path
    lines: str = "cps"
    settle: float = SETTLE
    link_factory: LinkFactory = serial_link_factory
    sleep: Callable[[float], None] = time.sleep
    say: Callable[[str], None] = print
    enforce_gap: bool = True

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"


def _read_state(ctx: Context) -> dict:
    try:
        return json.loads(ctx.state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(ctx: Context, **changes) -> None:
    state = _read_state(ctx) | changes
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    ctx.state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _wait_for_gap(ctx: Context, log: TrafficLog) -> None:
    """Keep sessions at least MIN_SESSION_GAP apart, across processes."""
    if not ctx.enforce_gap:
        return
    last = _read_state(ctx).get("dm32_last_session_end")
    if last is None:
        return
    wait = MIN_SESSION_GAP - (time.time() - last)
    if wait > 0:
        log.event("gap_wait", seconds=round(wait, 2))
        ctx.say(f"Waiting {wait:.1f} s since the previous session ended...")
        ctx.sleep(wait)


@contextmanager
def open_session(
    ctx: Context, log: TrafficLog, *, lines: str | None = None, session_cls: type[DM32Session] = DM32Session
) -> Iterator[DM32Session]:
    _wait_for_gap(ctx, log)
    link = ctx.link_factory(ctx.port.device, lines or ctx.lines, log)
    try:
        ctx.sleep(ctx.settle)
        yield session_cls(link, sleep=ctx.sleep)
    finally:
        link.close()
        _write_state(ctx, dm32_last_session_end=time.time())


def _port_record(ctx: Context, lines: str | None = None) -> dict:
    return ctx.port.to_dict() | {"lines": lines or ctx.lines}


# -- identify ------------------------------------------------------------------


def identify(ctx: Context, log: TrafficLog) -> dict:
    with open_session(ctx, log) as s:
        ident = s.identify()
        return {"identity": ident.to_dict(), "port": _port_record(ctx), "link_stats": dict(s.stats)}


# -- full read -------------------------------------------------------------------


@dataclass
class ReadResult:
    backup_dir: Path
    identity: dict
    pages_in_use: int
    pages_saved: int
    link_stats: dict
    summary: dict = field(default_factory=dict)


def read_codeplug(ctx: Context, *, all_pages: bool = False, copies: int = 3) -> ReadResult:
    """Read the whole codeplug area into a new backup directory.

    Saves every page in use (tag != 0xFF), including stale and unknown pages,
    so nothing the radio holds is lost; ``all_pages`` saves free pages too.
    A failed read still leaves a backup marked incomplete, with the error.
    """
    root = ctx.data_dir / "backups" / "dm32"
    staging = ctx.data_dir / "backups" / ".incoming"
    staging.mkdir(parents=True, exist_ok=True)
    log_path = staging / f"traffic-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"
    log = TrafficLog.to_file(log_path)
    pages: dict[int, bytes] = {}
    tags: dict[int, int] = {}
    extra: dict[str, bytes] = {}
    ident_dict: dict = {}
    session: DM32Session | None = None
    error: Exception | None = None
    try:
        with open_session(ctx, log) as s:
            session = s
            ident = s.identify()
            ident_dict = ident.to_dict()
            ctx.say(f"Radio {ident.model}, firmware {ident.firmware}, built {ident.build_date}")
            if ident.codeplug_range is None:
                raise p.ProtocolError("radio did not report its codeplug range (V10)")
            extra["g_block.bin"] = s.enter_program_mode()
            ctx.say("In programming mode; scanning page tags...")
            tags = s.scan_page_tags(copies=copies)
            wanted = sorted(a for a, t in tags.items() if all_pages or t != p.TAG_FREE)
            ctx.say(f"{sum(t != p.TAG_FREE for t in tags.values())} of {len(tags)} pages in use; reading {len(wanted)}...")
            for i, addr in enumerate(wanted, 1):
                data = s.read_page(addr, copies=copies)
                if data[-1] != tags[addr]:
                    raise p.ProtocolError(f"page {addr:06x}: tag {data[-1]:#04x} differs from scanned tag {tags[addr]:#04x}")
                pages[addr] = data
                if i % 10 == 0 or i == len(wanted):
                    ctx.say(f"  {i}/{len(wanted)} pages")
            s.end()
    except Exception as e:  # saved as an incomplete backup, then re-raised
        error = e
    finally:
        log.close()
    if not ident_dict:
        # Nothing was read from the radio, so there is nothing to back up.
        assert error is not None
        raise ReadFailed(f"{error} (traffic log: {log_path})", None) from error
    out = backup.new_backup_dir(root, ident_dict["model"], ident_dict.get("firmware"))
    log_path.replace(out / "traffic.jsonl")
    notes = [] if error is None else [f"read failed at stage {session.stage if session else 'open'}: {error}"]
    backup.write_backup(
        out,
        radio_driver="dm32",
        identity=ident_dict,
        port=_port_record(ctx),
        line_state=ctx.lines,
        page_tags=tags,
        pages=pages,
        extra_files=extra,
        link_stats=dict(session.stats) if session else {},
        complete=error is None,
        notes=notes,
    )
    if error is not None:
        raise ReadFailed(str(error), out) from error
    assert session is not None
    cp = codeplug.decode(pages)
    return ReadResult(
        backup_dir=out,
        identity=ident_dict,
        pages_in_use=sum(t != p.TAG_FREE for t in tags.values()),
        pages_saved=len(pages),
        link_stats=dict(session.stats),
        summary=summarize(cp),
    )


class ReadFailed(Exception):
    """A read did not finish. ``backup_dir`` holds what was read, if anything."""

    def __init__(self, message: str, backup_dir: Path | None):
        super().__init__(message)
        self.backup_dir = backup_dir


def summarize(cp: codeplug.Codeplug) -> dict:
    return {
        "channel_count_header": cp.channel_count,
        "channels_decoded": len(cp.channels),
        "zones": [{"number": z.number, "name": z.name, "members": len(z.members)} for z in cp.zones],
        "scan_lists": [{"number": s.number, "name": s.name, "members": len(s.members)} for s in cp.scan_lists],
        "problems": cp.problems,
    }


# -- diagnostics -----------------------------------------------------------------


@dataclass
class CaseResult:
    name: str
    description: str
    ok: bool = False
    stage: str = ""
    error: str | None = None
    seconds: float = 0.0
    gap_before: float | None = None
    detail: dict = field(default_factory=dict)


def diagnose(
    ctx: Context,
    *,
    gap: float = MIN_SESSION_GAP,
    fast_reconnect: bool = False,
    sweep_lines: bool = False,
) -> tuple[Path, list[CaseResult]]:
    """Run separate connection cases against the radio without changing its memory.

    Cases, in order:
      1. fresh identify
      2. reconnect and identify after ``gap`` seconds
      3. read session: programming mode and a short page-tag scan
      4. write preparation: the handshake a write session uses, up to (not
         including) the first write frame, ``gap`` seconds after case 3
      5. (opt-in) reconnect 0.4 s after a session, as NeonPlug does, then
         probe recovery at increasing delays
      6. (opt-in) identify with each RTS/DTR preset

    Cases 5 and 6 can leave the radio unresponsive until it is power cycled,
    so they run last and only when asked for.
    """
    out = backup.new_timestamped_dir(ctx.data_dir / "diagnostics")
    log = TrafficLog.to_file(out / "traffic.jsonl")
    results: list[CaseResult] = []
    # The cases set their own gaps; only the first waits for an earlier process.
    gap_ctx = dataclasses.replace(ctx, enforce_gap=False)

    def run(
        name: str,
        description: str,
        body: Callable[[DM32Session], dict],
        *,
        lines: str | None = None,
        gap_before: float | None = None,
    ) -> CaseResult:
        r = CaseResult(name=name, description=description, gap_before=gap_before)
        if gap_before:
            log.event("gap", seconds=gap_before)
            gap_ctx.sleep(gap_before)
        log.event("case_start", case=name, lines=lines or ctx.lines)
        ctx.say(f"[{name}] {description}")
        start = time.monotonic()
        session: DM32Session | None = None
        try:
            with open_session(gap_ctx, log, lines=lines) as s:
                session = s
                r.detail = body(s)
                r.ok = True
        except Exception as e:
            r.error = f"{type(e).__name__}: {e}"
        r.seconds = round(time.monotonic() - start, 2)
        r.stage = session.stage if session else "open"
        if session:
            r.detail["link_stats"] = dict(session.stats)
        log.event("case_end", case=name, ok=r.ok, stage=r.stage, error=r.error)
        ctx.say(f"    -> {'OK' if r.ok else 'FAILED at ' + r.stage + ': ' + str(r.error)} ({r.seconds} s)")
        results.append(r)
        return r

    def do_identify(s: DM32Session) -> dict:
        ident = s.identify()
        return {"model": ident.model, "firmware": ident.firmware, "build_date": ident.build_date, "dsp": ident.dsp_version}

    def do_read_session(s: DM32Session) -> dict:
        s.identify()
        s.enter_program_mode()
        assert s.identity is not None and s.identity.codeplug_range is not None
        start, _ = s.identity.codeplug_range
        tags = {f"{a:06x}": f"{s.read_block(a + 0xFFF, 1, timeout=0.5)[0]:02x}" for a in range(start, start + 8 * p.PAGE, p.PAGE)}
        s.end()
        return {"first_page_tags": tags}

    def do_write_prep(s: DM32Session) -> dict:
        s.identify()
        s.enter_program_mode(read_g=False)
        s.end()
        return {"note": "handshake complete; no write frame sent"}

    _wait_for_gap(ctx, log)
    first = run("fresh_identify", "Open the port and identify the radio", do_identify)
    if first.ok:
        run("reconnect_identify", f"Reconnect after {gap:g} s and identify again", do_identify, gap_before=gap)
        run("read_session", "Enter programming mode and scan 8 page tags (read only)", do_read_session, gap_before=gap)
        run(
            "write_prep",
            f"{gap:g} s after a read session: write-session handshake, stopping before any write",
            do_write_prep,
            gap_before=gap,
        )
        if fast_reconnect:
            run("read_session_2", "Another read session before the fast reconnect", do_read_session, gap_before=gap)
            fast = run(
                "fast_reconnect",
                f"Reconnect {NEONPLUG_REOPEN_DELAY} s after the session (NeonPlug timing)",
                do_identify,
                gap_before=NEONPLUG_REOPEN_DELAY,
            )
            if not fast.ok:
                for delay in (3.0, 6.0, 12.0):
                    if run(f"recovery_after_{delay:g}s", f"Identify again after {delay:g} s", do_identify, gap_before=delay).ok:
                        break
    if sweep_lines:
        for preset in ("cps", "rts-only", "dtr-only", "none"):
            run(f"lines_{preset}", f"Identify with line preset '{preset}'", do_identify, lines=preset, gap_before=gap)

    log.close()
    report = {
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "port": _port_record(ctx),
        "default_lines": ctx.lines,
        "settle_seconds": ctx.settle,
        "cases": [r.__dict__ for r in results],
    }
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return out, results


# -- guarded page writes ----------------------------------------------------------


class WriteTestAborted(Exception):
    """A pre-write check failed; no write frame was sent."""


PagePlan = Callable[[dict[int, bytes]], dict[int, bytes]]


def run_page_writes(
    ctx: Context,
    *,
    label: str,
    plan: PagePlan,
    really_write: bool,
    copies: int = 3,
    expect_identity: dict | None = None,
) -> tuple[Path, dict]:
    """Back up, plan, check, write, read back, back up again and compare.

    1. Full read into a fresh backup ("before"); ``plan`` turns its pages into
       {address: new page} for the pages to write. A preview of decoded
       channels and zones before and after is saved.
    2. Write session: the radio's identity and page map must match the backup,
       and every target page on the radio must equal the backup's copy. Then
       (only with ``really_write``) each page is written and read back, and the
       page map is scanned again.
    3. Full read into another backup ("after"), compared with the backup
       before plus the planned pages.

    Returns the report directory and the report.
    """
    from radiosmith.dm32.write import DM32WriteSession, UnsafeWrite, WriteUncertain, page_write_frame

    out = backup.new_timestamped_dir(ctx.data_dir / "writes")
    report: dict = {"label": label, "really_write": really_write, "steps": []}

    def save() -> None:
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def step(name: str, **info) -> None:
        report["steps"].append({"step": name, **info})
        save()

    ctx.say("Step 1: full backup before writing")
    before = read_codeplug(ctx, copies=copies)
    before_backup = backup.load_backup(before.backup_dir)
    if not before_backup.ok:
        raise WriteTestAborted(f"fresh backup has problems: {before_backup.problems}")
    step("backup_before", backup=before.backup_dir.as_posix())
    if expect_identity is not None:
        for key in ("model", "firmware"):
            if expect_identity.get(key) != before.identity.get(key):
                raise WriteTestAborted(
                    f"radio {key} {before.identity.get(key)} differs from the draft's {expect_identity.get(key)}"
                )
    writes = plan(dict(before_backup.pages))
    if not writes:
        raise WriteTestAborted("nothing to write")
    for addr in writes:
        if addr not in before_backup.pages:
            raise WriteTestAborted(f"planned page {addr:#08x} is not a page in use")
    expected = dict(before_backup.pages) | writes
    report["preview"] = preview(before_backup.pages, expected, writes)
    step("plan", pages=report["preview"]["pages"])

    ctx.say(f"Step 2: write session for {len(writes)} page(s)")
    log = TrafficLog.to_file(out / "traffic.jsonl")
    results: list[dict] = []
    changed: dict = {}
    try:
        with open_session(ctx, log, session_cls=DM32WriteSession) as s:
            assert isinstance(s, DM32WriteSession)
            ident = s.identify().to_dict()
            for key in ("model", "firmware", "codeplug_range"):
                if ident[key] != before.identity[key]:
                    raise WriteTestAborted(f"radio {key} {ident[key]} differs from the backup's {before.identity[key]}")
            s.enter_program_mode()
            tags = s.scan_page_tags(copies=copies)
            backup_tags = {int(a, 16): int(t, 16) for a, t in before_backup.manifest["page_tags"].items()}
            if tags != backup_tags:
                raise WriteTestAborted("the radio's page map changed since the backup")
            for addr, data in writes.items():
                if s.read_page(addr, copies=copies) != before_backup.pages[addr]:
                    raise WriteTestAborted(f"page {addr:#08x} on the radio differs from the backup")
                assert s.identity is not None and s.identity.codeplug_range is not None
                page_write_frame(addr, data, s.identity.codeplug_range, tags[addr])  # raises UnsafeWrite
            step("pre_write_checks", identity_match=True, page_map_match=True, pages_match=True)
            if not really_write:
                s.end()
                step("dry_run", note="checks passed; no W frame sent")
                return out, report
            for addr, data in writes.items():
                result = s.write_page(addr, data, copies=copies)
                results.append(result)
                step("write", **result)
                if not result["verified"]:
                    raise WriteUncertain(f"page {addr:06x} read back differently after the write")
            tags_after = s.scan_page_tags(copies=copies)
            changed = {f"{a:06x}": [f"{tags[a]:02x}", f"{tags_after[a]:02x}"] for a in tags if tags[a] != tags_after[a]}
            step("rescan", page_map_unchanged=not changed, changed=changed)
            s.end()
    except (WriteTestAborted, WriteUncertain, UnsafeWrite, p.ProtocolError) as e:
        step("error", error=f"{type(e).__name__}: {e}", pages_written=[r["address"] for r in results])
        raise
    finally:
        log.close()

    ctx.say("Step 3: full backup after writing, compared with what was planned")
    after = read_codeplug(ctx, copies=copies)
    after_pages = backup.load_backup(after.backup_dir).pages
    diff = backup.diff_pages(expected, after_pages)
    step("backup_after", backup=after.backup_dir.as_posix(), matches_plan=diff["identical"], diff=diff)
    report["after"] = summarize(codeplug.decode(after_pages))
    report["passed"] = bool(all(r["verified"] for r in results) and not changed and diff["identical"])
    save()
    return out, report


def preview(before: dict[int, bytes], after: dict[int, bytes], writes: dict[int, bytes]) -> dict:
    """What a planned write changes, decoded, plus a byte count per page."""
    cb = {c.number: c for c in codeplug.decode(before).channels}
    ca = {c.number: c for c in codeplug.decode(after).channels}
    zb = {z.number: z for z in codeplug.decode(before).zones}
    za = {z.number: z for z in codeplug.decode(after).zones}
    asdict = dataclasses.asdict
    return {
        "pages": [
            {
                "address": f"{a:06x}",
                "tag": f"{d[-1]:02x}",
                "bytes_changed": sum(x != y for x, y in zip(before[a], d, strict=True)),
            }
            for a, d in sorted(writes.items())
        ],
        "channels_added": [asdict(ca[n]) for n in sorted(set(ca) - set(cb))],
        "channels_changed": [
            {"before": asdict(cb[n]), "after": asdict(ca[n])} for n in sorted(set(ca) & set(cb)) if ca[n] != cb[n]
        ],
        "channels_removed": [asdict(cb[n]) for n in sorted(set(cb) - set(ca))],
        "zones_added": [asdict(za[n]) for n in sorted(set(za) - set(zb))],
        "zones_changed": [{"before": asdict(zb[n]), "after": asdict(za[n])} for n in sorted(set(za) & set(zb)) if za[n] != zb[n]],
        "zones_removed": [asdict(zb[n]) for n in sorted(set(zb) - set(za))],
    }


def noop_write_test(ctx: Context, *, tag: int, really_write: bool, copies: int = 3) -> tuple[Path, dict]:
    """Rewrite the page with ``tag`` with its own contents (changes nothing)."""

    def plan(pages: dict[int, bytes]) -> dict[int, bytes]:
        matches = [a for a, d in pages.items() if d[-1] == tag]
        if len(matches) != 1:
            raise WriteTestAborted(f"backup has {len(matches)} pages with tag {tag:#04x}")
        return {matches[0]: pages[matches[0]]}

    return run_page_writes(ctx, label=f"noop rewrite of tag {tag:02x}", plan=plan, really_write=really_write, copies=copies)


def load_profile(path: Path) -> dict:
    """Read a channel profile (JSON) and check it; see profiles/cda-listen.json."""
    from radiosmith.dm32.edit import AnalogChannel

    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["channel_objects"] = [AnalogChannel.from_dict(c) for c in profile["channels"]]
    for ch in profile["channel_objects"]:
        ch.validate()
    return profile


def apply_profile(ctx: Context, profile: dict, *, really_write: bool, copies: int = 3) -> tuple[Path, dict]:
    """Add a profile's analog channels to the radio, in a new zone named after the profile's zone."""
    from radiosmith.dm32.edit import add_zone_with_channels

    def plan(pages: dict[int, bytes]) -> dict[int, bytes]:
        return add_zone_with_channels(pages, profile["zone"], profile["channel_objects"])

    return run_page_writes(ctx, label=f"add zone {profile['zone']!r}", plan=plan, really_write=really_write, copies=copies)


def write_draft(ctx: Context, draft, *, approved_preview_id: str, really_write: bool, copies: int = 3) -> tuple[Path, dict]:
    """Write a draft (dm32.draft.Draft): replay its operations on a fresh read and write the result.

    Refuses unless the changes the replay would make are exactly the ones in the
    approved preview (same ``preview_id``) on the same model and firmware.
    """
    from radiosmith.dm32.draft import preview_id, semantic_changes
    from radiosmith.dm32.edit import EditError, apply_operations, changed_pages

    if approved_preview_id != draft.preview()["preview_id"]:
        raise WriteTestAborted("the approved preview is not this draft's current preview; preview it again")

    def plan(pages: dict[int, bytes]) -> dict[int, bytes]:
        try:
            after, _ = apply_operations(pages, draft.operations)
        except EditError as e:
            raise WriteTestAborted(f"the draft no longer applies to the radio: {e}") from e
        if preview_id(draft.identity, semantic_changes(pages, after)) != approved_preview_id:
            raise WriteTestAborted("on the radio this draft would change something other than the approved preview")
        return changed_pages(pages, after)

    out, report = run_page_writes(
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
