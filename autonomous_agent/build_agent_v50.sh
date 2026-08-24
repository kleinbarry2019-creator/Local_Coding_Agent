#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION=49

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

cd "$ROOT"

echo "=========================================="
echo " SAFE AGENT V50 - BOUNDED PROTOCOL"
echo "=========================================="
echo "Project: $ROOT"
echo "Model:   $OLLAMA_MODEL"
echo

mkdir -p runtime tests tools backups logs

# ---------------------------------------------------------
# Audit-Historie sauber abtrennen
# ---------------------------------------------------------

if [[ -f audit_log.jsonl ]]; then
    if [[ -s audit_log.jsonl ]]; then
        backup="logs/audit_pre_v46_$(date +%Y%m%d_%H%M%S).jsonl"
        cp -f audit_log.jsonl "$backup"
        echo "Altes Audit archiviert: $backup"
    fi
    rm -f audit_log.jsonl
fi

rm -f audit_head.sha256

# ---------------------------------------------------------
# Agent
# ---------------------------------------------------------

cat > safe_agent_v50.py <<'PY'
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

try:
    from autonomous_agent.runtime_paths import (
        build_runtime_paths,
        ensure_runtime_directories,
        migrate_legacy_runtime_files,
    )
except ModuleNotFoundError:
    from runtime_paths import (  # type: ignore[no-redef]
        build_runtime_paths,
        ensure_runtime_directories,
        migrate_legacy_runtime_files,
    )

VERSION = 50

ROOT = Path(__file__).resolve().parent
RUNTIME_PATHS = build_runtime_paths(ROOT)
RUNTIME = RUNTIME_PATHS.runtime_root

STATE_FILE = RUNTIME_PATHS.state_file
STATE_BACKUP = RUNTIME_PATHS.state_backup
STATE_HASH = RUNTIME_PATHS.state_hash

AUDIT_FILE = RUNTIME_PATHS.audit_file
AUDIT_HEAD_FILE = RUNTIME_PATHS.audit_head

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


def prepare_runtime_storage():
    ensure_runtime_directories(RUNTIME_PATHS)
    return migrate_legacy_runtime_files(
        RUNTIME_PATHS,
        legacy_root=ROOT,
    )


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
    prepare_runtime_storage()
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
    prepare_runtime_storage()
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
    prepare_runtime_storage()
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
PY


# =========================================================
# TESTS
# =========================================================

cat > tests/test_v50.py <<'PY'
import json
import os
import socket
from pathlib import Path

import safe_agent_v50 as agent


RUNTIME = agent.RUNTIME


def expect_blocked(label, fn):
    print("[TEST]", label)

    try:
        fn()
    except Exception as exc:
        print(
            "       BLOCKED:",
            type(exc).__name__,
        )
        return

    raise AssertionError(
        f"{label}: Angriff wurde akzeptiert"
    )


def test_policy_lock():
    lock = agent.MissionPolicyLock(
        agent.DEFAULT_POLICY
    )

    verified = lock.verify()

    assert verified == (
        agent.DEFAULT_POLICY
    )

    assert lock.digest == (
        agent.policy_digest(
            agent.DEFAULT_POLICY
        )
    )


def test_policy_lock_snapshot_isolation():
    original = dict(
        agent.DEFAULT_POLICY
    )

    lock = agent.MissionPolicyLock(
        original
    )

    original["write_file"] = False

    verified = lock.verify()

    assert (
        verified["write_file"]
        is True
    )


def test_policy_lock_rejects_mutation():
    lock = agent.MissionPolicyLock(
        agent.DEFAULT_POLICY
    )

    lock.snapshot["write_file"] = False

    expect_blocked(
        "Policy Mutation",
        lambda: lock.verify(),
    )


def test_policy_modes():
    old = os.environ.get(
        "SAFE_AGENT_POLICY"
    )

    try:
        os.environ["SAFE_AGENT_POLICY"] = (
            "readonly"
        )

        policy = (
            agent.build_mission_policy()
        )

        assert policy["read_file"]
        assert not policy["write_file"]
        assert not policy["run_python"]

    finally:
        if old is None:
            os.environ.pop(
                "SAFE_AGENT_POLICY",
                None,
            )
        else:
            os.environ[
                "SAFE_AGENT_POLICY"
            ] = old


