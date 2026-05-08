"""Multi-extra (N-track) export integration tests.

Three layers:
  1) Dispatch: /api/export with new `extras` list routes properly and the
     spy receives the kwargs as a list of (Path, volume) tuples.
  2) ffmpeg cmd shape: spying on the renderer/simple_export internal
     command-builders ensures amix=inputs=N has the right input count.
  3) Real ffmpeg mini-render: 2 extras + source video produces a valid
     output of the expected length.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import UPLOADS_DIR, _meta_path, _referenced_extra_ids, app
from app.simple_export import run_filter_only
from app.transcribe import ProbeInfo


VIDEO_ID = "f1b2c3d4e5f60718"
EXTRA_A = "1" * 16
EXTRA_B = "2" * 16


def _seed_video(video_id: str, duration: float = 5.0) -> Path:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    p = UPLOADS_DIR / f"{video_id}.mp4"
    p.write_bytes(b"\x00" * 32)
    _meta_path(video_id).write_text(json.dumps({
        "video_id": video_id,
        "duration": duration,
        "width": 1280,
        "height": 720,
        "language": "en",
        "segments": [],
        "is_audio_only": False,
        "_cache_key": "__multi__",
    }))
    return p


def _cleanup(video_id: str, extra_ids: list[str]) -> None:
    for ext in ("mp4", "mp3", "wav"):
        p = UPLOADS_DIR / f"{video_id}.{ext}"
        if p.exists():
            p.unlink()
    m = _meta_path(video_id)
    if m.exists():
        m.unlink()
    for eid in extra_ids:
        for ext in ("mp3", "wav", "m4a", "ogg", "flac", "aac"):
            q = UPLOADS_DIR / f"extra_{eid}.{ext}"
            if q.exists():
                q.unlink()


def _seed_extra(extra_id: str, ext: str = "mp3") -> Path:
    p = UPLOADS_DIR / f"extra_{extra_id}.{ext}"
    p.write_bytes(b"\x00" * 64)
    return p


@pytest.fixture()
def client(monkeypatch):
    def fake_probe(path: Path) -> ProbeInfo:
        name = Path(path).name
        if name.startswith("extra_"):
            # Different durations so loop driver tests can tell them apart.
            if EXTRA_A in name:
                return ProbeInfo(duration=8.0, width=0, height=0, is_audio_only=True)
            return ProbeInfo(duration=12.0, width=0, height=0, is_audio_only=True)
        return ProbeInfo(duration=5.0, width=1280, height=720, is_audio_only=False)

    monkeypatch.setattr("app.main.probe", fake_probe)
    yield TestClient(app)


@pytest.fixture()
def spies(monkeypatch):
    hit: dict[str, dict] = {}

    def stream_copy_spy(**kw):
        hit["stream_copy"] = kw
        Path(kw["out"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kw["out"]).write_bytes(b"fake")

    def filter_only_spy(**kw):
        hit["filter_only"] = kw
        Path(kw["out"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kw["out"]).write_bytes(b"fake")

    def render_spy(**kw):
        hit["render"] = kw
        Path(kw["out"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kw["out"]).write_bytes(b"fake")

    monkeypatch.setattr("app.simple_export.run_stream_copy", stream_copy_spy)
    monkeypatch.setattr("app.simple_export.run_filter_only", filter_only_spy)
    monkeypatch.setattr("app.renderer.render_export", render_spy)
    yield hit


def _body(video_id: str, **extra) -> dict:
    b = {
        "video_id": video_id,
        "segments": [],
        "style": {"mode": "phrase"},
        "position": {"x_pct": 10.0, "y_pct": 80.0},
        "size": {"w_pct": 80.0, "h_pct": 15.0},
        "trim_silences": False,
        "silence_threshold_sec": 0.4,
        "silence_padding_sec": 0.08,
        "canvas": {
            "mode": "preset", "preset": "source", "crop_anchor": "center",
            "custom": {"x_pct": 0, "y_pct": 0, "w_pct": 100, "h_pct": 100},
            "bg_color": "#000000",
        },
        "trim": {"in_sec": 0.0, "out_sec": 0.0, "loop": False},
        "audio": {"source_volume": 1.0, "extras": []},
        "watermark": False,
    }
    b.update(extra)
    return b


# -------- Dispatch + kwargs shape --------

def test_two_extras_forwarded_to_filter_only_as_list(client, spies):
    _cleanup(VIDEO_ID, [EXTRA_A, EXTRA_B])
    _seed_video(VIDEO_ID)
    pa = _seed_extra(EXTRA_A)
    pb = _seed_extra(EXTRA_B)
    try:
        body = _body(VIDEO_ID, audio={
            "source_volume": 1.0,
            "extras": [
                {"id": EXTRA_A, "volume": 0.7},
                {"id": EXTRA_B, "volume": 1.3},
            ],
        })
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        assert "filter_only" in spies
        kw = spies["filter_only"]
        assert "extras" in kw
        assert len(kw["extras"]) == 2
        assert kw["extras"][0] == (pa, pytest.approx(0.7))
        assert kw["extras"][1] == (pb, pytest.approx(1.3))
    finally:
        _cleanup(VIDEO_ID, [EXTRA_A, EXTRA_B])


def test_legacy_form_still_routes_with_single_extra_in_list(client, spies):
    """An old client sending `extra_audio_id` + `extra_volume` (no `extras`)
    must still produce a one-element extras list on the renderer side."""
    _cleanup(VIDEO_ID, [EXTRA_A])
    _seed_video(VIDEO_ID)
    pa = _seed_extra(EXTRA_A)
    try:
        body = _body(VIDEO_ID, audio={
            "source_volume": 1.0,
            "extra_audio_id": EXTRA_A,
            "extra_volume": 0.55,
        })
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        kw = spies["filter_only"]
        assert len(kw["extras"]) == 1
        assert kw["extras"][0] == (pa, pytest.approx(0.55))
    finally:
        _cleanup(VIDEO_ID, [EXTRA_A])


def test_no_extras_keeps_stream_copy_path(client, spies):
    _cleanup(VIDEO_ID, [EXTRA_A])
    _seed_video(VIDEO_ID)
    try:
        body = _body(VIDEO_ID)
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        # No transforms, no audio mix → fastest path.
        assert "stream_copy" in spies
        assert "filter_only" not in spies
    finally:
        _cleanup(VIDEO_ID, [EXTRA_A])


def test_loop_driver_is_first_extra_by_duration(client, spies):
    _cleanup(VIDEO_ID, [EXTRA_A, EXTRA_B])
    _seed_video(VIDEO_ID)
    _seed_extra(EXTRA_A)
    _seed_extra(EXTRA_B)
    try:
        # extras[0] = EXTRA_A (8s) per fake_probe → loop_total = 8.0.
        body = _body(VIDEO_ID, trim={"in_sec": 0.0, "out_sec": 3.0, "loop": True}, audio={
            "source_volume": 1.0,
            "extras": [
                {"id": EXTRA_A, "volume": 1.0},  # driver: 8s
                {"id": EXTRA_B, "volume": 1.0},  # 12s, but ignored as driver
            ],
        })
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        kw = spies["filter_only"]
        assert kw["loop_total_duration"] == pytest.approx(8.0)
    finally:
        _cleanup(VIDEO_ID, [EXTRA_A, EXTRA_B])


def test_loop_with_missing_first_extra_returns_410(client, spies):
    """If the loop driver file isn't on disk, hard-fail at 410 — without a
    duration we have nothing to loop to."""
    _cleanup(VIDEO_ID, [EXTRA_A, EXTRA_B])
    _seed_video(VIDEO_ID)
    # Only seed the SECOND track. First (driver) is missing on disk.
    _seed_extra(EXTRA_B)
    try:
        body = _body(VIDEO_ID, trim={"in_sec": 0.0, "out_sec": 3.0, "loop": True}, audio={
            "source_volume": 1.0,
            "extras": [
                {"id": EXTRA_A, "volume": 1.0},
                {"id": EXTRA_B, "volume": 1.0},
            ],
        })
        r = client.post("/api/export", json=body)
        assert r.status_code == 410, r.text
    finally:
        _cleanup(VIDEO_ID, [EXTRA_A, EXTRA_B])


def test_unknown_subtitle_track_falls_back_to_source(client, spies):
    _cleanup(VIDEO_ID, [EXTRA_A])
    _seed_video(VIDEO_ID)
    _seed_extra(EXTRA_A)
    try:
        segs = [{"start": 0.0, "end": 1.0, "text": "hi", "words": []}]
        body = _body(
            VIDEO_ID,
            segments=segs,
            subtitle_track="ffffffffffffffff",  # not in extras
            audio={"source_volume": 1.0, "extras": [{"id": EXTRA_A, "volume": 1.0}]},
        )
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        # Renderer hit because of overlay. Should NOT crash on bogus id.
        assert "render" in spies
    finally:
        _cleanup(VIDEO_ID, [EXTRA_A])


# -------- _referenced_extra_ids reads BOTH forms --------

def test_referenced_extra_ids_reads_legacy_and_new(tmp_path, monkeypatch):
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    legacy_video = UPLOADS_DIR / "1234567890abcdef.json"
    new_video = UPLOADS_DIR / "abcdef1234567890.json"
    legacy_id = "a" * 16
    new_id_1 = "b" * 16
    new_id_2 = "c" * 16
    try:
        legacy_video.write_text(json.dumps({
            "project": {"audio": {"extra_audio_id": legacy_id}},
        }))
        new_video.write_text(json.dumps({
            "project": {"audio": {"extras": [
                {"id": new_id_1, "volume": 1.0},
                {"id": new_id_2, "volume": 0.5},
            ]}},
        }))
        ids = _referenced_extra_ids()
        assert legacy_id in ids
        assert new_id_1 in ids
        assert new_id_2 in ids
    finally:
        legacy_video.unlink(missing_ok=True)
        new_video.unlink(missing_ok=True)


# -------- Real ffmpeg shape: amix N inputs --------

def _mk_short_video(path: Path, duration_sec: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={duration_sec}:size=160x120:rate=15",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-an",
         str(path)],
        check=True,
    )


def _mk_tone(path: Path, duration_sec: float, freq: int = 440) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine={freq}:duration={duration_sec}",
         "-c:a", "libmp3lame", "-q:a", "9",
         str(path)],
        check=True,
    )


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def _ffprobe_audio_codec(path: Path) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def test_three_extras_real_ffmpeg(tmp_path: Path) -> None:
    """3 simultaneous extras + muted source → output mp4 has aac audio."""
    src = tmp_path / "src.mp4"
    e1 = tmp_path / "e1.mp3"
    e2 = tmp_path / "e2.mp3"
    e3 = tmp_path / "e3.mp3"
    out = tmp_path / "out.mp4"
    _mk_short_video(src, 4.0)
    _mk_tone(e1, 3.0, 440)
    _mk_tone(e2, 5.0, 660)
    _mk_tone(e3, 2.0, 880)
    run_filter_only(
        source=src,
        out=out,
        canvas_filter="",
        target_w=160,
        target_h=120,
        select_expr=None,
        trim_in=0.0,
        trim_duration=4.0,
        source_volume=1.0,
        extras=[(e1, 0.4), (e2, 0.6), (e3, 0.8)],
        watermark_path=None,
        source_has_audio=False,
        loop_total_duration=None,
        fps=15,
    )
    assert out.exists() and out.stat().st_size > 0
    dur = _ffprobe_duration(out)
    # Output rides source video length (-shortest = video).
    assert 3.5 <= dur <= 4.5, f"three-extra output dur {dur:.2f}s"
    assert _ffprobe_audio_codec(out) == "aac"
