"""One local HTTP service: the web UI, its JSON API and the MCP server.

    /            the web interface (built into radiosmith/web_dist)
    /api/...     JSON API used by the web interface
    /mcp         MCP over Streamable HTTP

Only requests addressed to this machine (Host 127.0.0.1, localhost or [::1])
from no origin or a localhost origin are accepted, so a web page elsewhere
cannot drive the radio through the browser.
"""

from __future__ import annotations

import contextlib
import functools
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import anyio
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.routing import Mount

from radiosmith.dm32 import ops as dm32_ops
from radiosmith.dm32.draft import DraftError
from radiosmith.dm32.edit import EditError
from radiosmith.service import Service, ServiceError

WEB_DIST = Path(__file__).parent / "web_dist"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
EXPECTED = (ServiceError, DraftError, EditError, dm32_ops.WriteTestAborted, dm32_ops.ReadFailed, LookupError, OSError)

OPERATIONS_HELP = """Operations depend on the draft's radio (JSON objects, applied in order; channels can be referred to
by number or exact name).

DM-32UV drafts (radio "dm32"):
- {"op": "add_channels", "channels": [{"name": "Mica Peak", "rx_mhz": 146.98, "tx_mhz": null, "forbid_tx": true,
   "power": "low"|"middle"|"high", "bandwidth_khz": 25.0|12.5, "rx_tone": "127.3"|null, "tx_tone": null, "scan_list": 0}]}
  Analog only. tx_mhz null means no TX frequency, which requires forbid_tx true. Defaults: tx_mhz null, forbid_tx true,
  power low, 25 kHz, no tones, scan_list 0 (none). Names are 1-16 ASCII characters and must be unique.
- {"op": "update_channel", "channel": 5, "changes": {"name": ..., "rx_mhz": ..., "power": ...}}
  (bandwidth and tones: analog channels only)
- {"op": "replace_channel", "channel": 5, "with": {...analog channel...}}  (turns any channel into an analog one)
- {"op": "delete_channels", "channels": [3, "Old name"]}  (renumbers the rest; zones and scan lists follow)
- {"op": "add_zone", "name": "Hiking", "members": [1, "Mica Peak"]}  (up to 64 channels)
- {"op": "update_zone", "zone": 2 | "name", "name": "New name", "members": [...]}
- {"op": "delete_zone", "zone": 2 | "name"}
- {"op": "update_scan_list", "scan_list": 1 | "name", "name": "...", "members": [...]}  (up to 16 channels)
Frequencies are MHz in 136-174 or 400-480, on a 10 Hz step.

RT-950 PRO drafts (radio "rt950"); channels are fixed slots 1-960 (zone 1 = 1-99, zone 2 = 100-198, ...), never renumbered:
- {"op": "add_channels", "channels": [{"name": "Mica Peak", "rx_mhz": 146.98, "tx_mhz": null, "power": "low",
   "bandwidth_khz": 25.0, "rx_tone": null, "tx_tone": null, "scan_add": true, "busy_lock": false, "rx_am": false}],
   "at": 30}
  Into empty slots from `at`, or after the last used channel. tx_mhz null = receive only (the radio has no TX
  inhibit). Names 0-12 ASCII, unique when set. RX 18-1000 MHz; TX 136-174, 200-260 or 400-520 MHz. Tones: CTCSS only.
  rx_am true receives AM (airband, 118-137 MHz).
- {"op": "update_channel", "channel": 21, "changes": {...}}  (only the named fields are written)
- {"op": "replace_channel", "channel": 21, "with": {...}}
- {"op": "delete_channels", "channels": [21, "Name"]}  (empties the slots)"""


Radio = Annotated[str, Field(description="'dm32' (Baofeng DM-32UV) or 'rt950' (Radtel RT-950 PRO)")]
Port = Annotated[str | None, Field(description="serial port, e.g. COM3; omit to use the port that last answered as this radio")]


