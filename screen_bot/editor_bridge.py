"""
Local editor bridge for ScreenBot.

The bridge is the only component allowed to modify an editor buffer. It is
intentionally small and boring:

- HTTP on 127.0.0.1 only, with a random bearer token written to
  %APPDATA%/ScreenBot/bridge.json.
- A job queue. Editor integrations (VS Code/Antigravity extension today, other
  IDEs via the same protocol later) long-poll for jobs, execute them with the
  editor's own API, and return context/diagnostics.
- Pending changes. Anything that fails a gate, conflicts with the current
  buffer, or belongs to a dirty unsaved tab is stored here for review instead
  of being forced onto the user.

No editor code is imported here, so the protocol is language/IDE neutral.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

BRIDGE_VERSION = 1
DEFAULT_PORT = 47653
PORT_ATTEMPTS = 12


class BridgeError(RuntimeError):
    pass


class NoEditorHost(BridgeError):
    pass


def appdata_dir() -> Path:
    base = os.getenv("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / "ScreenBot"


def discovery_path() -> Path:
    return appdata_dir() / "bridge.json"


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp_{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class PendingChange:
    id: str
    created_at: float
    file_path: str
    base_hash: str
    proposed_text: str
    reason: str
    source: str = "screenbot"
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "file_path": self.file_path,
            "reason": self.reason,
            "source": self.source,
            "added_lines": max(0, len(self.proposed_text.splitlines())),
            "diagnostics": self.diagnostics[:20],
        }


class PendingStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._items: List[PendingChange] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for item in data.get("items", []):
                self._items.append(PendingChange(**item))
        except Exception:
            self._items = []

    def _save(self) -> None:
        _atomic_json(self.path, {"items": [asdict(item) for item in self._items]})

    def add(self, change: PendingChange) -> PendingChange:
        with self._lock:
            self._items.append(change)
            self._save()
        return change

    def list(self) -> List[PendingChange]:
        with self._lock:
            return list(self._items)

    def summaries(self) -> List[Dict[str, Any]]:
        return [item.summary() for item in self.list()]

    def get(self, item_id: str) -> Optional[PendingChange]:
        with self._lock:
            for item in self._items:
                if item.id == item_id:
                    return item
        return None

    def remove(self, item_id: str) -> Optional[PendingChange]:
        with self._lock:
            for idx, item in enumerate(self._items):
                if item.id == item_id:
                    del self._items[idx]
                    self._save()
                    return item
        return None

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._save()


@dataclass
class _Job:
    id: str
    type: str
    payload: Dict[str, Any]
    created_at: float
    ide: str = ""
    expires_at: float = 0.0
    event: threading.Event = field(default_factory=threading.Event)
    result: Optional[Dict[str, Any]] = None
    error: str = ""


class BridgeServer:
    """Threaded localhost job server. Safe to start/stop from the tray thread."""

    def __init__(
        self,
        pending_path: Path,
        token: Optional[str] = None,
        port: int = DEFAULT_PORT,
        logger=None,
    ):
        self.logger = logger
        self.token = token or secrets.token_urlsafe(32)
        self.pending = PendingStore(pending_path)
        self.pending_resolver: Optional[Callable[[str, bool], Dict[str, Any]]] = None
        self._lock = threading.Condition()
        self._jobs: List[_Job] = []
        self._waiting: Dict[str, _Job] = {}
        self._hosts: Dict[str, float] = {}
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = port
        self._stopped = False

    # -- lifecycle -----------------------------------------------------
    def start(self) -> int:
        last_err = None
        for offset in range(PORT_ATTEMPTS):
            candidate = DEFAULT_PORT + offset if self.port == DEFAULT_PORT else self.port + offset
            try:
                self._httpd = ThreadingHTTPServer(("127.0.0.1", candidate), self._handler_factory())
                self.port = int(self._httpd.server_address[1])
                break
            except OSError as exc:
                last_err = exc
                continue
        if self._httpd is None:
            raise BridgeError(f"could not bind bridge port: {last_err}")
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ScreenBotBridge", daemon=True)
        self._thread.start()
        self._write_discovery()
        if self.logger:
            self.logger.info(f"Editor bridge listening on 127.0.0.1:{self.port}")
        return self.port

    def stop(self) -> None:
        self._stopped = True
        with self._lock:
            self._jobs.clear()
            self._lock.notify_all()
        for job in list(self._waiting.values()):
            job.error = "bridge stopped"
            job.event.set()
        self._waiting.clear()
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
        try:
            discovery_path().unlink(missing_ok=True)
        except Exception:
            pass

    def _write_discovery(self) -> None:
        _atomic_json(discovery_path(), {
            "version": BRIDGE_VERSION,
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
        })

    # -- host/job plumbing --------------------------------------------
    def note_host(self, ide: str) -> None:
        ide = (ide or "unknown").lower()
        with self._lock:
            self._hosts[ide] = time.time()

    def host_alive(self, ide: str, max_age: float = 4.0) -> bool:
        ide = (ide or "").lower()
        with self._lock:
            seen = self._hosts.get(ide) or max(
                (v for k, v in self._hosts.items() if k.startswith(ide)), default=0.0
            )
        return (time.time() - seen) <= max_age

    def live_hosts(self) -> List[str]:
        now = time.time()
        with self._lock:
            return [k for k, v in self._hosts.items() if now - v <= 4.0]

    def request_job(
        self, job_type: str, payload: Dict[str, Any], timeout: float = 8.0, ide: str = ""
    ) -> Dict[str, Any]:
        timeout = max(0.2, float(timeout))
        if self._stopped:
            raise BridgeError("bridge is stopped")
        job = _Job(
            id=uuid.uuid4().hex,
            type=job_type,
            payload=payload,
            created_at=time.time(),
            ide=(ide or "").lower(),
            expires_at=time.time() + timeout,
        )
        with self._lock:
            self._waiting[job.id] = job
            self._jobs.append(job)
            self._lock.notify_all()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if job.event.wait(min(0.15, max(0.01, deadline - time.time()))):
                break
        if not job.event.is_set():
            # A timed-out job must never sit in the queue: an editor that
            # connects later would execute a stale edit after ScreenBot has
            # already decided to hold it.
            with self._lock:
                self._waiting.pop(job.id, None)
                try:
                    self._jobs.remove(job)
                except ValueError:
                    pass
            raise NoEditorHost(
                f"editor host did not answer '{job_type}' within {timeout:.1f}s"
            )
        with self._lock:
            self._waiting.pop(job.id, None)
        if job.error:
            raise BridgeError(job.error)
        return job.result or {}

    def _claim_job(self, ide: str, wait_seconds: float = 4.0) -> Optional[_Job]:
        ide = (ide or "unknown").lower()
        self.note_host(ide)
        deadline = time.time() + max(0.1, wait_seconds)
        with self._lock:
            while not self._stopped:
                for idx, job in enumerate(self._jobs):
                    # A job addressed to one IDE is never handed to another.
                    # Broadcast jobs (ide="") may be claimed by any host.
                    if not job.ide or job.ide == ide:
                        del self._jobs[idx]
                        return job
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._lock.wait(remaining)
        return None

    def _finish_job(self, job_id: str, result: Optional[dict], error: str) -> None:
        job = self._waiting.get(job_id)
        if job is None:
            return
        job.result = result
        job.error = error
        job.event.set()

    # -- HTTP ----------------------------------------------------------
    def _handler_factory(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
                return

            def _auth(self) -> bool:
                supplied = self.headers.get("X-ScreenBot-Token", "")
                if not supplied:
                    query = parse_qs(urlparse(self.path).query)
                    supplied = (query.get("token") or [""])[0]
                return secrets.compare_digest(supplied, server.token)

            def _send(self, status: int, data: Optional[dict] = None) -> None:
                body = b"" if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                if data is not None:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    length = 0
                if length <= 0:
                    return {}
                try:
                    parsed = json.loads(self.rfile.read(min(length, 4 * 1024 * 1024)).decode("utf-8"))
                    return parsed if isinstance(parsed, dict) else {}
                except Exception:
                    return {}

            def do_GET(self):  # noqa: N802 - stdlib signature
                path = urlparse(self.path).path
                if path == "/v1/health":
                    self._send(200, {
                        "ok": True,
                        "version": BRIDGE_VERSION,
                        "hosts": server.live_hosts(),
                    })
                    return
                if not self._auth():
                    self._send(401, {"error": "unauthorized"})
                    return
                if path == "/v1/pending":
                    self._send(200, {"items": server.pending.summaries()})
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802 - stdlib signature
                path = urlparse(self.path).path
                if not self._auth():
                    self._send(401, {"error": "unauthorized"})
                    return
                body = self._body()

                if path == "/v1/jobs/claim":
                    ide = str(body.get("ide") or "unknown").lower()
                    wait = min(6.0, max(0.2, float(body.get("wait") or 4.0)))
                    job = server._claim_job(ide, wait)
                    if job is None:
                        self._send(204)
                        return
                    self._send(200, {
                        "id": job.id,
                        "type": job.type,
                        "payload": job.payload,
                        "expires_at": job.expires_at,
                    })
                    return

                if path == "/v1/jobs/result":
                    job_id = str(body.get("id") or "")
                    result = body.get("result")
                    server._finish_job(
                        job_id,
                        result if isinstance(result, dict) else None,
                        str(body.get("error") or ""),
                    )
                    self._send(200, {"ok": True})
                    return

                if path == "/v1/pending/resolve":
                    item_id = str(body.get("id") or "")
                    accept = bool(body.get("accept", False))
                    if server.pending_resolver is None:
                        self._send(503, {"ok": False, "error": "review not available"})
                        return
                    try:
                        outcome = server.pending_resolver(item_id, accept)
                    except Exception as exc:
                        self._send(200, {"ok": False, "error": str(exc)})
                        return
                    self._send(200, {"ok": bool(outcome.get("ok")), **outcome})
                    return

                self._send(404, {"error": "not found"})

        return Handler
