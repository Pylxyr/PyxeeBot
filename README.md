# PyxeeBot

[![Python](https://img.shields.io/badge/Python-3.11%2B-3572A5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![discord.py](https://img.shields.io/badge/discord.py-2.7.1-5865F2?style=flat-square&logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-2026.06.09-CC0000?style=flat-square&logo=youtube&logoColor=white)](https://github.com/yt-dlp/yt-dlp)
[![Tests](https://img.shields.io/badge/tests-106%20passing-22c55e?style=flat-square&logo=pytest&logoColor=white)](tests/)
[![License](https://img.shields.io/badge/License-MIT-64748b?style=flat-square)](LICENSE)
[![Website](https://img.shields.io/badge/Website-PyxeeBot-FFAA40?style=flat-square)](https://pylxyr.github.io/PyxeeBot-Page/)

A self-hosted Discord music bot built with [discord.py](https://github.com/Rapptz/discord.py), yt-dlp, aiosqlite, and RapidFuzz. Designed to run well on a single-core VPS (tested on Oracle Cloud free-tier AMD E2.1.Micro running Ubuntu).

---

## Overview

- Plays audio from YouTube and YouTube Music
- A custom multi-signal scoring engine selects the most accurate YouTube result for any search query, with specific tuning for Japanese/anime content
- Last.fm integration for `!vibe` similar-track curation and per-server `!autoplay`
- Persistent queue snapshots survive restarts; per-server DJ role, prefix, 24/7 mode, and autoplay settings stored in SQLite
- Designed around the constraints of a 1/8-core shared VPS: single-threaded yt-dlp pool, 64 kbps Opus encoding, debounced panel refreshes, bounded deque-based queue

---

## Features

### Playback

- `!play` accepts YouTube/YouTube Music URLs, playlist URLs, or plain text search queries
- `!playnext` queues a track immediately after the current one
- `!search` shows up to 10 interactive results before committing
- Vote-skip (`!skip`): instant if you're the requester or a DJ; otherwise requires ≥50% of listeners to call it
- `!forceskip` — immediate skip, DJ-only
- `!skipto <position>` — jump to a queue position, dropping everything before it (DJ-only)
- `!prev` — requeue the last-played track
- `!pause` / `!resume`
- `!stop` — clears the queue and disconnects
- `!loop` — cycles through Off → Single track → Entire queue
- `!repeat` / `!replay` — aliases for one-track loop
- `!nowplaying` — live now-playing embed with queue preview

### Search Engine

The scoring engine ranks yt-dlp search candidates across multiple signals before committing to one:

- **Token overlap** — RapidFuzz partial ratio between query tokens and title
- **Artist / title format detection** — detects `Artist - Title` queries vs. bare title queries and adjusts anchor-phrase matching accordingly
- **Live/concert penalties** — live, concert, and festival keywords in title, description, or uploader name are penalised; tripled in curation mode
- **Cover penalties** — cover/tribute versions are penalised when not explicitly requested
- **Topic-channel bonus** — YouTube Music `- Topic` channels receive a bonus when title tokens also overlap
- **Verified-channel bonus** — applies for channels with a checkmark when title tokens match
- **JP/anime bonus** — CJK characters or hiragana/katakana in the title receive a small bonus; Latin-romanised query against a JP title gets an anchor-phrase bonus
- **Duration filter** — very short clips (<60s) and very long mixes (>20min) are penalised unless the query implies otherwise
- **Recency bonus** — tracks uploaded within the past two years receive a small boost, suppressed for heavily-penalised entries
- **Uploader preference** — known label/distributor uploaders receive a small bonus

### Vibe Curation (Last.fm)

`!vibe <query>` discovers similar tracks via Last.fm's `track.getSimilar` API. Results are sorted by match confidence (0.0–1.0). A curation panel lets you deselect tracks before queuing. When the queue drops to ≤10 tracks, a refill prompt surfaces automatically.

Curation resolutions for a single guild run up to `YTDLP_CURATION_CONCURRENCY` at a time (own per-guild semaphore, separate from the playback path), bounded overall by `YTDLP_CONCURRENT_EXTRACTS`.

Vibe searches use a strengthened version of the scoring engine — live/concert penalties are tripled, Topic channel bonus is raised, and queries are biased toward `official audio`.

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
- A global semaphore (`YTDLP_CONCURRENT_EXTRACTS`, default 1) limits concurrent extractions on the constrained vCPU
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

**Deploying to a fresh Ubuntu VPS (e.g. Oracle Cloud free tier)?** Clone the repo to the server, then run the setup script — it installs everything, walks you through getting a Discord token and (optionally) a Last.fm key with live validation, and starts the bot as a systemd service in one go:

```bash
git clone https://github.com/Pylxyr/PyxeeBot.git /home/ubuntu/musicbot
cd /home/ubuntu/musicbot
bash deploy/setup_oracle.sh
```

The manual steps below are for local development or other platforms.

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

> If you used `deploy/setup_oracle.sh`, this is already done — the bot is running as a systemd service. The steps below are for setting it up manually.

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
| `YTDLP_SEARCH_RESULTS` | `5` | Candidate count passed to the scoring engine per query |
| `YTDLP_RESOLVE_CACHE_SIZE` | `128` | Maximum cached stream URL entries |
| `YTDLP_RESOLVE_CACHE_TTL_SECONDS` | `1800` | Stream URL cache TTL (30 min) |
| `YTDLP_EXTRACT_TIMEOUT_SECONDS` | `45` | Per-extraction timeout |
| `YTDLP_SOCKET_TIMEOUT` | `15` | yt-dlp socket timeout |
| `NEAR_END_PREFETCH_SECONDS` | `30` | Trigger stream URL refresh this many seconds before track end |
| `YTDLP_COOKIES_FILE` | — | Path to Netscape cookies file |
| `YTDLP_JS_RUNTIME_PATH` | — | Path to a Node.js binary, for sites requiring JS signature decryption |
| `OPUS_BITRATE_KBPS` | `64` | Opus encoding bitrate (64–256) |
| `NP_AUTO_REFRESH` | `false` | Auto-refresh the now-playing panel on a timer |
| `NP_AUTO_REFRESH_INTERVAL` | `30` | Auto-refresh interval in seconds |
| `ERROR_ANNOUNCE` | `true` | Post playback errors to the announce channel |
| `RESTORE_QUEUE_ON_RESTART` | `true` | Restore queue from snapshot after bot restart |

---

## Commands

### Playback

| Command | Aliases | Description |
|---|---|---|
| `!join` | `summon` | Join your voice channel |
| `!leave` | `disconnect` | Leave the voice channel |
| `!play <query>` | `p` | Queue a URL, playlist, or search query. Cooldown: 2 uses / 4s per user |
| `!playnext <query>` | `pn` | Queue a track immediately after the current one |
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
| `!qsearch <keyword>` | `qs` | Search within the current queue |
| `!history` | — | Show recently played tracks (session only) |
| `!toptracks` | `top` | Show the all-time most-played tracks for this server |

### Search

| Command | Aliases | Description |
|---|---|---|
| `!search <query>` | `find`, `s` | Browse up to 10 interactive results before queuing. Cooldown: 1 use / 6s per user |
| `!why` | `searchdebug`, `scorewhy` | Show the score breakdown for the last search result |

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
| `!vibe-list` | `vlist` | List saved vibe playlists for this server |

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
├── pyproject.toml                  # pytest (asyncio_mode=auto) and ruff (py311, E/F/W) config
├── .github/
│   └── workflows/
│       └── deploy.yml              # CI: lint → test → SSH deploy to Oracle VPS
├── deploy/
│   ├── setup_oracle.sh             # Interactive one-run setup wizard for Ubuntu VPS
│   ├── musicbot.service            # systemd unit (ProtectSystem=full, memory limits, logrotate)
│   ├── musicbot-logrotate          # logrotate config (weekly, copytruncate)
│   └── .env.example                # Annotated environment template
├── musicbot/
│   ├── __init__.py
│   ├── bot.py                      # MusicBot subclass, help command, startup, owner resolution
│   ├── config.py                   # Settings dataclass, env var loading
│   ├── database.py                 # aiosqlite wrapper; all write methods hold a shared write lock
│   └── cogs/
│       ├── __init__.py
│       ├── admin.py                # AdminCog: prefix, DJ, stay, autoplay, stats, ping, commands
│       ├── curation.py             # CurationCog: !vibe family, autoplay queue trigger
│       └── music/
│           ├── __init__.py         # Public surface: exports MusicCog and EMBED_COLOUR
│           ├── cog.py              # MusicCog: composes all mixins, owns shared state dicts
│           ├── constants.py        # FFmpeg options, YTDL options, UI limits, scoring thresholds
│           ├── models.py           # Track, ResolvedTrackData, NowPlayingController dataclasses
│           ├── scoring.py          # Multi-signal search result scoring and ranking engine
│           ├── views.py            # Discord UI views: SearchSelection, Queue, NowPlaying, ScoreDebug
│           ├── player.py           # GuildPlayer: queue, playback loop, history, stay-connected flag
│           ├── _context.py         # ContextVar for current guild ID, threaded into the yt-dlp pool
│           ├── _extraction.py      # ExtractionMixin: yt-dlp wrapper, audio source construction
│           ├── _resolver.py        # ResolverMixin: stream URL resolution, per-track TTL cache
│           ├── _lifecycle.py       # LifecycleMixin: player creation (race-condition lock), snapshot restore
│           ├── _panel.py           # NPanelMixin: now-playing embed, debounced refresh loop
│           ├── _events.py          # EventsMixin: voice state and disconnect event handlers
│           ├── _helpers.py         # CommandHelpersMixin: DJ checks, skip votes, owner checks
│           ├── _playback_commands.py   # join, leave, play, playnext, pause, resume, skip, etc.
│           ├── _queue_commands.py      # queue, clear, shuffle, move, remove, qsearch, history, toptracks
│           ├── _search_commands.py     # search, why
│           └── _playlist_commands.py  # playlist save/load/list/show/delete
└── tests/
    ├── __init__.py
    ├── conftest.py                 # make_bot, make_guild, make_track, make_settings helpers
    ├── test_player.py              # GuildPlayer: enqueue, capacity, duration, snapshot, skip, prev (30 tests)
    ├── test_scoring.py             # Scoring engine: normalisation, signals, rank_entries() (41 tests)
    ├── test_scoring_golden.py      # Golden ranking scenarios against real J-pop/anime fixtures (14 tests)
    └── test_concurrency.py         # Concurrency and correctness regression tests (21 tests)
```

---

## Architecture Notes

**Player loop.** Each guild has one `GuildPlayer` with a long-running `_player_loop` asyncio task. Creation is protected by a per-guild `asyncio.Lock` to prevent a TOCTOU race where two concurrent commands (`!join` and `!play`) could each create an independent player before either writes to `self.players`. The loop pre-resolves the next track's stream URL via `_resolve_track_data` and stores it in a TTL cache (128 entries, 30-min TTL). Stream URLs are also refreshed 30s before the current track ends (`NEAR_END_PREFETCH_SECONDS`), and any cached stream URL older than 4 hours (`STREAM_URL_REFRESH_AGE_SECONDS`) is considered stale and re-resolved before playback.

**Audio pipeline.** yt-dlp extracts a direct stream URL; FFmpeg reads it over HTTP and re-encodes to Opus. Copy mode (`-c:a copy`) is explicitly avoided — the fallback path that would normally use `FFmpegOpusAudio.from_probe()` instead probes for bitrate only and discards the detected codec, since discord.py's constructor silently maps any detected `opus`/`libopus` result to copy mode, which bypasses the libopus encoder and causes pacing irregularities audible as fast-forward artefacts at the start of a track.

**Database.** A single `aiosqlite.Connection` is shared across the process. All write methods hold a module-level `asyncio.Lock` before executing — SQLite transactions are connection-scoped, so a concurrent single-statement `commit()` from one guild can otherwise land inside and force-commit another guild's still-open `BEGIN IMMEDIATE` transaction silently. Tables: `guild_settings` (prefix, DJ role, stay-connected, autoplay per guild), `saved_playlists` + `saved_playlist_items` (named server playlists), `queue_snapshots` (queue restored on restart), `play_history` (backing `!toptracks`).

**Scoring engine.** `scoring.py` is the most complex module. It normalises query and candidate text, tokenises with stop-word removal, then assembles a weighted score from ~15 signals including fuzzy token overlap (RapidFuzz), anchor-phrase matching, live/cover/mix duration penalties, topic-channel and verified-channel bonuses, JP/anime bonuses, and recency. The final `rank_entries()` call sorts and returns the best candidate.

**yt-dlp concurrency.** All extractions run in `ThreadPoolExecutor(max_workers=2)`. A global `asyncio.Semaphore(YTDLP_CONCURRENT_EXTRACTS)` gates concurrent work. A separate per-guild semaphore (`Semaphore(1)`) isolates playback-path extractions from other guilds. Curation (`!vibe`) uses its own per-guild semaphore sized by `YTDLP_CURATION_CONCURRENCY` so it doesn't compete with the playback semaphore. The thread pool is automatically recycled after 3 consecutive `asyncio.wait_for` timeouts, since a genuinely-stuck thread (e.g. blocked in DNS resolution outside a socket timeout) can't be force-killed and would otherwise permanently consume a worker slot.

**Bot owner resolution.** `setup_hook` calls `application_info()` to populate `owner_id` (personal app) or `owner_ids` (team-owned app, admin/developer roles only) at startup. discord.py would otherwise only populate these lazily on first `is_owner()` call, which nothing in this codebase triggers — meaning owner-only commands would silently fail for anyone not listed in `BOT_OWNERS`.

---

## Testing

```bash
pip install pytest pytest-asyncio
pytest tests/ -q
```

106 tests across four files:

- **`test_player.py`** (30) — `GuildPlayer` state: enqueue, queue capacity, duration tracking, snapshot serialisation, pause/resume timing, skip, and prev
- **`test_scoring.py`** (41) — scoring engine units: text normalisation, tokenisation, signal functions, and `rank_entries()` end-to-end
- **`test_scoring_golden.py`** (14) — golden ranking scenarios against real J-pop/anime fixture data, each asserting a specific track wins over a distracting alternative
- **`test_concurrency.py`** (21) — regression tests for concurrency bugs and correctness invariants: `_get_player` TOCTOU race, database write-lock covering all 7 write methods, `CancelledError` propagation through the shared-resolve shield, per-guild autoplay DB/command toggle, `setup_hook` owner population for personal and team-owned apps, reconnect announcement cooldown logic, and owner-check coverage for both `_is_authorized_owner` and `_is_bot_owner`

All async tests use `pytest-asyncio` in `auto` mode (configured in `pyproject.toml`). Ruff is configured for `py311` with `E`, `F`, `W` rules at line length 110.

---

## License

MIT
