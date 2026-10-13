import json

from exporters import (
    SESSION_RE, clean_text, dedupe_overlap, export_filename, fmt_ts, load_session, session_summary,
    slugify, to_md, to_srt, to_txt, to_vtt,
)

SEGMENTS = [
    {"type": "segment", "ts": "22:00:05", "text": "Hello everyone.", "start": 0.4, "end": 4.9},
    {"type": "segment", "ts": "22:00:10", "text": "Welcome to the live.", "start": 5.2, "end": 9.8},
    {"type": "segment", "ts": "22:01:12", "text": "Next topic.", "start": 71.0, "end": 74.5},
]


def write_session(tmp_path, lines, name="session_20261012_220000.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_fmt_ts():
    assert fmt_ts(0) == "00:00:00"
    assert fmt_ts(3725.5) == "01:02:05"
    assert fmt_ts(3725.5, ",") == "01:02:05,500"
    assert fmt_ts(-1) == "00:00:00"


def test_load_session_merges_meta_and_skips_bad_lines(tmp_path):
    path = write_session(tmp_path, [
        json.dumps({"type": "meta", "url": "https://x.test/live", "language": None}),
        json.dumps(SEGMENTS[0]),
        '{"type": "segment", "text": "half-writ',  # crash mid-write
        json.dumps({"type": "meta", "title": "My Live", "language": "pt"}),
        json.dumps(SEGMENTS[1]),
    ])
    meta, segs = load_session(path)
    assert meta == {"url": "https://x.test/live", "title": "My Live", "language": "pt"}
    assert [s["text"] for s in segs] == ["Hello everyone.", "Welcome to the live."]


def test_load_session_without_meta_is_backward_compatible(tmp_path):
    path = write_session(tmp_path, [json.dumps(s) for s in SEGMENTS])
    meta, segs = load_session(path)
    assert meta == {}
    assert len(segs) == 3
    summary = session_summary(path)
    assert summary["started_at"] == "2026-10-12T22:00:00"
    assert summary["segments"] == 3
    assert summary["duration"] == 74.5


def test_txt_uses_stream_offset():
    assert to_txt(SEGMENTS).splitlines()[2] == "[00:01:11] Next topic."


def test_srt_and_vtt():
    srt = to_srt(SEGMENTS)
    assert srt.startswith("1\n00:00:00,400 --> 00:00:04,900\nHello everyone.\n")
    vtt = to_vtt(SEGMENTS)
    assert vtt.startswith("WEBVTT\n\n00:00:00.400 --> 00:00:04.900\nHello everyone.\n")


def test_md_header_and_paragraphs():
    meta = {"title": "My Live", "url": "https://x.test/live", "channel": "Chan",
            "started_at": "2026-10-12T22:00:00", "language": "en", "model": "small"}
    md = to_md(meta, SEGMENTS)
    assert md.startswith("# My Live\n")
    assert "- **Source:** https://x.test/live" in md
    assert "- **Recorded:** 2026-10-12 22:00" in md
    assert "- **Duration:** 00:01:14" in md
    assert "**[00:00:00]** Hello everyone. Welcome to the live." in md
    assert "**[00:01:11]** Next topic." in md


def test_md_without_meta():
    md = to_md({}, SEGMENTS)
    assert md.startswith("# Live transcript\n")
    assert "Source" not in md


def test_session_id_validation():
    assert SESSION_RE.match("session_20261012_220000")
    assert SESSION_RE.match("session_20261012_220000_2")
    assert not SESSION_RE.match("../etc/passwd")
    assert not SESSION_RE.match("session_20261012_220000/../../x")


def test_filenames():
    assert slugify("Live: Ação & Reação!") == "live-acao-reacao"
    assert export_filename("session_20261012_220000", {"title": "Live: Ação"}, "md") == "live-acao_20261012_220000.md"
    assert export_filename("session_20261012_220000", {}, "txt") == "transcript_20261012_220000.txt"


def test_clean_text_collapses_ellipsis_runs():
    assert clean_text("story of... ... ... ... ...") == "story of..."
    assert clean_text("wait… … …") == "wait..."
    assert clean_text("a single... is fine") == "a single... is fine"


def test_dedupe_overlap():
    assert dedupe_overlap("Florida where four", "four astronauts are") == "astronauts are"
    assert dedupe_overlap("a number of times.", "of times. This mission") == "This mission"
    assert dedupe_overlap("It's the story of...", "story of humanity.") == "humanity."
    assert dedupe_overlap("we went home", "and then left") == "and then left"
    assert dedupe_overlap("yes", "yes") == "yes"  # never empty a segment
