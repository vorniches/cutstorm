from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("cutstorm")

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import ass_builder, burn, canvas as canvas_mod, peaks as peaks_mod, renderer, silence, simple_export, thumbnails as thumbs_mod, ws
from .models import (
    ExportRequest,
    ExportResponse,
    ExtraAudioResponse,
    FetchUrlRequest,
    Segment,
    TranscribeResponse,
    TranscriptSummary,
    UpdateSegmentsRequest,
    Word,
)
from .transcribe import _get_fw_model, probe, transcribe, transcribe_stream

UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", "/data/uploads"))
OUTPUTS_DIR = Path(os.environ.get("OUTPUTS_DIR", "/data/outputs"))
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/data/models"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", "/app/static"))
FONTS_DIR = Path(os.environ.get("FONTS_DIR", "/usr/share/fonts/cutstorm"))
FONTS_MANIFEST = Path(os.environ.get("FONTS_MANIFEST", "/opt/cutstorm/fonts-src/manifest.json"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(2 * 1024**3)))
MAX_FETCH_SEC = int(os.environ.get("MAX_FETCH_SEC", "900"))

for d in (UPLOADS_DIR, OUTPUTS_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _existing_video_ids() -> set[str]:
    """Set of video_ids that have at least one media file on disk right now."""
    ids: set[str] = set()
    try:
        for ext in _ALLOWED_EXTS:
            for p in UPLOADS_DIR.glob(f"*.{ext}"):
                stem = p.stem
                if _VIDEO_ID_RE.match(stem):
                    ids.add(stem)
    except Exception:  # pragma: no cover
        pass
    return ids


def _sweep_stale_meta() -> dict:
    """Mark any pending meta.json as stale when no bg task owns its video_id.
    Called on startup (after a crash pending becomes stale) and periodically
    during runtime (if a worker crashes mid-whisper)."""
    n = 0
    try:
        for f in UPLOADS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            if data.get("status") != "pending":
                continue
            vid = data.get("video_id")
            entry = _transcribe_tasks.get(vid) if vid else None
            if entry is not None and not entry[0].done():
                continue
            data["status"] = "stale"
            _atomic_write_text(f, json.dumps(data))
            log.info("meta.sweep stale video_id=%s", vid)
            n += 1
    except Exception as exc:  # pragma: no cover
        log.warning("meta.sweep failed: %s", exc)
    return {"stale_marked": n}


def _sweep_orphans() -> dict:
    """Remove artifacts on disk whose owning meta no longer exists.

    Cleans thumbnails sprite sheets, output mp4s, ass subtitle files, URL
    cache entries that point at missing videos, and extra-audio files not
    referenced by any project. Also runs the pending→stale pass."""
    counts = {"thumbs": 0, "outputs": 0, "ass": 0, "url_cache": 0, "extras": 0}
    try:
        owned = _existing_video_ids()
        owned_meta = {p.stem for p in UPLOADS_DIR.glob("*.json") if _VIDEO_ID_RE.match(p.stem)}

        # outputs/ — thumbnails, mp4, ass with dead owners
        for sprite in OUTPUTS_DIR.glob("thumbs_*.jpg"):
            # filename: thumbs_{video_id}_{count}_{width}.jpg
            parts = sprite.stem.split("_")
            if len(parts) >= 2 and not _VIDEO_ID_RE.match(parts[1]):
                continue
            if len(parts) >= 2 and parts[1] not in owned_meta:
                sprite.unlink()
                counts["thumbs"] += 1
        for mp4 in OUTPUTS_DIR.glob("*.mp4"):
            if _VIDEO_ID_RE.match(mp4.stem) and mp4.stem not in owned_meta:
                mp4.unlink()
                counts["outputs"] += 1
        for ass in OUTPUTS_DIR.glob("*.ass"):
            if _VIDEO_ID_RE.match(ass.stem) and ass.stem not in owned_meta:
                ass.unlink()
                counts["ass"] += 1
        for gif in OUTPUTS_DIR.glob("*.gif"):
            if _VIDEO_ID_RE.match(gif.stem) and gif.stem not in owned_meta:
                gif.unlink()
                counts["outputs"] += 1

        # uploads/url_cache/*.json
        cache_dir = UPLOADS_DIR / "url_cache"
        if cache_dir.exists():
            for entry in cache_dir.glob("*.json"):
                try:
                    d = json.loads(entry.read_text())
                except Exception:
                    entry.unlink(missing_ok=True)
                    counts["url_cache"] += 1
                    continue
                vid = d.get("video_id")
                if not vid or vid not in owned:
                    entry.unlink(missing_ok=True)
                    counts["url_cache"] += 1

        # uploads/extra_*.{audio-ext} — drop unreferenced.
        # IMPORTANT: skip files newer than EXTRA_GRACE_SEC. The frontend's
        # autosave debounces project-state PUTs by ~500ms, and a server
        # restart inside that window would otherwise wipe the extra track
        # the user just uploaded (race observed during loop-mode rollout).
        # 24h is plenty of headroom and still cleans up genuinely abandoned
        # files on the next periodic sweep.
        EXTRA_GRACE_SEC = 24 * 3600
        now = time.time()
        referenced = _referenced_extra_ids()
        for ext in _AUDIO_EXTS:
            for p in UPLOADS_DIR.glob(f"extra_*.{ext}"):
                stem = p.stem  # extra_{id}
                if not stem.startswith("extra_"):
                    continue
                eid = stem[len("extra_"):]
                if not _EXTRA_ID_RE.match(eid):
                    continue
                if eid in referenced:
                    continue
                try:
                    age = now - p.stat().st_mtime
                except OSError:
                    continue
                if age < EXTRA_GRACE_SEC:
                    log.info(
                        "orphan_sweep.skip_recent_extra id=%s age=%.0fs",
                        eid, age,
                    )
                    continue
                p.unlink()
                counts["extras"] += 1
    except Exception as exc:  # pragma: no cover
        log.warning("orphan_sweep failed: %s", exc)

    # Also run the pending-stale pass.
    stale = _sweep_stale_meta()
    summary = {**counts, **stale}
    log.info("orphan_sweep %s", summary)
    return summary


async def _sweep_loop() -> None:
    """Periodic background task. Runs _sweep_orphans every hour plus a
    faster pending-stale pass every 60s."""
    while True:
        try:
            await asyncio.sleep(60)
            _sweep_stale_meta()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            log.warning("sweep_loop.stale iteration failed: %s", exc)


async def _orphan_loop() -> None:
    while True:
        try:
            await asyncio.sleep(3600)
            _sweep_orphans()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            log.warning("sweep_loop.orphans iteration failed: %s", exc)


async def _warmup_whisper() -> None:
    """Preload the default whisper model on startup so the first real upload
    doesn't pay the cold-load cost (~2 min for large-v3 from a mounted volume
    on macOS Docker). Runs in a worker thread so uvicorn remains responsive
    during warmup — /health and UI come up immediately, transcription just
    waits on the already-holding model lock if it arrives mid-warmup."""
    model_name = os.environ.get("WHISPER_MODEL", "large-v3")
    log.info("warmup.whisper start model=%s", model_name)
    t0 = time.perf_counter()
    try:
        await asyncio.to_thread(_get_fw_model, model_name)
    except Exception as exc:
        log.warning("warmup.whisper failed: %s", exc)
        return
    log.info("warmup.whisper done elapsed=%.1fs", time.perf_counter() - t0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ws.set_loop(asyncio.get_running_loop())
    _sweep_orphans()
    stale_task = asyncio.create_task(_sweep_loop())
    orphan_task = asyncio.create_task(_orphan_loop())
    warmup_task = asyncio.create_task(_warmup_whisper())
    try:
        yield
    finally:
        stale_task.cancel()
        orphan_task.cancel()
        warmup_task.cancel()


app = FastAPI(title="CutStorm", lifespan=lifespan)


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


_CATEGORY_ORDER = ["display", "sans", "geometric", "serif", "handwritten", "cjk"]


@app.get("/api/fonts")
def api_fonts() -> dict:
    """Return the curated font list for the picker UI. Each entry carries the
    family name, category, scripts, and a URL to the primary file so the
    frontend can register @font-face dynamically."""
    try:
        data = json.loads(FONTS_MANIFEST.read_text())
    except Exception as exc:
        log.warning("fonts.manifest_unreadable path=%s err=%s", FONTS_MANIFEST, exc)
        return {"fonts": []}
    out: list[dict] = []
    for f in data.get("fonts", []):
        files = f.get("files", [])
        if not files:
            continue
        # Skip entries whose primary file isn't on disk (e.g. failed download).
        primary = files[0]
        if not (FONTS_DIR / primary).exists():
            log.warning("fonts.missing family=%s file=%s", f.get("family"), primary)
            continue
        out.append({
            "family": f["family"],
            "category": f.get("category", "sans"),
            "scripts": f.get("scripts", ["latin"]),
            "url": f"/fonts/{primary}",
        })
    out.sort(key=lambda x: (
        _CATEGORY_ORDER.index(x["category"]) if x["category"] in _CATEGORY_ORDER else len(_CATEGORY_ORDER),
        x["family"].lower(),
    ))
    return {"fonts": out}


_VIDEO_ID_RE = re.compile(r"^[a-f0-9]{16}$")


def _validate_video_id(video_id: str) -> None:
    if not _VIDEO_ID_RE.match(video_id):
        raise HTTPException(status_code=404, detail="video not found")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _meta_path(video_id: str) -> Path:
    return UPLOADS_DIR / f"{video_id}.json"


_VIDEO_EXTS = ("mp4", "mov", "mkv", "webm", "avi", "gif")
_AUDIO_EXTS = ("mp3", "wav", "m4a", "ogg", "flac", "aac")
_ALLOWED_EXTS = _VIDEO_EXTS + _AUDIO_EXTS

_MIME_BY_EXT = {
    "mp4":  "video/mp4",
    "mov":  "video/quicktime",
    "mkv":  "video/x-matroska",
    "webm": "video/webm",
    "avi":  "video/x-msvideo",
    "gif":  "image/gif",
    "mp3":  "audio/mpeg",
    "wav":  "audio/wav",
    "m4a":  "audio/mp4",
    "ogg":  "audio/ogg",
    "flac": "audio/flac",
    "aac":  "audio/aac",
}


def _ext_from_filename(name: str | None) -> str:
    if not name:
        return "mp4"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(status_code=415, detail=f"unsupported file type: .{ext}")
    return ext


def _video_path(video_id: str, ext: str = "mp4") -> Path:
    return UPLOADS_DIR / f"{video_id}.{ext}"


def _find_media_file(video_id: str) -> Path | None:
    for ext in _ALLOWED_EXTS:
        p = UPLOADS_DIR / f"{video_id}.{ext}"
        if p.exists():
            return p
    return None


_EXTRA_ID_RE = re.compile(r"^[a-f0-9]{16}$")


def _find_extra_audio(extra_id: str) -> Path | None:
    if not _EXTRA_ID_RE.match(extra_id):
        return None
    for ext in _AUDIO_EXTS:
        p = UPLOADS_DIR / f"extra_{extra_id}.{ext}"
        if p.exists():
            return p
    return None


def _output_path(video_id: str) -> Path:
    return OUTPUTS_DIR / f"{video_id}.mp4"


def _gif_output_path(video_id: str) -> Path:
    return OUTPUTS_DIR / f"{video_id}.gif"


_GIF_PRESETS: dict[str, dict[str, str | int]] = {
    "low":    {"width": 320, "fps": 10, "dither": "none"},
    "medium": {"width": 480, "fps": 15, "dither": "bayer"},
    "high":   {"width": 720, "fps": 20, "dither": "floyd_steinberg"},
}


def _encode_gif(src_mp4: Path, dst_gif: Path, quality: str) -> None:
    """Two-pass palette-gen/use ffmpeg conversion from the rendered MP4 to GIF.
    Audio is dropped (GIF has no audio track). Runs in a worker thread via
    asyncio.to_thread from the caller."""
    preset = _GIF_PRESETS.get(quality, _GIF_PRESETS["medium"])
    vf = (
        f"fps={preset['fps']},"
        f"scale={preset['width']}:-1:flags=lanczos,"
        "split[a][b];[a]palettegen[p];[b][p]paletteuse=dither="
        f"{preset['dither']}"
    )
    cmd = [
        "ffmpeg", "-y", "-nostats", "-loglevel", "error",
        "-i", str(src_mp4),
        "-vf", vf,
        "-an",
        str(dst_gif),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _ass_path(video_id: str) -> Path:
    return OUTPUTS_DIR / f"{video_id}.ass"


# Background transcription tasks keyed by video_id. Each entry is
# (task, cancel_flag). The flag lets callers interrupt a running whisper
# without waiting for asyncio.CancelledError to propagate through the thread
# pool — transcribe_stream polls the flag between segments.
_transcribe_tasks: dict[str, tuple[asyncio.Task, dict]] = {}


def _cancel_transcribes(except_video_id: str | None = None) -> None:
    """Flip the cancel flag + task.cancel() for every in-flight transcribe
    except optionally the one for `except_video_id`. Called on new upload so
    switching videos doesn't leave stale whisper runs eating CPU."""
    for vid, (task, flag) in list(_transcribe_tasks.items()):
        if vid == except_video_id:
            continue
        flag["v"] = True
        if not task.done():
            log.info("bg.transcribe cancelling stale task video_id=%s", vid)
            task.cancel()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write the file atomically: write to a sibling tmp, then rename. POSIX
    rename within the same directory is atomic, so concurrent readers always
    see either the old or the new file — never the half-written-then-half-
    overwritten frankenstein that produced the JSONDecodeError "Extra data"
    bug observed when autosave races with the upload finaliser."""
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{int(time.time() * 1000)}")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _update_meta(video_id: str, patch: dict) -> None:
    """Atomically merge `patch` into meta.json. Tolerant to missing file —
    callers that want initial meta already wrote it once."""
    path = _meta_path(video_id)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        data = {}
    data.update(patch)
    _atomic_write_text(path, json.dumps(data))


async def _run_transcribe_stream(
    video_id: str,
    media: Path,
    language: str | None,
    model: str | None,
    jid: str | None,
    cache_key: str,
    meta_template: dict,
    cancelled: dict,
) -> None:
    """Drive transcribe_stream in a worker thread, push each segment via WS.
    Also writes each segment to meta.json incrementally so a page reload can
    recover the in-progress transcription state."""
    log.info("bg.transcribe start video_id=%s", video_id)
    t0 = time.perf_counter()
    # Track running list of dumped segments so we can snapshot into meta.json
    # without re-serialising the whole Segment model each time.
    running: list[dict] = []

    def cancel_check() -> bool:
        return cancelled["v"]

    def on_segment(seg: Segment, idx: int, pct: int) -> None:
        ws.push(jid, {
            "phase": "segment",
            "video_id": video_id,
            "index": idx,
            "segment": seg.model_dump(),
            "percent": pct,
        })
        # Incrementally persist so reload can show partial progress.
        while len(running) <= idx:
            running.append({})
        running[idx] = seg.model_dump()
        _update_meta(video_id, {
            "segments": running,
            "percent": pct,
            "status": "pending",
        })

    def on_progress(phase: str, percent: int) -> None:
        ws.push(jid, {"phase": phase, "percent": percent, "video_id": video_id})

    def worker() -> tuple[list[Segment], str | None]:
        return transcribe_stream(
            media,
            language=language,
            model_name=model,
            on_segment=on_segment,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )

    try:
        segments, detected = await asyncio.to_thread(worker)
    except asyncio.CancelledError:
        cancelled["v"] = True
        log.info("bg.transcribe cancelled video_id=%s", video_id)
        _update_meta(video_id, {"status": "cancelled"})
        ws.push(jid, {"phase": "transcribe_cancelled", "video_id": video_id})
        return
    except Exception as exc:
        log.exception("bg.transcribe failed video_id=%s err=%s", video_id, exc)
        _update_meta(video_id, {"status": "error", "error": str(exc)})
        ws.push(jid, {"phase": "transcribe_error", "video_id": video_id, "error": str(exc)})
        return
    finally:
        _transcribe_tasks.pop(video_id, None)

    if cancelled["v"]:
        # cancel_check bailed cleanly out of transcribe_stream — user already
        # moved on to another video, leave the partial snapshot as "cancelled".
        log.info("bg.transcribe interrupted mid-stream video_id=%s", video_id)
        _update_meta(video_id, {"status": "cancelled"})
        ws.push(jid, {"phase": "transcribe_cancelled", "video_id": video_id})
        return

    # Persist final meta once alignment is complete so a reload skips whisper.
    info = probe(media)
    meta = TranscribeResponse(
        video_id=video_id,
        duration=info.duration,
        width=info.width,
        height=info.height,
        language=detected,
        segments=segments,
        original_filename=meta_template.get("original_filename"),
        model=model,
        is_audio_only=info.is_audio_only,
    )
    on_disk = json.loads(meta.model_dump_json())
    on_disk["_cache_key"] = cache_key
    on_disk["status"] = "done"
    on_disk["percent"] = 100
    on_disk["job_id"] = jid
    _atomic_write_text(_meta_path(video_id), json.dumps(on_disk))

    ws.push(jid, {
        "phase": "transcribe_done",
        "video_id": video_id,
        "percent": 100,
        "language": detected,
        "total_segments": len(segments),
    })
    log.info(
        "bg.transcribe done video_id=%s segments=%d elapsed=%.1fs",
        video_id, len(segments), time.perf_counter() - t0,
    )


def _finalize_uploaded_media(
    tmp_path: Path,
    ext: str,
    filename: str | None,
    language: str | None,
    model: str | None,
    generate_subs: bool,
    jid: str | None,
) -> TranscribeResponse:
    """Shared post-ingest flow for both file-upload and URL-import paths.

    Takes a downloaded/uploaded temp file, hashes it into a video_id, dedups
    against existing media, returns cached TranscribeResponse on hit, else
    writes initial meta and kicks off the background whisper task when
    requested. Caller is responsible for cleaning up tmp_path on error.
    """
    video_id = _hash_file(tmp_path)
    existing = _find_media_file(video_id)
    if existing is not None:
        tmp_path.unlink(missing_ok=True)
        final = existing
    else:
        final = _video_path(video_id, ext)
        tmp_path.rename(final)

    cache_key = json.dumps({"model": model, "language": language}, sort_keys=True)
    meta_file = _meta_path(video_id)

    if meta_file.exists():
        cached = json.loads(meta_file.read_text())
        if cached.get("_cache_key") == cache_key:
            log.info("finalize.cache_hit video_id=%s", video_id)
            cached.pop("_cache_key", None)
            return TranscribeResponse(**cached)

    # Kill every in-flight whisper for OTHER videos — user is switching focus,
    # the stale runs would only burn CPU and slow the new one down.
    _cancel_transcribes(except_video_id=video_id)

    old = _transcribe_tasks.get(video_id)
    if old is not None:
        old_task, old_flag = old
        if not old_task.done():
            log.info("finalize.cancelling previous task video_id=%s", video_id)
            old_flag["v"] = True
            old_task.cancel()

    info = probe(final)
    # Silent inputs (gifs, screen recordings without a mic) have nothing for
    # whisper to chew on — force-skip transcription so we don't spawn a task
    # that would just spin and emit zero segments.
    if not info.has_audio:
        if generate_subs:
            log.info("finalize.no_audio_skip_whisper video_id=%s", video_id)
        generate_subs = False
    immediate = TranscribeResponse(
        video_id=video_id,
        duration=info.duration,
        width=info.width,
        height=info.height,
        language=None,
        segments=[],
        original_filename=filename,
        model=model,
        is_audio_only=info.is_audio_only,
        status="pending" if generate_subs else "done",
        percent=0 if generate_subs else 100,
        job_id=jid,
    )

    initial_meta = json.loads(immediate.model_dump_json())
    initial_meta["_cache_key"] = "__no_subs__" if not generate_subs else "__pending__"
    initial_meta["status"] = "done" if not generate_subs else "pending"
    initial_meta["percent"] = 100 if not generate_subs else 0
    initial_meta["job_id"] = jid
    _atomic_write_text(meta_file, json.dumps(initial_meta))

    if generate_subs:
        loop = asyncio.get_running_loop()
        cancel_flag: dict = {"v": False}
        task = loop.create_task(_run_transcribe_stream(
            video_id=video_id,
            media=final,
            language=language,
            model=model,
            jid=jid,
            cache_key=cache_key,
            meta_template={"original_filename": filename},
            cancelled=cancel_flag,
        ))
        _transcribe_tasks[video_id] = (task, cancel_flag)
        log.info("finalize.bg_started video_id=%s", video_id)
    else:
        log.info("finalize.subs_disabled video_id=%s", video_id)

    return immediate


@app.post("/api/transcribe", response_model=TranscribeResponse)
async def api_transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default="ru"),
    model: str | None = Form(default=None),
    generate_subs: bool = Form(default=True),
    job_id: str | None = Query(default=None),
    x_job_id: str | None = Header(default=None),
) -> TranscribeResponse:
    """Upload + probe + return video_id immediately. Whisper (when requested)
    runs as a background task and streams segments via WS phase='segment'."""
    jid = job_id or x_job_id
    log.info(
        "transcribe.request filename=%r language=%s model=%s generate_subs=%s job_id=%s",
        file.filename, language, model, generate_subs, jid,
    )

    ext = _ext_from_filename(file.filename)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPLOADS_DIR / f".incoming-{os.getpid()}-{id(file)}"
    max_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", str(MAX_UPLOAD_BYTES)))
    written = 0
    try:
        with tmp.open("wb") as out:
            while chunk := file.file.read(1 << 20):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"file exceeds {max_bytes} bytes",
                    )
                out.write(chunk)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    upload_bytes = tmp.stat().st_size
    log.info("transcribe.upload bytes=%d (%.1f MB)", upload_bytes, upload_bytes / 1024 / 1024)

    return _finalize_uploaded_media(
        tmp_path=tmp,
        ext=ext,
        filename=file.filename,
        language=language,
        model=model,
        generate_subs=generate_subs,
        jid=jid,
    )


try:
    import yt_dlp as _yt_dlp  # type: ignore
except Exception:  # pragma: no cover — container always has it; tests monkeypatch
    _yt_dlp = None


# In-flight URL downloads keyed by job_id. Each entry is a threading.Event
# the yt-dlp progress_hook consults every tick; set it to interrupt the
# download cleanly.
import threading as _threading
_fetch_cancel_events: dict[str, _threading.Event] = {}


def _safe_external_url(url: str) -> bool:
    """SSRF guard: only allow http(s) against public hostnames."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False
    if host.startswith("192.168.") or host.startswith("10.") or host.startswith("169.254."):
        return False
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                n = int(parts[1])
            except ValueError:
                n = -1
            if 16 <= n <= 31:
                return False
    if host.startswith("fe80:") or host == "::":
        return False
    return True


class _FetchError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _classify_ytdlp_error(msg: str) -> tuple[int, str]:
    """Map a raw yt-dlp error string to an HTTP status + short human message.

    yt-dlp's error text is noisy and technical. The UI surfaces whatever we
    return in `detail` directly in the toast, so we want something a person
    can act on (retry, try another URL, etc). Order matters — more-specific
    signatures go first."""
    m = msg.lower() if msg else ""
    if "sign in to confirm your age" in m or "age-restricted" in m or "age restricted" in m:
        return 403, "This video is age-restricted and is not supported."
    if "private video" in m or "video is private" in m:
        return 403, "This video is private and cannot be downloaded."
    if "members-only" in m or "members only" in m:
        return 403, "Members-only content is not supported."
    if "removed by the uploader" in m or "has been removed" in m or "video unavailable" in m:
        return 404, "Video unavailable or removed."
    if "unsupported url" in m or "no video formats" in m:
        return 415, "This URL is not supported."
    if "http error 429" in m or "too many requests" in m:
        return 429, "Rate-limited by source. Try again in a few minutes."
    if (
        "network is unreachable" in m
        or "failed to resolve" in m
        or "connection timed out" in m
        or "name or service not known" in m
    ):
        return 502, "Could not reach the source. Check your connection."
    if "http error 403" in m:
        return 502, "Source refused the download (403)."
    if "http error 404" in m:
        return 404, "Video not found (404)."
    first_line = (msg or "").splitlines()[0] if msg else "download failed"
    return 502, f"Download failed: {first_line}"


def _url_cache_entry(url: str) -> Path:
    d = UPLOADS_DIR / "url_cache"
    d.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return d / f"{key}.json"


def _download_with_ytdlp(
    url: str,
    out_template: str,
    jid: str | None,
    cancel_event: _threading.Event | None = None,
) -> Path:
    """Runs inside a worker thread. Preflights duration/live, then downloads.
    Returns the final file path. If `cancel_event` is set mid-download the
    progress_hook raises a DownloadError and yt-dlp cleans up partial files."""
    if _yt_dlp is None:
        raise _FetchError(500, "yt-dlp is not installed")

    def _hook(d: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _yt_dlp.utils.DownloadError("cancelled by user")
        try:
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                pct = int(done / total * 100) if total else 0
                ws.push(jid, {"phase": "download", "percent": min(99, max(0, pct))})
            elif status == "finished":
                ws.push(jid, {"phase": "download_done", "percent": 100})
        except Exception:
            pass

    opts = {
        "outtmpl": out_template,
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "progress_hooks": [_hook],
    }
    with _yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("is_live"):
            raise _FetchError(400, "live streams not supported in first iteration")
        dur = info.get("duration") or 0
        if dur and dur > MAX_FETCH_SEC:
            raise _FetchError(413, f"video exceeds MAX_FETCH_SEC={MAX_FETCH_SEC}s limit")
        info = ydl.extract_info(url, download=True)
        if "requested_downloads" in info and info["requested_downloads"]:
            return Path(info["requested_downloads"][0]["filepath"])
        return Path(ydl.prepare_filename(info))


def _remux_to_mp4(src: Path) -> Path:
    """If src isn't already mp4, remux-copy into a sibling .mp4. Falls back to
    re-encode only when no mp4-compatible streams exist."""
    if src.suffix.lower() == ".mp4":
        return src
    dst = src.with_suffix(".mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-c", "copy",
             "-movflags", "+faststart", str(dst)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-c:v", "libx264", "-c:a", "aac",
             "-movflags", "+faststart", str(dst)],
            check=True, capture_output=True,
        )
    src.unlink(missing_ok=True)
    return dst


@app.post("/api/fetch-url", response_model=TranscribeResponse)
async def api_fetch_url(
    req: FetchUrlRequest,
    job_id: str | None = Query(default=None),
    x_job_id: str | None = Header(default=None),
) -> TranscribeResponse:
    """Download a video by URL via yt-dlp, then run the same post-ingest flow
    as /api/transcribe."""
    jid = job_id or x_job_id
    url = req.url.strip()
    log.info(
        "fetch_url.request url=%s generate_subs=%s job_id=%s", url, req.generate_subs, jid,
    )

    if not _safe_external_url(url):
        raise HTTPException(status_code=400, detail="invalid url")

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    cache_entry = _url_cache_entry(url)
    if cache_entry.exists():
        try:
            data = json.loads(cache_entry.read_text())
            cached_id = data.get("video_id")
        except Exception:
            cached_id = None
        if cached_id and _find_media_file(cached_id) is not None:
            meta_file = _meta_path(cached_id)
            cache_key = json.dumps({"model": req.model, "language": req.language}, sort_keys=True)
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                if meta.get("_cache_key") == cache_key:
                    meta.pop("_cache_key", None)
                    log.info("fetch_url.cache_hit url=%s video_id=%s", url, cached_id)
                    return TranscribeResponse(**meta)

    stem = f".incoming-url-{os.getpid()}-{int(time.time() * 1000)}"
    out_template = str(UPLOADS_DIR / f"{stem}.%(ext)s")

    cancel_event = _threading.Event()
    if jid:
        _fetch_cancel_events[jid] = cancel_event
    try:
        dl_path = await asyncio.to_thread(
            _download_with_ytdlp, url, out_template, jid, cancel_event,
        )
    except _FetchError as exc:
        ws.push(jid, {"phase": "download_error", "error": exc.detail})
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    except Exception as exc:
        raw = str(exc) or exc.__class__.__name__
        # Treat user cancellation as a client request, not a server error.
        if cancel_event.is_set() or "cancelled by user" in raw.lower():
            if jid:
                _fetch_cancel_events.pop(jid, None)
            raise HTTPException(status_code=499, detail="download cancelled")
        status, detail = _classify_ytdlp_error(raw)
        log.warning("fetch_url.download_failed url=%s status=%d raw=%s", url, status, raw.splitlines()[0] if raw else "")
        ws.push(jid, {"phase": "download_error", "error": detail})
        raise HTTPException(status_code=status, detail=detail)
    finally:
        if jid:
            _fetch_cancel_events.pop(jid, None)

    if not dl_path.exists():
        raise HTTPException(status_code=502, detail="download produced no file")

    try:
        dl_path = await asyncio.to_thread(_remux_to_mp4, dl_path)
    except subprocess.CalledProcessError as exc:
        dl_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f"remux failed: {exc}")

    try:
        info = probe(dl_path)
    except Exception as exc:
        dl_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f"probe failed: {exc}")
    if info.duration > MAX_FETCH_SEC + 1:
        dl_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=413,
            detail=f"video exceeds MAX_FETCH_SEC={MAX_FETCH_SEC}s limit",
        )

    try:
        host = urlparse(url).hostname or "url"
    except Exception:
        host = "url"
    filename = f"{host}.mp4"

    resp = _finalize_uploaded_media(
        tmp_path=dl_path,
        ext="mp4",
        filename=filename,
        language=req.language,
        model=req.model,
        generate_subs=req.generate_subs,
        jid=jid,
    )

    try:
        cache_entry.write_text(json.dumps({"url": url, "video_id": resp.video_id}))
    except Exception:
        pass

    return resp