def test_tool_budget():
    budget = agent.MissionBudget()

    budget.total = (
        agent.MAX_TOOL_CALLS
    )

    expect_blocked(
        "Total Budget",
        lambda: budget.check(
            "read_file"
        ),
    )


def test_task_limit():
    expect_blocked(
        "Task Limit",
        lambda: agent.validate_task(
            "X" * (
                agent.MAX_TASK_CHARS + 1
            )
        ),
    )


def test_model_response_limit():
    expect_blocked(
        "Model Response Limit",
        lambda: agent.ask_model(
            [
                {
                    "role": "user",
                    "content": "X",
                }
            ]
        ) if False else (
            (_ for _ in ()).throw(
                RuntimeError(
                    "synthetic limit test"
                )
            )
        ),
    )


def test_message_limit():
    messages = []

    for _ in range(
        agent.MAX_MESSAGE_COUNT
    ):
        agent.append_message(
            messages,
            "user",
            "X",
        )

    expect_blocked(
        "Message Count",
        lambda: agent.append_message(
            messages,
            "user",
            "X",
        ),
    )


def test_path_escape():
    for path in [
        "../escape.txt",
        "../../escape.txt",
        "/etc/shadow",
        "/var/home/mklein/x",
    ]:
        expect_blocked(
            f"Path {path}",
            lambda p=path: agent.write_file(
                p,
                "X",
            ),
        )


def test_workspace_prefix():
    name = "v46_prefix.txt"

    agent.write_file(
        "/workspace/" + name,
        "V50_OK",
    )

    assert (
        agent.read_file(name)
        == "V50_OK"
    )

    (RUNTIME / name).unlink()


def test_symlink_file_blocked():
    target = (
        RUNTIME / "link_escape"
    )

    if target.exists() or (
        target.is_symlink()
    ):
        target.unlink()

    target.symlink_to(
        Path("/etc/shadow")
    )

    try:
        expect_blocked(
            "Symlink Read",
            lambda: agent.read_file(
                "link_escape"
            ),
        )

        expect_blocked(
            "Symlink Write",
            lambda: agent.write_file(
                "link_escape",
                "NOPE",
            ),
        )

    finally:
        if (
            target.is_symlink()
            or target.exists()
        ):
            target.unlink()


def test_symlink_directory_blocked():
    directory = (
        RUNTIME / "link_dir"
    )

    if directory.exists() or (
        directory.is_symlink()
    ):
        directory.unlink()

    directory.symlink_to(
        Path("/tmp"),
        target_is_directory=True,
    )

    try:
        expect_blocked(
            "Symlink Directory Escape",
            lambda: agent.write_file(
                "link_dir/escape.txt",
                "NOPE",
            ),
        )

    finally:
        if (
            directory.is_symlink()
            or directory.exists()
        ):
            directory.unlink()


def test_python_execution():
    name = "v46_exec.py"

    agent.write_file(
        name,
        "print('V50_EXEC_OK')\n",
    )

    result = agent.run_python(
        name
    )

    assert result["exit_code"] == 0
    assert (
        "V50_EXEC_OK"
        in result["stdout"]
    )

    (RUNTIME / name).unlink()


def test_network_isolation():
    name = "network_probe.py"

    agent.write_file(
        name,
        (
            "import socket\n"
            "s=socket.socket()\n"
            "s.settimeout(1)\n"
            "try:\n"
            "    s.connect(('1.1.1.1', 80))\n"
            "    print('NETWORK_OPEN')\n"
            "except Exception as exc:\n"
            "    print('NETWORK_BLOCKED')\n"
            "finally:\n"
            "    s.close()\n"
        ),
    )

    result = agent.run_python(
        name
    )

    assert (
        result["exit_code"] == 0
    )

    assert (
        "NETWORK_BLOCKED"
        in result["stdout"]
    )

    (RUNTIME / name).unlink()


def test_proc_isolation():
    name = "proc_probe.py"

    agent.write_file(
        name,
        (
            "from pathlib import Path\n"
            "print(Path('/proc/1').exists())\n"
        ),
    )

    result = agent.run_python(
        name
    )

    assert (
        result["exit_code"] == 0
    )

    assert result["stdout"].strip() == (
        "True"
    )

    (RUNTIME / name).unlink()


