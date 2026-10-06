<div align="center">

<img src="https://github.com/Pylxyr/PyxeeBot-Page/blob/main/public/assets/logo.png" alt="PyxeeBot" width="120" />

# PyxeeBot

**A self-hosted Discord music bot built for music communities that care about getting the right track.**

Stream from YouTube · Last.fm curation · Live controls

[![Python](https://img.shields.io/badge/Python-3.11%2B-3572A5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![discord.py](https://img.shields.io/badge/discord.py-2.7.1-5865F2?style=flat-square&logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-2026.08.19-CC0000?style=flat-square&logo=youtube&logoColor=white)](https://github.com/yt-dlp/yt-dlp)
[![License](https://img.shields.io/badge/License-MIT-64748b?style=flat-square)](LICENSE)
[![Website](https://img.shields.io/badge/Website-PyxeeBot-FFAA40?style=flat-square)](https://pylxyr.github.io/PyxeeBot-Page/)

</div>

A self-hosted Discord music bot built on [discord.py](https://github.com/Rapptz/discord.py), yt-dlp and aiosqlite. It is designed to run comfortably on a single-core, 1 GB RAM VPS — tested on Oracle Cloud's Always Free AMD E2.1.Micro and Google Cloud's Always Free e2-micro, both on Ubuntu.

## Contents

- [Highlights](#highlights)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Automated VPS setup](#automated-vps-setup)
  - [Manual setup](#manual-setup)
  - [Running as a systemd service](#running-as-a-systemd-service)
- [Configuration](#configuration)
- [Commands](#commands)
- [Security notes](#security-notes)
- [Architecture notes](#architecture-notes)
- [Project structure](#project-structure)
- [Development](#development)
- [License](#license)

---

## Highlights

- **Playback** — YouTube and YouTube Music URLs, playlists, or plain-text queries. `!play` queues yt-dlp's top search result; `!search` lets you pick from up to 10 candidates.
- **Controls** — vote-skip, `!seek`, `!volume`, loop modes, `!prev`, shuffle/move/remove, and a live now-playing panel.
- **Last.fm curation** *(optional)* — `!vibe` finds similar tracks via `track.getSimilar` and lets you deselect before queuing; `!autoplay` queues a similar track whenever the queue runs dry; curated lists can be saved with `!vibe-save` / `!vibe-load`.
- **Persistence** — queue snapshots survive restarts; per-server prefix, DJ role, 24/7 mode, volume and autoplay live in SQLite; named server playlists and play-history stats (`!toptracks`, `!toprequestors`).
- **Low-resource by design** — bounded yt-dlp thread pool, 64 kbps Opus re-encode, debounced panel refreshes, per-guild isolation of extraction work, and a stream-URL cache with automatic refresh before a track ends.
- **Safe by default** — URLs that resolve to private or local addresses are refused, mentions in track titles can't ping roles or `@everyone`, and the systemd unit is sandboxed.

---

## Requirements

- Python 3.11+
- FFmpeg on `PATH`
- A Discord bot token, with the **Message Content** privileged intent enabled in the Developer Portal
- A JS runtime for yt-dlp (Deno) — installed automatically by the setup scripts
- Last.fm API key *(optional — only needed for `!vibe` and `!autoplay`)*

---

## Installation

### Automated VPS setup

On a fresh Ubuntu/Debian VPS, clone the repo and run the script for your host. It installs everything, walks you through creating a Discord token and (optionally) a Last.fm key with live validation, prints the bot's invite link, and starts the bot as a systemd service.

```bash
git clone https://github.com/Pylxyr/PyxeeBot.git ~/musicbot
cd ~/musicbot
```

| Host | Script | What it adds |
|---|---|---|
| Oracle Cloud | `bash deploy/setup_oracle.sh` | Reports whether you're on the AMD (E2.1.Micro) or ARM (Ampere A1) Always Free shape; notes Oracle's ~10 TB/month egress allowance |
| Google Cloud | `bash deploy/setup_gcp.sh` | Checks the VM's region against Always Free eligibility (`us-west1`/`us-central1`/`us-east1`); warns about the 1 GB/month egress cap and the network-tier and boot-disk gotchas that void the free tier |
| Anything else | `bash deploy/setup.sh` | Just the shared installer |

All three share one installer (`deploy/_common.sh`). `APP_DIR` is wherever you cloned the repo, so the folder name doesn't matter. On hosts with ≤2 GB RAM a 1 GB swap file is added, since a yt-dlp/ffmpeg burst can otherwise get an SSH session OOM-killed. Deno is installed from a pinned release and its SHA-256 is verified before it is placed in `/usr/local/bin`, and the generated `.env` is created owner-readable only (`chmod 600`).

### Manual setup

For local development or any platform the scripts don't cover.

```bash
git clone https://github.com/Pylxyr/PyxeeBot.git
cd PyxeeBot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp deploy/.env.example .env     # then edit .env and set DISCORD_TOKEN
python bot.py
```

Minimal `.env`:

```env
DISCORD_TOKEN=your_discord_bot_token
BOT_OWNERS=your_discord_user_id

# Optional
LASTFM_API_KEY=your_lastfm_api_key
DEFAULT_PREFIX=!
```

To invite the bot, use `https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&permissions=3230720&scope=bot%20applications.commands` — that permission set is View Channels, Send Messages, Embed Links, Read Message History, Connect and Speak, and nothing more.

### Running as a systemd service

> The VPS scripts already do this. These are the manual steps.

Create `/etc/systemd/system/musicbot.service` (adjust `User` and the paths to your install):

```ini
[Unit]
Description=Discord MusicBot
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/musicbot
Environment="PATH=/home/ubuntu/musicbot/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
Environment="PYTHONMALLOC=malloc"
Environment="MALLOC_TRIM_THRESHOLD_=65536"
EnvironmentFile=/home/ubuntu/musicbot/.env
ExecStart=/home/ubuntu/musicbot/.venv/bin/python bot.py
Nice=-10
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
SyslogIdentifier=musicbot
MemoryHigh=600M
MemoryMax=700M
OOMScoreAdjust=-500
LimitNOFILE=65536
ProtectSystem=full
PrivateTmp=yes
NoNewPrivileges=yes
ProtectHome=read-only
ReadWritePaths=/home/ubuntu/musicbot/data /home/ubuntu/musicbot/logs
CapabilityBoundingSet=
AmbientCapabilities=
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now musicbot
journalctl -u musicbot -f -o cat
```

`ReadWritePaths` limits writes to `data/` and `logs/`, so keep anything the bot writes — including the cookies file — under one of them.

**Why there is no `MemoryDenyWriteExecute=yes`:** yt-dlp needs a JS runtime (Deno) for YouTube, and Deno's V8 JIT requires writable+executable memory. Under that directive Deno panics on even a one-line script, which would break YouTube playback outright, so it is deliberately left out.

---

## Configuration

Settings are read from `.env`; every value except `DISCORD_TOKEN` has a default. `deploy/.env.example` is the annotated template.

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | required | Bot token |
| `LASTFM_API_KEY` | — | Enables `!vibe` curation and the per-server `!autoplay` toggle |
| `DEFAULT_PREFIX` | `!` | Global command prefix (per-server overrides via `!setprefix`) |
| `BOT_OWNERS` | — | Comma-separated owner user IDs (owner-only commands; app owner is always included) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_TO_FILE` | `true` | Write logs to `LOG_DIR` (rotated weekly by `deploy/musicbot-logrotate`, not in-process) |
| `LOG_DIR` | `logs` | Log file directory |
| `MAX_QUEUE_SIZE` | `100` | Maximum queue length per guild |
| `MAX_QUEUE_SIZE_PER_USER` | `0` | Per-user track limit; `0` disables the limit |
| `MAX_PLAYLIST_SIZE` | `25` | Maximum tracks loaded from a single playlist URL |
| `IDLE_TIMEOUT_SECONDS` | `180` | Disconnect after this many seconds idle (no tracks, no listeners) |
| `EMPTY_CHANNEL_TIMEOUT_SECONDS` | `60` | Disconnect after this many seconds alone in a voice channel |
| `YTDLP_CONCURRENT_EXTRACTS` | `1` | Global yt-dlp extraction concurrency limit |
| `YTDLP_PREFETCH_COUNT` | `1` | Tracks to pre-resolve ahead of the current position |
| `YTDLP_CURATION_CONCURRENCY` | `3` | Concurrent per-guild resolutions during `!vibe` / `!vibe-load` |
| `YTDLP_SEARCH_RESULTS` | `5` | Raw candidates fetched per search as a safety margin against malformed entries; the first valid one is used |
| `YTDLP_RESOLVE_CACHE_SIZE` | `128` | Maximum cached stream URL entries |
| `YTDLP_RESOLVE_CACHE_TTL_SECONDS` | `1800` | Stream URL cache TTL (30 min) |
| `YTDLP_EXTRACT_TIMEOUT_SECONDS` | `45` | Per-extraction timeout |
| `YTDLP_SOCKET_TIMEOUT` | `15` | yt-dlp socket timeout |
| `NEAR_END_PREFETCH_SECONDS` | `30` | Trigger stream URL refresh this many seconds before track end |
| `YTDLP_COOKIES_FILE` | — | Path to Netscape cookies file. Use `data/cookies.txt` — the systemd unit only makes `data/` and `logs/` writable, which `!refreshcookies` needs |
| `ALLOW_PRIVATE_URLS` | `false` | Allow URLs that resolve to private/loopback/link-local addresses (blocked by default to stop users pointing the bot at your internal network) |
| `YTDLP_JS_RUNTIME_PATH` | — | Path to a JS runtime binary, for sites requiring JS signature decryption. If unset, yt-dlp auto-detects a `deno` binary on `PATH` — the setup wizards install Deno system-wide for exactly this. Only set this to pin a different runtime/path (e.g. Node) instead |
| `OPUS_BITRATE_KBPS` | `64` | Opus encoding bitrate (64–256) |
| `NP_AUTO_REFRESH` | `false` | Auto-refresh the now-playing panel on a timer |
| `NP_AUTO_REFRESH_INTERVAL` | `30` | Auto-refresh interval in seconds |
| `ERROR_ANNOUNCE` | `true` | Post playback errors to the announce channel |
| `RESTORE_QUEUE_ON_RESTART` | `true` | Restore queue from snapshot after bot restart |
| `BOT_ACTIVITY_URL` | `pylxyr.github.io/PyxeeBot-Page/` | Text shown in the bot's Discord status ("Watching …") |

---

## Commands

Default prefix is `!` (change per server with `!setprefix`). "DJ" means the configured DJ role *or* the Manage Server permission.

### Playback

| Command | Aliases | Description |
|---|---|---|
| `!join` | `summon` | Join your voice channel |
| `!leave` | `disconnect` | Leave the voice channel |
| `!play <query>` | `p` | Queue a URL, playlist, or search query |
| `!playnext <query>` | `pn` | Queue a track immediately after the current one (DJ-only) |
| `!pause` | — | Pause playback |
| `!resume` | — | Resume playback |
| `!skip` | `next` | Vote-skip (instant if you're the requester or a DJ; requires ≥50% of listeners otherwise) |
| `!forceskip` | `fs` | Immediate skip, DJ-only |
| `!skipto <position>` | — | Jump to a queue position, dropping everything before it (DJ-only) |
| `!prev` | `previous`, `back` | Requeue the last-played track |
| `!stop` | — | Clear the queue and disconnect |
| `!loop` | — | Cycle loop mode: Off → Single track → Entire queue (DJ-only) |
| `!repeat` | `rp` | Toggle single-track loop on/off for the current track |
| `!replay` | — | Re-queue the current track to play immediately next (DJ-only) |
| `!seek <time>` | — | Jump to a position in the current track — `1:30`, `90`, or relative `+30`/`-15` (requester or DJ) |
| `!volume [0-200]` | `vol` | Show the current volume, or set it (DJ-only) |
| `!nowplaying` | `np` | Show the now-playing embed |

### Queue

| Command | Aliases | Description |
|---|---|---|
| `!queue` | `q` | Show the current queue |
| `!clear` | — | Clear the entire queue (DJ-only) |
| `!shuffle` | — | Shuffle the queue (DJ-only) |
| `!move <from> <to>` | — | Move a track to a different queue position (DJ-only) |
| `!remove <position>` | — | Remove a track (requester or DJ) |
| `!history` | — | Show recently played tracks (session only) |
| `!toptracks` | `top` | Show the all-time most-played tracks for this server |
| `!toprequestors` | `topreqs` | Show the all-time top track requestors for this server |

### Search

| Command | Aliases | Description |
|---|---|---|
| `!search <query>` | `find`, `s` | Browse up to 10 interactive results before queuing |

### Playlists

| Command | Aliases | Description |
|---|---|---|
| `!playlist save <name>` | — | Save the current queue as a named server playlist (overwriting an existing one needs its creator or a DJ; max 50 per server) |
| `!playlist load <name>` | — | Load a saved playlist into the queue |
| `!playlist list` | — | List saved playlists for this server |
| `!playlist show <name>` | — | Preview the tracks in a saved playlist |
| `!playlist delete <name>` | — | Delete a saved playlist |

### Curation

| Command | Aliases | Description |
|---|---|---|
| `!vibe <query>` | `vb` | Discover similar tracks via Last.fm and queue them interactively. Cooldown: 1 use / 15s per guild |
| `!vibe-save <name>` | `vsave` | Save the current vibe session's tracks as a named playlist |
| `!vibe-load <name>` | `vload` | Load and re-queue a saved vibe playlist |

### Admin & Settings

| Command | Aliases | Description |
|---|---|---|
| `!setprefix <prefix>` | — | Change the command prefix for this server (Manage Server) |
| `!setdj <role>` | — | Set the DJ role (Manage Server) |
| `!cleardj` | — | Remove the DJ role (Manage Server) |
| `!dj` | — | Show the current DJ role |
| `!stay` | — | Toggle 24/7 mode — bot stays connected when the queue empties (Manage Server) |
| `!autoplay` | — | Toggle per-server autoplay — queues a similar track when the queue empties (Manage Server) |
| `!stats` | — | Show bot process stats: versions, guild count, voice connections, RSS, latency (owner only) |
| `!refreshcookies` | — | Replace the yt-dlp cookies file by DM; the new file is live-tested and rolled back on failure (owner only) |
| `!ping` | — | Check gateway latency |
| `!commands` | `cmds` | Open the command help menu |

---

## Security notes

- **Private addresses are blocked.** Any `http(s)` URL — user-supplied, taken from a playlist, or returned by yt-dlp as a stream URL — must resolve only to public addresses, so users can't aim the bot at `localhost`, your LAN or cloud metadata endpoints. Set `ALLOW_PRIVATE_URLS=true` only if you need to play from a LAN stream server. Redirects followed inside yt-dlp/ffmpeg and DNS rebinding are not covered by this check.
- **Mentions are restricted.** The bot can mention users (for the opt-in requester tags) but never `@everyone`, `@here` or roles, so a track title can't ping a server.
- **Owner-only commands** (`!stats`, `!refreshcookies`) are limited to `BOT_OWNERS` and the application/team owners.
- **Cookies.** `!refreshcookies` takes a Netscape cookies file by DM, tests it with a live extraction, and rolls back (or removes the new file if there was none) when the test fails. `cookies.txt*` is git-ignored.
- **Secrets.** `.env` is git-ignored and created `chmod 600` by the setup scripts; the systemd unit runs with no capabilities and a read-only home.

---

## Architecture notes

**Player loop.** Each guild has one `GuildPlayer` with a long-running loop task. Creation is guarded by a per-guild lock so a simultaneous `!join` and `!play` can't create two players. The loop pre-resolves the next track's stream URL into a TTL cache (128 entries, 30 minutes by default), refreshes the current track's URL `NEAR_END_PREFETCH_SECONDS` before it ends, and re-resolves any URL older than 4 hours before playing it.

**Queue.** The queue is a plain deque with an explicit cap (`MAX_QUEUE_SIZE`) enforced when users add tracks. Internal re-queues (seek, volume change, `!prev`, resolve retry, loop modes) may exceed it by the one track they put back, so they never silently drop another queued track.

**Audio pipeline.** yt-dlp extracts a direct stream URL; FFmpeg reads it over HTTP and re-encodes to Opus at the configured bitrate. Copy mode is avoided on purpose: discord.py maps a detected `opus` codec to copy mode, which bypasses the encoder and causes pacing irregularities. The FFmpeg process is started immediately before `voice_client.play()`, after the voice connection has settled, to avoid pre-buffered audio causing a fast-forward at the start of a session. Volume is applied with an FFmpeg filter so playback stays on the low-CPU `FFmpegOpusAudio` path.

**yt-dlp concurrency.** Extractions run in a `ThreadPoolExecutor` sized from the two concurrency settings (minimum 2, maximum 16 workers). A global semaphore (`YTDLP_CONCURRENT_EXTRACTS`) gates playback-path work and a separate one (`YTDLP_CURATION_CONCURRENCY`) gates `!vibe`, so a large curation batch can't starve `!play`; per-guild semaphores isolate guilds from each other. A semaphore slot is held until the worker thread actually finishes — a timeout gives up waiting, but the slot stays taken so abandoned threads can't exceed the configured limit — and waiting for a slot is itself time-bounded. After 3 consecutive timeouts the pool is recycled.

**Database.** One shared `aiosqlite` connection in WAL mode; every write holds a process-wide lock because SQLite transactions are connection-scoped and a concurrent `commit()` could otherwise commit another guild's open transaction. Tables: `guild_settings` (the `prefix` column is nullable — `NULL` follows `DEFAULT_PREFIX`), `saved_playlists` + `saved_playlist_items`, `queue_snapshots`, `play_history` (capped at 5,000 rows per guild, trimmed every 50 inserts). The schema is versioned and migrated on startup.

**Search resolution.** A text query fetches `YTDLP_SEARCH_RESULTS` raw candidates in YouTube's relevance order and uses the first with a usable webpage URL — a guard against malformed entries, not a re-ranking. `!search` presents up to 10 candidates and lets the user choose.

**Owner resolution.** `setup_hook` calls `application_info()` to populate `owner_id` (personal app) or `owner_ids` (team app, admin/developer roles). If the API is unavailable the bot falls back to `BOT_OWNERS` rather than failing to start.

**Permissions.** DJ-gated actions accept either the DJ role or Manage Server. Vote-skip counts active human listeners in the voice channel, not guild members.

---

## Project structure

```
PyxeeBot/
├── bot.py                          # Entry point
├── requirements.txt
├── pyproject.toml                  # ruff, mypy and pytest config
├── .github/workflows/deploy.yml    # CI: lint → format → mypy → pytest → pip-audit → SSH deploy
├── deploy/
│   ├── _common.sh                  # Shared install engine (sourced by the setup scripts)
│   ├── setup_oracle.sh             # Setup wizard: Oracle Cloud
│   ├── setup_gcp.sh                # Setup wizard: Google Cloud
│   ├── setup.sh                    # Setup wizard: any other Ubuntu/Debian VPS
│   ├── musicbot.service            # Hardened systemd unit
│   ├── musicbot-logrotate          # logrotate config (weekly, copytruncate)
│   └── .env.example                # Annotated environment template
├── tests/                          # pytest suite (extraction, player queue, database)
└── musicbot/
    ├── bot.py                      # MusicBot subclass, help, startup, owner resolution, error handling
    ├── config.py                   # Settings dataclass and env loading
    ├── database.py                 # aiosqlite wrapper, schema + migrations
    └── cogs/
        ├── admin.py                # Prefix, DJ, stay, autoplay, stats, ping, cookies refresh
        ├── curation.py             # !vibe family, Last.fm client, autoplay trigger
        └── music/
            ├── cog.py              # MusicCog: composes the mixins, owns shared state
            ├── player.py           # GuildPlayer: queue, playback loop, history
            ├── models.py           # Track and related dataclasses
            ├── views.py            # Discord UI views (search, queue, now playing)
            ├── constants.py        # FFmpeg/yt-dlp options, limits
            ├── _base.py            # Shared attribute/method stubs for the mixins
            ├── _context.py         # Guild ContextVar and GuildContext type
            ├── _extraction.py      # yt-dlp wrapper, slot handling, audio sources
            ├── _urlsafety.py       # Public-address check for user-supplied URLs
            ├── _resolver.py        # Stream URL resolution and TTL cache
            ├── _lifecycle.py       # Player creation, snapshot restore
            ├── _panel.py           # Now-playing embed and debounced refresh
            ├── _events.py          # Voice-state and disconnect handlers
            ├── _helpers.py         # DJ checks, skip votes, owner checks
            ├── _playback_commands.py
            ├── _queue_commands.py
            ├── _search_commands.py
            └── _playlist_commands.py
```

---

## Development

```bash
pip install -r requirements.txt ruff mypy pytest pip-audit
ruff check musicbot/ bot.py tests/
ruff format musicbot/ bot.py tests/
mypy musicbot/ bot.py
pytest
```

CI runs the same checks on every push and pull request (formatting is auto-committed on pushes to `main`, and `--check`ed on pull requests); a push to `main` that passes is deployed over SSH, at the exact commit that was tested. `mypy` has one override for the command-decorator modules to silence a known discord.py ParamSpec false positive — see the comment in `pyproject.toml` before adding modules to it. Issues and pull requests are welcome.

---

## License

MIT — see [LICENSE](LICENSE).
