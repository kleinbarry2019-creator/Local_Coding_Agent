#!/usr/bin/env python3

import datetime
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


VERSION = 50

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"

STATE_FILE = ROOT / "agent_state.json"
STATE_BACKUP = ROOT / "agent_state.json.bak"
STATE_HASH = ROOT / "agent_state.sha256"

AUDIT_FILE = ROOT / "audit_log.jsonl"
AUDIT_HEAD_FILE = ROOT / "audit_head.sha256"

MAX_STATE_BYTES = 16384
MAX_AUDIT_BYTES = 1048576

MAX_CONTENT_BYTES = 262144
MAX_OUTPUT_CHARS = 8192
MAX_MODEL_RESPONSE_CHARS = 65536
MAX_TASK_CHARS = 8192
MAX_MESSAGE_COUNT = 64
MAX_MESSAGE_CHARS = 131072
MAX_AUDIT_DETAIL_CHARS = 8192
MAX_LIST_FILES = 2048
MAX_LIST_PATH_CHARS = 4096
MAX_HTTP_RESPONSE_BYTES = 131072

MAX_STEPS = 20
MAX_TOOL_CALLS = 20
MAX_READ_CALLS = 50
MAX_WRITE_CALLS = 20
MAX_EXEC_CALLS = 10
MAX_SCRIPT_ARGS = 16

COMMAND_TIMEOUT = 15

OLLAMA_URL = os.environ.get(
    "OLLAMA_URL",
    "http://127.0.0.1:11434/api/chat",
)

MODEL = os.environ.get(
    "OLLAMA_MODEL",
    "qwen2.5-coder:7b-instruct",
)


# =========================================================
# MISSION POLICY
# =========================================================

DEFAULT_POLICY = {
    "list_files": True,
    "read_file": True,
    "write_file": True,
    "run_python": True,
}

READ_ONLY_POLICY = {
    "list_files": True,
    "read_file": True,
    "write_file": False,
    "run_python": False,
}

NO_EXEC_POLICY = {
    "list_files": True,
    "read_file": True,
    "write_file": True,
    "run_python": False,
}


def normalize_policy(policy):
    if not isinstance(
        policy,
        dict,
    ):
        raise RuntimeError(
            "FAIL-CLOSED: Mission Policy muss Objekt sein."
        )

    if set(policy) != set(
        DEFAULT_POLICY
    ):
        raise RuntimeError(
            "FAIL-CLOSED: Ungültige Mission Policy."
        )

    if not all(
        isinstance(
            value,
            bool,
        )
        for value in policy.values()
    ):
        raise RuntimeError(
            "FAIL-CLOSED: Policy-Werte müssen bool sein."
        )

    return dict(policy)