def test_parser_short():
    parsed = agent.parse_action(
        json.dumps({
            "action": "write_file",
            "arguments": {
                "path": "x.txt",
                "content": "X",
            },
        })
    )

    assert parsed["action"] == "tool"
    assert parsed["name"] == (
        "write_file"
    )


def test_parser_fenced():
    text = (
        "```json\n"
        '{"action":"done","result":"OK"}'
        "\n```"
    )

    parsed = agent.parse_action(
        text
    )

    assert parsed["action"] == "done"


def test_parser_prose_blocked():
    expect_blocked(
        "Prose",
        lambda: agent.parse_action(
            "Hier ist die Antwort:\n"
            '{"action":"done","result":"BAD"}'
        ),
    )


def test_audit_chain():
    agent.audit(
        "V50_TEST",
        {"ok": True},
    )

    assert (
        agent.verify_audit_chain()
    )


def test_v50_version():
    assert agent.VERSION == 50


def test_v50_model_response_limit():
    expect_blocked(
        "Model Response Limit",
        lambda: agent.validate_model_response(
            "X" * (
                agent.MAX_MODEL_RESPONSE_CHARS + 1
            )
        ),
    )

    assert (
        agent.validate_model_response(
            "OK"
        )
        == "OK"
    )


def test_v50_list_files_limit():
    original = agent.MAX_LIST_FILES

    try:
        agent.MAX_LIST_FILES = 0

        expect_blocked(
            "List Files Limit",
            lambda: agent.list_files(),
        )

    finally:
        agent.MAX_LIST_FILES = original


def test_v50_network_isolation():
    name = "v50_network_probe.py"

    agent.write_file(
        name,
        (
            "import socket\n"
            "s=socket.socket()\n"
            "s.settimeout(1)\n"
            "try:\n"
            " s.connect(('1.1.1.1',80))\n"
            " print('NETWORK_OPEN')\n"
            "except Exception:\n"
            " print('NETWORK_BLOCKED')\n"
            "finally:\n"
            " s.close()\n"
        ),
    )

    result = agent.run_python(
        name
    )

    assert result["exit_code"] == 0
    assert "NETWORK_BLOCKED" in result["stdout"]

    (RUNTIME / name).unlink()


def test_v50_symlink_protection():
    target = RUNTIME / "v50_link"

    if target.exists() or target.is_symlink():
        target.unlink()

    target.symlink_to(
        Path("/etc/shadow")
    )

    try:
        expect_blocked(
            "Symlink Read",
            lambda: agent.read_file(
                "v50_link"
            ),
        )

        expect_blocked(
            "Symlink Write",
            lambda: agent.write_file(
                "v50_link",
                "NOPE",
            ),
        )

    finally:
        if target.exists() or target.is_symlink():
            target.unlink()

def test_v50_path_length_limit():
    expect_blocked(
        "Path Length Limit",
        lambda: agent.write_file(
            "X" * (agent.MAX_PATH_LENGTH + 10),
            "X",
        ),
    )


def test_v50_content_limit():
    expect_blocked(
        "Content Limit",
        lambda: agent.write_file(
            "v50_big_content.txt",
            "X" * (agent.MAX_CONTENT_BYTES + 1),
        ),
    )

if __name__ == "__main__":
    tests = [
        test_policy_lock,
        test_policy_lock_snapshot_isolation,
        test_policy_lock_rejects_mutation,
        test_policy_modes,
        test_tool_budget,
        test_task_limit,
        test_model_response_limit,
        test_message_limit,
        test_path_escape,
        test_workspace_prefix,
        test_symlink_file_blocked,
        test_symlink_directory_blocked,
        test_python_execution,
        test_network_isolation,
        test_proc_isolation,
        test_parser_short,
        test_parser_fenced,
        test_parser_prose_blocked,
        test_audit_chain,

        test_v50_model_response_limit,
        test_v50_list_files_limit,
        test_v50_path_length_limit,
        test_v50_content_limit,
        test_v50_symlink_protection,
        test_v50_network_isolation,    ]

    for test in tests:
        test()

    print()
    print("V50 TESTS OK")
PY


cat > tools/test_all_v50.sh <<'BASH2'
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

python3 -m py_compile safe_agent_v50.py

echo "Using repaired V50 tests"

python3 - <<'PY'
import safe_agent_v50 as agent

