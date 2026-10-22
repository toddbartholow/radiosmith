"""Terminal interface: the same workflow as the web interface, over the same service API.

    radiosmith tui [--url http://127.0.0.1:8765] [--allow-writes]

If no service answers at the URL, one is started inside this process (on that
port, so the web interface and MCP clients can use it too while the TUI runs).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from radiosmith.client import DEFAULT_URL, ApiError, Client

RADIO_NAMES = {"dm32": "DM-32UV", "rt950": "RT-950 PRO"}
CHANNEL_COLUMNS = ("#", "Name", "RX MHz", "TX MHz", "TX", "Mode", "Power", "BW", "Tones", "Scan")


def mhz(v) -> str:
    return "-" if v is None else f"{v:.5f}"


def identity_label(identity: dict) -> str:
    """Model and firmware; the RT-950 PRO reports no firmware, so it may be None."""
    return " ".join(str(v) for v in (identity.get("model"), identity.get("firmware")) if v)


def rx_only(ch: dict) -> bool:
    return ch.get("forbid_tx") or ch.get("tx_mhz") is None


def channel_row(ch: dict) -> tuple:
    tones = (
        f"CC{ch['color_code']} TS{ch['time_slot']}"
        if ch.get("color_code") is not None
        else f"{ch.get('rx_tone') or '-'}/{ch.get('tx_tone') or '-'}"
    )
    return (
        str(ch["number"]),
        ch["name"],
        mhz(ch["rx_mhz"]),
        mhz(ch["tx_mhz"]),
        Text("RX only", style="bold yellow") if rx_only(ch) else Text("TX", style="green"),
        ch.get("mode", "analog"),
        ch["power"],
        f"{ch['bandwidth_khz']:g}",
        tones,
        str(ch.get("scan_list", "")) if ch.get("scan_list") else "-",
    )


FIELD_LABELS = {
    "name": "name",
    "rx_mhz": "RX",
    "tx_mhz": "TX",
    "forbid_tx": "forbid TX",
    "power": "power",
    "bandwidth_khz": "bandwidth",
    "rx_tone": "RX tone",
    "tx_tone": "TX tone",
    "scan_list": "scan list",
    "mode": "mode",
    "members": "members",
}


def describe_channel(ch: dict) -> str:
    tx = "RX only" if rx_only(ch) else f"TX {mhz(ch['tx_mhz'])}"
    return f"{ch['number']:>4} {ch['name']:<16} RX {mhz(ch['rx_mhz'])}  {tx}  {ch.get('mode', '')} {ch['power']}"


def format_preview(pv: dict) -> str:
    """The decoded changes of a write preview as plain text."""
    c = pv["changes"]
    lines = [
        f"Draft {pv['draft']} on {identity_label(pv['identity'])}",
        f"preview_id {pv['preview_id']}",
        "",
    ]
    if pv["nothing_to_write"]:
        return "\n".join([*lines, "Nothing to write: the draft equals its backup."])
    for kind, label in (("channels", "Channel"), ("zones", "Zone"), ("scan_lists", "Scan list")):
        for item in c[f"{kind}_added"]:
            lines.append(
                f"+ {label} "
                + (describe_channel(item) if kind == "channels" else f"{item['number']} {item['name']!r}: {item['members']}")
            )
        for item in c[f"{kind}_removed"]:
            lines.append(f"- {label} " + (describe_channel(item) if kind == "channels" else f"{item['number']} {item['name']!r}"))
        for ch in c[f"{kind}_changed"]:
            b, a = ch["before"], ch["after"]
            diffs = [f"{FIELD_LABELS.get(k, k)} {b[k]!r} -> {a[k]!r}" for k in a if k != "number" and a.get(k) != b.get(k)]
            lines.append(f"~ {label} {ch['number']} {b['name']!r}: " + "; ".join(diffs))
    lines += ["", "Pages: " + ", ".join(f"{p['address']} (tag {p['tag']}, {p['bytes_changed']} bytes)" for p in pv["pages"])]
    return "\n".join(lines)


# -- modal screens ----------------------------------------------------------------------


class ChannelForm(ModalScreen[dict | None]):
    """Edit or add an analog channel. Returns only the fields that were filled or changed."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, channel: dict | None = None):
        super().__init__()
        self.title_text = title
        self.channel = channel or {}

    def compose(self) -> ComposeResult:
        ch = self.channel
        analog = ch.get("mode", "analog") == "analog"
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Label("Name (1-16 ASCII)")
            yield Input(ch.get("name", ""), id="name", max_length=16)
            with Horizontal(classes="row"):
                with Vertical():
                    yield Label("RX MHz")
                    yield Input("" if ch.get("rx_mhz") is None else f"{ch['rx_mhz']:.5f}", id="rx_mhz")
                with Vertical():
                    yield Label("TX MHz (empty: none)")
                    yield Input("" if ch.get("tx_mhz") is None else f"{ch['tx_mhz']:.5f}", id="tx_mhz")
            with Horizontal(classes="row"):
                with Vertical():
                    yield Label("Power")
                    yield Select(
                        [(p, p) for p in ("low", "middle", "high")], value=ch.get("power", "low"), id="power", allow_blank=False
                    )
                with Vertical():
                    yield Label("Bandwidth kHz")
                    yield Select(
                        [("25", 25.0), ("12.5", 12.5)],
                        value=ch.get("bandwidth_khz", 25.0),
                        id="bandwidth_khz",
                        allow_blank=False,
                        disabled=not analog,
                    )
                with Vertical():
                    yield Label("Scan list (0 none)")
                    yield Input(str(ch.get("scan_list", 0)), id="scan_list")
            with Horizontal(classes="row"):
                with Vertical():
                    yield Label("RX CTCSS Hz")
                    yield Input(ch.get("rx_tone") or "", id="rx_tone", disabled=not analog)
                with Vertical():
                    yield Label("TX CTCSS Hz")
                    yield Input(ch.get("tx_tone") or "", id="tx_tone", disabled=not analog)
            yield Checkbox("Forbid TX (receive only)", value=ch.get("forbid_tx", True), id="forbid_tx")
            yield Label("", id="form-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        def text(i: str) -> str:
            return self.query_one(f"#{i}", Input).value.strip()

        try:
            fields: dict = {
                "name": text("name"),
                "rx_mhz": float(text("rx_mhz")),
                "tx_mhz": float(text("tx_mhz")) if text("tx_mhz") else None,
                "power": self.query_one("#power", Select).value,
                "forbid_tx": self.query_one("#forbid_tx", Checkbox).value,
                "scan_list": int(text("scan_list") or 0),
            }
        except ValueError:
            self.query_one("#form-error", Label).update("RX, TX and scan list must be numbers")
            return
        if not self.query_one("#bandwidth_khz", Select).disabled:
            fields["bandwidth_khz"] = self.query_one("#bandwidth_khz", Select).value
            fields["rx_tone"] = text("rx_tone") or None
            fields["tx_tone"] = text("tx_tone") or None
        if fields["tx_mhz"] is None:
            fields["forbid_tx"] = True
        if self.channel:
            fields = {k: v for k, v in fields.items() if self.channel.get(k) != v}
        self.dismiss(fields)


class GroupForm(ModalScreen[dict | None]):
    """Name and members of a zone or scan list. Members: channel numbers or names, comma separated."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, name: str = "", members: list[int] | None = None, name_max: int = 16):
        super().__init__()
        self.title_text, self.name, self.members, self.name_max = title, name, members or [], name_max

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Label(f"Name (1-{self.name_max} ASCII)")
            yield Input(self.name, id="name", max_length=self.name_max)
            yield Label("Members: channel numbers or exact names, comma separated, in order")
            yield Input(", ".join(str(m) for m in self.members), id="members")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        raw = [m.strip() for m in self.query_one("#members", Input).value.split(",") if m.strip()]
        members = [int(m) if m.isdigit() else m for m in raw]
        self.dismiss({"name": self.query_one("#name", Input).value.strip(), "members": members})


class PickScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, options: list[tuple[str, str]]):
        super().__init__()
        self.title_text, self.options = title, options

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield OptionList(*[label for label, _ in self.options], id="options")
            yield Button("Cancel", id="cancel")

    @on(OptionList.OptionSelected)
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.options[event.option_index][1])

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, message: str, yes: str = "Yes"):
        super().__init__()
        self.message, self.yes = message, yes

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.message)
            with Horizontal(classes="buttons"):
                yield Button(self.yes, variant="error", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class ReviewScreen(ModalScreen[str | None]):
    """The preview of a write; returns 'dry', 'write' or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, preview: dict, writes_enabled: bool):
        super().__init__()
        self.preview, self.writes_enabled = preview, writes_enabled

    def compose(self) -> ComposeResult:
        nothing = self.preview["nothing_to_write"]
        with Vertical(classes="dialog wide"):
            yield Label("Review changes before writing", classes="dialog-title")
            with VerticalScroll(classes="preview"):
                yield Static(format_preview(self.preview), markup=False)
            if not self.writes_enabled:
                yield Label(
                    "Writes are disabled: restart with --allow-writes to write. A dry run is still possible.", classes="warn"
                )
            yield Checkbox("I reviewed these changes", id="reviewed", disabled=nothing or not self.writes_enabled)
            with Horizontal(classes="buttons"):
                yield Button("Dry run on radio", id="dry", disabled=nothing)
                yield Button("Write to radio", variant="error", id="write", disabled=True)
                yield Button("Close", id="close")

    @on(Checkbox.Changed, "#reviewed")
    def reviewed(self, event: Checkbox.Changed) -> None:
        self.query_one("#write", Button).disabled = not event.value

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss({"dry": "dry", "write": "write"}.get(event.button.id or ""))


