"""Unit tests for Feature Trim in/out + AudioMix.

Exercises:
- Pydantic validators on `Trim` and `AudioMix`.
- `_clip_segments_to_trim` segment retiming.
- Dispatch: trim in/out alone → filter_only; audio mix alone → filter_only.
- End-to-end: `/api/export` receives trim+audio fields and forwards correct
  kwargs into the export-pipeline (spy-based, no ffmpeg actually runs).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.main import (
    UPLOADS_DIR,
    _clip_segments_to_trim,
    _find_extra_audio,
    _meta_path,
    app,
)
from app.models import AudioMix, ExportRequest, Segment, Trim, Word
from app.transcribe import ProbeInfo
from fastapi.testclient import TestClient

VIDEO_ID = "c1b2c3d4e5f60718"


# ---------------- Trim model ----------------

def test_trim_default_is_noop() -> None:
    t = Trim()
    assert t.in_sec == 0.0 and t.out_sec == 0.0


def test_trim_rejects_in_gte_out() -> None:
    with pytest.raises(ValidationError):
        Trim(in_sec=5.0, out_sec=3.0)


def test_trim_allows_out_zero() -> None:
    # out_sec=0 is the sentinel for "to end" — not a real bound — must pass.
    t = Trim(in_sec=10.0, out_sec=0.0)
    assert t.out_sec == 0.0


def test_trim_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        Trim(in_sec=-1.0)


# ---------------- AudioMix model ----------------

def test_audio_default() -> None:
    a = AudioMix()
    assert a.source_volume == 1.0
    assert a.extras == []
    # Legacy fields are absorbed by the validator and never echoed back.
    assert a.extra_audio_id is None
    assert a.extra_volume is None


def test_audio_volume_bounds() -> None:
    with pytest.raises(ValidationError):
        AudioMix(source_volume=-0.1)
    with pytest.raises(ValidationError):
        AudioMix(source_volume=2.5)
    with pytest.raises(ValidationError):
        AudioMix(extras=[{"id": "a" * 16, "volume": -0.1}])
    with pytest.raises(ValidationError):
        AudioMix(extras=[{"id": "a" * 16, "volume": 2.5}])


def test_audio_legacy_form_promoted_to_extras() -> None:
    # Old projects (and backwards-compat clients) send the single-track
    # form. The validator must rebuild it as a one-element `extras` list.
    a = AudioMix(source_volume=0.5, extra_volume=1.5, extra_audio_id="a" * 16)
    assert a.source_volume == 0.5
    assert len(a.extras) == 1
    assert a.extras[0].id == "a" * 16
    assert a.extras[0].volume == 1.5
    assert a.extra_audio_id is None
    assert a.extra_volume is None


def test_audio_new_form_is_passthrough() -> None:
    a = AudioMix(extras=[
        {"id": "a" * 16, "volume": 0.7},
        {"id": "b" * 16, "volume": 1.2},
    ])
    assert len(a.extras) == 2
    assert a.extras[0].volume == 0.7
    assert a.extras[1].id == "b" * 16


def test_audio_new_form_wins_over_legacy() -> None:
    a = AudioMix(
        extras=[{"id": "c" * 16, "volume": 0.9}],
        extra_audio_id="a" * 16,
        extra_volume=0.1,
    )
    assert len(a.extras) == 1
    assert a.extras[0].id == "c" * 16
    assert a.extras[0].volume == 0.9


# ---------------- _clip_segments_to_trim ----------------

def _seg(start: float, end: float, words: list[tuple[float, float, str]] = None) -> Segment:
    ws = [Word(start=s, end=e, text=t) for s, e, t in (words or [])]
    return Segment(start=start, end=end, text=" ".join(w.text for w in ws) or "x", words=ws)


def test_clip_drops_segments_outside_range() -> None:
    segs = [_seg(0.0, 2.0), _seg(3.0, 5.0), _seg(20.0, 22.0)]
    out = _clip_segments_to_trim(segs, trim_in=4.0, trim_out=15.0)
    # [0,2] dropped (ends before in=4), [20,22] dropped (starts after out=15),
    # [3,5] clamped to [4,5] → shifted to [0, 1] in clip-space.
    assert len(out) == 1
    assert abs(out[0].start - 0.0) < 1e-6
    assert abs(out[0].end - 1.0) < 1e-6


def test_clip_shifts_segments_into_clip_timebase() -> None:
    # segment [2, 8] with trim [5, 10] → clamp to [5,8] → shift to [0, 3].
    seg = _seg(2.0, 8.0, [(2.0, 3.0, "a"), (6.0, 7.0, "b")])
    out = _clip_segments_to_trim([seg], trim_in=5.0, trim_out=10.0)
    assert len(out) == 1
    assert abs(out[0].start - 0.0) < 1e-6
    assert abs(out[0].end - 3.0) < 1e-6
    # word "a" at (2,3) → entirely before in=5 → dropped
    # word "b" at (6,7) → clamped to (6,7) then shifted → (1, 2)
    assert len(out[0].words) == 1
    assert out[0].words[0].text == "b"
    assert abs(out[0].words[0].start - 1.0) < 1e-6
    assert abs(out[0].words[0].end - 2.0) < 1e-6


def test_clip_clamps_word_edges() -> None:
    seg = _seg(0.0, 10.0, [(0.5, 6.0, "spans_cut"), (7.0, 8.0, "later")])
    out = _clip_segments_to_trim([seg], trim_in=5.0, trim_out=9.0)
    assert len(out) == 1
    # "spans_cut" starts at 0.5, crosses the cut-in at 5 → clamped start = 0 (in clip-space).
    assert out[0].words[0].text == "spans_cut"
    assert abs(out[0].words[0].start - 0.0) < 1e-6
    assert abs(out[0].words[0].end - 1.0) < 1e-6  # min(6, 9) - 5
    # "later" (7,8) → (2,3).
    assert abs(out[0].words[1].start - 2.0) < 1e-6
    assert abs(out[0].words[1].end - 3.0) < 1e-6


# ---------------- Dispatch with trim + audio ----------------

def _seed(video_id: str, duration: float = 20.0) -> Path:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOADS_DIR / f"{video_id}.mp4"
    path.write_bytes(b"\x00" * 32)
    _meta_path(video_id).write_text(json.dumps({
        "video_id": video_id,
        "duration": duration,
        "width": 1280,
        "height": 720,
        "language": "en",
        "segments": [],
        "is_audio_only": False,
        "_cache_key": "__trim__",
    }))
    return path


def _cleanup(video_id: str) -> None:
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
        if Path(path).name.startswith("extra_"):
            return ProbeInfo(duration=10.0, width=0, height=0, is_audio_only=True)
        return ProbeInfo(duration=20.0, width=1280, height=720, is_audio_only=False)

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
        "trim": {"in_sec": 0.0, "out_sec": 0.0},
        "audio": {"source_volume": 1.0, "extra_audio_id": None, "extra_volume": 1.0},
    }
    b.update(extra)
    return b


def test_trim_edges_active_routes_to_filter_only(client, spies):
    _cleanup(VIDEO_ID)
    _seed(VIDEO_ID, duration=20.0)
    try:
        body = _body(VIDEO_ID, trim={"in_sec": 5.0, "out_sec": 15.0})
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        assert "filter_only" in spies
        assert "stream_copy" not in spies
        kw = spies["filter_only"]
        assert abs(kw["trim_in"] - 5.0) < 1e-6
        assert abs(kw["trim_duration"] - 10.0) < 1e-6
    finally:
        _cleanup(VIDEO_ID)


def test_audio_volume_change_forces_reencode(client, spies):
    _cleanup(VIDEO_ID)
    _seed(VIDEO_ID, duration=20.0)
    try:
        body = _body(VIDEO_ID, audio={
            "source_volume": 0.5, "extra_audio_id": None, "extra_volume": 1.0,
        })
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        # Audio-mix active → fall out of stream_copy, must go to filter_only.
        assert "filter_only" in spies
        assert "stream_copy" not in spies
        assert abs(spies["filter_only"]["source_volume"] - 0.5) < 1e-6
    finally:
        _cleanup(VIDEO_ID)


def test_extra_audio_forwarded_when_present(client, spies, tmp_path):
    _cleanup(VIDEO_ID)
    _seed(VIDEO_ID, duration=20.0)
    # Seed a dummy extra audio file.
    extra_id = "a" * 16
    extra_path = UPLOADS_DIR / f"extra_{extra_id}.mp3"
    extra_path.write_bytes(b"\x00" * 32)
    try:
        body = _body(VIDEO_ID, audio={
            "source_volume": 1.0, "extra_audio_id": extra_id, "extra_volume": 0.8,
        })
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        assert "filter_only" in spies
        kw = spies["filter_only"]
        assert "extras" in kw
        assert len(kw["extras"]) == 1
        assert kw["extras"][0][0] == extra_path
        assert abs(kw["extras"][0][1] - 0.8) < 1e-6
    finally:
        extra_path.unlink(missing_ok=True)
        _cleanup(VIDEO_ID)


def test_trim_out_zero_interpreted_as_end(client, spies):
    """Contract: trim.out_sec=0 means 'to end of clip' — never literal 0."""
    _cleanup(VIDEO_ID)
    _seed(VIDEO_ID, duration=20.0)
    try:
        # in=10, out=0 → keep [10, 20] → duration 10
        body = _body(VIDEO_ID, trim={"in_sec": 10.0, "out_sec": 0.0})
        r = client.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        assert "filter_only" in spies
        kw = spies["filter_only"]
        assert abs(kw["trim_in"] - 10.0) < 1e-6
        assert abs(kw["trim_duration"] - 10.0) < 1e-6
    finally:
        _cleanup(VIDEO_ID)


def test_find_extra_audio_validates_id() -> None:
    assert _find_extra_audio("not-hex") is None
    assert _find_extra_audio("a" * 15) is None
    # Valid-shape id but no file → None (not an error).
    assert _find_extra_audio("f" * 16) is None


# ---------------- /api/extra-audio endpoint ----------------

def test_extra_audio_upload_rejects_video(client):
    r = client.post(
        "/api/extra-audio",
        files={"file": ("bad.mp4", b"\x00" * 128, "video/mp4")},
    )
    assert r.status_code == 415


def test_extra_audio_upload_happy_path(client, tmp_path, monkeypatch):
    # Use a real tiny mp3-shaped file; fake_probe returns fixed duration.
    payload = b"fake-mp3-bytes"
    r = client.post(
        "/api/extra-audio",
        files={"file": ("bg.mp3", payload, "audio/mpeg")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "bg.mp3"
    assert data["duration"] == 10.0  # from fake_probe
    assert len(data["extra_audio_id"]) == 16
    # File is on disk at the expected alias.
    p = UPLOADS_DIR / f"extra_{data['extra_audio_id']}.mp3"
    assert p.exists()
    p.unlink(missing_ok=True)
