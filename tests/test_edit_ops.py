import pytest

from radiosmith.dm32 import codeplug
from radiosmith.dm32 import protocol as p
from radiosmith.dm32.edit import EditError, apply_operation, apply_operations, changed_pages
from tests.fake_dm32 import page, sample_pages

CH_PAGE, ZONE_PAGE, SCAN_PAGE = 0x007000, 0x013000, 0x02A000


def decoded(pages):
    cp = codeplug.decode(pages)
    assert not cp.problems
    return cp


def changed_offsets(before: bytes, after: bytes) -> list[int]:
    return [i for i in range(p.PAGE) if before[i] != after[i]]


def test_add_channels_returns_numbers_and_uses_scan_list():
    pages = sample_pages()
    after, info = apply_operation(
        pages, {"op": "add_channels", "channels": [{"name": "Simplex", "rx_mhz": 146.52, "scan_list": 1}]}
    )
    assert info == {"numbers": [6]}
    ch = decoded(after).channels[-1]
    assert ch.name == "Simplex" and ch.scan_list == 1 and ch.forbid_tx and ch.tx_mhz is None
    assert set(changed_pages(pages, after)) == {CH_PAGE}


def test_add_channels_rejects_unknown_scan_list_and_fields():
    with pytest.raises(EditError, match="scan list 2 does not exist"):
        apply_operation(sample_pages(), {"op": "add_channels", "channels": [{"name": "X", "rx_mhz": 146.52, "scan_list": 2}]})
    with pytest.raises(EditError, match="unknown channel fields"):
        apply_operation(sample_pages(), {"op": "add_channels", "channels": [{"name": "X", "rx_mhz": 146.52, "colour": 1}]})


def test_add_channels_spills_onto_the_next_channel_page():
    pages = sample_pages()
    pages[0x015000] = page(0x13)
    new = [{"name": f"N{i}", "rx_mhz": 146.52} for i in range(80)]
    after, info = apply_operation(pages, {"op": "add_channels", "channels": new})
    assert info["numbers"][-1] == 85
    cp = decoded(after)
    assert cp.channel_count == 85 and cp.channels[-1].name == "N79"
    assert set(changed_pages(pages, after)) == {CH_PAGE, 0x015000}


def test_add_channels_refuses_a_missing_page():
    new = [{"name": f"N{i}", "rx_mhz": 146.52} for i in range(80)]
    with pytest.raises(EditError, match="tag 0x13 does not exist"):
        apply_operation(sample_pages(), {"op": "add_channels", "channels": new})


def test_update_channel_changes_only_named_fields():
    pages = sample_pages()
    after, _ = apply_operation(
        pages, {"op": "update_channel", "channel": "Mica Peak", "changes": {"power": "high", "rx_tone": "127.3"}}
    )
    before_ch, after_ch = decoded(pages).channels[1], decoded(after).channels[1]
    assert after_ch.power == "high" and after_ch.rx_tone == "127.3"
    assert after_ch.forbid_tx and after_ch.name == before_ch.name and after_ch.rx_mhz == before_ch.rx_mhz
    off = 0x10 + 0x30
    assert all(off + 0x18 <= i < off + 0x23 for i in changed_offsets(pages[CH_PAGE], after[CH_PAGE]))


def test_update_channel_refuses_tx_enable_without_frequency_and_duplicate_names():
    with pytest.raises(EditError, match="must forbid TX"):
        apply_operation(sample_pages(), {"op": "update_channel", "channel": 1, "changes": {"tx_mhz": None, "forbid_tx": False}})
    with pytest.raises(EditError, match="already exists"):
        apply_operation(sample_pages(), {"op": "update_channel", "channel": 1, "changes": {"name": "Canfield"}})
    with pytest.raises(EditError, match="cannot change"):
        apply_operation(sample_pages(), {"op": "update_channel", "channel": 1, "changes": {"mode": "digital"}})


def test_replace_channel_keeps_its_number():
    pages = sample_pages()
    after, info = apply_operation(pages, {"op": "replace_channel", "channel": 5, "with": {"name": "Weather 2", "rx_mhz": 162.55}})
    assert info == {"number": 5}
    ch = decoded(after).channels[4]
    assert ch.name == "Weather 2" and ch.tx_mhz is None and ch.rx_tone is None and ch.forbid_tx


