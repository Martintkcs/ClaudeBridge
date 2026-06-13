from __future__ import annotations

import argparse
import base64
from collections import deque
import datetime as dt
import html
import json
import mimetypes
import secrets
import socket
import sqlite3
import sys
import subprocess
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "claude_bridge.sqlite3"
CONFIG_PATH = ROOT / "config.json"
UPLOADS_DIR = ROOT / "uploads"
CLAUDE_TIMEOUT_SECONDS = 60 * 60
RUN_PROCESSES: dict[str, subprocess.Popen] = {}
RUN_PROCESSES_LOCK = threading.Lock()


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def safe_print(message: str) -> None:
    if sys.stdout:
        print(message, flush=True)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    config = {
        "token": secrets.token_urlsafe(24),
        "claude_command": "claude",
        "default_workdir": str(ROOT),
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


CONFIG = load_config()


def appdata_claude_dir() -> Path:
    local_appdata = os_environ("LOCALAPPDATA")
    return Path(local_appdata) / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"


def os_environ(name: str) -> str:
    import os

    return os.environ.get(name, "")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists runs (
                id text primary key,
                kind text not null,
                session_id text,
                workdir text,
                attachments text,
                model text,
                permission_mode text,
                allowed_tools text,
                message text not null,
                status text not null,
                summary text,
                error text,
                created_at text not null,
                started_at text,
                finished_at text
            );

            create table if not exists jobs (
                id text primary key,
                run_at text not null,
                session_id text,
                workdir text,
                attachments text,
                model text,
                permission_mode text,
                allowed_tools text,
                message text not null,
                status text not null,
                run_id text,
                created_at text not null,
                executed_at text
            );

            create table if not exists run_events (
                id integer primary key autoincrement,
                run_id text not null,
                kind text not null,
                title text,
                body text,
                raw text,
                created_at text not null
            );
            """
        )
        columns = {
            "runs": {row["name"] for row in conn.execute("pragma table_info(runs)")},
            "jobs": {row["name"] for row in conn.execute("pragma table_info(jobs)")},
        }
        if "workdir" not in columns["runs"]:
            conn.execute("alter table runs add column workdir text")
        if "workdir" not in columns["jobs"]:
            conn.execute("alter table jobs add column workdir text")
        if "attachments" not in columns["runs"]:
            conn.execute("alter table runs add column attachments text")
        if "attachments" not in columns["jobs"]:
            conn.execute("alter table jobs add column attachments text")
        for table in ("runs", "jobs"):
            table_columns = columns[table]
            if "model" not in table_columns:
                conn.execute(f"alter table {table} add column model text")
            if "permission_mode" not in table_columns:
                conn.execute(f"alter table {table} add column permission_mode text")
            if "allowed_tools" not in table_columns:
                conn.execute(f"alter table {table} add column allowed_tools text")


def add_run_event(run_id: str, kind: str, title: str = "", body: str = "", raw: object | None = None) -> None:
    raw_text = ""
    if raw is not None:
        try:
            raw_text = json.dumps(raw, ensure_ascii=False)
        except TypeError:
            raw_text = str(raw)
    with connect() as conn:
        conn.execute(
            """
            insert into run_events (run_id, kind, title, body, raw, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (run_id, kind, title, body, raw_text, now_iso()),
        )


def event_from_stream_payload(payload: dict) -> tuple[str, str, str, str]:
    event_type = str(payload.get("type") or payload.get("event") or "event")
    title = event_type
    body = ""
    text_parts: list[str] = []

    for key in ("delta", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            text_parts.append(value)
        elif isinstance(value, dict):
            nested = value.get("text") or value.get("content")
            if isinstance(nested, str):
                text_parts.append(nested)

    message = payload.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])

    def collect_text(value: object) -> None:
        if isinstance(value, str):
            text_parts.append(value)
        elif isinstance(value, dict):
            for key in ("text", "content", "message", "summary", "name", "input"):
                item = value.get(key)
                if isinstance(item, str):
                    text_parts.append(item)
            for item in value.values():
                if isinstance(item, (dict, list)):
                    collect_text(item)
        elif isinstance(value, list):
            for item in value:
                collect_text(item)

    if not text_parts:
        collect_text(payload)
    if text_parts:
        body = "\n".join(part for part in text_parts if part).strip()

    lower = event_type.lower()
    if "tool" in lower:
        kind = "tool"
    elif "permission" in lower or "approval" in lower:
        kind = "permission"
    elif "error" in lower:
        kind = "error"
    elif body:
        kind = "message"
    else:
        kind = "event"
    return kind, title, body, event_type


def parse_claude_ts(value: object, fallback: float) -> float:
    if isinstance(value, (int, float)):
        # Claude Desktop stores a millisecond timestamp in its local metadata.
        return float(value) / 1000
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp()
        except ValueError:
            return fallback
    return fallback


def short_path(path: str) -> str:
    if not path:
        return ""
    home = str(Path.home())
    if path.lower().startswith(home.lower()):
        return "~" + path[len(home) :]
    return path


def repair_mojibake(value: object) -> object:
    if not isinstance(value, str):
        return value
    if not any(marker in value for marker in ("Ă", "Ĺ", "Â")):
        return value
    try:
        return value.encode("cp1250").decode("utf-8")
    except UnicodeError:
        return value


def clean_api_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("message", "summary", "error"):
        if key in item:
            item[key] = repair_mojibake(item[key])
    if item.get("attachments"):
        try:
            item["attachments"] = json.loads(item["attachments"])
        except json.JSONDecodeError:
            item["attachments"] = []
    else:
        item["attachments"] = []
    return item


