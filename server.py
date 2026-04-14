#!/usr/bin/env python3
"""
JV Holdings · Oficina Viva — local backend
===========================================
Tiny zero-dependency HTTP server that:
  • Serves OFFICE_SIM.html + static files from this folder
  • GET  /office_state.json                 → read state
  • POST /api/patch                         → merge JSON patch into state
  • POST /api/task                          → append task to INBOX/TASKS.md + worklog
  • POST /api/answer                        → answer an agent's open question
  • POST /api/decision                      → archive CEO decision to _CEO/DECISIONS.md
  • POST /api/agent_update                  → update one agent's currentTask/lastOutput/mood
  • POST /api/worklog                       → append timestamped entry
  • POST /api/dialogue                      → record agent-to-agent dialogue bubble
  • POST /api/active_work                   → mark agent working (with endsAt)
  • POST /api/snapshot                      → save Drive/Gmail snapshot
  • GET  /api/financials                    → current financial data (all engines)
  • GET  /api/financials/<engine>           → single engine (gmp|uts|cargx)
  • POST /api/financials                    → receive push from Drive Financial Bridge
  • GET  /api/agent/<id>                    → full agent detail + recent worklog
  • POST /api/memory_event                  → push event to agent memory stream (Tier A)
  • GET  /api/memory/<id>                   → read agent memory stream + context pack
  • POST /api/reflect                       → save reflection insight to memory pack (Tier A)
  • POST /api/thinking_status               → toast: thinking loop on/off (Tier B)
  • POST /api/plan                          → agent sets daily plan (Tier C)
  • POST /api/report                        → agent sets EOD report (Tier C)
  • GET  /api/ceo_inbox                     → aggregated questions + approvals + escalations (Tier B)
  • GET  /api/health                        → heartbeat
  • POST /api/chat                          → CEO chat to agent (persists + queues as task)
  • GET  /api/chat_log/<agent>              → markdown of agent's chat history
  • GET  /api/chat_agents                   → list of agents available in chat
  • POST /api/ask_openai                    → ChatGPT assistant (Claude + agents can call)
  • POST /api/agent_exec                    → Agent runs a task using OpenAI toolbelt (web/code/image)
  • POST /api/claude_tool_request           → Agent queues a Claude-side MCP tool call (Drive/Gmail/Slack/etc)
  • POST /api/claude_tool_result            → Claude posts result of queued tool call
  • GET  /api/toolbelt                      → full tool catalog
  • GET  /api/toolbelt/<agent>              → per-agent toolbelt + recent tool runs
  • GET  /api/claude_tool_queue             → pending tool requests for Claude

Run:    python3 server.py
Open:   http://localhost:8765/OFFICE_SIM.html
"""
import json
import os
import sys
import re
import threading
import uuid
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Global lock serializing ALL state file reads/writes. ThreadingHTTPServer
# spawns a thread per request; without this lock, concurrent save_state
# calls race on the same .tmp file and produce concatenated/corrupt JSON.
_STATE_LOCK = threading.RLock()

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "office_state.json")
INBOX = os.path.join(ROOT, "INBOX")
CHATS_DIR = os.path.join(INBOX, "chats")
CEO_DIR = os.path.join(ROOT, "_CEO")
TOOLBELT_DIR = os.path.join(ROOT, "_TOOLBELT")
CLAUDE_QUEUE_DIR = os.path.join(ROOT, "workers", "queue", "claude_tools")
TOOLRUNS_DIR = os.path.join(INBOX, "toolruns")  # per-agent tool call log
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "0.0.0.0")

os.makedirs(INBOX, exist_ok=True)
os.makedirs(CHATS_DIR, exist_ok=True)
os.makedirs(CEO_DIR, exist_ok=True)
os.makedirs(TOOLBELT_DIR, exist_ok=True)
os.makedirs(CLAUDE_QUEUE_DIR, exist_ok=True)
os.makedirs(TOOLRUNS_DIR, exist_ok=True)

# Import toolbelt modules (lazy — so server still boots if either has a bug)
try:
    sys.path.insert(0, ROOT)
    from workers import openai_tools as _otools
except Exception as _e:
    _otools = None
    print(f"[warn] workers.openai_tools unavailable: {_e}", file=sys.stderr)

try:
    from workers import claude_tools as _ctools
except Exception as _e:
    _ctools = None
    print(f"[warn] workers.claude_tools unavailable: {_e}", file=sys.stderr)

# Agent roster for chat UI
CHAT_AGENTS = [
    {"id": "coo",       "name": "COO",           "color": "#10b981"},
    {"id": "finance",   "name": "Finance",       "color": "#3b82f6"},
    {"id": "legal",     "name": "Legal/HR",      "color": "#a855f7"},
    {"id": "ops",       "name": "Operations",    "color": "#f59e0b"},
    {"id": "marketing", "name": "Marketing",     "color": "#ec4899"},
    {"id": "bd",        "name": "BusinessDev",   "color": "#06b6d4"},
    {"id": "strategy",  "name": "Strategy",      "color": "#fbbf24"},
    {"id": "research",  "name": "Research",      "color": "#eab308"},
    {"id": "exec",      "name": "Exec Assistant","color": "#34d399"},
]

# ---------- state helpers ----------

# Structural keys that MUST exist in a valid state. If any are missing,
# load_state will refuse to treat the file as authoritative and instead
# preserves whatever is already on disk. This prevents a momentary
# read failure from nuking the canonical office data on next save.
_REQUIRED_STATE_KEYS = {"agents", "approvals", "floorPlan"}

def load_state():
    with _STATE_LOCK:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("state root is not a dict")
            return data
        except Exception as e:
            print(f"[warn] could not load state: {e}", file=sys.stderr)
            return {}

def save_state(state):
    with _STATE_LOCK:
        state["lastUpdated"] = datetime.now().astimezone().isoformat(timespec="seconds")
        # Defense: never overwrite a healthy file with a state that is
        # missing required structural keys. If the incoming state is
        # incomplete, try to recover by merging into the on-disk state.
        # If we can't read the disk either, ABORT — better to drop one
        # write than to clobber the whole office.
        if not _REQUIRED_STATE_KEYS.issubset(state.keys()):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                if isinstance(disk, dict) and _REQUIRED_STATE_KEYS.issubset(disk.keys()):
                    print(
                        f"[guard] incoming state missing "
                        f"{_REQUIRED_STATE_KEYS - set(state.keys())}; "
                        "merging into on-disk state",
                        file=sys.stderr,
                    )
                    disk.update(state)
                    state = disk
                else:
                    print("[guard] ABORT save: disk state also incomplete", file=sys.stderr)
                    return
            except Exception as e:
                print(f"[guard] ABORT save: cannot read disk ({e})", file=sys.stderr)
                return
        # Use a unique tmp name so concurrent saves (if any slipped past
        # the lock) can't clobber each other's temp files.
        tmp = f"{STATE_FILE}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp, STATE_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

def append_file(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line if line.endswith("\n") else line + "\n")


# ---------- Chat→Task auto-sync + auto-close functions ----------