@app.get("/api/transcripts", response_model=list[TranscriptSummary])
def list_transcripts() -> list[TranscriptSummary]:
    out: list[TranscriptSummary] = []
    for f in UPLOADS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            out.append(
                TranscriptSummary(
                    video_id=data.get("video_id", f.stem),
                    original_filename=data.get("original_filename"),
                    language=data.get("language"),
                    model=data.get("model"),
                    duration=float(data.get("duration", 0.0)),
                    width=int(data.get("width", 0)),
                    height=int(data.get("height", 0)),
                    segments_count=len(data.get("segments", [])),
                    updated_at=f.stat().st_mtime,
                    is_audio_only=bool(data.get("is_audio_only", False)),
                )
            )
        except Exception as exc:
            log.warning("transcripts.list skip %s: %s", f.name, exc)
    out.sort(key=lambda x: x.updated_at, reverse=True)
    return out


@app.get("/api/transcripts/{video_id}", response_model=TranscribeResponse)
def get_transcript(video_id: str) -> TranscribeResponse:
    _validate_video_id(video_id)
    meta = _meta_path(video_id)
    if not meta.exists():
        raise HTTPException(status_code=404, detail="transcript not found")
    data = json.loads(meta.read_text())
    data.pop("_cache_key", None)
    return TranscribeResponse(**data)