def normalize_attachments(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    attachments = []
    for item in value[:6]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        name = str(item.get("name") or Path(path).name).strip()
        if not path:
            continue
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(UPLOADS_DIR.resolve())
        except ValueError:
            continue
        if resolved.exists():
            mime = str(item.get("mime") or mimetypes.guess_type(name or resolved.name)[0] or "application/octet-stream")
            attachment = {
                "name": name or resolved.name,
                "path": str(resolved),
                "mime": mime,
                "kind": "image" if mime.startswith("image/") else "file",
            }
            data_url = str(item.get("data_url") or "")
            if attachment["kind"] == "image" and data_url.startswith("data:image/") and len(data_url) <= 7_000_000:
                attachment["data_url"] = data_url
            attachments.append(attachment)
    return attachments


def save_upload(payload: dict) -> dict:
    raw_name = str(payload.get("name") or "attachment").strip()
    encoded = str(payload.get("data") or "")
    if "," in encoded:
        encoded = encoded.split(",", 1)[1]
    data = base64.b64decode(encoded, validate=True)
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("A fájl túl nagy. Maximum 25 MB.")
    UPLOADS_DIR.mkdir(exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in Path(raw_name).name)
    target = UPLOADS_DIR / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{safe_name}"
    target.write_bytes(data)
    mime = mimetypes.guess_type(raw_name)[0] or "application/octet-stream"
    attachment = {
        "name": raw_name,
        "path": str(target.resolve()),
        "mime": mime,
        "kind": "image" if mime.startswith("image/") else "file",
    }
    if attachment["kind"] == "image" and len(data) <= 5 * 1024 * 1024:
        attachment["data_url"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return attachment


def normalize_title(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "Névtelen Claude session"
    return text if len(text) <= 96 else text[:93] + "..."


def clean_session_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def clean_tool_list(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return ""


def session_options_from_mapping(data: object) -> dict:
    if not isinstance(data, dict):
        return {"model": "", "permission_mode": "", "allowed_tools": ""}
    return {
        "model": clean_session_value(data.get("model") or data.get("modelId") or data.get("activeModel")),
        "permission_mode": clean_session_value(
            data.get("permissionMode")
            or data.get("permission_mode")
            or data.get("permission-mode")
            or data.get("activePermissionMode")
        ),
        "allowed_tools": clean_tool_list(data.get("allowedTools") or data.get("allowed_tools")),
    }


def merge_session_options(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key in ("model", "permission_mode", "allowed_tools"):
        if not merged.get(key) and extra.get(key):
            merged[key] = extra[key]
    return merged


def claude_project_state(cwd: str) -> dict:
    if not cwd:
        return {}
    state_path = Path.home() / ".claude.json"
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return {}
    candidates = [
        cwd,
        cwd.replace("\\", "/"),
        cwd.replace("/", "\\"),
    ]
    for key in candidates:
        value = projects.get(key)
        if isinstance(value, dict):
            return value
    return {}


def session_defaults_from_jsonl(path: Path) -> dict:
    defaults = {"model": "", "permission_mode": "", "allowed_tools": "", "cwd": ""}
    try:
        recent_lines: deque[str] = deque(maxlen=800)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                recent_lines.append(line)
    except OSError:
        return defaults

    for line in recent_lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("cwd"):
            defaults["cwd"] = clean_session_value(item.get("cwd"))
        defaults = merge_session_options(defaults, session_options_from_mapping(item))
        message = item.get("message")
        if isinstance(message, dict) and message.get("model"):
            defaults["model"] = clean_session_value(message.get("model"))
    return defaults


def session_from_desktop_metadata(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    cli_session_id = str(data.get("cliSessionId") or "").strip()
    if not cli_session_id:
        return None

    cwd = str(data.get("cwd") or data.get("originCwd") or "").strip()
    updated = parse_claude_ts(data.get("lastActivityAt") or data.get("lastFocusedAt") or data.get("createdAt"), path.stat().st_mtime)
    title = normalize_title(data.get("title"))
    defaults = session_options_from_mapping(data)
    defaults = merge_session_options(defaults, session_options_from_mapping(claude_project_state(cwd)))
    archived = bool(data.get("isArchived"))
    return {
        "id": cli_session_id,
        "title": title,
        "cwd": cwd,
        "cwd_short": short_path(cwd),
        "updated_at": dt.datetime.fromtimestamp(updated).replace(microsecond=0).isoformat(),
        "updated_sort": updated,
        "source": "Claude Desktop",
        "model": defaults["model"],
        "permission_mode": defaults["permission_mode"],
        "allowed_tools": defaults["allowed_tools"],
        "archived": archived,
    }


def first_user_title_from_jsonl(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index > 200:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = item.get("message") if isinstance(item, dict) else None
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    return normalize_title(content)
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            parts.append(str(part.get("text") or ""))
                    if parts:
                        return normalize_title(" ".join(parts))
    except OSError:
        return ""
    return ""


def session_from_project_jsonl(path: Path) -> dict | None:
    if path.suffix.lower() != ".jsonl":
        return None
    session_id = path.stem
    try:
        uuid.UUID(session_id)
    except ValueError:
        return None
    project_name = path.parent.name.replace("--", ":\\").replace("-", "\\")
    title = first_user_title_from_jsonl(path) or project_name or "Claude CLI session"
    updated = path.stat().st_mtime
    defaults = session_defaults_from_jsonl(path)
    cwd = defaults.get("cwd") or ""
    defaults = merge_session_options(defaults, session_options_from_mapping(claude_project_state(cwd)))
    return {
        "id": session_id,
        "title": normalize_title(title),
        "cwd": cwd,
        "cwd_short": short_path(cwd) if cwd else project_name,
        "updated_at": dt.datetime.fromtimestamp(updated).replace(microsecond=0).isoformat(),
        "updated_sort": updated,
        "source": "Claude CLI",
        "model": defaults["model"],
        "permission_mode": defaults["permission_mode"],
        "allowed_tools": defaults["allowed_tools"],
        "archived": False,
    }


def find_project_jsonl(session_id: str) -> Path | None:
    try:
        uuid.UUID(session_id)
    except ValueError:
        return None
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.exists():
        return None
    matches = list(projects_root.rglob(f"{session_id}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def message_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text") or ""))
    return "\n".join(part.strip() for part in parts if part.strip())


def message_attachments(content: object) -> list[dict]:
    if not isinstance(content, list):
        return []
    attachments = []
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image":
            source = part.get("source")
            if isinstance(source, dict) and source.get("type") == "base64":
                media_type = str(source.get("media_type") or "image/png")
                data = str(source.get("data") or "")
                if data:
                    attachments.append(
                        {
                            "name": f"Claude kép {index + 1}",
                            "kind": "image",
                            "mime": media_type,
                            "data_url": f"data:{media_type};base64,{data}",
                        }
                    )
        elif part.get("type") not in ("text", "tool_use", "tool_result", "thinking"):
            attachments.append(
                {
                    "name": str(part.get("name") or part.get("type") or f"Csatolmány {index + 1}"),
                    "kind": "file",
                    "mime": str(part.get("media_type") or ""),
                }
            )
    return attachments


def read_claude_thread(session_id: str, limit: int = 40) -> list[dict]:
    path = find_project_jsonl(session_id)
    if not path:
        return []
    messages = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = item.get("message") if isinstance(item, dict) else None
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = message.get("content")
                text = message_text(content)
                attachments = message_attachments(content)
                if not text and not attachments:
                    continue
                messages.append(
                    {
                        "role": role,
                        "text": repair_mojibake(text),
                        "attachments": attachments,
                        "timestamp": item.get("timestamp") or "",
                        "source": "claude",
                    }
                )
    except OSError:
        return []
    return messages[-limit:]


def list_claude_sessions(limit: int = 80) -> list[dict]:
    sessions: dict[str, dict] = {}

    desktop_root = appdata_claude_dir() / "claude-code-sessions"
    if desktop_root.exists():
        for path in desktop_root.rglob("*.json"):
            item = session_from_desktop_metadata(path)
            if item and not item["archived"]:
                current = sessions.get(item["id"])
                if not current or item["updated_sort"] > current["updated_sort"]:
                    sessions[item["id"]] = item

    projects_root = Path.home() / ".claude" / "projects"
    if projects_root.exists():
        for path in projects_root.rglob("*.jsonl"):
            item = session_from_project_jsonl(path)
            if item and item["id"] not in sessions:
                sessions[item["id"]] = item

    ordered = sorted(sessions.values(), key=lambda item: item["updated_sort"], reverse=True)
    for item in ordered:
        item.pop("updated_sort", None)
        item.pop("archived", None)
    return ordered[:limit]


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def normalize_session_id(value: str) -> str | None:
    value = value.strip()
    return value or None


def parse_local_datetime(value: str) -> dt.datetime:
    value = value.strip()
    if not value:
        raise ValueError("Hiányzik az időpont.")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(second=0, microsecond=0)


def normalize_model(value: object) -> str | None:
    model = str(value or "").strip()
    return model or None


def normalize_permission_mode(value: object) -> str | None:
    mode = str(value or "").strip()
    allowed = {"default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan"}
    return mode if mode in allowed else None


def normalize_allowed_tools(value: object) -> str | None:
    tools = str(value or "").strip()
    return tools or None


def run_claude(run_id: str) -> None:
    with connect() as conn:
        row = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        if not row:
            return
        conn.execute(
            "update runs set status = ?, started_at = ? where id = ?",
            ("running", now_iso(), run_id),
        )

    attachments = normalize_attachments(json.loads(row["attachments"] or "[]"))
    command = [CONFIG.get("claude_command", "claude")]
    if attachments:
        command.extend(["--add-dir", str(UPLOADS_DIR.resolve())])
    if row["model"]:
        command.extend(["--model", row["model"]])
    if row["permission_mode"]:
        command.extend(["--permission-mode", row["permission_mode"]])
    if row["allowed_tools"]:
        command.extend(["--allowedTools", row["allowed_tools"]])
    command.extend(["-p", "--output-format", "stream-json", "--include-partial-messages", "--include-hook-events"])
    if row["session_id"]:
        command.extend(["--resume", row["session_id"]])
    message = row["message"]
    if attachments:
        files = "\n".join(f"- {item['name']}: {item['path']}" for item in attachments)
        message = f"{message}\n\nCsatolt képek/fájlok:\n{files}"
    command.append(message)
    workdir = row["workdir"] or CONFIG.get("default_workdir", str(ROOT))
    if not Path(workdir).exists():
        workdir = CONFIG.get("default_workdir", str(ROOT))

    summary_parts: list[str] = []
    error_parts: list[str] = []
    status = "done"
    add_run_event(run_id, "started", "Claude futás indult", " ".join(command[:8]))
    try:
        process = subprocess.Popen(
            command,
            cwd=workdir,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with RUN_PROCESSES_LOCK:
            RUN_PROCESSES[run_id] = process
        assert process.stdout is not None
        start = dt.datetime.now()
        for line in process.stdout:
            if (dt.datetime.now() - start).total_seconds() > CLAUDE_TIMEOUT_SECONDS:
                process.kill()
                raise TimeoutError("Claude futás időtúllépés miatt leállítva.")
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                kind, title, body, event_type = event_from_stream_payload(payload)
                add_run_event(run_id, kind, title, body[:4000], payload)
                if kind == "message" and body:
                    summary_parts.append(body)
                    with connect() as conn:
                        conn.execute("update runs set summary = ? where id = ?", ("\n".join(summary_parts).strip(), run_id))
            except json.JSONDecodeError:
                summary_parts.append(line)
                add_run_event(run_id, "message", "stdout", line)
                with connect() as conn:
                    conn.execute("update runs set summary = ? where id = ?", ("\n".join(summary_parts).strip(), run_id))
        stderr = ""
        if process.stderr is not None:
            stderr = process.stderr.read().strip()
        return_code = process.wait()
        with RUN_PROCESSES_LOCK:
            RUN_PROCESSES.pop(run_id, None)
        if stderr:
            error_parts.append(stderr)
            add_run_event(run_id, "stderr", "Claude stderr", stderr[:4000])
        status = "done" if return_code == 0 else "failed"
        if return_code != 0 and not error_parts:
            error_parts.append(f"Claude CLI kilépési kód: {return_code}")
    except Exception as exc:
        with RUN_PROCESSES_LOCK:
            RUN_PROCESSES.pop(run_id, None)
        error_parts.append(str(exc))
        add_run_event(run_id, "error", "Futási hiba", str(exc))
        status = "failed"

    summary = "\n".join(summary_parts).strip()
    error = "\n".join(error_parts).strip()

    with connect() as conn:
        conn.execute(
            """
            update runs
               set status = ?, summary = ?, error = ?, finished_at = ?
             where id = ?
            """,
            (status, summary, error, now_iso(), run_id),
        )


def create_run(
    kind: str,
    message: str,
    session_id: str | None,
    workdir: str | None = None,
    attachments: list[dict] | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    allowed_tools: str | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            insert into runs (id, kind, session_id, workdir, attachments, model, permission_mode, allowed_tools, message, status, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                kind,
                session_id,
                workdir,
                json.dumps(normalize_attachments(attachments or []), ensure_ascii=False),
                normalize_model(model),
                normalize_permission_mode(permission_mode),
                normalize_allowed_tools(allowed_tools),
                message,
                "queued",
                now_iso(),
            ),
        )
    threading.Thread(target=run_claude, args=(run_id,), daemon=True).start()
    return run_id


def scheduler_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        current = dt.datetime.now().replace(second=0, microsecond=0).isoformat()
        with connect() as conn:
            jobs = conn.execute(
                """
                select * from jobs
                 where status = 'scheduled'
                   and run_at <= ?
                 order by run_at asc
                """,
                (current,),
            ).fetchall()

        for job in jobs:
            run_id = create_run(
                "scheduled",
                job["message"],
                job["session_id"],
                job["workdir"],
                json.loads(job["attachments"] or "[]"),
                job["model"],
                job["permission_mode"],
                job["allowed_tools"],
            )
            with connect() as conn:
                conn.execute(
                    """
                    update jobs
                       set status = ?, run_id = ?, executed_at = ?
                     where id = ?
                    """,
                    ("sent", run_id, now_iso(), job["id"]),
                )
        stop_event.wait(10)


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    security_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, body: str, status: int = 200, content_type: str = "text/html") -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    security_headers(handler)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def security_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Security-Policy", "default-src 'self' data: blob:; img-src 'self' data: blob:; style-src 'unsafe-inline' 'self'; script-src 'unsafe-inline' 'self'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeBridge/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{now_iso()}] {self.address_string()} {fmt % args}")

    def token_from_request(self) -> str:
        parsed = urlparse(self.path)
        query_token = parse_qs(parsed.query).get("token", [""])[0]
        header_token = self.headers.get("X-Claude-Bridge-Token", "")
        return query_token or header_token

    def authorized(self) -> bool:
        return secrets.compare_digest(self.token_from_request(), CONFIG["token"])

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        json_response(self, {"ok": False, "error": "Érvénytelen vagy hiányzó token."}, HTTPStatus.UNAUTHORIZED)
        return False

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            text_response(self, render_index_v2(self.authorized(), CONFIG["token"]))
            return
        if not self.require_auth():
            return
        if parsed.path == "/api/state":
            with connect() as conn:
                runs = [clean_api_row(row) for row in conn.execute("select * from runs order by created_at desc limit 25")]
                jobs = [clean_api_row(row) for row in conn.execute("select * from jobs order by run_at asc limit 50")]
                run_ids = [row["id"] for row in runs]
                events: list[dict] = []
                if run_ids:
                    placeholders = ",".join("?" for _ in run_ids)
                    events = [
                        dict(row)
                        for row in conn.execute(
                            f"select * from run_events where run_id in ({placeholders}) order by id asc limit 300",
                            run_ids,
                        )
                    ]
            json_response(self, {"ok": True, "runs": runs, "jobs": jobs, "events": events})
            return
        if parsed.path == "/api/sessions":
            json_response(self, {"ok": True, "sessions": list_claude_sessions()})
            return
        if parsed.path == "/api/thread":
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            json_response(self, {"ok": True, "messages": read_claude_thread(session_id)})
            return
        text_response(self, "Not found", HTTPStatus.NOT_FOUND, "text/plain")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self.require_auth():
            return
        try:
            payload = self.read_json()
            if parsed.path == "/api/upload":
                json_response(self, {"ok": True, "attachment": save_upload(payload)})
                return

            if parsed.path == "/api/cancel":
                job_id = str(payload.get("job_id", "")).strip()
                if not job_id:
                    raise ValueError("Hiányzik az időzítés azonosítója.")
                with connect() as conn:
                    cursor = conn.execute("update jobs set status = 'cancelled' where id = ? and status = 'scheduled'", (job_id,))
                json_response(self, {"ok": True, "cancelled": cursor.rowcount})
                return

            if parsed.path == "/api/stop":
                run_id = str(payload.get("run_id", "")).strip()
                if not run_id:
                    raise ValueError("Hiányzik a futás azonosítója.")
                stopped = False
                with RUN_PROCESSES_LOCK:
                    process = RUN_PROCESSES.get(run_id)
                if process and process.poll() is None:
                    process.terminate()
                    stopped = True
                with connect() as conn:
                    conn.execute(
                        "update runs set status = ?, error = ?, finished_at = ? where id = ? and status in ('queued', 'running')",
                        ("stopped", "Felhasználó megszakította.", now_iso(), run_id),
                    )
                add_run_event(run_id, "stopped", "Futás megszakítva", "Felhasználó megszakította a futást.")
                json_response(self, {"ok": True, "stopped": stopped})
                return

            message = str(payload.get("message", "")).strip()
            session_id = normalize_session_id(str(payload.get("session_id", "")))
            workdir = str(payload.get("workdir", "")).strip() or None
            attachments = normalize_attachments(payload.get("attachments"))
            model = normalize_model(payload.get("model"))
            permission_mode = normalize_permission_mode(payload.get("permission_mode"))
            allowed_tools = normalize_allowed_tools(payload.get("allowed_tools"))
            if not message:
                raise ValueError("Az üzenet nem lehet üres.")

            if parsed.path == "/api/send":
                run_id = create_run("manual", message, session_id, workdir, attachments, model, permission_mode, allowed_tools)
                json_response(self, {"ok": True, "run_id": run_id})
                return

            if parsed.path == "/api/schedule":
                run_at = parse_local_datetime(str(payload.get("run_at", "")))
                job_id = str(uuid.uuid4())
                with connect() as conn:
                    conn.execute(
                        """
                        insert into jobs (id, run_at, session_id, workdir, attachments, model, permission_mode, allowed_tools, message, status, created_at)
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            run_at.isoformat(),
                            session_id,
                            workdir,
                            json.dumps(attachments, ensure_ascii=False),
                            model,
                            permission_mode,
                            allowed_tools,
                            message,
                            "scheduled",
                            now_iso(),
                        ),
                    )
                json_response(self, {"ok": True, "job_id": job_id})
                return

            text_response(self, "Not found", HTTPStatus.NOT_FOUND, "text/plain")
        except Exception as exc:
            json_response(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)


def render_index(is_authorized: bool, token: str) -> str:
    safe_token = ""
    locked = "" if is_authorized else "locked"
    return f"""<!doctype html>
<html lang="hu">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Claude Bridge</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f3ea;
      --panel: #fffaf2;
      --text: #2b2118;
      --muted: #7a6d60;
      --line: #e2d7c8;
      --soft: #f2eadf;
      --accent: #c15f3c;
      --accent-2: #7b5f45;
      --danger: #a64235;
      --ok: #5f7d4b;
      --shadow: 0 12px 30px rgba(88, 61, 38, .08);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #191512;
        --panel: #211b17;
        --text: #f5eee4;
        --muted: #baaa9a;
        --line: #3b3029;
        --soft: #2a231e;
        --accent: #d87855;
        --accent-2: #c9ab8b;
        --danger: #e07a6f;
        --ok: #9fbd84;
        --shadow: none;
      }}
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      height: 100%;
      overflow: hidden;
    }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    main {{
      width: min(1060px, 100%);
      margin: 0 auto;
      padding: 16px;
      display: grid;
      gap: 16px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 8px 0 2px;
    }}
    h1 {{ font-size: 26px; margin: 0; line-height: 1.15; letter-spacing: 0; }}
    h2 {{ font-size: 16px; margin: 0; }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 16px;
      box-shadow: var(--shadow);
    }}
    .section-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }}
    label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 10px;
    }}
    input, textarea, button {{
      width: 100%;
      font: inherit;
      border-radius: 7px;
    }}
    input, textarea {{
      border: 1px solid var(--line);
      background: var(--soft);
      color: var(--text);
      padding: 10px;
    }}
    select {{
      width: 100%;
      font: inherit;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--soft);
      color: var(--text);
      padding: 10px;
    }}
    textarea {{ min-height: 140px; resize: vertical; line-height: 1.45; }}
    button {{
      border: 0;
      background: var(--accent);
      color: #fffaf2;
      min-height: 42px;
      padding: 10px 12px;
      cursor: pointer;
      font-weight: 650;
    }}
    button:disabled {{
      opacity: .62;
      cursor: progress;
    }}
    button.secondary {{
      background: transparent;
      color: var(--text);
      border: 1px solid var(--line);
    }}
    button.compact {{
      width: auto;
      min-height: 34px;
      padding: 7px 10px;
      font-size: 13px;
    }}
    .grid {{ display: grid; gap: 16px; grid-template-columns: minmax(0, 1.05fr) minmax(320px, .95fr); align-items: start; }}
    .stack {{ display: grid; gap: 16px; }}
    .row {{ display: flex; gap: 10px; }}
    .status {{ color: var(--muted); font-size: 13px; }}
    .top-status {{
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 12px;
      min-width: 0;
    }}
    .metric b {{
      display: block;
      font-size: 13px;
      margin-bottom: 5px;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      word-break: break-word;
    }}
    .item {{
      border-top: 1px solid var(--line);
      padding: 12px 0;
      display: grid;
      gap: 6px;
    }}
    .item:first-child {{ border-top: 0; }}
    .badge {{
      display: inline-flex;
      width: fit-content;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .badge.done {{ border-color: var(--ok); color: var(--ok); }}
    .badge.running, .badge.queued {{ border-color: var(--accent-2); color: var(--accent-2); }}
    .badge.failed {{ border-color: var(--danger); color: var(--danger); }}
    .badge.stopped {{ border-color: var(--muted); color: var(--muted); }}
    .event-timeline {{
      display: grid;
      gap: 6px;
      border-left: 2px solid var(--line);
      padding-left: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .event-row {{
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 7px 9px;
      background: color-mix(in srgb, var(--soft), transparent 22%);
      overflow-wrap: anywhere;
    }}
    .event-row.tool {{ border-color: color-mix(in srgb, var(--accent-2), var(--line) 50%); }}
    .event-row.permission {{ border-color: color-mix(in srgb, var(--danger), var(--line) 44%); }}
    .event-title {{ color: var(--text); font-weight: 650; margin-bottom: 3px; }}
    .feature-panel {{
      width: min(820px, 100%);
      margin: 0 auto 16px;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
      background: var(--panel);
      display: none;
      gap: 8px;
    }}
    .feature-panel.open {{ display: grid; }}
    .feature-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }}
    .feature-grid div {{
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px;
      color: var(--muted);
      background: var(--soft);
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      color: var(--text);
    }}
    .message {{
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
    }}
    .answer {{
      border-left: 3px solid var(--accent);
      padding-left: 10px;
    }}
    .toast {{
      min-height: 20px;
      color: var(--muted);
      font-size: 13px;
      margin-top: 10px;
    }}
    .error {{ color: var(--danger); }}
    .auth {{
      display: { "none" if is_authorized else "block" };
    }}
    .app.{locked} {{
      opacity: .45;
      pointer-events: none;
    }}
    @media (max-width: 760px) {{
      main {{ padding: 12px; }}
      .grid {{ grid-template-columns: 1fr; }}
      .top-status {{ grid-template-columns: 1fr; }}
      header {{ align-items: flex-start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Claude Bridge</h1>
        <div class="status">Helyi webes vezérlő Claude CLI sessionökhöz</div>
      </div>
      <button class="secondary compact" onclick="refresh()">Frissítés</button>
    </header>

    <section class="auth">
      <h2>Belépés</h2>
      <label>Token
        <input id="token" value="{safe_token}" autocomplete="off">
      </label>
      <button onclick="saveToken()">Megnyitás tokennel</button>
    </section>

    <div class="app {locked}">
      <div class="top-status">
        <div class="metric">
          <b>Claude futás</b>
          <span id="runCount">Nincs adat</span>
        </div>
        <div class="metric">
          <b>Következő időzítés</b>
          <span id="nextJob">Nincs időzítés</span>
        </div>
      </div>

      <div class="grid">
        <div class="stack">
          <section>
            <div class="section-head">
              <h2>Azonnali üzenet</h2>
              <span class="status">Claude CLI</span>
            </div>
            <label>Claude session
              <select id="sendSessionSelect"></select>
            </label>
            <label>Üzenet
              <textarea id="sendMessage" placeholder="Mit küldjek Claude-nak?"></textarea>
            </label>
            <button id="sendButton" onclick="sendNow()">Küldés Claude-nak</button>
            <div id="sendToast" class="toast"></div>
          </section>

          <section>
            <div class="section-head">
              <h2>Időzített üzenet</h2>
              <span class="status">perc pontosság</span>
            </div>
            <label>Időpont
              <input id="runAt" type="datetime-local">
            </label>
            <label>Claude session
              <select id="scheduleSessionSelect"></select>
            </label>
            <label>Üzenet
              <textarea id="scheduleMessage" placeholder="Ezt küldöm a megadott időpontban."></textarea>
            </label>
            <button id="scheduleButton" onclick="schedule()">Időzítés mentése</button>
            <div id="scheduleToast" class="toast"></div>
          </section>
        </div>

        <div class="stack">
          <section>
            <div class="section-head">
              <h2>Futások</h2>
              <button class="secondary compact" onclick="refresh()">Újratöltés</button>
            </div>
            <div id="runs" class="status">Betöltés...</div>
          </section>

          <section>
            <div class="section-head">
              <h2>Időzítések</h2>
              <span class="status">várakozó feladatok</span>
            </div>
            <div id="jobs" class="status">Betöltés...</div>
          </section>
        </div>
      </div>
    </div>
  </main>

  <script>
    const params = new URLSearchParams(location.search);
    let token = params.get("token") || localStorage.getItem("claudeBridgeToken") || "";
    if (params.get("token")) {{
      localStorage.setItem("claudeBridgeToken", params.get("token"));
      history.replaceState(null, "", location.pathname);
    }}
    let sessions = [];
    document.getElementById("token").value = token || "{safe_token}";

    function unlockIfToken() {{
      if (!token) return;
      document.querySelector(".auth").style.display = "none";
      document.querySelector(".app").classList.remove("locked");
    }}

    function api(path, options = {{}}) {{
      return fetch(path, {{
        ...options,
        headers: {{
          "Content-Type": "application/json",
          "X-Claude-Bridge-Token": token,
          ...(options.headers || {{}})
        }}
      }}).then(async response => {{
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "Hiba történt.");
        return data;
      }});
    }}

    function saveToken() {{
      token = document.getElementById("token").value.trim();
      localStorage.setItem("claudeBridgeToken", token);
      unlockIfToken();
      loadSessions();
      refresh();
    }}

    function escapeHtml(value) {{
      return String(value || "").replace(/[&<>"']/g, char => ({{
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }}[char]));
    }}

    function optionLabel(session) {{
      const updated = session.updated_at ? session.updated_at.replace("T", " ") : "";
      const cwd = session.cwd_short ? ` · ${{session.cwd_short}}` : "";
      return `${{session.title}} · ${{updated}}${{cwd}}`;
    }}

    function fillSessionSelect(selectId, selectedId = "") {{
      const select = document.getElementById(selectId);
      select.innerHTML = [
        `<option value="">Új / nincs kiválasztott session</option>`,
        ...sessions.map(session => `
          <option value="${{escapeHtml(session.id)}}"
                  data-cwd="${{escapeHtml(session.cwd || "")}}"
                  ${{session.id === selectedId ? "selected" : ""}}>
            ${{escapeHtml(optionLabel(session))}}
          </option>
        `)
      ].join("");
    }}

    async function loadSessions() {{
      if (!token) return;
      try {{
        const data = await api("/api/sessions");
        sessions = data.sessions || [];
        fillSessionSelect("sendSessionSelect");
        fillSessionSelect("scheduleSessionSelect");
      }} catch (error) {{
        sessions = [];
        fillSessionSelect("sendSessionSelect");
        fillSessionSelect("scheduleSessionSelect");
      }}
    }}

    function selectedSession(selectId) {{
      const select = document.getElementById(selectId);
      const option = select.options[select.selectedIndex];
      return {{
        session_id: select.value,
        workdir: option ? option.dataset.cwd || "" : ""
      }};
    }}

    async function sendNow() {{
      const button = document.getElementById("sendButton");
      const toast = document.getElementById("sendToast");
      button.disabled = true;
      toast.textContent = "Küldés folyamatban...";
      try {{
        const session = selectedSession("sendSessionSelect");
        await api("/api/send", {{
          method: "POST",
          body: JSON.stringify({{
            session_id: session.session_id,
            workdir: session.workdir,
            message: document.getElementById("sendMessage").value
          }})
        }});
        document.getElementById("sendMessage").value = "";
        toast.textContent = "Elküldve. A válasz hamarosan megjelenik a futásoknál.";
        refresh();
      }} catch (error) {{
        toast.innerHTML = `<span class="error">${{escapeHtml(error.message)}}</span>`;
      }} finally {{
        button.disabled = false;
      }}
    }}

    async function schedule() {{
      const button = document.getElementById("scheduleButton");
      const toast = document.getElementById("scheduleToast");
      button.disabled = true;
      toast.textContent = "Időzítés mentése...";
      try {{
        const session = selectedSession("scheduleSessionSelect");
        await api("/api/schedule", {{
          method: "POST",
          body: JSON.stringify({{
            run_at: document.getElementById("runAt").value,
            session_id: session.session_id,
            workdir: session.workdir,
            message: document.getElementById("scheduleMessage").value
          }})
        }});
        document.getElementById("scheduleMessage").value = "";
        toast.textContent = "Időzítve.";
        refresh();
      }} catch (error) {{
        toast.innerHTML = `<span class="error">${{escapeHtml(error.message)}}</span>`;
      }} finally {{
        button.disabled = false;
      }}
    }}

    async function cancelJob(jobId) {{
      await api("/api/cancel", {{
        method: "POST",
        body: JSON.stringify({{ job_id: jobId }})
      }});
      refresh();
    }}

    async function refresh() {{
      if (!token) return;
      try {{
        const data = await api("/api/state");
        document.getElementById("runCount").textContent = data.runs.length
          ? `${{data.runs.length}} legutóbbi futás betöltve`
          : "Még nincs futás";
        const nextScheduled = data.jobs.find(job => job.status === "scheduled");
        document.getElementById("nextJob").textContent = nextScheduled
          ? nextScheduled.run_at
          : "Nincs időzítés";
        document.getElementById("runs").innerHTML = data.runs.length ? data.runs.map(run => `
          <div class="item">
            <span class="badge ${{escapeHtml(run.status)}}">${{escapeHtml(run.status)}} · ${{escapeHtml(run.kind)}} · ${{escapeHtml(run.created_at)}}</span>
            <pre class="message">${{escapeHtml(run.message)}}</pre>
            ${{run.summary ? `<pre class="answer">${{escapeHtml(run.summary)}}</pre>` : ""}}
            ${{run.error ? `<pre class="error">${{escapeHtml(run.error)}}</pre>` : ""}}
          </div>
        `).join("") : "Még nincs futás.";
        document.getElementById("jobs").innerHTML = data.jobs.length ? data.jobs.map(job => `
          <div class="item">
            <span class="badge">${{escapeHtml(job.status)}} · ${{escapeHtml(job.run_at)}}</span>
            <pre>${{escapeHtml(job.message)}}</pre>
            ${{job.status === "scheduled" ? `<button class="secondary" onclick="cancelJob('${{job.id}}')">Törlés</button>` : ""}}
          </div>
        `).join("") : "Még nincs időzítés.";
      }} catch (error) {{
        document.getElementById("runs").innerHTML = `<span class="error">${{escapeHtml(error.message)}}</span>`;
      }}
    }}

    const later = new Date(Date.now() + 15 * 60 * 1000);
    later.setSeconds(0, 0);
    setScheduleDateTime(later);
    unlockIfToken();
    loadSessions();
    refresh();
    setInterval(refresh, 4000);
    setInterval(loadSessions, 30000);
  </script>
</body>
</html>"""


def render_index_v2(is_authorized: bool, token: str) -> str:
    safe_token = ""
    locked = "" if is_authorized else "locked"
    return f"""<!doctype html>
<html lang="hu">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Claude Bridge</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f3ea;
      --panel: #fffaf2;
      --text: #2b2118;
      --muted: #7a6d60;
      --line: #e2d7c8;
      --soft: #f2eadf;
      --accent: #c15f3c;
      --accent-2: #7b5f45;
      --danger: #a64235;
      --ok: #5f7d4b;
      --shadow: 0 12px 30px rgba(88, 61, 38, .08);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #191512;
        --panel: #211b17;
        --text: #f5eee4;
        --muted: #baaa9a;
        --line: #3b3029;
        --soft: #2a231e;
        --accent: #d87855;
        --accent-2: #c9ab8b;
        --danger: #e07a6f;
        --ok: #9fbd84;
        --shadow: none;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    button, input, textarea, select {{ font: inherit; }}
    button {{ cursor: pointer; }}
    input, select, textarea, button {{ min-width: 0; }}
    .auth {{
      display: { "none" if is_authorized else "grid" };
      min-height: 100vh;
      place-items: center;
      padding: 20px;
    }}
    .auth-card {{
      width: min(420px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 18px;
      box-shadow: var(--shadow);
    }}
    .auth-card h1 {{ margin: 0 0 6px; font-size: 24px; }}
    .auth-card p {{ margin: 0 0 14px; color: var(--muted); }}
    .auth-card .primary {{ margin-top: 12px; }}
    .app.{locked} {{ display: none; }}
    .app {{
      height: 100vh;
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
      overflow: hidden;
    }}
    .sidebar {{
      border-right: 1px solid var(--line);
      background: color-mix(in srgb, var(--panel), var(--bg) 34%);
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr);
    }}
    .brand {{ padding: 18px 16px 12px; }}
    .brand h1 {{ margin: 0; font-size: 22px; line-height: 1.1; letter-spacing: 0; }}
    .brand span {{ display: block; margin-top: 5px; color: var(--muted); font-size: 13px; }}
    .sidebar-tools {{ display: flex; gap: 8px; padding: 0 12px 12px; }}
    .sessions {{ min-height: 0; overflow: auto; padding: 2px 8px 12px; }}
    .session-group {{
      display: grid;
      gap: 2px;
      margin-bottom: 8px;
    }}
    .folder-btn {{
      width: 100%;
      border: 0;
      background: transparent;
      color: var(--text);
      text-align: left;
      border-radius: 8px;
      padding: 8px;
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr) auto;
      gap: 7px;
      align-items: center;
      font-weight: 700;
    }}
    .folder-btn:hover {{ background: var(--soft); }}
    .folder-chevron {{
      color: var(--muted);
      transition: transform .15s ease;
    }}
    .session-group.collapsed .folder-chevron {{ transform: rotate(-90deg); }}
    .folder-title {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 13px;
    }}
    .folder-count {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
    }}
    .session-group-items {{
      display: grid;
      gap: 2px;
      padding-left: 12px;
    }}
    .session-group.collapsed .session-group-items {{ display: none; }}
    .session-btn {{
      width: 100%;
      border: 0;
      background: transparent;
      color: var(--text);
      text-align: left;
      border-radius: 8px;
      padding: 7px 8px;
      display: grid;
      gap: 2px;
    }}
    .session-btn:hover, .session-btn.active {{ background: var(--soft); }}
    .session-title {{
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 13px;
      font-weight: 650;
    }}
    .session-meta {{
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--muted);
      font-size: 11px;
    }}
    .main {{
      min-width: 0;
      min-height: 0;
      height: 100vh;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
    }}
    .topbar {{
      border-bottom: 1px solid var(--line);
      padding: 14px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      background: color-mix(in srgb, var(--bg), var(--panel) 28%);
    }}
    .topbar h2 {{ margin: 0; font-size: 16px; line-height: 1.2; }}
    .refresh-button.loading::before {{
      content: "";
      display: inline-block;
      width: 13px;
      height: 13px;
      margin-right: 7px;
      border: 2px solid currentColor;
      border-right-color: transparent;
      border-radius: 999px;
      vertical-align: -2px;
      animation: spin .75s linear infinite;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .sub {{ margin-top: 4px; color: var(--muted); font-size: 12px; }}
    .content {{ min-height: 0; overflow: auto; padding: 22px 18px; }}
    .thread {{
      width: min(820px, 100%);
      margin: 0 auto;
      display: grid;
      gap: 18px;
    }}
    .schedule-banner {{
      width: min(820px, 100%);
      margin: 0 auto 16px;
      display: none;
      gap: 8px;
    }}
    .schedule-banner.open {{ display: grid; }}
    .schedule-card {{
      border: 1px solid color-mix(in srgb, var(--accent), var(--line) 58%);
      border-radius: 12px;
      background: color-mix(in srgb, var(--accent), var(--panel) 88%);
      padding: 10px 12px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      min-width: 0;
    }}
    .schedule-card > div {{ min-width: 0; }}
    .schedule-card strong {{ display: block; font-size: 13px; }}
    .schedule-card span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
      overflow: hidden;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
    }}
    .schedule-card button {{ justify-self: end; }}
    .empty {{ color: var(--muted); text-align: center; padding: 44px 10px; }}
    .bubble {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      background: var(--panel);
      box-shadow: var(--shadow);
      display: grid;
      gap: 10px;
      min-width: 0;
    }}
    .bubble.user {{
      margin-left: auto;
      margin-right: 0;
      width: min(620px, 86%);
      background: color-mix(in srgb, var(--accent), var(--soft) 84%);
      border-color: color-mix(in srgb, var(--accent), var(--line) 62%);
    }}
    .bubble.assistant {{
      margin-right: auto;
      margin-left: 0;
      width: min(720px, 92%);
      background: var(--panel);
    }}
    .bubble-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .bubble.user .bubble-head {{
      justify-content: flex-end;
    }}
    .bubble.assistant .bubble-head {{
      justify-content: flex-start;
    }}
    .badge {{
      display: inline-flex;
      width: fit-content;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .badge.done {{ border-color: var(--ok); color: var(--ok); }}
    .badge.running, .badge.queued {{ border-color: var(--accent-2); color: var(--accent-2); }}
    .badge.failed {{ border-color: var(--danger); color: var(--danger); }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      line-height: 1.45;
    }}
    .message-body.collapsed {{
      max-height: 260px;
      overflow: hidden;
      position: relative;
    }}
    .message-body.collapsed::after {{
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      height: 56px;
      background: linear-gradient(to bottom, transparent, var(--panel));
      pointer-events: none;
    }}
    .bubble.user .message-body.collapsed::after {{
      background: linear-gradient(to bottom, transparent, color-mix(in srgb, var(--accent), var(--soft) 84%));
    }}
    .expand-button {{
      width: fit-content;
      min-height: 30px;
      padding: 4px 9px;
      font-size: 12px;
      border-radius: 999px;
    }}
    .composer-wrap {{
      border-top: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg), var(--panel) 42%);
      padding: 14px 18px 18px;
    }}
    .composer {{
      width: min(820px, 100%);
      margin: 0 auto;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--panel);
      box-shadow: var(--shadow);
      padding: 10px;
      display: grid;
      gap: 10px;
    }}
    textarea {{
      width: 100%;
      min-height: 92px;
      resize: vertical;
      border: 0;
      outline: 0;
      background: transparent;
      color: var(--text);
      line-height: 1.45;
    }}
    .composer-actions {{
      display: flex;
      gap: 8px;
      justify-content: space-between;
      align-items: center;
    }}
    .run-options {{
      display: grid;
      grid-template-columns: minmax(120px, .8fr) minmax(150px, 1fr) minmax(180px, 1.2fr);
      gap: 8px;
    }}
    .run-options label {{ gap: 5px; }}
    .right-actions {{ display: flex; gap: 8px; align-items: center; }}
    button.primary, button.secondary {{
      border-radius: 10px;
      min-height: 38px;
      padding: 8px 12px;
      border: 1px solid transparent;
      font-weight: 650;
    }}
    button.primary {{ background: var(--accent); color: #fffaf2; }}
    button.secondary {{ background: transparent; color: var(--text); border-color: var(--line); }}
    button:disabled {{ opacity: .62; cursor: progress; }}
    .toast {{ color: var(--muted); font-size: 13px; min-height: 18px; }}
    .attachments {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .attachment-chip {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      color: var(--muted);
      font-size: 12px;
      background: var(--soft);
    }}
    .attachment-chip button {{
      border: 0;
      background: transparent;
      color: var(--muted);
      padding: 0 0 0 5px;
      min-height: 0;
    }}
    .attachment-preview {{
      display: grid;
      gap: 8px;
    }}
    .attachment-preview img {{
      max-width: min(100%, 420px);
      max-height: 320px;
      border-radius: 10px;
      border: 1px solid var(--line);
      object-fit: contain;
      background: var(--soft);
      cursor: zoom-in;
    }}
    .lightbox {{
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background: rgba(0, 0, 0, .82);
      z-index: 20;
    }}
    .lightbox.open {{ display: flex; }}
    .lightbox img {{
      max-width: 96vw;
      max-height: 88vh;
      object-fit: contain;
      border-radius: 12px;
      background: #111;
    }}
    .lightbox button {{
      position: fixed;
      top: max(12px, env(safe-area-inset-top));
      right: 14px;
      width: auto;
      min-height: 38px;
      border-radius: 999px;
      background: rgba(255, 255, 255, .12);
      color: white;
      border-color: rgba(255, 255, 255, .24);
    }}
    .error {{ color: var(--danger); }}
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, .42);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 18px;
      z-index: 10;
      overflow: hidden;
      overscroll-behavior: none;
      touch-action: none;
    }}
    .modal-backdrop.open {{ display: flex; }}
    .modal {{
      width: min(620px, calc(100vw - 36px));
      max-height: min(760px, calc(100dvh - 36px));
      overflow: auto;
      overscroll-behavior: contain;
      touch-action: pan-y;
      -webkit-overflow-scrolling: touch;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      box-shadow: var(--shadow);
      display: grid;
      gap: 12px;
      min-width: 0;
    }}
    .modal-head {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; }}
    .modal h2 {{ margin: 0; font-size: 18px; }}
    .schedule-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(120px, 150px);
      gap: 10px;
    }}
    .quick-times {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .quick-times button {{
      width: 100%;
    }}
    label {{ display: grid; gap: 6px; color: var(--muted); font-size: 13px; }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--soft);
      color: var(--text);
      padding: 10px;
      min-width: 0;
    }}
    select {{ text-overflow: ellipsis; }}
    .jobs {{
      border-top: 1px solid var(--line);
      padding-top: 10px;
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .job-row {{
      display: grid;
      gap: 5px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 9px;
      background: var(--soft);
    }}
    .mobile-back, .mobile-menu-button, .mobile-fab, .mobile-sheet {{
      display: none;
    }}
    .mobile-options-sheet {{
      display: none;
    }}
    @media (max-width: 780px) {{
      body {{ overflow: hidden; }}
      .app {{
        height: 100dvh;
        display: block;
      }}
      .sidebar {{
        border-right: 0;
        border-bottom: 0;
        height: 100dvh;
        max-height: none;
        grid-template-rows: auto auto minmax(0, 1fr) auto;
      }}
      .app.chat-open .sidebar {{
        display: none;
      }}
      .brand {{ padding: 12px 12px 8px; }}
      .brand h1 {{ text-align: center; font-size: 20px; }}
      .brand span {{ text-align: center; font-size: 12px; }}
      .sidebar-tools {{ padding: 0 10px 8px; }}
      .sessions {{ padding: 0 8px 8px; }}
      .session-btn {{ padding: 11px 14px; border-radius: 12px; }}
      .session-title {{ font-size: 16px; font-weight: 600; }}
      .session-meta {{ font-size: 12px; }}
      .mobile-fab {{
        display: block;
        margin: 10px 18px max(16px, env(safe-area-inset-bottom));
        border-radius: 999px;
        min-height: 56px;
        background: var(--text);
        color: var(--bg);
      }}
      .main {{
        display: none;
        height: auto;
        min-height: 0;
        grid-template-rows: auto minmax(0, 1fr) auto;
      }}
      .app.chat-open .main {{
        display: grid;
        height: 100dvh;
      }}
      .topbar {{
        padding: 10px 12px;
        align-items: center;
        flex-direction: row;
      }}
      .topbar h2 {{ font-size: 15px; }}
      .topbar .sub {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 58vw; }}
      .topbar .right-actions {{ display: none; }}
      .mobile-back, .mobile-menu-button {{
        display: inline-flex;
        width: 42px;
        min-height: 42px;
        align-items: center;
        justify-content: center;
        border-radius: 999px;
      }}
      .content {{ padding: 12px; }}
      .thread {{
        width: 100%;
        gap: 14px;
        padding-bottom: 8px;
      }}
      .schedule-banner {{
        width: 100%;
        margin-bottom: 12px;
      }}
      .schedule-card {{
        grid-template-columns: 1fr;
      }}
      .schedule-card button {{
        justify-self: stretch;
        width: 100%;
      }}
      .feature-grid {{
        grid-template-columns: 1fr;
      }}
      .bubble {{
        border-radius: 12px;
        padding: 10px 11px;
      }}
      .bubble.user {{
        width: min(88%, 520px);
        margin-left: auto;
        margin-right: 4px;
      }}
      .bubble.assistant {{
        width: min(92%, 560px);
        margin-left: 4px;
        margin-right: auto;
      }}
      .composer-wrap {{
        padding: 10px 10px max(10px, env(safe-area-inset-bottom));
      }}
      .composer {{
        border-radius: 14px;
        padding: 9px;
        gap: 8px;
      }}
      textarea {{
        min-height: 54px;
        max-height: 18dvh;
      }}
      .composer-actions {{
        align-items: center;
        flex-direction: row;
      }}
      .composer .toast {{
        min-height: 0;
        font-size: 12px;
      }}
      .run-options {{
        grid-template-columns: 1fr;
      }}
      .composer > .run-options {{
        display: none;
      }}
      .composer .right-actions {{
        display: grid;
        grid-template-columns: 42px 1fr;
        width: 100%;
      }}
      button.primary, button.secondary {{
        min-height: 42px;
      }}
      #sendButton {{ width: 100%; }}
      .composer .right-actions > button.secondary:not(.mobile-menu-button),
      .composer .right-actions > input,
      .composer .right-actions > button.secondary:nth-of-type(2) {{
        display: none;
      }}
      .mobile-sheet {{
        position: fixed;
        left: 10px;
        right: 10px;
        bottom: max(10px, env(safe-area-inset-bottom));
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 10px;
        box-shadow: var(--shadow);
        z-index: 9;
        gap: 8px;
      }}
      .mobile-sheet.open {{ display: grid; }}
      .mobile-sheet button {{ width: 100%; }}
      .mobile-options-sheet {{
        position: fixed;
        left: 10px;
        right: 10px;
        bottom: max(10px, env(safe-area-inset-bottom));
        max-height: min(70dvh, 420px);
        overflow: auto;
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 12px;
        box-shadow: var(--shadow);
        z-index: 11;
        gap: 10px;
      }}
      .mobile-options-sheet.open {{
        display: grid;
      }}
      .mobile-options-sheet .run-options {{
        display: grid;
        grid-template-columns: 1fr;
      }}
      .mobile-options-sheet-head {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 10px;
      }}
      .modal-backdrop {{
        align-items: center;
        justify-content: center;
        padding: max(12px, env(safe-area-inset-top)) 10px max(12px, env(safe-area-inset-bottom));
        overflow: hidden;
        touch-action: none;
      }}
      .modal {{
        width: min(100%, 440px);
        max-height: calc(100dvh - 32px - env(safe-area-inset-top) - env(safe-area-inset-bottom));
        overflow: auto;
        border-radius: 16px;
        padding-bottom: max(16px, env(safe-area-inset-bottom));
        overscroll-behavior: contain;
        touch-action: pan-y;
      }}
      .schedule-grid {{
        grid-template-columns: 1fr 128px;
      }}
      .quick-times {{
        grid-template-columns: 1fr;
      }}
      .modal-head {{
        position: sticky;
        top: 0;
        background: var(--panel);
        padding-bottom: 8px;
      }}
    }}
  </style>