def policy_digest(policy):
    return hashlib.sha256(
        json.dumps(
            policy,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_mission_policy():
    raw = os.environ.get(
        "SAFE_AGENT_POLICY",
        "default",
    )

    if raw == "default":
        return normalize_policy(
            DEFAULT_POLICY
        )

    if raw == "readonly":
        return normalize_policy(
            READ_ONLY_POLICY
        )

    if raw == "noexec":
        return normalize_policy(
            NO_EXEC_POLICY
        )

    raise RuntimeError(
        "FAIL-CLOSED: Unbekannte SAFE_AGENT_POLICY."
    )


# =========================================================
# STATE
# =========================================================

def default_state():
    return {
        "version": VERSION,
        "tasks_completed": 0,
        "last_result": None,
    }


def validate_state(state):
    if not isinstance(
        state,
        dict,
    ):
        raise RuntimeError(
            "FAIL-CLOSED: State ist kein Objekt."
        )

    required = {
        "version",
        "tasks_completed",
        "last_result",
    }

    missing = required - set(state)

    if missing:
        raise RuntimeError(
            f"FAIL-CLOSED: State-Felder fehlen: {sorted(missing)}"
        )

    if state["version"] != VERSION:
        raise RuntimeError(
            "FAIL-CLOSED: Falsche State-Version."
        )

    if (
        not isinstance(
            state["tasks_completed"],
            int,
        )
        or isinstance(
            state["tasks_completed"],
            bool,
        )
        or state["tasks_completed"] < 0
    ):
        raise RuntimeError(
            "FAIL-CLOSED: tasks_completed ungültig."
        )

    if (
        state["last_result"] is not None
        and not isinstance(
            state["last_result"],
            str,
        )
    ):
        raise RuntimeError(
            "FAIL-CLOSED: last_result ungültig."
        )

    return state


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def state_digest(state):
    return hashlib.sha256(
        canonical_json(state).encode("utf-8")
    ).hexdigest()


def atomic_write(path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        text=True,
    )

    tmp = Path(tmp_name)

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(
            tmp,
            path,
        )

    finally:
        if tmp.exists():
            tmp.unlink()


def verify_state_hash(state):
    if not STATE_HASH.exists():
        raise RuntimeError(
            "FAIL-CLOSED: State-Hash fehlt."
        )

    actual = STATE_HASH.read_text(
        encoding="utf-8"
    ).strip()

    expected = state_digest(
        state
    )

    if actual != expected:
        raise RuntimeError(
            "FAIL-CLOSED: State-Hash stimmt nicht."
        )


def save_state(state):
    validate_state(state)

    encoded = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    )

    if len(
        encoded.encode("utf-8")
    ) > MAX_STATE_BYTES:
        raise RuntimeError(
            "FAIL-CLOSED: State zu groß."
        )

    if STATE_FILE.exists():
        shutil.copy2(
            STATE_FILE,
            STATE_BACKUP,
        )

    atomic_write(
        STATE_FILE,
        encoded + "\n",
    )

    atomic_write(
        STATE_HASH,
        state_digest(state) + "\n",
    )


def load_one_state(path):
    if not path.exists():
        return None

    if path.stat().st_size > MAX_STATE_BYTES:
        raise RuntimeError(
            f"FAIL-CLOSED: State zu groß: {path}"
        )

    try:
        state = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        return validate_state(
            state
        )

    except Exception:
        return None


def load_state():
    current = load_one_state(
        STATE_FILE
    )

    if current is not None:
        verify_state_hash(
            current
        )
        return current

    backup = load_one_state(
        STATE_BACKUP
    )

    if backup is not None:
        save_state(
            backup
        )

        audit(
            "STATE_RECOVERY",
            {
                "source": str(
                    STATE_BACKUP
                ),
            },
        )

        return backup

    state = default_state()

    save_state(
        state
    )

    audit(
        "STATE_INITIALIZED",
        {},
    )

    return state


# =========================================================
# AUDIT CHAIN
# =========================================================

def audit_digest(entry, previous):
    return hashlib.sha256(
        (
            previous
            + "\n"
            + canonical_json(entry)
        ).encode("utf-8")
    ).hexdigest()


def read_audit_head():
    if not AUDIT_HEAD_FILE.exists():
        return "0" * 64

    value = AUDIT_HEAD_FILE.read_text(
        encoding="utf-8"
    ).strip()

    if len(value) != 64:
        raise RuntimeError(
            "FAIL-CLOSED: Audit-Head ungültig."
        )

    return value


def bounded_audit_details(details):
    encoded = json.dumps(
        details,
        ensure_ascii=False,
    )

    if len(
        encoded.encode("utf-8")
    ) <= MAX_AUDIT_DETAIL_CHARS:
        return details

    return {
        "truncated": True,
        "sha256": hashlib.sha256(
            encoded.encode("utf-8")
        ).hexdigest(),
        "preview": encoded[
            :MAX_AUDIT_DETAIL_CHARS
        ],
    }


def audit(event, details):
    entry = {
        "timestamp": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "event": event,
        "details": bounded_audit_details(
            details
        ),
    }

    if (
        AUDIT_FILE.exists()
        and AUDIT_FILE.stat().st_size
        > MAX_AUDIT_BYTES
    ):
        raise RuntimeError(
            "FAIL-CLOSED: Audit-Log zu groß."
        )

    previous = read_audit_head()

    digest = audit_digest(
        entry,
        previous,
    )

    record = {
        "prev": previous,
        "digest": digest,
        "entry": entry,
    }

    with AUDIT_FILE.open(
        "a",
        encoding="utf-8",
    ) as handle:
        handle.write(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )

    atomic_write(
        AUDIT_HEAD_FILE,
        digest + "\n",
    )


def verify_audit_chain():
    if not AUDIT_FILE.exists():
        return True

    previous = "0" * 64

    with AUDIT_FILE.open(
        "r",
        encoding="utf-8",
    ) as handle:
        for line_no, line in enumerate(
            handle,
            1,
        ):
            if not line.strip():
                continue

            record = json.loads(
                line
            )

            if set(record) != {
                "prev",
                "digest",
                "entry",
            }:
                raise RuntimeError(
                    f"FAIL-CLOSED: Audit-Schema Zeile {line_no}"
                )

            if record["prev"] != previous:
                raise RuntimeError(
                    f"FAIL-CLOSED: Audit-Kette Zeile {line_no}"
                )

            expected = audit_digest(
                record["entry"],
                previous,
            )

            if record["digest"] != expected:
                raise RuntimeError(
                    f"FAIL-CLOSED: Audit-Hash Zeile {line_no}"
                )

            previous = record[
                "digest"
            ]

    if read_audit_head() != previous:
        raise RuntimeError(
            "FAIL-CLOSED: Audit-Head stimmt nicht."
        )

    return True


# =========================================================
# PATH SECURITY
# =========================================================

def ensure_runtime():
    RUNTIME.mkdir(
        parents=True,
        exist_ok=True,
    )

    if RUNTIME.is_symlink():
        raise RuntimeError(
            "FAIL-CLOSED: Runtime ist Symlink."
        )


def reject_symlink_components(
    target
):
    ensure_runtime()

    try:
        relative = target.relative_to(
            RUNTIME
        )
    except ValueError as exc:
        raise PermissionError(
            "Pfad außerhalb des Runtime-Workspace."
        ) from exc

    current = RUNTIME

    for part in relative.parts:
        current = current / part

        try:
            info = current.lstat()
        except FileNotFoundError:
            continue

        if stat.S_ISLNK(
            info.st_mode
        ):
            raise PermissionError(
                f"Symlink im Runtime-Pfad verboten: {current}"
            )


def resolve_runtime_path(
    path_text
):
    if not isinstance(
        path_text,
        str,
    ):
        raise PermissionError(
            "path muss String sein."
        )

    if not path_text.strip():
        raise PermissionError(
            "Leerer Pfad."
        )

    ensure_runtime()

    if path_text == "/workspace":
        path_text = "."

    elif path_text.startswith(
        "/workspace/"
    ):
        path_text = path_text[
            len("/workspace/") :
        ]

    elif path_text.startswith("/"):
        raise PermissionError(
            "Absoluter Pfad außerhalb von /workspace."
        )

    candidate = (
        RUNTIME / path_text
    ).resolve(
        strict=False
    )

    try:
        candidate.relative_to(
            RUNTIME
        )
    except ValueError as exc:
        raise PermissionError(
            "Pfad außerhalb des Runtime-Workspace."
        ) from exc

    reject_symlink_components(
        candidate
    )

    return candidate


# =========================================================
# TOOLS
# =========================================================

def list_files():
    ensure_runtime()

    result = []

    for path in RUNTIME.rglob("*"):

        if not path.is_file():
            continue

        reject_symlink_components(
            path
        )

        relative = path.relative_to(
            RUNTIME
        ).as_posix()

        if len(relative) > MAX_LIST_PATH_CHARS:
            raise PermissionError(
                "Dateipfad überschreitet Größenlimit."
            )

        result.append(
            relative
        )

        if len(result) > MAX_LIST_FILES:
            raise PermissionError(
                "Zu viele Dateien im Runtime-Workspace."
            )

    return sorted(
        result
    )


def read_file(path):
    target = resolve_runtime_path(
        path
    )

    if not target.exists():
        raise FileNotFoundError(
            f"Datei nicht gefunden: {path}"
        )

    if not target.is_file():
        raise PermissionError(
            "Kein regulärer Dateipfad."
        )

    size = target.stat().st_size

    if size > MAX_CONTENT_BYTES:
        raise PermissionError(
            "Datei überschreitet Größenlimit."
        )

    content = target.read_text(
        encoding="utf-8"
    )

    if len(
        content.encode("utf-8")
    ) > MAX_CONTENT_BYTES:
        raise PermissionError(
            "Dateiinhalt überschreitet Größenlimit."
        )

    return content


def write_file(path, content):
    if not isinstance(
        content,
        str,
    ):
        raise TypeError(
            "content muss String sein."
        )

    encoded = content.encode(
        "utf-8"
    )

    if len(encoded) > MAX_CONTENT_BYTES:
        raise PermissionError(
            "Inhalt überschreitet Größenlimit."
        )

    target = resolve_runtime_path(
        path
    )

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    reject_symlink_components(
        target
    )

    atomic_write(
        target,
        content,
    )

    reject_symlink_components(
        target
    )

    return {
        "path": target.relative_to(
            RUNTIME
        ).as_posix(),
        "bytes": len(encoded),
    }


def run_python(script, args=None):
    if args is None:
        args = []

    if not isinstance(
        args,
        list,
    ):
        raise TypeError(
            "args muss Liste sein."
        )

    if len(args) > MAX_SCRIPT_ARGS:
        raise PermissionError(
            "Zu viele Script-Argumente."
        )

    if not all(
        isinstance(
            item,
            str,
        )
        for item in args
    ):
        raise TypeError(
            "args dürfen nur Strings enthalten."
        )

    script_path = resolve_runtime_path(
        script
    )

    if script_path.suffix != ".py":
        raise PermissionError(
            "Nur .py-Skripte erlaubt."
        )

    if not script_path.is_file():
        raise FileNotFoundError(
            f"Skript nicht gefunden: {script}"
        )

    relative_script = (
        script_path
        .relative_to(
            RUNTIME
        )
        .as_posix()
    )

    command = [
        "/usr/bin/bwrap",
        "--die-with-parent",

        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-cgroup",

        "--cap-drop",
        "ALL",

        "--ro-bind",
        "/usr",
        "/usr",

        "--ro-bind",
        "/bin",
        "/bin",

        "--ro-bind",
        "/lib",
        "/lib",

        "--ro-bind",
        "/lib64",
        "/lib64",

        "--ro-bind",
        str(ROOT),
        "/agent",

        "--bind",
        str(RUNTIME),
        "/workspace",

        "--proc",
        "/proc",

        "--dev",
        "/dev",

        "--tmpfs",
        "/etc",

        "--tmpfs",
        "/tmp",

        "--tmpfs",
        "/var",

        "--chdir",
        "/workspace",

        sys.executable,
        "/workspace/" + relative_script,
        *args,
    ]

    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=COMMAND_TIMEOUT,
            shell=False,
            check=False,
        )

    except subprocess.TimeoutExpired as exc:
        return {
            "status": "TIMEOUT",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
        }

    return {
        "status": (
            "OK"
            if result.returncode == 0
            else "ERROR"
        ),
        "exit_code": result.returncode,
        "stdout": result.stdout[
            :MAX_OUTPUT_CHARS
        ],
        "stderr": result.stderr[
            :MAX_OUTPUT_CHARS
        ],
    }


