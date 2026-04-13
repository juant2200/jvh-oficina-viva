#!/usr/bin/env python3
"""
JV Holdings Â· Oficina Viva â local backend (WITH CLAUDE AI CHAT)
=================================================================
HTTP server that:
â¢ Serves OFFICE_SIM.html + static files from this folder
â¢ All original API endpoints preserved exactly
â¢ NEW: POST /api/chat â SSE streaming chat with agents via Claude API
â¢ NEW: GET /api/chat_history/<id> â chat history per agent
â¢ Injects chat_agent.js into OFFICE_SIM.html automatically

Run:  python3 server.py
Open: http://localhost:8765/OFFICE_SIM.html
"""
import base64
import json
import os
import shutil
import sys
import re
import threading
import uuid
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

_STATE_LOCK = threading.RLock()
ROOT = os.path.dirname(os.path.abspath(__file__))

# --- env-var config (Railway-friendly) ---
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "0.0.0.0")
STATE_DIR = os.environ.get("STATE_DIR", ROOT)
STATE_FILE = os.path.join(STATE_DIR, "office_state.json")
INBOX = os.path.join(STATE_DIR, "INBOX")
CEO_DIR = os.path.join(STATE_DIR, "_CEO")

# --- HTTP Basic Auth ---
AUTH_USER = os.environ.get("OFFICE_USER", "jvh")
AUTH_PASS = os.environ.get("OFFICE_PASS", "")
AUTH_REALM = "JV Holdings Oficina Viva"
AUTH_EXEMPT_PATHS = {"/api/health"}

# --- NEW: Anthropic API key for Claude chat ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(INBOX, exist_ok=True)
os.makedirs(CEO_DIR, exist_ok=True)

# --- seed state from bundled default on first boot ---
_BUNDLED_STATE = os.path.join(ROOT, "office_state.json")
if not os.path.exists(STATE_FILE) and os.path.exists(_BUNDLED_STATE) and _BUNDLED_STATE != STATE_FILE:
    try:
        shutil.copy2(_BUNDLED_STATE, STATE_FILE)
        print(f"[boot] seeded state -> {STATE_FILE}", file=sys.stderr)
    except Exception as e:
        print(f"[boot] failed to seed state: {e}", file=sys.stderr)

# ========== NEW: AGENT SYSTEM PROMPTS ==========
AGENT_PROMPTS = {
    "coo": {
        "name": "COO",
        "role": "Chief Operating Officer",
        "prompt": "Eres el COO de JV Holdings. Tu nombre es COO. Coordinas todas las operaciones del grupo. Hablas en espaÃ±ol. Eres directo, estratÃ©gico y orientado a resultados. Tienes visibilidad de todos los departamentos. Puedes asignar tareas, priorizar proyectos y tomar decisiones operativas. Respondes de forma concisa y accionable."
    },
    "finance": {
        "name": "Finance",
        "role": "Director Financiero",
        "prompt": "Eres el Director Financiero de JV Holdings. Manejas cash flow, forecasts, P&L, tax planning y todo lo financiero. Hablas en espaÃ±ol. Eres preciso con los nÃºmeros, prudente con el gasto y proactivo con oportunidades de optimizaciÃ³n fiscal. Respondes con datos y recomendaciones claras."
    },
    "legal": {
        "name": "Legal/HR",
        "role": "Director Legal y RRHH",
        "prompt": "Eres el Director Legal y de RRHH de JV Holdings. Manejas contratos, compliance, estructura societaria, SPVs, contrataciones y asuntos legales. Hablas en espaÃ±ol. Eres meticuloso, cauteloso con el riesgo legal y claro en tus recomendaciones. Siempre consideras la estructura corporativa Ã³ptima."
    },
    "ops": {
        "name": "Ops",
        "role": "Director de Operaciones",
        "prompt": "Eres el Director de Operaciones de JV Holdings. Ejecutas SOPs, onboarding, launch checklists, y aseguras que todo se implemente correctamente. Hablas en espaÃ±ol. Eres sistemÃ¡tico, detallista y orientado a procesos. Creas checklists y SOPs cuando es necesario."
    },
    "bd": {
        "name": "BD",
        "role": "Director de Business Development",
        "prompt": "Eres el Director de Business Development de JV Holdings. Manejas pipeline de clientes, partnerships, deals y oportunidades comerciales. Hablas en espaÃ±ol. Eres persuasivo, orientado a resultados y excelente identificando oportunidades. Piensas en revenue y crecimiento."
    },
    "marketing": {
        "name": "Marketing",
        "role": "Director de Marketing",
        "prompt": "Eres el Director de Marketing de JV Holdings. Creas estrategias de marketing, landing pages, pitch decks, one-pagers, contenido y campaÃ±as. Hablas en espaÃ±ol. Eres creativo, orientado a conversiÃ³n y entiendes branding. Puedes crear contenido, diseÃ±ar estrategias y ejecutar campaÃ±as."
    },
    "strategy": {
        "name": "Strategy",
        "role": "Director de Estrategia",
        "prompt": "Eres el Director de Estrategia de JV Holdings. Analizas mercados, competencia, oportunidades de expansiÃ³n y modelos de negocio. Hablas en espaÃ±ol. Eres analÃ­tico, visionario y basado en datos. Piensas en largo plazo y ventajas competitivas."
    },
    "research": {
        "name": "Research",
        "role": "Director de Research",
        "prompt": "Eres el Director de Research de JV Holdings. Investigas mercados, tendencias, competidores y oportunidades. Hablas en espaÃ±ol. Eres metÃ³dico, curioso y basado en evidencia. Produces reportes detallados y findings accionables."
    },
    "exec": {
        "name": "Exec",
        "role": "Asistente Ejecutivo del CEO",
        "prompt": "Eres el Asistente Ejecutivo del CEO de JV Holdings. Manejas agenda, follow-ups, coordinaciÃ³n con otros departamentos y asuntos del CEO. Hablas en espaÃ±ol. Eres eficiente, organizado y anticipas las necesidades del CEO. Priorizas y filtras informaciÃ³n."
    }
}

