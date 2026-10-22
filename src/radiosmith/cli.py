"""Command line interface.

    radiosmith ports
    radiosmith dm32 identify  [--port COM3] [--lines cps]
    radiosmith dm32 read      [--port COM3] [--all-pages]
    radiosmith dm32 diagnose  [--port COM3] [--fast-reconnect] [--sweep-lines]
    radiosmith dm32 write-test [--tag 13] [--yes]
    radiosmith dm32 apply-profile profiles/cda-listen.json [--yes]
    radiosmith rt950 identify [--port COM4]
    radiosmith rt950 read     [--port COM4]
    radiosmith serve          [--port 8765] [--allow-writes]
    radiosmith tui            [--url http://127.0.0.1:8765] [--allow-writes]
    radiosmith backup verify  <backup dir>
    radiosmith backup show    <backup dir>

Only `dm32 write-test --yes` and `dm32 apply-profile --yes` write to a radio.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import serial

from radiosmith import __version__, backup
from radiosmith.dm32 import codeplug, ops
from radiosmith.dm32.edit import EditError
from radiosmith.dm32.protocol import ProtocolError
from radiosmith.dm32.write import UnsafeWrite, WriteUncertain
from radiosmith.rt950.protocol import ProtocolError as RT950ProtocolError
from radiosmith.transport import LINE_PRESETS, TrafficLog, choose_port, list_serial_ports


def _add_radio_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--port", help="serial port, e.g. COM3 or /dev/ttyUSB0 (default: the only CH340 cable)")
    ap.add_argument(
        "--lines", choices=sorted(LINE_PRESETS), default="cps", help="RTS/DTR state to open the port with (default: cps)"
    )
    ap.add_argument("--settle", type=float, default=ops.SETTLE, help="seconds to wait after opening the port")


def _context(args) -> ops.Context:
    return ops.Context(port=choose_port(args.port), data_dir=Path(args.data), lines=args.lines, settle=args.settle)


def cmd_ports(args) -> int:
    ports = list_serial_ports()
    if args.json:
        print(json.dumps([p.to_dict() for p in ports], indent=2))
        return 0
    if not ports:
        print("No serial ports found.")
    for p in ports:
        vidpid = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "-"
        print(f"{p.device:12} {vidpid:10} {'CH340 ' if p.is_ch340 else ''}{p.description}")
    return 0


def cmd_identify(args) -> int:
    ctx = _context(args)
    log_path = Path(args.data) / "logs" / "identify.jsonl"
    log = TrafficLog.to_file(log_path)
    try:
        result = ops.identify(ctx, log)
    finally:
        log.close()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        ident = result["identity"]
        print(f"Model:      {ident['model']}")
        print(f"Firmware:   {ident['firmware']}")
        print(f"Build date: {ident['build_date']}")
        print(f"DSP:        {ident['dsp_version']}")
        print(f"Codeplug:   {ident['codeplug_range']}")

        def flag(key: str) -> str:
            return "set" if ident[key] else "not set"

        print(f"Passwords:  write {flag('write_password_set')}, read {flag('read_password_set')}")
        print(f"Port:       {result['port']['device']} ({result['port']['description']}), lines {result['port']['lines']}")
        print(f"Link:       {result['link_stats'] or 'no errors'}")
        print(f"Log:        {log_path}")
    return 0


def cmd_read(args) -> int:
    ctx = _context(args)
    try:
        r = ops.read_codeplug(ctx, all_pages=args.all_pages, copies=args.copies)
    except ops.ReadFailed as e:
        print(f"READ FAILED: {e}", file=sys.stderr)
        if e.backup_dir:
            print(f"Partial backup (marked incomplete): {e.backup_dir}", file=sys.stderr)
        return 1
    s = r.summary
    print(f"Backup:   {r.backup_dir}")
    print(f"Pages:    {r.pages_saved} saved, {r.pages_in_use} in use")
    print(f"Channels: {s['channels_decoded']} (header count {s['channel_count_header']})")
    print(f"Zones:    {len(s['zones'])}  " + ", ".join(f"{z['name']} ({z['members']})" for z in s["zones"]))
    print(f"Scan:     {len(s['scan_lists'])}  " + ", ".join(f"{x['name']} ({x['members']})" for x in s["scan_lists"]))
    print(f"Link:     {r.link_stats or 'no errors'}")
    for problem in s["problems"]:
        print(f"Problem:  {problem}")
    return 0


def cmd_diagnose(args) -> int:
    ctx = _context(args)
    out, results = ops.diagnose(ctx, gap=args.gap, fast_reconnect=args.fast_reconnect, sweep_lines=args.sweep_lines)
    print()
    print(f"{'case':28} {'result':8} {'secs':>6}  detail")
    for r in results:
        detail = r.error or ", ".join(f"{k}={v}" for k, v in r.detail.items() if k in ("model", "firmware"))
        print(f"{r.name:28} {'OK' if r.ok else 'FAILED':8} {r.seconds:6.1f}  {detail}")
    print(f"\nReport and traffic log: {out}")
    return 0 if all(r.ok for r in results if not r.name.startswith(("fast_", "recovery_", "lines_"))) else 1


def _run_write(args, run) -> int:
    try:
        out, report = run()
    except ops.WriteTestAborted as e:
        print(f"ABORTED before writing: {e}", file=sys.stderr)
        return 1
    except ops.ReadFailed as e:
        print(f"READ FAILED: {e}", file=sys.stderr)
        return 1
    except EditError as e:
        print(f"EDIT REFUSED (nothing written): {e}", file=sys.stderr)
        return 1
    pv = report.get("preview", {})
    for ch in pv.get("channels_added", []):
        rx = f"{ch['rx_mhz']:.5f}"
        tx = f"{ch['tx_mhz']:.5f}" if ch["tx_mhz"] is not None else "none"
        print(
            f"  + channel {ch['number']:3} {ch['name']:16} rx {rx} tx {tx} {ch['mode']} {ch['bandwidth_khz']:g} kHz "
            f"{ch['power']} {'TX FORBIDDEN' if ch['forbid_tx'] else 'TX allowed'}"
        )
    for z in pv.get("zones_added", []):
        print(f"  + zone {z['number']} {z['name']!r}: channels {z['members']}")
    for key in ("channels_changed", "channels_removed", "zones_changed", "zones_removed"):
        if pv.get(key):
            print(f"  {key}: {len(pv[key])}")
    for st in report["steps"]:
        print(f"  {st['step']}: " + ", ".join(f"{k}={v}" for k, v in st.items() if k not in ("step", "diff")))
    if not args.yes:
        print("Dry run: checks passed, nothing written. Add --yes to write.")
        return 0
    print("PASSED" if report.get("passed") else "FAILED")
    print(f"Report: {out}")
    return 0 if report.get("passed") else 1


def cmd_write_test(args) -> int:
    ctx = _context(args)
    tag = int(args.tag, 16)
    return _run_write(args, lambda: ops.noop_write_test(ctx, tag=tag, really_write=args.yes, copies=args.copies))


def cmd_apply_profile(args) -> int:
    profile = ops.load_profile(Path(args.profile))
    ctx = _context(args)
    return _run_write(args, lambda: ops.apply_profile(ctx, profile, really_write=args.yes, copies=args.copies))


def cmd_rt950_identify(args) -> int:
    from radiosmith.rt950 import ops as rt_ops

    ctx = _context(args)
    log = TrafficLog.to_file(Path(args.data) / "logs" / "rt950-identify.jsonl")
    try:
        result = rt_ops.identify(ctx, log)
    finally:
        log.close()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        ident = result["identity"]
        print(f"Model:         {ident['model']}")
        print(f"Firmware info: {ident['firmware_info']} (raw F reply)")
        print(f"Port:          {result['port']['device']}, lines {result['port']['lines']}")
        print(f"Link:          {result['link_stats'] or 'no errors'}")
    return 0


def _run_rt950_write(args, run) -> int:
    from radiosmith.rt950.ops import WriteAborted
    from radiosmith.rt950.write import UnsafeWrite, WriteUncertain

    try:
        out, report = run()
    except (WriteAborted, EditError) as e:
        print(f"ABORTED before writing: {e}", file=sys.stderr)
        return 1
    except ops.ReadFailed as e:
        print(f"READ FAILED: {e}", file=sys.stderr)
        return 1
    except (WriteUncertain, UnsafeWrite) as e:
        print(f"WRITE FAILED: {e}", file=sys.stderr)
        return 1
    for ch in report.get("preview", {}).get("channels_added", []):
        tx = f"{ch['tx_mhz']:.5f}" if ch["tx_mhz"] is not None else "none (RX only)"
        bw = f"{ch['bandwidth_khz']:g} kHz"
        am = " AM" if ch.get("rx_am") else ""
        print(f"  + channel {ch['number']:3} {ch['name']:12} rx {ch['rx_mhz']:.5f}{am} tx {tx} {bw} {ch['power']}")
    for st in report["steps"]:
        print(f"  {st['step']}: " + ", ".join(f"{k}={v}" for k, v in st.items() if k != "step"))
    if not args.yes:
        print("Dry run: checks passed, nothing written. Add --yes to write.")
        return 0
    print("PASSED" if report.get("passed") else "FAILED")
    print(f"Report: {out}")
    return 0 if report.get("passed") else 1


def cmd_rt950_write_test(args) -> int:
    from radiosmith.rt950 import ops as rt_ops

    ctx = _context(args)
    sector = int(args.sector, 16)
    return _run_rt950_write(args, lambda: rt_ops.noop_write_test(ctx, sector=sector, really_write=args.yes, copies=args.copies))


def cmd_rt950_restore(args) -> int:
    from radiosmith.rt950 import ops as rt_ops

    ctx = _context(args)
    sectors = [int(x, 16) for x in args.sectors.split(",")]
    return _run_rt950_write(
        args, lambda: rt_ops.restore_sectors(ctx, Path(args.source), sectors, really_write=args.yes, copies=args.copies)
    )


def cmd_rt950_apply_profile(args) -> int:
    from radiosmith.rt950 import ops as rt_ops

    profile = rt_ops.load_profile(Path(args.profile))
    ctx = _context(args)
    return _run_rt950_write(
        args, lambda: rt_ops.apply_profile(ctx, profile, really_write=args.yes, copies=args.copies, at=args.at)
    )


def cmd_rt950_compare_cps(args) -> int:
    from radiosmith.rt950 import cpsfile

    if args.backup:
        b = Path(args.backup)
    else:
        found = sorted((Path(args.data) / "backups" / "rt950").glob("*/image.bin"))
        if not found:
            print("No RT-950 PRO backup; run `radiosmith rt950 read` first.", file=sys.stderr)
            return 1
        b = found[-1].parent
    loaded = backup.load_backup(b)
    if not loaded.ok:
        print(f"Backup {b} has problems: {loaded.problems}", file=sys.stderr)
        return 1
    r = cpsfile.compare(cpsfile.load(Path(args.cps_file)), (b / "image.bin").read_bytes())
    print(f"Backup: {b}")
    print(f"Channels: CPS {r['channels_cps']}, radiosmith {r['channels_radiosmith']}")
    for d in r["channel_differences"]:
        print(
            f"  channel {d['channel']}: "
            + (f"only in {d['only_in']}" if "only_in" in d else f"{d['field']} CPS {d['cps']!r} vs {d['radiosmith']!r}")
        )
    for k, v in r["vfos"].items():
        print(f"  {k}: CPS {v['cps']}, radiosmith {v['radiosmith']}")
    ok = not r["channel_differences"] and r["vfos_match"]
    print("MATCH" if ok else "DIFFERENT")
    return 0 if ok else 1


def cmd_rt950_read(args) -> int:
    from radiosmith.rt950 import ops as rt_ops

    ctx = _context(args)
    try:
        out, summary = rt_ops.read_image(ctx, copies=args.copies)
    except ops.ReadFailed as e:
        print(f"READ FAILED: {e}", file=sys.stderr)
        if e.backup_dir:
            print(f"Partial backup (marked incomplete): {e.backup_dir}", file=sys.stderr)
        return 1
    print(f"Backup: {out}")
    print(f"Image:  {summary['bytes']} bytes, APRS block {summary['aprs_bytes']} bytes")
    print(f"Link:   {summary['link_stats'] or 'no errors'}")
    return 0


def cmd_serve(args) -> int:
    from radiosmith.server import serve
    from radiosmith.service import Service

    service = Service(Path(args.data), allow_writes=args.allow_writes, profiles_dir=Path(args.profiles))
    print(f"radiosmith on http://{args.host}:{args.http_port}/  (MCP: http://{args.host}:{args.http_port}/mcp)")
    print("Writes to radios are " + ("ENABLED" if args.allow_writes else "disabled (start with --allow-writes to enable)"))
    serve(service, host=args.host, port=args.http_port)
    return 0


def cmd_tui(args) -> int:
    from radiosmith import tui

    tui.run(args.url, data_dir=Path(args.data), allow_writes=args.allow_writes, profiles_dir=Path(args.profiles))
    return 0


def cmd_backup_verify(args) -> int:
    b = backup.load_backup(Path(args.dir))
    print(f"{b.path}: {len(b.pages)} pages, {'OK' if b.ok else 'PROBLEMS'}")
    for problem in b.problems:
        print(f"  {problem}")
    return 0 if b.ok else 1


def cmd_backup_diff(args) -> int:
    old, new = backup.load_backup(Path(args.old)), backup.load_backup(Path(args.new))
    for b in (old, new):
        for problem in b.problems:
            print(f"Warning ({b.path.name}): {problem}")
    d = backup.diff_pages(old.pages, new.pages)
    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print("Identical by tag." if d["identical"] else "DIFFERENT.")
        for tag, info in d["changed"].items():
            print(f"  tag {tag}: {info['bytes_differ']} bytes differ at {', '.join(info['first_offsets'])}")
        for key in ("only_in_old", "only_in_new"):
            if d[key]:
                print(f"  {key}: {', '.join(d[key])}")
        print(f"  pages moved: {len(d['moved'])}; stale pages old/new: {d['stale_pages'][0]}/{d['stale_pages'][1]}")
    return 0 if d["identical"] else 1


def cmd_backup_show(args) -> int:
    b = backup.load_backup(Path(args.dir))
    for problem in b.problems:
        print(f"Warning: {problem}")
    ident = b.manifest.get("identity", {})
    print(f"{ident.get('model')} firmware {ident.get('firmware')}, read {b.manifest.get('created_utc')}")
    cp = codeplug.decode(b.pages)
    print(f"\n{'#':>4}  {'name':16} {'rx MHz':>10} {'tx MHz':>10}  {'mode':8} {'bw':>4}  {'pwr':6} {'tx?':4} tones")
    for ch in cp.channels:
        rx = f"{ch.rx_mhz:.5f}" if ch.rx_mhz is not None else "-"
        tx = f"{ch.tx_mhz:.5f}" if ch.tx_mhz is not None else "-"
        tones = f"{ch.rx_tone or '-'}/{ch.tx_tone or '-'}" if ch.color_code is None else f"CC{ch.color_code} TS{ch.time_slot}"
        can_tx = "no" if ch.forbid_tx else "yes"
        print(f"{ch.number:4}  {ch.name:16} {rx:>10} {tx:>10}  {ch.mode:8} {ch.bandwidth_khz:4g} {ch.power:6} {can_tx:4} {tones}")
    print()
    for z in cp.zones:
        print(f"Zone {z.number} {z.name!r}: {z.members}")
    for s in cp.scan_lists:
        print(f"Scan list {s.number} {s.name!r}: {s.members}")
    for problem in cp.problems:
        print(f"Problem: {problem}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="radiosmith", description="Local radio programmer (read-only for now).")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--data", default="data", help="directory for backups, logs and diagnostics (default: ./data)")
    sub = ap.add_subparsers(dest="command", required=True)

    ports = sub.add_parser("ports", help="list serial ports")
    ports.add_argument("--json", action="store_true")
    ports.set_defaults(func=cmd_ports)

    dm32 = sub.add_parser("dm32", help="Baofeng DM-32UV").add_subparsers(dest="action", required=True)
    ident = dm32.add_parser("identify", help="identify the radio (no programming mode)")
    _add_radio_args(ident)
    ident.add_argument("--json", action="store_true")
    ident.set_defaults(func=cmd_identify)

    read = dm32.add_parser("read", help="read the whole codeplug into a new backup")
    _add_radio_args(read)
    read.add_argument("--all-pages", action="store_true", help="also save free pages")
    read.add_argument("--copies", type=int, default=3, help="read each block this many times and merge (default 3)")
    read.set_defaults(func=cmd_read)

    diag = dm32.add_parser("diagnose", help="run connection cases without changing radio memory")
    _add_radio_args(diag)
    diag.add_argument("--gap", type=float, default=ops.MIN_SESSION_GAP, help="seconds between cases (default 5)")
    diag.add_argument(
        "--fast-reconnect",
        action="store_true",
        help="also reconnect 0.4 s after a session like NeonPlug (may need a power cycle after)",
    )
    diag.add_argument("--sweep-lines", action="store_true", help="also try every RTS/DTR preset (may need a power cycle after)")
    diag.set_defaults(func=cmd_diagnose)

    wt = dm32.add_parser("write-test", help="rewrite one page with its own contents, read back, compare full backups")
    _add_radio_args(wt)
    wt.add_argument("--tag", default="13", help="page tag in hex (default 13: channels 85-169)")
    wt.add_argument("--copies", type=int, default=3)
    wt.add_argument("--yes", action="store_true", help="really send the write (default: dry run)")
    wt.set_defaults(func=cmd_write_test)

    ap_ = dm32.add_parser("apply-profile", help="add a profile's channels and zone to the radio (dry run unless --yes)")
    _add_radio_args(ap_)
    ap_.add_argument("profile", help="profile JSON, e.g. profiles/cda-listen.json")
    ap_.add_argument("--copies", type=int, default=3)
    ap_.add_argument("--yes", action="store_true", help="really write (default: dry run)")
    ap_.set_defaults(func=cmd_apply_profile)

    rt = sub.add_parser("rt950", help="Radtel RT-950 PRO (read-only)").add_subparsers(dest="action", required=True)
    rt_ident = rt.add_parser("identify", help="handshake and model query, then end the session")
    _add_radio_args(rt_ident)
    rt_ident.add_argument("--json", action="store_true")
    rt_ident.set_defaults(func=cmd_rt950_identify)
    rt_read = rt.add_parser("read", help="read the whole image into a new backup")
    _add_radio_args(rt_read)
    rt_read.add_argument("--copies", type=int, default=3, help="read each block this many times and merge (default 3)")
    rt_read.set_defaults(func=cmd_rt950_read)
    rt_cmp = rt.add_parser("compare-cps", help="compare a vendor CPS file (.dat) with a backup")
    rt_cmp.add_argument("cps_file", help="file saved by the RT-950 PRO CPS after reading the radio")
    rt_cmp.add_argument("--backup", help="backup directory (default: the newest RT-950 PRO backup)")
    rt_cmp.set_defaults(func=cmd_rt950_compare_cps)
    rt_wt = rt.add_parser("write-test", help="rewrite one 4 KB channel sector with its own contents, read back, compare backups")
    _add_radio_args(rt_wt)
    rt_wt.add_argument("--sector", default="0000", help="sector address in hex (default 0000: channels 1-128)")
    rt_wt.add_argument("--copies", type=int, default=3)
    rt_wt.add_argument("--yes", action="store_true", help="really send the write (default: dry run)")
    rt_wt.set_defaults(func=cmd_rt950_write_test)
    rt_rs = rt.add_parser("restore", help="write sectors back from an earlier backup (dry run unless --yes)")
    _add_radio_args(rt_rs)
    rt_rs.add_argument("source", help="backup directory to restore from")
    rt_rs.add_argument("--sectors", default="0000", help="comma-separated sector addresses in hex (default 0000)")
    rt_rs.add_argument("--copies", type=int, default=3)
    rt_rs.add_argument("--yes", action="store_true", help="really write (default: dry run)")
    rt_rs.set_defaults(func=cmd_rt950_restore)
    rt_ap = rt.add_parser("apply-profile", help="add a profile's channels after the last used channel (dry run unless --yes)")
    _add_radio_args(rt_ap)
    rt_ap.add_argument("profile", help="profile JSON, e.g. profiles/cda-listen.json")
    rt_ap.add_argument("--at", type=int, help="first slot to use (default: after the last used channel)")
    rt_ap.add_argument("--copies", type=int, default=3)
    rt_ap.add_argument("--yes", action="store_true", help="really write (default: dry run)")
    rt_ap.set_defaults(func=cmd_rt950_apply_profile)

    srv = sub.add_parser("serve", help="run the local web interface and MCP server")
    srv.add_argument("--host", default="127.0.0.1", help="address to listen on (this computer only; default 127.0.0.1)")
    srv.add_argument("--port", dest="http_port", type=int, default=8765, help="HTTP port (default 8765)")
    srv.add_argument("--profiles", default="profiles", help="directory of channel profiles (default ./profiles)")
    srv.add_argument("--allow-writes", action="store_true", help="allow writing drafts to a radio")
    srv.set_defaults(func=cmd_serve)

    tui = sub.add_parser("tui", help="terminal interface (starts the service if none is running)")
    tui.add_argument("--url", default="http://127.0.0.1:8765", help="service to use or start (default http://127.0.0.1:8765)")
    tui.add_argument("--profiles", default="profiles", help="directory of channel profiles, if the service is started here")
    tui.add_argument("--allow-writes", action="store_true", help="allow writes, if the service is started here")
    tui.set_defaults(func=cmd_tui)

    bk = sub.add_parser("backup", help="inspect backups").add_subparsers(dest="action", required=True)
    verify = bk.add_parser("verify", help="check a backup's files against its hashes")
    verify.add_argument("dir")
    verify.set_defaults(func=cmd_backup_verify)
    diff = bk.add_parser("diff", help="compare two backups page by page (matched by tag)")
    diff.add_argument("old")
    diff.add_argument("new")
    diff.add_argument("--json", action="store_true")
    diff.set_defaults(func=cmd_backup_diff)
    show = bk.add_parser("show", help="decode channels, zones and scan lists from a backup")
    show.add_argument("dir")
    show.set_defaults(func=cmd_backup_show)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LookupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (ProtocolError, RT950ProtocolError, serial.SerialException, WriteUncertain, UnsafeWrite) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
