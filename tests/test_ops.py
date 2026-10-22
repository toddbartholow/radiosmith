import json

import pytest

from radiosmith import backup
from radiosmith.dm32 import ops
from radiosmith.dm32 import protocol as p
from radiosmith.transport import PortInfo
from tests.fake_dm32 import FakeDM32, sample_pages

PORT = PortInfo(device="FAKE1", description="USB-SERIAL CH340 (fake)", vid=0x1A86, pid=0x7523, manufacturer=None, hwid="")


class Bench:
    """Hands out a fresh FakeDM32 per session and records sleeps."""

    def __init__(self, **radio_kwargs):
        self.radio_kwargs = radio_kwargs
        self.radios: list[FakeDM32] = []
        self.lines: list[str] = []
        self.sleeps: list[float] = []

    def factory(self, device, lines, log):
        radio = FakeDM32(**self.radio_kwargs)
        self.radios.append(radio)
        self.lines.append(lines)
        return radio

    def context(self, tmp_path, **kw) -> ops.Context:
        return ops.Context(
            port=PORT, data_dir=tmp_path, link_factory=self.factory, sleep=self.sleeps.append, say=lambda _m: None, **kw
        )


def test_identify_op(tmp_path):
    bench = Bench()
    result = ops.identify(bench.context(tmp_path), ops.TrafficLog.null())
    assert result["identity"]["firmware"] == "DM32.NRF.01.049"
    assert result["port"]["vid"] == "1A86" and result["port"]["lines"] == "cps"
    assert bench.radios[0].closed


def test_read_codeplug_writes_verifiable_backup(tmp_path):
    pages = sample_pages()
    bench = Bench(pages=pages, fault_rate=0.001, seed=3)
    result = ops.read_codeplug(bench.context(tmp_path))
    assert result.pages_in_use == len(pages) and result.pages_saved == len(pages)
    assert result.summary["channels_decoded"] == 5
    assert [z["name"] for z in result.summary["zones"]] == ["CDA Listen", "Repeaters"]
    for frame in bench.radios[0].received:
        p.check_read_only(frame)

    b = backup.load_backup(result.backup_dir)
    assert b.ok, b.problems
    assert b.pages == pages
    assert (result.backup_dir / "g_block.bin").read_bytes() == b"\xff" * 256
    assert (result.backup_dir / "traffic.jsonl").exists()
    assert b.manifest["identity"]["model"] == "DP570UV"


def test_backup_detects_tampering(tmp_path):
    result = ops.read_codeplug(Bench(pages=sample_pages()).context(tmp_path))
    victim = next((result.backup_dir / "pages").iterdir())
    data = bytearray(victim.read_bytes())
    data[0] ^= 0xFF
    victim.write_bytes(bytes(data))
    b = backup.load_backup(result.backup_dir)
    assert not b.ok
    assert any("hash mismatch" in prob for prob in b.problems)


def test_backup_reads_pre_rename_format(tmp_path):
    result = ops.read_codeplug(Bench(pages=sample_pages()).context(tmp_path))
    manifest_path = result.backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "radiosmith-backup/1"

    manifest["format"] = "radio-mcp-backup/1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    b = backup.load_backup(result.backup_dir)
    assert b.ok, b.problems

    manifest["format"] = "something-else/1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert any("unknown format" in prob for prob in backup.load_backup(result.backup_dir).problems)


def test_diff_matches_pages_by_tag():
    pages = sample_pages()
    moved = {(a + 0x1000 if d[-1] == 0x12 else a): d for a, d in pages.items()}
    d = backup.diff_pages(pages, moved)
    assert d["identical"] and d["moved"] == {"12": ["007000", "008000"]}

    edited = dict(pages)
    data = bytearray(edited[0x007000])
    data[0x10] = ord("X")
    edited[0x007000] = bytes(data)
    d = backup.diff_pages(pages, edited)
    assert not d["identical"]
    assert d["changed"]["12"]["bytes_differ"] == 1 and d["changed"]["12"]["first_offsets"] == ["010"]


def test_interrupted_read_leaves_incomplete_backup(tmp_path):
    bench = Bench(pages=sample_pages(), silent_after=150)
    with pytest.raises(ops.ReadFailed) as err:
        ops.read_codeplug(bench.context(tmp_path))
    assert err.value.backup_dir is not None
    manifest = json.loads((err.value.backup_dir / "manifest.json").read_text())
    assert manifest["complete"] is False
    assert "page scan" in manifest["notes"][0]
    assert not backup.load_backup(err.value.backup_dir).ok


def test_unreachable_radio_makes_no_backup(tmp_path):
    with pytest.raises(ops.ReadFailed) as err:
        ops.read_codeplug(Bench(silent=True).context(tmp_path))
    assert err.value.backup_dir is None
    assert not (tmp_path / "backups" / "dm32").exists()


def test_sessions_are_spaced_across_calls(tmp_path):
    bench = Bench()
    ctx = bench.context(tmp_path)
    ops.identify(ctx, ops.TrafficLog.null())
    ops.identify(ctx, ops.TrafficLog.null())
    gap_waits = [s for s in bench.sleeps if s > 1.0]
    assert gap_waits and ops.MIN_SESSION_GAP - 1.0 < gap_waits[0] <= ops.MIN_SESSION_GAP


def test_diagnose_runs_cases_in_order(tmp_path):
    bench = Bench(pages=sample_pages())
    out, results = ops.diagnose(bench.context(tmp_path), sweep_lines=True)
    names = [r.name for r in results]
    assert names[:4] == ["fresh_identify", "reconnect_identify", "read_session", "write_prep"]
    assert names[4:] == ["lines_cps", "lines_rts-only", "lines_dtr-only", "lines_none"]
    assert all(r.ok for r in results)
    assert bench.lines[-4:] == ["cps", "rts-only", "dtr-only", "none"]
    assert p.G_READ not in bench.radios[3].received  # write_prep has no G read
    report = json.loads((out / "report.json").read_text())
    assert len(report["cases"]) == 8
    for radio in bench.radios:
        for frame in radio.received:
            p.check_read_only(frame)


def test_diagnose_stops_after_failed_first_case(tmp_path):
    out, results = ops.diagnose(Bench(silent=True).context(tmp_path))
    assert [r.name for r in results] == ["fresh_identify"]
    assert not results[0].ok and results[0].stage == "PSEARCH"


def test_fast_reconnect_probes_recovery(tmp_path):
    bench = Bench(pages=sample_pages())
    real_factory = bench.factory
    calls = {"n": 0}

    def factory(device, lines, log):
        calls["n"] += 1
        radio = real_factory(device, lines, log)
        if calls["n"] == 6:  # the fast reconnect: radio still busy, no reply
            radio.silent = True
        return radio

    ctx = bench.context(tmp_path)
    ctx.link_factory = factory
    _, results = ops.diagnose(ctx, fast_reconnect=True)
    by_name = {r.name: r for r in results}
    assert not by_name["fast_reconnect"].ok
    assert by_name["fast_reconnect"].gap_before == ops.NEONPLUG_REOPEN_DELAY
    assert by_name["recovery_after_3s"].ok
