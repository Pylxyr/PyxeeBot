<div align="center">

<img src="https://github.com/Pylxyr/PyxeeBot-Page/blob/main/public/assets/logo.png" alt="PyxeeBot" width="120" />

# PyxeeBot

**A self-hosted Discord music bot built for music communities that care about getting the right track.**

Stream from YouTube · Last.fm curation · Custom search scoring · Live controls

[![Python](https://img.shields.io/badge/Python-3.11%2B-3572A5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![discord.py](https://img.shields.io/badge/discord.py-2.7.1-5865F2?style=flat-square&logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=flat-square)](LICENSE)
[![Website](https://img.shields.io/badge/Website-pylxyr.github.io%2FPyxeeBot--Page-FFAA40?style=flat-square)](https://pylxyr.github.io/PyxeeBot-Page/)

</div>

---

## Overview

PyxeeBot is a fully self-hosted Discord music bot designed for servers that want accurate track selection and a polished playback experience. It runs on a single Linux instance with no external services beyond a Discord token and an optional Last.fm API key.

The search engine doesn't just take the top YouTube result. It runs every candidate through a multi-factor scoring pass — token overlap, sequence similarity, anchor phrase detection, uploader signals, live/cover penalties, Japanese-original detection — to select the most accurate studio version of what you actually asked for.

**[→ Visit the project page](https://pylxyr.github.io/PyxeeBot-Page/)**

---

## Features

### Playback

Streams audio from YouTube URLs, playlists, or plain search queries. Supports vote-skip, force-skip, loop modes (off / single track / full queue), previous track, pause/resume, and idle/empty-channel auto-disconnect. The queue survives a bot restart via SQLite snapshot persistence — `!clear` and `!leave` both flush the snapshot immediately so the queue does not reappear after a restart. Set `RESTORE_QUEUE_ON_RESTART=false` to disable restoration entirely.

A live now-playing panel shows a real-time progress bar and inline controls — skip, pause, loop, queue — without leaving the channel. The panel auto-refreshes on queue mutations and skips redundant HTTP edits when nothing visible has changed.

### Search Engine

Queries go through a custom multi-factor scoring engine built entirely in Python with no external ML dependencies. Each candidate is evaluated across 20+ weighted factors:

| Signal | What it measures |
|---|---|
| Token overlap | How many query words appear in the title / uploader |
| Sequence ratio | Full string similarity via rapidfuzz |
| Anchor phrases | Artist name extracted from cross-candidate analysis |
| Topic channel bonus | YouTube Music auto-generated channels (always studio) |
| Preferred uploaders | Label channels — HYBE, SMTOWN, Avex, Victor, etc. |
| Live / concert penalty | Suppresses festival recordings, BBC sessions, TV performances — checks title, description, and yt-dlp's `was_live` flag |
| Cover penalty | Suppresses guitar/piano covers, karaoke, English covers |
| Duration sanity | Penalises hour-long compilations and <60s clips |
| JP original detection | Boosts kana-titled uploads for J-pop / anime searches, with an extra boost when the query is romanized Latin and the uploader matches |
| View count signal | Log-scaled bonus, capped to avoid popularity bias |
| Recency bonus | Boosts tracks uploaded within the last 6 months / 1 year / 2 years |

Run `!why` after any search to see the full per-candidate score breakdown in Discord, or DM yourself the complete component-level breakdown.

### Vibe Curation (Last.fm)

`!vibe <query>` discovers similar tracks using Last.fm's `track.getSimilar` API. Results are sorted by Last.fm match confidence (0.0–1.0) so the strongest recommendations appear first. A curation panel lets you deselect tracks before queuing. When the queue drops to ≤10 tracks, a refill prompt surfaces automatically. Selecting tracks in the dropdown sends an ephemeral confirmation so it is clear which items are marked for exclusion before you commit with **Add All**.

Curation resolutions for a single guild run up to `YTDLP_CURATION_CONCURRENCY` at a time (own per-guild semaphore, separate from the playback path), bounded overall by `YTDLP_CONCURRENT_EXTRACTS`.

Vibe searches use a strengthened version of the scoring engine — live/concert penalties are tripled, Topic channel bonus is raised, and queries are biased toward `official audio` to keep studio versions out of reach of festival recordings.

Save and reload named curated playlists with `!vibe-save` / `!vibe-load`.

If `AUTOPLAY=true`, the bot queues one similar track (via the same Last.fm pipeline) whenever the queue fully empties, using the last completed track as the seed — no `!vibe` required.

### URL Pipeline

A background pipeline pre-resolves stream URLs for the top 3 queue positions as soon as tracks are enqueued, sequentially, with no concurrent yt-dlp calls. By the time the current track ends, the next track's URL has been warm for its entire duration — no gap, no buffering wait between tracks. A 20-second safety-net near-end refresh covers the edge case where a URL ages out during a long session.

### Performance

- **rapidfuzz** replaces difflib for all similarity scoring — 10–100× faster in the hot path
- **Thread-local YoutubeDL instances** — construction cost (5–20ms) paid once per options variant per worker thread; each thread holds its own instance set so concurrent extractions never share a `YoutubeDL` object across threads
- **Cached markdown escaping** on Track objects — `re.sub` runs once per track, not per render
- **Embed hash comparison** — NP panel skips HTTP edits when visible state is unchanged
- **Bounded queue deque** — enforced at the data structure level via `maxlen`
- **Running duration total** — O(1) queue total time instead of O(n) sum on every render
- **Proper 20ms Opus frames** — FFmpeg is always re-encoded through libopus (never `codec="copy"`) and forced to `-frame_duration 20 -flush_packets 1` to eliminate audio fast-forward and jitter caused by packet buffering. `codec="copy"` skips the encoder entirely, so these flags have no effect and playback fast-forwards for the first few seconds — do not reintroduce it as an optimization
- **Bounded yt-dlp socket timeout** — `socket_timeout: 15` on every extraction call prevents a single stalled connection from hanging the shared 2-worker extraction thread pool indefinitely
- **Extraction pool self-healing** — 3 consecutive `YTDLP_EXTRACT_TIMEOUT_SECONDS` timeouts recycle the 2-worker thread pool, since a hung thread (e.g. stuck DNS resolution) can't otherwise be force-killed and would permanently consume a worker slot

---

## Requirements

- Python 3.11+
- FFmpeg on `PATH`
- Discord bot token
- Last.fm API key *(optional — required for `!vibe` curation only)*

---

## Installation

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

All settings are read from `.env`. Every value has a default.

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | required | Bot token |
| `LASTFM_API_KEY` | — | Enables `!vibe` curation |
| `DEFAULT_PREFIX` | `!` | Command prefix |
| `BOT_OWNERS` | — | Comma-separated owner user IDs |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_TO_FILE` | `true` | Write logs to `LOG_DIR` (rotated weekly by `deploy/musicbot-logrotate`, not in-process) |
| `LOG_DIR` | `logs` | Log file directory |
| `MAX_QUEUE_SIZE` | `100` | Maximum queued tracks |
| `MAX_PLAYLIST_SIZE` | `25` | Maximum tracks loaded from a playlist URL |
| `IDLE_TIMEOUT_SECONDS` | `180` | Disconnect after idle this long |
| `EMPTY_CHANNEL_TIMEOUT_SECONDS` | `60` | Disconnect when alone this long |
| `YTDLP_SOCKET_TIMEOUT` | `15` | yt-dlp socket timeout (seconds) |
| `YTDLP_SEARCH_RESULTS` | `5` | Candidates fetched per search |
| `YTDLP_RESOLVE_CACHE_SIZE` | `128` | Stream URL cache size |
| `YTDLP_RESOLVE_CACHE_TTL_SECONDS` | `1800` | Stream URL cache TTL |
| `YTDLP_EXTRACT_TIMEOUT_SECONDS` | `45` | Abort yt-dlp after this long |
| `RESTORE_QUEUE_ON_RESTART` | `true` | Restore queue from DB snapshot on startup |
| `YTDLP_CONCURRENT_EXTRACTS` | `1` | Max simultaneous yt-dlp extractions — keep at `1` on single-core hosts |
| `OPUS_BITRATE_KBPS` | `96` | Opus encoding bitrate |
| `NP_AUTO_REFRESH` | `false` | Auto-refresh NP embed on a timer |
| `NP_AUTO_REFRESH_INTERVAL` | `30` | Seconds between auto-refresh edits |
| `YTDLP_COOKIES_FILE` | — | Path to Netscape cookies file |
| `YTDLP_JS_RUNTIME_PATH` | — | Path to a Node.js binary, for sites requiring JS signature decryption |
| `YTDLP_PREFETCH_COUNT` | `1` | Queue positions to pre-resolve in the background pipeline |
| `YTDLP_CURATION_CONCURRENCY` | `3` | Concurrent yt-dlp resolutions during `!vibe` and `!vibe-load` (max 6) |
| `MAX_QUEUE_SIZE_PER_USER` | `0` | Per-user track limit; `0` disables the limit |
| `NEAR_END_PREFETCH_SECONDS` | `30` | Trigger safety-net URL refresh this many seconds before track end |
| `ERROR_ANNOUNCE` | `true` | Post playback errors to the announce channel |
| `AUTOPLAY` | `false` | Queue similar Last.fm tracks when the queue empties |

---

## Commands

### Playback

| Command | Aliases | Description |
|---|---|---|
| `!play <query>` | `p` | Queue a URL, playlist, or search query |
| `!playnext <query>` | `pn` | Insert next in queue (DJ) |
| `!search <query>` | `find`, `s` | Browse results and pick manually |
| `!skip` | `next` | Vote-skip or instant skip |
| `!forceskip` | `fs` | Immediate skip (DJ) |
| `!prev` | `previous`, `back` | Return to last completed track |
| `!pause` | — | Pause playback |
| `!resume` | — | Resume playback |
| `!stop` | — | Stop and clear loop mode (DJ) |
| `!nowplaying` | `np` | Open the live control panel |
| `!loop` | — | Cycle loop mode (DJ) |
| `!replay` | — | Re-queue current track (DJ) |
| `!repeat` | `rp` | Toggle single-track repeat |
| `!join` | `summon` | Join your voice channel |
| `!leave` | `disconnect` | Disconnect and clear session |

### Queue

| Command | Aliases | Description |
|---|---|---|
| `!queue` | `q` | Show current queue |
| `!remove <index>` | — | Remove a track by position |
| `!clear` | — | Flush the queue (DJ) |
| `!shuffle` | — | Randomise the queue (DJ) |
| `!move <from> <to>` | — | Reorder by position (DJ) |
| `!skipto <position>` | — | Jump ahead, dropping earlier tracks (DJ) |
| `!qsearch <keyword>` | `qs` | Search within the current queue |
| `!history` | — | Show recently played tracks |
| `!toptracks` | `top` | Show the most-played tracks for this server |

### Playlists

| Command | Description |
|---|---|
| `!playlist save <name>` | Save current queue as a named playlist |
| `!playlist load <name>` | Load a saved playlist into the queue |
| `!playlist list` | List all server playlists |
| `!playlist show <name>` | Show tracks in a saved playlist |
| `!playlist delete <name>` | Delete a playlist (DJ) |

### Curation

| Command | Aliases | Description |
|---|---|---|
| `!vibe <query>` | `vb` | Discover similar tracks via Last.fm |
| `!vibe-save <name>` | `vsave` | Save current curation session |
| `!vibe-load <name>` | `vload` | Queue a saved curated playlist |
| `!vibe-list` | `vlist` | List saved curated playlists |

### Admin & Debug

| Command | Aliases | Description |
|---|---|---|
| `!why` | `searchdebug`, `scorewhy` | Show last search score breakdown |
| `!setdj <role>` | — | Set the DJ role |
| `!cleardj` | — | Remove the DJ role |
| `!dj` | — | Show current DJ role |
| `!setprefix <prefix>` | — | Change the command prefix for this server |
| `!stay` | — | Toggle 24/7 mode (stay connected when queue empties) |
| `!stats` | — | Show bot process stats (owner only) |
| `!ping` | — | Check gateway latency |

---

## Project Structure

```
PyxeeBot/
├── bot.py
├── pyproject.toml       — pytest config
├── tests/
│   ├── conftest.py      — shared fixtures
│   ├── test_scoring.py  — scoring engine unit tests
│   └── test_player.py   — player state-transition tests
└── musicbot/
    ├── bot.py           — MusicBot, help command, logging, shutdown
    ├── config.py        — Settings dataclass, .env loader
    ├── database.py      — Async SQLite: prefixes, playlists, snapshots
    └── cogs/
        ├── admin.py     — Ping, DJ role management
        ├── curation.py  — Last.fm vibe/curation, auto-refill
        └── music/
            ├── cog.py                  — MusicCog: composes all mixins, init, lifecycle hooks
            ├── _lifecycle.py           — LifecycleMixin: player create/restore/cleanup, snapshots
            ├── _helpers.py             — CommandHelpersMixin: permission checks, shared utilities
            ├── _events.py              — EventsMixin: bot/player event listeners
            ├── _playback_commands.py   — PlaybackCommandsMixin: join/play/skip/pause/loop/etc.
            ├── _queue_commands.py      — QueueCommandsMixin: queue/remove/clear/shuffle/move
            ├── _search_commands.py     — SearchCommandsMixin: search, why
            ├── _playlist_commands.py   — PlaylistCommandsMixin: playlist save/load/list/show/delete
            ├── _extraction.py          — ExtractionMixin: yt-dlp, audio source, search
            ├── _resolver.py            — ResolverMixin: stream-URL cache and pipeline
            ├── _panel.py               — NPanelMixin: NP embed rendering and refresh
            ├── _context.py             — Shared ContextVar (avoids circular imports)
            ├── player.py               — Per-guild audio state machine
            ├── scoring.py              — Pure search scoring (no Discord dependency)
            ├── views.py                — Discord UI: NP panel, queue, search, debug
            ├── models.py               — Pure dataclasses
            └── constants.py            — FFmpeg options, scoring tables, timing
```

---

## Architecture Notes

**Search scoring** is fully pure — `scoring.py` has no Discord or bot imports and can be unit tested in isolation. Scores are logged at `DEBUG` level; `!why` surfaces them in Discord.

**cog.py split** — `MusicCog` composes ten mixins: `ExtractionMixin`, `ResolverMixin`, `NPanelMixin`, `LifecycleMixin`, `CommandHelpersMixin`, `EventsMixin`, `PlaybackCommandsMixin`, `QueueCommandsMixin`, `SearchCommandsMixin`, and `PlaylistCommandsMixin`. `cog.py` itself only holds `__init__`, lifecycle hooks (`cog_load`/`cog_unload`/`shutdown`/`cog_command_error`), and `_bg_task`. All instance state still lives in `MusicCog.__init__`; no mixin carries state of its own. discord.py's `CogMeta` walks the full MRO when collecting commands and listeners, so `@commands.hybrid_command` and `@commands.Cog.listener()` decorators work identically whether defined directly on `MusicCog` or on any mixin it inherits from. `_context.py` holds the shared `_CURRENT_GUILD_ID` ContextVar to avoid circular imports between the mixin files.

**Player loop** runs as a single long-lived `asyncio.Task` per guild. After each track finishes it re-evaluates loop mode, appends to history, and dispatches `musicbot_queue_updated` which triggers the snapshot debounce, NP refresh, and URL pipeline.

**Debounce pattern** — snapshot writes, NP embed refreshes, and presence updates all use a deadline-timestamp + single-long-lived-task approach instead of spawning a new task on every event. Each loop re-checks its deadline after the async operation completes, so a deadline extension written during a slow await is never silently dropped. This avoids task churn on active queues.

**URL pipeline** keeps the top 3 queue positions pre-resolved at all times. Runs sequentially (never concurrently) and yields `asyncio.sleep(0)` between resolves so the audio thread isn't starved. The near-end task is a 20-second safety net only — in normal operation the next URL is already warm.

**Stream URL validation** — `_validate_stream_url` distinguishes between a server explicitly rejecting a URL (HTTP 4xx/5xx → returns `False`, triggers re-resolve) and a network error during the HEAD check (timeout / connection error → returns `True`, keeps the cached URL). Network unavailability does not mean the URL is stale.

**Last.fm error handling** — `_lastfm` retries once on transient failures (5xx, timeout, network error) with a short backoff. 429 backs off 5 seconds before the retry. 403 logs at `ERROR` and returns immediately. JSON decode failures and API-level error payloads are both caught and logged.

**Audio timing** — FFmpeg is always re-encoded through libopus and forced to `-frame_duration 20 -flush_packets 1` to produce exactly 20ms Opus frames matching `AudioPlayer.DELAY`. This eliminates the fast-forward and mid-track jitter caused by `codec="copy"` passthrough sending raw container packets at whatever cadence FFmpeg pre-buffered them. `_build_audio_source` deliberately skips `FFmpegOpusAudio.from_probe()`'s ffprobe subprocess for known-Opus streams (yt-dlp already reports `acodec`), but it must never pass `codec="copy"` to skip the encoder too — that reintroduces the fast-forward bug since `-frame_duration`/`-flush_packets` are libopus-only options with zero effect in copy mode.

---

## Testing

```bash
pip install pytest pytest-asyncio
pytest
```

Tests live in `tests/`. `test_scoring.py` covers the pure scoring engine (normalize, tokenize, signal tokens, overlap ratios, live/cover penalties, topic bonuses, anchor matching, rank ordering, debug record eviction). `test_scoring_golden.py` pins down real-world ranking outcomes end-to-end — one case per scoring bug found and fixed in production (JP original vs. romanized live, correct song vs. wrong songs on the preferred channel, Chinese vs. Japanese script detection, live-recording detection, discouraged-penalty cap, anchor disambiguation, and more) so a future scoring tweak can't silently reintroduce a solved bug. `test_player.py` covers `GuildPlayer` state transitions (enqueue duration tracking, replace_queue, snapshot fallback chain, pause/resume accounting, skip, play_previous).

---

## License

MIT — see [LICENSE](LICENSE).
