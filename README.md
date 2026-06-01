# Discord MusicBot

Custom prefix-based Discord music bot built from scratch for low-cost deployment. It uses classic text commands such as `!play`, supports a per-server custom prefix, and permanently keeps `!` available as a fallback even after the custom prefix changes.

## Features

- Prefix-first commands with hybrid command definitions for interactive contexts.
- Default `!` prefix with per-guild custom prefix support.
- YouTube playback via `yt-dlp` and FFmpeg.
- Audio-only extraction by default to avoid wasting CPU and bandwidth on unused video streams.
- Lightweight in-memory queue per guild.
- Flat playlist ingestion with lazy per-track resolution.
- Background prefetch of upcoming tracks.
- Near-end preload pass for the next track shortly before the current one ends.
- Small in-memory resolver cache to reduce repeated `yt-dlp` lookups.
- SQLite storage for guild settings, playlists, and queue snapshots.
- WAL-backed SQLite connections plus hot-path guild-setting caches.
- Commands tuned for small VPS deployments.
- Idle auto-disconnect to conserve RAM and bandwidth.
- Auto-leave when the bot is alone in voice for the configured timeout.

## Commands

- `!play <url or search>`: Queue a YouTube URL or open a paged selector for text-search results.
- `!join`: Join your voice channel.
- `!leave`: Disconnect and clear the queue.
- `!pause`: Pause playback.
- `!resume`: Resume playback.
- `!skip`: Skip the current track.
- `!prev`: Return to the previous track.
- `!stop`: Stop playback.
- `!queue`: Show current queue.
- `!nowplaying`: Show the current track with button controls and a next-song preview.
- `!remove <index>`: Remove a track from the queue.
- `!clear`: Clear queued tracks.
- `!shuffle`: Shuffle the queue.
- `!loop`: Toggle loop mode for the current track.
- `!playnext <url or search>`: Insert a track at the front of the queue, with a paged selector for text-search results.
- `!prefix <new_prefix>`: Set a guild-specific prefix.
- `!setdj @role`: Set a DJ role for protected commands.
- `!cleardj`: Clear the DJ role.
- `!dj`: Show the configured DJ role.
- `!forceskip`: DJ-only immediate skip.
- `!playlist save <name>`: Save the current queue.
- `!playlist load <name>`: Load a saved playlist.
- `!playlist list`: List saved playlists.
- `!playlist show <name>`: Show tracks in a saved playlist.
- `!playlist delete <name>`: Delete a saved playlist.
- `!ping`: Basic health check.
- `!help`: Show built-in help.

## Stack

- Python 3.11 recommended for deployment.
- `discord.py` for bot runtime and voice support.
- `yt-dlp` for media extraction.
- FFmpeg for audio transcoding/streaming.
- SQLite for small persistent settings.
- Rotating file logs for production deployments.

## Local setup

1. Create a Discord bot in the Discord developer portal.
2. Enable the `Message Content Intent`.
3. Copy `.env.example` to `.env`.
4. Fill in `DISCORD_TOKEN`.
5. Install FFmpeg and ensure `ffmpeg` is on `PATH`.
6. Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Start the bot:

```bash
python bot.py
```

## Oracle Always Free deployment notes

This design targets a small instance such as 1 OCPU / 1 GB RAM:

- Keep one bot process only.
- Use Python, not Node, to reduce baseline runtime overhead in this repo.
- Auto-disconnect after inactivity to avoid holding voice resources.
- Auto-leave after `EMPTY_CHANNEL_TIMEOUT_SECONDS` when no human listeners remain.
- Limit queue length and playlist expansion in `.env`.
- Do not cache audio files on disk.
- Use `tmux` or `systemd`, not Docker, unless you already need container tooling.
- Queue state persists in SQLite so a restart does not wipe the session.
- DJ-only controls reduce accidental queue destruction in busy servers.
- Playlist URLs are imported quickly, then resolved lazily to reduce `!play` latency on weak CPUs.
- Upcoming tracks are prefetched in the background to reduce gaps between songs.
- The bot refreshes the next-track preload near the end of playback and posts a fresh now-playing panel on track changes.
- Non-URL searches open a selector over the top ranked YouTube candidates before queueing.
- `!nowplaying` adds button controls for previous, skip/next, pause/resume, loop, and queue output.
- Playback prefers Opus output so Discord can skip extra encode work when possible.

Recommended Ubuntu setup:

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg logrotate
mkdir -p ~/apps/musicbot
cd ~/apps/musicbot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Or use the included bootstrap script after cloning the repo:

```bash
chmod +x deploy/setup_oracle.sh
./deploy/setup_oracle.sh
```

Example `systemd` unit:

```ini
[Unit]
Description=Discord MusicBot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/apps/musicbot
Environment="PATH=/home/ubuntu/apps/musicbot/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
EnvironmentFile=/home/ubuntu/apps/musicbot/.env
ExecStart=/home/ubuntu/apps/musicbot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5
MemoryMax=700M
OOMScoreAdjust=500
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable musicbot
sudo systemctl start musicbot
sudo systemctl status musicbot
```

## Important operational notes

- `!` always works even if a custom server prefix is set.
- `!prefix ?` makes both `?` and `!` valid for that server.
- Commands that modify server-wide playback state are DJ-protected.
- `!skip` supports vote-skip for non-DJ listeners.
- Saved playlists are server-scoped and stored in SQLite.
- Queue snapshots are restored after restart and re-resolved on next playback request.
- The bot leaves voice automatically if no human users remain for the configured timeout.
- FFmpeg must be installed on the server.
- If YouTube challenges your VPS IP, configure `YTDLP_COOKIES_FILE` with an exported Netscape-format cookies file. Prefer a repo-relative value such as `youtube-cookies.txt` so redeploys do not bake in old absolute paths.
- Set `YTDLP_JS_RUNTIME_PATH=/usr/bin/node` on Linux so `yt-dlp` can use Node for YouTube extraction.
- `YTDLP_PREFETCH_COUNT` controls how many upcoming tracks are resolved in the background.
- `YTDLP_CONCURRENT_EXTRACTS` controls how many `yt-dlp` lookups can run at once.
- `NEAR_END_PREFETCH_SECONDS` controls how close to the end of a track the bot refreshes the next-track preload.
- `OPUS_BITRATE_KBPS` controls the FFmpeg Opus encode bitrate when stream copy is not possible.
- `YTDLP_SEARCH_RESULTS` controls how many flat search candidates are ranked before one is picked.
- `YTDLP_RESOLVE_CACHE_*` settings reduce repeated extractor work for re-queued URLs.
- Voice playback depends on Discord voice support and outbound network access.
- Some media sources can change behavior over time. `yt-dlp` should be kept reasonably up to date.
- Playback stays at full output by design. Use Discord's per-user voice slider if you want the bot quieter in a channel.

## Limitations

- This bot streams audio and does not download songs permanently.
- Playlist ingestion is capped by `MAX_PLAYLIST_SIZE`.
- Search quality still depends on YouTube metadata, but text queries now expose a manual selector over the ranked results.

## File layout

```text
bot.py
musicbot/
  bot.py
  config.py
  database.py
  cogs/
    admin.py
    music.py
data/
requirements.txt
```
