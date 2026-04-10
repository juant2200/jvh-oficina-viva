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
  • GET  /api/agent/<id>                    → full agent detail + recent worklog
  • POST /api/memory_event                  → push event to agent memory stream (Tier A)
  • GET  /api/memory/<id>                   → read agent memory stream + context pack
  • POST /api/reflect                       → save reflection insight to memory pack (Tier A)
  • POST /api/thinking_status               → toast: thinking loop on/off (Tier B)
  • POST /api/plan                          → agent sets daily plan (Tier C)
  • POST /api/report                        → agent sets EOD report (Tier C)
  • GET  /api/ceo_inbox                     → aggregated questions + approvals + escalations (Tier B)
  • GET  /api/health                        → heartbeat

Run:    python3 server.py
Open:   http://localhost:8765/OFFICE_SIM.html
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

# Global lock serializing ALL state file reads/writes. ThreadingHTTPServer
# spawns a thread per request; without this lock, concurrent save_state
# calls race on the same .tmp file and produce concatenated/corrupt JSON.
_STATE_LOCK = threading.RLock()

ROOT = os.path.dirname(os.path.abspath(__file__))

# --- env-var config (Railway-friendly) ---
PORT      = int(os.environ.get("PORT", "8765"))
HOST      = os.environ.get("HOST", "0.0.0.0")
STATE_DIR = os.environ.get("STATE_DIR", ROOT)

STATE_FILE = os.path.join(STATE_DIR, "office_state.json")
INBOX      = os.path.join(STATE_DIR, "INBOX")
CEO_DIR    = os.path.join(STATE_DIR, "_CEO")

# --- HTTP Basic Auth ---
AUTH_USER  = os.environ.get("OFFICE_USER", "jvh")
AUTH_PASS  = os.environ.get("OFFICE_PASS", "")   # empty = no auth
AUTH_REALM = "JV Holdings Oficina Viva"
AUTH_EXEMPT_PATHS = {"/api/health"}

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(INBOX, exist_ok=True)
os.makedirs(CEO_DIR, exist_ok=True)

# --- seed state from bundled default on first boot ---
_BUNDLED_STATE = os.path.join(ROOT, "office_state.json")
if not os.path.exists(STATE_FILE) and os.path.exists(_BUNDLED_STATE) and _BUNDLED_STATE != STATE_FILE:
    try:
        shutil.copy2(_BUNDLED_STATE, STATE_FILE)
        print(f"[boot] seeded state → {STATE_FILE}", file=sys.stderr)
    except Exception as e:
        print(f"[boot] failed to seed state: {e}", file=sys.stderr)

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

def now_hm():
    return datetime.now().strftime("%H:%M")

def today_iso():
    return datetime.now().strftime("%Y-%m-%d")

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
    def log_message(self, fmt, *args):
        # quieter logs
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    # --- HTTP Basic Auth gate ---
    def _check_auth(self):
        if not AUTH_PASS:                       # no password set → open
            return True
        path = urlparse(self.path).path
        if path in AUTH_EXEMPT_PATHS:           # health check always open
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
        if not self._check_auth():
            return
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

    # ---------- route handlers ----------

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
    auth_info = "ON (user=" + AUTH_USER + ")" if AUTH_PASS else "OFF"
    print(f"  Auth:  {auth_info}")
    print("  Ctrl+C para detener")
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[bye] oficina cerrada")

if __name__ == "__main__":
    main()