@app.put("/api/transcripts/{video_id}")
def update_transcript(video_id: str, req: UpdateSegmentsRequest) -> dict:
    _validate_video_id(video_id)
    meta = _meta_path(video_id)
    if not meta.exists():
        raise HTTPException(status_code=404, detail="transcript not found")
    data = json.loads(meta.read_text())

    changed: list[str] = []
    if req.segments is not None:
        data["segments"] = [s.model_dump() for s in req.segments]
        changed.append(f"segments={len(req.segments)}")
    if req.project is not None:
        # Merge into existing project snapshot so partial saves don't wipe
        # fields the client didn't include. Pydantic exclude_none avoids
        # overwriting with Nones from a default-constructed ProjectState.
        existing = dict(data.get("project") or {})
        existing.update(req.project.model_dump(exclude_none=True))
        data["project"] = existing
        changed.append("project")

    _atomic_write_text(meta, json.dumps(data))
    log.info("transcripts.updated video_id=%s changed=%s", video_id, ",".join(changed) or "none")
    return {"ok": True}


def _referenced_extra_ids() -> set[str]:
    """Scan all project meta files for extra_audio_id references. Used by
    delete + orphan-sweep to avoid removing an extra audio file that another
    project still points at. Reads BOTH the legacy single-track form
    (`audio.extra_audio_id`) and the multi-track form (`audio.extras: list`)."""
    ids: set[str] = set()
    try:
        for f in UPLOADS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            audio = (data.get("project") or {}).get("audio") or {}
            # Legacy single-track form
            legacy = audio.get("extra_audio_id")
            if legacy:
                ids.add(legacy)
            # Multi-track form
            for entry in audio.get("extras") or []:
                if isinstance(entry, dict):
                    eid = entry.get("id")
                    if eid:
                        ids.add(eid)
                elif isinstance(entry, str):
                    ids.add(entry)
    except Exception:  # pragma: no cover
        pass
    return ids


