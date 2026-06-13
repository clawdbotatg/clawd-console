# clawd-console

A web **mirror + controller** for a real, **interactive** Claude Code session.
Drive a subscription-billed `claude` from a browser (desktop or phone) — read its
output cleanly, type/dictate/paste-images at it, and navigate its TUI menus —
without parsing the terminal's "weird text."

Pure Python stdlib server + a single HTML page. No build, no dependencies
(xterm.js and a QR lib load from a CDN).

## Why interactive (no `-p`)

The existing bridges (`clawd-tg-claude`, `clawd-web-claude`) drive `claude -p
--output-format stream-json`. Clean, but **as of 2026-06-15 `-p`/headless usage
draws from a separate metered Agent SDK credit pool** at full API rates. The
**interactive TUI** (`claude` with no `-p`) keeps drawing on your Max
subscription — so this runs the real interactive session and mirrors it.

The catch with interactive mode is the TUI emits spinner frames, ANSI colors, and
cursor moves. **We never parse that.** The channels are decoupled:

| | how | clean? |
|---|---|---|
| **write** | inject keystrokes into the PTY | n/a |
| **read (visual)** | stream raw PTY bytes → **xterm.js** renders them | yes — the emulator *interprets* the ANSI for us (same idea as `ttyd`/`gotty`) |
| **read (structured)** | tail the session **transcript JSONL** | yes — pure structured JSON, no ANSI |

Everything stays on the subscription because it's the same interactive `claude`
you'd run in a terminal.

## What you get

Two views, switched with the header button (one at a time):

- **Terminal** — a live, faithful xterm.js mirror of the session, token-by-token.
  Auto-scrolls to the bottom on new output. Defaults on desktop.
  - A **key bar** (`esc ↑ ↓ ← → ⏎ ⇥ ⌃C`) sends raw escape sequences straight to
    the PTY, so you can drive TUI menus (e.g. `/model`) from the web UI — even on
    touch, where the terminal itself is read-only.
- **Transcript** — the same conversation as clean, structured bubbles parsed from
  the session JSONL: `user`/`assistant` (markdown-rendered), tool calls,
  collapsible tool results, slash-command chips, and system notes. History is
  replayed on connect so it's never empty. Defaults on mobile (native scrolling,
  no xterm touch quirks).

Shared across both:

- **Message box** — type or dictate; Enter sends. Mobile dictation/paste/image
  handling is native because it's a real `<textarea>`.
- **Image paste/drop** — uploads to the workdir; the path is folded into your
  message and claude `Read`s it (vision works by file path).
- **Status pill** — `working…` / `idle · ready`, driven by Stop/tool hooks.
- **📱 Phone** — a QR to open the same UI on your phone over the LAN.

## Run

```bash
python3 server.py
# open the tokenized URL it prints, e.g. http://127.0.0.1:7900/?t=<token>
```

Requires the `claude` CLI authenticated with a Claude subscription (OAuth, not an
API key). Python 3, stdlib only.

A **token** gates the WebSocket and `/hook` — printed at startup, persisted in
`.clawd-console.token`, or set via `CONSOLE_TOKEN`. The server binds `0.0.0.0` so
it's reachable on your LAN; the token is the only thing gating command execution
and the session runs with bypass-permissions — **don't expose it beyond a trusted
network.**

Env knobs: `PORT` (7900), `BIND` (0.0.0.0), `WORKDIR` (cwd — where claude runs),
`CLAUDE_BIN`, `COLS`/`ROWS` (120×34), `SEND_SETTLE` (1.5), `CONSOLE_TOKEN`.

### Phone / LAN

Tap **📱 Phone** → a QR of `http://<lan-ip>:<port>/?t=<token>`. Scan it on a phone
on the same network for the same live session. (Both clients drive the one PTY and
share its terminal size — fine for a prototype.)

### Run as a daemon (survives closing the terminal)

```bash
./daemon.sh install [WORKDIR]      # launchd LaunchAgent: RunAtLoad + KeepAlive
./daemon.sh status | logs | restart | uninstall
```

