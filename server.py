"""
99talks — live stream transcription server (CPU, local).

Pipeline:
  yt-dlp -> ffmpeg (16kHz mono PCM) -> faster-whisper -> WebSocket broadcast

Robustness:
  - auto-reconnect with backoff if the stream drops
  - every segment persisted to JSONL as it arrives (survives restart)
  - Markdown/TXT/SRT/VTT export of any session, with real stream timestamps
  - surfaces real yt-dlp/ffmpeg errors (stderr captured)
  - small overlap between chunks so words aren't cut at boundaries

Usage:
  python server.py
  Then open http://localhost:8000 and paste a live stream (or video) URL.
"""

import asyncio
import json
import os
import warnings

# Silence harmless numpy matmul warnings from whisper's mel filter on silent chunks.
warnings.filterwarnings("ignore", category=RuntimeWarning, module="faster_whisper")
import shutil
import signal
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response
from faster_whisper import WhisperModel

from exporters import (
    SESSION_RE, clean_text, dedupe_overlap, export_filename, load_session, session_summary,
    to_md, to_srt, to_txt, to_vtt,
)

# ---------- Config ----------
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")  # tiny|base|small|medium|large-v3
DEFAULT_LANGUAGE = os.environ.get("WHISPER_LANG", "auto")  # ISO code (en, pt, es...) or "auto"
SAMPLE_RATE = 16000
CHUNK_SECONDS = int(os.environ.get("CHUNK_SECONDS", "5"))
OVERLAP_SECONDS = float(os.environ.get("OVERLAP_SECONDS", "0.5"))  # carry tail to avoid cutting words
BYTES_PER_SAMPLE = 2  # s16le
CHUNK_BYTES = SAMPLE_RATE * CHUNK_SECONDS * BYTES_PER_SAMPLE
OVERLAP_BYTES = int(SAMPLE_RATE * OVERLAP_SECONDS) * BYTES_PER_SAMPLE
LANG_LOCK_PROBABILITY = 0.7  # "auto": lock the language once detection is this confident

MAX_RECONNECTS = int(os.environ.get("MAX_RECONNECTS", "10"))
HOST = os.environ.get("HOST", "127.0.0.1")  # set HOST=0.0.0.0 to expose on your network (no auth!)
PORT = int(os.environ.get("PORT", "8000"))
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("TRANSCRIPTS_DIR", BASE_DIR / "transcripts"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

EXPORTS = {
    "md": ("text/markdown; charset=utf-8", lambda meta, segs: to_md(meta, segs)),
    "txt": ("text/plain; charset=utf-8", lambda meta, segs: to_txt(segs)),
    "srt": ("application/x-subrip", lambda meta, segs: to_srt(segs)),
    "vtt": ("text/vtt", lambda meta, segs: to_vtt(segs)),
}


# ---------- State ----------
class State:
    def __init__(self):
        self.model: WhisperModel | None = None
        self.clients: set[WebSocket] = set()
        self.capture_task: asyncio.Task | None = None
        self.current_url: str | None = None
        self.language: str | None = None   # None = auto-detect (until locked)
        self.recent: list[dict] = []        # recent segments, replayed to new WebSocket clients
        self.last_text = ""                 # previous chunk's text, to drop words repeated by the overlap
        self.stop_requested = False         # distinguishes manual stop from stream drop
        self.audio_seconds = 0.0            # absolute position in the audio stream
        self.is_live: bool | None = None    # from metadata; False = recorded video, don't reconnect at the end
        self.session_file: Path | None = None

state = State()


# ---------- Model load ----------
def load_model():
    print(f"[init] loading faster-whisper '{MODEL_SIZE}' (CPU int8)...")
    t0 = time.time()
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    print(f"[init] model loaded in {time.time() - t0:.1f}s")
    return model


# ---------- Broadcast ----------
async def broadcast(payload: dict):
    dead = []
    for ws in state.clients:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.clients.discard(ws)


async def status(message: str, phase: str):
    """phase: capturing | reconnecting | ended (drives the live indicator in the UI)."""
    await broadcast({"type": "status", "message": message, "phase": phase})


# ---------- Persistence ----------
def new_session_file() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = DATA_DIR / f"session_{stamp}.jsonl"
    n = 1
    while path.exists():  # two starts within the same second
        n += 1
        path = DATA_DIR / f"session_{stamp}_{n}.jsonl"
    return path


def persist(entry: dict, session_file: Path | None = None):
    target = session_file or state.session_file
    if not target:
        return
    try:
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[persist] error: {e}")


async def fetch_metadata(url: str, session_file: Path):
    """Best effort: record title/channel/description so exports carry context."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--dump-single-json", "--skip-download", "--no-warnings", "--no-playlist", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        info = json.loads(out)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[meta] could not fetch metadata: {e}")
        return
    if session_file == state.session_file:
        state.is_live = info.get("live_status") == "is_live" or bool(info.get("is_live"))
    meta = {
        "type": "meta",
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "description": (info.get("description") or "")[:2000] or None,
        "webpage_url": info.get("webpage_url"),
    }
    persist({k: v for k, v in meta.items() if v}, session_file)
    print(f"[meta] {meta['title']!r} by {meta['channel']!r}")
    await broadcast({"type": "sessions_changed"})


# ---------- Capture + transcribe ----------
async def _drain_stderr(proc, name: str, sink: list[str]):
    """Read a process's stderr so it doesn't block, keep last lines for diagnostics."""
    if not proc.stderr:
        return
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip()
        if text:
            sink.append(text)
            if len(sink) > 20:
                del sink[0]
            print(f"[{name}] {text}")


async def _run_pipeline_once(stream_url: str) -> bool:
    """One capture attempt. Returns True if it produced audio (healthy run)."""
    ytdlp_cmd = [
        "yt-dlp",
        # YouTube lives often expose only combined A/V formats (no audio-only),
        # so fall back to the lowest-bandwidth stream; ffmpeg drops video (-vn).
        "-f", "bestaudio/worst",
        "-o", "-",
        "--quiet", "--no-warnings", "--no-part",
        "--hls-use-mpegts",
        stream_url,
    ]
    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le",
        "pipe:1",
    ]

    # Connect yt-dlp -> ffmpeg with a real OS pipe. asyncio's StreamReader
    # (proc.stdout) is NOT a file descriptor and cannot be passed as another
    # process's stdin, so we wire raw fds and let the parent close its copies.
    read_fd, write_fd = os.pipe()
    ytdlp = await asyncio.create_subprocess_exec(
        *ytdlp_cmd,
        stdout=write_fd,
        stderr=asyncio.subprocess.PIPE,
    )
    os.close(write_fd)  # parent's copy; yt-dlp keeps its own
    ffmpeg = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd,
        stdin=read_fd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    os.close(read_fd)  # parent's copy; ffmpeg keeps its own (sees EOF when yt-dlp ends)

    yt_err: list[str] = []
    ff_err: list[str] = []
    err_tasks = [
        asyncio.create_task(_drain_stderr(ytdlp, "yt-dlp", yt_err)),
        asyncio.create_task(_drain_stderr(ffmpeg, "ffmpeg", ff_err)),
    ]

    produced = False
    carry = b""  # overlap tail from previous chunk
    try:
        buf = bytearray()
        while True:
            data = await ffmpeg.stdout.read(4096)
            if not data:
                break
            buf.extend(data)
            while len(buf) >= CHUNK_BYTES:
                chunk = bytes(buf[:CHUNK_BYTES])
                del buf[:CHUNK_BYTES]
                produced = True
                await transcribe_chunk(carry + chunk, has_overlap=bool(carry))
                carry = chunk[-OVERLAP_BYTES:] if OVERLAP_BYTES else b""
        if len(buf) >= SAMPLE_RATE * BYTES_PER_SAMPLE:  # stream ended: flush the last partial chunk (>=1s)
            produced = True
            await transcribe_chunk(carry + bytes(buf), has_overlap=bool(carry))
    except asyncio.CancelledError:
        raise
    finally:
        for p in (ffmpeg, ytdlp):
            if p.returncode is None:
                try:
                    p.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        await asyncio.gather(ffmpeg.wait(), ytdlp.wait(), *err_tasks, return_exceptions=True)

    if not produced:
        msg = (yt_err[-1] if yt_err else (ff_err[-1] if ff_err else "no audio (is the stream offline?)"))
        await broadcast({"type": "error", "message": msg})
    return produced


async def capture_loop(stream_url: str):
    """Run the pipeline, auto-reconnecting on drops until stopped or too many failures."""
    for tool in ("yt-dlp", "ffmpeg"):
        if not shutil.which(tool):
            await broadcast({"type": "error", "message": f"{tool} is not installed"})
            await status("Capture ended.", "ended")
            return

    fails = 0
    attempt = 0
    try:
        while not state.stop_requested:
            attempt += 1
            label = f"Capturing: {stream_url}" if attempt == 1 else f"Reconnected (attempt {attempt})"
            print(f"[capture] attempt {attempt}")
            await status(label, "capturing")
            healthy = await _run_pipeline_once(stream_url)

            if state.stop_requested:
                break

            if healthy and state.is_live is False:
                await status("Video finished.", "ended")
                break

            if healthy:
                fails = 0  # a productive run resets the failure counter
            else:
                fails += 1
                if fails >= MAX_RECONNECTS:
                    await broadcast({"type": "error",
                                     "message": f"Gave up after {fails} failures in a row. "
                                                "The stream ended or the URL is wrong."})
                    break

            backoff = min(2 ** min(fails, 5), 30) if fails else 3
            await status(f"Stream dropped. Reconnecting in {backoff}s...", "reconnecting")
            await asyncio.sleep(backoff)
    except asyncio.CancelledError:
        print("[capture] cancelled")
        raise
    finally:
        print("[capture] loop ended")
        await status("Capture ended.", "ended")
        await broadcast({"type": "sessions_changed"})


async def transcribe_chunk(pcm_bytes: bytes, has_overlap: bool):
    """Run whisper on a chunk in a thread, broadcast + persist segments."""
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    overlap = OVERLAP_SECONDS if has_overlap else 0.0
    # Whisper timestamps are relative to the chunk start, which includes the carried overlap.
    base_offset = state.audio_seconds - overlap
    state.audio_seconds += len(pcm_bytes) / BYTES_PER_SAMPLE / SAMPLE_RATE - overlap
    language = state.language

    def run():
        segments, info = state.model.transcribe(
            audio,
            language=language,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            beam_size=1,
            condition_on_previous_text=False,
        )
        out = [(s.start, s.end, clean_text(s.text)) for s in segments]
        out = [seg for seg in out if seg[2].strip(". …")]  # drop empty / dots-only segments
        return out, info

    t0 = time.time()
    try:
        segs, info = await asyncio.to_thread(run)
    except Exception as e:
        print(f"[transcribe] error: {e}")
        return
    dt = time.time() - t0

    if not segs:
        state.last_text = ""
        return

    if state.language is None and info.language_probability >= LANG_LOCK_PROBABILITY:
        # Per-chunk detection flip-flops on short audio; lock in the first confident guess.
        state.language = info.language
        persist({"type": "meta", "language": info.language})
        print(f"[lang] detected '{info.language}' ({info.language_probability:.2f}), locking")

    text = " ".join(t for _, _, t in segs)
    if has_overlap and state.last_text:
        text = dedupe_overlap(state.last_text, text)
    state.last_text = text
    entry = {
        "type": "segment",
        "ts": time.strftime("%H:%M:%S"),
        "text": text,
        "start": round(max(base_offset + segs[0][0], 0), 2),
        "end": round(base_offset + segs[-1][1], 2),
        "latency": round(dt, 2),
    }
    state.recent.append(entry)
    if len(state.recent) > 300:
        del state.recent[0]
    persist(entry)
    print(f"[{entry['ts']}] ({dt:.1f}s) {entry['text']}")
    await broadcast(entry)


# ---------- FastAPI app ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    state.model = load_model()
    yield
    state.stop_requested = True
    if state.capture_task:
        state.capture_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")


async def _cancel_capture():
    state.stop_requested = True
    if state.capture_task and not state.capture_task.done():
        state.capture_task.cancel()
        try:
            await state.capture_task
        except asyncio.CancelledError:
            pass


@app.post("/start")
async def start(payload: dict):
    url = (payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "enter a full http(s) URL"}
    language = (payload.get("language") or DEFAULT_LANGUAGE).strip().lower()
    await _cancel_capture()
    state.stop_requested = False
    state.current_url = url
    state.language = None if language == "auto" else language
    state.recent = []
    state.last_text = ""
    state.is_live = None
    state.audio_seconds = 0.0
    state.session_file = new_session_file()
    persist({
        "type": "meta",
        "url": url,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "language": state.language,
        "model": MODEL_SIZE,
    })
    asyncio.create_task(fetch_metadata(url, state.session_file))
    state.capture_task = asyncio.create_task(capture_loop(url))
    return {"ok": True, "session": state.session_file.stem}


@app.post("/stop")
async def stop():
    await _cancel_capture()
    return {"ok": True}


def _session_path(session: str | None) -> Path | None:
    """Resolve a session id to its file. Defaults to the current, else the most recent session."""
    if session is None:
        if state.session_file and state.session_file.exists():
            return state.session_file
        files = sorted(DATA_DIR.glob("session_*.jsonl"))
        return files[-1] if files else None
    if not SESSION_RE.match(session):
        return None
    path = DATA_DIR / f"{session}.jsonl"
    return path if path.exists() else None


@app.get("/sessions")
async def sessions():
    files = sorted(DATA_DIR.glob("session_*.jsonl"), reverse=True)
    current = state.session_file.stem if state.session_file and state.capture_task \
        and not state.capture_task.done() else None
    out = []
    for f in files:
        summary = session_summary(f)
        summary["live"] = summary["id"] == current
        if summary["segments"] or summary["live"]:  # hide failed attempts that never produced text
            out.append(summary)
    return out


@app.get("/export")
async def export(fmt: str = "md", session: str | None = None):
    fmt = fmt.lower()
    if fmt not in EXPORTS:
        return PlainTextResponse(f"unknown format; use one of: {', '.join(EXPORTS)}", status_code=400)
    path = _session_path(session)
    if not path:
        return PlainTextResponse("session not found", status_code=404)
    meta, segments = load_session(path)
    if not segments:
        return PlainTextResponse("(empty transcript)", status_code=404)
    media_type, render = EXPORTS[fmt]
    filename = export_filename(path.stem, meta, fmt)
    return Response(render(meta, segments), media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    state.clients.add(ws)
    for entry in state.recent:
        await ws.send_text(json.dumps(entry))
    try:
        while True:
            await ws.receive_text()  # keep-alive; no client input expected
    except WebSocketDisconnect:
        pass
    finally:
        state.clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