def build_mcp(service: Service) -> MCPServer:
    mcp = MCPServer(
        "radiosmith",
        instructions=(
            "Program a Baofeng DM-32UV (radio 'dm32') and a Radtel RT-950 PRO (radio 'rt950') connected to this computer. "
            "Workflow: identify_radio, read_radio (a job: poll get_job), create_draft from the backup, edit_draft, "
            "preview_write, then write_radio with the preview_id after the user approves the exact changes, then "
            "verify_radio. Drafts never touch the radio. Writes are only possible when the service was started with "
            "--allow-writes."
        ),
    )

    async def call(fn, *args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except EXPECTED as e:
            raise ToolError(str(e)) from e

    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    radio_read = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
    local_edit = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

    @mcp.tool(annotations=read_only)
    async def status() -> dict:
        """Service status: whether writes are enabled, the data directory, running jobs."""
        return service.status()

    @mcp.tool(annotations=read_only)
    async def list_ports() -> list[dict]:
        """Serial ports, with the radio each one last answered as."""
        return await call(service.list_ports)

    @mcp.tool(annotations=radio_read)
    async def identify_radio(radio: Radio, port: Port = None) -> dict:
        """Identify the radio (model, firmware) without reading or changing its memory. Takes a few seconds."""
        return await call(service.identify, radio, port)

    @mcp.tool(annotations=radio_read)
    async def read_radio(radio: Radio, port: Port = None) -> dict:
        """Start a full read of the radio into a new local backup. Returns a job; poll get_job.

        Takes about 4 minutes (dm32) or 30 seconds (rt950)."""
        return await call(service.start_read, radio, port)

    @mcp.tool(annotations=read_only)
    async def get_job(job_id: str, wait_seconds: Annotated[float, Field(ge=0, le=55)] = 20) -> dict:
        """Status, progress log and result of a radio job. Waits up to wait_seconds for it to finish."""
        return await call(service.get_job, job_id, wait_seconds)

    @mcp.tool(annotations=read_only)
    async def list_backups(
        radio: Annotated[str | None, Field(description="'dm32' or 'rt950'; omit for both")] = None,
    ) -> list[dict]:
        """Local backups, newest first."""
        return await call(service.list_backups, radio)

    @mcp.tool(annotations=read_only)
    async def get_backup(backup: str) -> dict:
        """Channels (and for the DM-32UV zones and scan lists) decoded from a backup listed by list_backups."""
        return await call(service.backup_codeplug, backup)

    @mcp.tool(annotations=local_edit)
    async def create_draft(
        backup: Annotated[str | None, Field(description="a backup from list_backups; omit for the radio's newest one")] = None,
        name: str = "",
        radio: Annotated[str, Field(description="'dm32' or 'rt950'; used when backup is omitted")] = "dm32",
    ) -> dict:
        """Start an offline draft from a backup. Returns the draft id, its radio and its decoded channels."""
        return await call(service.create_draft, backup, name, radio)

    @mcp.tool(annotations=read_only)
    async def list_drafts() -> list[dict]:
        """Saved drafts, newest first."""
        return await call(service.list_drafts)

    @mcp.tool(annotations=read_only)
    async def get_draft(draft_id: str) -> dict:
        """A draft's operations and its edited channels, zones and scan lists."""
        return await call(service.get_draft, draft_id)

    @mcp.tool(
        annotations=local_edit,
        description="Validate and append edit operations to a draft (all or nothing).\n\n" + OPERATIONS_HELP,
    )
    async def edit_draft(draft_id: str, operations: list[dict[str, Any]]) -> dict:
        return await call(service.edit_draft, draft_id, operations)

    @mcp.tool(annotations=local_edit)
    async def undo_draft(draft_id: str, count: int = 1) -> dict:
        """Remove the last `count` operations from a draft."""
        return await call(service.undo_draft, draft_id, count)

    @mcp.tool(annotations=read_only)
    async def list_profiles() -> list[dict]:
        """Channel profiles shipped with radiosmith (e.g. cda-listen)."""
        return await call(service.list_profiles)

    @mcp.tool(annotations=local_edit)
    async def apply_profile(
        draft_id: str,
        profile: str,
        at: Annotated[int | None, Field(description="RT-950 PRO only: first slot to use, e.g. 100 for zone 2")] = None,
    ) -> dict:
        """Add a profile's channels to a draft: on the DM-32UV in a new zone named by the profile, on the RT-950 PRO
        into empty slots from `at` or after the last used channel. Profiles with AM channels (rx_am, e.g. airband)
        are for the RT-950 PRO only."""
        return await call(service.apply_profile, draft_id, profile, at)

    @mcp.tool(annotations=read_only)
    async def export_channels_csv(draft_id: str) -> str:
        """The draft's channels as CSV.

        Columns: number,name,rx_mhz,tx_mhz,mode,forbid_tx,power,bandwidth_khz,rx_tone,tx_tone,scan_list."""
        return await call(service.export_csv, draft_id)

    @mcp.tool(annotations=local_edit)
    async def import_channels_csv(draft_id: str, csv: str) -> dict:
        """Make the draft's channels match an edited CSV from export_channels_csv: changed rows are updated,
        rows with no number are added, missing rows are deleted."""
        return await call(service.import_csv, draft_id, csv)

    @mcp.tool(annotations=read_only)
    async def preview_write(draft_id: str) -> dict:
        """Exactly what writing the draft would change (decoded) and which pages, plus the preview_id write_radio needs.
        Show these changes to the user before writing."""
        return await call(service.preview, draft_id)

    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
    )
    async def write_radio(
        draft_id: str,
        preview_id: str,
        port: Port = None,
        dry_run: Annotated[bool, Field(description="run every check against the radio but send no write")] = False,
    ) -> dict:
        """Write a draft to its radio. Only after the user approved the preview with this preview_id.

        Takes a full backup first, refuses if the radio's model/firmware differ or the replayed changes differ
        from the preview, writes only the changed pages (DM-32UV) or whole 4 KB sectors (RT-950 PRO), reads each
        back, and compares a second full backup. Returns a job; poll get_job (about 8 minutes for the DM-32UV,
        about 1 minute for the RT-950 PRO)."""
        return await call(service.start_write, draft_id, preview_id, port, dry_run=dry_run)

    @mcp.tool(annotations=radio_read)
    async def verify_radio(draft_id: str, port: Port = None) -> dict:
        """Read the draft's radio and check its channels (and zones and scan lists) equal the draft's. Returns a job."""
        return await call(service.start_verify, draft_id, port)

    return mcp


