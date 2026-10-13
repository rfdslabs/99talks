"""
Session storage format + transcript exporters. Pure Python, no heavy deps.

A session file is JSONL. Each line is one of:
  {"type": "meta", ...}     session metadata (url, title, channel, started_at, language, model).
                            May appear more than once; later lines update earlier ones.
  {"type": "segment", ...}  one transcribed chunk: text, start/end (seconds into the
                            stream), ts (wall-clock HH:MM:SS), latency.
"""

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

SESSION_RE = re.compile(r"^session_\d{8}_\d{6}(_\d+)?$")
PARAGRAPH_SECONDS = 60  # markdown: start a new timestamped paragraph every ~minute


_ELLIPSIS_RUN = re.compile(r"(?:\s*(?:\.{3}|…)){2,}")


def clean_text(text: str) -> str:
    """Collapse Whisper's '... ... ...' runs (common on music/silence) into one ellipsis."""
    return _ELLIPSIS_RUN.sub("...", text).strip()


def _norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def dedupe_overlap(prev: str, text: str, max_words: int = 4) -> str:
    """Drop words at the start of `text` that repeat the end of `prev`.

    Chunks overlap slightly so words aren't cut in half, which means Whisper often
    transcribes the boundary word(s) twice: "where four" + "four astronauts".
    """
    prev_words, words = prev.split(), text.split()
    for k in range(min(max_words, len(prev_words), len(words) - 1), 0, -1):
        tail = [_norm(w) for w in prev_words[-k:]]
        head = [_norm(w) for w in words[:k]]
        if tail == head and any(tail):
            return " ".join(words[k:])
    return text


# ---------- Loading ----------
def load_session(path: Path) -> tuple[dict, list[dict]]:
    """Return (meta, segments) from a session JSONL file. Skips corrupt lines."""
    meta: dict = {}
    segments: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # e.g. a half-written last line after a crash
            if entry.get("type") == "meta":
                meta.update({k: v for k, v in entry.items() if k != "type"})
            elif entry.get("type") == "segment" and entry.get("text"):
                segments.append(entry)
    return meta, segments


def session_summary(path: Path) -> dict:
    meta, segments = load_session(path)
    return {
        "id": path.stem,
        "title": meta.get("title"),
        "channel": meta.get("channel"),
        "url": meta.get("url"),
        "started_at": meta.get("started_at") or _started_at_from_name(path.stem),
        "language": meta.get("language"),
        "segments": len(segments),
        "duration": round(segments[-1].get("end", 0), 2) if segments else 0,
    }


def _started_at_from_name(stem: str) -> str | None:
    try:
        return datetime.strptime(stem[:23], "session_%Y%m%d_%H%M%S").isoformat()
    except ValueError:
        return None


# ---------- Formatting helpers ----------
def fmt_ts(seconds: float, sep: str | None = None) -> str:
    """HH:MM:SS, or HH:MM:SS<sep>mmm when sep is given (',' for SRT, '.' for VTT)."""
    ms = int(round(max(seconds, 0) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    base = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{base}{sep}{ms:03d}" if sep else base


def _start(e: dict) -> float:
    return e.get("start", 0)


def _end(e: dict) -> float:
    return e.get("end", _start(e) + 2)


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-")


def export_filename(session_id: str, meta: dict, ext: str) -> str:
    slug = slugify(meta.get("title") or "")
    stamp = session_id.removeprefix("session_")
    return f"{slug}_{stamp}.{ext}" if slug else f"transcript_{stamp}.{ext}"


# ---------- Exporters ----------
def to_srt(segments: list[dict]) -> str:
    lines = []
    for i, e in enumerate(segments, 1):
        lines.append(f"{i}\n{fmt_ts(_start(e), ',')} --> {fmt_ts(_end(e), ',')}\n{e['text']}\n")
    return "\n".join(lines)


def to_vtt(segments: list[dict]) -> str:
    body = ["WEBVTT", ""]
    for e in segments:
        body.append(f"{fmt_ts(_start(e), '.')} --> {fmt_ts(_end(e), '.')}\n{e['text']}\n")
    return "\n".join(body)


def to_txt(segments: list[dict]) -> str:
    return "\n".join(f"[{fmt_ts(_start(e))}] {e['text']}" for e in segments) + "\n"


def to_md(meta: dict, segments: list[dict]) -> str:
    """Readable document for LLM tools (NotebookLM, agents): metadata header + timestamped paragraphs."""
    title = meta.get("title") or "Live transcript"
    out = [f"# {title}", ""]

    details = [
        ("Source", meta.get("url")),
        ("Channel", meta.get("channel")),
        ("Recorded", _human_date(meta.get("started_at"))),
        ("Duration", fmt_ts(_end(segments[-1])) if segments else None),
        ("Language", meta.get("language")),
        ("Transcribed with", f"faster-whisper ({meta['model']}) via 99talks" if meta.get("model") else None),
    ]
    out += [f"- **{k}:** {v}" for k, v in details if v]
    if meta.get("description"):
        out += ["", "## Description", "", meta["description"].strip()]
    out += ["", "## Transcript", ""]

    para: list[str] = []
    para_start = None
    for e in segments:
        if para_start is None or _start(e) - para_start >= PARAGRAPH_SECONDS:
            if para:
                out += [f"**[{fmt_ts(para_start)}]** " + " ".join(para), ""]
            para, para_start = [], _start(e)
        para.append(e["text"])
    if para:
        out += [f"**[{fmt_ts(para_start)}]** " + " ".join(para), ""]
    return "\n".join(out)


def _human_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso
