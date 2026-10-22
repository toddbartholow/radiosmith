import pytest
from fastapi.testclient import TestClient
from textual.widgets import Button, DataTable, Input, RichLog, Static

from radiosmith.client import Client
from radiosmith.server import create_app
from radiosmith.tui import ChannelForm, RadiosmithApp, ReviewScreen, format_preview, identity_label
from tests.test_service import finish, make_service


@pytest.fixture
def setup(tmp_path):
    svc, radio = make_service(tmp_path)
    finish(svc, svc.start_read("dm32"))
    http = TestClient(create_app(svc), base_url="http://127.0.0.1:8765")
    with http:
        yield svc, radio, Client(http)


async def settle(pilot, app, n=6):
    for _ in range(n):
        await pilot.pause(0.05)
        await app.workers.wait_for_complete()


@pytest.mark.anyio
async def test_tui_draft_edit_and_review(setup):
    svc, radio, client = setup
    d = svc.create_draft(name="tui test")
    app = RadiosmithApp(client)
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot, app)
        assert app.query_one("#ports", DataTable).row_count == 1
        assert app.query_one("#backup-list", DataTable).row_count == 1
        assert app.query_one("#draft-list", DataTable).row_count == 1

        app.query_one("TabbedContent").active = "drafts"
        app.open_draft(d["id"])
        await settle(pilot, app)
        assert app.query_one("#draft-channels", DataTable).row_count == 5

        # add a channel through the form
        await pilot.press("a")
        await settle(pilot, app)
        form = app.screen
        assert isinstance(form, ChannelForm)
        form.query_one("#name", Input).value = "Simplex"
        form.query_one("#rx_mhz", Input).value = "146.52"
        form.query_one("#save", Button).press()
        await settle(pilot, app)
        assert app.query_one("#draft-channels", DataTable).row_count == 6
        assert svc.get_draft(d["id"])["operations"][-1]["op"] == "add_channels"

        # a refused edit shows the service's message and changes nothing
        app.apply([{"op": "delete_zone", "zone": "Nope"}])
        await settle(pilot, app)
        assert len(svc.get_draft(d["id"])["operations"]) == 1

        # review: writes disabled, so only the dry run is offered
        await pilot.press("w")
        await settle(pilot, app)
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert "Simplex" in str(review.query_one(".preview Static", Static).render())
        assert review.query_one("#write", Button).disabled
        review.query_one("#dry", Button).press()
        await settle(pilot, app, 20)
        for _ in range(100):
            if svc.list_jobs()[0]["status"] != "running":
                break
            await pilot.pause(0.05)
        await settle(pilot, app)
        assert svc.list_jobs()[0]["kind"] == "dry-run" and svc.list_jobs()[0]["status"] == "done"
        assert radio.writes() == []
        log = app.query_one("#job-log", RichLog)
        assert any("Dry run passed" in str(line.text) for line in log.lines)


def test_format_preview_shows_field_changes():
    pv = {
        "draft": "d",
        "identity": {"model": "DP570UV", "firmware": "x"},
        "preview_id": "abc",
        "nothing_to_write": False,
        "pages": [{"address": "007000", "tag": "12", "bytes_changed": 2}],
        "changes": {
            **{f"{k}_{a}": [] for k in ("channels", "zones", "scan_lists") for a in ("added", "removed", "changed")},
            "channels_changed": [{"number": 2, "before": {"name": "A", "power": "low"}, "after": {"name": "A", "power": "high"}}],
        },
    }
    text = format_preview(pv)
    assert "~ Channel 2 'A': power 'low' -> 'high'" in text and "007000" in text


def test_identity_label_leaves_out_missing_firmware():
    assert identity_label({"model": "DP570UV", "firmware": "DM32.NRF.01.049"}) == "DP570UV DM32.NRF.01.049"
    assert identity_label({"model": "RT-950", "firmware": None}) == "RT-950"


@pytest.fixture
def anyio_backend():
    return "asyncio"
