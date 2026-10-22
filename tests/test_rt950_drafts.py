from pathlib import Path

import pytest

from radiosmith.dm32.edit import EditError
from radiosmith.rt950 import codeplug
from radiosmith.rt950.draftops import apply_operation, apply_operations
from radiosmith.service import Service, ServiceError
from radiosmith.transport import PortInfo
from tests.test_rt950 import FakeRT950, sample_image

PROFILES = Path(__file__).parent.parent / "profiles"
RT_PORT = PortInfo(device="FAKE4", description="USB-SERIAL CH340 (fake)", vid=0x1A86, pid=0x7523, manufacturer=None, hwid="")


def chans(image: bytes) -> list:
    return codeplug.decode_channels(image)


def test_add_update_replace_delete():
    img = sample_image()
    img, info = apply_operation(
        img, {"op": "add_channels", "channels": [{"name": "Simplex", "rx_mhz": 146.52, "tx_mhz": 146.52}]}
    )
    assert info == {"numbers": [2]}
    img, _ = apply_operation(img, {"op": "add_channels", "channels": [{"name": "Far", "rx_mhz": 162.55}], "at": 100})
    c = {ch.number: ch for ch in chans(img)}
    assert c[2].name == "Simplex" and not c[2].rx_only and c[2].scan_add and c[2].power == "low"
    assert c[100].rx_only

    img, _ = apply_operation(
        img, {"op": "update_channel", "channel": "Simplex", "changes": {"power": "high", "busy_lock": True, "tx_tone": "100.0"}}
    )
    s = {ch.number: ch for ch in chans(img)}[2]
    assert s.power == "high" and s.busy_lock and s.tx_tone == "100.0" and s.rx_mhz == 146.52

    img, _ = apply_operation(img, {"op": "replace_channel", "channel": 2, "with": {"name": "WX2", "rx_mhz": 162.55}})
    assert {ch.number: ch.name for ch in chans(img)}[2] == "WX2"

    img, info = apply_operation(img, {"op": "delete_channels", "channels": ["WX2", 100]})
    assert info == {"deleted": [2, 100]} and [ch.number for ch in chans(img)] == [1]


def test_am_receive_add_update_and_keep():
    img, _ = apply_operation(
        sample_image(), {"op": "add_channels", "channels": [{"name": "GEG Tower", "rx_mhz": 118.3, "rx_am": True}]}
    )
    c = {ch.number: ch for ch in chans(img)}[2]
    assert c.rx_am and c.rx_only and c.rx_mhz == 118.3
    img, _ = apply_operation(img, {"op": "update_channel", "channel": 2, "changes": {"name": "GEG Twr"}})
    assert {ch.number: ch for ch in chans(img)}[2].rx_am  # an update that does not name rx_am keeps it
    img, _ = apply_operation(img, {"op": "update_channel", "channel": 2, "changes": {"rx_am": False}})
    rec = img[32:64]
    assert not {ch.number: ch for ch in chans(img)}[2].rx_am and rec[15] & 0x01 == 0 and rec[15] & 0x04


def test_update_writes_only_the_named_fields():
    img = bytearray(sample_image())
    img[13] = 3  # PTT-ID both
    img[15] |= 0x02 | 0x80  # factory bit 1, FHSS learn
    img[8:10] = bytes([23, 0])  # a DCS RX tone this project cannot encode yet
    before = bytes(img)
    after, _ = apply_operation(bytes(img), {"op": "update_channel", "channel": 1, "changes": {"name": "Renamed"}})
    differ = [i for i in range(32) if before[i] != after[i]]
    assert differ and all(20 <= i < 32 for i in differ)


@pytest.mark.parametrize(
    "op, why",
    [
        ({"op": "add_channels", "channels": [{"name": "x" * 13, "rx_mhz": 146.52}]}, "up to 12"),
        ({"op": "add_channels", "channels": [{"name": "Air", "rx_mhz": 121.5, "tx_mhz": 121.5}]}, "tx 121.5 MHz is out of range"),
        ({"op": "add_channels", "channels": [{"name": "Low", "rx_mhz": 10.0}]}, "rx 10.0 MHz is out of range"),
        ({"op": "add_channels", "channels": [{"name": "D", "rx_mhz": 146.52, "rx_tone": "D023N"}]}, "CTCSS"),
        ({"op": "add_channels", "channels": [{"name": "F", "rx_mhz": 146.52, "forbid_tx": True}]}, "unknown channel fields"),
        ({"op": "add_channels", "channels": [{"name": "X", "rx_mhz": 146.52}], "at": 1}, "channel 1 is in use"),
        ({"op": "update_channel", "channel": 5, "changes": {"name": "Y"}}, "channel 5 is empty"),
        ({"op": "delete_channels", "channels": ["Nope"]}, "no channel named"),
        ({"op": "add_zone", "name": "Z", "members": [1]}, "unknown operation 'add_zone' for the RT-950 PRO"),
    ],
)
def test_refusals(op, why):
    with pytest.raises(EditError, match=why):
        apply_operation(sample_image(), op)


def test_failed_batch_changes_nothing():
    img = sample_image()
    with pytest.raises(EditError, match="operation 2"):
        apply_operations(
            img,
            [{"op": "add_channels", "channels": [{"name": "A", "rx_mhz": 146.52}]}, {"op": "delete_channels", "channels": [9]}],
        )


# -- through the service ------------------------------------------------------------------


@pytest.fixture
def rt(tmp_path):
    image = bytearray(sample_image())
    radios: list[FakeRT950] = []

    def factory(device, lines, log):
        radios.append(FakeRT950(image))
        return radios[-1]

    def make(allow_writes=False):
        return Service(
            tmp_path,
            allow_writes=allow_writes,
            profiles_dir=PROFILES,
            link_factory=factory,
            ports=lambda: [RT_PORT],
            sleep=lambda _s: None,
        )

    return make, image, radios


