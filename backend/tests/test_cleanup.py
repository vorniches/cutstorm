from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.main import app


@pytest.fixture
def fresh_dirs(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    uploads.mkdir()
    outputs.mkdir()
    monkeypatch.setattr(app_main, "UPLOADS_DIR", uploads)
    monkeypatch.setattr(app_main, "OUTPUTS_DIR", outputs)
    # reset peaks cache between tests
    app_main._peaks_cache.clear()
    return uploads, outputs


def _seed_project(uploads: Path, video_id: str, extra_id: str | None = None) -> Path:
    meta = uploads / f"{video_id}.json"
    project: dict = {}
    if extra_id:
        project = {"audio": {"extra_audio_id": extra_id, "source_volume": 1.0, "extra_volume": 1.0}}
    meta.write_text(json.dumps({
        "video_id": video_id,
        "duration": 5.0, "width": 320, "height": 240,
        "segments": [],
        "status": "done", "percent": 100,
        "project": project,
    }))
    (uploads / f"{video_id}.mp4").write_bytes(b"\x00" * 16)
    return meta


# ---------- Item 9: extended delete cleanup ----------


def test_delete_cleans_thumbs(fresh_dirs) -> None:
    uploads, outputs = fresh_dirs
    video_id = "1111111111111111"
    _seed_project(uploads, video_id)
    # Two sprite sheets with different count/width combos.
    (outputs / f"thumbs_{video_id}_40_160.jpg").write_bytes(b"j")
    (outputs / f"thumbs_{video_id}_80_320.jpg").write_bytes(b"j")

    client = TestClient(app)
    r = client.delete(f"/api/transcripts/{video_id}?drop_video=true")
    assert r.status_code == 200
    assert not (outputs / f"thumbs_{video_id}_40_160.jpg").exists()
    assert not (outputs / f"thumbs_{video_id}_80_320.jpg").exists()
    assert "thumb" in r.json()["removed"]


def test_delete_cleans_url_cache(fresh_dirs) -> None:
    uploads, _ = fresh_dirs
    video_id = "2222222222222222"
    _seed_project(uploads, video_id)
    cache_dir = uploads / "url_cache"
    cache_dir.mkdir()
    (cache_dir / "aaa.json").write_text(json.dumps({"url": "https://example.com/v", "video_id": video_id}))
    (cache_dir / "bbb.json").write_text(json.dumps({"url": "https://example.com/z", "video_id": "ffffffffffffffff"}))

    client = TestClient(app)
    client.delete(f"/api/transcripts/{video_id}")
    # cache entry for this video_id gone; other one intact.
    assert not (cache_dir / "aaa.json").exists()
    assert (cache_dir / "bbb.json").exists()


def test_delete_clears_peaks_cache(fresh_dirs) -> None:
    uploads, _ = fresh_dirs
    video_id = "3333333333333333"
    _seed_project(uploads, video_id)
    # Pre-populate peaks cache for this video at two bin sizes.
    app_main._peaks_cache[(f"v:{video_id}", 500)] = [0.1, 0.2]
    app_main._peaks_cache[(f"v:{video_id}", 1000)] = [0.3]
    app_main._peaks_cache[(f"v:other", 500)] = [0.9]

    client = TestClient(app)
    client.delete(f"/api/transcripts/{video_id}")
    assert (f"v:{video_id}", 500) not in app_main._peaks_cache
    assert (f"v:{video_id}", 1000) not in app_main._peaks_cache
    assert (f"v:other", 500) in app_main._peaks_cache  # unrelated entry preserved


# ---------- Item 10: extra audio lifecycle ----------


def test_delete_project_cleans_exclusive_extra_audio(fresh_dirs) -> None:
    uploads, _ = fresh_dirs
    video_id = "4444444444444444"
    extra_id = "aaaabbbbccccdddd"
    _seed_project(uploads, video_id, extra_id=extra_id)
    extra_path = uploads / f"extra_{extra_id}.mp3"
    extra_path.write_bytes(b"\x00")

    client = TestClient(app)
    client.delete(f"/api/transcripts/{video_id}?drop_video=true")
    assert not extra_path.exists()


def test_delete_project_keeps_shared_extra_audio(fresh_dirs) -> None:
    uploads, _ = fresh_dirs
    extra_id = "eeeeffff00001111"
    _seed_project(uploads, "5555555555555555", extra_id=extra_id)
    _seed_project(uploads, "6666666666666666", extra_id=extra_id)
    extra_path = uploads / f"extra_{extra_id}.wav"
    extra_path.write_bytes(b"\x00")

    client = TestClient(app)
    client.delete("/api/transcripts/5555555555555555?drop_video=true")
    # Extra still referenced by the other project → kept.
    assert extra_path.exists()

    # Delete the second referencer → now truly orphan → removed.
    client.delete("/api/transcripts/6666666666666666?drop_video=true")
    assert not extra_path.exists()


# ---------- Item 11: orphan sweep ----------


def test_sweep_removes_orphan_thumbnail(fresh_dirs) -> None:
    uploads, outputs = fresh_dirs
    # thumb for a video that has no meta
    orphan = outputs / "thumbs_deadbeef12345678_40_160.jpg"
    orphan.write_bytes(b"j")

    counts = app_main._sweep_orphans()
    assert not orphan.exists()
    assert counts["thumbs"] == 1


def test_sweep_keeps_active_thumbnail(fresh_dirs) -> None:
    uploads, outputs = fresh_dirs
    video_id = "7777777777777777"
    _seed_project(uploads, video_id)
    active = outputs / f"thumbs_{video_id}_40_160.jpg"
    active.write_bytes(b"j")

    app_main._sweep_orphans()
    assert active.exists()


def test_sweep_removes_orphan_url_cache(fresh_dirs) -> None:
    uploads, _ = fresh_dirs
    cache_dir = uploads / "url_cache"
    cache_dir.mkdir()
    stray = cache_dir / "zzz.json"
    stray.write_text(json.dumps({"url": "https://x", "video_id": "deadbeefdeadbeef"}))

    counts = app_main._sweep_orphans()
    assert not stray.exists()
    assert counts["url_cache"] >= 1


def test_sweep_removes_unreferenced_extra_audio(fresh_dirs) -> None:
    uploads, _ = fresh_dirs
    stray = uploads / "extra_0000111122223333.mp3"
    stray.write_bytes(b"x")
    # Sweep keeps fresh extras for 24h to avoid racing with the autosave
    # debounce window during a server restart. Back-date this stray to
    # simulate a genuinely abandoned file.
    import os
    import time
    old = time.time() - (25 * 3600)
    os.utime(stray, (old, old))

    counts = app_main._sweep_orphans()
    assert not stray.exists()
    assert counts["extras"] >= 1


def test_sweep_keeps_recent_unreferenced_extra_audio(fresh_dirs) -> None:
    """Files newer than the 24h grace period stay even if unreferenced —
    autosave needs that window to flush the project state to disk."""
    uploads, _ = fresh_dirs
    fresh = uploads / "extra_0000222233334444.mp3"
    fresh.write_bytes(b"x")
    counts = app_main._sweep_orphans()
    assert fresh.exists()
    assert counts["extras"] == 0


def test_sweep_marks_orphaned_pending_stale(fresh_dirs) -> None:
    uploads, _ = fresh_dirs
    video_id = "8888888888888888"
    meta = uploads / f"{video_id}.json"
    meta.write_text(json.dumps({
        "video_id": video_id, "duration": 5.0, "width": 320, "height": 240,
        "segments": [], "status": "pending", "percent": 20,
    }))
    (uploads / f"{video_id}.mp4").write_bytes(b"")

    # No task registered for this video_id.
    assert video_id not in app_main._transcribe_tasks

    counts = app_main._sweep_stale_meta()
    assert counts["stale_marked"] == 1
    assert json.loads(meta.read_text())["status"] == "stale"


def test_sweep_keeps_active_pending(fresh_dirs) -> None:
    """If _transcribe_tasks still has an entry, the meta stays pending."""
    import asyncio
    uploads, _ = fresh_dirs
    video_id = "9999999999999999"
    meta = uploads / f"{video_id}.json"
    meta.write_text(json.dumps({
        "video_id": video_id, "duration": 5.0, "width": 320, "height": 240,
        "segments": [], "status": "pending", "percent": 5,
    }))
    (uploads / f"{video_id}.mp4").write_bytes(b"")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def noop() -> None:
        await asyncio.sleep(10)
    task = loop.create_task(noop())
    app_main._transcribe_tasks[video_id] = (task, {"v": False})
    try:
        app_main._sweep_stale_meta()
        assert json.loads(meta.read_text())["status"] == "pending"
    finally:
        task.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        app_main._transcribe_tasks.pop(video_id, None)
        loop.close()
