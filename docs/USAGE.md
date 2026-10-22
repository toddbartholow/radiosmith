# Using radiosmith

## Commands

Close any other program that uses the port (vendor CPS, CHIRP, NeonPlug) first.

```sh
uv run radiosmith ports                    # list serial ports; CH340 cables are marked
uv run radiosmith dm32 identify            # model, firmware, codeplug range; no programming mode
uv run radiosmith dm32 read                # full codeplug into data/backups/dm32/<time>_<model>_<fw>/
uv run radiosmith dm32 diagnose            # separate connection cases, see below
uv run radiosmith dm32 write-test --tag 13 # dry run of the page-rewrite test; --yes writes
uv run radiosmith dm32 apply-profile profiles/cda-listen.json   # preview and dry run; --yes writes
uv run radiosmith rt950 identify --port COM4   # RT-950 PRO handshake and model query
uv run radiosmith rt950 read --port COM4       # full image into data/backups/rt950/
uv run radiosmith rt950 compare-cps <file.dat> # compare a vendor CPS file with the newest backup
uv run radiosmith rt950 apply-profile profiles/cda-listen.json --port COM4   # dry run; --yes writes
uv run radiosmith rt950 restore <backup dir> --sectors 0000 --port COM4    # dry run; --yes writes
uv run radiosmith backup verify <dir>      # check a backup against its SHA-256 manifest
uv run radiosmith backup diff <old> <new>  # compare two backups page by page
uv run radiosmith backup show <dir>        # decode channels, zones and scan lists
uv run radiosmith serve                    # web interface and MCP server
uv run radiosmith tui                      # terminal interface
```

`--port COM3` (or `/dev/ttyUSB0`) picks a port; by default the only CH340 cable
is used. `--lines` sets the RTS/DTR state the port opens with: `cps` (both
asserted, the vendor default), `rts-only`, `dtr-only` or `none`.

`dm32 diagnose` runs these cases, 5 s apart: fresh identify, reconnect and
identify, a read session in programming mode, and a write-session handshake
that stops before any write frame. Two opt-in cases can leave the radio
unresponsive until it is power cycled: `--fast-reconnect` (reconnect 0.4 s
after a session, then probe recovery) and `--sweep-lines` (identify with each
line preset). Each run saves `report.json` and a traffic log under
`data/diagnostics/`.

## The service

```sh
uv run radiosmith serve                  # web GUI on http://127.0.0.1:8765/ ; writes disabled
uv run radiosmith serve --allow-writes   # also allow writing drafts to a radio
uv run radiosmith tui                    # terminal interface
```

The web GUI, the terminal interface (TUI) and MCP clients all go through the
same local service, so they can be used side by side. `radiosmith tui`
connects to a running service, or starts one inside itself (on the same port,
so the web GUI and MCP work while it runs); `tui --allow-writes` applies only
when it starts the service. TUI keys: `e` edit channel, `a` add channel, `Del`
delete channel, `u` undo, `w` review and write, `F5` refresh, `q` quit.

The service listens on this computer only and refuses requests whose Host or
Origin is not local, so other web pages cannot reach the radio through the
browser. It runs one radio operation at a time. Full reads and writes run as
jobs with a progress log. It remembers which port answered as which radio,
which matters when both radios use CH340 cables with the same USB ID.

## Workflow

The workflow is the same in the web interface, the TUI and over MCP:

1. Identify the radio and read it into a backup.
2. Create a draft from the backup and edit it. A draft is the backup plus a
   list of edit operations, saved under `data/drafts/<id>/` with the radio it
   is for. Drafts never touch the radio.
   - DM-32UV: add, update, replace and delete channels (later channels are
     renumbered, and zones and scan lists follow); add, rename, reorder and
     delete zones; scan list members.
   - RT-950 PRO: add (optionally into a chosen slot), update, replace and
     delete channels. Channels are fixed slots and are never renumbered, and
     zones are fixed ranges of slots. A receive-only channel is one without a
     TX frequency.
3. Preview the write. The preview lists the decoded changes and the memory
   they touch (DM-32UV pages, RT-950 PRO 4 KB sectors), and carries a
   `preview_id`.
