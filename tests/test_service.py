import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from radiosmith.server import build_mcp, create_app
from radiosmith.service import Service, ServiceError
from tests.fake_dm32 import FakeDM32, sample_pages
from tests.test_ops import PORT

PROFILES = Path(__file__).parent.parent / "profiles"


class Radio:
    def __init__(self, **kw):
        self.pages = sample_pages()
        self.kw = kw
        self.radios: list[FakeDM32] = []

    def factory(self, device, lines, log):
        self.radios.append(FakeDM32(self.pages, **self.kw))
        return self.radios[-1]

    def writes(self):
        return [f for r in self.radios for f in r.received if f[:1] == b"W"]


def make_service(tmp_path, allow_writes=False):
    radio = Radio()
    svc = Service(
        tmp_path,
        allow_writes=allow_writes,
        profiles_dir=PROFILES,
        link_factory=radio.factory,
        ports=lambda: [PORT],
        sleep=lambda _s: None,
    )
    return svc, radio


def finish(svc, job):
    out = svc.get_job(job["id"], wait_seconds=30)
    assert out["status"] == "done", out
    return out["result"]


def test_identify_remembers_port(tmp_path):
    svc, _ = make_service(tmp_path)
    r = svc.identify("dm32")
    assert r["identity"]["model"] == "DP570UV"
    assert svc.list_ports()[0]["last_seen_radio"] == "dm32"
    with pytest.raises(ServiceError, match="unknown radio"):
        svc.identify("uv5r")


def test_read_draft_preview_and_writes_disabled(tmp_path):
    svc, radio = make_service(tmp_path)
    result = finish(svc, svc.start_read("dm32"))
    assert result["backup"].startswith("backups/dm32/")
    assert svc.list_backups()[0]["backup"] == result["backup"]

    d = svc.create_draft()
    assert [c["name"] for c in d["codeplug"]["channels"]][0] == "Spokane WX"
    svc.edit_draft(d["id"], [{"op": "add_channels", "channels": [{"name": "Simplex", "rx_mhz": 146.52}]}])
    pv = svc.preview(d["id"])
    assert pv["changes"]["channels_added"][0]["name"] == "Simplex"
    with pytest.raises(ServiceError, match="writes are disabled"):
        svc.start_write(d["id"], pv["preview_id"])
    # a dry run is allowed and sends no W frame
    finish(svc, svc.start_write(d["id"], pv["preview_id"], dry_run=True))
    assert radio.writes() == []


def test_write_then_verify(tmp_path):
    svc, radio = make_service(tmp_path, allow_writes=True)
    finish(svc, svc.start_read("dm32"))
    d = svc.create_draft()
    svc.edit_draft(d["id"], [{"op": "update_zone", "zone": "Repeaters", "name": "Rptrs"}])
    with pytest.raises(ServiceError, match="preview_id does not match"):
        svc.start_write(d["id"], "deadbeefdeadbeef")
    result = finish(svc, svc.start_write(d["id"], svc.preview(d["id"])["preview_id"]))
    assert result["passed"]
    assert radio.writes()
    verified = finish(svc, svc.start_verify(d["id"]))
    assert verified["matches_draft"], verified["differences"]


def test_profiles_and_csv(tmp_path):
    svc, _ = make_service(tmp_path)
    finish(svc, svc.start_read("dm32"))
    d = svc.create_draft()
    assert "cda-listen" in [p["profile"] for p in svc.list_profiles()]
    with pytest.raises(Exception, match="already exists"):  # the sample radio already has the CDA channels
        svc.apply_profile(d["id"], "cda-listen")
    with pytest.raises(Exception, match="cannot receive AM"):
        svc.apply_profile(d["id"], "cda-airband")
    with pytest.raises(ServiceError, match="at is for the RT-950 PRO"):
        svc.apply_profile(d["id"], "cda-listen", at=100)
    with pytest.raises(ServiceError, match="no profile"):
        svc.apply_profile(d["id"], "../secrets")
    text = svc.export_csv(d["id"]).replace("Canfield", "Canfield 2")
    out = svc.import_csv(d["id"], text)
    assert out["operations_added"] == 1
    assert svc.import_csv(d["id"], svc.export_csv(d["id"]))["operations_added"] == 0


def test_one_radio_job_at_a_time(tmp_path):
    svc, _ = make_service(tmp_path)
    svc.radio_lock.acquire()
    try:
        with pytest.raises(ServiceError, match="already running"):
            svc.start_read("dm32")
    finally:
        svc.radio_lock.release()


def test_http_api_and_local_only(tmp_path):
    svc, _ = make_service(tmp_path)
    with TestClient(create_app(svc), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/status").json()["allow_writes"] is False
        job = client.post("/api/radios/dm32/read").json()
        assert client.get(f"/api/jobs/{job['id']}", params={"wait": 30}).json()["status"] == "done"
        d = client.post("/api/drafts", json={}).json()
        r = client.post(f"/api/drafts/{d['id']}/operations", json={"operations": [{"op": "delete_zone", "zone": "Nope"}]})
        assert r.status_code == 400 and "no zone named" in r.json()["error"]
        assert "Spokane WX" in client.get(f"/api/drafts/{d['id']}/channels.csv").text

        assert client.get("/api/status", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/api/status", headers={"Origin": "http://localhost:5173"}).status_code == 200


def test_mcp_endpoint_lists_tools(tmp_path):
    svc, _ = make_service(tmp_path)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    }
    with TestClient(create_app(svc), base_url="http://127.0.0.1:8765") as client:
        r = client.post("/mcp", json=init, headers=headers)
        assert r.status_code == 200, r.text
        session = r.headers.get("mcp-session-id")
        h = headers | ({"mcp-session-id": session} if session else {})
        client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h)
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert {"identify_radio", "read_radio", "edit_draft", "preview_write", "write_radio", "verify_radio"} <= names
        write = next(t for t in r.json()["result"]["tools"] if t["name"] == "write_radio")
        assert write["annotations"]["destructiveHint"] is True


@pytest.mark.anyio
async def test_mcp_tool_errors_are_readable(tmp_path):
    svc, _ = make_service(tmp_path)
    mcp = build_mcp(svc)
    with pytest.raises(Exception, match="no draft"):
        await mcp.call_tool("preview_write", {"draft_id": "nope"})
    result = await mcp.call_tool("list_ports", {})
    assert "FAKE1" in json.dumps(result, default=str)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_failed_write_job_returns_its_report(tmp_path):
    svc, radio = make_service(tmp_path, allow_writes=True)
    finish(svc, svc.start_read("dm32"))
    d = svc.create_draft()
    svc.edit_draft(d["id"], [{"op": "update_zone", "zone": 1, "name": "Z"}])
    radio.kw["firmware"] = b"DM32.NRF.01.050"
    job = svc.start_write(d["id"], svc.preview(d["id"])["preview_id"])
    out = svc.get_job(job["id"], wait_seconds=30)
    assert out["status"] == "failed" and "firmware" in out["error"] and out["draft"] == d["id"]
    assert out["result"]["passed"] is False and out["result"]["steps"][0]["step"] == "backup_before"
    assert radio.writes() == []