TOOL_NAMES = {
    "list_files",
    "read_file",
    "write_file",
    "run_python",
}


# =========================================================
# BUDGET + POLICY LOCK
# =========================================================

class MissionBudget:
    def __init__(self):
        self.total = 0
        self.reads = 0
        self.writes = 0
        self.execs = 0

    def check(self, name):
        if self.total >= MAX_TOOL_CALLS:
            raise RuntimeError(
                "FAIL-CLOSED: Tool-Budget erschöpft."
            )

        if (
            name in {
                "list_files",
                "read_file",
            }
            and self.reads >= MAX_READ_CALLS
        ):
            raise RuntimeError(
                "FAIL-CLOSED: Read-Budget erschöpft."
            )

        if (
            name == "write_file"
            and self.writes >= MAX_WRITE_CALLS
        ):
            raise RuntimeError(
                "FAIL-CLOSED: Write-Budget erschöpft."
            )

        if (
            name == "run_python"
            and self.execs >= MAX_EXEC_CALLS
        ):
            raise RuntimeError(
                "FAIL-CLOSED: Execute-Budget erschöpft."
            )

    def consume(self, name):
        self.total += 1

        if name in {
            "list_files",
            "read_file",
        }:
            self.reads += 1

        elif name == "write_file":
            self.writes += 1

        elif name == "run_python":
            self.execs += 1