@app.delete("/api/transcripts/{video_id}")
def delete_transcript(video_id: str, drop_video: bool = False) -> dict:
    _validate_video_id(video_id)

    # Cancel any in-flight whisper for this video_id BEFORE touching disk.
    # Otherwise the bg task re-writes meta half a second later over files
    # we just removed, and we end up with a zombie meta with `pending`
    # status that nothing can clean up.
    entry = _transcribe_tasks.pop(video_id, None)
    if entry is not None:
        task, flag = entry
        flag["v"] = True
        if not task.done():
            task.cancel()
            log.info("transcribe.cancelled_by_delete video_id=%s", video_id)

    meta = _meta_path(video_id)
    media = _find_media_file(video_id)
    out = _output_path(video_id)
    ass = _ass_path(video_id)

    # Read meta BEFORE unlinking it so we know which extra-audio to sweep.
    meta_data: dict = {}
    if meta.exists():
        try:
            meta_data = json.loads(meta.read_text())
        except Exception:
            meta_data = {}

    removed: list[str] = []
    if meta.exists():
        meta.unlink()
        removed.append("meta")
    if drop_video and media is not None:
        media.unlink()
        removed.append("video")
    if out.exists():
        out.unlink()
        removed.append("output")
    gif = _gif_output_path(video_id)
    if gif.exists():
        gif.unlink()
        removed.append("gif")
    if ass.exists():
        ass.unlink()
        removed.append("ass")

    # Thumbnail sprite sheets — multiple possible per video (different
    # count/width combos are generated on demand by the timeline).
    try:
        for sprite in OUTPUTS_DIR.glob(f"thumbs_{video_id}_*.jpg"):
            sprite.unlink()
            removed.append("thumb")
    except Exception:  # pragma: no cover
        pass

    # URL cache entries that pointed at this video_id.
    try:
        cache_dir = UPLOADS_DIR / "url_cache"
        if cache_dir.exists():
            for entry in cache_dir.glob("*.json"):
                try:
                    data = json.loads(entry.read_text())
                except Exception:
                    continue
                if data.get("video_id") == video_id:
                    entry.unlink()
                    removed.append("url_cache")
    except Exception:  # pragma: no cover
        pass

    # In-memory peaks cache — drop any entries keyed by this video.
    for key in [k for k in list(_peaks_cache.keys()) if k[0] == f"v:{video_id}"]:
        _peaks_cache.pop(key, None)

    # Extra audio: collect ALL ids this project owns (legacy single field
    # plus the multi-track `extras` list), and drop the file for any id
    # that no OTHER project still references.
    audio_meta = (meta_data.get("project") or {}).get("audio") or {}
    extra_ids_owned: list[str] = []
    legacy_extra = audio_meta.get("extra_audio_id")
    if legacy_extra:
        extra_ids_owned.append(legacy_extra)
    for entry in audio_meta.get("extras") or []:
        if isinstance(entry, dict) and entry.get("id"):
            extra_ids_owned.append(entry["id"])
        elif isinstance(entry, str):
            extra_ids_owned.append(entry)
    if extra_ids_owned:
        still_referenced = _referenced_extra_ids()
        for eid in extra_ids_owned:
            if eid in still_referenced:
                continue
            for ext in _AUDIO_EXTS:
                p = UPLOADS_DIR / f"extra_{eid}.{ext}"
                if p.exists():
                    p.unlink()
                    removed.append("extra_audio")
                    break

    log.info("transcripts.deleted video_id=%s removed=%s", video_id, removed)
    return {"ok": True, "removed": removed}


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except Exception:  # pragma: no cover
        pass
    return total