# -- the app ------------------------------------------------------------------------------


class RadiosmithApp(App):
    TITLE = "radiosmith"
    CSS = """
    #statusbar { height: 1; padding: 0 1; background: $panel; }
    .buttons { height: auto; padding: 1 0 0 0; }
    .buttons Button { margin-right: 2; }
    .row { height: auto; }
    .row > Vertical { height: auto; width: 1fr; margin-right: 1; }
    TabbedContent, ContentSwitcher, TabPane { height: 1fr; }
    TabPane > Horizontal { height: 1fr; }
    TabPane > Horizontal > Vertical { height: 1fr; }
    DataTable { height: 1fr; }
    #ports { height: 8; }
    #backup-list { width: 44; }
    #draft-list-pane { width: 36; }
    #draft-side { width: 44; }
    #draft-groups { height: 1fr; border: round $panel; padding: 0 1; }
    RichLog { height: 1fr; border: round $panel; }
    ModalScreen { align: center middle; }
    .dialog { width: 76; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
    .dialog.wide { width: 110; }
    .dialog-title { text-style: bold; padding-bottom: 1; }
    .preview { height: auto; max-height: 30; border: round $panel; }
    .error { color: $error; }
    .warn { color: $warning; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("f5", "refresh", "Refresh"),
        Binding("e", "edit_channel", "Edit ch", show=True),
        Binding("a", "add_channel", "Add ch"),
        Binding("delete", "delete_channel", "Delete ch"),
        Binding("u", "undo", "Undo"),
        Binding("w", "review", "Review/write"),
    ]

    def __init__(self, client: Client):
        super().__init__()
        self.client = client
        self.status: dict = {}
        self.draft: dict | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="statusbar")
        with TabbedContent(initial="radios"):
            with TabPane("Radios", id="radios"):
                yield Label("Ports (select one, then identify it as a radio):")
                yield DataTable(id="ports", cursor_type="row")
                with Horizontal(classes="buttons"):
                    yield Button(compact=True, label="Identify as DM-32UV", id="identify-dm32")
                    yield Button(compact=True, label="Identify as RT-950 PRO", id="identify-rt950")
                    yield Button(compact=True, label="Read DM-32UV to backup", id="read-dm32", variant="primary")
                    yield Button(compact=True, label="Read RT-950 PRO to backup", id="read-rt950", variant="primary")
                yield RichLog(id="radio-log", wrap=True, markup=False)
            with TabPane("Backups", id="backups"):
                with Horizontal():
                    yield DataTable(id="backup-list", cursor_type="row")
                    with Vertical():
                        yield DataTable(id="backup-channels", cursor_type="row")
                        with Horizontal(classes="buttons"):
                            yield Button(compact=True, label="New draft from this backup", id="new-draft", variant="primary")
            with TabPane("Drafts", id="drafts"):
                with Horizontal():
                    with Vertical(id="draft-list-pane"):
                        yield DataTable(id="draft-list", cursor_type="row")
                        with Horizontal(classes="buttons"):
                            yield Button(compact=True, label="Delete draft", id="delete-draft", variant="error")
                    with Vertical():
                        yield Label("Open a draft from the list", id="draft-title")
                        yield DataTable(id="draft-channels", cursor_type="row")
                        with Horizontal(classes="buttons"):
                            yield Button(compact=True, label="Edit (e)", id="edit-channel")
                            yield Button(compact=True, label="Add (a)", id="add-channel")
                            yield Button(compact=True, label="Delete (Del)", id="delete-channel")
                            yield Button(compact=True, label="Make analog", id="replace-channel")
                        with Horizontal(classes="buttons"):
                            yield Button(compact=True, label="Undo (u)", id="undo")
                            yield Button(compact=True, label="Review and write (w)", id="review", variant="primary")
                    with Vertical(id="draft-side"):
                        yield Static(id="draft-groups")
                        with Horizontal(classes="buttons"):
                            yield Button(compact=True, label="Add zone", id="add-zone")
                            yield Button(compact=True, label="Edit zone", id="edit-zone")
                        with Horizontal(classes="buttons"):
                            yield Button(compact=True, label="Delete zone", id="delete-zone")
                            yield Button(compact=True, label="Edit scan list", id="edit-scan")
                        with Horizontal(classes="buttons"):
                            yield Button(compact=True, label="Apply profile", id="profile")
                            yield Button(compact=True, label="Verify radio", id="verify")
            with TabPane("Jobs", id="jobs"):
                yield DataTable(id="job-list", cursor_type="row")
                yield RichLog(id="job-log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ports", DataTable).add_columns("Port", "Description", "USB ID", "Last answered as")
        self.query_one("#backup-list", DataTable).add_columns("Backup", "Radio", "Done")
        self.query_one("#backup-channels", DataTable).add_columns(*CHANNEL_COLUMNS)
        self.query_one("#draft-list", DataTable).add_columns("Name", "Edits")
        self.query_one("#draft-channels", DataTable).add_columns(*CHANNEL_COLUMNS)
        self.query_one("#job-list", DataTable).add_columns("Job", "Kind", "Radio", "Port", "Status", "Started")
        self.action_refresh()
        self.set_interval(5, self.refresh_status)

    # -- helpers -------------------------------------------------------------------------

    def error(self, e: Exception) -> None:
        self.notify(str(e), title="Refused" if isinstance(e, ApiError) else "Error", severity="error", timeout=12)

    def say(self, line: str) -> None:
        self.query_one("#radio-log", RichLog).write(line)

    @staticmethod
    def selected_key(table: DataTable) -> str | None:
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    # -- refresh -----------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="refresh")
    def action_refresh(self) -> None:
        try:
            status, ports, backups, drafts, jobs = (
                self.client.status(),
                self.client.ports(),
                self.client.backups(),
                self.client.drafts(),
                self.client.jobs(),
            )
        except ApiError as e:
            self.call_from_thread(self.error, e)
            return
        self.call_from_thread(self.fill, status, ports, backups, drafts, jobs)

    def fill(self, status, ports, backups, drafts, jobs) -> None:
        self.show_status(status)
        for table_id, rows in (
            (
                "#ports",
                [
                    (
                        p["device"],
                        (
                            p["device"],
                            p["description"],
                            f"{p['vid'] or '-'}:{p['pid'] or '-'}",
                            RADIO_NAMES.get(p["last_seen_radio"], "-"),
                        ),
                    )
                    for p in ports
                ],
            ),
            (
                "#backup-list",
                [
                    (b["backup"], (b["backup"].split("/")[-1][:30], b["radio"], "yes" if b["complete"] else "PARTIAL"))
                    for b in backups
                ],
            ),
            (
                "#draft-list",
                [(d["id"], (d.get("name", d.get("error", ""))[:26], str(d.get("operations", "?")))) for d in drafts],
            ),
            (
                "#job-list",
                [(j["id"], (j["id"], j["kind"], j["radio"], j["port"], j["status"], j["started_utc"][11:19])) for j in jobs],
            ),
        ):
            table = self.query_one(table_id, DataTable)
            keep = self.selected_key(table)
            table.clear()
            for key, row in rows:
                table.add_row(*row, key=key)
            if keep is not None:
                for i, (key, _) in enumerate(rows):
                    if key == keep:
                        table.move_cursor(row=i)

    def show_status(self, status: dict) -> None:
        self.status = status
        writes = Text("writes ENABLED", style="bold red") if status.get("allow_writes") else Text("writes disabled", style="dim")
        busy = (
            Text(f"  busy: job {', '.join(status['jobs_running'])}", style="bold yellow")
            if status.get("jobs_running")
            else Text("  idle")
        )
        self.query_one("#statusbar", Static).update(
            Text.assemble(writes, busy, Text(f"  data: {status.get('data_dir', '')}", style="dim"))
        )

    @work(thread=True, group="status")
    def refresh_status(self) -> None:
        try:
            status = self.client.status()
        except ApiError:
            return
        self.call_from_thread(self.show_status, status)

    # -- radios --------------------------------------------------------------------------

    @on(Button.Pressed, "#identify-dm32, #identify-rt950")
    def identify(self, event: Button.Pressed) -> None:
        radio = (event.button.id or "").split("-", 1)[1]
        port = self.selected_key(self.query_one("#ports", DataTable))
        if port is None:
            self.notify("Select a port first", severity="warning")
            return
        self.say(f"Identifying {port} as {RADIO_NAMES[radio]}...")
        self.run_identify(radio, port)

    @work(thread=True, group="radio")
    def run_identify(self, radio: str, port: str) -> None:
        try:
            r = self.client.identify(radio, port)
        except ApiError as e:
            self.call_from_thread(self.say, f"  failed: {e}")
            self.call_from_thread(self.error, e)
            return
        ident = r["identity"]
        lines = [f"  {k}: {v}" for k, v in ident.items() if k not in ("v_replies", "psearch_reply")]
        self.call_from_thread(
            self.say, f"{RADIO_NAMES[radio]} on {port}:\n" + "\n".join(lines) + f"\n  link: {r.get('link_stats') or 'no errors'}"
        )
        self.call_from_thread(self.action_refresh)

    @on(Button.Pressed, "#read-dm32, #read-rt950")
    def read(self, event: Button.Pressed) -> None:
        radio = (event.button.id or "").split("-", 1)[1]
        try:
            job = self.client.read(radio)
        except ApiError as e:
            self.error(e)
            return
        self.say(f"Reading {RADIO_NAMES[radio]} on {job['port']} (job {job['id']})...")
        self.follow(job["id"], self.query_one("#radio-log", RichLog))

    @work(thread=True)
    def follow(self, job_id: str, log: RichLog) -> None:
        """Stream a job's log lines into ``log`` until it ends."""
        seen = 0
        while True:
            try:
                job = self.client.job(job_id, wait=5)
            except ApiError as e:
                self.call_from_thread(log.write, f"  lost the job: {e}")
                return
            for line in job["log"][seen:]:
                self.call_from_thread(log.write, f"  {line}")
            seen = len(job["log"])
            if job["status"] != "running":
                break
        self.call_from_thread(log.write, self.job_summary(job))
        self.call_from_thread(self.action_refresh)
        if self.draft:
            self.call_from_thread(self.open_draft, self.draft["id"])

    @staticmethod
    def job_summary(job: dict) -> str:
        r = job.get("result") or {}
        if job["status"] == "failed":
            return f"FAILED: {job['error']}" + (f"\n  report: {r['report_dir']}" if r.get("report_dir") else "")
        if job["kind"] == "read":
            return f"Done: backup {r.get('backup')}"
        if job["kind"] == "dry-run":
            return f"Dry run passed every check; nothing was written. Report: {r.get('report_dir')}"
        if job["kind"] == "write":
            return ("PASSED" if r.get("passed") else "NOT VERIFIED") + f": report {r.get('report_dir')}"
        if job["kind"] == "verify":
            return (
                "Radio matches the draft."
                if r.get("matches_draft")
                else f"Radio DIFFERS from the draft: {list(r.get('differences', {}))}"
            )
        return "Done"

    # -- backups ---------------------------------------------------------------------------

    @on(DataTable.RowHighlighted, "#backup-list")
    def backup_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value:
            self.load_backup(event.row_key.value)

    @work(thread=True, exclusive=True, group="backup")
    def load_backup(self, ref: str) -> None:
        try:
            cp = self.client.backup(ref)
        except ApiError as e:
            self.call_from_thread(self.error, e)
            return

        def show() -> None:
            table = self.query_one("#backup-channels", DataTable)
            table.clear()
            for ch in cp["channels"]:
                table.add_row(*channel_row(ch), key=str(ch["number"]))

        self.call_from_thread(show)

    @on(Button.Pressed, "#new-draft")
    def new_draft(self) -> None:
        ref = self.selected_key(self.query_one("#backup-list", DataTable))
        if ref is None:
            self.notify("Select a backup first", severity="warning")
            return
        try:
            d = self.client.create_draft(ref)
        except ApiError as e:
            self.error(e)
            return
        self.notify(f"Draft {d['id']} created")
        self.query_one(TabbedContent).active = "drafts"
        self.action_refresh()
        self.open_draft(d["id"])

    # -- drafts ------------------------------------------------------------------------------

    @on(DataTable.RowSelected, "#draft-list")
    def draft_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self.open_draft(event.row_key.value)

    def open_draft(self, draft_id: str) -> None:
        try:
            d = self.client.draft(draft_id)
        except ApiError as e:
            self.error(e)
            return
        self.draft = d
        cp = d["codeplug"]
        self.query_one("#draft-title", Label).update(
            f"{d['name']}  [{d['id']}]  {identity_label(d['identity'])}  "
            f"{len(cp['channels'])} channels, {len(d['operations'])} edits"
        )
        table = self.query_one("#draft-channels", DataTable)
        keep = self.selected_key(table)
        table.clear()
        for ch in cp["channels"]:
            table.add_row(*channel_row(ch), key=str(ch["number"]))
        if keep is not None and keep.isdigit():
            table.move_cursor(row=min(int(keep), table.row_count) - 1)
        groups = ["Zones:"]
        groups += [f" {z['number']}. {z['name']}: {', '.join(map(str, z['members']))}" for z in cp["zones"]]
        groups += ["", "Scan lists:"]
        groups += [f" {s['number']}. {s['name']}: {', '.join(map(str, s['members']))}" for s in cp["scan_lists"]]
        self.query_one("#draft-groups", Static).update(Text("\n".join(groups)))

    def current_channel(self) -> dict | None:
        if not self.draft:
            self.notify("Open a draft first", severity="warning")
            return None
        key = self.selected_key(self.query_one("#draft-channels", DataTable))
        return next((c for c in self.draft["codeplug"]["channels"] if str(c["number"]) == key), None)

    def apply(self, operations: list[dict]) -> bool:
        assert self.draft is not None
        try:
            self.client.edit(self.draft["id"], operations)
        except ApiError as e:
            self.error(e)
            return False
        self.open_draft(self.draft["id"])
        return True

    @on(Button.Pressed, "#edit-channel")
    def action_edit_channel(self) -> None:
        ch = self.current_channel()
        if ch is None:
            return

        def done(changes: dict | None) -> None:
            if changes:
                self.apply([{"op": "update_channel", "channel": ch["number"], "changes": changes}])

        self.push_screen(ChannelForm(f"Edit channel {ch['number']} ({ch['mode']})", ch), done)

    @on(Button.Pressed, "#add-channel")
    def action_add_channel(self) -> None:
        if not self.draft:
            self.notify("Open a draft first", severity="warning")
            return

        def done(fields: dict | None) -> None:
            if fields:
                self.apply([{"op": "add_channels", "channels": [fields]}])

        self.push_screen(ChannelForm("Add an analog channel"), done)

    @on(Button.Pressed, "#replace-channel")
    def replace_channel(self) -> None:
        ch = self.current_channel()
        if ch is None:
            return
        base = {k: ch[k] for k in ("name", "rx_mhz", "tx_mhz", "forbid_tx", "power", "scan_list")} | {
            "mode": "analog",
            "bandwidth_khz": 25.0,
        }

        def done(fields: dict | None) -> None:
            if fields is not None:
                self.apply(
                    [
                        {
                            "op": "replace_channel",
                            "channel": ch["number"],
                            "with": {k: v for k, v in base.items() if k != "mode"} | fields,
                        }
                    ]
                )

        self.push_screen(ChannelForm(f"Replace channel {ch['number']} with an analog channel", base), done)

    @on(Button.Pressed, "#delete-channel")
    def action_delete_channel(self) -> None:
        ch = self.current_channel()
        if ch is None:
            return

        def done(yes: bool | None) -> None:
            if yes:
                self.apply([{"op": "delete_channels", "channels": [ch["number"]]}])

        self.push_screen(ConfirmScreen(f"Delete channel {ch['number']} {ch['name']!r}? Later channels move up.", "Delete"), done)

    @on(Button.Pressed, "#undo")
    def action_undo(self) -> None:
        if not self.draft:
            return
        try:
            self.client.undo(self.draft["id"])
        except ApiError as e:
            self.error(e)
            return
        self.open_draft(self.draft["id"])

    def pick_group(self, kind: str, then) -> None:
        if not self.draft:
            self.notify("Open a draft first", severity="warning")
            return
        items = self.draft["codeplug"][kind]
        if not items:
            self.notify(f"The draft has no {kind.replace('_', ' ')}", severity="warning")
            return
        self.push_screen(
            PickScreen("Which one?", [(f"{i['number']}. {i['name']}", str(i["number"])) for i in items]),
            lambda n: n and then(next(i for i in items if str(i["number"]) == n)),
        )

    @on(Button.Pressed, "#add-zone")
    def add_zone(self) -> None:
        if not self.draft:
            return
        self.push_screen(GroupForm("Add a zone"), lambda r: r and self.apply([{"op": "add_zone", **r}]))

    @on(Button.Pressed, "#edit-zone")
    def edit_zone(self) -> None:
        def edit(z: dict) -> None:
            self.push_screen(
                GroupForm(f"Zone {z['number']}", z["name"], z["members"]),
                lambda r: r and self.apply([{"op": "update_zone", "zone": z["number"], **r}]),
            )

        self.pick_group("zones", edit)

    @on(Button.Pressed, "#delete-zone")
    def delete_zone(self) -> None:
        self.pick_group(
            "zones",
            lambda z: self.push_screen(
                ConfirmScreen(f"Delete zone {z['number']} {z['name']!r}?", "Delete"),
                lambda yes: yes and self.apply([{"op": "delete_zone", "zone": z["number"]}]),
            ),
        )

    @on(Button.Pressed, "#edit-scan")
    def edit_scan(self) -> None:
        def edit(s: dict) -> None:
            self.push_screen(
                GroupForm(f"Scan list {s['number']}", s["name"], s["members"], name_max=11),
                lambda r: r and self.apply([{"op": "update_scan_list", "scan_list": s["number"], **r}]),
            )

        self.pick_group("scan_lists", edit)

    @on(Button.Pressed, "#profile")
    def profile(self) -> None:
        if not self.draft:
            return
        try:
            profiles = self.client.profiles()
        except ApiError as e:
            self.error(e)
            return

        def done(name: str | None) -> None:
            if name:
                try:
                    self.client.apply_profile(self.draft["id"], name)
                except ApiError as e:
                    self.error(e)
                    return
                self.open_draft(self.draft["id"])

        self.push_screen(
            PickScreen("Apply a profile", [(f"{p['name']} ({p['channels']} channels)", p["profile"]) for p in profiles]), done
        )

    @on(Button.Pressed, "#delete-draft")
    def delete_draft(self) -> None:
        draft_id = self.selected_key(self.query_one("#draft-list", DataTable))
        if not draft_id:
            return

        def done(yes: bool | None) -> None:
            if not yes:
                return
            try:
                self.client.delete_draft(draft_id)
            except ApiError as e:
                self.error(e)
                return
            if self.draft and self.draft["id"] == draft_id:
                self.draft = None
                self.query_one("#draft-channels", DataTable).clear()
                self.query_one("#draft-title", Label).update("Open a draft from the list")
                self.query_one("#draft-groups", Static).update("")
            self.action_refresh()

        self.push_screen(ConfirmScreen(f"Delete draft {draft_id}? Backups are kept.", "Delete"), done)

    # -- review, write, verify ---------------------------------------------------------------

    @on(Button.Pressed, "#review")
    def action_review(self) -> None:
        if not self.draft:
            self.notify("Open a draft first", severity="warning")
            return
        draft_id = self.draft["id"]
        try:
            pv = self.client.preview(draft_id)
        except ApiError as e:
            self.error(e)
            return

        def done(choice: str | None) -> None:
            if choice not in ("dry", "write"):
                return
            try:
                job = self.client.write(draft_id, pv["preview_id"], dry_run=choice == "dry")
            except ApiError as e:
                self.error(e)
                return
            self.query_one(TabbedContent).active = "jobs"
            log = self.query_one("#job-log", RichLog)
            log.write(
                f"{'Dry run' if choice == 'dry' else 'WRITE'} of draft {draft_id} (job {job['id']}, preview {pv['preview_id']}):"
            )
            self.follow(job["id"], log)

        self.push_screen(ReviewScreen(pv, bool(self.status.get("allow_writes"))), done)

    @on(Button.Pressed, "#verify")
    def verify(self) -> None:
        if not self.draft:
            return
        try:
            job = self.client.verify(self.draft["id"])
        except ApiError as e:
            self.error(e)
            return
        self.query_one(TabbedContent).active = "jobs"
        log = self.query_one("#job-log", RichLog)
        log.write(f"Verify draft {self.draft['id']} against the radio (job {job['id']}):")
        self.follow(job["id"], log)

    @on(DataTable.RowSelected, "#job-list")
    def job_selected(self, event: DataTable.RowSelected) -> None:
        job_id = event.row_key.value
        if not job_id:
            return
        try:
            job = self.client.job(job_id)
        except ApiError as e:
            self.error(e)
            return
        log = self.query_one("#job-log", RichLog)
        log.clear()
        log.write(f"Job {job['id']} ({job['kind']} {job['radio']} on {job['port']}), {job['status']}")
        for line in job["log"]:
            log.write(f"  {line}")
        if job["status"] == "running":
            self.follow(job_id, log)
        else:
            log.write(self.job_summary(job))