class MissionPolicyLock:
    def __init__(self, policy):
        self.snapshot = normalize_policy(
            policy
        )
        self.digest = policy_digest(
            self.snapshot
        )

    def verify(self):
        if (
            policy_digest(
                self.snapshot
            )
            != self.digest
        ):
            raise RuntimeError(
                "FAIL-CLOSED: Mission Policy verändert."
            )

        return self.snapshot


def execute_tool(
    name,
    arguments,
    policy_lock,
    budget,
):
    policy = policy_lock.verify()

    if name not in TOOL_NAMES:
        raise PermissionError(
            f"Tool nicht erlaubt: {name}"
        )

    if not policy.get(
        name,
        False,
    ):
        raise PermissionError(
            f"Tool durch Mission Policy verboten: {name}"
        )

    if not isinstance(
        arguments,
        dict,
    ):
        raise TypeError(
            "arguments muss Objekt sein."
        )

    budget.check(name)

    if name == "list_files":
        if arguments:
            raise PermissionError(
                "list_files akzeptiert keine Argumente."
            )

        result = list_files()

    elif name == "read_file":
        if set(arguments) != {
            "path"
        }:
            raise PermissionError(
                "read_file erwartet exakt path."
            )

        result = read_file(
            arguments["path"]
        )

    elif name == "write_file":
        if set(arguments) != {
            "path",
            "content",
        }:
            raise PermissionError(
                "write_file erwartet path und content."
            )

        result = write_file(
            arguments["path"],
            arguments["content"],
        )

    elif name == "run_python":
        if set(arguments) - {
            "script",
            "args",
        }:
            raise PermissionError(
                "run_python enthält unerlaubte Argumente."
            )

        result = run_python(
            arguments["script"],
            arguments.get(
                "args",
                [],
            ),
        )

    else:
        raise RuntimeError(
            "Unbekanntes Tool."
        )

    budget.consume(name)

    return result