def finish(svc, job):
    out = svc.get_job(job["id"], wait_seconds=30)
    assert out["status"] == "done", out
    return out["result"]


def test_rt950_draft_preview_dry_run_write_verify(rt):
    make, image, radios = rt
    svc = make()
    finish(svc, svc.start_read("rt950", "FAKE4"))
    d = svc.create_draft(radio="rt950")
    assert d["radio"] == "rt950" and len(d["codeplug"]["channels"]) == 1
    svc.apply_profile(d["id"], "cda-listen")
    pv = svc.preview(d["id"])
    assert pv["radio"] == "rt950" and pv["pages"] == [
        {"address": "0000", "tag": "sector", "bytes_changed": pv["pages"][0]["bytes_changed"]}
    ]
    assert [c["name"] for c in pv["changes"]["channels_added"]] == ["Spokane WX", "Mica Peak", "Canfield", "Blacktail"]
    assert all(c["rx_only"] for c in pv["changes"]["channels_added"])

    with pytest.raises(ServiceError, match="writes are disabled"):
        svc.start_write(d["id"], pv["preview_id"], "FAKE4")
    result = finish(svc, svc.start_write(d["id"], pv["preview_id"], "FAKE4", dry_run=True))
    assert result["checks_passed"] and not any(r.writes for r in radios)

    svc = make(allow_writes=True)
    result = finish(svc, svc.start_write(d["id"], pv["preview_id"], "FAKE4"))
    assert result["passed"], result["steps"]
    assert [ch.name for ch in chans(bytes(image))] == ["", "Spokane WX", "Mica Peak", "Canfield", "Blacktail"]
    assert finish(svc, svc.start_verify(d["id"], "FAKE4"))["matches_draft"]
    assert svc.list_jobs()[0]["draft"] == d["id"]


def test_rt950_csv_round_trip(rt):
    make, _, _ = rt
    svc = make()
    finish(svc, svc.start_read("rt950", "FAKE4"))
    d = svc.create_draft(radio="rt950")
    text = svc.export_csv(d["id"])
    assert text.splitlines()[0] == "number,name,rx_mhz,tx_mhz,power,bandwidth_khz,rx_tone,tx_tone,scan_add,busy_lock,rx_am"
    text += "21,Spokane WX,162.4,,low,25.0,,,True,False,False\n,Simplex,146.52,146.52,low,25.0,,,True,False,False\n"
    text += "30,GEG Tower,118.3,,low,25.0,,,True,False,True\n"
    out = svc.import_csv(d["id"], text)
    assert out["operations_added"] == 3
    by_number = {c["number"]: c for c in svc.get_draft(d["id"])["codeplug"]["channels"]}
    assert by_number[21]["name"] == "Spokane WX" and by_number[31]["name"] == "Simplex"  # unnumbered: after the last
    assert by_number[30]["rx_am"] and not by_number[21]["rx_am"]
    assert svc.import_csv(d["id"], svc.export_csv(d["id"]))["operations_added"] == 0


def test_rt950_csv_without_rx_am_column_keeps_am(rt):
    make, _, _ = rt
    svc = make()
    finish(svc, svc.start_read("rt950", "FAKE4"))
    d = svc.create_draft(radio="rt950")
    svc.edit_draft(d["id"], [{"op": "add_channels", "channels": [{"name": "GEG Tower", "rx_mhz": 118.3, "rx_am": True}]}])
    old_csv = "".join(line.rsplit(",", 1)[0] + "\n" for line in svc.export_csv(d["id"]).splitlines())
    assert "rx_am" not in old_csv
    assert svc.import_csv(d["id"], old_csv)["operations_added"] == 0


def test_rt950_airband_profile(rt):
    make, _, _ = rt
    svc = make()
    finish(svc, svc.start_read("rt950", "FAKE4"))
    d = svc.create_draft(radio="rt950")
    svc.apply_profile(d["id"], "cda-airband", at=100)
    added = svc.preview(d["id"])["changes"]["channels_added"]
    assert added and all(c["rx_am"] and c["rx_only"] and 118.0 <= c["rx_mhz"] <= 137.0 for c in added)
    assert [c["number"] for c in added] == list(range(100, 100 + len(added)))  # zone 2 starts at slot 100
    with pytest.raises(EditError, match="channel 100 is in use"):
        svc.apply_profile(d["id"], "cda-listen", at=100)


def test_rt950_profile_refuses_tx_with_forbid(rt, tmp_path):
    make, _, _ = rt
    svc = make()
    svc.profiles_dir = tmp_path / "profiles"
    svc.profiles_dir.mkdir()
    (svc.profiles_dir / "bad.json").write_text(
        '{"name": "Bad", "zone": "Z", "channels": [{"name": "Rpt", "rx_mhz": 146.98, "tx_mhz": 146.38, "forbid_tx": true}]}'
    )
    finish(svc, svc.start_read("rt950", "FAKE4"))
    d = svc.create_draft(radio="rt950")
    with pytest.raises(ServiceError, match="no TX inhibit"):
        svc.apply_profile(d["id"], "bad")


def test_old_drafts_without_radio_load_as_dm32(tmp_path):
    from radiosmith.dm32.draft import DraftStore
    from tests.test_draft import make_backup

    _, _, b = make_backup(tmp_path)
    store = DraftStore(tmp_path)
    d = store.create(b)
    path = tmp_path / "drafts" / d.id / "draft.json"
    text = path.read_text().replace('"radio": "dm32",\n  ', "")
    assert '"radio"' not in text
    path.write_text(text)
    assert store.load(d.id).radio == "dm32"
