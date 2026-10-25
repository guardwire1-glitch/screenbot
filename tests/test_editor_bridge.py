"""Contract tests for the localhost editor bridge.

The bridge is the only component that may touch a live editor buffer, so these
tests lock down the properties the safety story depends on:
  - token auth on every data endpoint,
  - job routing to the requested IDE (never to a different editor),
  - a timed-out job is removed, not executed later by a late-joining host,
  - the claim response carries an expiry the extension can check,
  - pending review changes resolve through the registered resolver.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

from editor_bridge import BridgeServer, NoEditorHost, PendingChange, discovery_path


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    srv = BridgeServer(
        pending_path=tmp_path / "pending.json",
        token="test-token",
        port=0,
    )
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _request(bridge, method, route, body=None, token="test-token"):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-ScreenBot-Token"] = token
    req = urllib.request.Request(
        f"http://127.0.0.1:{bridge.port}{route}",
        data=payload,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def test_discovery_file_matches_the_bound_port(bridge):
    data = json.loads(discovery_path().read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["port"] == bridge.port
    assert data["token"] == "test-token"


def test_data_endpoints_require_the_token(bridge):
    status, _ = _request(bridge, "GET", "/v1/pending", token=None)
    assert status == 401

    status, data = _request(bridge, "GET", "/v1/pending", token="test-token")
    assert status == 200
    assert data == {"items": []}

    status, data = _request(bridge, "GET", "/v1/health", token=None)
    assert status == 200
    assert data["ok"] is True


def test_token_in_query_string_is_never_accepted(bridge):
    """Only the header is trusted; URLs leak into logs and browser history."""
    status, _ = _request(
        bridge, "GET", "/v1/pending?token=test-token", token=None
    )
    assert status == 401

    status, _ = _request(
        bridge, "GET", "/v1/pending?token=test-token", token="wrong-token"
    )
    assert status == 401

    status, _ = _request(
        bridge, "GET", "/v1/pending?token=wrong-token", token="test-token"
    )
    assert status == 200


def test_jobs_are_routed_only_to_the_requested_ide(bridge):
    outcome = {}

    def caller():
        outcome["result"] = bridge.request_job(
            "context", {"file_path": "C:/work/Demo.cs"}, timeout=5, ide="vscode"
        )

    worker = threading.Thread(target=caller)
    worker.start()
    time.sleep(0.1)

    # A different editor must not steal a job addressed to VS Code.
    status, _ = _request(
        bridge, "POST", "/v1/jobs/claim", {"ide": "antigravity", "wait": 0.4}
    )
    assert status == 204

    status, claim = _request(
        bridge, "POST", "/v1/jobs/claim", {"ide": "vscode", "wait": 2}
    )
    assert status == 200
    assert claim["type"] == "context"
    assert claim["payload"]["file_path"] == "C:/work/Demo.cs"
    assert claim["expires_at"] > time.time()

    status, _ = _request(
        bridge,
        "POST",
        "/v1/jobs/result",
        {"id": claim["id"], "result": {"ok": True, "text": "buffer"}, "error": ""},
    )
    assert status == 200

    worker.join(6)
    assert not worker.is_alive()
    assert outcome["result"]["text"] == "buffer"


def test_timed_out_job_is_never_left_in_the_queue(bridge):
    with pytest.raises(NoEditorHost):
        bridge.request_job("apply", {"file_path": "C:/work/Demo.cs"}, timeout=0.3, ide="vscode")

    status, _ = _request(
        bridge, "POST", "/v1/jobs/claim", {"ide": "vscode", "wait": 0.3}
    )
    assert status == 204
    assert bridge._jobs == []


def test_pending_detail_returns_the_full_proposal(bridge):
    """The review diff needs the proposed text, which the list omits."""
    item = bridge.pending.add(
        PendingChange(
            id="pc_detail",
            created_at=time.time(),
            file_path="C:/work/Demo.cs",
            base_hash="abc123",
            proposed_text="class Demo\n{\n    int x = 2;\n}\n",
            reason="lecturer 1 edited block(s)",
            source="disk",
        )
    )

    status, listing = _request(bridge, "GET", "/v1/pending")
    assert status == 200
    assert listing["items"] == [item.summary()]
    assert "proposed_text" not in listing["items"][0]

    status, data = _request(bridge, "GET", "/v1/pending/pc_detail")
    assert status == 200
    assert data["item"]["proposed_text"] == item.proposed_text
    assert data["item"]["base_hash"] == "abc123"

    status, _ = _request(bridge, "GET", "/v1/pending/pc_missing")
    assert status == 404

    status, _ = _request(bridge, "GET", "/v1/pending/pc_detail", token=None)
    assert status == 401


def test_pending_changes_resolve_through_the_registered_resolver(bridge):
    seen = []
    bridge.pending_resolver = lambda item_id, accept: (
        seen.append((item_id, accept)) or {"ok": True, "accepted": accept}
    )

    status, data = _request(
        bridge, "POST", "/v1/pending/resolve", {"id": "pc_1", "accept": True}
    )

    assert status == 200
    assert data["ok"] is True
    assert seen == [("pc_1", True)]


def test_stop_releases_waiting_jobs_and_removes_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    srv = BridgeServer(pending_path=tmp_path / "pending.json", token="t", port=0)
    srv.start()

    outcome = {}

    def caller():
        try:
            srv.request_job("context", {}, timeout=5, ide="vscode")
        except Exception as exc:  # noqa: BLE001 - the test asserts the type
            outcome["error"] = exc

    worker = threading.Thread(target=caller)
    worker.start()
    time.sleep(0.15)

    srv.stop()
    worker.join(3)

    assert isinstance(outcome.get("error"), Exception)
    assert not discovery_path().exists()