# =========================================================
# MODEL
# =========================================================


def validate_model_response(content):

    if not isinstance(content, str):
        raise RuntimeError(
            "FAIL-CLOSED: Modellantwort muss Text sein."
        )

    size = len(
        content.encode("utf-8")
    )

    if size > MAX_MODEL_RESPONSE_CHARS:
        raise RuntimeError(
            "FAIL-CLOSED: Modellantwort zu groß."
        )

    return content


def ask_model(messages):
    if len(messages) > MAX_MESSAGE_COUNT:
        raise RuntimeError(
            "FAIL-CLOSED: Nachrichtenlimit erreicht."
        )

    encoded_messages = json.dumps(
        messages,
        ensure_ascii=False,
    )

    if len(
        encoded_messages.encode(
            "utf-8"
        )
    ) > MAX_MESSAGE_CHARS:
        raise RuntimeError(
            "FAIL-CLOSED: Nachrichten zu groß."
        )

    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(
            payload
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:

            declared = response.headers.get(
                "Content-Length"
            )

            if declared is not None:
                try:
                    if int(declared) > MAX_HTTP_RESPONSE_BYTES:
                        raise RuntimeError(
                            "FAIL-CLOSED: HTTP-Antwort zu groß."
                        )
                except ValueError:
                    pass

            raw = response.read(
                MAX_HTTP_RESPONSE_BYTES + 1
            )

            if len(raw) > MAX_HTTP_RESPONSE_BYTES:
                raise RuntimeError(
                    "FAIL-CLOSED: HTTP-Antwort zu groß."
                )

            body = json.loads(
                raw.decode("utf-8")
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Ollama HTTP {exc.code}: {detail}"
        ) from exc

    except Exception as exc:
        raise RuntimeError(
            f"Ollama nicht erreichbar: {exc}"
        ) from exc

    message = body.get(
        "message"
    )

    if not isinstance(
        message,
        dict,
    ):
        raise RuntimeError(
            "Ollama-Antwort enthält keine message."
        )

    content = message.get(
        "content"
    )

    if not isinstance(
        content,
        str,
    ):
        raise RuntimeError(
            "Ollama-Antwort enthält keinen Text."
        )

    if len(
        content.encode(
            "utf-8"
        )
    ) > MAX_MODEL_RESPONSE_CHARS:
        raise RuntimeError(
            "FAIL-CLOSED: Modellantwort zu groß."
        )

    return validate_model_response(
        content.strip()
    )


# =========================================================
# PROTOCOL
# =========================================================

def normalize_model_json(text):
    if not isinstance(
        text,
        str,
    ):
        raise RuntimeError(
            "FAIL-CLOSED: Modellantwort muss Text sein."
        )

    value = text.strip()

    if (
        value.startswith("```")
        and value.endswith("```")
    ):
        lines = value.splitlines()

        if len(lines) < 3:
            raise RuntimeError(
                "FAIL-CLOSED: Ungültiger Markdown-Codeblock."
            )

        opening = lines[
            0
        ].strip().lower()

        closing = lines[
            -1
        ].strip()

        if closing != "```":
            raise RuntimeError(
                "FAIL-CLOSED: Codeblock nicht korrekt geschlossen."
            )

        if opening not in {
            "```",
            "```json",
        }:
            raise RuntimeError(
                "FAIL-CLOSED: Nur JSON-Codeblock erlaubt."
            )

        value = "\n".join(
            lines[1:-1]
        ).strip()

    if not value:
        raise RuntimeError(
            "FAIL-CLOSED: Leere Modellantwort."
        )

    return value


def parse_action(text):
    value = normalize_model_json(
        text
    )

    try:
        obj = json.loads(
            value
        )

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "FAIL-CLOSED: Antwort ist kein einzelnes JSON."
        ) from exc

    if not isinstance(
        obj,
        dict,
    ):
        raise RuntimeError(
            "FAIL-CLOSED: JSON-Objekt erwartet."
        )

    if set(obj) == {
        "action",
        "name",
        "arguments",
    }:
        if obj["action"] != "tool":
            raise RuntimeError(
                "FAIL-CLOSED: Ungültige Tool-Aktion."
            )

        if not isinstance(
            obj["name"],
            str,
        ):
            raise RuntimeError(
                "FAIL-CLOSED: Toolname ungültig."
            )

        if not isinstance(
            obj["arguments"],
            dict,
        ):
            raise RuntimeError(
                "FAIL-CLOSED: arguments ungültig."
            )

        return obj

    if set(obj) == {
        "action",
        "arguments",
    }:
        if obj["action"] not in TOOL_NAMES:
            raise RuntimeError(
                "FAIL-CLOSED: Unbekannte Kurzformat-Aktion."
            )

        if not isinstance(
            obj["arguments"],
            dict,
        ):
            raise RuntimeError(
                "FAIL-CLOSED: arguments ungültig."
            )

        return {
            "action": "tool",
            "name": obj["action"],
            "arguments": obj["arguments"],
        }

    if set(obj) == {
        "action",
        "result",
    }:
        if obj["action"] != "done":
            raise RuntimeError(
                "FAIL-CLOSED: Ungültige Abschlussaktion."
            )

        if not isinstance(
            obj["result"],
            str,
        ):
            raise RuntimeError(
                "FAIL-CLOSED: result ungültig."
            )

        return obj

    raise RuntimeError(
        "FAIL-CLOSED: Unbekanntes Antwortschema."
    )


