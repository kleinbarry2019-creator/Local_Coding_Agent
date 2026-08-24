"""Loopback-only browser interface for the shared ACB autonomy runtime."""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol, cast
from urllib.parse import urlsplit

from autonomous_agent.core.autonomy import AutonomyRuntime, RuntimeResult
from autonomous_agent.core.config import AgentConfig
from autonomous_agent.core.goals import GoalError
from autonomous_agent.core.task_state import TaskRecord

UI_NAME = "ACB – Autonome Computing Butler"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765
MAX_REQUEST_BYTES = 65_536
MAX_TASK_RECORDS = 100
_REQUEST_ID_PATTERN = re.compile(r"^request-[0-9a-f]{32}$")
_SESSION_ID_PATTERN = re.compile(r"^session-[0-9a-f]{32}$")


class _TaskStore(Protocol):
    def load_task(self, session_id: str) -> TaskRecord | None: ...


class _Runtime(Protocol):
    tasks: _TaskStore

    def run(self, raw_goal: str) -> RuntimeResult: ...


@dataclass
class UiTask:
    request_id: str
    goal: str
    status: str
    created_at: str
    session_id: str | None = None
    result: Mapping[str, object] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "request_id": self.request_id,
            "goal": self.goal,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.session_id is not None:
            document["session_id"] = self.session_id
        if self.result is not None:
            document["result"] = dict(self.result)
        if self.error is not None:
            document["error"] = self.error
        return document