def start_embedded_service(url: str, data_dir: Path, allow_writes: bool, profiles_dir: Path) -> None:
    """Run the service in a daemon thread and wait until it answers."""
    from urllib.parse import urlsplit

    import uvicorn

    from radiosmith.server import create_app
    from radiosmith.service import Service

    parts = urlsplit(url)
    service = Service(data_dir, allow_writes=allow_writes, profiles_dir=profiles_dir)
    config = uvicorn.Config(create_app(service), host=parts.hostname or "127.0.0.1", port=parts.port or 8765, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="radiosmith-service", daemon=True).start()
    client = Client.connect(url, timeout=2)
    for _ in range(100):
        if client.alive():
            return
        time.sleep(0.1)
    raise SystemExit(f"could not start the radiosmith service on {url}")


def run(
    url: str = DEFAULT_URL, *, data_dir: Path = Path("data"), allow_writes: bool = False, profiles_dir: Path = Path("profiles")
) -> None:
    # Library log lines written to the console would draw over the TUI.
    for name in ("httpx", "httpcore", "mcp", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    client = Client.connect(url)
    if not client.alive():
        start_embedded_service(url, data_dir, allow_writes, profiles_dir)
    elif allow_writes and not client.status().get("allow_writes"):
        print("Note: a service is already running at", url, "with writes disabled; --allow-writes has no effect on it.")
    RadiosmithApp(client).run()