# =========================================================
# AGENT LOOP
# =========================================================

def validate_task(task):
    if not isinstance(
        task,
        str,
    ):
        raise TypeError(
            "Task muss String sein."
        )

    if not task.strip():
        raise RuntimeError(
            "FAIL-CLOSED: Leere Aufgabe."
        )

    if len(
        task.encode("utf-8")
    ) > MAX_TASK_CHARS:
        raise RuntimeError(
            "FAIL-CLOSED: Aufgabe überschreitet Größenlimit."
        )

    return task.strip()


def append_message(
    messages,
    role,
    content,
):
    if not isinstance(
        content,
        str,
    ):
        raise TypeError(
            "Nachrichteninhalt muss String sein."
        )

    if len(
        content.encode("utf-8")
    ) > MAX_MESSAGE_CHARS:
        raise RuntimeError(
            "FAIL-CLOSED: Nachrichteninhalt zu groß."
        )

    messages.append(
        {
            "role": role,
            "content": content,
        }
    )

    if len(messages) > MAX_MESSAGE_COUNT:
        raise RuntimeError(
            "FAIL-CLOSED: Nachrichtenlimit erreicht."
        )


def run_agent(task):
    task = validate_task(
        task
    )

    state = load_state()

    policy_lock = MissionPolicyLock(
        build_mission_policy()
    )

    budget = MissionBudget()

    verify_audit_chain()

    audit(
        "MISSION_START",
        {
            "task": task,
            "model": MODEL,
            "policy": policy_lock.snapshot,
            "policy_digest": policy_lock.digest,
        },
    )

    system_prompt = """
Du bist ein sicherer autonomer Entwickler-Agent.

Du arbeitest ausschließlich in /workspace.

Verfügbare Werkzeuge:

{"action":"tool","name":"list_files","arguments":{}}

{"action":"tool","name":"read_file","arguments":{"path":"datei.txt"}}

{"action":"tool","name":"write_file","arguments":{"path":"datei.txt","content":"TEXT"}}

{"action":"tool","name":"run_python","arguments":{"script":"test.py","args":[]}}

Abschluss:

{"action":"done","result":"..."}

Ein einzelner ```json-Codeblock ist erlaubt.

Regeln:
- Kein Shell.
- Kein bash.
- Kein sh.
- Kein exec.
- Kein subprocess.
- Kein Text vor dem JSON.
- Kein Text nach dem JSON.
- Genau ein JSON-Objekt pro Antwort.
- Nur /workspace verändern.
- Niemals /agent verändern.
- Prüfe jedes Tool-Ergebnis.
"""

    messages = []

    append_message(
        messages,
        "system",
        system_prompt,
    )

    append_message(
        messages,
        "user",
        (
            "AUFGABE:\n"
            + task
            + "\n"
            + "Beginne jetzt."
        ),
    )

    for step in range(
        1,
        MAX_STEPS + 1,
    ):
        policy_lock.verify()

        print()
        print(
            f"=== STEP {step}/{MAX_STEPS} ==="
        )

        response = ask_model(
            messages
        )

        print("MODEL:")
        print(response)

        action = parse_action(
            response
        )

        append_message(
            messages,
            "assistant",
            response,
        )

        if action["action"] == "done":
            result = action["result"]

            state["tasks_completed"] += 1
            state["last_result"] = result

            save_state(
                state
            )

            audit(
                "MISSION_DONE",
                {
                    "step": step,
                    "result": result,
                    "policy_digest": policy_lock.digest,
                    "tool_usage": {
                        "total": budget.total,
                        "reads": budget.reads,
                        "writes": budget.writes,
                        "execs": budget.execs,
                    },
                },
            )

            print()
            print("=== DONE ===")
            print(result)
            return

        try:
            result = execute_tool(
                action["name"],
                action["arguments"],
                policy_lock,
                budget,
            )

        except Exception as exc:
            result = {
                "status": "ERROR",
                "error": type(exc).__name__,
                "message": str(exc),
            }

        audit(
            "TOOL",
            {
                "step": step,
                "name": action["name"],
                "status": (
                    result.get(
                        "status",
                        "OK",
                    )
                    if isinstance(
                        result,
                        dict,
                    )
                    else "OK"
                ),
                "policy_digest": policy_lock.digest,
                "usage": {
                    "total": budget.total,
                    "reads": budget.reads,
                    "writes": budget.writes,
                    "execs": budget.execs,
                },
            },
        )

        print("RESULT:")

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
            if isinstance(
                result,
                (dict, list),
            )
            else result
        )

        feedback = (
            "TOOL-ERGEBNIS:\n"
            + json.dumps(
                result,
                ensure_ascii=False,
            )
            + "\n"
            + "Analysiere das Ergebnis und fahre fort."
        )

        append_message(
            messages,
            "user",
            feedback,
        )

    state["last_result"] = (
        "FAIL-CLOSED: Schrittlimit erreicht."
    )

    save_state(
        state
    )

    audit(
        "MISSION_ABORTED",
        {
            "reason": "step_limit",
            "max_steps": MAX_STEPS,
            "policy_digest": policy_lock.digest,
            "tool_usage": {
                "total": budget.total,
                "reads": budget.reads,
                "writes": budget.writes,
                "execs": budget.execs,
            },
        },
    )

    raise RuntimeError(
        "FAIL-CLOSED: Maximale Schrittzahl erreicht."
    )


def main():
    if len(sys.argv) < 2:
        print(
            'Usage: python3 safe_agent_v50.py "AUFGABE"'
        )
        raise SystemExit(2)

    print(
        "=== SAFE AGENT V50 ==="
    )
    print(
        "Project:",
        ROOT,
    )
    print(
        "Runtime:",
        RUNTIME,
    )
    print(
        "Model:",
        MODEL,
    )
    print(
        "Policy:",
        os.environ.get(
            "SAFE_AGENT_POLICY",
            "default",
        ),
    )

    run_agent(
        " ".join(sys.argv[1:])
    )


if __name__ == "__main__":
    main()