class AcbUiServer:
    """Serve a small browser UI while sharing one persistent runtime instance."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        host: str = DEFAULT_UI_HOST,
        port: int = DEFAULT_UI_PORT,
        runtime: _Runtime | None = None,
    ) -> None:
        self.host = validate_ui_host(host)
        self.port = _validate_port(port)
        self.token = secrets.token_urlsafe(24)
        self.runtime: _Runtime = (
            cast(_Runtime, AutonomyRuntime(config)) if runtime is None else runtime
        )
        self._tasks: dict[str, UiTask] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="acb-ui-runtime",
        )
        self._serving = threading.Event()
        self._httpd = _AcbHttpServer((self.host, self.port), _AcbRequestHandler, self)

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self._httpd.server_port}/"

    def serve_forever(self) -> None:
        self._serving.set()
        try:
            self._httpd.serve_forever()
        finally:
            self._serving.clear()
            self._executor.shutdown(wait=True, cancel_futures=True)

    def shutdown(self) -> None:
        if self._serving.is_set():
            self._httpd.shutdown()
        self._httpd.server_close()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def submit(self, goal: str) -> UiTask:
        request_id = f"request-{uuid.uuid4().hex}"
        task = UiTask(
            request_id=request_id,
            goal=goal,
            status="queued",
            created_at=_timestamp(),
        )
        with self._lock:
            self._tasks[request_id] = task
            self._trim_tasks()
        self._executor.submit(self._run_task, request_id, goal)
        return task

    def task(self, request_id: str) -> UiTask | None:
        with self._lock:
            task = self._tasks.get(request_id)
            return None if task is None else _copy_task(task)

    def tasks(self) -> list[UiTask]:
        with self._lock:
            return [_copy_task(item) for item in self._tasks.values()]

    def persisted_session(self, session_id: str) -> dict[str, object] | None:
        record = self.runtime.tasks.load_task(session_id)
        if record is None:
            return None
        document: dict[str, object] = {
            "session_id": record.session_id,
            "original_goal": record.original_goal,
            "status": record.status,
            "current_step": record.current_step,
            "attempts": record.attempts,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        if record.failure_fingerprint is not None:
            document["failure_fingerprint"] = record.failure_fingerprint
        if record.completion is not None:
            document["completion"] = dict(record.completion)
        return document

    def _run_task(self, request_id: str, goal: str) -> None:
        self._update_task(request_id, status="running")
        try:
            result = self.runtime.run(goal)
            document = result.to_dict()
            session_id = result.session_id
            self._update_task(
                request_id,
                status=result.status,
                session_id=session_id,
                result=document,
            )
        except GoalError:
            self._update_task(
                request_id,
                status="rejected",
                error="goal could not be understood or was not supported",
            )
        except Exception:  # noqa: BLE001 - browser boundary must stay redacted
            self._update_task(
                request_id,
                status="failed",
                error="task execution failed before verification",
            )

    def _update_task(
        self,
        request_id: str,
        *,
        status: str,
        session_id: str | None = None,
        result: Mapping[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            task = self._tasks.get(request_id)
            if task is None:
                return
            task.status = status
            task.session_id = session_id
            task.result = result
            task.error = error

    def _trim_tasks(self) -> None:
        while len(self._tasks) > MAX_TASK_RECORDS:
            oldest = next(iter(self._tasks))
            del self._tasks[oldest]


class _AcbHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        app: AcbUiServer,
    ) -> None:
        self.app = app
        super().__init__(server_address, handler_class)


class _AcbRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        app = self._app()
        if path == "/":
            self._send_html(_render_html(app.token))
            return
        if path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {"name": UI_NAME, "status": "ok", "runtime": "ready"},
            )
            return
        if path == "/api/tasks":
            self._send_json(
                HTTPStatus.OK,
                {"tasks": [task.to_dict() for task in reversed(app.tasks())]},
            )
            return
        if path.startswith("/api/tasks/"):
            request_id = path.removeprefix("/api/tasks/")
            if not _REQUEST_ID_PATTERN.fullmatch(request_id):
                self._send_error_json(HTTPStatus.NOT_FOUND, "task not found")
                return
            task = app.task(request_id)
            if task is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "task not found")
                return
            self._send_json(HTTPStatus.OK, task.to_dict())
            return
        if path.startswith("/api/sessions/"):
            session_id = path.removeprefix("/api/sessions/")
            if not _SESSION_ID_PATTERN.fullmatch(session_id):
                self._send_error_json(HTTPStatus.NOT_FOUND, "session not found")
                return
            session = app.persisted_session(session_id)
            if session is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "session not found")
                return
            self._send_json(HTTPStatus.OK, session)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not self._authorized():
            self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
            return
        if path != "/api/tasks":
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        goal = payload.get("goal")
        if type(goal) is not str or not goal.strip():
            self._send_error_json(HTTPStatus.BAD_REQUEST, "goal is required")
            return
        try:
            task = self._app().submit(goal)
        except Exception:  # noqa: BLE001 - do not disclose local paths
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "task rejected")
            return
        self._send_json(HTTPStatus.ACCEPTED, task.to_dict())

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _app(self) -> AcbUiServer:
        server = cast(_AcbHttpServer, self.server)
        return server.app

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-ACB-Token", "")
        return secrets.compare_digest(supplied, self._app().token)

    def _read_json(self) -> dict[str, object] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            self._send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON required")
            return None
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._send_error_json(HTTPStatus.LENGTH_REQUIRED, "request length required")
            return None
        if length > MAX_REQUEST_BYTES:
            self._send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid request")
            return None
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return None
        if type(value) is not dict:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "JSON object required")
            return None
        return cast(dict[str, object], value)

    def _send_json(self, status: HTTPStatus, value: Mapping[str, object]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._headers("text/html; charset=utf-8", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )


def validate_ui_host(host: str) -> str:
    """Allow only loopback listeners; never expose the task executor remotely."""
    if type(host) is not str or not host or host == "localhost":
        return DEFAULT_UI_HOST if host == "localhost" else _invalid_host()
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("UI host must be loopback") from error
    if not address.is_loopback:
        raise ValueError("UI host must be loopback")
    return host


def _invalid_host() -> str:
    raise ValueError("UI host must be loopback")


def _validate_port(port: int) -> int:
    if type(port) is not int or not 0 <= port <= 65_535:
        raise ValueError("UI port is invalid")
    return port


def _copy_task(task: UiTask) -> UiTask:
    return UiTask(
        request_id=task.request_id,
        goal=task.goal,
        status=task.status,
        created_at=task.created_at,
        session_id=task.session_id,
        result=task.result,
        error=task.error,
    )


def _timestamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="microseconds")


def _render_html(token: str) -> str:
    safe_token = json.dumps(token, ensure_ascii=True)
    return _HTML.replace("__ACB_TOKEN__", safe_token)


_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ACB – Autonome Computing Butler</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1220; --panel:#121c2e; --line:#263653; --ink:#e7edf8; --muted:#9fb0ca; --accent:#5eead4; --warn:#fbbf24; --bad:#fb7185; }
    * { box-sizing: border-box; } body { margin:0; min-height:100vh; font:16px/1.5 system-ui,sans-serif; color:var(--ink); background:radial-gradient(circle at top right,#183b55 0,var(--bg) 42%); }
    main { width:min(1120px,calc(100% - 32px)); margin:0 auto; padding:44px 0 64px; } header { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:28px; }
    h1 { margin:0; font-size:clamp(2rem,5vw,3.5rem); letter-spacing:-.04em; } h1 span { color:var(--accent); } p { color:var(--muted); }
    .badge { border:1px solid #2c766d; color:var(--accent); padding:7px 11px; border-radius:999px; white-space:nowrap; font-size:.85rem; }
    .panel { background:color-mix(in srgb,var(--panel) 92%,transparent); border:1px solid var(--line); border-radius:18px; padding:22px; box-shadow:0 18px 60px #02061766; }
    textarea { width:100%; min-height:118px; resize:vertical; border:1px solid #385070; border-radius:12px; padding:14px; color:var(--ink); background:#09111f; font:inherit; }
    button { margin-top:14px; border:0; border-radius:10px; padding:11px 18px; color:#052e2b; background:var(--accent); font-weight:700; cursor:pointer; } button:disabled { opacity:.5; cursor:wait; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:18px; margin-top:18px; } .label { color:var(--muted); font-size:.82rem; text-transform:uppercase; letter-spacing:.08em; }
    #state { margin-top:10px; white-space:pre-wrap; color:var(--muted); } #history { display:grid; gap:10px; margin-top:12px; } .task { border-left:3px solid var(--accent); padding:10px 12px; background:#0b1728; border-radius:7px; } .task.failed,.task.rejected { border-color:var(--bad); } .task.completed { border-color:var(--accent); }
    code { color:#c4d4ef; } .error { color:var(--bad); } .success { color:var(--accent); } pre { white-space:pre-wrap; overflow:auto; color:#cbd5e1; }
  </style>
</head>
<body>
<main>
  <header><div><div class="label">Lokaler Autonomie-Runtime</div><h1><span>ACB</span> Butler</h1><p>Natürliche Aufträge ausführen, prüfen und dauerhaft nachvollziehbar machen.</p></div><div class="badge" id="health">Runtime wird geprüft …</div></header>
  <section class="panel"><div class="label">Neuer Auftrag</div><form id="task-form"><textarea id="goal" required placeholder="Zum Beispiel: Erstelle die Datei notes.txt mit dem Inhalt Hallo ACB"></textarea><button id="submit" type="submit">Auftrag ausführen</button></form><div id="state">Bereit. Die Oberfläche ist ausschließlich auf diesem Rechner erreichbar.</div></section>
  <div class="grid"><section class="panel"><div class="label">Aktueller Status</div><div id="current">Noch kein Auftrag gestartet.</div></section><section class="panel"><div class="label">Verlauf</div><div id="history">Keine Aufträge in dieser Sitzung.</div></section></div>
</main>
<script>
const TOKEN = __ACB_TOKEN__; const form = document.getElementById('task-form'); const goal = document.getElementById('goal'); const submit = document.getElementById('submit'); const state = document.getElementById('state'); const current = document.getElementById('current'); const history = document.getElementById('history');
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
async function request(path, options={}) { const response = await fetch(path, {...options, headers:{'Content-Type':'application/json','X-ACB-Token':TOKEN,...(options.headers||{})}}); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Anfrage fehlgeschlagen'); return data; }
function renderTask(task) { const result = task.result || {}; const completion = result.completion || {}; const criteria = (completion.criteria || []).map(item => `<div>${item.passed ? '✓' : '✗'} ${esc(item.criterion_id)}: ${esc(item.evidence)}</div>`).join(''); current.innerHTML = `<strong class="${task.status === 'completed' ? 'success' : task.status === 'failed' || task.status === 'rejected' ? 'error' : ''}">${esc(task.status)}</strong><p>${esc(task.goal)}</p>${task.session_id ? `<div>Session: <code>${esc(task.session_id)}</code></div>` : ''}${criteria ? `<p>${criteria}</p>` : ''}${task.error ? `<p class="error">${esc(task.error)}</p>` : ''}${result.outputs ? `<details><summary>Ausgabe</summary><pre>${esc(JSON.stringify(result.outputs,null,2))}</pre></details>` : ''}`; }
async function refresh() { try { const data = await request('/api/tasks',{headers:{}}); history.innerHTML = data.tasks.length ? data.tasks.map(task => `<div class="task ${esc(task.status)}"><strong>${esc(task.status)}</strong> · ${esc(task.goal)}${task.session_id ? `<br><code>${esc(task.session_id)}</code>` : ''}</div>`).join('') : 'Keine Aufträge in dieser Sitzung.'; } catch (error) { state.textContent = error.message; state.className='error'; } }
async function poll(id) { try { const task = await request(`/api/tasks/${encodeURIComponent(id)}`,{headers:{}}); renderTask(task); await refresh(); if (['queued','running'].includes(task.status)) setTimeout(() => poll(id), 600); else { submit.disabled=false; state.textContent = task.status === 'completed' ? 'Auftrag vollständig verifiziert.' : 'Auftrag beendet; bitte Evidenz prüfen.'; state.className = task.status === 'completed' ? 'success' : 'error'; } } catch (error) { submit.disabled=false; state.textContent=error.message; state.className='error'; } }
form.addEventListener('submit', async event => { event.preventDefault(); submit.disabled=true; state.textContent='Auftrag angenommen …'; state.className=''; try { const task=await request('/api/tasks',{method:'POST',body:JSON.stringify({goal:goal.value})}); renderTask(task); goal.value=''; poll(task.request_id); } catch(error) { submit.disabled=false; state.textContent=error.message; state.className='error'; } });
request('/api/health',{headers:{}}).then(data => { document.getElementById('health').textContent = data.status === 'ok' ? '● Runtime bereit' : 'Runtime prüfen'; }).catch(() => { document.getElementById('health').textContent='Runtime nicht erreichbar'; }); refresh();
</script>
</body>
</html>"""


__all__ = [
    "DEFAULT_UI_HOST",
    "DEFAULT_UI_PORT",
    "MAX_REQUEST_BYTES",
    "UI_NAME",
    "AcbUiServer",
    "validate_ui_host",
]
