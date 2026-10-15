#!/usr/bin/env python3
"""
opencode-remote bridge — control an opencode session from Telegram.

What it does:
  • Long-polls Telegram (getUpdates, outbound HTTPS only — no ports opened)
    and forwards your messages into the *attached* opencode session via the
    local opencode server REST API (127.0.0.1, see config.json).
  • Sends completed agent replies back to Telegram.
  • Announces agent tool calls as they happen (bash commands, edits, writes,
    subagent tasks, web fetches — compact one-liners, no tool output).
  • Pins a banner with the attached session's name (updated if renamed).
  • Mirrors the agent's todo list into a single pinned Telegram message that
    gets EDITED in place as items progress, and unpinned when all are done.
  • On every attach: wipes the DM history and replays the newly attached
    session's full history in order (user 👤 / tools / agent).
  • On every detach (/detach in Telegram, /remote off there, or /remote
    toggled off in the TUI): wipes the DM again, silently, so the chat is
    left clean & empty. A bridge restart alone never wipes —
    only real attach/detach events do.
  • Pushes permission requests to Telegram (with tap-to-reply buttons)
    and relays your decision back to opencode.
  • Attachments (photo / document / voice / audio / video) are downloaded
    to ~/opencode-remote/inbox/ (last 30 kept) and announced to the session
    with the saved path, so the agent can work on them with its tools.

Attach/detach:
  • From an opencode TUI attached to this server:  /remote   (or /remote off)
  • From Telegram:  /sessions  then  /attach <n>,  /new,  /detach
    (/dir sets the root dir that /sessions lists from and /new creates in;
    the same is doable via buttons: 📂 in /sessions, and /new asks to
    confirm the root dir — create / change / cancel — before taking effect)

Credentials: read from ~/opencode-remote/.env (BOT_TOKEN, ADMIN_USER_ID) —
             local to this project (@OpenCode_divonr_bot, dedicated bot).
State:       ~/opencode-remote/state.json (atomic writes; safe to delete).
             On bot-token change, Telegram-side state (update offset, pinned
             message ids) is auto-reset — the old chat's ids don't apply.
Service:     systemctl --user status opencode-remote-bridge

Only messages from ADMIN_USER_ID are accepted; everyone else is ignored.

Markdown ⇄ Telegram formatting (tg_md.py — from the telegram MCP server):
  • Outbound agent texts are converted markdown → Telegram HTML (bold, code,
    tables, links, headers), with a plain-text fallback on any failure.
    Unlike the MCP version, no ‏RLM/right-align marks are injected.
  • Inbound Telegram formatting (entities) is converted back to markdown
    before the text is forwarded to the session (also for captions).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import time
import traceback
from html import escape as html_escape
from pathlib import Path

import requests

# markdown ⇄ telegram formatting (adapted from the user's telegram MCP server;
# no \u200f RTL marks — this feed is mostly English/LTR)
from tg_md import markdown_to_tg_html, tg_entities_to_markdown

# ── Config ────────────────────────────────────────────────────────────────

BASE = Path(__file__).resolve().parent
STATE_PATH = BASE / "state.json"
CONFIG_PATH = BASE / "config.json"
GATEWAY_ENV = BASE / ".env"
TG_MAX = 4000  # telegram message limit is 4096; leave headroom
MSG_WINDOW = 60  # how many recent messages to scan each poll

_cfg = {"server_port": 17496, "poll_seconds": 3}
try:
    _cfg.update(json.loads(CONFIG_PATH.read_text()))
except Exception:
    pass

SERVER = f"http://127.0.0.1:{_cfg['server_port']}"
POLL = float(_cfg.get("poll_seconds", 3))


def _load_env(path: Path) -> dict[str, str]:
    creds = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            creds[k.strip()] = v.strip()
    return creds


_creds = _load_env(GATEWAY_ENV)
BOT_TOKEN = _creds["BOT_TOKEN"]
ADMIN_ID = int(_creds["ADMIN_USER_ID"])
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

log = lambda *a: print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def _remote_marker() -> str:
    """Prefix of the /remote command template body (up to $ARGUMENTS) — used
    to detect /remote-created turns in the session for mirror-skip + cleanup."""
    try:
        body = (Path.home() / ".config" / "opencode" / "command" / "remote.md").read_text()
        body = body.split("---", 2)[-1]  # drop frontmatter
        return body.split("$ARGUMENTS")[0].strip()
    except Exception:
        return "Use the `telegram_remote` tool. Map the user input"


REMOTE_MARKER = _remote_marker()


# ── State (state.json is the single source of truth) ──────────────────────

_lock = threading.Lock()


def state_read() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def state_write(patch: dict) -> dict:
    with _lock:
        cur = state_read()
        cur.update(patch)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2))
        os.replace(tmp, STATE_PATH)
        return cur


# ── Telegram helpers ──────────────────────────────────────────────────────

def tg_send(text: str, html: bool = False, reply_markup: dict | None = None) -> int | None:
    """Send text to the admin, splitting on Telegram's 4096-char limit.

    Returns the message_id of the LAST chunk sent (for later editing).
    """
    text = text or "(empty)"
    last_id = None
    while text:
        chunk = text[:TG_MAX]
        if len(text) > TG_MAX and "\n" in chunk:
            chunk = chunk.rsplit("\n", 1)[0]
        text = text[len(chunk):]
        body: dict = {"chat_id": ADMIN_ID, "text": chunk}
        if html:
            body["parse_mode"] = "HTML"
        if reply_markup is not None and not text:  # attach keyboard to last chunk
            body["reply_markup"] = reply_markup
        r = requests.post(f"{TG_API}/sendMessage", json=body, timeout=15)
        r.raise_for_status()
        last_id = r.json()["result"]["message_id"]
    if last_id:
        st = state_read()
        if last_id > (st.get("max_tg_msg_id") or 0):
            state_write({"max_tg_msg_id": last_id})
    return last_id


def tg_edit(message_id: int, text: str) -> bool:
    """Edit a previously sent message. False if editing isn't possible."""
    try:
        r = requests.post(
            f"{TG_API}/editMessageText",
            json={"chat_id": ADMIN_ID, "message_id": message_id, "text": text[:TG_MAX]},
            timeout=15,
        )
        return r.ok
    except Exception:
        return False


def tg_edit_kb(message_id: int, text: str, reply_markup: dict | None = None,
               html: bool = False) -> bool:
    """Edit a message's text and (optionally) its inline keyboard in one call."""
    try:
        body: dict = {"chat_id": ADMIN_ID, "message_id": message_id, "text": text[:TG_MAX]}
        if html:
            body["parse_mode"] = "HTML"
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        r = requests.post(f"{TG_API}/editMessageText", json=body, timeout=15)
        return r.ok
    except Exception:
        return False


def tg_answer_cb(cb_id: str, text: str = "") -> None:
    """Stop an inline-button's loading spinner (with an optional toast)."""
    try:
        requests.post(f"{TG_API}/answerCallbackQuery",
                      json={"callback_query_id": cb_id, "text": text[:200]}, timeout=10)
    except Exception:
        pass


def tg_send_md(text: str) -> int | None:
    """Send markdown-formatted agent text: convert to Telegram HTML and send;
    falls back to raw plain text if conversion or the HTML send fails (so a
    weird formatting edge can never wedge the watcher loop)."""
    try:
        return tg_send(markdown_to_tg_html(text), html=True)
    except Exception as e:
        log("md-formatted send failed, retrying plain:", e)
        try:
            return tg_send(text)
        except Exception as e2:
            log("plain fallback send failed too:", e2)
            return None


def tg_delete(message_id: int) -> None:
    try:
        requests.post(
            f"{TG_API}/deleteMessage",
            json={"chat_id": ADMIN_ID, "message_id": message_id},
            timeout=15,
        )
    except Exception:
        pass


def tg_pin(message_id: int) -> bool:
    try:
        r = requests.post(
            f"{TG_API}/pinChatMessage",
            json={"chat_id": ADMIN_ID, "message_id": message_id, "disable_notification": True},
            timeout=15,
        )
        return r.ok
    except Exception:
        return False


def tg_unpin(message_id: int) -> None:
    try:
        requests.post(
            f"{TG_API}/unpinChatMessage",
            json={"chat_id": ADMIN_ID, "message_id": message_id},
            timeout=15,
        )
    except Exception:
        pass


def tg_updates(offset: int) -> list[dict]:
    r = requests.get(
        f"{TG_API}/getUpdates",
        params={"offset": offset, "timeout": 30},
        timeout=45,
    )
    r.raise_for_status()
    return r.json()["result"]


# ── opencode server helpers ───────────────────────────────────────────────

