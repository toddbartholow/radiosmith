"""A small client for the radiosmith service's JSON API (used by the TUI)."""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8765"


class ApiError(Exception):
    """The service refused a request; the message is the service's explanation."""


class Client:
    def __init__(self, http: httpx.Client):
        self.http = http

    @classmethod
    def connect(cls, url: str = DEFAULT_URL, timeout: float = 70.0) -> Client:
        return cls(httpx.Client(base_url=url, timeout=timeout))

    def _call(self, method: str, path: str, **kw) -> Any:
        try:
            r = self.http.request(method, f"/api{path}", **kw)
        except httpx.HTTPError as e:
            raise ApiError(f"cannot reach the radiosmith service: {e}") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except ValueError:
                msg = r.text
            raise ApiError(msg)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text

    def get(self, path: str, **params) -> Any:
        return self._call("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: dict | None = None, **params) -> Any:
        return self._call("POST", path, json=body or {}, params={k: v for k, v in params.items() if v is not None})

    def delete(self, path: str) -> Any:
        return self._call("DELETE", path)

    def alive(self) -> bool:
        try:
            self.get("/status")
            return True
        except ApiError:
            return False

    # convenience wrappers
    def status(self) -> dict:
        return self.get("/status")

    def ports(self) -> list[dict]:
        return self.get("/ports")

    def identify(self, radio: str, port: str | None = None) -> dict:
        return self.post(f"/radios/{radio}/identify", port=port)

    def read(self, radio: str, port: str | None = None) -> dict:
        return self.post(f"/radios/{radio}/read", port=port)

    def job(self, job_id: str, wait: float = 0) -> dict:
        return self.get(f"/jobs/{job_id}", wait=wait)

    def jobs(self) -> list[dict]:
        return self.get("/jobs")

    def backups(self, radio: str | None = None) -> list[dict]:
        return self.get("/backups", radio=radio)

    def backup(self, ref: str) -> dict:
        return self.get(f"/backups/{ref}")

    def profiles(self) -> list[dict]:
        return self.get("/profiles")

    def drafts(self) -> list[dict]:
        return self.get("/drafts")

    def draft(self, draft_id: str) -> dict:
        return self.get(f"/drafts/{draft_id}")

    def create_draft(self, backup: str | None = None, name: str = "") -> dict:
        return self.post("/drafts", {"backup": backup, "name": name})

    def delete_draft(self, draft_id: str) -> dict:
        return self.delete(f"/drafts/{draft_id}")

    def edit(self, draft_id: str, operations: list[dict]) -> dict:
        return self.post(f"/drafts/{draft_id}/operations", {"operations": operations})

    def undo(self, draft_id: str, count: int = 1) -> dict:
        return self.post(f"/drafts/{draft_id}/undo", {"count": count})

    def apply_profile(self, draft_id: str, profile: str) -> dict:
        return self.post(f"/drafts/{draft_id}/profile", {"profile": profile})

    def preview(self, draft_id: str) -> dict:
        return self.get(f"/drafts/{draft_id}/preview")

    def write(self, draft_id: str, preview_id: str, *, dry_run: bool, port: str | None = None) -> dict:
        return self.post(f"/drafts/{draft_id}/write", {"preview_id": preview_id, "dry_run": dry_run, "port": port})

    def verify(self, draft_id: str, port: str | None = None) -> dict:
        return self.post(f"/drafts/{draft_id}/verify", {"port": port})
