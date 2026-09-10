# Twitch Radio Bot

A standalone Twitch chat bot: 24/7 in your chat, `!sr <query>` searches and
queues a song from **YouTube or SoundCloud** (nothing else — see
[Security](#security)), and the queue plays as a live MP3 stream you pull
into your own OBS as a Media Source — plus a Browser Source overlay
(thumbnail, progress bar, up-next). It does **not** stream to Twitch on its
own; it has no Twitch stream key at all. Think of it as a Discord music
bot's experience, adapted for the fact that Twitch has no equivalent of
"join a voice channel and play audio into it" — OBS has to pull the audio
in itself.

Originally part of a Discord music bot; split out into its own service so a
Twitch credential problem or a stuck ffmpeg process on one side can't take
the other down. No dependency on, or awareness of, any Discord bot.

## Requirements

- A Linux server — Ubuntu/Debian assumed by `deploy/setup.sh`; any box works —
  or an Android phone via Termux (`deploy/setup_termux.sh`; see below)
- Python 3.11+, `ffmpeg`
- A Twitch account for the bot to chat as (a dedicated account, made a
  moderator in your channel, is recommended over reusing your own), plus an
  app registered at [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)

## Installation

```bash
git clone https://github.com/Pylxyr/pyxee-radio-bot.git twitch-radio-bot
cd twitch-radio-bot
bash ./deploy/setup.sh
```

Installs system packages, [Deno](https://deno.com) (yt-dlp needs an external
JS runtime for full YouTube support), a virtualenv, and a systemd unit
(installed, not started). Then walks `.env` interactively: four required
credentials first (Enter skips one to fill in by hand later — the service
won't start until all four are set), then every other setting with its
default shown (Enter keeps it). Safe to re-run; already-filled values are
left alone. `SKIP_WIZARD=1` skips the whole thing for a scripted install.

## Termux (Android) setup

Runs directly on a phone — no VPS needed. Same service, same `.env`, same
commands; the differences below are all this script handles for you.

```bash
pkg install git
git clone https://github.com/Pylxyr/pyxee-radio-bot.git twitch-radio-bot
cd twitch-radio-bot
bash deploy/setup_termux.sh
```

Two real differences from the VPS install:

- **Node instead of Deno** for yt-dlp's JS runtime. Deno doesn't reliably
  run on Termux at all — it links against glibc, not Android's own Bionic
  libc, and Termux's own package build for it has a long, still-unresolved
  history of being added-then-disabled for exactly this reason (see
  [termux-packages#17398](https://github.com/termux/termux-packages/issues/17398)
  and linked issues). Termux's `nodejs` package is mature and built
  natively for Bionic — [nodejs.org's own install
  docs](https://nodejs.org/en/download/package-manager/all) point Android/
  Termux users at it directly. `config.py` already supports
  `YTDLP_JS_RUNTIME_NAME` for exactly this pairing — the Termux script just
  points it at Node instead of Deno; nothing in the Python code needed to
  change.
- **No systemd.** The script offers `termux-services` (a real supervisor —
  restarts the bot on crash) with a detached `tmux` session as the
  fallback, instead of the systemd unit the VPS path installs.

Everything else — the Python dependency install, `ffmpeg`, the `.env`
wizard — is the same story as `setup.sh`, just without the systemd/apt/sudo
assumptions baked in. One thing that genuinely doesn't translate from a
VPS: OBS doesn't run on Android, so the phone is always the "OBS is on a
different machine" case — the script skips asking and configures for that
directly (see [Running the bot on a separate machine from
OBS](#running-the-bot-on-a-separate-machine-from-obs) below for what that
means in practice on a phone specifically, which is different from the
cloud-VM version of that same section).

One thing that's actually *simpler* on Termux: the one-time OAuth step
below normally needs an SSH tunnel to reach `localhost:4343` from your own
browser — on a phone, the bot and the browser are the same device, so
there's no tunnel to set up; just open the two URLs directly in a browser
app on the phone.

## Configuration and one-time Twitch authorization

1. **Fill in `.env`** — done by `setup.sh`'s wizard, unless you skipped it.
   By hand (also in `.env.example`):
   - `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` — register an app at
     [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps).
     Category "Chat Bot", OAuth Redirect URL exactly
     `http://localhost:4343/oauth/callback`, Client Type "Confidential".
   - `TWITCH_BOT_ID` / `TWITCH_OWNER_ID` — numeric Twitch user IDs, **digits
     only** (not "Twitch ID:1536026185" — just "1536026185"; validated at
     startup). Bot account and your own broadcaster account. Look one up at
     [streamweasels.com's converter](https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/).

2. **Start the service:**
   ```bash
   sudo systemctl enable --now twitch-radio
   ```
   Chat won't be read yet — the bot has no way to authenticate until step 3.

3. **Authorize both accounts.** A local web server on port 4343 receives the
   OAuth grant; on a remote server, tunnel it first:
   ```bash
   ssh -L 4343:localhost:4343 <user>@<host>
   ```
   (On Termux: skip this — the bot and your browser are the same phone, so
   `localhost:4343` is already reachable with no tunnel. See [Termux
   (Android) setup](#termux-android-setup).)

   Then, in a browser:
   - As the **bot account**:
     `http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true`
   - As the **broadcaster account**:
     `http://localhost:4343/oauth?scopes=channel:bot&force_verify=true`
     (Optional if the bot is already a mod in your channel, but doing it
     anyway removes the "is it still a mod" dependency.)

   **Use two separate browser sessions** (e.g. a normal window + a private/
   incognito one) — reusing the same logged-in session for both authorizes
   the SAME account twice with no error, and chat just silently doesn't
   work afterward. The bot checks for exactly this at startup and logs
   which account, if either, is missing a token — check `journalctl -u
   twitch-radio -f -o cat` right after starting if `!sr` doesn't respond.

   Tokens save to `data/twitch_tokens.json`, reloaded on every future start.

4. Watch it come up: `journalctl -u twitch-radio -f -o cat`

## Adding it to OBS

Two separate sources, both pointed at the local HTTP surface below:

- **Media Source** → `http://<host>:<port>/stream.mp3` — the actual audio.
  Uncheck "Local File".
- **Browser Source** (optional) → `http://<host>:<port>/overlay` — visual
  only (thumbnail, progress bar, next 2 songs), no audio. Size it to taste;
  the page background is transparent. Open the URL in a regular browser
  tab first if you want to preview it before adding it to OBS.

If this service runs on the **same machine** as OBS, `<host>` is
`localhost` and nothing else is needed. If it runs on a **different
machine** (a cloud VM, as below), keep reading.

By default the queue keeps advancing on its own real-time clock whether or
not OBS is actually connected — if OBS crashes or a scene reloads,
whatever played in the meantime is just gone when it reconnects. Set
`PAUSE_QUEUE_WHEN_NO_LISTENERS=true` in `.env` to hold off starting a new
track until something's listening again (a track already playing when the
last listener disconnects still finishes normally — this only affects
track boundaries).

## Running the bot on a separate machine from OBS

Common case: a cloud VM running the bot, OBS on your own PC. `setup.sh`'s
wizard asks about this directly (host, port, a settings password, your
public IP, and the exact firewall commands to run) — this section is the
manual/reference version of the same steps, for editing `.env` by hand or
if the wizard's auto-detected IP didn't work out.

**On Termux, this section doesn't apply the same way.** There's no cloud
firewall or public IP involved — `setup_termux.sh` detects the phone's
*local network* IP instead, and the realistic setup is OBS on a computer
on the same Wi-Fi as the phone (`http://<phone's-local-IP>:8098/stream.mp3`).
Reaching it from outside that Wi-Fi network (cellular data instead of
Wi-Fi, or an OBS box elsewhere on the internet) isn't the same problem as
opening a cloud firewall port — most mobile carriers block inbound
connections outright (CGNAT), so the steps below generally won't apply; a
tunneling tool (e.g. Tailscale, or Cloudflare Tunnel) is the realistic
option if you need that.

1. In `.env`, set `TWITCH_NOWPLAYING_HOST=0.0.0.0` and set
   `TWITCH_SETTINGS_PASSWORD` to something (a startup warning fires if you
   leave it unset with a non-localhost host — `/settings` changes your
   queue/cooldown limits, and this makes it internet-reachable).
2. Open `TWITCH_NOWPLAYING_PORT` (default 8098) in **two** places — missing
   either one still blocks the connection:
   - **Your cloud provider's firewall/security rule** — ingress, TCP, that
     port, source `0.0.0.0/0`. On Oracle Cloud: the VCN's Default Security
     List (or the NSG attached to the instance).
   - **The VM's own OS firewall** — commonly blocks it too, even after the
     rule above. On Oracle's Ubuntu images specifically: `ufw` is disabled
     by default and won't help; edit `/etc/iptables/rules.v4` directly,
     copying the existing line that allows SSH (port 22) and changing the
     port:
     ```bash
     sudo cp /etc/iptables/rules.v4 /etc/iptables/rules.v4.bak
     sudo sed -i '/--dport 22 -j ACCEPT/a -A INPUT -p tcp -m state --state NEW -m tcp --dport 8098 -j ACCEPT' /etc/iptables/rules.v4
     sudo iptables-restore < /etc/iptables/rules.v4
     sudo netfilter-persistent save
     ```
     Double-check the SSH rule is still there before disconnecting — a
     mistake here can lock you out. `sudo iptables -L INPUT -n --line-numbers`
     to inspect the live rules.
3. Find your public IP if you need it: `curl ifconfig.me`, or your cloud
   console.
4. Restart the service, then point OBS at
   `http://<VM's public IP>:8098/stream.mp3` and `.../overlay`.

This surface has no TLS. Fine for audio/overlay; if you'd rather not send
the `/settings` Basic Auth password in cleartext over the open internet,
put a reverse proxy (e.g. Caddy, which gets you free automatic HTTPS in one
line) in front instead of exposing the port directly.

## Commands (in Twitch chat)

| Command | Who | Does |
|---|---|---|
| `!sr <query>` / `!songrequest <query>` | anyone | Resolves a YouTube/SoundCloud search or link and queues it |
| `!skip` | moderators (broadcaster included), or anyone skipping their *own* currently-playing (or still-loading) song | Skips the currently playing track |
| `!voteskip` / `!vs` | anyone | Adds a vote to skip the current track; skips once `vote_skip_threshold` unique voters have voted |
| `!remove` / `!cancel` / `!unqueue` | anyone | Pulls your own most-recently-queued (not-yet-playing) request back out |
| `!position` / `!pos` | anyone | Shows where your request(s) sit in the queue |
| `!queue` | anyone | Shows how many requests are queued |
| `!nowplaying` / `!np` | anyone | Shows the current track and who requested it |
| `!commands` / `!help` | anyone | Lists the commands above |
| `!setlimit <key> <value>` | moderators | Adjusts one request-limit tunable live from chat — same keys/ranges as `/settings` below |
| `!block <url or uploader>` | moderators | Blocks a specific track (by link) or every track from an uploader (by name) from being requested again |
| `!unblock <url or uploader>` | moderators | Reverses `!block` |
| `!blocklist` | moderators | Shows how many tracks/uploaders are currently blocked |

Request limits (max pending per chatter, cooldown, queue cap, max track
length, vote-skip threshold) are live-adjustable from `/settings` without a
restart, or from chat via `!setlimit` (moderators only) — e.g. `!setlimit
queue_cap 100`. Valid keys: `max_pending_per_chatter`,
`request_cooldown_seconds`, `queue_cap`, `max_request_duration_seconds`,
`vote_skip_threshold`.

## The local HTTP surface

Binds to `127.0.0.1` by default (`TWITCH_NOWPLAYING_HOST`) — see above for
opening it up to a separate OBS machine.

- `GET /stream.mp3` — the live audio feed. Always public.
- `GET /overlay` — the visual now-playing/up-next widget. Always public.
- `GET /nowplaying.json` — the same data as JSON, for a custom overlay.
- `GET /ws/nowplaying` — WebSocket version of the above; the built-in
  overlay uses this and pushes on every change, falling back to polling
  `/nowplaying.json` if the connection is unavailable.
- `GET/POST /settings` — the tunables page. Gated by HTTP Basic Auth if
  `TWITCH_SETTINGS_PASSWORD` is set (any username, that password); see
  [Security](#security) for the protections around this endpoint.

## Security

A few things worth knowing about the trust model:

- **Only YouTube and SoundCloud are accepted** — `!sr` rejects any other
  direct link outright, and yt-dlp itself is additionally restricted to
  those two extractors as a second layer. Not configurable: less "what
  sites does this support" and more "every extractor is attack surface",
  since arbitrary sites can serve crafted metadata into the overlay page
  and chat replies.
- **`/settings` is protected against CSRF** — a cross-site POST with a
  mismatched `Origin`/`Referer` is rejected with 403, so a malicious page
  can't submit settings changes using an admin's cached Basic Auth
  credentials.
- **`/settings` has brute-force lockout** — 10 failed password attempts
  from the same address within 5 minutes get a 429 (in-memory only, resets
  on restart).
- **The OAuth token file (`data/twitch_tokens.json`) is `chmod 600`**
  after every save — the same protection `setup.sh` already gives `.env`.
- **`.env` and `data/` are gitignored** and the systemd unit's sandboxing
  only allows writes under `data/`.

None of this adds up to a hardened public-internet service — the intent is
still "one streamer's own bot, reachable by the people who need it", not
"safe to expose to strangers with no other precautions." Put a reverse
proxy in front (see above) if you're exposing this beyond your own network.

## Project structure

```
twitch-radio-bot/
├── bot.py                       # entry point
├── requirements.txt
├── pyproject.toml                # ruff/mypy config
├── deploy/
│   ├── .env.example
│   ├── setup.sh                  # installer (Ubuntu/Debian VPS)
│   ├── setup_termux.sh           # installer (Termux/Android — standalone, no systemd)
│   ├── twitch-radio.service      # systemd unit
│   └── twitch-radio-logrotate
└── twitch_radio/
    ├── config.py                 # Settings dataclass, env var loading
    ├── models.py                 # Track dataclass
    ├── extraction.py             # yt-dlp resolver + short-lived cache (YouTube/SoundCloud only)
    ├── store.py                  # atomic JSON persistence
    ├── tunables.py                # TwitchTunables dataclass
    ├── blocklist.py               # moderation blocklist normalization/lookup
    ├── player.py                  # RadioPlayer: MP3 encoder + subscriber fan-out, gapless queue
    ├── chatbot.py                 # TwitchChatBot + SongRequestComponent
    ├── admin_server.py            # aiohttp: /stream.mp3, /overlay, /nowplaying.json, /ws/nowplaying, /settings
    └── bot.py                     # wires everything together, owns shutdown, --check-config
```

## A note on `MemoryDenyWriteExecute` and `SystemCallFilter`

Both absent from `deploy/twitch-radio.service`'s hardening, even though
they're normally reasonable defaults. yt-dlp needs a working V8 JS runtime
(Deno by default; Node also supported) for full YouTube support, and JIT
compilation is fundamentally incompatible with what both directives
restrict:

- `MemoryDenyWriteExecute=yes` — tested directly: Deno panics on ENOMEM on
  the very first script it runs under it.
- `SystemCallFilter=@system-service` — confirmed directly under this exact
  unit: Deno's JS-challenge solver died with **SIGSYS (-31)** under it,
  surfacing as an unrelated-looking yt-dlp error (`Requested format is not
  available`). Worked outside systemd, and under `systemd-run` without this
  directive — isolating it as the cause.

Both would break song requests outright, so both stay off; every other
hardening directive stays in place. Prefer keeping `SystemCallFilter` and
switching to Node instead? It needs **Node ≥22**
([yt-dlp-ejs's stated minimum](https://github.com/7tikar/ejs)) — Ubuntu's
own `apt install nodejs` is almost always older than that; use
[NodeSource's setup script](https://github.com/nodesource/distributions) or
`nvm`.

## A note on `YTDLP_COOKIES_FILE` — and YouTube blocking cloud IPs

**On a residential/home connection:** leave it unset. Ordinary public
YouTube searches/URLs don't need cookies, and yt-dlp has a known, recurring
failure — `The page needs to be reloaded.` — that shows up specifically on
cookie-authenticated requests (see
[yt-dlp#16212](https://github.com/yt-dlp/yt-dlp/issues/16212),
[yt-dlp#17389](https://github.com/yt-dlp/yt-dlp/issues/17389)), so turning
this on "just in case" can make things worse.

**On a cloud VM (Oracle, AWS, GCP, DigitalOcean, ...): this is different.**
YouTube actively blocks known datacenter IP ranges more aggressively than
residential ones, and anonymous (no-cookie) requests from a flagged IP get
`Sign in to confirm you're not a bot` even for a completely ordinary
search. Two ways to deal with it, roughly simplest-first:

1. **Cookies from a real logged-in browser session.** Export a real
   `cookies.txt` (private/incognito window, log into YouTube, export with a
   browser extension, close the window), point `YTDLP_COOKIES_FILE` at it
   under `data/` (e.g. `data/cookies.txt` — see below for why it must be
   there). Simple, zero new processes, but the export needs periodic
   refreshing as the session ages, and an empty/placeholder file makes
   things *worse*, not better.
2. **A local PO-token provider** — a small companion process that proves
   requests are legitimate without needing a browser session at all, so
   nothing to ever refresh. [bgutil-ytdlp-pot-provider-rs](https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs)
   is a single pre-built binary (no Node/Docker/build step), documented at
   <50MB RAM in practice — realistic even on a free-tier VM:
   ```bash
   wget https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-x86_64
   chmod +x bgutil-pot-linux-x86_64
   sudo mv bgutil-pot-linux-x86_64 /usr/local/bin/bgutil-pot
   # then run it persistently, e.g. as its own tiny systemd unit:
   #   ExecStart=/usr/local/bin/bgutil-pot server --host 127.0.0.1 --port 4416
   ```
   Then install its yt-dlp plugin (a release zip extracted into a yt-dlp
   plugin directory — see that repo's README for the current exact steps,
   since plugin packaging details do shift between releases) and set
   `YTDLP_POT_PROVIDER_URL=http://127.0.0.1:4416` in `.env` — this service
   passes it straight through as yt-dlp's `youtubepot-bgutilhttp:base_url`
   extractor-arg. Not set up by `setup.sh` (it's a separate process this
   service doesn't own or supervise), and — its own maintainers' words, not
   just ours — **not a guaranteed fix**: "providing a POT token does not
   guarantee bypassing 403 errors or bot checks, but it may help your
   traffic seem more legitimate."

Either way: if `YTDLP_COOKIES_FILE` is set, it must be a path under `data/`
(e.g. `YTDLP_COOKIES_FILE=data/cookies.txt`) — the only directory this
service's systemd sandbox can write to; yt-dlp saves this file back on
every `!sr`. Checked at startup with a clear error if misconfigured.

Cookies also change which YouTube client yt-dlp presents as — one default
candidate (`tv_downgraded`) is the thing behind issue #17389 above, and a
mobile client (android/ios/etc.) combined with cookies fails outright,
since those clients reject cookie auth entirely. `YTDLP_PLAYER_CLIENT`
defaults to a cookie-compatible pair whenever cookies are configured, and
this service validates it at startup either way — an incompatible
combination refuses to start with a specific error instead of silently
failing every `!sr`. Check [the EJS wiki](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
if requests start failing again after a yt-dlp update — YouTube changes
what works here often.

## Performance: why the first request after a restart feels slower

Every extraction is a real network round trip plus, for YouTube, a JS
challenge — roughly 15-20s cold. A `!sr` triggers this twice by design:
once in chat to confirm/queue it, again right before it actually plays
(stream URLs expire, and content can change state in between).
`YTDLP_CACHE_TTL_SECONDS` (default 300) makes the second nearly free for
anything near the front of the queue, while still forcing a real re-resolve
for anything sitting in a longer queue. `0` disables caching.

## Manual installation (without `setup.sh`)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp deploy/.env.example .env   # then edit it
mkdir -p data logs
```

Install Deno (`curl -fsSL https://deno.land/install.sh | sh`), make sure
it's on `PATH`, then either run `python bot.py` directly or adapt
`deploy/twitch-radio.service` for your own paths/user.

Before a real deploy (or in CI), `python bot.py --check-config` validates
`.env` and confirms `ffmpeg` is on `PATH` without starting the bot,
spawning ffmpeg, or touching Twitch/yt-dlp.