def oc_get(path: str, directory: str | None = None, **params):
    if directory:
        params["directory"] = directory
    r = requests.get(f"{SERVER}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def oc_post(path: str, body: dict | None = None, directory: str | None = None):
    params = {"directory": directory} if directory else {}
    r = requests.post(f"{SERVER}{path}", params=params, json=body or {}, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}


def oc_patch(path: str, body: dict, directory: str | None = None):
    params = {"directory": directory} if directory else {}
    r = requests.patch(f"{SERVER}{path}", params=params, json=body, timeout=15)
    r.raise_for_status()
    return r.json() if r.content else {}


def oc_delete(path: str, directory: str | None = None, **params):
    if directory:
        params["directory"] = directory
    r = requests.delete(f"{SERVER}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json() if r.content else {}


def server_alive() -> bool:
    try:
        requests.get(f"{SERVER}/global/health", timeout=5)
        return True
    except Exception:
        return False


# ── Tool-call rendering ───────────────────────────────────────────────────

TOOL_ICONS = {
    "bash": "🔧",
    "edit": "✏️",
    "write": "📝",
    "task": "🤖",
    "webfetch": "🌐",
    "websearch": "🔍",
}
# tools not worth a notification (too frequent / handled elsewhere)
TOOL_SKIP = {"read", "glob", "grep", "list", "todowrite", "question",
             "skill", "telegram_remote", "lsp"}


def tool_sig(part: dict) -> str | None:
    """Dedupe signature: same tool + same target. A streak of identical
    signatures is reported once."""
    name = part.get("tool", "?")
    if name in TOOL_SKIP:
        return None
    inp = (part.get("state") or {}).get("input") or {}
    if name == "bash":
        return "bash:" + str(inp.get("command", ""))
    if name in ("edit", "write"):
        return f"{name}:" + str(inp.get("filePath", ""))
    if name == "task":
        return "task:" + str(inp.get("description", ""))
    if name in ("webfetch", "websearch"):
        return f"{name}:" + str(inp.get("url") or inp.get("query", ""))
    return name + ":" + json.dumps(inp, sort_keys=True, ensure_ascii=False)[:120]


def fmt_tool(part: dict) -> str | None:
    """HTML-formatted one-liner for a tool call (no tool output)."""
    name = part.get("tool", "?")
    if name in TOOL_SKIP:
        return None
    st = part.get("state") or {}
    inp = st.get("input") or {}
    icon = TOOL_ICONS.get(name, "▫️")
    mark = " ❌" if st.get("status") == "error" else ""
    if name == "bash":
        cmd = " ".join(str(inp.get("command", "")).split())[:250]
        desc = inp.get("description")
        head = f"{icon} bash"
        if desc:
            head += f" [{html_escape(str(desc))}]"
        return f"{head}: <code>{html_escape(cmd)}</code>{mark}"
    if name in ("edit", "write"):
        detail = inp.get("filePath", "")
        return f"{icon} {name}: <code>{html_escape(str(detail))}</code>{mark}"
    if name == "task":
        detail = inp.get("description") or str(inp.get("prompt", ""))[:100]
    elif name in ("webfetch", "websearch"):
        detail = inp.get("url") or inp.get("query", "")
    else:
        detail = json.dumps(inp, ensure_ascii=False)[:120]
    detail = " ".join(str(detail).split())[:250]
    return f"{icon} {name}: {html_escape(detail)}{mark}"


# ── Todo rendering ────────────────────────────────────────────────────────

TODO_ICONS = {"completed": "✅", "in_progress": "🔄", "pending": "⬜", "cancelled": "➖"}


def todos_done(items: list[dict]) -> bool:
    return all(it.get("status") in ("completed", "cancelled") for it in items)


def render_todo(items: list[dict]) -> str:
    header = "📋 משימות (הושלמו ✅):" if todos_done(items) else "📋 משימות:"
    lines = [header]
    for it in items:
        icon = TODO_ICONS.get(it.get("status"), "⬜")
        lines.append(f"{icon} {it.get('content', '')}")
    return "\n".join(lines)


def todos_hash(items: list[dict]) -> str:
    raw = json.dumps(items, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()


def fmt_tokens(n: int | None) -> str:
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return f"{n/1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


def short_model_name(raw: str | None) -> str:
    """Short display name for a model id: strip any provider prefix (text
    before `/` or `:`), then cut at the FIRST occurrence of any of: space,
    hyphen, slash, or an English CAPITAL letter A-Z (delimiter excluded).
    Returns "?" for None/empty/unparseable input — the banner never omits
    the model segment."""
    if not raw or not isinstance(raw, str):
        return "?"
    s = raw.strip()
    if not s:
        return "?"
    # strip provider prefix (text before `/` or `:`). Note: a bare `/` also
    # doubles as a cut delimiter ("Abc/models"->"Abc"), while a "Provider
    # Name/model" prefix contains a space ("Experiential Labs/gemini-4-argon"
    # ->"gemini-4-argon"). So: `:` always strips; `/` strips only when the
    # prefix contains a space, otherwise the `/` is left for the cut below.
    if ":" in s:
        s = s.rsplit(":", 1)[-1].strip()
        if not s:
            return "?"
    if "/" in s:
        pre, _, post = s.rpartition("/")
        if " " in pre:
            s = post.strip()
            if not s:
                return "?"
        # else: keep the `/` — the cut loop below stops at it ("Abc/..."->"Abc")
    # cut at first delimiter (capitals only count past index 0, so "Abc" stays "Abc")
    for i in range(1, len(s)):
        c = s[i]
        if c in (" ", "-", "/") or ("A" <= c <= "Z"):
            s = s[:i]
            break
    return s or "?"


def session_model_from_obj(sess: dict | None) -> str | None:
    """Extract the raw model id from a GET /session/{id} response object.
    Returns None when unavailable (caller shows "?"). Never raises."""
    try:
        if not isinstance(sess, dict):
            return None
        m = sess.get("model")
        if isinstance(m, dict):
            mid = m.get("id") or m.get("modelID") or m.get("modelId")
            if mid:
                return str(mid)
        elif isinstance(m, str) and m.strip():
            return m.strip()
        for key in ("modelID", "modelId", "model_id"):
            v = sess.get(key)
            if v and isinstance(v, str) and v.strip():
                return v.strip()
        return None
    except Exception:
        return None


def session_model_raw(sid: str, directory: str | None) -> str | None:
    """Resolve the session's CURRENT model id via GET /session/{sid}
    (field: `model.id`). Returns None on any failure (banner shows "?")."""
    try:
        sess = oc_get(f"/session/{sid}", directory)
        return session_model_from_obj(sess)
    except Exception as e:
        log("session model lookup failed:", e)
        return None


def session_banner_text(title: str, sid: str, directory: str | None,
                        tokens: int | None = None, busy: bool = False,
                        model: str | None = None) -> str:
    """📌 [🟢 ][<tokens> | ]<shortModel> | <session title>  (🟢 only while busy)"""
    segs = []
    tok = fmt_tokens(tokens)
    if tok:
        segs.append(tok)
    segs.append(short_model_name(model))
    segs.append(title)
    return "📌 " + ("🟢 " if busy else "") + " | ".join(segs)


# ── Telegram command menu (registered on startup) ─────────────────────────

TG_COMMANDS = [
    ("help", "כל הפקודות"),
    ("sessions", "סשנים אחרונים (עם כפתור לבחירת תיקייה)"),
    ("dir", "תיקיית הבסיס לסשנים (הצגה/הגדרה/איפוס)"),
    ("attach", "חיבור לסשן (מנקה צ'אט, מציג 15 פניות אחרונות)"),
    ("new", "סשן חדש + חיבור (עם אישור תיקיית הבסיס)"),
    ("rename", "שינוי שם הסשן המחובר"),
    ("detach", "ניתוק מהסשן (ומנקה את הצ'אט)"),
    ("abort", "עצירת הריצה הנוכחית"),
    ("status", "מצב הגשר והסשן"),
    ("tools", "הדלקה/כיבוי התראות כלים"),
    ("todos", "הדלקה/כיבוי רשימת משימות"),
    ("q", "מענה טקסט חופשי לשאלה פתוחה"),
    ("model", "החלפת המודל (בחירה / מעבר / ברירת מחדל)"),
    ("remote", "סטטוס/ניתוק הגשר (בטלגרם: attach דרך /sessions)"),
]


TG_CMD_NAME = re.compile(r"^[a-z0-9_]{1,32}$")


def register_telegram_commands() -> None:
    """Bridge commands first, then native opencode commands (from /command)."""
    cmds = [{"command": c, "description": d} for c, d in TG_COMMANDS]
    owned = {c for c, _ in TG_COMMANDS}
    try:
        items = oc_get("/command", str(Path.home()))
        if isinstance(items, dict):
            items = items.get("commands", [])
        for c in items:
            name = str(c.get("name") or "")
            desc = (c.get("description") or name)[:256] or name
            if name and name not in owned and TG_CMD_NAME.match(name):
                cmds.append({"command": name, "description": desc})
    except Exception as e:
        log("native commands fetch failed:", e)
    cmds = cmds[:100]  # telegram cap
    try:
        r = requests.post(
            f"{TG_API}/setMyCommands",
            json={"commands": cmds, "scope": {"type": "all_private_chats"}},
            timeout=15,
        )
        if not r.ok:
            log("setMyCommands failed:", r.text[:120])
        r2 = requests.post(
            f"{TG_API}/setChatMenuButton",
            json={"chat_id": ADMIN_ID, "menu_button": {"type": "commands"}},
            timeout=15,
        )
        if not r2.ok:
            log("setChatMenuButton failed:", r2.text[:120])
        log(f"registered {len(cmds)} telegram commands")
    except Exception as e:
        log("setMyCommands error:", e)


# ── Bridge commands (Telegram side) ───────────────────────────────────────

HELP = """🤖 opencode remote
Just send text — it goes into the attached session.
Attaching (/attach, /new, /remote) wipes this chat and mirrors the session's last turns (button on top loads earlier ones).

/sessions — list recent sessions (from the /dir root; 📂 button opens a dir browser to pick another root)
/attach <n|ses_id> — attach to a session
/new [title] — new session & attach (asks to confirm the root dir first — create / change dir / cancel)
/dir [path|reset] — show/set the root dir for /sessions & /new (no arg = show, reset = auto)
/remote — status (or /remote off to detach; attach from here via /sessions)
/rename <name> — rename the attached session
/detach — detach and wipe this chat clean
/abort — stop current run
/status — bridge & session status
/tools — toggle tool-call notifications
/todos — toggle the live todo list
/q <text> — free-text answer to a pending question
/model — pick the attached session's model (provider → model buttons)
/model <provider>/<model> — switch the attached session now (also provider:model or a bare model id)
/model --config [<provider>/<model>] — show / set the default model for NEW sessions
/allow · /always · /deny — answer a permission request

Native opencode commands also work here — /init, /compact, /review, /undo…
run straight on the attached session via /session/{id}/command.
"""

def root_dir(st: dict) -> str:
    """Root dir that /sessions lists from and /new creates in:
    explicit `root_dir` override → attached session's directory → home."""
    return st.get("root_dir") or st.get("directory") or str(Path.home())


def cmd_dir(st: dict, arg: str) -> str:
    """Show/set the root directory for session listing & creation."""
    if not arg:
        explicit = st.get("root_dir")
        if explicit:
            return (f"📂 root dir: {explicit}\n"
                    "(/sessions lists from here, /new creates here; /dir reset = back to auto)")
        return (f"📂 root dir: {root_dir(st)} (auto — attached session's dir, else home)\n"
                "set with /dir <path>, clear with /dir reset")
    if arg.lower() in ("reset", "auto", "default"):
        state_write({"root_dir": None})
        return f"📂 root dir override cleared — /sessions & /new now use: {root_dir(state_read())}"
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = Path(root_dir(st)) / p  # relative to current root, like cd
    p = p.resolve()
    if not p.is_dir():
        return f"⚠️ not a directory: {p}"
    state_write({"root_dir": str(p)})
    return f"📂 root dir set: {p}\n/sessions will list from here, /new will create here."


_last_sessions: list[dict] = []  # for /attach <n>
_native_cmds: dict = {"ts": 0.0, "names": set()}


def native_command_names(directory: str | None) -> set:
    """Names of this server's slash-commands (cached 60s)."""
    if time.time() - _native_cmds["ts"] > 60:
        try:
            items = oc_get("/command", directory)
            if isinstance(items, dict):
                items = items.get("commands", [])
            _native_cmds["names"] = {c.get("name") for c in items if c.get("name")}
            _native_cmds["ts"] = time.time()
        except Exception:
            pass
    return _native_cmds["names"]


def cmd_status(st: dict) -> str:
    alive = server_alive()
    sid = st.get("session_id")
    lines = [f"🖥 opencode server: {'✅ up' if alive else '❌ DOWN'} (port {_cfg['server_port']})"]
    if sid:
        lines.append(f"🔗 attached: {sid}\n📂 {st.get('directory', '?')}")
        try:
            s = oc_get(f"/session/{sid}", st.get("directory"))
            lines.append(f"📝 title: {s.get('title', '?')}")
        except Exception as e:
            lines.append(f"⚠️ session lookup failed: {e}")
    else:
        lines.append("🔌 not attached — use /sessions + /attach, or /remote in the TUI")
    toggles = f"🛠 tool notifications: {'on' if st.get('notify_tools', True) else 'off'}"
    toggles += f" · 📋 todos: {'on' if st.get('notify_todos', True) else 'off'}"
    lines.append(toggles)
    root_note = "" if st.get("root_dir") else " (auto)"
    lines.append(f"🗂 root dir: {root_dir(st)}{root_note}")
    return "\n".join(lines)


def cmd_sessions(st: dict) -> tuple[str, dict]:
    """Session listing from the current root dir + a button to browse for
    another root dir. Returns (text, reply_markup)."""
    global _last_sessions
    d = root_dir(st)
    sessions = oc_get("/session", d)
    sessions.sort(key=lambda s: (s.get("time") or {}).get("updated", 0), reverse=True)
    _last_sessions = sessions[:8]
    kb = {"inline_keyboard": [[
        {"text": "📂 בחר תיקיית בסיס אחרת…", "callback_data": "dir:browse:sessions"}]]}
    head = f"📂 {d}"
    if not _last_sessions:
        return head + "\nNo sessions found here — /new creates one.", kb
    out = [head, "📋 Recent sessions (attach with /attach <n>):"]
    for i, s in enumerate(_last_sessions, 1):
        mark = " 🔗" if s.get("id") == st.get("session_id") else ""
        out.append(f"{i}. {s.get('title', 'untitled')[:40]} · {s['id'][-6:]}{mark}")
    return "\n".join(out), kb


def cmd_attach(st: dict, arg: str) -> str:
    sid = None
    if arg.isdigit() and _last_sessions:
        i = int(arg) - 1
        if 0 <= i < len(_last_sessions):
            sid = _last_sessions[i]["id"]
    elif arg.startswith("ses_"):
        sid = arg
    elif _last_sessions and not arg:
        sid = _last_sessions[0]["id"]
    if not sid:
        return "Usage: /attach <n from /sessions | ses_...>"
    if _last_sessions and arg.isdigit():
        i = int(arg) - 1
        s = _last_sessions[i]
        directory = s.get("directory") or root_dir(st)
    else:
        directory = root_dir(state_read())
    state_write({"session_id": sid, "directory": directory, "reseed": True,
                 "todo_msg_id": None, "todo_hash": None, "todo_pinned": False,
                 "children": [], "children_initialized": False})
    return f"🔗 attaching to {sid}…"


def create_and_attach(title: str, d: str) -> str:
    """Create a session in directory `d` and attach the bridge to it."""
    s = oc_post("/session", {"title": title or "telegram"}, d)
    state_write({"session_id": s["id"], "directory": d, "reseed": True,
                 "todo_msg_id": None, "todo_hash": None, "todo_pinned": False,
                 "children": [], "children_initialized": False})
    return f"🆕 session created & attached: {s['id']}\n📂 {d}"


def cmd_new(st: dict, title: str) -> tuple[str, dict]:
    """First ask to confirm the root dir (buttons: create / change / cancel);
    the session is only created after the ✅ button is tapped."""
    d = root_dir(st)
    state_write({"pending_new": {"title": title, "dir": d}})
    kb = {"inline_keyboard": [
        [{"text": "✅ צור כאן", "callback_data": "new:ok"}],
        [{"text": "📂 שנה תיקייה…", "callback_data": "new:chg"},
         {"text": "❌ ביטול", "callback_data": "new:no"}]]}
    text = "🆕 New session" + (f": «{title[:80]}»" if title else "")
    text += f"\n📂 {d}\ncreate the session in this root dir?"
    return text, kb


def handle_command(text: str) -> str | tuple[str, dict] | None:
    """Return the reply (plain text, or (text, reply_markup) when the reply
    carries buttons) if `text` was a bridge command, else None."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    st = state_read()
    try:
        if cmd in ("start", "help"):
            return HELP
        if cmd == "status":
            return cmd_status(st)
        if cmd == "sessions":
            return cmd_sessions(st)
        if cmd == "attach":
            return cmd_attach(st, arg)
        if cmd == "new":
            return cmd_new(st, arg)
        if cmd == "dir":
            return cmd_dir(st, arg)
        if cmd == "model":
            return cmd_model(st, arg)
        if cmd == "rename":
            sid = st.get("session_id")
            if not sid:
                return "Not attached."
            if not arg:
                return "Usage: /rename <new session name>"
            oc_patch(f"/session/{sid}", {"title": arg}, st.get("directory"))
            # banner syncs within a few seconds via the watcher
            return f"✏️ renamed to: {arg}"
        if cmd == "detach":
            state_write({"session_id": None, "todo_msg_id": None, "todo_hash": None, "todo_pinned": False})
            return "🔌 detached — wiping this chat clean…"
        if cmd == "remote":
            # TUI-side toggle; from Telegram: status / off. Attaching from
            # here is /sessions + /attach <n> (there's no "current TUI
            # session" to grab from this side). Handled natively so it never
            # hits the /session/:id/command endpoint.
            sub = arg.lower()
            if sub in ("off", "detach"):
                state_write({"session_id": None, "todo_msg_id": None,
                             "todo_hash": None, "todo_pinned": False})
                return "🔌 detached — wiping this chat clean…"
            if sub in ("on", "attach"):
                return "Attach from Telegram with /sessions then /attach <n>, or /new for a fresh session."
            return cmd_status(st)
        if cmd == "abort":
            sid = st.get("session_id")
            if sid:
                oc_post(f"/session/{sid}/abort", {}, st.get("directory"))
                return "🛑 abort sent."
            return "Not attached."
        if cmd == "tools":
            new = not st.get("notify_tools", True)
            state_write({"notify_tools": new})
            return f"🛠 tool notifications: {'on' if new else 'off'}"
        if cmd == "todos":
            new = not st.get("notify_todos", True)
            if not new and st.get("todo_msg_id"):
                tg_delete(st["todo_msg_id"])
                state_write({"todo_msg_id": None, "todo_hash": None, "todo_pinned": False})
            state_write({"notify_todos": new})
            return f"📋 live todo list: {'on' if new else 'off'}"
        if cmd == "q":
            pqs = dict(state_read().get("pending_qs", {}))
            if not pqs:
                return "No pending question."
            if not arg:
                return "Usage: /q <your answer text> (answers the latest pending question)"
            qid = list(pqs.keys())[-1]
            rec = pqs[qid]
            labels = [s.strip() for s in arg.split(",")] if rec.get("multiple") else [arg]
            oc_post(f"/question/{qid}/reply", {"answers": [labels]}, rec.get("directory"))
            pqs.pop(qid, None)
            state_write({"pending_qs": pqs})
            return f"✅ answered: {arg}"
        if cmd in ("allow", "always", "deny"):
            pend = state_read().get("pending_perm")
            if not pend:
                return "No pending permission request."
            reply = {"allow": "once", "always": "always", "deny": "reject"}[cmd]
            oc_post(f"/permission/{pend['id']}/reply", {"reply": reply}, pend.get("directory"))
            state_write({"pending_perm": None})
            return f"✅ replied: {reply}"
    except Exception as e:
        return f"⚠️ {cmd} failed: {e}"
    # not a bridge command → maybe a native opencode slash command (/init, /compact, /review…)
    sid = st.get("session_id")
    if sid and cmd in native_command_names(st.get("directory")):
        try:
            oc_post(f"/session/{sid}/command", {"command": cmd, "arguments": arg},
                    st.get("directory"))
            return f"⚡ /{cmd} {arg}".rstrip()
        except Exception as e:
            return f"⚠️ native /{cmd} failed: {e}"
    return None


# ── Attachments ───────────────────────────────────────────────────────────

INBOX = BASE / "inbox"
TG_BOT_FILE_LIMIT = 20 * 1024 * 1024  # Bot API download ceiling
INBOX_KEEP = 30


def _pick_attachment(msg: dict) -> dict | None:
    """Extract (ext, file_id, mime, filename) from common attachment kinds."""
    def entry(ext, fid, mime, name=None):
        return {"ext": ext, "file_id": fid, "mime": mime or "application/octet-stream", "name": name}

    if msg.get("photo"):
        best = max(msg["photo"], key=lambda p: p.get("file_size") or p.get("width", 0))
        return entry("jpg", best["file_id"], "image/jpeg")
    if msg.get("voice"):
        v = msg["voice"]
        return entry("ogg", v["file_id"], v.get("mime_type", "audio/ogg"))
    if msg.get("audio"):
        a = msg["audio"]
        name = a.get("file_name")
        ext = name.rsplit(".", 1)[-1] if name and "." in name else "mp3"
        return entry(ext, a["file_id"], a.get("mime_type", "audio/mpeg"), name)
    if msg.get("document"):
        d = msg["document"]
        name = d.get("file_name")
        ext = name.rsplit(".", 1)[-1] if name and "." in name else "bin"
        return entry(ext, d["file_id"], d.get("mime_type"), name)
    if msg.get("video"):
        v = msg["video"]
        name = v.get("file_name")
        ext = name.rsplit(".", 1)[-1] if name and "." in name else "mp4"
        return entry(ext, v["file_id"], v.get("mime_type", "video/mp4"), name)
    return None


def tg_download(file_id: str, ext: str, name: str | None) -> Path | None:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=15)
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    data = requests.get(url, timeout=60).content
    INBOX.mkdir(exist_ok=True)
    safe = "".join(c for c in (name or "") if c.isalnum() or c in "._- ")[:60] or f"file.{ext}"
    dest = INBOX / f"{int(time.time())}_{safe}"
    dest.write_bytes(data)
    # rotation: keep the newest INBOX_KEEP files
    files = sorted(INBOX.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[INBOX_KEEP:]:
        old.unlink(missing_ok=True)
    return dest


def human_size(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def handle_attachment(msg: dict) -> None:
    """Download a Telegram attachment and announce it to the attached session."""
    atta = _pick_attachment(msg)
    if not atta:
        tg_send("ℹ️ קיבלתי הודעה מסוג שלא נתמך עדיין (סטיקר/מיקום וכו׳).")
        return
    size = (msg.get("photo") and max(msg["photo"], key=lambda p: p.get("file_size") or 0).get("file_size")) \
        or (msg.get("voice") or msg.get("audio") or msg.get("document") or msg.get("video") or {}).get("file_size")
    if size and size > TG_BOT_FILE_LIMIT:
        tg_send(f"⚠️ הקובץ גדול מדי להורדה דרך הבוט ({human_size(size)} > 20MB).")
        return
    try:
        dest = tg_download(atta["file_id"], atta["ext"], atta.get("name"))
    except Exception as e:
        tg_send(f"⚠️ הורדת הקובץ נכשלה: {e}")
        return
    try:
        caption = (tg_entities_to_markdown(msg.get("caption") or "",
                                           msg.get("caption_entities")) or "").strip()
    except Exception:
        caption = (msg.get("caption") or "").strip()
    ack = f"📎 נשמר בשרת:\n{dest}\n{atta['mime']} · {human_size(dest.stat().st_size)}"
    tg_send(ack)
    st = state_read()
    sid = st.get("session_id")
    if not sid:
        tg_send("🔌 אין סשן מחובר — הקובץ נשמר, אבל לא הועבר לשום סשן.")
        return
    note = (f"[Telegram attachment from the user — saved on server]\n"
            f"path: {dest}\n"
            f"mime: {atta['mime']} · size: {human_size(dest.stat().st_size)}\n"
            + (f"caption: {caption}\n" if caption else "")
            + "Note: the model cannot see/hear files directly — use your tools "
              "(read/parse/convert the file at that path) if the user asks about it.")
    try:
        oc_post(f"/session/{sid}/prompt_async",
                {"parts": [{"type": "text", "text": note}]},
                st.get("directory"))
        inj = state_read().get("injected_texts", [])
        inj.append({"t": note, "ts": time.time()})
        state_write({"injected_texts": inj[-100:]})
    except Exception as e:
        tg_send(f"⚠️ הקובץ נשמר אבל שליחתו לסשן נכשלה: {e}")


# ── Root-dir browser (inline-keyboard mini file picker) ──────────────────
# Opened from the /sessions button (ctx "sessions" → picked dir becomes the
# root_dir override, then the listing is refreshed) or from /new's 📂 button
# (ctx "new" → the new session is created in the picked dir). Browse state
# lives in state.json["dir_browser"]; callbacks are dir:<action>[:payload]
# with only tiny payloads (paths stay server-side, Telegram caps data ≤64B).

DIR_PAGE = 8  # subdirs per browser page


def _dir_entries(path: str) -> tuple[list[str], str | None]:
    """Non-hidden subdirectories of `path` ((names, error))."""
    try:
        names = [e.name for e in Path(path).iterdir()
                 if e.is_dir() and not e.name.startswith(".")]
        return sorted(names, key=str.lower), None
    except Exception as e:
        return [], str(e)


def dir_browser_open(base: str, ctx: str, new_title: str | None = None) -> None:
    state_write({"dir_browser": {"path": base or str(Path.home()), "page": 0,
                                 "entries": [], "ctx": ctx, "new_title": new_title}})


def dir_browser_render() -> tuple[str, dict]:
    """Rebuild the browser message from state; refreshes the entry listing
    (which the dir:cd:<i> callbacks index into) and returns (text, keyboard)."""
    brow = dict(state_read().get("dir_browser") or {})
    path = brow.get("path") or str(Path.home())
    ctx = brow.get("ctx") or "sessions"
    page = int(brow.get("page") or 0)
    dirs, err = _dir_entries(path)
    npages = max(1, (len(dirs) + DIR_PAGE - 1) // DIR_PAGE)
    page = min(page, npages - 1)
    entries = dirs[page * DIR_PAGE:(page + 1) * DIR_PAGE]
    state_write({"dir_browser": {**brow, "path": path, "page": page, "entries": entries}})
    rows = [[{"text": "📁 " + name[:44], "callback_data": f"dir:cd:{i}"}]
            for i, name in enumerate(entries)]
    nav = []
    p = Path(path)
    if p.parent != p:
        nav.append({"text": "⬆️ ..", "callback_data": "dir:up"})
    nav.append({"text": "🏠", "callback_data": "dir:home"})
    if npages > 1:
        if page > 0:
            nav.append({"text": "◀️", "callback_data": f"dir:pg:{page - 1}"})
        nav.append({"text": f"{page + 1}/{npages}", "callback_data": "dir:noop"})
        if page < npages - 1:
            nav.append({"text": "▶️", "callback_data": f"dir:pg:{page + 1}"})
    rows.append(nav)
    action = ("✅ השתמש כתיקיית בסיס לסשנים" if ctx == "sessions"
              else "🆕 צור סשן חדש כאן")
    rows.append([{"text": action, "callback_data": "dir:use"}])
    rows.append([{"text": "❌ ביטול", "callback_data": "dir:stop"}])
    head = ("📂 בחירת תיקיית בסיס לסשנים:" if ctx == "sessions"
            else "📂 בחירת תיקייה לסשן החדש:")
    text = f"{head}\n<b>{html_escape(path)}</b>"
    if err:
        text += f"\n⚠️ {html_escape(err)}"
    elif not dirs:
        text += "\n(אין תתי־תיקיות)"
    return text, {"inline_keyboard": rows}


def handle_dir_cb(cb: dict, data: str) -> None:
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    payload = parts[2] if len(parts) > 2 else ""
    cbid = cb.get("id", "")
    mid = (cb.get("message") or {}).get("message_id")
    st = state_read()

    if action == "noop":
        tg_answer_cb(cbid)
        return
    if action == "browse":  # the 📂 button on the /sessions listing
        dir_browser_open(root_dir(st), payload or "sessions")
    else:
        brow = dict(st.get("dir_browser") or {})
        if not brow:
            tg_answer_cb(cbid, "הדפדפן כבר נסגר")
            if mid:
                tg_edit_kb(mid, "⌛ הדפדפן כבר נסגר — הרץ /sessions או /new מחדש.")
            return
        path = brow.get("path") or str(Path.home())
        if action == "cd":
            try:
                name = (brow.get("entries") or [])[int(payload)]
            except (ValueError, IndexError):
                tg_answer_cb(cbid, "⚠️ הרשימה השתנתה — נסה שוב")
                text, kb = dir_browser_render()
                if mid:
                    tg_edit_kb(mid, text, kb, html=True)
                return
            path = str((Path(path) / name).resolve())
            brow["page"] = 0
        elif action == "up":
            path = str(Path(path).parent)
            brow["page"] = 0
        elif action == "home":
            path = str(Path.home())
            brow["page"] = 0
        elif action == "pg":
            try:
                brow["page"] = int(payload)
            except ValueError:
                brow["page"] = 0
        elif action == "use":
            ctx = brow.get("ctx") or "sessions"
            state_write({"dir_browser": None})
            if ctx == "new":
                title = (brow.get("new_title")
                         or (st.get("pending_new") or {}).get("title") or "telegram")
                state_write({"pending_new": None})
                try:
                    result = create_and_attach(title, path)
                except Exception as e:
                    tg_answer_cb(cbid, "⚠️ נכשל")
                    if mid:
                        tg_edit_kb(mid, f"⚠️ יצירת הסשן נכשלה: {e}")
                    return
                tg_answer_cb(cbid, "🆕 נוצר")
                if mid:
                    tg_edit_kb(mid, result)
            else:  # sessions: adopt as root_dir and refresh the listing
                state_write({"root_dir": path})
                tg_answer_cb(cbid, "📂 " + path[:190])
                try:
                    text, kb = cmd_sessions(state_read())
                except Exception as e:
                    text, kb = f"📂 root dir set: {path}\n⚠️ /sessions failed: {e}", None
                if mid:
                    tg_edit_kb(mid, text, kb)
            return
        elif action == "stop":
            state_write({"dir_browser": None})
            tg_answer_cb(cbid, "בוטל")
            if mid:
                tg_edit_kb(mid, "❌ בחירת התיקייה בוטלה.")
            return
        else:
            return
        state_write({"dir_browser": {**brow, "path": path}})

    # browse / cd / up / home / pg → re-render the browser in place
    text, kb = dir_browser_render()
    tg_answer_cb(cbid)
    if mid:
        tg_edit_kb(mid, text, kb, html=True)


def handle_new_cb(cb: dict, action: str) -> None:
    """Answer the /new root-dir confirmation (new:ok / new:chg / new:no)."""
    cbid = cb.get("id", "")
    mid = (cb.get("message") or {}).get("message_id")
    st = state_read()
    pend = st.get("pending_new")
    if not pend:
        tg_answer_cb(cbid, "הבקשה פגה — הרץ /new מחדש")
        if mid:
            tg_edit_kb(mid, "⌛ בקשת /new פגה — הרץ /new מחדש.")
        return
    if action == "no":
        state_write({"pending_new": None})
        tg_answer_cb(cbid, "בוטל")
        if mid:
            tg_edit_kb(mid, "❌ יצירת הסשן בוטלה.")
        return
    if action == "chg":  # open the dir browser in /new context
        dir_browser_open(pend.get("dir") or root_dir(st), "new", pend.get("title"))
        text, kb = dir_browser_render()
        tg_answer_cb(cbid)
        if mid:
            tg_edit_kb(mid, text, kb, html=True)
        return
    if action == "ok":
        title = pend.get("title") or "telegram"
        d = pend.get("dir") or root_dir(st)
        state_write({"pending_new": None})
        try:
            result = create_and_attach(title, d)
        except Exception as e:
            tg_answer_cb(cbid, "⚠️ נכשל")
            if mid:
                tg_edit_kb(mid, f"⚠️ יצירת הסשן נכשלה: {e}")
            return
        tg_answer_cb(cbid, "🆕 נוצר")
        if mid:
            tg_edit_kb(mid, result)
        return


# ── Model switching (/model) ──────────────────────────────────────────────
# List:   GET /config/providers?directory=… → {"providers": [{"id", "models": {mid: …}}], "default": …}
# Switch: POST /session/{id}/prompt_async {model: {providerID, modelID}, noReply: true, parts: []}
#         (PATCH /session/{id} only accepts {title}; the session's stored model
#         follows the last prompt's model, so a no-reply prompt flips it. The
#         residue textless marker message is deleted best-effort right after.)
#         Verified via GET /session/{id} → model: {id, providerID, variant}.
#         The 📌 banner picks the new model up via its text-compare path.
# Default for NEW sessions: top-level "model" in opencode.jsonc (JSONC — text edit).
# Picker state lives in state.json["model_browser"]; callbacks are tiny
# indexes (model:p:<i> / model:m:<pi>:<mi> / model:back) — never full model ids.

OPENCODE_JSONC = Path.home() / ".config" / "opencode" / "opencode.jsonc"


def fetch_model_catalog(directory: str | None) -> tuple[list[str], dict[str, list[str]]]:
    """(providers, models_by_provider) from GET /config/providers.
    Raises on server/parse failure (caller turns it into a friendly reply)."""
    data = oc_get("/config/providers", directory)
    providers: list[str] = []
    models_by_provider: dict[str, list[str]] = {}
    for p in data.get("providers") or []:
        pid = p.get("id")
        if not pid:
            continue
        providers.append(pid)
        models_by_provider[pid] = sorted((p.get("models") or {}).keys())
    if not providers:
        raise RuntimeError("no providers returned by /config/providers")
    return providers, models_by_provider


def _model_candidates(arg: str, providers: list[str],
                       models_by_provider: dict[str, list[str]], limit: int = 12) -> list[str]:
    full = [f"{p}/{m}" for p in providers for m in models_by_provider.get(p, [])]
    close = difflib.get_close_matches(arg, full, n=limit, cutoff=0.4)
    low = arg.lower()
    sub = [f for f in full if low in f.lower() and f not in close]
    return (close + sub)[:limit]


def _model_candidates_text(arg: str, providers: list[str],
                            models_by_provider: dict[str, list[str]]) -> str:
    cands = _model_candidates(arg, providers, models_by_provider)
    if not cands:
        return "no close matches — /model with no args browses all providers."
    lines = "\n".join(f"• {c}" for c in cands)
    return f"Did you mean:\n{lines}\n\n/model with no args browses all providers."


def resolve_model_arg(arg: str, providers: list[str],
                      models_by_provider: dict[str, list[str]]
                      ) -> tuple[str | None, str | None, str | None]:
    """Parse `<provider>/<model>`, `<provider>:<model>` or a bare `<model>`.

    Returns (providerID, modelID, None) on success, else (None, None, reply)
    with an ambiguity / miss message including close candidates."""
    arg = (arg or "").strip()
    if not arg:
        return None, None, "Usage: /model <provider>/<model>  (or /model with no args to browse)"
    for sep in ("/", ":"):
        if sep in arg:
            p, _, m = arg.partition(sep)
            p, m = p.strip(), m.strip()
            if p in models_by_provider and m in models_by_provider[p]:
                return p, m, None
            if p not in models_by_provider:
                known = ", ".join(providers)
                return (None, None,
                        f"⚠️ unknown provider: {p}\nproviders: {known}\n"
                        + _model_candidates_text(arg, providers, models_by_provider))
            return (None, None,
                    f"⚠️ unknown model: {arg}\n"
                    + _model_candidates_text(arg, providers, models_by_provider))
    # bare model id: exact match first, then suffix match across providers
    exact = [(p, m) for p in providers for m in models_by_provider.get(p, []) if m == arg]
    if len(exact) == 1:
        return exact[0][0], exact[0][1], None
    if len(exact) > 1:
        lines = "\n".join(f"• {p}/{m}" for p, m in exact)
        return (None, None,
                f"⚠️ ambiguous model {arg!r} — in {len(exact)} providers, be explicit:\n{lines}")
    suf = [(p, m) for p in providers for m in models_by_provider.get(p, [])
           if m.split("/")[-1] == arg or m.endswith("/" + arg)]
    if len(suf) == 1:
        return suf[0][0], suf[0][1], None
    if len(suf) > 1:
        lines = "\n".join(f"• {p}/{m}" for p, m in suf)
        return (None, None,
                f"⚠️ ambiguous model {arg!r} — matches in {len(suf)} providers, be explicit:\n{lines}")
    return (None, None,
            f"⚠️ unknown model: {arg}\n" + _model_candidates_text(arg, providers, models_by_provider))


def switch_session_model(sid: str, directory: str | None,
                         provider_id: str, model_id: str) -> None:
    """Flip a session's stored model via a no-reply prompt, then delete the
    residue textless marker message best-effort (history stays clean).
    Raises on switch failure; cleanup failure is only logged."""
    oc_post(f"/session/{sid}/prompt_async",
            {"model": {"providerID": provider_id, "modelID": model_id},
             "noReply": True, "parts": []},
            directory)
    try:
        now_ms = time.time() * 1000
        msgs = oc_get(f"/session/{sid}/message", directory, limit=10)
        best: tuple[float, str] | None = None
        for m in msgs:
            info = m.get("info") or {}
            if info.get("role") != "user":
                continue
            created = info.get("time", {}).get("created", 0) or 0
            if now_ms - created > 120_000:
                continue  # not ours — never touch older messages
            parts = m.get("parts") or []
            if any((p.get("text") or "").strip() for p in parts if p.get("type") == "text"):
                continue
            if any(p.get("type") == "tool" for p in parts):
                continue
            if best is None or created > best[0]:
                best = (created, info.get("id"))
        if best and best[1]:
            oc_delete(f"/session/{sid}/message/{best[1]}", directory)
    except Exception as e:
        log("model-switch cleanup failed:", e)


def cmd_model_switch(st: dict, arg: str) -> str:
    sid = st.get("session_id")
    if not sid:
        return "🔌 No session attached — /sessions to pick one, or /new."
    try:
        providers, models_by_provider = fetch_model_catalog(st.get("directory"))
    except Exception as e:
        return f"⚠️ model list failed: {e}"
    pid, mid, reply = resolve_model_arg(arg, providers, models_by_provider)
    if reply:
        return reply
    try:
        switch_session_model(sid, st.get("directory"), pid, mid)
    except Exception as e:
        return f"⚠️ model switch failed: {e}"
    full = f"{pid}/{mid}"
    return (f"🔀 model switched: {short_model_name(full)} ({full})\n"
            "📌 banner updates in a few seconds.")


def cmd_model_picker(st: dict) -> tuple[str, dict | None]:
    """Bare /model: provider picker (model list on next tap)."""
    try:
        providers, models_by_provider = fetch_model_catalog(st.get("directory"))
    except Exception as e:
        return f"⚠️ model list failed: {e}", None
    state_write({"model_browser": {"providers": providers,
                                   "models_by_provider": models_by_provider}})
    rows = [[{"text": p, "callback_data": f"model:p:{i}"}]
            for i, p in enumerate(providers)]
    return "🔀 pick a provider:", {"inline_keyboard": rows}


def model_providers_kb(brow: dict) -> dict:
    rows = [[{"text": p, "callback_data": f"model:p:{i}"}]
            for i, p in enumerate(brow.get("providers") or [])]
    return {"inline_keyboard": rows}


def model_models_kb(brow: dict, pi: int) -> tuple[str, dict]:
    p = brow["providers"][pi]
    models = (brow.get("models_by_provider") or {}).get(p, [])
    rows = [[{"text": m[:60], "callback_data": f"model:m:{pi}:{mi}"}]
            for mi, m in enumerate(models)]
    rows.append([{"text": "⬅️ providers", "callback_data": "model:back"}])
    return p, {"inline_keyboard": rows}


def handle_model_cb(cb: dict, data: str) -> None:
    """model:p:<i> → model list; model:m:<pi>:<mi> → switch now; model:back → providers."""
    parts = data.split(":")
    kind = parts[1] if len(parts) > 1 else ""
    cbid = cb.get("id", "")
    mid = (cb.get("message") or {}).get("message_id")
    brow = dict(state_read().get("model_browser") or {})
    if not brow or not brow.get("providers"):
        tg_answer_cb(cbid, "browser expired — run /model again")
        if mid:
            tg_edit_kb(mid, "⌛ model browser expired — run /model again.")
        return
    try:
        if kind == "back":
            tg_answer_cb(cbid)
            if mid:
                tg_edit_kb(mid, "🔀 pick a provider:", model_providers_kb(brow))
            return
        if kind == "p":
            pi = int(parts[2])
            p, kb = model_models_kb(brow, pi)
            tg_answer_cb(cbid, p[:190])
            if mid:
                tg_edit_kb(mid, f"🔀 {p} — pick a model:", kb)
            return
        if kind == "m":
            pi, mi = int(parts[2]), int(parts[3])
            p = brow["providers"][pi]
            m = (brow.get("models_by_provider") or {}).get(p, [])[mi]
            st = state_read()
            sid = st.get("session_id")
            if not sid:
                tg_answer_cb(cbid, "⚠️ not attached")
                if mid:
                    tg_edit_kb(mid, "🔌 No session attached — /sessions to pick one, or /new.")
                return
            switch_session_model(sid, st.get("directory"), p, m)
            state_write({"model_browser": None})
            full = f"{p}/{m}"
            tg_answer_cb(cbid, f"🔀 {short_model_name(full)}")
            if mid:
                tg_edit_kb(mid, f"🔀 model switched: {short_model_name(full)} ({full})\n"
                                "📌 banner updates in a few seconds.")
            return
    except (ValueError, IndexError):
        tg_answer_cb(cbid, "⚠️ list changed — run /model again")
        return
    except Exception as e:
        tg_answer_cb(cbid, "⚠️ failed")
        if mid:
            tg_edit_kb(mid, f"⚠️ model switch failed: {e}")
        return
    tg_answer_cb(cbid)


def cmd_model_config_show() -> str:
    try:
        raw = OPENCODE_JSONC.read_text()
    except Exception as e:
        return f"⚠️ model config read failed: {e}"
    m = re.search(r'"model"\s*:\s*"([^"]+)"', raw)
    if m:
        return ("⚙️ default model for NEW sessions: " + m.group(1) +
                "\n(/model --config <provider>/<model> to change)")
    return "⚙️ no top-level \"model\" set — new sessions use the server default."


def cmd_model_config_set(spec: str) -> str:
    try:
        providers, models_by_provider = fetch_model_catalog(state_read().get("directory"))
    except Exception as e:
        return f"⚠️ model list failed: {e}"
    pid, mid, reply = resolve_model_arg(spec, providers, models_by_provider)
    if reply:
        return reply
    full = f"{pid}/{mid}"
    try:
        raw = OPENCODE_JSONC.read_text()
    except Exception as e:
        return f"⚠️ model config read failed: {e}"
    bak = OPENCODE_JSONC.with_name(f"opencode.jsonc.bak-model-{int(time.time())}")
    try:
        bak.write_text(raw)
    except Exception as e:
        return f"⚠️ model config backup failed: {e}"
    # replace the top-level "model" line (shallowest indent wins — never a nested key),
    # or insert after the opening brace when the key is missing
    found = sorted(re.finditer(r'^([ \t]*)"model"\s*:\s*"[^"]*"', raw, re.M),
                   key=lambda m: len(m.group(1)))
    if found:
        t = found[0]
        new_raw = raw[:t.start()] + f'{t.group(1)}"model": "{full}"' + raw[t.end():]
    else:
        new_raw = raw.replace("{", "{\n  \"model\": \"" + full + "\",", 1)
    try:
        OPENCODE_JSONC.write_text(new_raw)
    except Exception as e:
        return f"⚠️ model config write failed: {e}"
    return (f"⚙️ default model for NEW sessions: {full}\n"
            f"(backup: {bak.name}; current session untouched — /model {full} to switch it)")


def cmd_model(st: dict, arg: str) -> str | tuple[str, dict] | None:
    """Three forms: bare → provider picker; <spec> → switch now;
    --config [...] → show/set the default for NEW sessions."""
    if not arg:
        return cmd_model_picker(st)
    if arg == "--config" or arg.startswith("--config "):
        rest = arg[len("--config"):].strip()
        if not rest:
            return cmd_model_config_show()
        return cmd_model_config_set(rest)
    return cmd_model_switch(st, arg)


# ── Telegram inbound loop ─────────────────────────────────────────────────

PERM_LABELS = {"once": "✅ Once", "always": "🟢 Always", "reject": "🚫 Denied"}


def handle_callback(cb: dict) -> None:
    """Handle an inline-keyboard tap: reply to the opencode permission,
    remove the buttons, and mark the original message with the decision."""
    if (cb.get("from") or {}).get("id") != ADMIN_ID:
        return
    if ((cb.get("message") or {}).get("chat") or {}).get("id") != ADMIN_ID:
        return  # only our DM — never trust other chats
    data = cb.get("data") or ""
    if data.startswith("q:"):
        _, qid, sidx = data.split(":", 2)
        idx = int(sidx)
        pend = dict(state_read().get("pending_qs", {}))
        rec = pend.get(qid) or {}
        err = None
        try:
            if idx == -1:
                oc_post(f"/question/{qid}/reject", {}, rec.get("directory"))
                label = "🚫 נדחה"
            else:
                label = (rec.get("labels") or ["?"])[idx]
                oc_post(f"/question/{qid}/reply", {"answers": [[label]]}, rec.get("directory"))
                label = f"✅ {label}"
        except Exception as e:
            err = e
        try:
            requests.post(f"{TG_API}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"],
                                "text": f"{'⚠️ ' + str(err)[:80] if err else label}"}, timeout=10)
        except Exception:
            pass
        msg = cb.get("message") or {}
        orig = msg.get("text") or ""
        suffix = f"\n\n⚠️ reply failed: {err}" if err else f"\n\n→ {label}"
        try:
            requests.post(f"{TG_API}/editMessageText",
                          json={"chat_id": ADMIN_ID, "message_id": msg.get("message_id"),
                                "text": (orig + suffix)[:TG_MAX], "parse_mode": "HTML",
                                "reply_markup": {"inline_keyboard": []}},
                          timeout=10)
        except Exception:
            pass
        if not err:
            pend.pop(qid, None)
            state_write({"pending_qs": pend})
        return
    if data.startswith("hist:"):
        shown = int(data.split(":", 1)[1])
        try:
            requests.post(f"{TG_API}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"],
                                "text": "טוען הודעות קודמות…"}, timeout=10)
        except Exception:
            pass
        state_write({"reseed": True, "replay_tail": shown + REPLAY_TAIL})
        return
    if data.startswith("dir:"):
        handle_dir_cb(cb, data)
        return
    if data.startswith("new:"):
        handle_new_cb(cb, data.split(":", 1)[1])
        return
    if data.startswith("model:"):
        handle_model_cb(cb, data)
        return
    if not data.startswith("perm:"):
        return
    _, reply, reqid = data.split(":", 2)
    pend = state_read().get("pending_perm") or {}
    err = None
    try:
        oc_post(f"/permission/{reqid}/reply", {"reply": reply}, pend.get("directory"))
    except Exception as e:
        err = e
    # stop the button's loading spinner
    try:
        requests.post(f"{TG_API}/answerCallbackQuery",
                      json={"callback_query_id": cb["id"],
                            "text": f"{'⚠️ failed: ' + str(err)[:80] if err else PERM_LABELS[reply]}"},
                      timeout=10)
    except Exception:
        pass
    # replace buttons with the decision (inline on the original message)
    msg = cb.get("message") or {}
    orig = msg.get("text") or ""
    suffix = f"\n\n⚠️ reply failed: {err}" if err else f"\n\n→ {PERM_LABELS[reply]}"
    try:
        requests.post(f"{TG_API}/editMessageText",
                      json={"chat_id": ADMIN_ID, "message_id": msg.get("message_id"),
                            "text": (orig + suffix)[:TG_MAX], "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": []}},
                      timeout=10)
    except Exception:
        pass
    if not err:
        state_write({"pending_perm": None})


def telegram_loop() -> None:
    offset = state_read().get("offset", 0)
    log("telegram poll loop started")
    while True:
        try:
            updates = tg_updates(offset)
        except Exception as e:
            log("getUpdates error:", e)
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            state_write({"offset": offset})
            if upd.get("callback_query"):
                try:
                    handle_callback(upd["callback_query"])
                except Exception as e:
                    log("callback error:", e)
                continue
            msg = upd.get("message") or {}
            if (msg.get("from") or {}).get("id") != ADMIN_ID:
                continue
            if (msg.get("chat") or {}).get("id") != ADMIN_ID:
                log("ignored foreign-chat msg from chat", (msg.get("chat") or {}).get("id"))
                continue  # ignore anything outside the private DM (e.g. groups)
            if msg.get("message_id") and msg["message_id"] > (state_read().get("max_tg_msg_id") or 0):
                state_write({"max_tg_msg_id": msg["message_id"]})
            raw_text = msg.get("text") or ""
            text = raw_text.strip()
            # Telegram formatting (bold/code/links…) → markdown for the session
            try:
                formatted_text = (tg_entities_to_markdown(raw_text, msg.get("entities")) or "").strip()
            except Exception as e:
                log("entity→md conversion failed, sending raw:", e)
                formatted_text = text
            if not text:
                if any(k in msg for k in ("photo", "voice", "audio", "document", "video", "sticker")):
                    try:
                        handle_attachment(msg)
                    except Exception as e:
                        log("attachment error:", e)
                continue
            log("tg >", text[:80])
            if text.startswith("/"):
                reply = handle_command(text)
                if reply is not None:
                    if isinstance(reply, tuple):  # (text, reply_markup) — buttoned reply
                        text_out, kb = reply
                    else:
                        text_out, kb = reply, None
                    try:
                        tg_send(text_out, reply_markup=kb)
                    except Exception as e:
                        log("tg send error:", e)
                    continue
            # plain text → attached session
            st = state_read()
            sid = st.get("session_id")
            if not sid:
                try:
                    tg_send("🔌 No session attached. /sessions to pick one, or /new.")
                except Exception:
                    pass
                continue
            try:
                oc_post(f"/session/{sid}/prompt_async",
                        {"parts": [{"type": "text", "text": formatted_text}]},
                        st.get("directory"))
                inj = state_read().get("injected_texts", [])
                inj.append({"t": formatted_text, "ts": time.time()})
                state_write({"injected_texts": inj[-100:]})
                tg_send("⏳ sent to session.")
            except Exception as e:
                log("prompt error:", e)
                try:
                    tg_send(f"⚠️ failed to reach session: {e}")
                except Exception:
                    pass


# ── Attach: wipe chat + replay the session's history ──────────────────────

REPLAY_TAIL = 15  # default window of turn-groups replayed on attach


def build_turn_groups(msgs: list[dict]) -> list[dict]:
    """Collapse consecutive same-role messages into turn groups
    (the user's messages 🧍 in one bubble, the agent's run in one bubble)."""
    groups: list[dict] = []
    for m in msgs:
        role = m["info"]["role"]
        if role not in ("user", "assistant"):
            continue
        texts = [p.get("text", "").strip() for p in m.get("parts", [])
                 if p.get("type") == "text" and p.get("text", "").strip()]
        tools = [p for p in m.get("parts", []) if p.get("type") == "tool"]
        if not texts and not tools:
            continue
        entry = {"id": m["info"]["id"], "texts": texts, "tools": tools}
        if groups and groups[-1]["role"] == role:
            groups[-1]["items"].append(entry)
        else:
            groups.append({"role": role, "items": [entry]})
    return groups


def _tg_post(method: str, body: dict, timeout: int = 15) -> dict:
    r = requests.post(f"{TG_API}/{method}", json=body, timeout=timeout)
    if r.status_code == 429:  # flood control — honor the wait Telegram asks for
        wait = r.json().get("parameters", {}).get("retry_after", 5)
        log(f"429 on {method}, waiting {wait}s")
        time.sleep(min(wait, 60))
    return r.json() if r.content else {}


def wipe_chat() -> tuple[int, int | None]:
    """Delete ALL messages in the DM: full-range sweep (floor..max+margin].
    Bulk deleteMessages (≤100/call), per-id fallback, 429 honored.

    Telegram hard limit (deleteMessage docs): a bot can ONLY delete messages
    younger than 48 hours ("Bad Request: message can't be deleted for
    everyone"). Older messages are fossils — undeletable via the API, ever.
    Message ids within a chat are time-ordered, so anything older than a
    confirmed fossil is a fossil (or a never-used gap) too: `wipe_floor` in
    state.json stores the highest confirmed fossil id, and future wipes skip
    it — otherwise every wipe would waste ~100 single calls per fossil chunk.
    `fossil_count` accumulates confirmed undeletable messages (internal
    bookkeeping only — nothing is reported to the user). Both reset
    when the floor probe finds the fossil gone (user cleared history by hand)
    or when the bot token changes. Fossils stay visible in the app; only the
    user can purge them (chat menu → Clear history).

    Returns (deleted, remaining): remaining is the total known fossil count,
    None when fossils provably exist (floor held) but were recorded before
    counting landed, 0 when the chat is fully clean."""
    st = state_read()
    hi = (st.get("max_tg_msg_id") or 0) + 1000  # generous margin for msgs sent while bridge was down
    floor = int(st.get("wipe_floor") or 0)
    original_floor = floor
    known_fossils = st.get("fossil_count")  # int | None
    if floor:
        # Self-heal: if the floor fossil is gone (user cleared history by
        # hand, or it was a never-used gap), reset the floor so this sweep
        # covers the full range again. On network trouble keep the floor —
        # next wipe retries the probe.
        try:
            r = _tg_post("deleteMessage", {"chat_id": ADMIN_ID, "message_id": floor}, timeout=10)
        except Exception as e:
            log("wipe: floor probe failed:", e)
            r = {"description": "can't be deleted"}  # keep floor, sweep as usual
        if "can't be deleted" not in (r.get("description") or ""):
            log(f"wipe: floor {floor} is gone — resetting, full sweep from 1")
            floor = 0
            known_fossils = 0
    lo = floor + 1
    if lo > hi:
        log(f"wipe: nothing to sweep (floor {floor}, hi {hi})")
        if known_fossils is None and floor:
            return 0, None  # fossils exist but count unknown
        return 0, known_fossils or 0
    try:  # fossil banners can't be deleted, but they CAN be unpinned
        requests.post(f"{TG_API}/unpinAllChatMessages", json={"chat_id": ADMIN_ID}, timeout=15)
    except Exception:
        pass
    deleted = fossils = 0
    new_floor = floor
    ids = range(lo, hi + 1)
    for i in range(0, len(ids), 100):
        chunk = list(ids[i:i + 100])
        res = _tg_post("deleteMessages", {"chat_id": ADMIN_ID, "message_ids": chunk}, timeout=30)
        if res.get("ok"):
            deleted += len(chunk)  # incl. never-used ids (skipped server-side)
        else:  # ≥1 fossil (or quirk) in chunk → per-id fallback
            for mid in chunk:
                r = _tg_post("deleteMessage", {"chat_id": ADMIN_ID, "message_id": mid}, timeout=10)
                if r.get("ok"):
                    deleted += 1
                elif "can't be deleted" in r.get("description", ""):
                    fossils += 1
                    new_floor = max(new_floor, mid)  # ids are time-ordered
                # "message to delete not found" = id never used — ignore
                time.sleep(0.03)
        time.sleep(0.05)
    patch: dict = {"known_ids": []}
    if new_floor != original_floor:
        patch["wipe_floor"] = new_floor
    remaining: int | None = (known_fossils or 0) + fossils
    if known_fossils is None and new_floor:
        remaining = None  # old fossils exist below the floor, count unknown
    if remaining != st.get("fossil_count"):
        patch["fossil_count"] = remaining
    state_write(patch)
    log(f"wipe: swept {lo}..{hi} — deleted ~{deleted}, {fossils} new undeletable (>48h, Telegram limit)"
        + (f", floor → {new_floor}" if new_floor != floor else "")
        + (f", {known_fossils} fossils carried over" if known_fossils else ""))
    return deleted, remaining


def attach_replay(sid: str, d: str | None) -> None:
    """Wipe the chat, then replay the attached session's LAST `REPLAY_TAIL`
    turn-groups in order. A "⬆️ load earlier" button (rebuilds a bigger
    window) sits on top if more history exists. User turns are prefixed
    with 👤; the agent's run goes via the bot. Everything in the FULL
    history is marked seen so the live mirror never re-sends old messages."""
    tail = int(state_read().get("replay_tail") or REPLAY_TAIL)
    msgs = oc_get(f"/session/{sid}/message", d, limit=1000)
    msgs.sort(key=lambda m: m["info"].get("time", {}).get("created", 0))
    groups = build_turn_groups(msgs)
    shown = groups[-tail:]
    remaining = max(0, len(groups) - len(shown))
    wipe_chat()
    if remaining:
        kb = {"inline_keyboard": [[
            {"text": f"⬆️ טען הודעות קודמות ({remaining} נותרו)",
             "callback_data": f"hist:{len(shown)}"}]]}
        tg_send(f"📜 מוצגות {len(shown)} הפניות האחרונות של הסשן", reply_markup=kb)
    last_sig = None
    for g in shown:
        if g["role"] == "user":
            text = "\n\n".join(t for it in g["items"] for t in it["texts"]
                               if not t.startswith(REMOTE_MARKER))
            if text.strip():
                tg_send("👤 " + text)
        else:
            lines = []
            plain_parts = []
            for it in g["items"]:
                for p in it["tools"]:
                    sig = tool_sig(p)
                    if sig is not None and sig == last_sig:
                        continue
                    line = fmt_tool(p)
                    if line:
                        lines.append(line)
                        last_sig = sig
                plain_parts.extend(it["texts"])
            if lines:
                body = "\n".join(lines[:20]) + (f"\n… (+{len(lines) - 20})" if len(lines) > 20 else "")
                if plain_parts:
                    body += "\n\n" + markdown_to_tg_html("\n\n".join(plain_parts))
                tg_send(body, html=True)
            elif plain_parts:
                tg_send_md("\n\n".join(plain_parts))
        time.sleep(0.06)
    # mark the FULL history seen — replayed or not, never re-send live
    seen_a = [m["info"]["id"] for m in msgs if m["info"]["role"] == "assistant"]
    seen_t = [p["id"] for m in msgs for p in m.get("parts", []) if p.get("type") == "tool"]
    seen_u = [
        m["info"]["id"] for m in msgs
        if m["info"]["role"] == "user"
        and not any(p.get("type") == "text" and p.get("text", "").strip().startswith(REMOTE_MARKER)
                    for p in m.get("parts", []))
    ]
    state_write({"seen_msgs": seen_a[-500:], "seen_tools": seen_t[-500:],
                 "seen_user": seen_u[-500:],
                 "last_tool_sig": last_sig, "reseed": False, "replay_tail": None,
                 # pinned/todo messages were wiped — force recreation
                 "session_banner_text": None, "session_msg_id": None,
                 "todo_msg_id": None, "todo_hash": None, "todo_pinned": False})
    log(f"attach replay: wiped, showed last {len(shown)} turn-groups of {len(msgs)} messages")


# ── Detach cleanup ────────────────────────────────────────────────────────

def detach_cleanup() -> None:
    """Runs once on every attach→detached transition — /detach in Telegram,
    /remote off in Telegram, or /remote toggled off in the TUI (the plugin
    writes session_id=null; the watcher spots the transition either way).
    Wipes the whole DM and stays SILENT, so the chat is left clean & empty."""
    try:
        wipe_chat()
    except Exception as e:
        log("detach wipe failed:", e)
    state_write({"session_msg_id": None, "session_msg_sid": None,
                 "session_title": None, "session_banner_text": None,
                 "todo_msg_id": None, "todo_hash": None, "todo_pinned": False,
                 "pending_perm": None, "pending_new": None, "dir_browser": None,
                 "model_browser": None,
                 "pending_qs": {}, "children": [], "children_initialized": False})


# ── opencode → Telegram watcher ───────────────────────────────────────────

def watcher_loop() -> None:
    log("watcher loop started")
    known_session = None
    failed_since = None
    while True:
        time.sleep(POLL)
        try:
            st = state_read()
            sid = st.get("session_id")
            if not sid:
                if known_session:  # real detach event (/detach, /remote off, TUI /remote off)
                    known_session = None
                    detach_cleanup()  # wipe the chat, leave a clean marker
                elif st.get("session_msg_id"):  # bridge restart while unattached
                    tg_delete(st["session_msg_id"])
                    state_write({"session_msg_id": None, "session_msg_sid": None,
                                 "session_title": None})
                continue
            d = st.get("directory")

            # (re)seed on attach / restart
            if sid != known_session or st.get("reseed"):
                if st.get("reseed"):
                    # real attach event (/remote, /attach, /new): wipe + replay history
                    attach_replay(sid, d)
                else:
                    # bridge restart, same session: quiet seed, no wipe
                    msgs = oc_get(f"/session/{sid}/message", d, limit=MSG_WINDOW)
                    seen_a = set()
                    seen_t = set()
                    seen_u = set()
                    for m in msgs:
                        info = m["info"]
                        if info["role"] == "user" and not any(
                                p.get("type") == "text" and p.get("text", "").strip().startswith(REMOTE_MARKER)
                                for p in m.get("parts", [])):
                            seen_u.add(info["id"])
                        if info["role"] == "assistant" and info.get("time", {}).get("completed"):
                            seen_a.add(info["id"])
                        for p in m.get("parts", []):
                            if p.get("type") == "tool":
                                seen_t.add(p["id"])
                    state_write({"seen_msgs": sorted(seen_a)[-500:],
                                 "seen_tools": sorted(seen_t)[-500:],
                                 "seen_user": sorted(seen_u)[-500:]})
                known_session = sid
                st = state_read()  # re-read: attach_replay just wiped/reset ids
                # pinned banner with the active session name + model
                try:
                    _sess = oc_get(f"/session/{sid}", d)
                    title = _sess.get("title") or "untitled"
                    _model = session_model_from_obj(_sess)
                except Exception:
                    title = "untitled"
                    _model = None
                try:
                    text = session_banner_text(title, sid, d, model=_model)
                except Exception as e:
                    log("banner build failed:", e)
                    text = session_banner_text(title, sid, d)
                state_write({"session_title": title})
                if st.get("session_msg_sid") != sid:
                    # different session → replace the banner
                    if st.get("session_msg_id"):
                        tg_delete(st["session_msg_id"])
                    mid = tg_send(text)
                    if mid:
                        tg_pin(mid)
                    state_write({"session_msg_id": mid, "session_msg_sid": sid,
                                 "session_banner_text": text})
                elif text != st.get("session_banner_text"):
                    # same session, banner text stale (title/format changed)
                    mid = st.get("session_msg_id")
                    if not (mid and tg_edit(mid, text)):
                        if mid:
                            tg_delete(mid)
                        mid = tg_send(text)
                        if mid:
                            tg_pin(mid)
                        state_write({"session_msg_id": mid})
                    state_write({"session_banner_text": text})
                continue

            msgs = oc_get(f"/session/{sid}/message", d, limit=MSG_WINDOW)
            msgs.sort(key=lambda m: m["info"].get("time", {}).get("created", 0))

            # 0) keep the pinned banner in sync: title + token count (updated
            #    when the model finishes) + "thinking..." while it works
            try:
                _sess = oc_get(f"/session/{sid}", d)
                cur_title = _sess.get("title") or "untitled"
                try:
                    _model = session_model_from_obj(_sess)
                except Exception as e:
                    log("session model lookup failed:", e)
                    _model = None
                busy = (oc_get("/session/status", d).get(sid) or {}).get("type") == "busy"
                tokens = None
                for m in reversed(msgs):
                    if m["info"]["role"] != "assistant":
                        continue
                    t = m["info"].get("tokens") or {}
                    tot = (t.get("input", 0) + t.get("output", 0) + t.get("reasoning", 0)
                           + (t.get("cache") or {}).get("read", 0))
                    if tot:
                        tokens = tot
                        break
                text = session_banner_text(cur_title, sid, d, tokens, busy, _model)
                if text != st.get("session_banner_text"):
                    mid = st.get("session_msg_id")
                    if mid and tg_edit(mid, text):
                        state_write({"session_title": cur_title, "session_banner_text": text})
                    elif mid := tg_send(text):  # banner lost → recreate
                        tg_pin(mid)
                        state_write({"session_msg_id": mid, "session_msg_sid": sid,
                                     "session_title": cur_title, "session_banner_text": text})
            except Exception:
                pass

            # 2) completed assistant texts
            # 1) tool-call notifications (batched per poll, streak-deduped)
            seen_tools = set(st.get("seen_tools", []))
            new_tools = set(seen_tools)
            last_sig = st.get("last_tool_sig")
            if st.get("notify_tools", True):
                events = []
                for m in msgs:
                    for p in m.get("parts", []):
                        if p.get("type") == "tool" and p["id"] not in seen_tools:
                            new_tools.add(p["id"])
                            sig = tool_sig(p)
                            if sig is not None and sig == last_sig:
                                continue  # same action streak — already reported
                            line = fmt_tool(p)
                            if line:
                                events.append(line)
                                last_sig = sig
                if events:
                    body = "\n".join(events[:12])
                    if len(events) > 12:
                        body += f"\n… (+{len(events) - 12} more)"
                    tg_send(body, html=True)
                    state_write({"last_tool_sig": last_sig})

            # 2) completed assistant texts
            seen_msgs = set(st.get("seen_msgs", []))
            new_msgs = set(seen_msgs)
            for m in msgs:
                info = m["info"]
                if info["role"] != "assistant":
                    continue
                mid = info["id"]
                if mid in seen_msgs or not info.get("time", {}).get("completed"):
                    continue
                texts = [p.get("text", "").strip() for p in m.get("parts", [])
                         if p.get("type") == "text" and p.get("text", "").strip()]
                if texts:
                    tg_send_md("\n\n".join(texts))
                new_msgs.add(mid)

            if new_tools != seen_tools or new_msgs != seen_msgs:
                state_write({"seen_tools": sorted(new_tools)[-500:],
                             "seen_msgs": sorted(new_msgs)[-500:]})

            # 2b) mirror user turns typed in the TUI (👤) — skip our own
            #     telegram forwards AND /remote template turns
            seen_user = set(st.get("seen_user", []))
            inj_texts = {e.get("t") for e in st.get("injected_texts", [])
                         if time.time() - e.get("ts", 0) < 900}
            new_user = set(seen_user)
            cleanup = dict(st.get("cleanup_remote", {}))
            for m in msgs:
                info = m["info"]
                if info["role"] != "user":
                    continue
                mid = info["id"]
                if mid in seen_user:
                    continue
                new_user.add(mid)
                texts = [p.get("text", "").strip() for p in m.get("parts", [])
                         if p.get("type") == "text" and p.get("text", "").strip()]
                if not texts:
                    continue
                text = "\n\n".join(texts)
                if text.startswith(REMOTE_MARKER):
                    cleanup[mid] = {"sid": sid, "directory": d,
                                    "created": info.get("time", {}).get("created", 0),
                                    "ts": time.time()}
                    log("remote-turn cleanup queued:", mid[-6:])
                    continue
                if text in inj_texts:
                    continue  # this message came FROM telegram — don't bounce it back
                tg_send("👤 " + text)
                log("mirror tui>:", text[:60].replace("\n", " "))
            if new_user != seen_user or cleanup != st.get("cleanup_remote", {}):
                state_write({"seen_user": sorted(new_user)[-500:], "cleanup_remote": cleanup})

            # 2c) /remote turn cleanup: once the turn completed, delete the
            #     template user msg + the agent's reply — zero context residue
            cleanup = dict(st.get("cleanup_remote", {}))
            if cleanup:
                remaining = {}
                for uid, rec in cleanup.items():
                    if time.time() - rec.get("ts", 0) > 1800:
                        continue  # stale → drop
                    done = False
                    for m in msgs:
                        info = m["info"]
                        it = info.get("time", {})
                        if info["role"] == "assistant" and it.get("created", 0) > rec["created"] \
                                and it.get("completed"):
                            for delid in (uid, info["id"]):
                                try:
                                    r = requests.delete(f"{SERVER}/session/{rec['sid']}/message/{delid}",
                                                        params={"directory": rec.get("directory")} if rec.get("directory") else {},
                                                        timeout=15)
                                    log("remote-turn deleted:", delid[-6:], "->", r.status_code)
                                except Exception as e:
                                    log("remote-turn cleanup delete failed:", e)
                            done = True
                    if not done:
                        remaining[uid] = rec
                if remaining != cleanup:
                    state_write({"cleanup_remote": remaining})

            # 3) live todo list (single edited + pinned message)
            if st.get("notify_todos", True):
                try:
                    todos = oc_get(f"/session/{sid}/todo", d)
                except Exception:
                    todos = None
                if todos is not None:
                    h = todos_hash(todos) if todos else ""
                    if h != (st.get("todo_hash") or ""):
                        if not todos:
                            if st.get("todo_msg_id"):
                                tg_delete(st["todo_msg_id"])
                            state_write({"todo_hash": h, "todo_msg_id": None, "todo_pinned": False})
                        else:
                            text = render_todo(todos)
                            mid = st.get("todo_msg_id")
                            if mid and tg_edit(mid, text):
                                state_write({"todo_hash": h})
                            else:
                                mid = tg_send(text)
                                state_write({"todo_hash": h, "todo_msg_id": mid, "todo_pinned": False})
                                if mid and tg_pin(mid):
                                    state_write({"todo_pinned": True})
                            done = todos_done(todos)
                            pinned = bool(state_read().get("todo_pinned"))
                            if done and pinned:
                                tg_unpin(mid)
                                state_write({"todo_pinned": False})
                            elif not done and not pinned and mid:
                                # fresh work after a completed round → re-pin
                                if tg_pin(mid):
                                    state_write({"todo_pinned": True})

            # 4) permission requests (with action buttons) — from ANY session
            try:
                perms = oc_get("/permission", d)
            except Exception:
                perms = []
            notified = set(st.get("notified_perms", []))
            for p in perms or []:
                pid = p.get("id")
                if pid in notified:
                    continue
                pretty = p.get("permission", "permission")
                ssid = (p.get("sessionID") or "")[-6:]
                patterns = ", ".join(p.get("patterns", []))
                kb = {"inline_keyboard": [[
                    {"text": "✅ Once", "callback_data": f"perm:once:{pid}"},
                    {"text": "🟢 Always", "callback_data": f"perm:always:{pid}"},
                    {"text": "🚫 Deny", "callback_data": f"perm:reject:{pid}"},
                ]]}
                tg_send(f"⚠️ permission requested (סשן …{ssid})\n🔧 {html_escape(str(pretty))}\n<code>{html_escape(patterns)}</code>",
                        html=True, reply_markup=kb)
                state_write({"pending_perm": {"id": pid, "directory": d},
                             "notified_perms": sorted(notified | {pid})[-100:]})
                notified.add(pid)

            # 5) agent questions (option buttons, /q for free text) — any session
            try:
                pendings_q = oc_get("/question", d)
            except Exception:
                pendings_q = []
            notified_q = set(st.get("notified_qs", []))
            pqs = dict(st.get("pending_qs", {}))
            for q in pendings_q or []:
                qid = q.get("id")
                if qid in notified_q:
                    continue
                qlist = q.get("questions") or []
                ssid = (q.get("sessionID") or "")[-6:]
                if len(qlist) != 1:
                    tg_send(f"❓ שאלה מורכבת ({len(qlist)} תתי־שאלות) מהסוכן, סשן …{ssid}\nענה בטקסט חופשי עם /q <answer>")
                    pqs[qid] = {"directory": d, "labels": None, "question": "", "multiple": False}
                else:
                    q1 = qlist[0]
                    opts = q1.get("options") or []
                    multiple = bool(q1.get("multiple"))
                    body = f"❓ שאלה מהסוכן (סשן …{ssid}):\n<b>{html_escape(q1.get('question', ''))}</b>\n"
                    for i, o in enumerate(opts, 1):
                        desc = (o.get("description") or "")[:200]
                        body += f"\n{i}. <b>{html_escape(o.get('label', ''))}</b> — {html_escape(desc)}"
                    if multiple:
                        body += "\n(שאלת בחירה מרובה — ענה בסדר מופרד בפסיקים עם /q)"
                    rows = [[{"text": (o.get("label", "") or "?")[:36],
                              "callback_data": f"q:{qid}:{i}"}] for i, o in enumerate(opts)]
                    rows.append([{"text": "🚫 דחה / התעלם", "callback_data": f"q:{qid}:-1"}])
                    tg_send(body, html=True, reply_markup={"inline_keyboard": rows})
                    pqs[qid] = {"directory": d, "labels": [o.get("label", "") for o in opts],
                                "question": (q1.get("question") or "")[:120], "multiple": multiple}
                state_write({"notified_qs": sorted(notified_q | {qid})[-100:], "pending_qs": pqs})
                notified_q.add(qid)

            # 6) subagent (child-session) mirroring
            try:
                children = oc_get(f"/session/{sid}/children", d)
            except Exception:
                children = []
            kids_known = list(st.get("children", []))
            kids_initialized = bool(st.get("children_initialized"))
            for c in children or []:
                cid = c.get("id")
                if not cid:
                    continue
                if cid not in kids_known:
                    # appeared → seed its history silently; announce only live spawns
                    try:
                        cm = oc_get(f"/session/{cid}/message", d, limit=MSG_WINDOW)
                    except Exception:
                        cm = []
                    sa = {m["info"]["id"] for m in cm
                          if m["info"]["role"] == "assistant" and m["info"].get("time", {}).get("completed")}
                    stt = {p["id"] for m in cm for p in m.get("parts", []) if p.get("type") == "tool"}
                    cur = state_read()
                    state_write({"seen_msgs": sorted(set(cur.get("seen_msgs", [])) | sa)[-500:],
                                 "seen_tools": sorted(set(cur.get("seen_tools", [])) | stt)[-500:],
                                 "children": kids_known + [cid]})
                    kids_known.append(cid)
                    if kids_initialized:
                        tg_send(f"🤖 subagent delegating: {(c.get('title') or '')[:60]}\n🆔 …{cid[-6:]}")
                    else:
                        kids_initialized = True
                        state_write({"children_initialized": True})
                    continue
                # live activity of a known child
                try:
                    cm = oc_get(f"/session/{cid}/message", d, limit=MSG_WINDOW)
                except Exception:
                    continue
                cm.sort(key=lambda m: m["info"].get("time", {}).get("created", 0))
                cur = state_read()
                cm_seen = set(cur.get("seen_msgs", []))
                ct_seen = set(cur.get("seen_tools", []))
                last_sig_c = cur.get("last_tool_sig")
                lines = []
                for m in cm:
                    info = m["info"]
                    for p in m.get("parts", []):
                        if p.get("type") == "tool" and p["id"] not in ct_seen:
                            ct_seen.add(p["id"])
                            sig = tool_sig(p)
                            if sig is not None and sig == last_sig_c:
                                continue
                            line = fmt_tool(p)
                            if line:
                                lines.append(line)
                                last_sig_c = sig
                    if (info["role"] == "assistant" and info["id"] not in cm_seen
                            and info.get("time", {}).get("completed")):
                        texts = [p.get("text", "").strip() for p in m.get("parts", [])
                                 if p.get("type") == "text" and p.get("text", "").strip()]
                        if texts:
                            tg_send_md("👥 " + "\n\n".join(texts))
                        cm_seen.add(info["id"])
                if lines:
                    tg_send("👥 " + "\n".join(lines[:10]), html=True)
                state_write({"seen_msgs": sorted(cm_seen)[-500:],
                             "seen_tools": sorted(ct_seen)[-500:],
                             "last_tool_sig": last_sig_c})
            failed_since = None
        except Exception as e:
            if failed_since is None:
                failed_since = time.time()
                log("watcher error:", e)
            elif time.time() - failed_since > 300:  # session likely gone
                log("watcher failing for 5m:", e)
                failed_since = time.time()


# ── main ──────────────────────────────────────────────────────────────────

def _reset_telegram_state_on_token_change() -> None:
    """Update offsets / message ids belong to the *old bot's* chat. When the
    bot token changes, wipe Telegram-side state so the bridge starts clean
    (opencode-side seen-sets are kept to avoid replaying agent output)."""
    fp = hashlib.md5(BOT_TOKEN.encode()).hexdigest()
    st = state_read()
    if st.get("bot_token_fp") == fp:
        return
    log("bot token changed — resetting telegram-side state")
    state_write({
        "bot_token_fp": fp,
        "offset": 0,
        "max_tg_msg_id": None,
        "wipe_floor": 0,
        "known_ids": [],
        "todo_msg_id": None,
        "todo_hash": None,
        "todo_pinned": False,
        "session_msg_id": None,
        "session_msg_sid": None,
        "session_banner_text": None,
        "session_title": None,
        "pending_perm": None,
        "notified_perms": [],
        "pending_new": None,
        "dir_browser": None,
        "model_browser": None,
    })


def main() -> None:
    log(f"bridge starting (server {SERVER}, admin {ADMIN_ID})")
    _reset_telegram_state_on_token_change()
    register_telegram_commands()
    threading.Thread(target=watcher_loop, daemon=True).start()
    try:
        tg_send("🟢 opencode-remote bridge online. /help for commands.")
    except Exception as e:
        log("startup telegram send failed:", e)
    telegram_loop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