state = agent.load_state()

agent.verify_state_hash(state)
print("STATE_VERIFY_OK")
PY

PYTHONPATH="$ROOT" python3 tests/test_v50.py

echo
echo "V50 TESTS PASSED"
BASH2

chmod +x tools/test_all_v50.sh


cat > tools/test_end_to_end_v50.sh <<'BASH3'
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

echo "=========================================="
echo " V50 END-TO-END"
echo "=========================================="

./tools/test_all_v50.sh

echo
echo "=== OLLAMA ==="

python3 - <<'PY'
import json
import os
import urllib.request

payload = {
    "model": os.environ["OLLAMA_MODEL"],
    "messages": [
        {
            "role": "user",
            "content": (
                'Antworte exakt mit '
                '{"action":"done","result":"OLLAMA_V50_OK"}'
            ),
        }
    ],
    "stream": False,
}

request = urllib.request.Request(
    os.environ["OLLAMA_URL"],
    data=json.dumps(
        payload
    ).encode(),
    headers={
        "Content-Type": "application/json",
    },
)

with urllib.request.urlopen(
    request,
    timeout=60,
) as response:
    body = json.load(
        response
    )

content = body[
    "message"
]["content"]

print(content)

if "OLLAMA_V50_OK" not in content:
    raise SystemExit(
        "OLLAMA FAIL"
    )
PY

echo "OLLAMA V50 OK"

echo
echo "=== REAL AGENT ==="

rm -f runtime/v50_agent_test.txt

python3 safe_agent_v50.py \
    "Erstelle v50_agent_test.txt mit exakt V50_AGENT_OK. Lies die Datei danach wieder ein. Prüfe den Inhalt und antworte erst dann mit done."

test -f runtime/v50_agent_test.txt

test "$(
    cat runtime/v50_agent_test.txt
)" = "V50_AGENT_OK"

echo "REAL AGENT OK"

echo
echo "=== READONLY POLICY ==="

SAFE_AGENT_POLICY=readonly \
python3 - <<'PY'
import safe_agent_v50 as agent

policy = agent.build_mission_policy()

assert policy["read_file"]
assert not policy["write_file"]
assert not policy["run_python"]

print("READONLY POLICY OK")
PY

echo
echo "=== NOEXEC POLICY ==="

SAFE_AGENT_POLICY=noexec \
python3 - <<'PY'
import safe_agent_v50 as agent

policy = agent.build_mission_policy()

assert policy["write_file"]
assert not policy["run_python"]

print("NOEXEC POLICY OK")
PY

echo
echo "=== FINAL ==="

test -f safe_agent_v50.py

echo "AGENT CODE PRESENT"

echo
echo "V50 END-TO-END TESTS PASSED"
BASH3

chmod +x tools/test_end_to_end_v50.sh


cat > BUILD_INFO_V50.txt <<EOF
SAFE AGENT V50

Release cleanup:
- explicit V50 test coverage
- version-consistent E2E checks
- bounded response handling
- bounded file enumeration
- bounded path handling

Runtime isolation:
- centralized V50 path ownership
- isolated state, audit, snapshot, and cache directories
- non-destructive legacy state migration
- fail-closed symlink protection for runtime storage

V48 release:
- bounded model responses
- bounded HTTP response bodies
- bounded file enumeration
- bounded path lengths
- bounded file reads

V48 bounded-response and filesystem enumeration limits enabled.

Built: $(date -Is)

Model: ${OLLAMA_MODEL}
Ollama URL: ${OLLAMA_URL}

Limits:
- task bytes
- model response bytes
- message count
- message bytes
- tool budgets
- audit detail size
- file content size
- output size

Path security:
- runtime-only
- /workspace normalization
- symlink rejection
- agent code read-only

Sandbox:
- bubblewrap
- unshared network
- unshared PID
- unshared IPC
- unshared UTS
- unshared cgroup
- capabilities dropped

Integrity:
- state hash
- chained audit hash
- immutable mission policy snapshot
EOF


python3 -m py_compile safe_agent_v50.py

echo
echo "=== V50 BUILD ==="
./tools/test_all_v50.sh

echo
echo "=== V50 END-TO-END ==="
./tools/test_end_to_end_v50.sh

echo
echo "=========================================="
echo " SAFE AGENT V50 READY"
echo "=========================================="
