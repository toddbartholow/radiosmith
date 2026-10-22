"""The local service core shared by the web API and the MCP server.

One Service owns the serial ports: radio operations run one at a time under a
lock, and the slow ones (full reads, writes) run as background jobs whose
progress can be polled. Editing drafts never touches a radio.

Writes are refused unless the service was started with ``allow_writes``, and a
write must name a draft and the preview_id of that draft's current preview.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from radiosmith import backup
from radiosmith.dm32 import codeplug
from radiosmith.dm32 import ops as dm32_ops
from radiosmith.dm32.draft import Draft, DraftError, DraftStore, decoded, operations_from_csv
from radiosmith.dm32.edit import AnalogChannel, EditError
from radiosmith.transport import PortInfo, TrafficLog, list_serial_ports

RADIOS = {"dm32": "Baofeng DM-32UV", "rt950": "Radtel RT-950 PRO"}


class ServiceError(Exception):
    """A request cannot be carried out; the message says why."""


class JobFailed(Exception):
    """A job failed after producing something worth returning (e.g. a write report)."""

    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


@dataclass
class Job:
    id: str
    kind: str
    radio: str
    port: str
    draft: str | None = None
    status: str = "running"  # running, done, failed
    started_utc: str = ""
    finished_utc: str | None = None
    log: list[str] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if f.name != "done"}
        d["log"] = self.log[-200:]
        return d


def _jsonable(obj):
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    return obj


class Service:
    def __init__(
        self,
        data_dir: Path,
        *,
        allow_writes: bool = False,
        profiles_dir: Path | None = None,
        link_factory: dm32_ops.LinkFactory | None = None,
        ports: Callable[[], list[PortInfo]] = list_serial_ports,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.data_dir = data_dir
        self.allow_writes = allow_writes
        self.profiles_dir = profiles_dir or Path("profiles")
        self.link_factory = link_factory
        self._ports = ports
        self.sleep = sleep
        self.drafts = DraftStore(data_dir)
        self.radio_lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self._jobs_lock = threading.Lock()

    # -- ports -----------------------------------------------------------------

    def _port_map(self) -> dict[str, str]:
        try:
            state = json.loads((self.data_dir / "state.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return state.get("ports", {})

    def _remember_port(self, device: str, radio: str) -> None:
        mapping = {d: r for d, r in self._port_map().items() if r != radio and d != device} | {device: radio}
        state_file = self.data_dir / "state.json"
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state | {"ports": mapping}, indent=2) + "\n", encoding="utf-8")

    def list_ports(self) -> list[dict]:
        mapping = self._port_map()
        return [p.to_dict() | {"last_seen_radio": mapping.get(p.device)} for p in self._ports()]

    def _pick_port(self, radio: str, port: str | None) -> PortInfo:
        if radio not in RADIOS:
            raise ServiceError(f"unknown radio {radio!r}; choose from {sorted(RADIOS)}")
        ports = self._ports()
        if port:
            for p in ports:
                if p.device.lower() == port.lower():
                    return p
            raise ServiceError(f"port {port} not found; ports: {[p.device for p in ports]}")
        remembered = [d for d, r in self._port_map().items() if r == radio]
        for p in ports:
            if p.device in remembered:
                return p
        cables = [p for p in ports if p.is_ch340]
        if len(cables) == 1:
            return cables[0]
        raise ServiceError(
            f"cannot tell which port the {RADIOS[radio]} is on ({[p.device for p in cables]}); "
            "give the port, and it will be remembered after a successful identify"
        )

    def _context(self, port: PortInfo, say: Callable[[str], None]) -> dm32_ops.Context:
        kw = {"link_factory": self.link_factory} if self.link_factory else {}
        return dm32_ops.Context(port=port, data_dir=self.data_dir, sleep=self.sleep, say=say, **kw)

    # -- jobs ------------------------------------------------------------------

    def _start_job(
        self, kind: str, radio: str, port: PortInfo, work: Callable[[dm32_ops.Context], dict], *, draft: str | None = None
    ) -> dict:
        if not self.radio_lock.acquire(blocking=False):
            busy = [j.id for j in self.jobs.values() if j.status == "running"]
            raise ServiceError(f"a radio operation is already running (job {', '.join(busy) or 'starting'}); wait for it")
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, radio=radio, port=port.device, draft=draft)
        job.started_utc = datetime.now(UTC).isoformat(timespec="seconds")
        with self._jobs_lock:
            self.jobs[job.id] = job

        def run() -> None:
            try:
                job.result = _jsonable(work(self._context(port, job.log.append)))
                job.status = "done"
            except Exception as e:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                job.log.append(job.error)
                if not isinstance(
                    e, ServiceError | JobFailed | DraftError | EditError | dm32_ops.WriteTestAborted | dm32_ops.ReadFailed
                ):
                    job.log.append(traceback.format_exc(limit=3))
                if isinstance(e, dm32_ops.ReadFailed) and e.backup_dir:
                    job.result = {"partial_backup": self._rel(e.backup_dir)}
                if isinstance(e, JobFailed):
                    job.result = _jsonable(e.result)
            finally:
                job.finished_utc = datetime.now(UTC).isoformat(timespec="seconds")
                self.radio_lock.release()
                job.done.set()

        threading.Thread(target=run, name=f"radiosmith-{kind}-{job.id}", daemon=True).start()
        return job.to_dict()

    def get_job(self, job_id: str, wait_seconds: float = 0) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise ServiceError(f"no job {job_id!r}")
        # Wait until the job finishes or logs a new line, whichever comes first.
        deadline = time.monotonic() + min(max(wait_seconds, 0), 55)
        lines = len(job.log)
        while time.monotonic() < deadline and len(job.log) == lines:
            if job.done.wait(0.25):
                break
        return job.to_dict()

    def list_jobs(self) -> list[dict]:
        return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: j.started_utc, reverse=True)]

    # -- radio operations ----------------------------------------------------------

    def identify(self, radio: str, port: str | None = None) -> dict:
        """Identify without entering programming mode. Runs synchronously (a few seconds)."""
        info = self._pick_port(radio, port)
        if not self.radio_lock.acquire(timeout=1):
            raise ServiceError("a radio operation is already running; wait for it")
        try:
            ctx = self._context(info, lambda _m: None)
            log_path = self.data_dir / "logs" / f"{radio}-identify.jsonl"
            log = TrafficLog.to_file(log_path)
            try:
                if radio == "dm32":
                    result = dm32_ops.identify(ctx, log)
                else:
                    from radiosmith.rt950 import ops as rt_ops

                    result = rt_ops.identify(ctx, log)
            finally:
                log.close()
        finally:
            self.radio_lock.release()
        self._remember_port(info.device, radio)
        return _jsonable(result | {"radio": radio})

    def start_read(self, radio: str, port: str | None = None) -> dict:
        info = self._pick_port(radio, port)

        def work(ctx: dm32_ops.Context) -> dict:
            if radio == "dm32":
                r = dm32_ops.read_codeplug(ctx)
                out = {"backup": self._rel(r.backup_dir), "pages_saved": r.pages_saved, "summary": r.summary}
            else:
                from radiosmith.rt950 import ops as rt_ops

                path, summary = rt_ops.read_image(ctx)
                out = {"backup": self._rel(path)} | summary
            self._remember_port(info.device, radio)
            return out

        return self._start_job("read", radio, info, work)

    def start_write(self, draft_id: str, preview_id: str, port: str | None = None, *, dry_run: bool = False) -> dict:
        if not dry_run and not self.allow_writes:
            raise ServiceError("writes are disabled: start the service with --allow-writes to write to a radio")
        draft = self.drafts.load(draft_id)
        if draft.preview()["nothing_to_write"]:
            raise ServiceError("the draft changes nothing")
        if preview_id != draft.preview()["preview_id"]:
            raise ServiceError("preview_id does not match the draft's current preview; preview it again")
        info = self._pick_port(draft.radio, port)
        if draft.radio == "rt950":
            from radiosmith.rt950 import ops as rt_ops

            write_draft = rt_ops.write_draft
        else:
            write_draft = dm32_ops.write_draft

        def work(ctx: dm32_ops.Context) -> dict:
            writes = self.data_dir / "writes"
            earlier = set(writes.iterdir()) if writes.is_dir() else set()
            try:
                out, report = write_draft(ctx, draft, approved_preview_id=preview_id, really_write=not dry_run)
            except Exception as e:
                new = sorted(set(writes.iterdir()) - earlier) if writes.is_dir() else []
                result: dict = {"dry_run": dry_run, "passed": False}
                if new and (new[-1] / "report.json").is_file():
                    result |= {
                        "report_dir": self._rel(new[-1]),
                        "steps": json.loads((new[-1] / "report.json").read_text())["steps"],
                    }
                raise JobFailed(f"{type(e).__name__}: {e}", result) from e
            if dry_run:
                return {"report_dir": self._rel(out), "dry_run": True, "checks_passed": True, "steps": report["steps"]}
            return {"report_dir": self._rel(out), "dry_run": False, "passed": report.get("passed"), "steps": report["steps"]}

        return self._start_job("dry-run" if dry_run else "write", draft.radio, info, work, draft=draft_id)

    def start_verify(self, draft_id: str, port: str | None = None) -> dict:
        """Read the radio and check its channels, zones and scan lists equal the draft's."""
        draft = self.drafts.load(draft_id)
        info = self._pick_port(draft.radio, port)
        want = draft.codeplug()

        def work(ctx: dm32_ops.Context) -> dict:
            if draft.radio == "rt950":
                from radiosmith.rt950 import codeplug as rt_codeplug
                from radiosmith.rt950 import ops as rt_ops

                path, _ = rt_ops.read_image(ctx)
                got = rt_codeplug.decoded((path / "image.bin").read_bytes())
            else:
                r = dm32_ops.read_codeplug(ctx)
                path = r.backup_dir
                got = decoded(backup.load_backup(path).pages)
            diffs = {k: {"radio": got[k], "draft": want[k]} for k in ("channels", "zones", "scan_lists") if got[k] != want[k]}
            return {"backup": self._rel(path), "matches_draft": not diffs, "differences": diffs}

        return self._start_job("verify", draft.radio, info, work, draft=draft_id)

    # -- backups ---------------------------------------------------------------------

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.data_dir.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def _backup_dir(self, ref: str) -> Path:
        path = (self.data_dir / ref).resolve()
        root = (self.data_dir / "backups").resolve()
        if root not in path.parents or not (path / "manifest.json").is_file():
            raise ServiceError(f"no backup {ref!r}; use list_backups")
        return path

    def list_backups(self, radio: str | None = None) -> list[dict]:
        out = []
        for driver in [radio] if radio else sorted(RADIOS):
            root = self.data_dir / "backups" / driver
            if not root.is_dir():
                continue
            for d in sorted(root.iterdir(), reverse=True):
                m = d / "manifest.json"
                if not m.is_file():
                    continue
                man = json.loads(m.read_text(encoding="utf-8"))
                ident = man.get("identity", {})
                out.append(
                    {
                        "backup": self._rel(d),
                        "radio": man.get("radio_driver"),
                        "model": ident.get("model"),
                        "firmware": ident.get("firmware"),
                        "created_utc": man.get("created_utc"),
                        "complete": man.get("complete"),
                    }
                )
        return out

    def backup_codeplug(self, ref: str) -> dict:
        b = backup.load_backup(self._backup_dir(ref))
        if b.manifest.get("radio_driver") == "rt950":
            from radiosmith.rt950 import codeplug as rt_codeplug

            image = (b.path / "image.bin").read_bytes() if (b.path / "image.bin").is_file() else b""
            return {"backup": ref, "ok": b.ok, "problems": b.problems, "radio": "rt950"} | rt_codeplug.decoded(image)
        return {"backup": ref, "ok": b.ok, "problems": b.problems, "radio": "dm32"} | decoded(b.pages)

    # -- drafts ------------------------------------------------------------------------

    def create_draft(self, backup_ref: str | None = None, name: str = "", radio: str = "dm32") -> dict:
        if backup_ref is None:
            if radio not in RADIOS:
                raise ServiceError(f"unknown radio {radio!r}; choose from {sorted(RADIOS)}")
            found = [b for b in self.list_backups(radio) if b["complete"]]
            if not found:
                raise ServiceError(f"no complete {RADIOS[radio]} backup; read the radio first")
            backup_ref = found[0]["backup"]
        d = self.drafts.create(self._backup_dir(backup_ref), name)
        return self.draft_view(d)

    def draft_view(self, d: Draft) -> dict:
        return d.summary() | {"codeplug": d.codeplug(), "operations": d.operations}

    def get_draft(self, draft_id: str) -> dict:
        return self.draft_view(self.drafts.load(draft_id))

    def list_drafts(self) -> list[dict]:
        return self.drafts.list()

    def edit_draft(self, draft_id: str, operations: list[dict]) -> dict:
        d = self.drafts.load(draft_id)
        results = d.apply(operations)
        self.drafts.save(d)
        pv = d.preview()
        return {
            "results": results,
            "operations": len(d.operations),
            "preview_id": pv["preview_id"],
            "pages_changed": len(pv["pages"]),
        }

    def undo_draft(self, draft_id: str, count: int = 1) -> dict:
        d = self.drafts.load(draft_id)
        d.undo(count)
        self.drafts.save(d)
        return d.summary()

    def rename_draft(self, draft_id: str, name: str) -> dict:
        d = self.drafts.load(draft_id)
        d.name = name
        self.drafts.save(d)
        return d.summary()

    def delete_draft(self, draft_id: str) -> dict:
        self.drafts.delete(draft_id)
        return {"deleted": draft_id}

    def preview(self, draft_id: str) -> dict:
        return self.drafts.load(draft_id).preview()

    def export_csv(self, draft_id: str) -> str:
        return self.drafts.load(draft_id).channels_csv()

    def import_csv(self, draft_id: str, text: str) -> dict:
        d = self.drafts.load(draft_id)
        ops_ = operations_from_csv(text, d.codeplug(), d.radio)
        if not ops_:
            return {"results": [], "operations_added": 0, "note": "the CSV matches the draft"}
        return self.edit_draft(draft_id, ops_) | {"operations_added": len(ops_)}

    # -- profiles ------------------------------------------------------------------------

    def list_profiles(self) -> list[dict]:
        if not self.profiles_dir.is_dir():
            return []
        out = []
        for f in sorted(self.profiles_dir.glob("*.json")):
            j = json.loads(f.read_text(encoding="utf-8"))
            out.append(
                {
                    "profile": f.stem,
                    "name": j.get("name"),
                    "description": j.get("description"),
                    "channels": len(j.get("channels", [])),
                }
            )
        return out

    def apply_profile(self, draft_id: str, profile: str, at: int | None = None) -> dict:
        f = self.profiles_dir / f"{profile}.json"
        if not f.is_file() or f.parent.resolve() != self.profiles_dir.resolve():
            raise ServiceError(f"no profile {profile!r}; use list_profiles")
        j = json.loads(f.read_text(encoding="utf-8"))
        if self.drafts.load(draft_id).radio == "rt950":
            from radiosmith.rt950.draftops import profile_channels

            try:
                op = {"op": "add_channels", "channels": profile_channels(j["channels"])}
                ops_ = [op | ({"at": at} if at is not None else {})]
            except EditError as e:
                raise ServiceError(str(e)) from e
        elif at is not None:
            raise ServiceError("at is for the RT-950 PRO; on the DM-32UV profile channels go after the last channel")
        else:
            chans = [AnalogChannel.from_dict(c) for c in j["channels"]]
            ops_ = [
                {"op": "add_channels", "channels": [dataclasses.asdict(c) for c in chans]},
                {"op": "add_zone", "name": j["zone"], "members": [c.name for c in chans]},
            ]
        return self.edit_draft(draft_id, ops_)

    def status(self) -> dict:
        return {
            "allow_writes": self.allow_writes,
            "data_dir": self.data_dir.resolve().as_posix(),
            "radios": RADIOS,
            "busy": self.radio_lock.locked(),
            "jobs_running": [j.id for j in self.jobs.values() if j.status == "running"],
            "decoded_fields": {"channel": [f.name for f in dataclasses.fields(codeplug.Channel)]},
        }