def _is_actionable_request(text: str) -> bool:
    """Returns True if text appears to be an actionable task request.
    Heuristics:
    - Contains imperative verbs: crea, haz, prepara, envía, busca, analiza, redacta, etc.
    - Or ends with '?' and asks something actionable (puedes hacer X?)
    - NOT actionable if: <10 chars, solo saludo, or empieza con "↳ DRIVE_FETCH"
    """
    if not text or len(text.strip()) < 10:
        return False

    # Sistema messages y auto-respuestas no son tareas
    if text.strip().startswith("↳"):
        return False

    text_lower = text.lower().strip()

    # Saludos y confirmaciones simples
    simple_non_actions = {"hola", "gracias", "ok", "si", "no", "dale", "listo", "perfecto", "yep", "vale"}
    if text_lower in simple_non_actions:
        return False

    # Verbos imperativos que indican acción
    imperative_verbs = [
        "crea", "haz", "prepara", "envía", "busca", "analiza", "redacta", "revisa",
        "arma", "monta", "saca", "manda", "dame", "consigue", "verifica", "hazme",
        "escribe", "genera", "diseña", "monta", "actualiza", "completa", "termina",
        "entrega", "calcula", "recopila", "resumo", "valida", "checkea", "produce"
    ]

    if any(verb in text_lower for verb in imperative_verbs):
        return True

    # Preguntas accionables que terminan con ?
    if text.rstrip().endswith("?"):
        # Evitar preguntas simples o vagas
        if any(q in text_lower for q in ["hola", "cómo estás", "qué hay", "cómo va"]):
            return False
        # Si pregunta "puedes hacer X?" es accionable
        if "puedes" in text_lower or "puedo" in text_lower or "hago" in text_lower:
            return True

    return False


