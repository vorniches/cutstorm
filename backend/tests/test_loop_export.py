"""Loop-mode export integration tests.

Two layers:
  1) Spy-based dispatch: confirm /api/export with trim.loop=true + extra
     audio routes through filter_only with loop_total_duration set.
  2) Real ffmpeg mini-render: feed a 2s sample video + 6s tone, assert the
     output mp4 is ~6s and decodable.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import UPLOADS_DIR, _meta_path, app
from app.simple_export import run_filter_only
from app.transcribe import ProbeInfo


VIDEO_ID = "d1b2c3d4e5f60718"
EXTRA_ID = "e1b2c3d4e5f60718"


# -------- Spy: dispatch routing --------

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
        "_cache_key": "__loop__",
    }))
    return p


def _cleanup_video(video_id: str) -> None:
    for ext in ("mp4", "mp3"):
        p = UPLOADS_DIR / f"{video_id}.{ext}"
        if p.exists():
            p.unlink()
    m = _meta_path(video_id)
    if m.exists():
        m.unlink()


@pytest.fixture()
def client(monkeypatch):
    def fake_probe(path: Path) -> ProbeInfo:
        name = Path(path).name
        if name.startswith("extra_"):
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
        "audio": {"source_volume": 1.0, "extra_audio_id": None, "extra_volume": 1.0},
        "watermark": False,
    }
    b.update(extra)
    return b


def test_loop_with_extra_routes_to_filter_only_with_total_duration(client, spies):
    _cleanup_video(VIDEO_ID)
    _seed_video(VIDEO_ID, duration=5.0)
    # Seed an extra audio file alongside.
    extra_path = UPLOADS_DIR / f"extra_{EXTRA_ID}.mp3"
    extra_path.write_bytes(b"\x00" * 64)
    try:
        body = _body(
            VIDEO_ID,
            trim={"in_sec": 0.0, "out_sec": 3.0, "loop": True},
            audio={"source_volume": 1.0, "extra_audio_id": EXTRA_ID, "extra_volume": 1.0},
        )
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        assert "filter_only" in spies
        kw = spies["filter_only"]
        assert kw["loop_total_duration"] == pytest.approx(12.0)
        # trim_duration carries the SHORT clip (one iteration), not the total.
        assert kw["trim_duration"] == pytest.approx(3.0)
        assert len(kw["extras"]) == 1
        assert kw["extras"][0][0] == extra_path
    finally:
        extra_path.unlink(missing_ok=True)
        _cleanup_video(VIDEO_ID)


def test_loop_without_extra_audio_is_noop(client, spies):
    """trim.loop=true without extras must not engage the loop filter chain.
    The flag stays a flag — it needs at least one extra track to mean
    anything."""
    _cleanup_video(VIDEO_ID)
    _seed_video(VIDEO_ID, duration=5.0)
    try:
        body = _body(VIDEO_ID, trim={"in_sec": 0.0, "out_sec": 3.0, "loop": True})
        r = client.post("/api/export", json=body)
        # Backend hard-fails when loop is requested but the driver track
        # doesn't exist. That's a 410 — the user has to re-upload or turn
        # loop off. Accept either 410 (current contract) or 200 with
        # loop_total_duration=None (alternate "soft fall-through" if a
        # future relaxation lands).
        if r.status_code == 410:
            return
        assert r.status_code == 200, r.text
        assert "filter_only" in spies
        kw = spies["filter_only"]
        assert kw.get("loop_total_duration") in (None, 0)
    finally:
        _cleanup_video(VIDEO_ID)


def test_loop_with_overlay_routes_to_renderer(client, spies):
    _cleanup_video(VIDEO_ID)
    _seed_video(VIDEO_ID, duration=5.0)
    extra_path = UPLOADS_DIR / f"extra_{EXTRA_ID}.mp3"
    extra_path.write_bytes(b"\x00" * 64)
    try:
        segs = [{"start": 0.0, "end": 1.0, "text": "hi", "words": []}]
        body = _body(
            VIDEO_ID,
            segments=segs,
            trim={"in_sec": 0.0, "out_sec": 3.0, "loop": True},
            audio={"source_volume": 1.0, "extra_audio_id": EXTRA_ID, "extra_volume": 1.0},
        )
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        assert "render" in spies
        kw = spies["render"]
        assert kw["loop_total_duration"] == pytest.approx(12.0)
        # In source-track mode the segments are expanded to cover total_dur:
        # 12s / 3s = 4 iterations of 1 segment = 4 segments.
        assert len(kw["segments"]) == 4
    finally:
        extra_path.unlink(missing_ok=True)
        _cleanup_video(VIDEO_ID)


def test_loop_with_subtitle_track_extra_does_not_expand(client, spies):
    _cleanup_video(VIDEO_ID)
    _seed_video(VIDEO_ID, duration=5.0)
    extra_path = UPLOADS_DIR / f"extra_{EXTRA_ID}.mp3"
    extra_path.write_bytes(b"\x00" * 64)
    try:
        # Caller declares the segments are from extra audio (their timings
        # already live on the extra-audio timeline). Do NOT re-stamp.
        segs = [
            {"start": 1.0, "end": 2.0, "text": "a", "words": []},
            {"start": 9.0, "end": 10.0, "text": "b", "words": []},
        ]
        body = _body(
            VIDEO_ID,
            segments=segs,
            subtitle_track=EXTRA_ID,
            trim={"in_sec": 0.0, "out_sec": 3.0, "loop": True},
            audio={"source_volume": 1.0, "extra_audio_id": EXTRA_ID, "extra_volume": 1.0},
        )
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        assert "render" in spies
        kw = spies["render"]
        # Two segments in, two segments out — no expansion.
        assert len(kw["segments"]) == 2
        assert kw["segments"][0].text == "a"
        assert kw["segments"][1].text == "b"
    finally:
        extra_path.unlink(missing_ok=True)
        _cleanup_video(VIDEO_ID)


# -------- Real ffmpeg mini-render --------

def _mk_short_video(path: Path, duration_sec: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={duration_sec}:size=160x120:rate=15",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-an",
         str(path)],
        check=True,
    )


def _mk_tone(path: Path, duration_sec: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=440:duration={duration_sec}",
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


def test_run_filter_only_loop_real_ffmpeg(tmp_path: Path) -> None:
    """End-to-end: 2s muted source + 6s tone + loop_total=6s → out ≈ 6s."""
    src = tmp_path / "src.mp4"
    extra = tmp_path / "extra.mp3"
    out = tmp_path / "out.mp4"
    _mk_short_video(src, 2.0)
    _mk_tone(extra, 6.0)

    run_filter_only(
        source=src,
        out=out,
        canvas_filter="",
        target_w=160,
        target_h=120,
        select_expr=None,
        trim_in=0.0,
        trim_duration=2.0,
        source_volume=1.0,
        extras=[(extra, 1.0)],
        watermark_path=None,
        source_has_audio=False,  # source has no audio (-an)
        loop_total_duration=6.0,
        fps=15,
    )
    assert out.exists() and out.stat().st_size > 0
    dur = _ffprobe_duration(out)
    # Tolerance ±0.5s: ffmpeg's atrim/trim alignment can drift by < 1 frame /
    # 1 audio sample around the cut.
    assert 5.5 <= dur <= 6.5, f"loop output duration {dur:.2f}s out of band"


def test_run_filter_only_no_loop_unchanged(tmp_path: Path) -> None:
    """Sanity: passing loop_total_duration=None preserves prior behaviour
    (output ≈ trim_duration)."""
    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"
    _mk_short_video(src, 3.0)
    run_filter_only(
        source=src,
        out=out,
        canvas_filter="",
        target_w=160,
        target_h=120,
        select_expr=None,
        trim_in=0.5,
        trim_duration=1.5,
        source_volume=1.0,
        extras=None,
        watermark_path=None,
        source_has_audio=False,
        loop_total_duration=None,
        fps=15,
    )
    dur = _ffprobe_duration(out)
    assert 1.3 <= dur <= 1.7, f"no-loop duration {dur:.2f}s out of band"


def test_run_filter_only_two_extras_real_ffmpeg(tmp_path: Path) -> None:
    """3s muted source + tone1(2s) + tone2(4s), no loop → out ≈ 3s,
    audio = mix of both extras padded with silence after they run out."""
    src = tmp_path / "src.mp4"
    e1 = tmp_path / "e1.mp3"
    e2 = tmp_path / "e2.mp3"
    out = tmp_path / "out.mp4"
    _mk_short_video(src, 3.0)
    _mk_tone(e1, 2.0)
    _mk_tone(e2, 4.0)

    run_filter_only(
        source=src,
        out=out,
        canvas_filter="",
        target_w=160,
        target_h=120,
        select_expr=None,
        trim_in=0.0,
        trim_duration=3.0,
        source_volume=1.0,
        extras=[(e1, 0.5), (e2, 0.8)],
        watermark_path=None,
        source_has_audio=False,
        loop_total_duration=None,
        fps=15,
    )
    assert out.exists() and out.stat().st_size > 0
    dur = _ffprobe_duration(out)
    # Output rides the source video's length (-shortest = video).
    assert 2.7 <= dur <= 3.3, f"two-extra output duration {dur:.2f}s out of band"
