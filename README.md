# 99talks

**Turn live streams into text your AI tools can learn from.**

99talks transcribes a YouTube Live (or a recorded video) in real time, 100% locally, and saves everything as you go. When the talk is over, export it as Markdown and drop it into [NotebookLM](https://notebooklm.google.com), ChatGPT, Claude, or your own agents to generate study notes, summaries, content ideas, or a knowledge base.

- **Local and free**: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your CPU. No API keys, nothing leaves your machine.
- **Live**: text appears a few seconds behind the speaker, in the browser.
- **Never lose a talk**: every segment is written to disk as it's transcribed, and the stream auto-reconnects if it drops.
- **LLM-ready export**: Markdown with title, channel, URL, date and description, plus timestamped paragraphs. Also TXT, SRT, and VTT.
- **Any language** Whisper supports, with auto-detection.

## Requirements

- **Python 3.10 – 3.12** (`ctranslate2` has no wheels for 3.13+ yet)
- [`ffmpeg`](https://ffmpeg.org) and [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
  - macOS: `brew install ffmpeg yt-dlp`
  - Debian/Ubuntu: `sudo apt install ffmpeg && pip install yt-dlp`
- 4 GB+ of free RAM (Apple Silicon or x86_64)

## Quick start

```bash
git clone https://github.com/<you>/99talks.git
cd 99talks
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Open http://localhost:8000, paste a live URL, pick the spoken language, and hit **Start**.

The first run downloads the Whisper model (~460 MB for `small`) to `~/.cache/huggingface`.

## From live to NotebookLM

1. Start 99talks when the live begins (or after it ends, using the recording URL).
2. When it's over, click **Markdown** in the session list.
3. In NotebookLM, **Add source → Upload** the `.md` file.
4. Ask it for a study guide, FAQ, audio overview, or anything else.

The same file works as context for agents. It's plain Markdown with a metadata header:

```markdown
# Building Agents in Production

- **Source:** https://www.youtube.com/watch?v=...
- **Channel:** Some Channel
- **Recorded:** 2026-10-12 20:00
- **Duration:** 01:32:10
- **Language:** en
- **Transcribed with:** faster-whisper (small) via 99talks

## Transcript

**[00:00:00]** Welcome everyone, today we're going to talk about...

**[00:01:00]** The first thing you need is...
```

## Export API

Every session can be exported, including past ones:

```
GET /sessions                                  # list sessions (id, title, duration, ...)
GET /export?fmt=md                             # current (or most recent) session
GET /export?fmt=txt&session=session_20261012_200000
```

Formats: `md`, `txt` (`[HH:MM:SS] text`), `srt`, `vtt`. All timestamps are positions in the stream.

Raw data lives in `transcripts/session_<timestamp>.jsonl`, with one JSON object per line: `meta` lines (url, title, channel, language, ...) and `segment` lines (`text`, `start`, `end` in seconds). It's easy to process with your own scripts.

## Configuration

All settings are environment variables:

| Variable          | Default         | Description |
|-------------------|-----------------|-------------|
| `WHISPER_MODEL`   | `small`         | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `WHISPER_LANG`    | `auto`          | Default language when the UI doesn't send one (`en`, `pt`, `es`, ...) |
| `CHUNK_SECONDS`   | `5`             | Audio chunk size sent to Whisper |
| `OVERLAP_SECONDS` | `0.5`           | Audio carried between chunks so words aren't cut |
| `MAX_RECONNECTS`  | `10`            | Consecutive failed reconnects before giving up |
| `TRANSCRIPTS_DIR` | `./transcripts` | Where sessions are saved |
| `HOST` / `PORT`   | `127.0.0.1` / `8000` | Bind address. Only use `HOST=0.0.0.0` on trusted networks: there is no authentication |

Model trade-offs on an Apple Silicon CPU with 5 s chunks:

| Model  | Latency per chunk | Quality   | RAM   |
|--------|-------------------|-----------|-------|
| tiny   | ~0.5 s            | poor      | ~1 GB |
| base   | ~1 s              | fair      | ~1 GB |
| small  | ~2–3 s            | good      | ~2 GB |
| medium | ~5–8 s            | very good | ~5 GB |

`small` is the sweet spot for keeping up with a live stream in real time.

## How it works

```
stream URL ─▶ yt-dlp ─▶ ffmpeg (16 kHz mono PCM) ─▶ 5 s chunks ─▶ faster-whisper ─▶ WebSocket ─▶ browser
                                                                        └──────────▶ transcripts/*.jsonl
```

- `server.py`: FastAPI app, capture pipeline, reconnection, WebSocket broadcast.
- `exporters.py`: session file format and Markdown/TXT/SRT/VTT exporters (no heavy deps, unit-tested).
- `index.html`: the whole UI, vanilla HTML/JS.

Anything `yt-dlp` can stream should work (YouTube is the tested path). For recorded videos, capture stops when the video ends. For lives, it reconnects until you hit **Stop**.

## Limitations

- **Latency** is roughly chunk size + transcription time (~6–8 s on CPU).
- **No speaker diarization**: Whisper doesn't know who is speaking.
- **One capture at a time** per server.
- **Chunk-boundary repeats**: the 0.5 s overlap can occasionally duplicate a word.
- **YouTube changes** can break `yt-dlp`. Keep it updated (`pip install -U yt-dlp`).

## Roadmap

Ideas and contributions welcome:

- [ ] CLI mode (`99talks <url> -o talk.md`) for headless/agent use
- [ ] GPU backends (CUDA, Apple MLX)
- [ ] Multiple simultaneous captures
- [ ] Speaker diarization (`pyannote`)
- [ ] Docker image

## Contributing

Issues and PRs are welcome. Run the tests with:

```bash
pip install pytest
python -m pytest
```

Please keep the project dependency-light and local-first.

## License

[MIT](LICENSE) © Rafael Silva
