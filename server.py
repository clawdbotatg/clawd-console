#!/usr/bin/env python3
"""
clawd-console — a web terminal mirror for an INTERACTIVE (subscription-billed)
Claude Code session.

Why interactive (no -p): on 2026-06-15 `claude -p`/headless usage moves to a
separate metered Agent SDK credit pool. The interactive TUI keeps drawing on the
Claude subscription. So we run real `claude` (no -p) inside a pseudo-terminal and
mirror it to the browser.

We never parse the "weird text" the TUI emits. Two decoupled channels:
  • WRITE  -> keystrokes injected into the PTY (raw passthrough + a "send" helper)
  • READ   -> (a) raw PTY bytes streamed to xterm.js, which *renders* the ANSI
              faithfully (the live, token-level visual mirror), and
              (b) the session transcript JSONL (clean, structured, zero ANSI),
              tailed and forwarded so a controller can act on real events.

Pure Python stdlib. Reuses the PTY recipe from clawd-tg-claude/pty_probe.py and
the hand-rolled RFC 6455 WebSocket framing from clawd-web-claude/server.py.

Run:
  python3 server.py
  PORT=7900 WORKDIR=/some/dir CLAUDE_BIN=claude python3 server.py
Then open http://127.0.0.1:7900
"""

import base64
import fcntl
import glob
import hashlib
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, TCPServer

# ── config ──────────────────────────────────────────────────────────────────
PORT       = int(os.environ.get("PORT", "7900"))
BIND       = os.environ.get("BIND", "0.0.0.0")   # 0.0.0.0 = reachable on the LAN
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
WORKDIR    = os.path.abspath(os.environ.get("WORKDIR", os.getcwd()))
COLS       = int(os.environ.get("COLS", "120"))
ROWS       = int(os.environ.get("ROWS", "34"))
RING_MAX   = int(os.environ.get("RING_MAX", str(256 * 1024)))  # replay buffer cap
# Settle gap between typing a message and pressing Enter. Claude's TUI treats a
# fast text+CR burst as a multi-line *paste* (CR becomes a newline, not submit);
# a pause lets the paste finalize so the CR registers as Enter. <0.6s fails here.
# Big/multi-line pastes need the full settle; short one-liners only need to clear
# the 0.6s cliff, so they submit ~2x faster (SEND_SETTLE_MIN).
SEND_SETTLE     = float(os.environ.get("SEND_SETTLE", "1.5"))
SEND_SETTLE_MIN = float(os.environ.get("SEND_SETTLE_MIN", "0.7"))

# Env vars that, when inherited, put a spawned `claude` into a nested/embedded
# mode (e.g. it stops writing a normal session transcript). We scrub them so the
# child is a pristine, top-level interactive session — and drop the API key so
# it authenticates with the subscription (OAuth), not metered API credits.
SCRUB_ENV = [
    "ANTHROPIC_API_KEY",
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_EFFORT",
    "AI_AGENT",
]

WS_GUID    = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 magic
HERE       = Path(__file__).resolve().parent
UPLOAD_DIR = Path(WORKDIR) / ".clawd-console-uploads"   # pasted images land here
MAX_UPLOAD = 25 * 1024 * 1024
EXT_BY_CTYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                "image/webp": ".webp"}
SESSION_FILE = HERE / ".clawd-console.session"       # persists the session id across restarts

# Shared secret. Required on /ws and /hook because we bind to the LAN and the
# session runs with bypass-permissions — without it anyone on the wifi could run
# commands as you. Persisted so the URL/QR stays stable across restarts.
def _load_or_make_token():
    env = os.environ.get("CONSOLE_TOKEN")
    if env:
        return env
    tok_file = HERE / ".clawd-console.token"
    try:
        return tok_file.read_text().strip()
    except OSError:
        tok = uuid.uuid4().hex[:16]
        tok_file.write_text(tok)
        return tok

TOKEN = _load_or_make_token()


def lan_ip():
    """Best-effort primary LAN IP (no traffic actually sent)."""
    import socket as _s
    sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    try:
        sk.connect(("8.8.8.8", 80))
        return sk.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sk.close()