def _local_origin(origin: str) -> bool:
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and (parts.hostname or "") in LOCAL_HOSTS


def create_app(service: Service) -> FastAPI:
    mcp = build_mcp(service)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="radiosmith", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        origin = request.headers.get("origin")
        if urlsplit(f"//{request.headers.get('host', '')}").hostname not in LOCAL_HOSTS or (origin and not _local_origin(origin)):
            return JSONResponse({"error": "radiosmith only accepts requests from this computer"}, status_code=403)
        return await call_next(request)

    async def expected_error(_request: Request, exc: Exception):
        return JSONResponse({"error": str(exc)}, status_code=400)

    for cls in EXPECTED:
        app.add_exception_handler(cls, expected_error)

    s = service
    api = "/api"

    @app.get(f"{api}/status")
    def status():
        return s.status()

    @app.get(f"{api}/ports")
    def ports():
        return s.list_ports()

    @app.post(f"{api}/radios/{{radio}}/identify")
    def identify(radio: str, port: str | None = None):
        return s.identify(radio, port)

    @app.post(f"{api}/radios/{{radio}}/read")
    def read(radio: str, port: str | None = None):
        return s.start_read(radio, port)

    @app.get(f"{api}/jobs")
    def jobs():
        return s.list_jobs()

    @app.get(f"{api}/jobs/{{job_id}}")
    def job(job_id: str, wait: float = 0):
        return s.get_job(job_id, wait)

    @app.get(f"{api}/backups")
    def backups(radio: str | None = None):
        return s.list_backups(radio)

    @app.get(f"{api}/backups/{{ref:path}}")
    def backup_codeplug(ref: str):
        return s.backup_codeplug(ref)

    @app.get(f"{api}/profiles")
    def profiles():
        return s.list_profiles()

    @app.get(f"{api}/drafts")
    def drafts():
        return s.list_drafts()

    @app.get(f"{api}/drafts/{{draft_id}}")
    def get_draft(draft_id: str):
        return s.get_draft(draft_id)

    @app.delete(f"{api}/drafts/{{draft_id}}")
    def delete_draft(draft_id: str):
        return s.delete_draft(draft_id)

    @app.get(f"{api}/drafts/{{draft_id}}/preview")
    def preview(draft_id: str):
        return s.preview(draft_id)

    @app.post(f"{api}/drafts")
    def create_draft(body: Annotated[dict, Body()]):
        return s.create_draft(body.get("backup"), body.get("name", ""), body.get("radio", "dm32"))

    @app.post(f"{api}/drafts/{{draft_id}}/operations")
    def edit_draft(draft_id: str, body: Annotated[dict, Body()]):
        return s.edit_draft(draft_id, body.get("operations", []))

    @app.post(f"{api}/drafts/{{draft_id}}/undo")
    def undo_draft(draft_id: str, body: Annotated[dict, Body()]):
        return s.undo_draft(draft_id, int(body.get("count", 1)))

    @app.post(f"{api}/drafts/{{draft_id}}/rename")
    def rename_draft(draft_id: str, body: Annotated[dict, Body()]):
        return s.rename_draft(draft_id, str(body.get("name", "")))

    @app.post(f"{api}/drafts/{{draft_id}}/profile")
    def apply_profile(draft_id: str, body: Annotated[dict, Body()]):
        at = body.get("at")
        return s.apply_profile(draft_id, str(body.get("profile", "")), at if isinstance(at, int) else None)

    @app.get(f"{api}/drafts/{{draft_id}}/channels.csv")
    def export_csv(draft_id: str):
        return PlainTextResponse(
            s.export_csv(draft_id),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{draft_id}.csv"'},
        )

    @app.post(f"{api}/drafts/{{draft_id}}/channels.csv")
    def import_csv(draft_id: str, body: Annotated[dict, Body()]):
        return s.import_csv(draft_id, str(body.get("csv", "")))

    @app.post(f"{api}/drafts/{{draft_id}}/write")
    def write(draft_id: str, body: Annotated[dict, Body()]):
        return s.start_write(draft_id, str(body.get("preview_id", "")), body.get("port"), dry_run=bool(body.get("dry_run")))

    @app.post(f"{api}/drafts/{{draft_id}}/verify")
    def verify(draft_id: str, body: Annotated[dict, Body()]):
        return s.start_verify(draft_id, body.get("port"))

    if (WEB_DIST / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB_DIST / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        def no_ui():
            return PlainTextResponse(
                "radiosmith is running. The web interface is not built (cd web && npm install && npm run build)."
            )

    # Last, so the routes above win: MCP at /mcp.
    app.router.routes.append(Mount("", app=mcp_app))
    return app


def serve(service: Service, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    if host not in LOCAL_HOSTS:
        raise SystemExit("radiosmith only listens on this computer (127.0.0.1, localhost or ::1)")
    uvicorn.run(create_app(service), host=host, port=port, log_level="info")
