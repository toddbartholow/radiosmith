import json

import pytest

from radiosmith.dm32 import ops
from radiosmith.dm32.draft import DraftError, DraftStore, operations_from_csv
from tests.fake_dm32 import page, sample_pages
from tests.test_ops import Bench

ADD = {"op": "add_channels", "channels": [{"name": "Simplex", "rx_mhz": 146.52}]}


def make_backup(tmp_path, pages=None):
    pages = pages if pages is not None else sample_pages()
    bench = Bench(pages=pages)
    ctx = bench.context(tmp_path)
    return bench, ctx, ops.read_codeplug(ctx).backup_dir


def test_draft_survives_save_and_reload(tmp_path):
    _, _, b = make_backup(tmp_path)
    store = DraftStore(tmp_path)
    d = store.create(b, "Hike")
    d.apply([ADD, {"op": "add_zone", "name": "Hike", "members": ["Simplex", 1]}])
    store.save(d)

    again = store.load(d.id)
    assert again.name == "Hike" and len(again.operations) == 2
    assert again.state == d.state
    assert [z["name"] for z in again.codeplug()["zones"]][-1] == "Hike"
    assert "Simplex" in (tmp_path / "drafts" / d.id / "channels.csv").read_text()
    assert store.list()[0]["id"] == d.id


def test_preview_lists_decoded_changes_and_pages(tmp_path):
    _, _, b = make_backup(tmp_path)
    d = DraftStore(tmp_path).create(b)
    assert d.preview()["nothing_to_write"]
    d.apply([ADD, {"op": "update_channel", "channel": 5, "changes": {"name": "Rpt"}}])
    pv = d.preview()
    assert [c["name"] for c in pv["changes"]["channels_added"]] == ["Simplex"]
    assert pv["changes"]["channels_changed"][0]["after"]["name"] == "Rpt"
    assert [pg["tag"] for pg in pv["pages"]] == ["12"]
    assert len(pv["preview_id"]) == 16


def test_invalid_operation_adds_nothing(tmp_path):
    _, _, b = make_backup(tmp_path)
    d = DraftStore(tmp_path).create(b)
    with pytest.raises(Exception, match="no channel named"):
        d.apply([ADD, {"op": "add_zone", "name": "Z", "members": ["Nope"]}])
    assert d.operations == [] and d.preview()["nothing_to_write"]


def test_undo(tmp_path):
    _, _, b = make_backup(tmp_path)
    d = DraftStore(tmp_path).create(b)
    d.apply([ADD])
    d.undo()
    assert d.operations == [] and d.preview()["nothing_to_write"]


def test_tampered_base_backup_is_refused(tmp_path):
    _, _, b = make_backup(tmp_path)
    store = DraftStore(tmp_path)
    d = store.create(b)
    manifest = json.loads((b / "manifest.json").read_text())
    manifest["notes"] = ["edited"]
    (b / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DraftError, match="changed since"):
        store.load(d.id)


def test_csv_round_trip_to_operations(tmp_path):
    _, _, b = make_backup(tmp_path)
    d = DraftStore(tmp_path).create(b)
    lines = d.channels_csv().splitlines()
    header, rows = lines[0], lines[1:]
    rows = [r.replace("Mica Peak", "Mica") for r in rows if not r.startswith("4,")]
    rows.append(",Simplex,146.52,,,True,low,25.0,,,0")
    ops_ = operations_from_csv("\n".join([header, *rows]), d.codeplug())
    assert [o["op"] for o in ops_] == ["update_channel", "add_channels", "delete_channels"]
    assert ops_[0] == {"op": "update_channel", "channel": 2, "changes": {"name": "Mica"}}
    assert ops_[2]["channels"] == [4]
    d.apply(ops_)
    names = [c["name"] for c in d.codeplug()["channels"]]
    assert names == ["Spokane WX", "Mica", "Canfield", "Repeater", "Simplex"]


def test_write_draft_end_to_end(tmp_path):
    pages = sample_pages()
    bench, ctx, b = make_backup(tmp_path, pages)
    d = DraftStore(tmp_path).create(b)
    d.apply([ADD, {"op": "add_zone", "name": "Hike", "members": ["Simplex"]}])
    pid = d.preview()["preview_id"]
    _, report = ops.write_draft(ctx, d, approved_preview_id=pid, really_write=True)
    assert report["passed"] and report["preview_id"] == pid
    names = [c["name"] for c in d.codeplug()["channels"]]
    from radiosmith.dm32 import codeplug

    assert [c.name for c in codeplug.decode(pages).channels] == names


def test_write_draft_tolerates_unrelated_changes_but_not_changed_intent(tmp_path):
    pages = sample_pages()
    bench, ctx, b = make_backup(tmp_path, pages)
    d = DraftStore(tmp_path).create(b)
    d.apply([ADD])
    pid = d.preview()["preview_id"]

    # The radio's settings page changes after the backup: fine.
    pages[0x0C8000] = page(0x04, b"\x01Changed")
    _, report = ops.write_draft(ctx, d, approved_preview_id=pid, really_write=False)
    assert report["steps"][-1]["step"] == "dry_run"

    # Someone renames a channel on the radio: the same draft would now be a different write... unless
    # the rename does not touch what the draft changes. Adding a channel with that name does.
    body = bytearray(pages[0x007000])
    body[0x10 : 0x10 + 16] = b"Simplex".ljust(16, b"\x00")
    pages[0x007000] = bytes(body)
    with pytest.raises(ops.WriteTestAborted, match="no longer applies"):
        ops.write_draft(ctx, d, approved_preview_id=pid, really_write=False)


def test_write_draft_refuses_stale_preview_and_other_firmware(tmp_path):
    pages = sample_pages()
    bench, ctx, b = make_backup(tmp_path, pages)
    d = DraftStore(tmp_path).create(b)
    d.apply([ADD])
    with pytest.raises(ops.WriteTestAborted, match="not this draft's current preview"):
        ops.write_draft(ctx, d, approved_preview_id="0" * 16, really_write=False)
    bench.radio_kwargs["firmware"] = b"DM32.NRF.01.050"
    with pytest.raises(ops.WriteTestAborted, match="firmware"):
        ops.write_draft(ctx, d, approved_preview_id=d.preview()["preview_id"], really_write=False)
    assert not any(f[:1] == b"W" for r in bench.radios for f in r.received)
