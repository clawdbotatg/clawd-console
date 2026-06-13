# clawd-console

A web terminal **mirror + controller** for a real, **interactive** Claude Code
session — so you can read and write to it cleanly from outside (a web UI today;
Telegram or a controller agent next), while staying on the **Claude
subscription** instead of metered `-p` usage.

![two-panel UI: live terminal mirror on the left, structured transcript on the right]

## Why interactive (no `-p`)

The existing bridges (`clawd-tg-claude`, `clawd-web-claude`) drive `claude -p
--output-format stream-json`. That's clean, but **on 2026-06-15 `-p`/headless
usage moves to a separate metered Agent SDK credit pool** at full API rates. The
**interactive TUI** (`claude` with no `-p`) keeps drawing on your Max
subscription.

The catch with interactive mode is that the TUI emits "weird text" — spinner
frames, ANSI colors, cursor moves. **We never parse that.** Instead we decouple
the channels:

| | how | clean? | billing |
|---|---|---|---|
| **write** | inject keystrokes into the PTY | n/a | subscription |
| **read (visual)** | stream raw PTY bytes → **xterm.js** renders them | yes — the emulator *interprets* the ANSI for us | subscription |
| **read (structured)** | tail the session **transcript JSONL** | yes — pure structured JSON, no ANSI | subscription |

xterm.js is doing what `ttyd`/`gotty`/`wetty` do: render a real terminal in the
browser. So the left pane *is* the terminal, live and token-by-token. The right
pane is the same conversation as clean `user`/`assistant`/tool events a
controller can act on.

## Run

```bash
python3 server.py
# open the tokenized URL it prints, e.g. http://127.0.0.1:7900/?t=<token>
```

Pure Python stdlib — no dependencies, no install. xterm.js + a QR lib load from a
CDN. A **token** is required on the WebSocket (and `/hook`) — printed at startup,
persisted in `.clawd-console.token`, or set `CONSOLE_TOKEN`. The server binds
`0.0.0.0` by default so it's reachable on your LAN; the token is the only thing
gating command execution, and the session runs bypass-permissions — so **don't
expose this beyond a trusted network.**

Env knobs: `PORT` (7900), `BIND` (0.0.0.0), `WORKDIR` (cwd — where claude runs),
`CLAUDE_BIN`, `COLS`/`ROWS` (120×34), `SEND_SETTLE` (1.5), `CONSOLE_TOKEN`.

### Phone / LAN

Click **📱 Phone** in the header → a QR of `http://<lan-ip>:<port>/?t=<token>`.
Scan it on a phone on the same network and you get the same live interface. (Both
clients drive the one PTY; for a TUI the terminal size is shared, so phone +
desktop will share a fixed width — fine for a prototype.)

### Run as a daemon (survives closing the terminal)

```bash
./daemon.sh install [WORKDIR]   # launchd LaunchAgent: RunAtLoad + KeepAlive
./daemon.sh status | logs | restart | uninstall
```

The server pins `--session-id` and saves it to `.clawd-console.session`; on every
(re)start it re-attaches to that session with `--resume`, so the conversation
survives crashes (KeepAlive resurrects it), closing the terminal (it's detached),
and reboot (RunAtLoad). Verified: kill the server → launchd restarts it → same
session, context intact. (A turn that was mid-flight when killed is lost; context
up to the last saved step is restored.)

## Architecture

```
Browser (index.html)                      server.py (stdlib)
┌──────────────────────────┐              ┌──────────────────────────────────┐
│ xterm.js  ◄──────────────┼─ WS binary ──┤ PTY master ◄─► claude (no -p,     │
│  (live mirror)           │              │              --session-id <uuid>) │
│ input box + Send ────────┼─ WS json ───►│  → keystrokes / text+CR           │
│ structured panel ◄───────┼─ WS json ────┤ tail <uuid>.jsonl → slim events   │
└──────────────────────────┘              │ + serve index.html, /config       │
                                          └──────────────────────────────────┘
```

- **`server.py`** — spawns one interactive `claude` in a PTY (`pty.openpty` +
  `subprocess.Popen` with `setsid`+`TIOCSCTTY`), bridges it to N browser clients
  over a single hand-rolled WebSocket (binary frames = PTY bytes, text frames =
  JSON control/events), and tails the transcript JSONL for structured events.
- **`index.html`** — xterm.js terminal bound to the PTY stream, a message box
  that sends a high-level "type + Enter", and a structured-event side panel.
- **`smoke_test.py`** — headless WebSocket client that sends a message and
  asserts both channels (PTY stream + structured events + on-disk transcript).

## Two non-obvious things this prototype figured out

Both were dead ends until found, and both are baked into `server.py`:

1. **Scrub the nested-claude env vars.** If the server is launched from *inside*
   another Claude Code session, the child inherits `CLAUDECODE`,
   `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`, etc. and runs in an
   embedded mode that **doesn't write a normal session transcript** (and changes
   input handling). `SCRUB_ENV` strips them so the child is a pristine top-level
   session. (We also drop `ANTHROPIC_API_KEY` so it uses subscription OAuth.)
2. **Settle before Enter.** Claude's TUI treats a fast `text`+`\r` burst as a
   multi-line **paste** — the `\r` becomes a newline, not a submit. A ~1.5s pause
   (`SEND_SETTLE`) between typing and the carriage return makes the `\r` register
   as a discrete Enter. Sub-0.6s reliably fails.

## Verify

```bash
python3 server.py &
python3 smoke_test.py        # asserts user+assistant events and a transcript file
```

Or open the page, type a message, and watch it appear in both panes. Run
`/status` in the mirror to confirm it's drawing on the subscription. The session
lives server-side, so refreshing the browser re-attaches to the running session
(the replay buffer repaints the current screen).

## Hooks → turn-boundary signal

The interactive transcript has no "turn done" marker, so the server injects a
hooks config at launch via `claude --settings <generated-file>` (self-contained;
never touches `~/.claude`). Each hook `curl`s its stdin JSON to the server's
`/hook` endpoint, which updates state and broadcasts a `hook` WS event. Verified
firing order in v2.1.177:

```
SessionStart → UserPromptSubmit → PreToolUse → PostToolUse → Stop → SessionEnd
```

Most useful for a controller:
- **Stop** — turn complete; payload has `stop_hook_active` (loop guard) and
  `last_assistant_message` (the final reply). Drives the working→idle state.
- **UserPromptSubmit** — turn started; includes the `prompt` text.
- **Pre/PostToolUse** — tool activity; `tool_name`, `tool_input`, `tool_response`.
- **SessionStart/SessionEnd** — lifecycle (`source`, `reason`, `model`) for
  respawn detection.

The UI shows a working/idle status pill driven by these.

## Image paste

Paste or drop an image into the message box → it uploads to `/upload` and lands
in `.clawd-console-uploads/` in the workdir; on send, its absolute path is folded
into your message. Interactive claude `Read`s the path and sees the image (vision
works by file path — verified: a crimson PNG came back as "Red"). Pasting from a
phone works too (mobile keyboards expose image paste / the share sheet).

## Status / next

Working & verified: PTY mirror, send box, structured channel, collapsible
results, hooks turn-signal, token auth, LAN bind + QR-to-phone, session resume
across restarts, the launchd daemon (survives crash/terminal-close/reboot), and
image paste.

Next ideas: a Telegram front-end on the same controller core; per-client terminal
sizing instead of one shared PTY size; vendoring the CDN assets for offline use;
periodic cleanup of `.clawd-console-uploads/`.