# ---------- state helpers (ORIGINAL) ----------
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

def now_hm():
    return datetime.now().strftime("%H:%M")

def today_iso():
    return datetime.now().strftime("%Y-%m-%d")

def push_worklog(state, agent, txt, persist_md=True):
    entry = {"t": now_hm(), "agent": agent, "txt": txt}
    state.setdefault("worklog", []).append(entry)
    state["worklog"] = state["worklog"][-100:]
    if persist_md:
        append_file(
            os.path.join(INBOX, "WORKLOG.md"),
            f"- {today_iso()} {entry['t']} [{agent}] {txt}"
        )

# ---------- request handler ----------
class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    # --- HTTP Basic Auth gate ---
    def _check_auth(self):
        if not AUTH_PASS:
            return True
        path = urlparse(self.path).path
        if path in AUTH_EXEMPT_PATHS:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8", errors="replace")
                user, _, pw = decoded.partition(":")
                if user == AUTH_USER and pw == AUTH_PASS:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authentication required\n")
        return False

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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        SimpleHTTPRequestHandler.end_headers(self)
# Serve office_state.json from STATE_DIR (not deploy dir)
        if parsed.path == "/office_state.json":
            return self._send_json(load_state())
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)

        # ========== NEW: Serve OFFICE_SIM.html with chat_agent.js injection ==========
        if parsed.path == "/OFFICE_SIM.html" or parsed.path == "/office_sim.html":
            html_path = os.path.join(ROOT, "OFFICE_SIM.html")
            if os.path.exists(html_path):
                with open(html_path, "r", encoding="utf-8") as f:
                    html = f.read()
                inject = '<script src="/chat_agent.js"></script>'
                if inject not in html:
                    html = html.replace("</body>", f"{inject}\n</body>")
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # Skip self.end_headers() to avoid double CORS
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                SimpleHTTPRequestHandler.end_headers(self)
                self.wfile.write(body)
                return

        # ========== NEW: Serve chat_agent.js ==========
        if parsed.path == "/chat_agent.js":
            js_path = os.path.join(ROOT, "chat_agent.js")
            if os.path.exists(js_path):
                with open(js_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                SimpleHTTPRequestHandler.end_headers(self)
                self.wfile.write(body)
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # GET /api/health
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
            wl = state.get("worklog", [])
            name_norm = agent["name"].lower()
            mine = [w for w in wl if name_norm in w.get("agent","").lower()][-30:]
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

        # ========== NEW: GET /api/chat_history/<id> ==========
        m = re.match(r"^/api/chat_history/([a-z_-]+)$", parsed.path)
        if m:
            agent_id = m.group(1)
            state = load_state()
            hist = state.get("chatHistory", {}).get(agent_id, [])
            return self._send_json({"ok": True, "history": hist[-50:]})

        # GET /api/ceo_inbox
        if parsed.path == "/api/ceo_inbox":
            state = load_state()
            questions = [
                {"agent": a["id"], "name": a["name"], "color": a.get("color"),
                 "question": a.get("question")}
                for a in state.get("agents", [])
                if a.get("question")
            ]
            approvals = state.get("approvals", [])
            wl = state.get("worklog", [])
            escalations = [w for w in wl[-40:]
                          if "escalate" in w.get("txt","").lower() or "\U0001f6a8" in w.get("txt","")]
            thinking = state.get("thinkingLoopStatus", {"running": False})
            drive = state.get("driveSnapshot", {})
            mail = state.get("mailSnapshot", {})
            return self._send_json({
                "ok": True,
                "questions": questions,
                "approvals": approvals,
                "escalations": escalations,
                "thinking": thinking,
                "drive": {"lastPulledAt": drive.get("lastPulledAt"),
                         "count": len(drive.get("files", []))},
                "mail": {"lastPulledAt": mail.get("lastPulledAt"),
                        "count": len(mail.get("threads", []))},
            })

        # Redirect / to /OFFICE_SIM.html
        if parsed.path == "/" or parsed.path == "":
            self.send_response(302)
            self.send_header("Location", "/OFFICE_SIM.html")
            self.end_headers()
            return

        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._send_json({"ok": False, "error": "invalid json"}, status=400)

        route = parsed.path

        # ========== NEW: /api/chat (SSE streaming) â BEFORE the lock ==========
        if route == "/api/chat":
            return self._handle_chat(data)

        # --- All original POST handlers under the state lock ---
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
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            return self._send_json({"ok": False, "error": str(e)}, status=500)

        return self._send_json({"ok": False, "error": "unknown route"}, status=404)

    # ---------- ORIGINAL route handlers (exact same signatures) ----------

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
            f"- [ ] @{who} {text} !{prio} ({today_iso()} {now_hm()})"
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
        agent["lastOutput"] = f"CEO respondio: {answer[:80]}"
        push_worklog(state, agent["name"], f"CEO respondio: {answer[:120]}")
        append_file(
            os.path.join(CEO_DIR, "DECISIONS.md"),
            f"\n## {today_iso()} {now_hm()} - {agent['name']}\n"
            f"**Pregunta:** {prev_q or chr(8212)}\n\n**Respuesta:** {answer}\n"
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
            f"\n## {today_iso()} {now_hm()} - {approval_id} -- {ap.get('title','')}\n"
            f"**Owner:** {owner_name}\n**Status previo:** {ap.get('status','')}\n\n"
            f"**Decision:** {decision}\n"
        )
        state["approvals"] = [x for x in aps if x["id"] != approval_id]
        push_worklog(state, "CEO", f"Decision {approval_id}: {decision[:120]}")
        if owner_agent:
            owner_agent["lastOutput"] = f"CEO decidio {approval_id}: {decision[:80]}"
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_agent_update(self, data):
        agent_id = data.get("agent")
        state = load_state()
        agent = next((a for a in state.get("agents", []) if a["id"] == agent_id), None)
        if not agent:
            return self._send_json({"ok": False, "error": "agent not found"}, status=404)
        for k in ("currentTask", "lastOutput", "mood", "status", "question"):
            if k in data:
                agent[k] = data[k]
        push_worklog(state, agent["name"], f"Update: {agent.get('currentTask','')[:80]}")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_worklog(self, data):
        state = load_state()
        push_worklog(state, data.get("agent", "SYS"), data.get("txt", ""))
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_patch(self, data):
        """Deep-merge top-level keys. Careful -- replaces lists."""
        state = load_state()
        for k, v in data.items():
            state[k] = v
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_dialogue(self, data):
        """Record an agent-to-agent dialogue bubble."""
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
        state["dialogues"] = state["dialogues"][-30:]
        frm_agent = next((a for a in state.get("agents", []) if a["id"] == frm), None)
        label = frm_agent["name"] if frm_agent else frm.upper()
        to_label = ""
        if to:
            to_agent = next((a for a in state.get("agents", []) if a["id"] == to), None)
            to_label = f" -> {to_agent['name']}" if to_agent else f" -> {to}"
        push_worklog(state, label, f"\U0001f4ac{to_label}: {text}")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_active_work(self, data):
        """Mark one or more agents as actively working with an endsAt timestamp."""
        entries = data.get("entries") or []
        if not isinstance(entries, list) or not entries:
            return self._send_json({"ok": False, "error": "need entries list"}, status=400)
        state = load_state()
        now = datetime.now().astimezone().timestamp()
        aw = state.setdefault("activeWork", [])
        aw = [x for x in aw if x.get("endsAt", 0) > now]
        agents_by_id = {a["id"]: a for a in state.get("agents", [])}
        for e in entries:
            aid = e.get("agent")
            task = (e.get("task") or "").strip()
            dur = int(e.get("durationSec") or 180)
            if not aid or aid not in agents_by_id:
                continue
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
        """Save a Drive or Gmail snapshot."""
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
        push_worklog(state, "SYS", f"Snapshot {kind} actualizado - {len(items)} items")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    # ---------- Tier A: memory streams + reflection ----------

    def _handle_memory_event(self, data):
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
        streams[agent_id] = arr[-50:]
        save_state(state)
        return self._send_json({"ok": True, "count": len(streams[agent_id])})

    def _handle_reflect(self, data):
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
        mp_rel = agent.get("memoryPath", "")
        if mp_rel:
            mp_path = os.path.join(ROOT, mp_rel)
            try:
                append_file(
                    mp_path,
                    f"\n## Reflection - {today_iso()} {now_hm()} ({scope})\n{insight}\n"
                )
            except Exception as e:
                print(f"[warn] could not write memory pack {mp_path}: {e}", file=sys.stderr)
        push_worklog(state, agent["name"], f"\U0001fa9e Reflection ({scope}): {insight[:100]}")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    # ---------- Tier B: thinking status toast ----------

    def _handle_thinking_status(self, data):
        state = load_state()
        running = bool(data.get("running"))
        state["thinkingLoopStatus"] = {
            "running": running,
            "agents": data.get("agents") or [],
            "note": data.get("note") or "",
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if running:
            push_worklog(state, "COO", f"\U0001f9e0 Thinking loop ON - {', '.join(data.get('agents') or [])}")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    # ---------- Tier C: daily plan + report ----------

    def _handle_plan(self, data):
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
        push_worklog(state, label, f"\U0001f4cb Plan del dia: {len(items)} items")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    def _handle_report(self, data):
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
        push_worklog(state, label, f"\U0001f4ca EOD report: {report[:120]}")
        save_state(state)
        return self._send_json({"ok": True, "state": state})

    # ========== NEW: Claude AI Chat ==========

    def _handle_chat(self, data):
        """SSE streaming chat with an agent via Claude API."""
        agent_id = data.get("agent")
        message = (data.get("message") or "").strip()

        if not agent_id or not message:
            self.send_response(400)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            SimpleHTTPRequestHandler.end_headers(self)
            self.wfile.write(b"data: {\"error\": \"need agent + message\"}\n\n")
            return

        if not ANTHROPIC_API_KEY:
            self.send_response(503)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            SimpleHTTPRequestHandler.end_headers(self)
            self.wfile.write(b"data: {\"error\": \"ANTHROPIC_API_KEY not configured\"}\n\n")
            return

        agent_config = AGENT_PROMPTS.get(agent_id)
        if not agent_config:
            self.send_response(404)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            SimpleHTTPRequestHandler.end_headers(self)
            self.wfile.write(b"data: {\"error\": \"agent not found\"}\n\n")
            return

        # Build context from state
        state = load_state()
        agent_data = next((a for a in state.get("agents", []) if a["id"] == agent_id), {})

        # Get memory streams
        mem_streams = state.get("memoryStreams", {}).get(agent_id, [])[-10:]
        reflections = state.get("reflections", {}).get(agent_id, [])[-5:]
        plan = state.get("dailyPlans", {}).get(agent_id, {})

        # Get recent worklog for this agent
        wl = state.get("worklog", [])
        name_norm = agent_config["name"].lower()
        recent_work = [w for w in wl if name_norm in w.get("agent","").lower()][-10:]

        # Get chat history
        chat_hist = state.get("chatHistory", {}).get(agent_id, [])[-20:]

        # Build system prompt with context
        context_parts = [agent_config["prompt"]]
        context_parts.append(
            "\nEmpresa: JV Holdings. Holding con varias empresas: "
            "GMP (media/publicidad), UTS (tech services), CargX (logistica), y otras."
        )
        context_parts.append(f"\nTu tarea actual: {agent_data.get('currentTask', 'ninguna asignada')}")
        context_parts.append(f"\nTu estado de animo: {agent_data.get('mood', 'neutral')}")

        if recent_work:
            context_parts.append("\nActividad reciente:")
            for w in recent_work[-5:]:
                context_parts.append(f"  - [{w.get('t', '')}] {w.get('txt', '')}")

        if mem_streams:
            context_parts.append("\nMemoria reciente:")
            for s in mem_streams[-5:]:
                context_parts.append(f"  - [{s.get('kind', '')}] {s.get('text', '')}")

        if reflections:
            context_parts.append("\nReflexiones:")
            for r in reflections[-3:]:
                context_parts.append(f"  - {r.get('text', '')}")

        if plan and plan.get("items"):
            context_parts.append(f"\nPlan del dia: {', '.join(str(i) for i in plan['items'][:5])}")

        system_prompt = "\n".join(context_parts)

        # Build messages array from chat history + new message
        messages = []
        for entry in chat_hist[-10:]:
            messages.append({"role": entry["role"], "content": entry["content"]})
        messages.append({"role": "user", "content": message})

        # Start SSE response
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        SimpleHTTPRequestHandler.end_headers(self)

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

            full_response = ""

            with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=system_prompt,
                messages=messages
            ) as stream:
                for text in stream.text_stream:
                    full_response += text
                    chunk = json.dumps({"token": text}, ensure_ascii=False)
                    self.wfile.write(f"data: {chunk}\n\n".encode("utf-8"))
                    self.wfile.flush()

            # Send done event
            self.wfile.write(b"data: {\"done\": true}\n\n")
            self.wfile.flush()

            # Save to chat history
            with _STATE_LOCK:
                state = load_state()
                hist = state.setdefault("chatHistory", {})
                agent_hist = hist.setdefault(agent_id, [])
                agent_hist.append({
                    "role": "user",
                    "content": message,
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds")
                })
                agent_hist.append({
                    "role": "assistant",
                    "content": full_response,
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds")
                })
                hist[agent_id] = agent_hist[-100:]

                agent_obj = next((a for a in state.get("agents", []) if a["id"] == agent_id), None)
                if agent_obj:
                    agent_obj["lastOutput"] = full_response[:120]
                    agent_obj["mood"] = "working"

                push_worklog(state, agent_config["name"],
                           f"\U0001f4ac Chat CEO: {message[:60]} -> {full_response[:60]}")
                save_state(state)

        except ImportError:
            err = json.dumps({"error": "anthropic package not installed"}, ensure_ascii=False)
            self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception as e:
            err = json.dumps({"error": str(e)}, ensure_ascii=False)
            self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
            self.wfile.flush()

    # ---------- main ----------

def main():
    os.chdir(ROOT)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 60)
    print("  JV HOLDINGS - OFICINA VIVA - backend corriendo")
    print(f"  -> http://{HOST}:{PORT}/OFFICE_SIM.html")
    print(f"  Root:  {ROOT}")
    print(f"  State: {STATE_FILE}")
    auth_info = "ON (user=" + AUTH_USER + ")" if AUTH_PASS else "OFF"
    print(f"  Auth:  {auth_info}")
    if ANTHROPIC_API_KEY:
        print(f"  Claude Chat: ENABLED")
    else:
        print(f"  Claude Chat: DISABLED (set ANTHROPIC_API_KEY)")
    print("  Ctrl+C para detener")
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[bye] oficina cerrada")

if __name__ == "__main__":
    main()