@app.get("/api/storage")
def storage_info() -> dict:
    """Report disk usage for the /data subdirectories and a count of
    recognised projects. Used by the Sidebar to show the "Using N GB"
    indicator and drive the Clean-up button."""
    uploads = _dir_size_bytes(UPLOADS_DIR)
    outputs = _dir_size_bytes(OUTPUTS_DIR)
    models = _dir_size_bytes(MODELS_DIR)
    projects = 0
    try:
        projects = sum(
            1 for f in UPLOADS_DIR.glob("*.json") if _VIDEO_ID_RE.match(f.stem)
        )
    except Exception:  # pragma: no cover
        pass
    return {
        "uploads_bytes": uploads,
        "outputs_bytes": outputs,
        "models_bytes": models,
        "total_bytes": uploads + outputs + models,
        "projects": projects,
    }


@app.post("/api/storage/sweep-now")
def storage_sweep_now() -> dict:
    """Run the orphan + stale sweep immediately and return the counts."""
    return _sweep_orphans()


@app.post("/api/fetch-url/{job_id}/cancel")
def cancel_fetch_url(job_id: str) -> dict:
    """Signal the yt-dlp progress_hook for this job to bail out. The download
    thread sees the event on its next tick (usually sub-second) and exits
    cleanly. Partial files inside UPLOADS_DIR are removed by yt-dlp itself."""
    event = _fetch_cancel_events.get(job_id)
    if event is None:
        return {"ok": True, "cancelled": False}
    event.set()
    log.info("fetch_url.cancel job_id=%s", job_id)
    return {"ok": True, "cancelled": True}


@app.post("/api/transcribe/{video_id}/cancel")
def cancel_transcribe(video_id: str) -> dict:
    """Interrupt the background whisper task for this video. The partial meta
    is left behind with status='cancelled' so a page reload shows the correct
    state. Disk artifacts (video, meta, output) are not removed — use
    DELETE /api/transcripts/{id} for that."""
    _validate_video_id(video_id)
    entry = _transcribe_tasks.get(video_id)
    if entry is None:
        return {"ok": True, "cancelled": False}
    task, flag = entry
    flag["v"] = True
    if not task.done():
        task.cancel()
        log.info("transcribe.cancel video_id=%s", video_id)
        _update_meta(video_id, {"status": "cancelled"})
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False}


@app.get("/api/video/{video_id}")
def api_video(video_id: str) -> FileResponse:
    _validate_video_id(video_id)
    path = _find_media_file(video_id)
    if path is None:
        raise HTTPException(status_code=404, detail="video not found")
    ext = path.suffix.lstrip(".").lower()
    mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=mime)