</head>
<body>
  <div class="auth">
    <section class="auth-card">
      <h1>Claude Bridge</h1>
      <p>Helyi vezérlő Claude Code sessionökhöz.</p>
      <label>Token
        <input id="token" value="{safe_token}" autocomplete="off">
      </label>
      <button class="primary" onclick="saveToken()">Megnyitás tokennel</button>
    </section>
  </div>

  <div class="app {locked}">
    <aside class="sidebar">
      <div class="brand">
        <h1>Claude Bridge</h1>
        <span>Sessionök és üzenetek</span>
      </div>
      <div class="sidebar-tools">
        <button id="sessionRefreshButton" class="secondary refresh-button" onclick="refreshSessions(true)">Frissítés</button>
        <button class="secondary" onclick="selectNewSession()">Új</button>
      </div>
      <div id="sessionList" class="sessions"></div>
      <button class="primary mobile-fab" onclick="selectNewSession()">+ Új feladat</button>
    </aside>

    <main class="main">
      <header class="topbar">
        <button class="secondary mobile-back" onclick="backToSessions()">‹</button>
        <div>
          <h2 id="currentTitle">Új beszélgetés</h2>
          <div id="currentMeta" class="sub">Válassz sessiont bal oldalt, vagy indíts újat.</div>
        </div>
        <div class="right-actions">
          <button class="secondary" onclick="toggleFeatures()">Állapot</button>
          <button class="secondary" onclick="openSchedule()">Időzítés</button>
          <button id="refreshButton" class="secondary refresh-button" onclick="refresh(true)">Frissítés</button>
        </div>
      </header>
      <section class="content">
        <div id="featurePanel" class="feature-panel">
          <strong>Mit mutat ez?</strong>
          <div class="sub">Ez egy állapot/roadmap panel: azt mutatja, mely Claude/Codex-szerű képességek vannak már beépítve, és mi a következő nagy fejlesztési irány.</div>
          <div class="feature-grid">
            <div><b>Kész:</b> streamelt válasz és futási timeline</div>
            <div><b>Kész:</b> stop gomb futó Claude munkára</div>
            <div><b>Kész:</b> modell és engedély mód választás</div>
            <div><b>Kész:</b> allowed tools mező, pl. Read/Edit/Bash</div>
            <div><b>Következő:</b> diff előnézet fájlmódosításokhoz</div>
            <div><b>Következő:</b> valódi approval broker, ha a CLI eventek stabilan adják</div>
          </div>
        </div>
        <div id="scheduleBanner" class="schedule-banner"></div>
        <div id="thread" class="thread"></div>
      </section>
      <section class="composer-wrap">
        <div class="composer">
          <textarea id="sendMessage" placeholder="Írj Claude-nak..."></textarea>
          <div class="run-options">
            <label>Modell
              <select id="sendModel">
                <option value="">alapértelmezett</option>
                <option value="sonnet">sonnet</option>
                <option value="opus">opus</option>
              </select>
            </label>
            <label>Engedély mód
              <select id="sendPermissionMode">
                <option value="">default</option>
                <option value="acceptEdits">acceptEdits</option>
                <option value="auto">auto</option>
                <option value="plan">plan</option>
                <option value="bypassPermissions">bypassPermissions</option>
              </select>
            </label>
            <label>Engedélyezett toolok
              <input id="sendAllowedTools" placeholder="pl. Bash(git *) Edit Write Read">
            </label>
          </div>
          <div class="composer-actions">
            <div id="sendToast" class="toast"></div>
            <div class="right-actions">
              <input id="sendFiles" type="file" multiple hidden onchange="handleFiles('send')">
              <button class="secondary mobile-menu-button" onclick="toggleMobileSheet()">+</button>
              <button class="secondary" onclick="document.getElementById('sendFiles').click()">Csatolás</button>
              <button class="secondary" onclick="openSchedule()">Időzítés</button>
              <button id="sendButton" class="primary" onclick="sendNow()">Küldés</button>
            </div>
          </div>
          <div id="sendAttachments" class="attachments"></div>
        </div>
      </section>
    </main>
  </div>

  <div id="mobileSheet" class="mobile-sheet">
    <button class="secondary" onclick="document.getElementById('sendFiles').click(); closeMobileSheet();">Fájl csatolása</button>
    <button class="secondary" onclick="openMobileOptions(); closeMobileSheet();">Modell és engedélyek</button>
    <button class="secondary" onclick="openSchedule(); closeMobileSheet();">Üzenet időzítése</button>
  </div>

  <div id="mobileOptionsSheet" class="mobile-options-sheet">
    <div class="mobile-options-sheet-head">
      <strong>Modell és engedélyek</strong>
      <button class="secondary" onclick="closeMobileOptions()">Bezárás</button>
    </div>
    <div class="run-options">
      <label>Modell
        <select id="mobileSendModel" onchange="syncMobileOptionsToSend()">
          <option value="">alapértelmezett</option>
          <option value="sonnet">sonnet</option>
          <option value="opus">opus</option>
        </select>
      </label>
      <label>Engedély mód
        <select id="mobileSendPermissionMode" onchange="syncMobileOptionsToSend()">
          <option value="">default</option>
          <option value="acceptEdits">acceptEdits</option>
          <option value="auto">auto</option>
          <option value="plan">plan</option>
          <option value="bypassPermissions">bypassPermissions</option>
        </select>
      </label>
      <label>Engedélyezett toolok
        <input id="mobileSendAllowedTools" placeholder="pl. Bash(git *) Edit Write Read" oninput="syncMobileOptionsToSend()">
      </label>
    </div>
  </div>

  <div id="imageLightbox" class="lightbox" onclick="closeImagePreview(event)">
    <button class="secondary" onclick="closeImagePreview(event)">Bezárás</button>
    <img id="imageLightboxImg" src="" alt="Csatolt kép nagy nézetben">
  </div>

  <div id="scheduleModal" class="modal-backdrop" onclick="backdropClose(event)">
    <section class="modal">
      <div class="modal-head">
        <h2>Üzenet időzítése</h2>
        <button class="secondary" onclick="closeSchedule()">Bezárás</button>
      </div>
      <div class="schedule-grid">
        <label>Dátum
          <input id="runDate" type="date">
        </label>
        <label>Idő
          <input id="runTime" type="time" step="60">
        </label>
      </div>
      <div class="quick-times">
        <button class="secondary" type="button" onclick="setScheduleOffset(15)">+15 perc</button>
        <button class="secondary" type="button" onclick="setScheduleOffset(60)">+1 óra</button>
        <button class="secondary" type="button" onclick="setTomorrowMorning()">Holnap 9:00</button>
      </div>
      <label>Claude session
        <select id="scheduleSessionSelect" onchange="applyScheduleSessionDefaults()"></select>
      </label>
      <div class="run-options">
        <label>Modell
          <select id="scheduleModel">
            <option value="">alapértelmezett</option>
            <option value="sonnet">sonnet</option>
            <option value="opus">opus</option>
          </select>
        </label>
        <label>Engedély mód
          <select id="schedulePermissionMode">
            <option value="">default</option>
            <option value="acceptEdits">acceptEdits</option>
            <option value="auto">auto</option>
            <option value="plan">plan</option>
            <option value="bypassPermissions">bypassPermissions</option>
          </select>
        </label>
        <label>Engedélyezett toolok
          <input id="scheduleAllowedTools" placeholder="pl. Bash(git *) Edit Write Read">
        </label>
      </div>
      <label>Üzenet
        <textarea id="scheduleMessage" placeholder="Ezt küldöm a megadott időpontban."></textarea>
      </label>
      <input id="scheduleFiles" type="file" multiple hidden onchange="handleFiles('schedule')">
      <button class="secondary" onclick="document.getElementById('scheduleFiles').click()">Fájl csatolása</button>
      <div id="scheduleAttachments" class="attachments"></div>
      <button id="scheduleButton" class="primary" onclick="schedule()">Időzítés mentése</button>
      <div id="scheduleToast" class="toast"></div>
    </section>
  </div>

  <script>
    const params = new URLSearchParams(location.search);
    let token = params.get("token") || localStorage.getItem("claudeBridgeToken") || "";
    if (params.get("token")) {{
      localStorage.setItem("claudeBridgeToken", params.get("token"));
      history.replaceState(null, "", location.pathname);
    }}
    let sessions = [];
    let runs = [];
    let jobs = [];
    let events = [];
    let transcript = [];
    let sendAttachments = [];
    let scheduleAttachments = [];
    let selectedSessionId = localStorage.getItem("claudeBridgeSelectedSession") || "";
    let pendingInitialScroll = true;
    let collapsedSessionGroups = JSON.parse(localStorage.getItem("claudeBridgeCollapsedGroups") || "[]");
    document.getElementById("token").value = token || "{safe_token}";

    function unlockIfToken() {{
      if (!token) return;
      document.querySelector(".auth").style.display = "none";
      document.querySelector(".app").classList.remove("locked");
    }}

    function api(path, options = {{}}) {{
      return fetch(path, {{
        ...options,
        headers: {{
          "Content-Type": "application/json",
          "X-Claude-Bridge-Token": token,
          ...(options.headers || {{}})
        }}
      }}).then(async response => {{
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "Hiba történt.");
        return data;
      }});
    }}

    function saveToken() {{
      token = document.getElementById("token").value.trim();
      localStorage.setItem("claudeBridgeToken", token);
      unlockIfToken();
      loadSessions();
      refresh();
    }}

    function escapeHtml(value) {{
      return String(value || "").replace(/[&<>"']/g, char => ({{
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }}[char]));
    }}

    function optionLabel(session) {{
      const updated = session.updated_at ? session.updated_at.replace("T", " ") : "";
      const cwd = session.cwd_short ? ` - ${{session.cwd_short}}` : "";
      return `${{session.title}} - ${{updated}}${{cwd}}`;
    }}

    function currentSession() {{
      return sessions.find(session => session.id === selectedSessionId) || null;
    }}

    function optionExists(select, value) {{
      return Array.from(select.options).some(option => option.value === value);
    }}

    function setSelectValue(selectId, value, label) {{
      const select = document.getElementById(selectId);
      const clean = String(value || "").trim();
      if (!clean) {{
        select.value = "";
        return;
      }}
      if (!optionExists(select, clean)) {{
        const option = new Option(label || clean, clean);
        option.dataset.sessionDefault = "true";
        select.appendChild(option);
      }}
      select.value = clean;
    }}

    function applyOptionsToPrefix(prefix, session) {{
      if (!session) {{
        setSelectValue(prefix + "Model", "", "");
        setSelectValue(prefix + "PermissionMode", "", "");
        document.getElementById(prefix + "AllowedTools").value = "";
        return;
      }}
      setSelectValue(prefix + "Model", session.model || "", session.model ? `session: ${{session.model}}` : "");
      setSelectValue(prefix + "PermissionMode", session.permission_mode || "", session.permission_mode ? `session: ${{session.permission_mode}}` : "");
      document.getElementById(prefix + "AllowedTools").value = session.allowed_tools || "";
    }}

    function applyCurrentSessionDefaults() {{
      const session = currentSession();
      applyOptionsToPrefix("send", session);
      syncSendOptionsToMobile();
    }}

    function applyScheduleSessionDefaults() {{
      const select = document.getElementById("scheduleSessionSelect");
      const session = sessions.find(item => item.id === select.value) || null;
      applyOptionsToPrefix("schedule", session);
    }}

    function sessionGroupKey(session) {{
      return session.cwd_short || session.cwd || session.source || "Egyéb";
    }}

    function sessionGroupName(key) {{
      const clean = String(key || "Egyéb").replace(/\\\\+/g, "\\\\").replace(/\\/+$/g, "");
      const parts = clean.split(/[\\\\/]/).filter(Boolean);
      return parts.length ? parts[parts.length - 1] : clean || "Egyéb";
    }}

    function sessionGroupId(key) {{
      return btoa(unescape(encodeURIComponent(String(key || "Egyéb")))).replace(/=+$/g, "");
    }}

    function isGroupCollapsed(id) {{
      return collapsedSessionGroups.includes(id);
    }}

    function toggleSessionGroup(id) {{
      if (isGroupCollapsed(id)) {{
        collapsedSessionGroups = collapsedSessionGroups.filter(item => item !== id);
      }} else {{
        collapsedSessionGroups.push(id);
      }}
      localStorage.setItem("claudeBridgeCollapsedGroups", JSON.stringify(collapsedSessionGroups));
      renderSessions();
    }}

    function fillSessionSelect(selectId) {{
      const select = document.getElementById(selectId);
      select.innerHTML = [
        `<option value="">Új beszélgetés</option>`,
        ...sessions.map(session => `
          <option value="${{escapeHtml(session.id)}}"
                  data-cwd="${{escapeHtml(session.cwd || "")}}"
                  ${{session.id === selectedSessionId ? "selected" : ""}}>
            ${{escapeHtml(optionLabel(session))}}
          </option>
        `)
      ].join("");
    }}

    function renderSessions() {{
      const list = document.getElementById("sessionList");
      if (!sessions.length) {{
        list.innerHTML = `<div class="empty">Nincs betöltött session.</div>`;
        return;
      }}
      const groups = [];
      const byKey = new Map();
      sessions.forEach(session => {{
        const key = sessionGroupKey(session);
        if (!byKey.has(key)) {{
          const group = {{ key, id: sessionGroupId(key), name: sessionGroupName(key), sessions: [] }};
          byKey.set(key, group);
          groups.push(group);
        }}
        byKey.get(key).sessions.push(session);
      }});
      list.innerHTML = groups.map(group => `
        <section class="session-group ${{isGroupCollapsed(group.id) ? "collapsed" : ""}}">
          <button class="folder-btn" onclick="toggleSessionGroup('${{group.id}}')">
            <span class="folder-chevron">⌄</span>
            <span class="folder-title">${{escapeHtml(group.name)}}</span>
            <span class="folder-count">${{group.sessions.length}}</span>
          </button>
          <div class="session-group-items">
            ${{group.sessions.map(session => `
              <button class="session-btn ${{session.id === selectedSessionId ? "active" : ""}}"
                      onclick="selectSession('${{escapeHtml(session.id)}}')">
                <span class="session-title">${{escapeHtml(session.title)}}</span>
                <span class="session-meta">${{escapeHtml(session.cwd_short || session.source || "")}}</span>
              </button>
            `).join("")}}
          </div>
        </section>
      `).join("");
    }}

    function selectSession(sessionId) {{
      selectedSessionId = sessionId;
      if (selectedSessionId) localStorage.setItem("claudeBridgeSelectedSession", selectedSessionId);
      else localStorage.removeItem("claudeBridgeSelectedSession");
      applyCurrentSessionDefaults();
      document.querySelector(".app").classList.add("chat-open");
      fillSessionSelect("scheduleSessionSelect");
      renderSessions();
      renderHeader();
      renderJobs();
      transcript = [];
      pendingInitialScroll = true;
      loadThread();
    }}

    function selectNewSession() {{
      selectSession("");
    }}

    function backToSessions() {{
      document.querySelector(".app").classList.remove("chat-open");
      closeMobileSheet();
      closeMobileOptions();
    }}

    function toggleMobileSheet() {{
      document.getElementById("mobileSheet").classList.toggle("open");
      closeMobileOptions();
    }}

    function closeMobileSheet() {{
      document.getElementById("mobileSheet").classList.remove("open");
    }}

    function openMobileOptions() {{
      syncSendOptionsToMobile();
      document.getElementById("mobileOptionsSheet").classList.add("open");
    }}

    function closeMobileOptions() {{
      document.getElementById("mobileOptionsSheet").classList.remove("open");
    }}

    function syncSendOptionsToMobile() {{
      document.getElementById("mobileSendModel").value = document.getElementById("sendModel").value;
      document.getElementById("mobileSendPermissionMode").value = document.getElementById("sendPermissionMode").value;
      document.getElementById("mobileSendAllowedTools").value = document.getElementById("sendAllowedTools").value;
    }}

    function syncMobileOptionsToSend() {{
      document.getElementById("sendModel").value = document.getElementById("mobileSendModel").value;
      document.getElementById("sendPermissionMode").value = document.getElementById("mobileSendPermissionMode").value;
      document.getElementById("sendAllowedTools").value = document.getElementById("mobileSendAllowedTools").value;
    }}

    function toggleFeatures() {{
      document.getElementById("featurePanel").classList.toggle("open");
    }}

    function renderHeader() {{
      const session = currentSession();
      document.getElementById("currentTitle").textContent = session ? session.title : "Új beszélgetés";
      const optionMeta = session && (session.model || session.permission_mode)
        ? ` · ${{[session.model, session.permission_mode].filter(Boolean).join(" · ")}}`
        : "";
      document.getElementById("currentMeta").textContent = session
        ? `${{session.cwd_short || session.source || ""}}${{optionMeta}}`
        : "A következő küldés új nem-interaktív Claude futást indít.";
    }}

    async function loadSessions() {{
      if (!token) return;
      try {{
        const data = await api("/api/sessions");
        sessions = data.sessions || [];
        if (selectedSessionId && !sessions.some(session => session.id === selectedSessionId)) {{
          selectedSessionId = "";
          localStorage.removeItem("claudeBridgeSelectedSession");
        }}
        fillSessionSelect("scheduleSessionSelect");
        applyCurrentSessionDefaults();
        renderSessions();
        renderHeader();
        renderJobs();
        if (selectedSessionId) {{
          document.querySelector(".app").classList.add("chat-open");
        }}
        if (!transcript.length) {{
          pendingInitialScroll = true;
          loadThread();
        }}
      }} catch (error) {{
        sessions = [];
        fillSessionSelect("scheduleSessionSelect");
        renderSessions();
      }}
    }}

    async function refreshSessions(showSpinner = false) {{
      const button = document.getElementById("sessionRefreshButton");
      if (showSpinner && button) {{
        button.classList.add("loading");
        button.disabled = true;
      }}
      try {{
        await loadSessions();
      }} finally {{
        if (showSpinner && button) {{
          button.classList.remove("loading");
          button.disabled = false;
        }}
      }}
    }}

    function selectedSessionFromId(sessionId) {{
      const session = sessions.find(item => item.id === sessionId);
      return {{
        session_id: session ? session.id : "",
        workdir: session ? session.cwd || "" : ""
      }};
    }}

    function selectedSessionFromSelect(selectId) {{
      const select = document.getElementById(selectId);
      const option = select.options[select.selectedIndex];
      return {{
        session_id: select.value,
        workdir: option ? option.dataset.cwd || "" : ""
      }};
    }}

    function localDateTimeParts(date) {{
      const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
      return {{
        date: local.toISOString().slice(0, 10),
        time: local.toISOString().slice(11, 16)
      }};
    }}

    function setScheduleDateTime(date) {{
      const parts = localDateTimeParts(date);
      document.getElementById("runDate").value = parts.date;
      document.getElementById("runTime").value = parts.time;
    }}

    function getScheduleDateTime() {{
      const date = document.getElementById("runDate").value;
      const time = document.getElementById("runTime").value;
      if (!date || !time) return "";
      return `${{date}}T${{time}}`;
    }}

    function formatRunAt(value) {{
      if (!value) return "";
      return value.replace("T", " ").slice(0, 16);
    }}

    function formatTimestamp(value) {{
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 19);
      return date.toLocaleString("hu-HU", {{
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit"
      }});
    }}

    function timestampMs(value) {{
      if (!value) return 0;
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? 0 : date.getTime();
    }}

    function runOptions(prefix) {{
      return {{
        model: document.getElementById(prefix + "Model").value,
        permission_mode: document.getElementById(prefix + "PermissionMode").value,
        allowed_tools: document.getElementById(prefix + "AllowedTools").value
      }};
    }}

    function copyRunOptions(fromPrefix, toPrefix) {{
      document.getElementById(toPrefix + "Model").value = document.getElementById(fromPrefix + "Model").value;
      document.getElementById(toPrefix + "PermissionMode").value = document.getElementById(fromPrefix + "PermissionMode").value;
      document.getElementById(toPrefix + "AllowedTools").value = document.getElementById(fromPrefix + "AllowedTools").value;
    }}

    function setScheduleOffset(minutes) {{
      const date = new Date(Date.now() + minutes * 60 * 1000);
      date.setSeconds(0, 0);
      setScheduleDateTime(date);
    }}

    function setTomorrowMorning() {{
      const date = new Date();
      date.setDate(date.getDate() + 1);
      date.setHours(9, 0, 0, 0);
      setScheduleDateTime(date);
    }}

    function renderAttachments(kind) {{
      const list = kind === "send" ? sendAttachments : scheduleAttachments;
      const target = document.getElementById(kind === "send" ? "sendAttachments" : "scheduleAttachments");
      target.innerHTML = list.map((item, index) => `
        <span class="attachment-chip">
          ${{escapeHtml(item.name)}}
          <button onclick="removeAttachment('${{kind}}', ${{index}})">x</button>
        </span>
      `).join("");
    }}

    function removeAttachment(kind, index) {{
      if (kind === "send") sendAttachments.splice(index, 1);
      else scheduleAttachments.splice(index, 1);
      renderAttachments(kind);
    }}

    function renderMessageAttachments(list) {{
      if (!list || !list.length) return "";
      return `<div class="attachment-preview">${{list.map(item => {{
        if (item.kind === "image" && item.data_url) {{
          return `<img src="${{escapeHtml(item.data_url)}}" alt="${{escapeHtml(item.name || "csatolt kép")}}" onclick="openImagePreview(this.src)">`;
        }}
        return `<span class="attachment-chip">${{escapeHtml(item.name || "Csatolmány")}}</span>`;
      }}).join("")}}</div>`;
    }}

    function eventsForRun(runId) {{
      return events.filter(event => event.run_id === runId).slice(-8);
    }}

    function renderRunEvents(run) {{
      const list = eventsForRun(run.id);
      if (!list.length) return "";
      return `<div class="event-timeline">${{list.map(event => `
        <div class="event-row ${{escapeHtml(event.kind || "")}}">
          <div class="event-title">${{escapeHtml(event.title || event.kind || "event")}}</div>
          ${{event.body ? `<div>${{escapeHtml(event.body).slice(0, 900)}}</div>` : ""}}
        </div>
      `).join("")}}</div>`;
    }}

    function renderRunControls(run) {{
      if (!["queued", "running"].includes(run.status)) return "";
      return `<button class="secondary expand-button" onclick="stopRun('${{run.id}}')">Stop</button>`;
    }}

    function renderMessageText(text, id) {{
      const value = String(text || "");
      const shouldCollapse = value.length > 1200 || value.split("\\n").length > 18;
      return `
        <pre id="${{id}}" class="message-body ${{shouldCollapse ? "collapsed" : ""}}">${{escapeHtml(value)}}</pre>
        ${{shouldCollapse ? `<button class="secondary expand-button" onclick="toggleMessage('${{id}}', this)">Mutasd tovább</button>` : ""}}
      `;
    }}

    function toggleMessage(id, button) {{
      const node = document.getElementById(id);
      node.classList.toggle("collapsed");
      button.textContent = node.classList.contains("collapsed") ? "Mutasd tovább" : "Összecsukás";
    }}

    function comparableText(value) {{
      return String(value || "")
        .toLowerCase()
        .replace(/\\s+/g, " ")
        .trim()
        .slice(0, 1200);
    }}

    function hasTranscriptText(text) {{
      const needle = comparableText(text);
      if (!needle) return false;
      return transcript.some(message => {{
        const haystack = comparableText(message.text);
        return haystack === needle || haystack.includes(needle) || needle.includes(haystack);
      }});
    }}

    function openImagePreview(src) {{
      document.getElementById("imageLightboxImg").src = src;
      document.getElementById("imageLightbox").classList.add("open");
    }}

    function closeImagePreview(event) {{
      if (event && event.target && event.target.id === "imageLightboxImg") return;
      document.getElementById("imageLightbox").classList.remove("open");
      document.getElementById("imageLightboxImg").src = "";
    }}

    function scrollThreadToBottom() {{
      const go = () => {{
        const content = document.querySelector(".content");
        if (content) content.scrollTop = content.scrollHeight;
      }};
      requestAnimationFrame(go);
      setTimeout(go, 80);
      setTimeout(go, 250);
      setTimeout(go, 700);
    }}

    function uploadFile(file) {{
      return new Promise((resolve, reject) => {{
        const reader = new FileReader();
        reader.onload = async () => {{
          try {{
            const data = await api("/api/upload", {{
              method: "POST",
              body: JSON.stringify({{ name: file.name, data: reader.result }})
            }});
            resolve(data.attachment);
          }} catch (error) {{
            reject(error);
          }}
        }};
        reader.onerror = () => reject(new Error("Nem sikerült beolvasni a fájlt."));
        reader.readAsDataURL(file);
      }});
    }}

    async function handleFiles(kind) {{
      const input = document.getElementById(kind === "send" ? "sendFiles" : "scheduleFiles");
      const toast = document.getElementById(kind === "send" ? "sendToast" : "scheduleToast");
      const files = Array.from(input.files || []);
      input.value = "";
      if (!files.length) return;
      toast.textContent = "Csatolmány feltöltése...";
      try {{
        const uploaded = await Promise.all(files.map(uploadFile));
        if (kind === "send") sendAttachments.push(...uploaded);
        else scheduleAttachments.push(...uploaded);
        renderAttachments(kind);
        toast.textContent = "Csatolva.";
      }} catch (error) {{
        toast.innerHTML = `<span class="error">${{escapeHtml(error.message)}}</span>`;
      }}
    }}

    document.addEventListener("paste", async event => {{
      const items = Array.from(event.clipboardData ? event.clipboardData.items : []);
      const imageItems = items.filter(item => item.kind === "file" && item.type.startsWith("image/"));
      if (!imageItems.length) return;
      event.preventDefault();
      const toast = document.getElementById("sendToast");
      toast.textContent = "Vágólapos kép csatolása...";
      try {{
        const files = imageItems.map((item, index) => {{
          const file = item.getAsFile();
          const ext = (file.type.split("/")[1] || "png").replace("jpeg", "jpg");
          return new File([file], `screenshot-${{Date.now()}}-${{index + 1}}.${{ext}}`, {{ type: file.type }});
        }});
        const uploaded = await Promise.all(files.map(uploadFile));
        sendAttachments.push(...uploaded);
        renderAttachments("send");
        toast.textContent = "Screenshot csatolva.";
      }} catch (error) {{
        toast.innerHTML = `<span class="error">${{escapeHtml(error.message)}}</span>`;
      }}
    }});

    async function loadThread() {{
      if (!token || !selectedSessionId) {{
        transcript = [];
        renderThread();
        return;
      }}
      try {{
        const data = await api("/api/thread?session_id=" + encodeURIComponent(selectedSessionId));
        transcript = data.messages || [];
      }} catch (error) {{
        transcript = [];
      }}
      renderThread();
    }}

    function renderThread() {{
      const thread = document.getElementById("thread");
      let visibleRuns = selectedSessionId
        ? runs.filter(run => run.session_id === selectedSessionId)
        : runs.filter(run => !run.session_id);
      if (selectedSessionId && transcript.length) {{
        visibleRuns = visibleRuns.filter(run => {{
          if (run.status !== "done") return true;
          return !(hasTranscriptText(run.message) || hasTranscriptText(run.summary));
        }});
      }}
      if (!transcript.length && !visibleRuns.length) {{
        thread.innerHTML = `<div class="empty">Még nincs üzenet ebben a nézetben.</div>`;
        return;
      }}
      const items = [];
      transcript.forEach((message, index) => items.push({{
        role: message.role === "user" ? "user" : "assistant",
        label: message.role === "user" ? "Te" : "Claude",
        time: message.timestamp || "",
        sort: timestampMs(message.timestamp) || index,
        text: message.text || "",
        attachments: message.attachments || [],
        status: "",
        order: index
      }}));
      visibleRuns.forEach((run, index) => {{
        const base = 100000 + index * 2;
        items.push({{
          role: "user",
          label: "Te",
          time: run.created_at || "",
          sort: timestampMs(run.created_at) || base,
          text: run.message || "",
          attachments: run.attachments || [],
          status: "",
          order: base
        }});
        items.push({{
          role: "assistant",
          label: run.status ? `${{run.status}} - ${{run.kind}}` : "Claude",
          time: run.finished_at || run.started_at || run.created_at || "",
          sort: timestampMs(run.finished_at || run.started_at || run.created_at) || base + 1,
          text: run.summary || run.error || "Claude válaszára vár...",
          attachments: [],
          status: run.status || "",
          run: run,
          order: base + 1
        }});
      }});
      items.sort((a, b) => (a.sort - b.sort) || (a.order - b.order));
      thread.innerHTML = items.map((message, index) => `
        <article class="bubble ${{message.role}}">
          <div class="bubble-head">
            <span class="${{message.status ? `badge ${{escapeHtml(message.status)}}` : ""}}">${{escapeHtml(message.label)}}</span>
            <span>${{escapeHtml(formatTimestamp(message.time))}}</span>
          </div>
          ${{renderMessageText(message.text, `msg-${{index}}`)}}
          ${{renderMessageAttachments(message.attachments)}}
          ${{message.run ? renderRunEvents(message.run) : ""}}
          ${{message.run ? renderRunControls(message.run) : ""}}
        </article>
      `).join("");
      if (pendingInitialScroll) {{
        scrollThreadToBottom();
        pendingInitialScroll = false;
      }}
    }}

    function renderJobs() {{
      const box = document.getElementById("scheduleBanner");
      const active = jobs.filter(job =>
        job.status === "scheduled" &&
        (!selectedSessionId || !job.session_id || job.session_id === selectedSessionId)
      );
      box.classList.toggle("open", active.length > 0);
      box.innerHTML = active.map(job => `
        <div class="schedule-card">
          <div>
            <strong>Időzített üzenet beállítva: ${{escapeHtml(formatTimestamp(job.run_at))}}</strong>
            <span>${{escapeHtml(job.message)}}</span>
          </div>
          <button class="secondary" onclick="cancelJob('${{job.id}}')">Törlés</button>
        </div>
      `).join("");
    }}

    async function sendNow() {{
      const button = document.getElementById("sendButton");
      const toast = document.getElementById("sendToast");
      button.disabled = true;
      toast.textContent = "Küldés folyamatban...";
      try {{
        const session = selectedSessionFromId(selectedSessionId);
        await api("/api/send", {{
          method: "POST",
          body: JSON.stringify({{
            session_id: session.session_id,
            workdir: session.workdir,
            message: document.getElementById("sendMessage").value,
            attachments: sendAttachments,
            ...runOptions("send")
          }})
        }});
        document.getElementById("sendMessage").value = "";
        sendAttachments = [];
        renderAttachments("send");
        closeMobileSheet();
        closeMobileOptions();
        toast.textContent = "Elküldve.";
        refresh();
        loadThread();
      }} catch (error) {{
        toast.innerHTML = `<span class="error">${{escapeHtml(error.message)}}</span>`;
      }} finally {{
        button.disabled = false;
      }}
    }}

    function openSchedule() {{
      fillSessionSelect("scheduleSessionSelect");
      if (!document.getElementById("runDate").value || !document.getElementById("runTime").value) {{
        setScheduleOffset(15);
      }}
      document.getElementById("scheduleToast").textContent = "";
      document.getElementById("scheduleMessage").value = document.getElementById("sendMessage").value;
      copyRunOptions("send", "schedule");
      scheduleAttachments = [...sendAttachments];
      renderAttachments("schedule");
      closeMobileSheet();
      document.getElementById("scheduleModal").classList.add("open");
    }}

    function closeSchedule() {{
      document.getElementById("scheduleModal").classList.remove("open");
    }}

    function backdropClose(event) {{
      if (event.target.id === "scheduleModal") closeSchedule();
    }}

    document.getElementById("scheduleModal").addEventListener("touchmove", event => {{
      if (!event.target.closest(".modal")) event.preventDefault();
    }}, {{ passive: false }});

    async function schedule() {{
      const button = document.getElementById("scheduleButton");
      const toast = document.getElementById("scheduleToast");
      button.disabled = true;
      toast.textContent = "Időzítés mentése...";
      try {{
        const session = selectedSessionFromSelect("scheduleSessionSelect");
        const runAt = getScheduleDateTime();
        await api("/api/schedule", {{
          method: "POST",
          body: JSON.stringify({{
            run_at: runAt,
            session_id: session.session_id,
            workdir: session.workdir,
            message: document.getElementById("scheduleMessage").value,
            attachments: scheduleAttachments,
            ...runOptions("schedule")
          }})
        }});
        document.getElementById("scheduleMessage").value = "";
        document.getElementById("sendMessage").value = "";
        sendAttachments = [];
        renderAttachments("send");
        scheduleAttachments = [];
        renderAttachments("schedule");
        toast.textContent = "Időzítve.";
        closeSchedule();
        refresh();
      }} catch (error) {{
        toast.innerHTML = `<span class="error">${{escapeHtml(error.message)}}</span>`;
      }} finally {{
        button.disabled = false;
      }}
    }}

    async function cancelJob(jobId) {{
      await api("/api/cancel", {{
        method: "POST",
        body: JSON.stringify({{ job_id: jobId }})
      }});
      refresh();
    }}

    async function stopRun(runId) {{
      await api("/api/stop", {{
        method: "POST",
        body: JSON.stringify({{ run_id: runId }})
      }});
      refresh();
    }}

    async function refresh(showSpinner = false) {{
      if (!token) return;
      const refreshButton = document.getElementById("refreshButton");
      if (showSpinner && refreshButton) {{
        refreshButton.classList.add("loading");
        refreshButton.disabled = true;
      }}
      try {{
        const data = await api("/api/state");
        runs = data.runs || [];
        jobs = data.jobs || [];
        events = data.events || [];
        renderThread();
        renderJobs();
      }} catch (error) {{
        document.getElementById("thread").innerHTML = `<span class="error">${{escapeHtml(error.message)}}</span>`;
      }} finally {{
        if (showSpinner && refreshButton) {{
          refreshButton.classList.remove("loading");
          refreshButton.disabled = false;
        }}
      }}
    }}

    const later = new Date(Date.now() + 15 * 60 * 1000);
    later.setSeconds(0, 0);
    setScheduleDateTime(later);
    loadSessions();
    refresh();
    setInterval(refresh, 4000);
    setInterval(loadThread, 10000);
    setInterval(loadSessions, 30000);
  </script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Mobilról elérhető Claude CLI bridge.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()

    init_db()
    stop_event = threading.Event()
    threading.Thread(target=scheduler_loop, args=(stop_event,), daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    ip = local_ip()
    safe_print("Claude Bridge fut.")
    safe_print(f"Gépen:  http://127.0.0.1:{args.port}/")
    safe_print(f"Mobil:  http://{ip}:{args.port}/")
    safe_print(f"Token:  {CONFIG['token']}")
    safe_print("Leállítás: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        (ROOT / "bridge_error.log").write_text(f"{now_iso()} {type(exc).__name__}: {exc}\n", encoding="utf-8")
        raise


