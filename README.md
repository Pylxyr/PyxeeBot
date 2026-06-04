# PyxeeBot

A self-hosted Discord music bot built in Python. Streams audio from YouTube, delivers a polished playback experience, and uses Last.fm to discover and curate music based on your taste.

---

## Features

**Playback**
- Stream audio from YouTube URLs, playlists, or plain search queries
- Vote-skip, force-skip, loop modes (off / single track / full queue), and previous track
- Live now-playing panel with progress bar, inline controls, and auto-refresh
- Queue browsing with pagination, search within the queue, and track reordering
- Snapshot persistence — the queue survives a bot restart

**Search**
- Custom multi-factor scoring engine ranks results by title/artist overlap, token matching, sequence similarity, preferred uploaders, and discouraged content penalties
- Japanese-original-upload detection (`jp_original_bonus`) for accurate J-pop/anime results
- `!search` command opens a paginated selection menu when you want to pick manually
- `!why` shows a full per-candidate score breakdown; DM yourself the complete breakdown

**Curation (Last.fm)**
- `!vibe <query>` — discovers up to 25 similar tracks via Last.fm, shows a curated panel where you can remove tracks before queuing
- Auto-refill — when the queue drops to ≤ 10 tracks, a refill prompt appears with fresh suggestions
- Save and reload named curated playlists with `!vibe-save` / `!vibe-load`

**Server management**
- Per-guild DJ role, configurable command prefix, playlist library
- Idle and empty-channel auto-disconnect
- Graceful shutdown with SIGTERM handling

---

## Requirements

- Python 3.11+
- FFmpeg (must be on `PATH`)
- A Discord bot token
- A Last.fm API key (optional — required only for `!vibe` curation)

---

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/Pylxyr/PyxeeBot.git
cd PyxeeBot
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Create a `.env` file in the project root**

```env
DISCORD_TOKEN=your_discord_bot_token_here

# Optional
LASTFM_API_KEY=your_lastfm_api_key_here
DEFAULT_PREFIX=!
BOT_OWNERS=123456789012345678
```

