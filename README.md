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

A self-hosted Discord music bot built with [discord.py](https://github.com/Rapptz/discord.py), yt-dlp, and aiosqlite. Designed to run well on a single-core, 1 GB RAM VPS (tested on both Oracle Cloud's Always Free AMD E2.1.Micro and Google Cloud's Always Free e2-micro, running Ubuntu).

## Contents

- [Overview](#overview)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Automated VPS setup](#automated-vps-setup)
  - [Local setup](#local-setup)
- [Running as a systemd service](#running-as-a-systemd-service)
- [Configuration](#configuration)
- [Commands](#commands)
- [Project Structure](#project-structure)
- [Architecture Notes](#architecture-notes)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

- Plays audio from YouTube and YouTube Music
- `!play`/`!playnext` queue yt-dlp's own top search result directly; `!search` shows a list of candidates to pick from manually when the top hit isn't the one you want
- Last.fm integration for `!vibe` similar-track curation and per-server `!autoplay`
- Persistent queue snapshots survive restarts; per-server DJ role, prefix, 24/7 mode, and autoplay settings stored in SQLite
- Designed around the constraints of a 1/8-core shared VPS: single-threaded yt-dlp pool, 64 kbps Opus encoding, debounced panel refreshes, bounded deque-based queue

---

## Features

### Playback

- `!play` accepts YouTube/YouTube Music URLs, playlist URLs, or plain text search queries — for a text query, the first yt-dlp search result is queued directly
- `!playnext` queues a track immediately after the current one
- `!search` shows up to 10 interactive results before committing — use this when `!play` picks the wrong track
- Vote-skip (`!skip`): instant if you're the requester or a DJ; otherwise requires ≥50% of listeners to call it
- `!forceskip` — immediate skip, DJ-only
- `!skipto <position>` — jump to a queue position, dropping everything before it (DJ-only)
- `!prev` — requeue the last-played track
- `!pause` / `!resume`
- `!stop` — clears the queue and disconnects
- `!loop` — cycles through Off → Single track → Entire queue
- `!repeat` / `!replay` — aliases for one-track loop
- `!nowplaying` — live now-playing embed with queue preview

### Vibe Curation (Last.fm)

`!vibe <query>` discovers similar tracks via Last.fm's `track.getSimilar` API. Results are sorted by match confidence (0.0–1.0). A curation panel lets you deselect tracks before queuing. When the queue drops to ≤10 tracks during an active vibe session, a refill prompt surfaces automatically offering more similar tracks.

Curation resolutions for a single guild run up to `YTDLP_CURATION_CONCURRENCY` at a time (own per-guild semaphore, separate from the playback path). Curation also has its own dedicated global semaphore, sized by the same `YTDLP_CURATION_CONCURRENCY` value — a large `!vibe` batch resolving in the background can no longer starve `!play`/`!playnext`/`!search` of the single global playback slot (`YTDLP_CONCURRENT_EXTRACTS`).

Each similar track from Last.fm is resolved to YouTube by taking yt-dlp's top search result for `<artist> - <title>` — no re-ranking.

Save and reload named curated playlists with `!vibe-save` / `!vibe-load`.

If autoplay is enabled for the server (`!autoplay`), the bot queues one similar track (via the same Last.fm pipeline) whenever the queue fully empties, using the last completed track as the seed — no `!vibe` required.

### URL Pipeline

- YouTube watch URLs, short URLs (`youtu.be`), and playlist URLs all resolve correctly
- Playlist URLs respect `MAX_PLAYLIST_SIZE` (default 25)
- yt-dlp selects `bestaudio[ext=webm]` → `bestaudio[ext=m4a]` → `bestaudio` → `best[height<=480]`
- Stream URLs are cached per-track (128 entries, 30-minute TTL by default) and refreshed automatically 30s before the track ends
- Audio re-encodes through libopus at 64 kbps by default — copy mode is intentionally avoided to prevent pacing irregularities

### Performance

- yt-dlp runs in a `ThreadPoolExecutor(max_workers=2)` to avoid blocking the event loop
- A global semaphore (`YTDLP_CONCURRENT_EXTRACTS`, default 1) limits concurrent playback-path extractions on the constrained vCPU
- Curation (`!vibe`) resolves through its own separate global semaphore (sized by `YTDLP_CURATION_CONCURRENCY`), so a large curated playlist resolving in the background can't block ordinary playback commands
- Per-guild playback semaphore (`Semaphore(1)`) isolates guilds from each other
- Curation resolutions use a separate per-guild semaphore sized by `YTDLP_CURATION_CONCURRENCY`
- Thread pool automatically recycles after 3 consecutive extraction timeouts
- Bounded yt-dlp socket timeout (`socket_timeout: 15`) prevents stalled connections from permanently consuming a worker slot
- Now-playing panel refresh is debounced (0.8s) with a state-key check to skip redundant Discord edits
- Queue duration tracked as a running total (`O(1)`) rather than summing on every render

---

## Requirements

- Python 3.11+
- FFmpeg on `PATH`
- Discord bot token
- Last.fm API key *(optional — required for `!vibe` curation and the per-server `!autoplay` toggle only)*

---

## Installation

### Automated VPS setup

**Deploying to a fresh Ubuntu VPS?** Clone the repo to the server, then run the setup script for your host — it installs everything, walks you through getting a Discord token and (optionally) a Last.fm key with live validation, and starts the bot as a systemd service in one go. All three scripts share the same installer under the hood (`deploy/_common.sh`); they only differ in a couple of host-specific checks and reminders.

```bash
git clone https://github.com/Pylxyr/PyxeeBot.git ~/musicbot
cd ~/musicbot
```

| Host | Script | What it adds on top of the shared installer |
|---|---|---|
| Oracle Cloud | `bash deploy/setup_oracle.sh` | Reports whether you're on the AMD (E2.1.Micro) or ARM (Ampere A1) Always Free shape; notes Oracle's ~10 TB/month egress allowance |
| Google Cloud | `bash deploy/setup_gcp.sh` | Checks the VM's region against GCP's Always Free eligibility (`us-west1`/`us-central1`/`us-east1`) via the instance metadata server; warns about the 1 GB/month egress cap and the Network Service Tier / boot disk type gotchas that void the free tier |
| Anything else (DigitalOcean, Hetzner, AWS, bare metal, etc.) | `bash deploy/setup.sh` | Nothing extra — just the shared installer |

`APP_DIR` defaults to wherever you actually cloned the repo (not a hardcoded path), so it doesn't matter what you name the folder or where it lives — `cd` into it and run the matching script. All three also add a 1 GB swap file automatically on any host with ≤2 GB RAM, since a single yt-dlp/ffmpeg burst can otherwise pressure a 1 GB box hard enough to risk an OOM-killed SSH session.

### Local setup

The steps below are for local development or platforms other than the automated script above.

**1. Clone**

```bash
git clone https://github.com/Pylxyr/PyxeeBot.git
cd PyxeeBot
```

**2. Create a virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

**4. Configure**

Copy `deploy/.env.example` to `.env` in the project root and fill in your token:

```bash
cp deploy/.env.example .env
```

```env
DISCORD_TOKEN=your_discord_bot_token

# Optional
LASTFM_API_KEY=your_lastfm_api_key
DEFAULT_PREFIX=!
```

**5. Run**

```bash
python bot.py
```

---

## Running as a systemd service

> If you used one of the `deploy/setup_*.sh` scripts, this is already done — the bot is running as a systemd service. The steps below are for setting it up manually.

Create `/etc/systemd/system/musicbot.service`:

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
MemoryDenyWriteExecute=yes
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
sudo systemctl enable musicbot
sudo systemctl start musicbot
journalctl -u musicbot -f -o cat
```

---

## Configuration

All settings are read from `.env`. Every value has a default. See `deploy/.env.example` for the full annotated list.

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
| `YTDLP_COOKIES_FILE` | — | Path to Netscape cookies file |
| `YTDLP_JS_RUNTIME_PATH` | — | Path to a Node.js binary, for sites requiring JS signature decryption. If unset, yt-dlp 2026.08+ will try to auto-detect and use a `deno` runtime on `PATH` instead; set this to pin Node explicitly |
| `OPUS_BITRATE_KBPS` | `64` | Opus encoding bitrate (64–256) |
| `NP_AUTO_REFRESH` | `false` | Auto-refresh the now-playing panel on a timer |
| `NP_AUTO_REFRESH_INTERVAL` | `30` | Auto-refresh interval in seconds |
| `ERROR_ANNOUNCE` | `true` | Post playback errors to the announce channel |
| `RESTORE_QUEUE_ON_RESTART` | `true` | Restore queue from snapshot after bot restart |
| `BOT_ACTIVITY_URL` | `pylxyr.github.io/PyxeeBot-Page/` | Text shown in the bot's Discord status ("Watching …") |

---

## Commands

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
| `!playlist save <name>` | — | Save the current queue as a named server playlist |
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
| `!ping` | — | Check gateway latency |
| `!commands` | `cmds` | Open the command help menu |

---

## Project Structure

```
PyxeeBot/
├── bot.py                          # Entry point
├── requirements.txt
├── pyproject.toml                  # ruff (py311, E/F/W) and mypy (strict) config
├── .github/
│   └── workflows/
│       └── deploy.yml              # CI: lint → format-check → security-audit → SSH deploy to the configured VPS
├── deploy/
│   ├── _common.sh                  # Shared install engine — sourced by the three setup_*.sh scripts, not run directly
│   ├── setup_oracle.sh             # Interactive one-run setup wizard for Oracle Cloud
│   ├── setup_gcp.sh                # Interactive one-run setup wizard for Google Cloud
│   ├── setup.sh                    # Interactive one-run setup wizard for any other Ubuntu/Debian VPS
│   ├── musicbot.service            # systemd unit (ProtectHome, MemoryMax, SystemCallFilter, logrotate)
│   ├── musicbot-logrotate          # logrotate config (weekly, copytruncate)
│   ├── twitch_background.png       # Looping video background for the Twitch relay (swap for your own art)
│   └── .env.example                # Annotated environment template
├── musicbot/
│   ├── __init__.py
│   ├── bot.py                      # MusicBot subclass, help command, startup, owner resolution
│   ├── config.py                   # Settings dataclass, env var loading
│   ├── database.py                 # aiosqlite wrapper; all write methods hold a shared write lock
│   ├── twitch/                     # Optional — only runs if TWITCH_STREAM_KEY is set (see config.twitch_enabled)
│   │   ├── __init__.py
│   │   ├── relay.py                # TwitchRadioRelay: persistent RTMP muxer, gapless request queue
│   │   ├── chatbot.py              # TwitchChatBot + SongRequestComponent: !sr, !skip, !queue, !nowplaying
│   │   ├── tunables.py             # TwitchTunables: live request-limit settings, DB-backed
│   │   └── admin_server.py         # aiohttp: /nowplaying.json overlay feed + /settings GUI
│   └── cogs/
│       ├── __init__.py
│       ├── admin.py                # AdminCog: prefix, DJ, stay, autoplay, stats, ping, commands
│       ├── curation.py             # CurationCog: !vibe family, autoplay queue trigger
│       └── music/
│           ├── __init__.py         # Public surface: exports MusicCog and EMBED_COLOUR
│           ├── cog.py              # MusicCog: composes all mixins, owns shared state dicts
│           ├── _base.py            # MusicCogBase: shared attribute and method stubs for all mixins
│           ├── constants.py        # FFmpeg options, YTDL options, LoopMode, UI limits
│           ├── models.py           # Track, ResolvedTrackData, NowPlayingController dataclasses
│           ├── views.py            # Discord UI views: SearchSelection, Queue, NowPlaying
│           ├── player.py           # GuildPlayer: queue, playback loop, history, stay-connected flag
│           ├── _context.py         # _CURRENT_GUILD_ID ContextVar for yt-dlp pool; GuildContext type
│           ├── _extraction.py      # ExtractionMixin: yt-dlp wrapper, audio source construction
│           ├── _resolver.py        # ResolverMixin: stream URL resolution, per-track TTL cache
│           ├── _lifecycle.py       # LifecycleMixin: player creation (race-condition lock), snapshot restore
│           ├── _panel.py           # NPanelMixin: now-playing embed, debounced refresh loop
│           ├── _events.py          # EventsMixin: voice state and disconnect event handlers
│           ├── _helpers.py         # CommandHelpersMixin: DJ checks, skip votes, owner checks
│           ├── _playback_commands.py   # join, leave, play, playnext, pause, resume, skip, etc.
│           ├── _queue_commands.py      # queue, clear, shuffle, move, remove, history, toptracks, toprequestors
│           ├── _search_commands.py     # search
│           └── _playlist_commands.py  # playlist save/load/list/show/delete
```

---

## Architecture Notes

**Player loop.** Each guild has one `GuildPlayer` with a long-running `_player_loop` asyncio task. Creation is protected by a per-guild `asyncio.Lock` to prevent a TOCTOU race where two concurrent commands (`!join` and `!play`) could each create an independent player before either writes to `self.players`. The loop pre-resolves the next track's stream URL via `_resolve_track_data` and stores it in a TTL cache (128 entries, 30-min TTL). Stream URLs are also refreshed 30s before the current track ends (`NEAR_END_PREFETCH_SECONDS`), and any cached stream URL older than 4 hours (`STREAM_URL_REFRESH_AGE_SECONDS`) is considered stale and re-resolved before playback.

**Audio pipeline.** yt-dlp extracts a direct stream URL; FFmpeg reads it over HTTP and re-encodes to Opus at the configured bitrate. Copy mode (`-c:a copy`) is explicitly avoided — discord.py's constructor maps any detected `opus`/`libopus` codec to copy mode, which bypasses the libopus encoder and causes pacing irregularities. The bitrate is read from yt-dlp's `abr` field (available in the manifest), so no second probe connection is ever made. The FFmpeg subprocess is created immediately before `voice_client.play()` — after the voice connection has stabilised and any reconnect delay has elapsed — to prevent pre-buffered audio causing fast-forward at the start of the first track in a session.

**Database.** A single `aiosqlite.Connection` is shared across the process. All write methods hold a module-level `asyncio.Lock` before executing — SQLite transactions are connection-scoped, so a concurrent single-statement `commit()` from one guild can otherwise land inside and force-commit another guild's still-open `BEGIN IMMEDIATE` transaction silently. Tables: `guild_settings` (prefix, DJ role, stay-connected, autoplay per guild), `saved_playlists` + `saved_playlist_items` (named server playlists), `queue_snapshots` (queue restored on restart), `play_history` (backing `!toptracks` and `!toprequestors`). `play_history` is capped at 5 000 rows per guild (trimmed every 50 inserts). Two indexes cover it: `(guild_id, played_at)` for recency scans and `(guild_id, webpage_url, played_at)` for the `GROUP BY webpage_url` pattern used by `!toptracks`. A `PRAGMA wal_checkpoint(PASSIVE)` runs every 100 play-history writes to prevent WAL file growth on long sessions.

**Search resolution.** `!play`/`!playnext` and Last.fm curation resolve a text query by fetching `YTDLP_SEARCH_RESULTS` raw candidates from yt-dlp (in YouTube's own relevance order) and taking the first one that has a usable webpage URL — this is a safety margin against occasional malformed search entries, not a ranking step. `!search` fetches `SEARCH_SELECTION_LIMIT` candidates the same way and presents all of them for the user to pick from directly.

**yt-dlp concurrency.** All extractions run in `ThreadPoolExecutor(max_workers=2)`. Two separate global semaphores gate concurrent work: `asyncio.Semaphore(YTDLP_CONCURRENT_EXTRACTS)` for playback-path extractions, and a dedicated `asyncio.Semaphore(YTDLP_CURATION_CONCURRENCY)` for curation — kept separate so a `!vibe` confirmation resolving a large batch in the background can never monopolize the single playback slot and stall `!play`/`!playnext`/`!search`. A further per-guild semaphore (`Semaphore(1)`) isolates playback-path extractions from other guilds, and curation (`!vibe`) also has its own per-guild semaphore sized by `YTDLP_CURATION_CONCURRENCY`. The thread pool is automatically recycled after 3 consecutive `asyncio.wait_for` timeouts, since a genuinely-stuck thread (e.g. blocked in DNS resolution outside a socket timeout) can't be force-killed and would otherwise permanently consume a worker slot.

**Bot owner resolution.** `setup_hook` calls `application_info()` to populate `owner_id` (personal app) or `owner_ids` (team-owned app, admin/developer roles only) at startup. If the Discord API is temporarily unavailable, the call is caught and the bot falls back to `BOT_OWNERS` only rather than aborting startup. discord.py would otherwise only populate these lazily on first `is_owner()` call, which nothing in this codebase triggers — meaning owner-only commands would silently fail for anyone not listed in `BOT_OWNERS`.

**Permissions.** DJ-gated actions (`!forceskip`, `!skipto`, `!clear`, `!shuffle`, `!move`, `!loop`, `!replay`, `!playnext`, etc.) accept either the configured DJ role (`!setdj`) or the Manage Server permission — a server manager always has DJ-level access, even before a DJ role is set. Vote-skip (`!skip`) is based on active human listeners in the voice channel, not guild member count.

---

## Contributing

Issues and pull requests are welcome. `pyproject.toml` config: `ruff` (`py311`, `E`/`F`/`W`) for linting and `mypy --strict` for type checking — please run both before opening a PR.

---

## License

MIT