4. Write with that `preview_id`. The write takes a fresh backup and replays the
   draft's operations on it. It refuses if the radio's model or firmware
   differ, or if the replay would change anything other than the approved
   preview. Then it writes only the changed pages or sectors, reads each back,
   and compares a second full backup with the plan.
5. Verify: read the radio again and compare its channels (and zones and scan
   lists) with the draft.

## MCP clients

The MCP endpoint is `http://127.0.0.1:8765/mcp` (Streamable HTTP); start the
service first.

```sh
claude mcp add --transport http radiosmith http://127.0.0.1:8765/mcp   # Claude Code
```

For Codex, add to `~/.codex/config.toml` (check your Codex version's MCP docs
for Streamable HTTP support):

```toml
[mcp_servers.radiosmith]
url = "http://127.0.0.1:8765/mcp"
```

Tools:

- Radio access: `status`, `list_ports`, `identify_radio`, `read_radio`,
  `get_job`, `write_radio`, `verify_radio`.
- Backups and profiles: `list_backups`, `get_backup`, `list_profiles`,
  `apply_profile`.
- Drafts: `create_draft`, `list_drafts`, `get_draft`, `edit_draft`,
  `undo_draft`, `preview_write`, `export_channels_csv`,
  `import_channels_csv`.

Only `write_radio` changes a radio. It is marked destructive, needs
`--allow-writes`, and can also run as a dry run.

## Profiles

A profile is a JSON file listing analog channels to add to the radio; see
`profiles/` for examples. Each channel has `name` (up to 16 ASCII characters;
12 on the RT-950 PRO), `rx_mhz`, `tx_mhz` (`null` for none), `forbid_tx`,
`power` (`low`, `middle`, `high`), `bandwidth_khz` (12.5 or 25) and optional
`rx_tone`/`tx_tone` (CTCSS in Hz). A channel with no TX frequency must forbid
TX. `rx_am: true` receives AM, for airband; only the RT-950 PRO can, so a
profile with AM channels is refused for the DM-32UV.

On the DM-32UV the channels go into a new zone named by the profile's `zone`.
On the RT-950 PRO they go into the empty slots after the last used channel,
or from the slot given with `--at` (`at` over MCP); zones are fixed ranges of
99 slots, so `--at 100` starts zone 2.

Example profiles, all receive only, for Coeur d'Alene, Idaho:

- `cda-listen`: NOAA weather and the outputs of nearby linked repeaters.
- `cda-public-service`: public service channels.
- `cda-outdoors`: marine, FRS/GMRS, MURS and simplex calling channels.
- `cda-airband`: AM aviation channels (RT-950 PRO only).

`apply-profile` makes a full backup, shows the channels it will add and the
memory that changes, checks that the radio still matches the backup, and stops
there unless `--yes` is given. With `--yes` it writes, reads each page or
sector back, re-reads the whole radio and checks it equals the backup plus the
planned changes. It refuses a zone or channel name that already exists, so
running it twice does not add duplicates.

## Channel CSV

`export_channels_csv` (or the web interface) produces
`number,name,rx_mhz,tx_mhz,mode,forbid_tx,power,bandwidth_khz,rx_tone,tx_tone,scan_list`
for the DM-32UV and
`number,name,rx_mhz,tx_mhz,power,bandwidth_khz,rx_tone,tx_tone,scan_add,busy_lock,rx_am`
for the RT-950 PRO (a file without the `rx_am` column leaves AM unchanged).
Importing an edited CSV into a draft updates the rows whose fields changed,
adds rows with an empty `number`, and deletes channels missing from the file.
Setting `mode` to `analog` on a digital channel replaces it with an analog one.
This is radiosmith's own format, not the vendor CPS's.

## Data

`data/` holds backups, drafts, write reports, traffic logs and diagnostics. It
is git-ignored because backups contain personal data: DMR IDs, contacts, and
any radio passwords in clear text. Traffic logs record handshake bytes in full
but only the length, first bytes and a hash of each page.

A DM-32UV backup directory contains `manifest.json` (identity, port, page tag
map, SHA-256 of every file), `pages/<address>_tag<tag>.bin` for every page in
use (including stale and unidentified pages, so nothing is dropped),
`g_block.bin` and `traffic.jsonl`. An RT-950 PRO backup holds `image.bin` and
`aprs.bin` with the same kind of manifest.