The session id is pinned and saved to `.clawd-console.session`; on every (re)start
the server re-attaches with `--resume`, so the conversation survives crashes
(KeepAlive resurrects it), closing the terminal (it's detached), and reboot
(RunAtLoad). A turn that was mid-flight when killed is lost; context up to the last
saved step is restored.

## Architecture

```
Browser (index.html)                         server.py (stdlib)
┌───────────────────────────┐                ┌──────────────────────────────────┐
│ terminal view (xterm.js) ◄─┼── WS binary ───┤ PTY master ◄─► claude (no -p,     │
│ key bar ───────────────────┼── WS json ────►│              --session-id <uuid>) │
│ transcript view (bubbles)◄─┼── WS json ─────┤ tail <uuid>.jsonl → slim events   │
│ message box / image paste ─┼── WS / POST ──►│ /upload → file in workdir         │
└───────────────────────────┘                │ hooks → POST /hook → turn signal  │
                                              │ serve index.html, /config         │
                                              └──────────────────────────────────┘
```

One WebSocket carries everything: **binary** frames are raw PTY bytes (→ xterm),
**text** frames are JSON (control in, structured/hook events out).

### Files

- **`server.py`** — spawns one interactive `claude` in a PTY (`pty.openpty` +
  `subprocess.Popen` with `setsid`+`TIOCSCTTY`), bridges it to N browser clients
  over a hand-rolled WebSocket, tails the transcript JSONL into slim structured
  events, serves the page, handles `/hook` and `/upload`, and injects the hooks
  config via `claude --settings`.
- **`index.html`** — the whole UI: view switcher, xterm terminal, key bar,
  transcript renderer (safe markdown + command/system parsing), message box,
  image paste, QR.
- **`daemon.sh`** — launchd install/uninstall/status/logs/restart.
- **`smoke_test.py`** — headless WebSocket client; asserts both channels (PTY
  stream + structured events + on-disk transcript).

### Hooks → turn-boundary signal

The interactive transcript has no "turn done" marker, so the server injects a
hooks config at launch via `claude --settings <generated-file>` (self-contained;
never touches `~/.claude`). Each hook `curl`s its stdin JSON to `/hook`, which
updates state and broadcasts a `hook` WS event. Verified firing order on
v2.1.177: `SessionStart → UserPromptSubmit → PreToolUse → PostToolUse → Stop →
SessionEnd`. **Stop** carries `last_assistant_message` and drives the working→idle
pill; **UserPromptSubmit** carries the prompt; **Pre/PostToolUse** carry tool name
+ response.

## Two non-obvious things this figured out

Both were dead ends until found; both are baked into `server.py`:

1. **Scrub nested-claude env vars.** If the server runs from *inside* another
   Claude Code session, the child inherits `CLAUDECODE`,
   `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`, etc. and goes into an
   embedded mode that **doesn't write a normal transcript** (and changes input
   handling). `SCRUB_ENV` strips them — and `ANTHROPIC_API_KEY`, so it uses the
   subscription.
2. **Settle before Enter.** Claude's TUI treats a fast `text`+`\r` burst as a
   multi-line *paste*, so the `\r` becomes a newline, not a submit. A ~1.5s pause
   (`SEND_SETTLE`) makes the `\r` register as a discrete Enter. Sub-0.6s fails.

## Verify

```bash
python3 server.py &
python3 smoke_test.py     # sends a message; asserts user+assistant events + transcript file
```

Or open the page, send a message, and watch it in both views. Run `/status` in the
terminal to confirm it's on the subscription.

## Notes / gotchas

- **Stale-cache on `127.0.0.1`** — if a different app previously ran on this port,
  Chrome may serve its cached page. Hard-refresh once (Cmd+Shift+R) or use the LAN
  URL. clawd-console sends `Cache-Control: no-store` so it never goes stale itself.
- **Runtime/secret files are gitignored** — `.clawd-console.token`,
  `.clawd-console.session`, `.clawd-console.hooks.json` (embeds the token), and
  `.clawd-console-uploads/` (pasted images).

## Next ideas

A Telegram front-end on the same controller core; per-client terminal sizing
instead of one shared PTY size; vendoring the CDN assets for offline use; periodic
cleanup of `.clawd-console-uploads/`.