def _close_task_by_output(agent_id: str, output_path: str, summary: str) -> None:
    """Busca la última tarea abierta de un agente en TASKS.md y la marca como cerrada.
    Formato final: - [x] @{agent_id} {summary} → {output_path} (auto-cerrada)
    """
    tasks_file = os.path.join(INBOX, "TASKS.md")
    if not os.path.exists(tasks_file):
        return

    try:
        with open(tasks_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Buscar desde el final (última tarea abierta del agente)
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            # Buscar patrón: - [ ] @{agent_id}
            if f"- [ ] @{agent_id}" in line:
                # Truncar summary a 80 chars
                summary_short = (summary[:77] + "...") if len(summary) > 80 else summary
                # Reemplazar: - [ ] por - [x] y agregar output y auto-close marker
                new_line = f"- [x] @{agent_id} {summary_short} → {output_path} (auto-cerrada)\n"
                lines[i] = new_line

                with open(tasks_file, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                return
    except Exception as e:
        print(f"[warn] could not auto-close task for {agent_id}: {e}", file=sys.stderr)


# Map chat-agent id → directory that holds MEMORY_PACK.md
_AGENT_DIR_BY_ID = {
    "coo":       "_COO",
    "finance":   "Finance",
    "legal":     "Legal_HR",
    "ops":       "Operations",
    "marketing": "Marketing",
    "bd":        "BusinessDev",
    "strategy":  "Strategy",
    "research":  "Research",
    "exec":      "ExecAssistant",
}

def _load_agent_memory_pack(agent_id: str) -> str:
    """Return the text of the agent's MEMORY_PACK.md (best-effort, safe)."""
    folder = _AGENT_DIR_BY_ID.get(agent_id)
    if not folder:
        return ""
    # Try common filenames
    candidates = [
        os.path.join(ROOT, folder, "MEMORY_PACK.md"),
        os.path.join(ROOT, folder, "COO_MEMORY_PACK.md"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read()
            except Exception:
                continue
    return ""


_DRIVE_INDEX_CACHE = {"text": "", "mtime": 0.0}

def _load_drive_index() -> str:
    """Return cached DRIVE_INDEX.md. Every agent gets this in its system
    prompt so none of them can claim 'no tengo acceso a tu Drive'."""
    path = os.path.join(CEO_DIR, "DRIVE_INDEX.md")
    if not os.path.exists(path):
        return ""
    try:
        mtime = os.path.getmtime(path)
        if mtime != _DRIVE_INDEX_CACHE["mtime"]:
            with open(path, "r", encoding="utf-8") as fh:
                _DRIVE_INDEX_CACHE["text"] = fh.read()
            _DRIVE_INDEX_CACHE["mtime"] = mtime
        return _DRIVE_INDEX_CACHE["text"]
    except Exception:
        return _DRIVE_INDEX_CACHE["text"] or ""


# Regex to catch agents emitting [[DRIVE_FETCH: <id|query>]] markers in replies.
_DRIVE_FETCH_RE = re.compile(r"\[\[DRIVE_FETCH\s*:\s*([^\]\n]+?)\s*\]\]", re.IGNORECASE)

def _extract_drive_fetch_markers(text: str) -> list:
    """Return list of Drive fetch requests the agent embedded in its reply."""
    if not text:
        return []
    return [m.strip() for m in _DRIVE_FETCH_RE.findall(text)]

def _read_chat_tail(chat_path: str, chars: int = 2500) -> str:
    """Return the last `chars` characters of a chat log, for context."""
    if not os.path.exists(chat_path):
        return ""
    try:
        with open(chat_path, "r", encoding="utf-8") as fh:
            txt = fh.read()
        return txt[-chars:] if len(txt) > chars else txt
    except Exception:
        return ""

def _looks_like_error(text: str, meta: dict) -> bool:
    """True when an LLM backend returned something that LOOKS like a reply
    but is actually an error string (HTTP code, empty output, etc). Used by
    the chat router to decide whether to fail over to the other backend."""
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    markers = ("http 4", "http 5", "http 401", "http 429", "http 500",
               "insufficient_quota", "rate_limit", "overloaded",
               "invalid_api_key", "authentication_error", "credit balance",
               "api key no configurada", "dry-run", "(error llamando")
    if any(m in low for m in markers):
        return True
    if isinstance(meta, dict):
        err = str(meta.get("error") or "").lower()
        if err and (err.startswith("http ") or "error" in err or "rate" in err):
            return True
    return False


def _friendly_openai_error(text: str, meta: dict) -> str:
    """If `text`/`meta` indicate a common OpenAI failure, return a friendly
    Spanish message for the chat. Otherwise return '' (caller keeps original)."""
    t = (text or "").strip()
    err = ""
    if isinstance(meta, dict):
        err = str(meta.get("error") or "")
        body = str(meta.get("body") or "")
    else:
        body = ""
    blob = f"{t} {err} {body}".lower()

    # Quota / billing
    if "insufficient_quota" in blob or "exceeded your current quota" in blob or "http 429" in blob:
        return (
            "🧠 Sin cuota de OpenAI. Guardé tu pedido en `TASKS.md` y lo respondo "
            "apenas se recargue billing.\n\n"
            "→ Recargar saldo: https://platform.openai.com/settings/organization/billing"
        )
    # Auth
    if "invalid_api_key" in blob or "incorrect api key" in blob or "http 401" in blob:
        return (
            "🔐 La API key de OpenAI no es válida o expiró. Guardé tu pedido en `TASKS.md`. "
            "Actualizá la key en `config/.env` (OPENAI_API_KEY) y reintentar."
        )
    # Rate limit (transient) vs quota — differentiate by body mentioning 'rate' only
    if "rate_limit_exceeded" in blob or ("rate limit" in blob and "quota" not in blob):
        return (
            "⏳ OpenAI me dijo 'rate limit' — demasiadas requests en poco tiempo. "
            "Volvé a mandar en ~30s y debería pasar."
        )
    # 5xx server errors
    if "http 500" in blob or "http 502" in blob or "http 503" in blob or "http 504" in blob:
        return (
            "🛠 OpenAI devolvió error de servidor. Reintentá en ~1 min; si persiste, revisar status.openai.com."
        )
    # Network issues (no internet / DNS)
    if "name or service not known" in blob or "temporary failure" in blob or "connection refused" in blob:
        return "🌐 No pude alcanzar OpenAI (red caída). Guardé tu pedido — reintento cuando vuelva conexión."
    return ""

def now_hm():
    return datetime.now().strftime("%H:%M")

def today_iso():
    return datetime.now().strftime("%Y-%m-%d")

def load_openai_key():
    """Return (api_key, org_id) from workers/.env, or (None, None) if missing/placeholder."""
    env_path = os.path.join(ROOT, "workers", ".env")
    if not os.path.exists(env_path):
        return (None, None)
    api_key = None
    org_id = None
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip(); v = v.strip().strip("'").strip('"')
                if k == "OPENAI_API_KEY" and v and not v.startswith("sk-proj-REPLACE"):
                    api_key = v
                elif k == "OPENAI_ORG_ID" and v:
                    org_id = v
    except Exception as e:
        print(f"[warn] could not read workers/.env: {e}", file=sys.stderr)
    return (api_key, org_id)


def call_openai(prompt, system=None, model="gpt-4o-mini", max_tokens=1200):
    """Synchronous one-shot ChatGPT call. Returns (ok, text_or_error, meta)."""
    api_key, org_id = load_openai_key()
    if not api_key:
        return (False, "OpenAI API key not configured. Add OPENAI_API_KEY to workers/.env and restart the server.", {"dry_run": True})
    try:
        import urllib.request
        import urllib.error
    except Exception as e:
        return (False, f"urllib unavailable: {e}", {})
    body = {
        "model": model,
        "messages": (
            ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": prompt}]
        ),
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if org_id:
        headers["OpenAI-Organization"] = org_id
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return (True, text, {"model": model, "usage": usage})
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8")
        except Exception:
            err = str(e)
        return (False, f"OpenAI HTTP {e.code}: {err[:500]}", {})
    except Exception as e:
        return (False, f"OpenAI call failed: {e}", {})


def push_worklog(state, agent, txt, persist_md=True):
    entry = {"t": now_hm(), "agent": agent, "txt": txt}
    state.setdefault("worklog", []).append(entry)
    # keep last 100
    state["worklog"] = state["worklog"][-100:]
    if persist_md:
        append_file(
            os.path.join(INBOX, "WORKLOG.md"),
            f"- {today_iso()} {entry['t']} [{agent}] {txt}"
        )

# ---------- request handler ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # Force an explicit directory so the handler never calls os.getcwd()
        # (which fails with PermissionError on macOS when the launch folder
        # is inside a sandboxed location like iCloud/Documents without
        # Full Disk Access granted to Terminal / python3).
        kwargs["directory"] = ROOT
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # quieter logs
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_no_cache_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")

    def end_headers(self):
        # Add CORS + no-cache to every static response too
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        SimpleHTTPRequestHandler.end_headers(self)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self._send_json({"ok": True, "lastUpdated": load_state().get("lastUpdated")})
        # GET /api/agent/<id>
        m = re.match(r"^/api/agent/([a-z_-]+)$", parsed.path)
        if m:
            agent_id = m.group(1)
            state = load_state()
            agent = next((a for a in state.get("agents", []) if a["id"] == agent_id), None)
            if not agent:
                return self._send_json({"ok": False, "error": "not found"}, status=404)
            # slice worklog entries mentioning this agent (by name or label)
            wl = state.get("worklog", [])
            name_norm = agent["name"].lower()
            mine = [w for w in wl if name_norm in w.get("agent","").lower()][-30:]
            # decisions log (tail) — just raw text lines mentioning the agent
            decisions_path = os.path.join(CEO_DIR, "DECISIONS.md")
            decisions = ""
            if os.path.exists(decisions_path):
                try:
                    with open(decisions_path, "r", encoding="utf-8") as f:
                        decisions = f.read()[-4000:]
                except Exception:
                    pass
            return self._send_json({
                "ok": True,
                "agent": agent,
                "worklog": mine,
                "decisionsTail": decisions,
            })
        # GET /api/memory/<id>
        m = re.match(r"^/api/memory/([a-z_-]+)$", parsed.path)
        if m:
            agent_id = m.group(1)
            state = load_state()
            streams = state.get("memoryStreams", {})
            reflections = state.get("reflections", {})
            plans = state.get("dailyPlans", {}).get(agent_id, {})
            # peer context — last 3 events from each other agent, so this one can "observe"
            peer_context = {}
            for aid, evs in streams.items():
                if aid == agent_id:
                    continue
                peer_context[aid] = evs[-3:]
            return self._send_json({
                "ok": True,
                "stream": streams.get(agent_id, []),
                "reflections": reflections.get(agent_id, []),
                "dailyPlan": plans,
                "peerContext": peer_context,
            })
        # GET /api/financials — return current financial data from state
        if parsed.path == "/api/financials":
            state = load_state()
            fin = state.get("financials", {})
            return self._send_json({"ok": True, "financials": fin})
        # GET /api/financials/<engine> — return single engine
        m2 = re.match(r"^/api/financials/([a-z]+)$", parsed.path)
        if m2:
            eng = m2.group(1)
            state = load_state()
            fin = state.get("financials", {})
            eng_data = fin.get(eng, {})
            return self._send_json({"ok": True, "engine": eng, "data": eng_data, "lastSyncedAt": fin.get("lastSyncedAt")})
        # GET /api/ceo_inbox
        if parsed.path == "/api/ceo_inbox":
            state = load_state()
            questions = [
                {"agent": a["id"], "name": a["name"], "color": a.get("color"),
                 "question": a.get("question")}
                for a in state.get("agents", []) if a.get("question")
            ]
            approvals = state.get("approvals", [])
            # escalations = any worklog entry tagged with "escalate" in last 40
            wl = state.get("worklog", [])
            escalations = [w for w in wl[-40:] if "escalate" in w.get("txt","").lower() or "🚨" in w.get("txt","")]
            thinking = state.get("thinkingLoopStatus", {"running": False})
            drive = state.get("driveSnapshot", {})
            mail = state.get("mailSnapshot", {})
            return self._send_json({
                "ok": True,
                "questions": questions,
                "approvals": approvals,
                "escalations": escalations,
                "thinking": thinking,
                "drive": {"lastPulledAt": drive.get("lastPulledAt"), "count": len(drive.get("files", []))},
                "mail":  {"lastPulledAt": mail.get("lastPulledAt"),  "count": len(mail.get("threads", []))},
            })
        # GET /api/chat_log/<agent> — return chat markdown for an agent
        m = re.match(r"^/api/chat_log/([a-z_-]+)$", parsed.path)
        if m:
            agent_id = m.group(1)
            path = os.path.join(CHATS_DIR, f"{agent_id}.md")
            content = ""
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception:
                    content = ""
            return self._send_json({"ok": True, "agent": agent_id, "markdown": content})
        # GET /api/chat_agents — list of agents for sidebar
        if parsed.path == "/api/chat_agents":
            state = load_state()
            # enrich with currentTask + question + mood from state if available
            enriched = []
            for a in CHAT_AGENTS:
                s = next((x for x in state.get("agents", []) if x["id"] == a["id"]), {})
                enriched.append({
                    **a,
                    "currentTask": s.get("currentTask"),
                    "question": s.get("question"),
                    "mood": s.get("mood"),
                    "lastOutput": s.get("lastOutput"),
                })
            return self._send_json({"ok": True, "agents": enriched})

        # GET /api/toolbelt — catalog of all tools available in the office
        if parsed.path == "/api/toolbelt":
            tools = _otools.list_tools() if _otools else []
            return self._send_json({
                "ok": True,
                "tools": tools,
                "live": bool(_otools and _otools.api_key()),
            })

        # GET /api/toolbelt/<agent> — per-agent tool access + recent runs
        m = re.match(r"^/api/toolbelt/([a-z_-]+)$", parsed.path)
        if m:
            agent_id = m.group(1)
            # Load manifest
            manifest_path = os.path.join(TOOLBELT_DIR, "MANIFEST.json")
            agent_tools: list = []
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, "r") as fh:
                        manifest = json.load(fh)
                    agent_tools = manifest.get(agent_id, manifest.get("_default", []))
                except Exception:
                    pass
            if not agent_tools and _otools:
                agent_tools = list(_otools.DEFAULT_TOOL_SET) + [
                    "claude_drive", "claude_gmail", "claude_slack", "claude_web_fetch"
                ]
            # Recent tool runs
            runs_path = os.path.join(TOOLRUNS_DIR, f"{agent_id}.jsonl")
            recent_runs: list = []
            if os.path.exists(runs_path):
                try:
                    with open(runs_path, "r") as fh:
                        lines = fh.read().strip().split("\n")
                    for line in lines[-20:]:
                        if line.strip():
                            recent_runs.append(json.loads(line))
                except Exception:
                    pass
            return self._send_json({
                "ok": True,
                "agent": agent_id,
                "tools": agent_tools,
                "runs": list(reversed(recent_runs)),
            })

        # GET /api/claude_tool_queue — pending requests for Claude to execute
        if parsed.path == "/api/claude_tool_queue":
            pending = []
            for fn in sorted(os.listdir(CLAUDE_QUEUE_DIR)):
                if fn.endswith(".json"):
                    p = os.path.join(CLAUDE_QUEUE_DIR, fn)
                    try:
                        with open(p, "r") as fh:
                            pending.append(json.load(fh))
                    except Exception:
                        pass
            return self._send_json({"ok": True, "queue": pending, "count": len(pending)})

        # GET /api/agent/<agent_id>/deliverables — list deliverables for an agent
        m = re.match(r"^/api/agent/([a-z_-]+)/deliverables$", parsed.path)
        if m:
            agent_id = m.group(1)
            # Map agent_id to folder name
            agent_folder_map = {
                "coo": "_COO",
                "finance": "Finance",
                "legal": "Legal_HR",
                "ops": "Operations",
                "marketing": "Marketing",
                "bd": "BusinessDev",
                "strategy": "Strategy",
                "research": "Research",
                "exec": "ExecAssistant",
            }
            folder_name = agent_folder_map.get(agent_id)
            deliverables = []
            if folder_name:
                folder_path = os.path.join(ROOT, folder_name)
                if os.path.isdir(folder_path):
                    # Search for .md files in outputs/
                    outputs_path = os.path.join(folder_path, "outputs")
                    if os.path.isdir(outputs_path):
                        for fn in os.listdir(outputs_path):
                            if fn.endswith(".md"):
                                full_path = os.path.join(outputs_path, fn)
                                try:
                                    mtime = os.path.getmtime(full_path)
                                    deliverables.append({
                                        "name": fn,
                                        "relPath": f"{folder_name}/outputs/{fn}",
                                        "mtime": mtime,
                                        "kind": "memo"
                                    })
                                except Exception:
                                    pass
                    # Search for .html files in web/ (only for marketing)
                    if agent_id == "marketing":
                        web_path = os.path.join(folder_path, "web")
                        if os.path.isdir(web_path):
                            for root_web, dirs, files in os.walk(web_path):
                                for fn in files:
                                    if fn == "index.html":
                                        full_path = os.path.join(root_web, fn)
                                        rel = os.path.relpath(full_path, ROOT)
                                        try:
                                            mtime = os.path.getmtime(full_path)
                                            deliverables.append({
                                                "name": os.path.basename(os.path.dirname(full_path)),
                                                "relPath": rel,
                                                "mtime": mtime,
                                                "kind": "webpage"
                                            })
                                        except Exception:
                                            pass
            # Sort by mtime descending
            deliverables.sort(key=lambda x: x["mtime"], reverse=True)
            return self._send_json({"ok": True, "agent": agent_id, "deliverables": deliverables})

        # GET /api/deliverables/recent — top 10 most recent deliverables across all agents
        if parsed.path == "/api/deliverables/recent":
            all_deliverables = []
            agent_folder_map = {
                "coo": "_COO",
                "finance": "Finance",
                "legal": "Legal_HR",
                "ops": "Operations",
                "marketing": "Marketing",
                "bd": "BusinessDev",
                "strategy": "Strategy",
                "research": "Research",
                "exec": "ExecAssistant",
            }
            for agent_id, folder_name in agent_folder_map.items():
                folder_path = os.path.join(ROOT, folder_name)
                if os.path.isdir(folder_path):
                    outputs_path = os.path.join(folder_path, "outputs")
                    if os.path.isdir(outputs_path):
                        for fn in os.listdir(outputs_path):
                            if fn.endswith(".md"):
                                full_path = os.path.join(outputs_path, fn)
                                try:
                                    mtime = os.path.getmtime(full_path)
                                    all_deliverables.append({
                                        "agent": agent_id,
                                        "agent_name": next((a["name"] for a in CHAT_AGENTS if a["id"] == agent_id), agent_id.upper()),
                                        "name": fn,
                                        "path": f"{folder_name}/outputs/{fn}",
                                        "mtime": mtime,
                                        "kind": "memo"
                                    })
                                except Exception:
                                    pass
                    # Add webpages for marketing
                    if agent_id == "marketing":
                        web_path = os.path.join(folder_path, "web")
                        if os.path.isdir(web_path):
                            for root_web, dirs, files in os.walk(web_path):
                                for fn in files:
                                    if fn == "index.html":
                                        full_path = os.path.join(root_web, fn)
                                        rel = os.path.relpath(full_path, ROOT)
                                        try:
                                            mtime = os.path.getmtime(full_path)
                                            all_deliverables.append({
                                                "agent": agent_id,
                                                "agent_name": "Marketing",
                                                "name": os.path.basename(os.path.dirname(full_path)),
                                                "path": rel,
                                                "mtime": mtime,
                                                "kind": "webpage"
                                            })
                                        except Exception:
                                            pass
            # Sort by mtime and return top 10
            all_deliverables.sort(key=lambda x: x["mtime"], reverse=True)
            return self._send_json({"ok": True, "deliverables": all_deliverables[:10]})

        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._send_json({"ok": False, "error": "invalid json"}, status=400)

        route = parsed.path
        # Serialize ALL mutating handlers under the state lock.
        # This turns each handler's load→modify→save cycle into a
        # single atomic critical section — eliminating the
        # lost-update and file-corruption races between concurrent
        # POSTs (e.g. rapid scheduled-task dialogue bursts).
        try:
            with _STATE_LOCK:
                if route == "/api/task":
                    return self._handle_task(data)
                if route == "/api/answer":
                    return self._handle_answer(data)
                if route == "/api/decision":
                    return self._handle_decision(data)
                if route == "/api/agent_update":
                    return self._handle_agent_update(data)
                if route == "/api/worklog":
                    return self._handle_worklog(data)
                if route == "/api/financials":
                    return self._handle_financials(data)
                if route == "/api/patch":
                    return self._handle_patch(data)
                if route == "/api/dialogue":
                    return self._handle_dialogue(data)
                if route == "/api/active_work":
                    return self._handle_active_work(data)
                if route == "/api/snapshot":
                    return self._handle_snapshot(data)
                if route == "/api/memory_event":
                    return self._handle_memory_event(data)
                if route == "/api/reflect":
                    return self._handle_reflect(data)
                if route == "/api/thinking_status":
                    return self._handle_thinking_status(data)
                if route == "/api/plan":
                    return self._handle_plan(data)
                if route == "/api/report":
                    return self._handle_report(data)
                if route == "/api/chat":
                    return self._handle_chat(data)
                if route == "/api/ask_openai":
                    return self._handle_ask_openai(data)
                if route == "/api/agent_exec":
                    return self._handle_agent_exec(data)
                if route == "/api/claude_tool_request":
                    return self._handle_claude_tool_request(data)
                if route == "/api/claude_tool_result":
                    return self._handle_claude_tool_result(data)
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            return self._send_json({"ok": False, "error": str(e)}, status=500)

        return self._send_json({"ok": False, "error": "unknown route"}, status=404)

    # ---------- route handlers ----------

    def _handle_chat(self, data):
        """CEO sends a message to an agent's chat.
        The agent REPLIES IMMEDIATELY in the same chat (synchronous OpenAI call).
        Also mirrors the task into TASKS.md so the background thinking loop can
        pick up longer follow-ups — but the CEO never has to wait for a cycle
        to see an acknowledgement or answer."""
        agent_id = (data.get("agent") or "").lower()
        text = (data.get("text") or "").strip()
        role = (data.get("role") or "ceo").lower()  # "ceo" | "agent"
        as_task = bool(data.get("as_task", role == "ceo"))
        prio = data.get("prio") or "p2"
        if not agent_id or not text:
            return self._send_json({"ok": False, "error": "need agent + text"}, status=400)
        if not any(a["id"] == agent_id for a in CHAT_AGENTS):
            return self._send_json({"ok": False, "error": f"unknown agent {agent_id}"}, status=404)
        chat_path = os.path.join(CHATS_DIR, f"{agent_id}.md")
        ts = f"{today_iso()} {now_hm()}"
        label = "CEO" if role == "ceo" else agent_id.upper()
        append_file(chat_path, f"\n### {label} · {ts}\n{text}\n")

        # If this is an agent self-posting (role != ceo), we're done.
        if role != "ceo":
            return self._send_json({"ok": True, "agent": agent_id, "ts": ts})

        # Mirror to TASKS.md for the thinking loop (longer deep-work follow-ups)
        # Only create task if the message is actionable (not just a greeting)
        if as_task and _is_actionable_request(text):
            append_file(
                os.path.join(INBOX, "TASKS.md"),
                f"- [ ] @{agent_id} {text} !{prio}  ({ts}) [actionable]"
            )

        # --- INSTANT REPLY: call OpenAI synchronously and append to chat.md ---
        agent_meta = next((a for a in CHAT_AGENTS if a["id"] == agent_id), None)
        agent_name = agent_meta["name"] if agent_meta else agent_id.upper()
        mem_pack = _load_agent_memory_pack(agent_id)[:3500]
        chat_tail = _read_chat_tail(chat_path, chars=2500)
        drive_index = _load_drive_index()[:6000]

        system = (
            f"Eres el agente {agent_name} de la Oficina Viva de JV Holdings. "
            f"Operas bajo el COO (Claude) y el CEO (JT). "
            f"Respondes YA, DIRECTO en este chat — nunca digas 'lo hago en el próximo ciclo' "
            f"ni 'reporto luego'. Si puedes ejecutar ahora mismo con lo que sabes, hazlo y reporta el resultado. "
            f"Si necesitas una herramienta del toolbelt o tiempo para un deep-work, dilo explícito y propón el siguiente paso. "
            f"Idioma: español. Tono: directo, 2–6 líneas salvo que el CEO pida detalle. Sin relleno.\n\n"
            f"## REGLA DRIVE (crítica)\n"
            f"Tienes acceso TOTAL al Google Drive del CEO a través del COO (Claude) vía MCP. "
            f"PROHIBIDO responder cosas como 'no tengo acceso a tu Drive', 'compárteme el doc', "
            f"'necesito que me pases el archivo', 'no puedo leer drives externos'. Si lo haces, "
            f"estás violando la regla de autonomía y el CEO lo verá como falla grave.\n"
            f"Cómo usar Drive:\n"
            f"  1. Revisa el DRIVE_INDEX (abajo) — tiene los IDs y links de TODOS los docs clave.\n"
            f"  2. Si el doc está en el índice, úsalo por nombre y link. El CEO puede abrirlo clickeando.\n"
            f"  3. Si necesitas el CONTENIDO específico de un doc para responder, emite este marcador "
            f"exacto en tu respuesta: [[DRIVE_FETCH: <fileId o nombre>]]. El COO lo lee y te lo inyecta "
            f"en el siguiente turno automáticamente.\n"
            f"  4. NUNCA inventes IDs. Si un doc no está en el índice, dilo y pide al CEO que lo agregue, "
            f"o emite [[DRIVE_FETCH: <query descriptivo>]] para que el COO lo busque.\n\n"
            f"## Memoria del agente (extracto)\n{mem_pack}"
            f"\n\n## Drive del CEO (índice vivo)\n{drive_index}"
            f"\n\n## Contexto reciente del chat\n{chat_tail}"
        )

        # --- INSTANT REPLY: dual-backend router ---
        # Try the preferred backend; if it fails with a recoverable error
        # (quota / auth / network / rate-limit), automatically fall over to
        # the other one so the agent always replies in the same chat.
        reply_text = "(toolbelt offline — ni OpenAI ni Anthropic disponibles)"
        reply_ok = False
        reply_meta: dict = {}
        attempt_log: list = []

        def _try_openai():
            if not _otools:
                return (False, "(OpenAI toolbelt no cargado)", {"backend": "openai", "error": "module missing"})
            try:
                ok, txt, meta = _otools.run_agent_task(
                    agent_id=agent_id,
                    task=text,
                    tools=[],
                    system=system,
                    model="gpt-4o-mini",
                    max_output_tokens=700,
                )
                meta = dict(meta or {})
                meta.setdefault("backend", "openai")
                return (ok, txt, meta)
            except Exception as e:
                return (False, f"(error llamando OpenAI: {e})", {"backend": "openai", "error": str(e)})

        def _try_claude():
            if not _ctools:
                return (False, "(Claude toolbelt no cargado)", {"backend": "claude", "error": "module missing"})
            try:
                ok, txt, meta = _ctools.run_agent_task_claude(
                    agent_id=agent_id,
                    task=text,
                    system=system,
                    max_output_tokens=700,
                )
                meta = dict(meta or {})
                meta.setdefault("backend", "claude")
                return (ok, txt, meta)
            except Exception as e:
                return (False, f"(error llamando Claude: {e})", {"backend": "claude", "error": str(e)})

        # Decide primary backend: CHAT_BACKEND env, otherwise prefer whichever has a key.
        preferred = (_ctools.preferred_backend() if _ctools else "openai")
        order = ["claude", "openai"] if preferred == "claude" else ["openai", "claude"]

        for backend in order:
            ok, txt, meta = _try_openai() if backend == "openai" else _try_claude()
            attempt_log.append({"backend": backend, "ok": ok, "error": (meta or {}).get("error", "")})
            # Real answer → commit and stop.
            if ok and txt and not _looks_like_error(txt, meta):
                reply_text, reply_ok, reply_meta = txt, True, meta
                break
            # Otherwise remember the last failure and try the next backend.
            reply_text, reply_ok, reply_meta = txt, False, meta

        reply_meta["attempts"] = attempt_log

        # --- Graceful error surface: never show raw "HTTP 429" / "HTTP 401" to CEO ---
        if not reply_ok:
            # If BOTH backends ran out of credit, show a combined billing message
            # so the CEO sees both links — not just the one from the last attempt.
            both_out_of_credit = False
            if len(attempt_log) >= 2:
                err_blob = " ".join(str(a.get("error", "")).lower() for a in attempt_log)
                openai_dead = "http 429" in err_blob or "insufficient_quota" in err_blob
                claude_dead = ("http 400" in err_blob or "credit balance" in err_blob
                               or "low to access" in err_blob)
                body_blob = (str(reply_meta.get("body", "")) if isinstance(reply_meta, dict) else "").lower()
                if "credit balance" in body_blob or "low to access" in body_blob:
                    claude_dead = True
                both_out_of_credit = openai_dead and claude_dead

            if both_out_of_credit:
                friendly = (
                    "💳 Ambos proveedores LLM están sin saldo:\n"
                    "• OpenAI: https://platform.openai.com/settings/organization/billing\n"
                    "• Anthropic: https://console.anthropic.com/settings/billing\n"
                    "Guardé tu pedido en `TASKS.md` — respondo apenas cargues cualquiera de los dos."
                )
            else:
                friendly = ""
                # Try OpenAI-style error mapper first, then Anthropic-style.
                friendly = _friendly_openai_error(reply_text, reply_meta) or friendly
                if not friendly and _ctools:
                    friendly = _ctools.friendly_claude_error(reply_text, reply_meta)
                if not friendly:
                    friendly = ("⚠ No pude alcanzar OpenAI ni Anthropic ahora mismo. "
                                "Guardé tu pedido en `TASKS.md` y respondo cuando vuelva alguno de los dos.")
            reply_text = friendly
            if as_task:
                append_file(
                    os.path.join(INBOX, "TASKS.md"),
                    f"  ↳ _pendiente: esperando backend LLM (OpenAI/Anthropic). Reintentar cuando se recargue billing._"
                )

        reply_ts = f"{today_iso()} {now_hm()}"
        append_file(chat_path, f"\n### {agent_name.upper()} · {reply_ts}\n{reply_text}\n")

        # Surface any DRIVE_FETCH markers the agent emitted so the COO /
        # background loop can resolve them and inject the real content next turn.
        drive_fetches = _extract_drive_fetch_markers(reply_text)
        if drive_fetches:
            try:
                for q in drive_fetches:
                    fetch_id = uuid.uuid4().hex[:8]
                    req = {
                        "req_id": fetch_id,
                        "agent": agent_id,
                        "tool": "claude_drive",
                        "params": {"query": q},
                        "ts": reply_ts,
                        "source": "chat_drive_fetch_marker",
                    }
                    with open(os.path.join(CLAUDE_QUEUE_DIR, f"{fetch_id}.json"), "w") as fh:
                        json.dump(req, fh, ensure_ascii=False, indent=2)
            except Exception:
                pass

        # Pulse state so the agent looks alive in the sim + emit completion event
        try:
            state = load_state()
            push_worklog(state, agent_id.upper(), f"CEO chat: {text[:80]} → respondió")
            agent_s = next((a for a in state.get("agents", []) if a["id"] == agent_id), None)
            if agent_s:
                agent_s["currentTask"] = text[:100]
                agent_s["lastOutput"] = reply_text[:200]
                agent_s["lastCompletedAt"] = reply_ts
            # Completion notifications queue (UI polls this for popup)
            notifs = state.setdefault("completionNotifications", [])
            notifs.append({
                "id": uuid.uuid4().hex[:8],
                "agent": agent_id,
                "agent_name": agent_name,
                "task": text[:120],
                "summary": reply_text[:160],
                "ts": reply_ts,
                "ok": bool(reply_ok),
                "seen": False,
            })
            # keep only last 20 to avoid unbounded growth
            if len(notifs) > 20:
                state["completionNotifications"] = notifs[-20:]
            save_state(state)
        except Exception:
            pass

        return self._send_json({
            "ok": True,
            "agent": agent_id,
            "ts": ts,
            "reply": reply_text,
            "reply_ok": reply_ok,
            "reply_ts": reply_ts,
            "meta": reply_meta,
            "drive_fetches": drive_fetches,
            "completion_notification": {
                "agent": agent_id,
                "agent_name": agent_name,
                "task": text[:120],
                "summary": reply_text[:160],
                "ts": reply_ts,
                "ok": bool(reply_ok),
            },
        })

    def _handle_ask_openai(self, data):
        """Personal ChatGPT assistant for Claude (COO) or any agent.
        Accepts {prompt, agent (optional), system (optional), model (optional), log (optional bool)}.
        If `agent` is given AND log!=False, the Q&A is appended to that agent's chat log
        so the conversation stays visible in the drawer."""
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return self._send_json({"ok": False, "error": "need prompt"}, status=400)
        agent_id = (data.get("agent") or "").lower() or None
        system = data.get("system")
        if not system and agent_id:
            agent_meta = next((a for a in CHAT_AGENTS if a["id"] == agent_id), None)
            if agent_meta:
                system = (
                    f"You are the ChatGPT assistant embedded inside JV Holdings' Oficina Viva. "
                    f"You're currently helping the {agent_meta['name']} agent. "
                    f"Be concise, decisive, and practical. Respond in the same language as the prompt "
                    f"(usually Spanish). No fluff."
                )
        if not system:
            system = (
                "You are the ChatGPT assistant embedded inside JV Holdings' Oficina Viva, "
                "serving the COO (Claude) and the department agents. Be concise, decisive, and practical. "
                "Respond in the same language as the prompt (usually Spanish)."
            )
        model = data.get("model") or "gpt-4o-mini"
        max_tokens = int(data.get("max_tokens") or 1200)
        should_log = data.get("log") if data.get("log") is not None else bool(agent_id)

        ok, answer, meta = call_openai(prompt, system=system, model=model, max_tokens=max_tokens)
        ts = f"{today_iso()} {now_hm()}"

        if agent_id and should_log:
            chat_path = os.path.join(CHATS_DIR, f"{agent_id}.md")
            label = "ChatGPT"
            header = f"\n### CEO→ChatGPT · {ts}\n{prompt}\n\n### {label} · {ts}\n{answer}\n"
            append_file(chat_path, header)

        return self._send_json({
            "ok": ok,
            "answer": answer,
            "meta": meta,
            "ts": ts,
            "agent": agent_id,
        })

    def _handle_agent_exec(self, data):
        """Agent executes a task using its OpenAI toolbelt (web_search, code_interpreter, etc.).
        Body: {agent, task, tools?, model?, log?, system?}
        Any caller (CEO, Claude COO, another agent, a scheduled worker) can invoke this."""
        agent_id = (data.get("agent") or "coo").lower()
        task = (data.get("task") or data.get("prompt") or "").strip()
        if not task:
            return self._send_json({"ok": False, "error": "need task"}, status=400)
        if not _otools:
            return self._send_json({"ok": False, "error": "workers.openai_tools module not loaded"}, status=500)
        tools = data.get("tools")  # None → use default
        model = data.get("model") or "gpt-4o-mini"
        system = data.get("system")
        should_log_chat = data.get("log") if data.get("log") is not None else True

        # Only keep openai-provider tools here; claude_* are queued separately
        if tools is not None:
            tools = [t for t in tools if not t.startswith("claude_")]

        ok, text, meta = _otools.run_agent_task(
            agent_id=agent_id,
            task=task,
            tools=tools,
            system=system,
            model=model,
        )

        ts = f"{today_iso()} {now_hm()}"

        # Log tool run to disk
        run_entry = {
            "ts": ts, "agent": agent_id, "task": task[:300],
            "ok": ok, "tools": tools if tools is not None else (_otools.DEFAULT_TOOL_SET if _otools else []),
            "model": meta.get("model"), "tool_trace": meta.get("tool_trace", []),
            "usage": meta.get("usage", {}), "answer_preview": (text or "")[:500],
        }
        try:
            with open(os.path.join(TOOLRUNS_DIR, f"{agent_id}.jsonl"), "a") as fh:
                fh.write(json.dumps(run_entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # Log to agent chat so it's visible in the drawer
        if should_log_chat:
            label = f"{agent_id.upper()}·TOOLBELT"
            trace_txt = ""
            if meta.get("tool_trace"):
                trace_txt = "\n\n_tools:_ " + ", ".join(
                    f"{t.get('tool')}({t.get('summary','')})" for t in meta["tool_trace"]
                )
            chat_path = os.path.join(CHATS_DIR, f"{agent_id}.md")
            append_file(
                chat_path,
                f"\n### CEO→{agent_id.upper()} · {ts}\n🛠 Toolbelt task: {task}\n\n### {label} · {ts}\n{text}{trace_txt}\n",
            )

        # Worklog pulse so the agent looks alive in the sim
        state = load_state()
        push_worklog(state, agent_id.upper(), f"Toolbelt: {task[:80]} → {len(text or '')} chars")
        save_state(state)

        return self._send_json({
            "ok": ok, "agent": agent_id, "answer": text,
            "meta": meta, "ts": ts,
        })

    def _handle_claude_tool_request(self, data):
        """Agent asks Claude (during next thinking loop) to run an MCP tool.
        Body: {agent, tool (e.g. 'claude_drive'), params: {...}, intent: 'why I need this'}
        Writes a JSON request to workers/queue/claude_tools/; Claude picks it up and posts result."""
        agent_id = (data.get("agent") or "").lower()
        tool = (data.get("tool") or "").lower()
        params = data.get("params") or {}
        intent = (data.get("intent") or "").strip()
        if not agent_id or not tool:
            return self._send_json({"ok": False, "error": "need agent + tool"}, status=400)
        req_id = f"{today_iso()}_{now_hm().replace(':','')}_{agent_id}_{uuid.uuid4().hex[:6]}"
        req = {
            "id": req_id,
            "ts": f"{today_iso()} {now_hm()}",
            "agent": agent_id,
            "tool": tool,
            "params": params,
            "intent": intent,
            "status": "pending",
        }
        path = os.path.join(CLAUDE_QUEUE_DIR, f"{req_id}.json")
        with open(path, "w") as fh:
            json.dump(req, fh, ensure_ascii=False, indent=2)
        # Surface in chat so CEO can see the agent asked for a tool
        ts = req["ts"]
        append_file(
            os.path.join(CHATS_DIR, f"{agent_id}.md"),
            f"\n### {agent_id.upper()}·TOOL-REQUEST · {ts}\n"
            f"🔧 Pide a Claude: `{tool}` — _{intent or 'sin contexto'}_\n"
            f"Req ID: `{req_id}`\n"
        )
        return self._send_json({"ok": True, "id": req_id, "queued": path})

    def _handle_claude_tool_result(self, data):
        """Claude posts the result of a queued tool request. Marks the queue
        item as done and appends the result to the agent's chat."""
        req_id = (data.get("id") or "").strip()
        result = data.get("result")
        if not req_id:
            return self._send_json({"ok": False, "error": "need id"}, status=400)
        path = os.path.join(CLAUDE_QUEUE_DIR, f"{req_id}.json")
        if not os.path.exists(path):
            return self._send_json({"ok": False, "error": "request not found"}, status=404)
        with open(path, "r") as fh:
            req = json.load(fh)
        req["status"] = "done"
        req["completed_at"] = f"{today_iso()} {now_hm()}"
        req["result"] = result
        done_dir = os.path.join(CLAUDE_QUEUE_DIR, "done")
        os.makedirs(done_dir, exist_ok=True)
        with open(os.path.join(done_dir, f"{req_id}.json"), "w") as fh:
            json.dump(req, fh, ensure_ascii=False, indent=2)
        os.remove(path)
        ts = req["completed_at"]
        agent_id = req.get("agent", "coo")
        preview = json.dumps(result, ensure_ascii=False)[:800] if result else "(sin contenido)"
        append_file(
            os.path.join(CHATS_DIR, f"{agent_id}.md"),
            f"\n### CLAUDE·TOOL-RESULT · {ts}\n"
            f"🔧 `{req.get('tool')}` → {preview}\n"
            f"(req `{req_id}` resuelto)\n"
        )
        return self._send_json({"ok": True, "id": req_id, "archived": True})

    def _handle_task(self, data):
        who = (data.get("who") or "coo").lower()
        prio = data.get("prio") or "p3"
        text = (data.get("text") or "").strip()
        if not text:
            return self._send_json({"ok": False, "error": "empty text"}, status=400)
        state = load_state()
        agent = next((a for a in state.get("agents", []) if a["id"] == who), None)
        label = agent["name"] if agent else who.upper()
        append_file(
            os.path.join(INBOX, "TASKS.md"),
            f"- [ ] @{who} {text} !{prio}  ({today_iso()} {now_hm()})"
        )
        push_worklog(state, label, f"Nueva tarea ({prio}): {text}")
        if agent:
            agent["currentTask"] = text
        save_state(state)
        return self._send_json({"ok": True, "agent": who, "state": state})

    def _handle_answer(self, data):
        agent_id = data.get("agent")
        answer = (data.get("answer") or "").strip()
        if not agent_id or not answer:
            return self._send_json({"ok": False, "error": "need agent + answer"}, status=400)
        state = load_state()
        agent = next((a for a in state.get("agents", []) if a["id"] == agent_id), None)
        if not agent:
            return self._send_json({"ok": False, "error": "agent not found"}, status=404)
        prev_q = agent.get("question")
        agent["question"] = None
        agent["lastOutput"] = f"CEO respondió: {answer[:80]}"
        push_worklog(state, agent["name"], f"CEO respondió: {answer[:120]}")
        append_file(
            os.path.join(CEO_DIR, "DECISIONS.md"),
            f"\n## {today_iso()} {now_hm()} · {agent['name']}\n"
            f"**Pregunta:** {prev_q or '—'}\n\n**Respuesta:** {answer}\n"
        )
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_decision(self, data):
        approval_id = data.get("id")
        decision = (data.get("decision") or "").strip()
        if not approval_id or not decision:
            return self._send_json({"ok": False, "error": "need id + decision"}, status=400)
        state = load_state()
        aps = state.get("approvals", [])
        ap = next((x for x in aps if x["id"] == approval_id), None)
        if not ap:
            return self._send_json({"ok": False, "error": "approval not found"}, status=404)
        owner = ap.get("owner")
        owner_agent = next((a for a in state.get("agents", []) if a["id"] == owner), None)
        owner_name = owner_agent["name"] if owner_agent else owner or "?"
        append_file(
            os.path.join(CEO_DIR, "DECISIONS.md"),
            f"\n## {today_iso()} {now_hm()} · {approval_id} — {ap.get('title','')}\n"
            f"**Owner:** {owner_name}\n**Status previo:** {ap.get('status','')}\n\n"
            f"**Decisión:** {decision}\n"
        )
        # remove from approvals queue
        state["approvals"] = [x for x in aps if x["id"] != approval_id]
        push_worklog(state, "CEO", f"Decisión {approval_id}: {decision[:120]}")
        if owner_agent:
            owner_agent["lastOutput"] = f"CEO decidió {approval_id}: {decision[:80]}"
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_agent_update(self, data):
        agent_id = data.get("agent")
        state = load_state()
        agent = next((a for a in state.get("agents", []) if a["id"] == agent_id), None)
        if not agent:
            return self._send_json({"ok": False, "error": "agent not found"}, status=404)

        # Check if lastOutput is being set with a successful result (for auto-close)
        has_output = "lastOutput" in data
        output_value = data.get("lastOutput", "")
        output_path = data.get("lastOutputPath", "")

        for k in ("currentTask", "lastOutput", "mood", "status", "question"):
            if k in data:
                agent[k] = data[k]
        push_worklog(state, agent["name"], f"Update: {agent.get('currentTask','')[:80]}")
        save_state(state)

        # Auto-close task if we have a successful output
        if has_output and output_value and output_path:
            summary = output_value[:80] if isinstance(output_value, str) else str(output_value)[:80]
            _close_task_by_output(agent_id, output_path, summary)

        return self._send_json({"ok": True, "state": state})

    def _handle_worklog(self, data):
        state = load_state()
        push_worklog(state, data.get("agent", "SYS"), data.get("txt", ""))
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_financials(self, data):
        """Receive financial data push from Google Apps Script bridge.
        Body: {gmp:{...}, uts:{...}, cargx:{...}} or full financials object."""
        state = load_state()
        fin = state.setdefault("financials", {})
        fin["lastSyncedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
        # Accept either {financials:{gmp,uts,cargx}} or direct {gmp,uts,cargx}
        source = data.get("financials", data)
        for eng in ("gmp", "uts", "cargx"):
            if eng in source:
                fin[eng] = source[eng]
        push_worklog(state, "SYS", f"💰 Financial sync: {', '.join(k for k in ('gmp','uts','cargx') if k in source)}")
        save_state(state)
        return self._send_json({"ok": True, "lastSyncedAt": fin["lastSyncedAt"]})

    def _handle_patch(self, data):
        """Deep-merge top-level keys. Careful — replaces lists."""
        state = load_state()
        for k, v in data.items():
            state[k] = v
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_dialogue(self, data):
        """Record an agent-to-agent dialogue bubble that the UI will show for N seconds."""
        frm = data.get("from")
        to = data.get("to")
        text = (data.get("text") or "").strip()
        duration = int(data.get("durationSec") or 8)
        if not frm or not text:
            return self._send_json({"ok": False, "error": "need from + text"}, status=400)
        state = load_state()
        now = datetime.now().astimezone()
        entry = {
            "from": frm,
            "to": to,
            "text": text,
            "ts": now.isoformat(timespec="seconds"),
            "expiresAt": now.timestamp() + duration,
        }
        state.setdefault("dialogues", []).append(entry)
        # keep last 30
        state["dialogues"] = state["dialogues"][-30:]
        # mirror in worklog for durable record
        frm_agent = next((a for a in state.get("agents", []) if a["id"] == frm), None)
        label = frm_agent["name"] if frm_agent else frm.upper()
        to_label = ""
        if to:
            to_agent = next((a for a in state.get("agents", []) if a["id"] == to), None)
            to_label = f" → {to_agent['name']}" if to_agent else f" → {to}"
        push_worklog(state, label, f"💬{to_label}: {text}")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_active_work(self, data):
        """Mark one or more agents as actively working with an endsAt timestamp so UI pulses."""
        entries = data.get("entries") or []
        if not isinstance(entries, list) or not entries:
            return self._send_json({"ok": False, "error": "need entries list"}, status=400)
        state = load_state()
        now = datetime.now().astimezone().timestamp()
        aw = state.setdefault("activeWork", [])
        # prune expired
        aw = [x for x in aw if x.get("endsAt", 0) > now]
        agents_by_id = {a["id"]: a for a in state.get("agents", [])}
        for e in entries:
            aid = e.get("agent")
            task = (e.get("task") or "").strip()
            dur = int(e.get("durationSec") or 180)
            if not aid or aid not in agents_by_id:
                continue
            # replace any existing entry for same agent
            aw = [x for x in aw if x.get("agent") != aid]
            aw.append({
                "agent": aid,
                "task": task,
                "startedAt": now,
                "endsAt": now + dur,
            })
            agents_by_id[aid]["activity"] = "working"
            if task:
                agents_by_id[aid]["currentTask"] = task
        state["activeWork"] = aw
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_snapshot(self, data):
        """Save a Drive or Gmail snapshot. Body: {kind:'drive'|'mail', items:[...]}"""
        kind = data.get("kind")
        items = data.get("items") or []
        if kind not in ("drive", "mail"):
            return self._send_json({"ok": False, "error": "kind must be drive|mail"}, status=400)
        state = load_state()
        key = "driveSnapshot" if kind == "drive" else "mailSnapshot"
        field = "files" if kind == "drive" else "threads"
        state[key] = {
            "lastPulledAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            field: items[:50],
        }
        push_worklog(state, "SYS", f"Snapshot {kind} actualizado · {len(items)} items")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    # ---------- Tier A: memory streams + reflection ----------

    def _handle_memory_event(self, data):
        """Append an event to an agent's memory stream. Body: {agent, kind, text, meta?}"""
        agent_id = data.get("agent")
        kind = (data.get("kind") or "observation").strip()
        text = (data.get("text") or "").strip()
        if not agent_id or not text:
            return self._send_json({"ok": False, "error": "need agent + text"}, status=400)
        state = load_state()
        streams = state.setdefault("memoryStreams", {})
        arr = streams.setdefault(agent_id, [])
        arr.append({
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            "text": text,
            "meta": data.get("meta") or {},
        })
        # keep last 50 per agent
        streams[agent_id] = arr[-50:]
        save_state(state)
        return self._send_json({"ok": True, "count": len(streams[agent_id])})

    def _handle_reflect(self, data):
        """Save a reflection insight. Body: {agent, insight, scope?:'daily'|'weekly'}"""
        agent_id = data.get("agent")
        insight = (data.get("insight") or "").strip()
        scope = data.get("scope") or "daily"
        if not agent_id or not insight:
            return self._send_json({"ok": False, "error": "need agent + insight"}, status=400)
        state = load_state()
        agent = next((a for a in state.get("agents", []) if a["id"] == agent_id), None)
        if not agent:
            return self._send_json({"ok": False, "error": "agent not found"}, status=404)
        refl = state.setdefault("reflections", {})
        arr = refl.setdefault(agent_id, [])
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "scope": scope,
            "text": insight,
        }
        arr.append(entry)
        refl[agent_id] = arr[-30:]
        # also append to memory pack markdown file
        mp_rel = agent.get("memoryPath", "")
        if mp_rel:
            mp_path = os.path.join(ROOT, mp_rel)
            try:
                append_file(
                    mp_path,
                    f"\n## Reflection · {today_iso()} {now_hm()} ({scope})\n{insight}\n"
                )
            except Exception as e:
                print(f"[warn] could not write memory pack {mp_path}: {e}", file=sys.stderr)
        push_worklog(state, agent["name"], f"🪞 Reflection ({scope}): {insight[:100]}")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    # ---------- Tier B: thinking status toast ----------

    def _handle_thinking_status(self, data):
        """Body: {running:bool, agents?:[ids], note?}"""
        state = load_state()
        running = bool(data.get("running"))
        state["thinkingLoopStatus"] = {
            "running": running,
            "agents": data.get("agents") or [],
            "note": data.get("note") or "",
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if running:
            push_worklog(state, "COO", f"🧠 Thinking loop ON · {', '.join(data.get('agents') or [])}")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    # ---------- Tier C: daily plan + report ----------

    def _handle_plan(self, data):
        """Body: {agent, items:[...], date?}"""
        agent_id = data.get("agent")
        items = data.get("items") or []
        date = data.get("date") or today_iso()
        if not agent_id or not items:
            return self._send_json({"ok": False, "error": "need agent + items"}, status=400)
        state = load_state()
        plans = state.setdefault("dailyPlans", {})
        plans[agent_id] = {
            "date": date,
            "items": items,
            "report": None,
            "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        agent = next((a for a in state.get("agents", []) if a["id"] == agent_id), None)
        label = agent["name"] if agent else agent_id
        push_worklog(state, label, f"📋 Plan del día: {len(items)} items")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_report(self, data):
        """Body: {agent, report, done?:[items]}"""
        agent_id = data.get("agent")
        report = (data.get("report") or "").strip()
        if not agent_id or not report:
            return self._send_json({"ok": False, "error": "need agent + report"}, status=400)
        state = load_state()
        plans = state.setdefault("dailyPlans", {})
        cur = plans.get(agent_id) or {"date": today_iso(), "items": [], "createdAt": None}
        cur["report"] = report
        cur["done"] = data.get("done") or []
        cur["reportedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
        plans[agent_id] = cur
        agent = next((a for a in state.get("agents", []) if a["id"] == agent_id), None)
        label = agent["name"] if agent else agent_id
        push_worklog(state, label, f"📊 EOD report: {report[:120]}")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

# ---------- main ----------

def main():
    os.chdir(ROOT)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 60)
    print("  JV HOLDINGS · OFICINA VIVA · backend corriendo")
    print(f"  → http://{HOST}:{PORT}/OFFICE_SIM.html")
    print(f"  Root:  {ROOT}")
    print(f"  State: {STATE_FILE}")
    print("  Ctrl+C para detener")
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[bye] oficina cerrada")

if __name__ == "__main__":
    main()