def test_delete_channels_renumbers_zones_and_scan_lists():
    pages = sample_pages()
    after, info = apply_operation(pages, {"op": "delete_channels", "channels": [2, "Canfield"]})
    assert info["deleted"] == [2, 3]
    cp = decoded(after)
    assert [c.name for c in cp.channels] == ["Spokane WX", "Blacktail", "Repeater"]
    assert [c.number for c in cp.channels] == [1, 2, 3]
    assert cp.zones[0].members == [1, 2] and cp.zones[1].members == [3]
    assert cp.scan_lists[0].members == [1, 2]
    # zone names and other zone bytes untouched
    assert [z.name for z in cp.zones] == ["CDA Listen", "Repeaters"]
    assert after[ZONE_PAGE][:0x10] == pages[ZONE_PAGE][:0x10]


def test_delete_channels_refuses_to_empty_a_zone():
    with pytest.raises(EditError, match="Repeaters' would be left empty"):
        apply_operation(sample_pages(), {"op": "delete_channels", "channels": [5]})
    after, _ = apply_operations(
        sample_pages(), [{"op": "delete_zone", "zone": "Repeaters"}, {"op": "delete_channels", "channels": [5]}]
    )
    assert [c.name for c in decoded(after).channels][-1] == "Blacktail"


def test_zone_operations():
    pages = sample_pages()
    after, infos = apply_operations(
        pages,
        [
            {"op": "add_zone", "name": "Mixed", "members": ["Repeater", 1]},
            {"op": "update_zone", "zone": "CDA Listen", "members": [4, 3]},
            {"op": "update_zone", "zone": 2, "name": "Rptrs"},
        ],
    )
    assert infos[0] == {"number": 3, "members": [5, 1]}
    zones = decoded(after).zones
    assert [(z.name, z.members) for z in zones] == [("CDA Listen", [4, 3]), ("Rptrs", [5]), ("Mixed", [5, 1])]
    after2, _ = apply_operation(after, {"op": "delete_zone", "zone": 1})
    assert [z.name for z in decoded(after2).zones] == ["Rptrs", "Mixed"]


def test_zone_validation():
    for op, why in [
        ({"op": "add_zone", "name": "CDA Listen", "members": [1]}, "already exists"),
        ({"op": "add_zone", "name": "Z", "members": []}, "non-empty"),
        ({"op": "add_zone", "name": "Z", "members": [1, 1]}, "twice"),
        ({"op": "add_zone", "name": "Z", "members": [9]}, "channel 9 does not exist"),
        ({"op": "add_zone", "name": "Z", "members": ["Nope"]}, "no channel named"),
        ({"op": "update_zone", "zone": 7}, "zone 7 does not exist"),
        ({"op": "update_zone", "zone": 1}, "give a new name"),
        ({"op": "delete_zone", "zone": "Nope"}, "no zone named"),
    ]:
        with pytest.raises(EditError, match=why):
            apply_operation(sample_pages(), op)


def test_update_scan_list_members_only_touches_member_bytes():
    pages = sample_pages()
    after, _ = apply_operation(pages, {"op": "update_scan_list", "scan_list": "Listen", "members": [5, 1]})
    assert decoded(after).scan_lists[0].members == [5, 1]
    off = 0x39 * 1 - 0x38
    assert all(i == off + 0x0B or off + 0x18 <= i < off + 0x38 for i in changed_offsets(pages[SCAN_PAGE], after[SCAN_PAGE]))


def test_unknown_operation_and_arguments():
    with pytest.raises(EditError, match="unknown operation"):
        apply_operation(sample_pages(), {"op": "format_radio"})
    with pytest.raises(EditError, match="unknown arguments"):
        apply_operation(sample_pages(), {"op": "delete_zone", "zone": 1, "force": True})


def test_failed_operation_leaves_pages_untouched():
    pages = sample_pages()
    snapshot = dict(pages)
    with pytest.raises(EditError, match="operation 2"):
        apply_operations(pages, [{"op": "update_zone", "zone": 1, "name": "A"}, {"op": "delete_zone", "zone": 9}])
    assert pages == snapshot