def _clip_segments_to_trim(
    segments: list[Segment], trim_in: float, trim_out: float,
) -> list[Segment]:
    """Drop/clamp segments so their timestamps live in the [0, trim_out-trim_in] range."""
    out_segs: list[Segment] = []
    for s in segments:
        if s.end <= trim_in or s.start >= trim_out:
            continue
        ns = max(s.start, trim_in) - trim_in
        ne = min(s.end, trim_out) - trim_in
        new_words = [
            Word(
                start=max(w.start, trim_in) - trim_in,
                end=min(w.end, trim_out) - trim_in,
                text=w.text,
            )
            for w in (s.words or [])
            if w.end > trim_in and w.start < trim_out
        ]
        if new_words:
            text = " ".join(w.text for w in new_words).strip()
        else:
            text = s.text
        out_segs.append(Segment(start=ns, end=ne, text=text, words=new_words))
    return out_segs


# In-flight extra-audio transcribe runs keyed by extra_audio_id. Each entry
# is a cancel flag dict — the worker thread polls flag["v"] between segments
# and bails when set. Mirrors `_transcribe_tasks` semantics for source video.
_extra_transcribe_cancels: dict[str, dict] = {}


@app.post("/api/transcribe-extra")
async def api_transcribe_extra(
    extra_audio_id: str = Form(...),
    language: str | None = Form(default="ru"),
    model: str | None = Form(default=None),
    job_id: str | None = Query(default=None),
    x_job_id: str | None = Header(default=None),
) -> dict:
    """Run whisper on a previously uploaded extra audio track. Streams progress
    via the same WS channel as /api/transcribe (phases prefixed with
    `extra_*`), then returns the final segments. The result is NOT cached on
    disk — extra-audio transcripts are stored client-side in the project
    state (`extra_segments` field) so they survive reload alongside the
    source transcript."""
    jid = job_id or x_job_id
    if not _EXTRA_ID_RE.match(extra_audio_id):
        raise HTTPException(status_code=400, detail="invalid extra_audio_id")
    extra_path = _find_extra_audio(extra_audio_id)
    if extra_path is None:
        raise HTTPException(status_code=404, detail="extra audio not found")

    log.info("transcribe_extra.start id=%s job_id=%s", extra_audio_id, jid)

    info = probe(extra_path)

    # Replace any in-flight run for the SAME extra audio (user clicked
    # "Generate" again before the first one finished). Different extra IDs
    # run in parallel — same as how /api/transcribe per-video tasks work.
    prev = _extra_transcribe_cancels.pop(extra_audio_id, None)
    if prev is not None:
        prev["v"] = True
        log.info("transcribe_extra.preempt previous run id=%s", extra_audio_id)

    cancel_flag: dict = {"v": False}
    _extra_transcribe_cancels[extra_audio_id] = cancel_flag

    def on_segment(seg: Segment, idx: int, pct: int) -> None:
        ws.push(jid, {
            "phase": "extra_segment",
            "extra_audio_id": extra_audio_id,
            "index": idx,
            "segment": seg.model_dump(),
            "percent": pct,
        })

    def on_progress(phase: str, percent: int) -> None:
        # Re-tag phase so the frontend can route extra-track progress without
        # being confused with source-video progress.
        ws.push(jid, {
            "phase": f"extra_{phase}",
            "percent": percent,
            "extra_audio_id": extra_audio_id,
        })

    def cancel_check() -> bool:
        return cancel_flag["v"]

    def worker() -> tuple[list[Segment], str | None]:
        return transcribe_stream(
            extra_path,
            language=language,
            model_name=model,
            on_segment=on_segment,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )

    try:
        segments, detected = await asyncio.to_thread(worker)
    except Exception as exc:
        _extra_transcribe_cancels.pop(extra_audio_id, None)
        log.exception("transcribe_extra.failed id=%s err=%s", extra_audio_id, exc)
        ws.push(jid, {"phase": "extra_transcribe_error", "extra_audio_id": extra_audio_id, "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"extra transcribe failed: {exc}")

    # Drop our slot before notifying — by the time the client reads the
    # response, the cancel flag is no longer relevant.
    if _extra_transcribe_cancels.get(extra_audio_id) is cancel_flag:
        _extra_transcribe_cancels.pop(extra_audio_id, None)

    if cancel_flag["v"]:
        log.info(
            "transcribe_extra.cancelled id=%s segments_so_far=%d",
            extra_audio_id, len(segments),
        )
        ws.push(jid, {
            "phase": "extra_transcribe_cancelled",
            "extra_audio_id": extra_audio_id,
            "total_segments": len(segments),
        })
        # Still return what we have so the client can keep partial captions
        # if it wants — symmetric with the source-track behaviour.
        return {
            "extra_audio_id": extra_audio_id,
            "duration": info.duration,
            "language": detected,
            "segments": [s.model_dump() for s in segments],
            "cancelled": True,
        }

    ws.push(jid, {
        "phase": "extra_transcribe_done",
        "extra_audio_id": extra_audio_id,
        "percent": 100,
        "language": detected,
        "total_segments": len(segments),
    })
    log.info(
        "transcribe_extra.done id=%s segments=%d lang=%s",
        extra_audio_id, len(segments), detected,
    )
    return {
        "extra_audio_id": extra_audio_id,
        "duration": info.duration,
        "language": detected,
        "segments": [s.model_dump() for s in segments],
    }


@app.post("/api/transcribe-extra/{extra_audio_id}/cancel")
def cancel_transcribe_extra(extra_audio_id: str) -> dict:
    """Signal the running transcribe-extra worker to stop after the next
    segment boundary. Mirrors /api/transcribe/{video_id}/cancel."""
    if not _EXTRA_ID_RE.match(extra_audio_id):
        raise HTTPException(status_code=400, detail="invalid extra_audio_id")
    flag = _extra_transcribe_cancels.get(extra_audio_id)
    if flag is None:
        return {"ok": True, "cancelled": False}
    flag["v"] = True
    log.info("transcribe_extra.cancel id=%s", extra_audio_id)
    return {"ok": True, "cancelled": True}


@app.post("/api/extra-audio", response_model=ExtraAudioResponse)
async def api_extra_audio(
    file: UploadFile = File(...),
) -> ExtraAudioResponse:
    """Upload an extra audio track for the mix. Video files rejected."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _AUDIO_EXTS:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported audio type: .{ext} (allowed: {_AUDIO_EXTS})",
        )
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPLOADS_DIR / f".incoming-extra-{os.getpid()}-{id(file)}"
    max_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", str(MAX_UPLOAD_BYTES)))
    written = 0
    try:
        with tmp.open("wb") as out_f:
            while chunk := file.file.read(1 << 20):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail="file too large")
                out_f.write(chunk)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    extra_id = _hash_file(tmp)
    final = UPLOADS_DIR / f"extra_{extra_id}.{ext}"
    if final.exists():
        tmp.unlink(missing_ok=True)
    else:
        tmp.rename(final)
    info = probe(final)
    log.info("extra_audio.uploaded id=%s name=%r duration=%.2fs", extra_id, file.filename, info.duration)
    return ExtraAudioResponse(
        extra_audio_id=extra_id,
        duration=info.duration,
        name=file.filename,
    )


_peaks_cache: dict[tuple[str, int], list[float]] = {}


async def _peaks_for(path: Path, cache_key: tuple[str, int], bins: int) -> list[float]:
    if cache_key in _peaks_cache:
        return _peaks_cache[cache_key]
    try:
        values = await asyncio.to_thread(peaks_mod.compute_peaks, path, bins)
    except Exception as exc:
        log.warning("peaks.compute failed id=%s err=%s", cache_key[0], exc)
        values = []
    _peaks_cache[cache_key] = values
    return values


@app.get("/api/thumbnails/{video_id}")
async def api_thumbnails(
    video_id: str,
    count: int = Query(default=40, ge=4, le=120),
    width: int = Query(default=160, ge=40, le=320),
) -> FileResponse:
    """Return a JPEG sprite sheet of `count` evenly-spaced thumbnails."""
    _validate_video_id(video_id)
    media = _find_media_file(video_id)
    if media is None:
        raise HTTPException(status_code=404, detail="video not found")
    sprite = thumbs_mod.sprite_path(OUTPUTS_DIR, video_id, count, width)
    if not sprite.exists():
        info = probe(media)
        if info.is_audio_only:
            raise HTTPException(status_code=400, detail="audio-only file has no frames")
        try:
            await asyncio.to_thread(
                thumbs_mod.build_sprite, media, info.duration, sprite, count, width,
            )
        except Exception as exc:
            log.warning("thumbnails.build failed id=%s err=%s", video_id, exc)
            raise HTTPException(status_code=500, detail=f"thumb build failed: {exc}")
    return FileResponse(
        sprite,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/peaks/extra/{extra_id}")
async def api_peaks_extra(
    extra_id: str,
    bins: int = Query(default=500, ge=16, le=4000),
) -> JSONResponse:
    """Waveform peaks for an extra audio track uploaded via /api/extra-audio."""
    extra = _find_extra_audio(extra_id)
    if extra is None:
        raise HTTPException(status_code=404, detail="extra audio not found")
    values = await _peaks_for(extra, (f"e:{extra_id}", bins), bins)
    return JSONResponse({"peaks": values})


@app.get("/api/extra-audio/{extra_id}")
def api_extra_audio_file(extra_id: str) -> FileResponse:
    """Serve the raw extra-audio file. Used by the preview's WebAudio graph
    after a page reload, when the in-memory blob URL is gone but the project
    still references the upload by id."""
    extra = _find_extra_audio(extra_id)
    if extra is None:
        raise HTTPException(status_code=404, detail="extra audio not found")
    ext = extra.suffix.lstrip(".").lower()
    mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
    return FileResponse(extra, media_type=mime)


@app.get("/api/extra-audio/{extra_id}/info")
def api_extra_audio_info(extra_id: str) -> dict:
    """Lightweight metadata for the extra-audio file: duration and file
    extension. Used by the editor on project-load to rehydrate the trim
    bar's waveform width and the toolbar's track label without forcing the
    user to re-upload."""
    extra = _find_extra_audio(extra_id)
    if extra is None:
        raise HTTPException(status_code=404, detail="extra audio not found")
    info = probe(extra)
    return {
        "extra_audio_id": extra_id,
        "duration": info.duration,
        "ext": extra.suffix.lstrip(".").lower(),
    }


@app.get("/api/peaks/{video_id}")
async def api_peaks(
    video_id: str,
    bins: int = Query(default=500, ge=16, le=4000),
) -> JSONResponse:
    """Waveform peaks for a source video/audio file (downsampled, [0, 1])."""
    _validate_video_id(video_id)
    media = _find_media_file(video_id)
    if media is None:
        raise HTTPException(status_code=404, detail="video not found")
    values = await _peaks_for(media, (f"v:{video_id}", bins), bins)
    return JSONResponse({"peaks": values})


@app.post("/api/export", response_model=ExportResponse)
async def api_export(
    req: ExportRequest,
    job_id: str | None = Query(default=None),
    x_job_id: str | None = Header(default=None),
) -> ExportResponse:
    _validate_video_id(req.video_id)
    jid = job_id or x_job_id
    t0 = time.perf_counter()
    log.info(
        "export.request video_id=%s mode=%s segments=%d trim_silences=%s trim=(%.2f,%.2f) audio=(src=%.2f,extras=%d) subtitle_track=%s job_id=%s",
        req.video_id,
        req.style.mode,
        len(req.segments),
        req.trim_silences,
        req.trim.in_sec,
        req.trim.out_sec,
        req.audio.source_volume,
        len(req.audio.extras),
        req.subtitle_track,
        jid,
    )
    media = _find_media_file(req.video_id)
    if media is None:
        log.warning("export.404 video_id=%s not found on disk", req.video_id)
        raise HTTPException(status_code=404, detail="video not found")
    info = probe(media)

    # Audio-only cannot use "source" preset (no aspect to preserve); default to 9:16.
    canvas_cfg = req.canvas
    if info.is_audio_only and canvas_cfg.preset == "source":
        canvas_cfg = canvas_cfg.model_copy(update={"preset": "9:16"})
    resolved = canvas_mod.resolve(canvas_cfg, info.width, info.height)

    # ---- trim in/out edges ----
    trim_in = max(0.0, float(req.trim.in_sec))
    trim_out_raw = float(req.trim.out_sec)
    trim_out = info.duration if trim_out_raw <= 0.0 else min(trim_out_raw, info.duration)
    if trim_in >= trim_out:
        trim_in = 0.0
        trim_out = info.duration
    edge_trim_active = (trim_in > 0.01) or (trim_out < info.duration - 0.01)
    clipped_duration = trim_out - trim_in

    # Resolve the active subtitle track. "source" is the source-video
    # transcript; any extra_audio_id present in audio.extras maps onto that
    # extra. An unknown id (e.g. user removed the extra but client still
    # sent its old id) falls back to "source" silently.
    extras_ids = {e.id for e in req.audio.extras}
    if req.subtitle_track == "source":
        effective_subtitle_track = "source"
    elif req.subtitle_track in extras_ids:
        effective_subtitle_track = req.subtitle_track
    else:
        if req.subtitle_track:
            log.warning(
                "export.unknown_subtitle_track id=%s — falling back to source",
                req.subtitle_track,
            )
        effective_subtitle_track = "source"

    segments_for_render = req.segments
    # Source-track subtitles are tied to the source video timeline and need
    # clipping when the user trimmed in/out. Extra-track subtitles ride the
    # extra-audio timeline as-is and must NOT be clipped.
    if edge_trim_active and effective_subtitle_track == "source":
        segments_for_render = _clip_segments_to_trim(req.segments, trim_in, trim_out)

    keeps: list[tuple[float, float]] | None = None
    new_duration = clipped_duration
    if req.trim_silences:
        keeps = silence.cuts_from_words(
            segments_for_render,
            threshold_sec=req.silence_threshold_sec,
            padding_sec=req.silence_padding_sec,
            total_duration=clipped_duration,
        )
        segments_for_render = silence.retime_segments(segments_for_render, keeps)
        new_duration = silence.kept_duration(keeps)
        log.info(
            "export.trim keeps=%d new_duration=%.2fs (from %.2fs)",
            len(keeps),
            new_duration,
            clipped_duration,
        )

    # ---- resolve extras list (multi-track) ----
    # Each entry becomes (Path, volume) for the renderer. Missing files are
    # dropped with a warning; in loop mode the FIRST track is the duration
    # driver, so if it's the missing one we hard-fail (no point rendering a
    # loop without a length).
    resolved_extras: list[tuple[Path, float]] = []
    missing_extra_ids: list[str] = []
    for e in req.audio.extras:
        p = _find_extra_audio(e.id)
        if p is None:
            missing_extra_ids.append(e.id)
            continue
        resolved_extras.append((p, e.volume))
    if missing_extra_ids:
        log.warning("export.extras_missing ids=%s", missing_extra_ids)

    if req.trim.loop:
        first_id = req.audio.extras[0].id if req.audio.extras else None
        first_path = (
            resolved_extras[0][0]
            if resolved_extras and (first_id is not None and first_id not in missing_extra_ids)
            else None
        )
        if first_path is None:
            log.warning(
                "export.loop_driver_missing first_id=%s missing=%s — refusing export",
                first_id, missing_extra_ids,
            )
            raise HTTPException(
                status_code=410,
                detail=(
                    "Loop track is missing on the server (file was cleaned "
                    "up). Re-upload the first extra track or turn loop off."
                ),
            )

    # ---- loop mode (Coub-style): repeat the trimmed slice across the FIRST
    # extra track's full duration. ----
    loop_active = bool(req.trim.loop) and len(resolved_extras) > 0 and not info.is_audio_only
    loop_total_duration: float | None = None
    if loop_active:
        driver_path, _vol = resolved_extras[0]
        try:
            driver_info = probe(driver_path)
            driver_dur = float(driver_info.duration)
        except Exception as exc:
            log.warning("export.loop_probe_failed extra=%s err=%s", driver_path, exc)
            driver_dur = 0.0
        if driver_dur > 0:
            loop_clip_duration = clipped_duration  # short slice
            loop_total_duration = driver_dur
            new_duration = driver_dur
            # Source-track subtitles: stamp copies onto each iteration so each
            # loop displays them. Extra-track subtitles already ride the
            # driver extra-audio timeline and need no expansion.
            if effective_subtitle_track == "source":
                from .loop_segments import expand_loop_segments
                segments_for_render = expand_loop_segments(
                    segments_for_render,
                    trim_in=trim_in,
                    loop_clip_duration=loop_clip_duration,
                    total_duration=loop_total_duration,
                )
            log.info(
                "export.loop active clip=%.2fs total=%.2fs subtitle_track=%s",
                loop_clip_duration, loop_total_duration, effective_subtitle_track,
            )
        else:
            loop_active = False  # bail out, treat as normal export

    log.info(
        "export.render start target=%dx%d audio_only=%s duration=%.2fs trim_edges=(%.2f,%.2f) extras=%d loop=%s",
        resolved.target_w, resolved.target_h, info.is_audio_only, new_duration,
        trim_in, trim_out, len(resolved_extras), loop_active,
    )

    out = _output_path(req.video_id)
    last_pct = -10

    def on_progress(pct: int) -> None:
        nonlocal last_pct
        if pct >= last_pct + 5 or pct == 100:
            log.info("export.render percent=%d", pct)
            last_pct = pct
        ws.push(jid, {"phase": "encode", "percent": pct, "video_id": req.video_id})

    ws.push(jid, {"phase": "encode", "percent": 0, "video_id": req.video_id})

    select_expr = silence.build_select_expr(keeps) if keeps is not None else None

    has_overlay = any(
        (s.text or "").strip() for s in segments_for_render
    )
    trim_active = keeps is not None
    canvas_transform = bool(resolved.ffmpeg_filter)
    audio_mix_active = (
        len(resolved_extras) > 0 or abs(req.audio.source_volume - 1.0) > 1e-3
    )

    trim_duration_arg = clipped_duration if edge_trim_active else None

    # Watermark PNG is bundled into the Vite build output, so it lives
    # alongside other static assets after `docker compose up --build`.
    watermark_path = (STATIC_DIR / "watermark.png") if req.watermark else None
    if watermark_path is not None and not watermark_path.exists():
        log.warning("export.watermark_missing path=%s — exporting without it", watermark_path)
        watermark_path = None
    wm_active = watermark_path is not None

    # In loop mode we always need a filter pass — no stream_copy can repeat
    # frames. Trim_duration_arg here means "the short clip length", not the
    # output length.
    loop_trim_duration_arg = (
        clipped_duration if (loop_active and clipped_duration > 0) else trim_duration_arg
    )

    def do_render() -> None:
        if not has_overlay and not info.is_audio_only:
            if (
                not canvas_transform
                and not trim_active
                and not edge_trim_active
                and not audio_mix_active
                and not wm_active
                and not loop_active
            ):
                log.info("export.path stream_copy")
                try:
                    simple_export.run_stream_copy(
                        source=media, out=out, on_progress=on_progress,
                    )
                    return
                except RuntimeError as exc:
                    log.warning("export.stream_copy fallback filter_only err=%s", exc)
            log.info(
                "export.path filter_only trim_silence=%s edge_trim=%s canvas=%s audio_mix=%s loop=%s",
                trim_active, edge_trim_active, canvas_transform, audio_mix_active, loop_active,
            )
            simple_export.run_filter_only(
                source=media, out=out,
                canvas_filter=resolved.ffmpeg_filter,
                target_w=resolved.target_w,
                target_h=resolved.target_h,
                select_expr=select_expr,
                on_progress=on_progress,
                trim_in=trim_in,
                trim_duration=loop_trim_duration_arg,
                source_volume=req.audio.source_volume,
                extras=resolved_extras,
                watermark_path=watermark_path,
                source_has_audio=info.has_audio,
                loop_total_duration=loop_total_duration,
            )
            return
        log.info(
            "export.path renderer has_overlay=%s audio_only=%s loop=%s",
            has_overlay, info.is_audio_only, loop_active,
        )
        renderer.render_export(
            source=media,
            out=out,
            target_w=resolved.target_w,
            target_h=resolved.target_h,
            canvas=canvas_cfg,
            canvas_filter=resolved.ffmpeg_filter,
            segments=segments_for_render,
            style=req.style,
            position=req.position,
            size=req.size,
            duration=new_duration,
            is_audio_only=info.is_audio_only,
            select_expr=select_expr,
            on_progress=on_progress,
            trim_in=trim_in,
            trim_duration=loop_trim_duration_arg,
            source_volume=req.audio.source_volume,
            extras=resolved_extras,
            watermark=wm_active,
            source_has_audio=info.has_audio,
            loop_total_duration=loop_total_duration,
        )

    try:
        await asyncio.to_thread(do_render)
    except Exception as exc:
        log.exception("export.failed video_id=%s err=%s", req.video_id, exc)
        raise HTTPException(status_code=500, detail=f"export failed: {exc}")

    out_size = out.stat().st_size if out.exists() else 0
    elapsed = time.perf_counter() - t0
    log.info(
        "export.done video_id=%s out_size=%d (%.1f MB) elapsed=%.1fs",
        req.video_id,
        out_size,
        out_size / 1024 / 1024,
        elapsed,
    )
    # GIF post-processing: convert the rendered MP4 into a GIF using a
    # two-pass palette pipeline. The intermediate MP4 stays on disk so a
    # subsequent MP4 download still works.
    final_path: Path = out
    output_format = req.format
    if req.format == "gif":
        gif_path = _gif_output_path(req.video_id)
        ws.push(jid, {"phase": "encode", "percent": 100, "video_id": req.video_id, "stage": "gif_start"})
        log.info("export.gif start video_id=%s quality=%s", req.video_id, req.gif_quality)
        try:
            await asyncio.to_thread(_encode_gif, out, gif_path, req.gif_quality)
        except subprocess.CalledProcessError as exc:
            log.warning("export.gif_failed video_id=%s err=%s", req.video_id, exc)
            raise HTTPException(status_code=500, detail=f"gif encode failed: {exc}")
        final_path = gif_path
        gif_size = gif_path.stat().st_size if gif_path.exists() else 0
        log.info("export.gif done video_id=%s size=%d (%.1f MB)", req.video_id, gif_size, gif_size / 1024 / 1024)

    ws.push(jid, {"phase": "encode", "percent": 100, "video_id": req.video_id})

    return ExportResponse(
        video_id=req.video_id,
        output_path=str(final_path),
        output_format=output_format,
        original_duration=info.duration,
        output_duration=new_duration,
        cuts=keeps,
    )


@app.get("/api/download/{video_id}")
def api_download(video_id: str, format: str = "mp4") -> FileResponse:
    _validate_video_id(video_id)
    if format == "gif":
        out = _gif_output_path(video_id)
        media_type = "image/gif"
        ext = "gif"
    else:
        out = _output_path(video_id)
        media_type = "video/mp4"
        ext = "mp4"
    if not out.exists():
        raise HTTPException(status_code=404, detail="output not found")
    return FileResponse(out, media_type=media_type, filename=f"{video_id}.{ext}")


@app.websocket("/ws/progress/{job_id}")
async def ws_progress(websocket: WebSocket, job_id: str) -> None:
    await ws.handle(websocket, job_id)


if FONTS_DIR.exists():
    app.mount("/fonts", StaticFiles(directory=str(FONTS_DIR)), name="fonts")

if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