# ── PTY-backed Claude session ─────────────────────────────────────────────────
class ClaudeSession:
    """One interactive `claude` process in a PTY, mirrored to N websocket clients."""

    def __init__(self):
        self.session_id, self.resuming = self._pick_session_id()
        self.master_fd = None
        self.pid = None
        self.proc = None
        self.alive = False

        self.ring = bytearray()                  # recent PTY output for late joiners
        self.ring_lock = threading.Lock()

        self.clients = set()                     # set of _Client
        self.clients_lock = threading.Lock()

        self.transcript_path = None
        self.busy = False                        # working (turn in flight) vs idle
        self.last_tool = None
        self.settings_path = None

    def _pick_session_id(self):
        """Reuse the saved session id (and resume it) if its transcript exists;
        otherwise mint a fresh one. This is what lets the server restart — or be
        run as a daemon — without losing the conversation."""
        try:
            saved = SESSION_FILE.read_text().strip()
        except OSError:
            saved = ""
        if saved and glob.glob(os.path.expanduser(
                f"~/.claude/projects/*/{saved}.jsonl")):
            return saved, True
        sid = str(uuid.uuid4())
        try:
            SESSION_FILE.write_text(sid)
        except OSError:
            pass
        return sid, False

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        master, slave = pty.openpty()
        self._set_winsize(master, ROWS, COLS)

        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(COLS)
        env["LINES"] = str(ROWS)
        for k in SCRUB_ENV:                      # pristine top-level + subscription auth
            env.pop(k, None)

        self.settings_path = self._write_hook_settings()
        cmd = [CLAUDE_BIN,
               ("--resume" if self.resuming else "--session-id"), self.session_id,
               "--settings", self.settings_path]

        def _preexec():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)  # slave becomes controlling tty

        self.proc = subprocess.Popen(
            cmd, cwd=WORKDIR, env=env,
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=_preexec, close_fds=True,
        )
        os.close(slave)                          # parent only needs the master
        self.master_fd = master
        self.pid = self.proc.pid
        self.alive = True
        print(f"[session] claude pid={self.pid} session_id={self.session_id} "
              f"({'resumed' if self.resuming else 'new'})", flush=True)
        print(f"[session] workdir={WORKDIR}", flush=True)

        threading.Thread(target=self._pump_pty, daemon=True).start()
        threading.Thread(target=self._tail_transcript, daemon=True).start()

    def _write_hook_settings(self):
        """Generate a settings file that POSTs every hook event's stdin JSON to
        our /hook endpoint. Self-contained — passed via `claude --settings`, so
        it never touches the user's ~/.claude or project settings."""
        post = (f"curl -sS -m 2 -X POST http://127.0.0.1:{PORT}/hook?t={TOKEN} "
                f"--data-binary @- >/dev/null 2>&1 || true")
        one = [{"hooks": [{"type": "command", "command": post}]}]
        star = [{"matcher": "*", "hooks": [{"type": "command", "command": post}]}]
        settings = {"hooks": {
            "SessionStart": one, "SessionEnd": one,
            "UserPromptSubmit": one, "Stop": one, "Notification": one,
            "PreToolUse": star, "PostToolUse": star,
        }}
        path = str(HERE / ".clawd-console.hooks.json")
        Path(path).write_text(json.dumps(settings))
        return path

    def _set_winsize(self, fd, rows, cols):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def on_hook(self, obj):
        """Handle one hook callback (from claude via /hook) → update state +
        broadcast a slim event. This is the clean turn-boundary signal."""
        ev = obj.get("hook_event_name", "?")
        data = {}
        if ev == "UserPromptSubmit":
            self.busy = True
            data = {"prompt": obj.get("prompt", "")}
        elif ev == "PreToolUse":
            self.busy = True
            self.last_tool = obj.get("tool_name")
            data = {"tool": obj.get("tool_name")}
        elif ev == "PostToolUse":
            self.busy = True
            resp = obj.get("tool_response") or {}
            data = {"tool": obj.get("tool_name"),
                    "duration_ms": obj.get("duration_ms")}
        elif ev == "Stop":
            self.busy = False
            self.last_tool = None
            data = {"last": obj.get("last_assistant_message", "")}
        elif ev == "Notification":
            data = {"message": obj.get("message", "")}
        elif ev == "SessionStart":
            self.busy = False
            data = {"source": obj.get("source"), "model": obj.get("model")}
        elif ev == "SessionEnd":
            data = {"reason": obj.get("reason")}
        self._broadcast_json({"type": "hook", "event": ev,
                              "busy": self.busy, "tool": self.last_tool,
                              "data": data})

    def resize(self, cols, rows):
        if self.master_fd is not None and cols and rows:
            try:
                self._set_winsize(self.master_fd, int(rows), int(cols))
            except OSError:
                pass

    # -- write channel ---------------------------------------------------------
    def write(self, data: bytes):
        """Raw keystrokes -> PTY."""
        if self.master_fd is None:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def send_message(self, text: str):
        """High-level: type a message, let the paste settle, then submit (CR)."""
        self.write(text.encode("utf-8"))
        # Short one-liners only need to clear the 0.6s burst cliff; big or
        # multi-line pastes take longer to finalize, so keep the full settle.
        big = len(text) > 280 or text.count("\n") >= 1
        time.sleep(SEND_SETTLE if big else SEND_SETTLE_MIN)
        self.write(b"\r")

    # -- read channel: raw PTY bytes -> all clients ----------------------------
    def _pump_pty(self):
        while True:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not r:
                continue
            try:
                chunk = os.read(self.master_fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            with self.ring_lock:
                self.ring.extend(chunk)
                if len(self.ring) > RING_MAX:
                    del self.ring[:-RING_MAX]
            self._broadcast_bytes(chunk)
        self.alive = False
        print("[session] PTY closed / claude exited", flush=True)
        self._broadcast_json({"type": "exit"})

    # -- read channel: transcript JSONL -> structured events -------------------
    def _find_transcript(self):
        # Locate by session-id across all project dirs (robust to path encoding).
        hits = glob.glob(os.path.expanduser(
            f"~/.claude/projects/*/{self.session_id}.jsonl"))
        return hits[0] if hits else None

    def _tail_transcript(self):
        # Wait (indefinitely, while the session lives) for the file to appear;
        # claude creates it on the first turn, which may be long after launch.
        path = None
        while self.alive:
            path = self._find_transcript()
            if path:
                break
            time.sleep(0.25)
        if not path:
            return
        self.transcript_path = path
        print(f"[transcript] tailing {path}", flush=True)
        with open(path, "r") as f:
            buf = ""
            while True:
                line = f.readline()
                if not line:
                    if not self.alive:
                        # drain anything left, then stop
                        pass
                    time.sleep(0.2)
                    continue
                buf += line
                if not buf.endswith("\n"):
                    continue                     # partial line; wait for the rest
                raw, buf = buf, ""
                ev = self._slim_event(raw.strip())
                if ev:
                    self._broadcast_json({"type": "transcript", "event": ev})

    def _slim_event(self, line: str):
        """Reduce a raw transcript line to the bits a controller cares about.
        Event shapes mirror clawd-tg-claude/bot.py's stream-json handling."""
        if not line:
            return None
        try:
            obj = json.loads(line)
        except Exception:
            return None
        t = obj.get("type")
        if t == "user":
            content = (obj.get("message") or {}).get("content")
            text = content if isinstance(content, str) else _collect_text(content)
            if text:
                # local slash-command artifacts → clean events, not raw XML tags
                m = re.search(r"<command-name>([^<]*)</command-name>", text)
                if m:
                    name = m.group(1).strip()
                    am = re.search(r"<command-args>([^<]*)</command-args>", text)
                    args = am.group(1).strip() if am else ""
                    return {"role": "command", "text": (name + " " + args).strip()}
                m = re.search(r"<local-command-stdout>([\s\S]*?)</local-command-stdout>", text)
                if m:
                    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", m.group(1))  # strip ANSI
                    out = re.sub(r"<[^>]+>", "", out).strip()
                    return {"role": "system", "text": out} if out else None
                clean = _strip_noise(text).strip()
                if not clean:
                    return None
                return {"role": "user", "text": clean}
            # tool_result blocks arrive as user messages too
            tr = _collect_tool_results(content)
            if tr:
                return {"role": "tool_result", "results": tr}
            return None
        if t == "assistant":
            content = (obj.get("message") or {}).get("content") or []
            text = _collect_text(content)
            tools = _collect_tool_uses(content)
            out = {"role": "assistant"}
            if text:
                out["text"] = text
            if tools:
                out["tools"] = tools
            return out if (text or tools) else None
        if t == "result":
            return {"role": "result",
                    "subtype": obj.get("subtype"),
                    "is_error": obj.get("is_error"),
                    "duration_ms": obj.get("duration_ms"),
                    "usage": obj.get("usage")}
        return None

    # -- client registry / broadcast ------------------------------------------
    def add_client(self, client):
        with self.clients_lock:
            self.clients.add(client)
        # Replay recent output so a late joiner sees the current screen.
        with self.ring_lock:
            snapshot = bytes(self.ring)
        if snapshot:
            client.send_bytes(snapshot)
        client.send_json({"type": "hello",
                          "sessionId": self.session_id,
                          "workdir": WORKDIR,
                          "busy": self.busy,
                          "cols": COLS, "rows": ROWS})
        self._replay_history(client)

    def _replay_history(self, client, limit=150):
        """Send recent transcript events so a fresh client's structured view
        isn't empty — important now that mobile defaults to the transcript."""
        path = self.transcript_path or self._find_transcript()
        if not path:
            return
        try:
            lines = open(path).read().splitlines()
        except OSError:
            return
        events = [e for e in (self._slim_event(l) for l in lines) if e]
        for ev in events[-limit:]:
            client.send_json({"type": "transcript", "event": ev, "history": True})

    def remove_client(self, client):
        with self.clients_lock:
            self.clients.discard(client)

    def _broadcast_bytes(self, data: bytes):
        with self.clients_lock:
            targets = list(self.clients)
        for c in targets:
            c.send_bytes(data)

    def _broadcast_json(self, obj):
        with self.clients_lock:
            targets = list(self.clients)
        for c in targets:
            c.send_json(obj)

    def shutdown(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
        except Exception:
            pass


def _strip_noise(text):
    """Drop harness boilerplate that shouldn't show as a user message."""
    text = re.sub(r"<local-command-caveat>[\s\S]*?</local-command-caveat>", "", text)
    text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text)
    text = re.sub(r"</?command-(message|name|args)>", "", text)
    return text


def _collect_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "".join(parts).strip()


def _collect_tool_uses(content):
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            out.append({"name": b.get("name"), "input": b.get("input")})
    return out


def _collect_tool_results(content):
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            c = b.get("content")
            out.append(c if isinstance(c, str) else _collect_text(c))
    return out


SESSION = ClaudeSession()


# ── WebSocket framing (RFC 6455) — from clawd-web-claude/server.py ─────────────
def ws_send(wfile, lock, data, opcode=0x1):
    payload = data.encode("utf-8") if isinstance(data, str) else data
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    with lock:
        wfile.write(bytes(header) + payload)
        wfile.flush()


def ws_read_message(rfile):
    payload = b""
    msg_opcode = None
    while True:
        hdr = rfile.read(2)
        if len(hdr) < 2:
            return None
        b0, b1 = hdr[0], hdr[1]
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            ext = rfile.read(2)
            if len(ext) < 2:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = rfile.read(8)
            if len(ext) < 8:
                return None
            length = struct.unpack(">Q", ext)[0]
        mask = rfile.read(4) if masked else b""
        chunk = rfile.read(length) if length else b""
        if masked and chunk:
            chunk = bytes(chunk[i] ^ mask[i % 4] for i in range(len(chunk)))
        if opcode == 0x8:
            return ("close", chunk)
        if opcode == 0x9:
            return ("ping", chunk)
        if opcode == 0xA:
            return ("pong", chunk)
        if opcode != 0x0:
            msg_opcode = opcode
        payload += chunk
        if fin:
            return (msg_opcode or 0x1, payload)


class _Client:
    """A connected browser. Owns its send lock so broadcasts are thread-safe."""
    def __init__(self, wfile):
        self.wfile = wfile
        self.lock = threading.Lock()
        self.dead = False

    def send_bytes(self, data: bytes):
        if self.dead:
            return
        try:
            ws_send(self.wfile, self.lock, data, opcode=0x2)  # binary = PTY bytes
        except Exception:
            self.dead = True

    def send_json(self, obj):
        if self.dead:
            return
        try:
            ws_send(self.wfile, self.lock, json.dumps(obj), opcode=0x1)
        except Exception:
            self.dead = True


# ── HTTP + WS handler ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # quiet; the session logs what matters

    def _is_ws_upgrade(self):
        return (self.headers.get("Upgrade", "").lower() == "websocket"
                and "upgrade" in self.headers.get("Connection", "").lower())

    def _token_ok(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        return q.get("t", [""])[0] == TOKEN

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ws" and self._is_ws_upgrade():
            if not self._token_ok():
                return self.send_error(403, "bad token")
            return self.handle_ws()
        if path in ("/", "/index.html"):
            # page loads without a token; it just can't open the WS without one
            return self._serve_file(HERE / "index.html", "text/html; charset=utf-8")
        if path == "/config":
            # no token here, and no token returned — the page builds the phone
            # URL from the token already in its own address bar
            return self._serve_json({
                "sessionId": SESSION.session_id,
                "workdir": WORKDIR,
                "alive": SESSION.alive,
                "lanIp": lan_ip(),
                "port": PORT,
            })
        self.send_error(404, "not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/hook":
            return self._handle_hook()
        if path == "/upload":
            return self._handle_upload()
        self.send_error(404, "not found")

    def _handle_hook(self):
        if not self._token_ok():
            return self.send_error(403, "bad token")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n) if n else b""
            obj = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            obj = {}
        if obj:
            SESSION.on_hook(obj)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_upload(self):
        """Save a pasted/dropped image to the workdir and return its path so the
        browser can fold it into the next message (claude reads it via Read)."""
        if not self._token_ok():
            return self.send_error(403, "bad token")
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > MAX_UPLOAD:
            return self.send_error(413, "bad size")
        ctype = self.headers.get("Content-Type", "image/png").split(";")[0].strip()
        ext = EXT_BY_CTYPE.get(ctype, ".png")
        data = self.rfile.read(n)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = f"paste-{uuid.uuid4().hex[:8]}{ext}"
        dest = UPLOAD_DIR / name
        dest.write_bytes(data)
        print(f"[upload] {n} bytes -> {dest}", flush=True)
        self._serve_json({"path": str(dest), "name": name})

    def _serve_file(self, path, ctype):
        try:
            body = Path(path).read_bytes()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")   # always serve fresh UI
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_ws(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True

        client = _Client(self.wfile)
        SESSION.add_client(client)
        print("[ws] client connected", flush=True)
        try:
            while True:
                try:
                    msg = ws_read_message(self.rfile)
                except Exception:
                    break
                if msg is None:
                    break
                kind, data = msg
                if kind == "close":
                    break
                if kind == "ping":
                    try:
                        ws_send(self.wfile, client.lock, data, opcode=0xA)
                    except Exception:
                        break
                    continue
                if kind == "pong":
                    continue
                # data frame: control JSON from the browser
                try:
                    frame = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                self._dispatch(frame)
        finally:
            SESSION.remove_client(client)
            print("[ws] client disconnected", flush=True)

    def _dispatch(self, frame):
        t = frame.get("type")
        if t == "input":
            SESSION.write(frame.get("data", "").encode("utf-8"))
        elif t == "send":
            txt = frame.get("text", "")
            print(f"[ws] send: {txt[:60]!r}", flush=True)
            SESSION.send_message(txt)
        elif t == "resize":
            SESSION.resize(frame.get("cols"), frame.get("rows"))


class ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    SESSION.start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    ip = lan_ip()
    print(f"[http] clawd-console (token required)", flush=True)
    print(f"[http]   local : http://127.0.0.1:{PORT}/?t={TOKEN}", flush=True)
    print(f"[http]   phone : http://{ip}:{PORT}/?t={TOKEN}", flush=True)
    if BIND == "0.0.0.0":
        print("[http]   ⚠ reachable on your LAN with bypass-permissions — "
              "the token is the only thing gating command execution.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[http] shutting down", flush=True)
    finally:
        SESSION.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