See [Configuration](#configuration) for the full list of options.

**4. Run the bot**

```bash
python bot.py
```

---

## Running as a systemd service (Linux)

Create `/etc/systemd/system/musicbot.service`:

```ini
[Unit]
Description=PyxeeBot Discord Music Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/musicbot
ExecStart=/usr/bin/python3 /home/ubuntu/musicbot/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable musicbot
sudo systemctl start musicbot
sudo journalctl -u musicbot -f   # follow logs
```

---

## Configuration

All settings are read from `.env`. Every value has a sensible default.

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Your bot's Discord token |
| `LASTFM_API_KEY` | — | Last.fm API key; enables `!vibe` curation |
| `DEFAULT_PREFIX` | `!` | Command prefix |
| `BOT_OWNERS` | — | Comma-separated Discord user IDs with owner privileges |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_TO_FILE` | `true` | Write rotating log files to `LOG_DIR` |
| `LOG_DIR` | `logs` | Directory for log files (relative to project root) |
| `MAX_QUEUE_SIZE` | `100` | Maximum number of tracks in the queue |
| `MAX_PLAYLIST_SIZE` | `25` | Maximum tracks loaded from a playlist URL |
| `IDLE_TIMEOUT_SECONDS` | `180` | Disconnect after this many seconds with an empty queue |
| `EMPTY_CHANNEL_TIMEOUT_SECONDS` | `60` | Disconnect after this many seconds alone in a voice channel |
| `YTDLP_SOCKET_TIMEOUT` | `15` | yt-dlp network socket timeout (seconds) |
| `YTDLP_PREFETCH_COUNT` | `1` | Tracks to pre-resolve ahead of the current one |
| `YTDLP_CONCURRENT_EXTRACTS` | `1` | Global concurrency limit for yt-dlp operations |
| `YTDLP_SEARCH_RESULTS` | `5` | Candidates fetched per search query |
| `YTDLP_RESOLVE_CACHE_SIZE` | `128` | Maximum entries in the stream URL cache |
| `YTDLP_RESOLVE_CACHE_TTL_SECONDS` | `1800` | Stream URL cache TTL (seconds) |
| `YTDLP_EXTRACT_TIMEOUT_SECONDS` | `45` | Abort a yt-dlp operation after this many seconds |
| `NEAR_END_PREFETCH_SECONDS` | `30` | Start resolving the next track this many seconds before the current one ends |
| `OPUS_BITRATE_KBPS` | `96` | Opus encoding bitrate |
| `NP_AUTO_REFRESH` | `false` | Automatically refresh the now-playing embed on a timer |
| `NP_AUTO_REFRESH_INTERVAL` | `30` | Seconds between auto-refresh edits |
| `ERROR_ANNOUNCE` | `true` | Post skip-error messages to the announce channel |
| `YTDLP_COOKIES_FILE` | — | Path to a Netscape-format cookies file for age-restricted videos |
| `YTDLP_JS_RUNTIME_PATH` | — | Path to Node.js binary for yt-dlp JS-based extraction |

---

## Commands

### Playback

| Command | Aliases | Description |
|---|---|---|
| `!join` | `summon` | Join your voice channel |
| `!leave` | `disconnect` | Disconnect and clear the session |
| `!play <query>` | `p` | Queue a URL, playlist, or search query |
| `!playnext <query>` | `pn` | Insert a track next in queue (DJ only) |
| `!search <query>` | `find`, `s` | Browse results and pick one manually |
| `!skip` | `next` | Vote-skip the current track |
| `!forceskip` | `fs` | Immediately skip (DJ only) |
| `!prev` | `previous`, `back` | Return to the last track |
| `!pause` | — | Pause playback |
| `!resume` | — | Resume playback |
| `!stop` | — | Stop playback and clear loop mode (DJ only) |
| `!nowplaying` | `np` | Open the live control panel |
| `!loop` | — | Cycle loop: off → single track → full queue → off (DJ only) |
| `!repeat` | `rp` | Toggle repeat for the current track |
| `!replay` | — | Re-queue the current track (DJ only) |

### Queue

| Command | Aliases | Description |
|---|---|---|
| `!queue` | `q` | Show the current queue |
| `!remove <index>` | — | Remove a track by position |
| `!clear` | — | Flush the queue (DJ only) |
| `!shuffle` | — | Randomise the queue (DJ only) |
| `!move <from> <to>` | — | Move a track to a different position (DJ only) |
| `!skipto <position>` | — | Jump to a queue position, dropping everything before it (DJ only) |
| `!qsearch <keyword>` | `qs` | Search within the current queue |
| `!history` | — | Show recently played tracks |

### Playlists

| Command | Description |
|---|---|
| `!playlist save <name>` | Save the current queue as a named playlist |
| `!playlist load <name>` | Load a saved playlist into the queue |
| `!playlist list` | List all saved playlists for this server |
| `!playlist show <name>` | Show the tracks in a saved playlist |
| `!playlist delete <name>` | Delete a saved playlist (DJ only) |

### Curation (Last.fm)

| Command | Aliases | Description |
|---|---|---|
| `!vibe <query>` | `vb` | Discover similar tracks and open the curation panel |
| `!vibe-save <name>` | `vsave` | Save the active curation session as a playlist |
| `!vibe-load <name>` | `vload` | Queue a saved curated playlist |
| `!vibe-list` | `vlist` | List all saved curated playlists |

### Debug & Admin

| Command | Aliases | Description |
|---|---|---|
| `!why` | `searchdebug`, `scorewhy` | Show how the last search's results were scored |
| `!ping` | — | Check gateway latency |
| `!setdj <role>` | — | Set the DJ role (Manage Server required) |
| `!cleardj` | — | Remove the DJ role (Manage Server required) |
| `!dj` | — | Show the current DJ role |

---

## Project Structure

```
PyxeeBot/
├── bot.py                          # Entry point
└── musicbot/
    ├── bot.py                      # MusicBot class, help command, logging, shutdown
    ├── config.py                   # Settings dataclass, .env loader
    ├── database.py                 # Async SQLite: prefixes, DJ roles, playlists, snapshots
    └── cogs/
        ├── admin.py                # Ping, DJ role, prefix management
        ├── curation.py             # Last.fm vibe/curation, auto-refill
        └── music/
            ├── cog.py              # MusicCog: commands and event handlers
            ├── player.py           # GuildPlayer: per-guild audio state machine
            ├── scoring.py          # Pure search-scoring functions
            ├── views.py            # Discord UI views (NP, queue, search, debug)
            ├── models.py           # Pure data classes
            ├── constants.py        # FFmpeg options, scoring tables, brand constants
            └── resolve.py          # ResolveCache: stream URL cache with task dedup
```

---

## Architecture Notes

**Search scoring** — `scoring.py` is fully pure (no Discord/bot dependencies). Each candidate is scored across ~20 weighted factors including token overlap, sequence similarity, anchor phrase matching, uploader signals, duration sanity, view count, channel verification, and content penalties. Scores are logged at `DEBUG` level; `!why` surfaces them in Discord.

**Player loop** — `GuildPlayer` runs a single long-lived `asyncio.Task` per guild. After each track finishes it re-evaluates loop mode, appends to history, and dispatches `musicbot_queue_updated` for downstream listeners (snapshot writes, NP refresh, curation refill check).

**Debounce pattern** — Snapshot writes and NP embed refreshes both use a deadline-timestamp + single-long-lived-task approach instead of creating a new task on every event, avoiding task churn on busy queues.

**Resolve cache** — `ResolveCache` deduplicates in-flight resolve tasks so that if two sources request the same track simultaneously, only one yt-dlp call is made. TTL is configurable; expired entries are evicted lazily.

---

