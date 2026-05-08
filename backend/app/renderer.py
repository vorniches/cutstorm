"""Pixel-accurate export: render subtitle overlay frames in headless Chromium,
combine with source video/audio via ffmpeg.

Replaces the old libass-based burn for the goal of preview = export parity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from playwright.async_api import async_playwright

from .canvas import hex_to_ffmpeg_color
from .models import Canvas, Position, Segment, Size, Style
from .overlay_timing import compute_overlay_change_times

log = logging.getLogger(__name__)

ProgressCb = Callable[[int], None]

RENDER_FPS = 60
RENDER_URL = "http://127.0.0.1:8000/?render=1"


def _build_render_state(
    segments: list[Segment],
    style: Style,
    position: Position,
    size: Size,
    canvas: Canvas,
    target_w: int,
    target_h: int,
    duration: float,
    is_audio_only: bool,
    watermark: bool = False,
) -> dict:
    return {
        "dims": {"w": target_w, "h": target_h},
        "segments": [s.model_dump() for s in segments],
        "style": style.model_dump(),
        "position": position.model_dump(),
        "size": size.model_dump(),
        "canvas": canvas.model_dump(),
        "duration": duration,
        "videoW": target_w,
        "videoH": target_h,
        "isAudioOnly": is_audio_only,
        "watermark": watermark,
    }


def _dedup_enabled() -> bool:
    return os.environ.get("CUTSTORM_DEDUP", "0") != "0"


async def _capture_frames(
    state: dict,
    target_w: int,
    target_h: int,
    duration: float,
    fps: int,
    segments: list[Segment],
    style: Style,
    on_frame: Callable[[bytes], None],
    on_progress: Optional[ProgressCb] = None,
) -> int:
    """Drive headless Chromium, yield PNG bytes per frame via on_frame.

    Renders one PNG per stable overlay-content interval instead of per frame:
    `compute_overlay_change_times` gives the timestamps at which the overlay
    actually changes. Between adjacent points the PNG is identical, so we
    re-emit the cached bytes to ffmpeg's stdin for every frame in that
    interval. Set `CUTSTORM_DEDUP=0` to force the old per-frame render path.
    """
    total_frames = max(1, int(round(duration * fps)))
    last_pct = -10

    if _dedup_enabled():
        change_times = compute_overlay_change_times(segments, style, duration)
    else:
        # Legacy per-frame path: every frame timestamp is its own change point.
        change_times = [i / fps for i in range(total_frames)] + [duration]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context(
            viewport={"width": target_w, "height": target_h},
            device_scale_factor=1,
        )
        page = await context.new_page()
        await page.goto(RENDER_URL, wait_until="networkidle")
        await page.evaluate(
            "(s) => { window.__setRenderState(s); }",
            state,
        )
        await page.wait_for_selector('[data-testid="render-canvas"]')
        # Give fonts a beat to load (fontsource uses @font-face).
        await page.evaluate("() => document.fonts.ready")

        ci = 0
        cached_png: bytes | None = None
        last_rendered_at: float = -1.0
        renders = 0
        reuses = 0

        for i in range(total_frames):
            t = i / fps
            while ci + 1 < len(change_times) and change_times[ci + 1] <= t:
                ci += 1
            render_at = change_times[ci]

            if cached_png is None or render_at != last_rendered_at:
                await page.evaluate(f"window.__setFrameTime({render_at})")
                await page.wait_for_function(
                    "() => window.__renderReady === true",
                    timeout=5000,
                )
                cached_png = await page.screenshot(
                    omit_background=True,
                    type="png",
                    full_page=False,
                    animations="disabled",
                )
                last_rendered_at = render_at
                renders += 1
            else:
                reuses += 1

            on_frame(cached_png)
            if on_progress is not None:
                pct = int(i / total_frames * 100)
                if pct >= last_pct + 5:
                    on_progress(min(99, pct))
                    last_pct = pct
        log.info(
            "renderer.dedup renders=%d reuses=%d total_frames=%d change_points=%d",
            renders, reuses, total_frames, len(change_times),
        )
        await browser.close()
    return total_frames


def _ffmpeg_cmd_video(
    source: Path,
    out: Path,
    target_w: int,
    target_h: int,
    fps: int,
    canvas_filter: str,
    select_expr: str | None,
    trim_in: float = 0.0,
    trim_duration: float | None = None,
    source_volume: float = 1.0,
    extra_audio: Path | None = None,
    extra_volume: float = 1.0,
    source_has_audio: bool = True,
    loop_total_duration: float | None = None,
) -> list[str]:
    """Build ffmpeg for: source video → scale/crop + overlay PNG stream + audio.

    `loop_total_duration` (Coub mode): when set together with `trim_duration`,
    the trimmed source slice is repeated to cover this total duration via the
    `loop` / `aloop` filters. Caller is responsible for sizing the overlay
    PNG stream to match `loop_total_duration` (i.e. PNG-frames captured at
    `total_duration * fps`).
    """
    pre = ""
    if select_expr:
        pre = f"select='{select_expr}',setpts=N/FRAME_RATE/TB,"

    loop_active = (
        loop_total_duration is not None
        and loop_total_duration > 0
        and trim_duration is not None
        and trim_duration > 0
    )
    if loop_active:
        frames_in_clip = max(1, int(round(trim_duration * fps)) + 2)
        loop_video_suffix = (
            f",loop=loop=-1:size={frames_in_clip}:start=0"
            f",trim=duration={loop_total_duration:.3f}"
            f",setpts=N/FRAME_RATE/TB"
        )
        samples_in_clip = max(1, int(round(trim_duration * 48000)) + 1024)
        loop_audio_suffix = (
            f",aloop=loop=-1:size={samples_in_clip}:start=0"
            f",atrim=duration={loop_total_duration:.3f}"
            f",asetpts=N/SR/TB"
        )
    else:
        loop_video_suffix = ""
        loop_audio_suffix = ""

    if canvas_filter:
        chain = f"[0:v]{pre}{canvas_filter}{loop_video_suffix}[bg]"
    else:
        chain = f"[0:v]{pre}scale={target_w}:{target_h}{loop_video_suffix}[bg]"

    has_extra = extra_audio is not None
    # Branches that touch [0:a] must be gated on source_has_audio — otherwise
    # ffmpeg blows up with "stream specifier ':a' matches no streams" on
    # muted source videos (screen-recording etc).
    needs_audio_encode = (
        has_extra
        or (source_has_audio and abs(source_volume - 1.0) > 1e-3)
        or (source_has_audio and select_expr is not None)
        or (source_has_audio and loop_active)
    )

    if select_expr and source_has_audio:
        src_audio = (
            f"[0:a]aselect='{select_expr}',asetpts=N/SR/TB"
            f"{loop_audio_suffix},volume={source_volume:.3f}"
        )
    elif loop_active and source_has_audio:
        src_audio = (
            f"[0:a]{loop_audio_suffix.lstrip(',')}"
            f",volume={source_volume:.3f}"
        )
    else:
        src_audio = f"[0:a]volume={source_volume:.3f}"

    if has_extra and source_has_audio:
        # overlay input is index 1 (image2pipe); extra audio is index 2.
        # apad pads the SHORTER input with silence so amix doesn't end the
        # output stream early when extra audio < video clip and -shortest
        # is in effect later. Without this the encoder closes its stdin
        # mid-render and the PNG-pipe driver hits BrokenPipeError.
        audio_chain = (
            f";{src_audio},apad[a0];[2:a]volume={extra_volume:.3f},apad[a1];"
            f"[a0][a1]amix=inputs=2:duration=longest:normalize=0[a]"
        )
        audio_map = ["-map", "[a]"]
        acopy = ["-c:a", "aac", "-b:a", "192k"]
    elif has_extra:
        # Source is silent — extra is the only audio. Pad with trailing
        # silence so the audio stream lasts as long as the video; otherwise
        # ffmpeg closes its output (and the stdin PNG pipe) the moment
        # extra runs out, killing the renderer mid-encode.
        audio_chain = f";[2:a]volume={extra_volume:.3f},apad[a]"
        audio_map = ["-map", "[a]"]
        acopy = ["-c:a", "aac", "-b:a", "192k"]
    elif source_has_audio and needs_audio_encode:
        audio_chain = f";{src_audio}[a]"
        audio_map = ["-map", "[a]"]
        acopy = ["-c:a", "aac", "-b:a", "192k"]
    elif source_has_audio:
        audio_chain = ""
        audio_map = ["-map", "0:a?"]
        acopy = ["-c:a", "copy"]
    else:
        # No source audio and no extra — silent output, skip audio mapping.
        audio_chain = ""
        audio_map = ["-an"]
        acopy = []
    filter_complex = f"{chain};[bg][1:v]overlay=format=auto[v]{audio_chain}"

    cmd = ["ffmpeg", "-y", "-nostats", "-loglevel", "error"]
    if trim_in > 0.0:
        cmd += ["-ss", f"{trim_in:.3f}"]
    if trim_duration is not None and trim_duration > 0.0:
        cmd += ["-t", f"{trim_duration:.3f}"]
    cmd += ["-i", str(source)]
    cmd += [
        "-f", "image2pipe",
        "-framerate", str(fps),
        "-i", "pipe:0",
    ]
    if has_extra:
        # In loop mode the extra audio drives total length — do not cap it.
        if not loop_active and trim_duration is not None and trim_duration > 0.0:
            cmd += ["-t", f"{trim_duration:.3f}"]
        cmd += ["-i", str(extra_audio)]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        *audio_map,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "slow",
        "-crf", "16",
        *acopy,
        "-shortest",
        str(out),
    ]
    return cmd


def _ffmpeg_cmd_audio_only(
    audio: Path,
    out: Path,
    target_w: int,
    target_h: int,
    fps: int,
    bg_color: str,
    duration: float,
    select_expr: str | None,
    trim_in: float = 0.0,
    trim_duration: float | None = None,
    source_volume: float = 1.0,
    extra_audio: Path | None = None,
    extra_volume: float = 1.0,
) -> list[str]:
    """Build ffmpeg for: synthetic color bg + audio + overlay PNG stream."""
    ff_color = hex_to_ffmpeg_color(bg_color)
    color_input = f"color=c={ff_color}:s={target_w}x{target_h}:r={fps}:d={duration:.3f}"

    has_extra = extra_audio is not None

    if select_expr:
        src_audio = f"[1:a]aselect='{select_expr}',asetpts=N/SR/TB,volume={source_volume:.3f}"
    else:
        src_audio = f"[1:a]volume={source_volume:.3f}"

    # Inputs: 0=color, 1=audio, 2=image2pipe, [3=extra]
    if has_extra:
        audio_chain = (
            f"{src_audio}[a0];[3:a]volume={extra_volume:.3f}[a1];"
            f"[a0][a1]amix=inputs=2:duration=first:normalize=0[a]"
        )
    else:
        audio_chain = f"{src_audio}[a]"
    audio_map = ["-map", "[a]"]

    filter_complex = f"[0:v][2:v]overlay=format=auto[v];{audio_chain}"

    cmd = ["ffmpeg", "-y", "-nostats", "-loglevel", "error",
           "-f", "lavfi", "-i", color_input]
    if trim_in > 0.0:
        cmd += ["-ss", f"{trim_in:.3f}"]
    if trim_duration is not None and trim_duration > 0.0:
        cmd += ["-t", f"{trim_duration:.3f}"]
    cmd += ["-i", str(audio),
            "-f", "image2pipe",
            "-framerate", str(fps),
            "-i", "pipe:0"]
    if has_extra:
        if trim_duration is not None and trim_duration > 0.0:
            cmd += ["-t", f"{trim_duration:.3f}"]
        cmd += ["-i", str(extra_audio)]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        *audio_map,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "slow",
        "-crf", "16",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(out),
    ]
    return cmd


def render_export(
    source: Path,
    out: Path,
    target_w: int,
    target_h: int,
    canvas: Canvas,
    canvas_filter: str,
    segments: list[Segment],
    style: Style,
    position: Position,
    size: Size,
    duration: float,
    is_audio_only: bool,
    select_expr: str | None = None,
    on_progress: Optional[ProgressCb] = None,
    fps: int = RENDER_FPS,
    trim_in: float = 0.0,
    trim_duration: float | None = None,
    source_volume: float = 1.0,
    extra_audio: Path | None = None,
    extra_volume: float = 1.0,
    watermark: bool = False,
    source_has_audio: bool = True,
    loop_total_duration: float | None = None,
) -> None:
    """Synchronous entry. Runs Playwright frame capture + ffmpeg pipe.

    Intended to be called via asyncio.to_thread from async FastAPI handler.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    state = _build_render_state(
        segments, style, position, size, canvas,
        target_w, target_h, duration, is_audio_only,
        watermark=watermark,
    )

    if is_audio_only:
        cmd = _ffmpeg_cmd_audio_only(
            audio=source, out=out,
            target_w=target_w, target_h=target_h, fps=fps,
            bg_color=canvas.bg_color, duration=duration,
            select_expr=select_expr,
            trim_in=trim_in, trim_duration=trim_duration,
            source_volume=source_volume,
            extra_audio=extra_audio, extra_volume=extra_volume,
        )
    else:
        cmd = _ffmpeg_cmd_video(
            source=source, out=out,
            target_w=target_w, target_h=target_h, fps=fps,
            canvas_filter=canvas_filter,
            select_expr=select_expr,
            trim_in=trim_in, trim_duration=trim_duration,
            source_volume=source_volume,
            extra_audio=extra_audio, extra_volume=extra_volume,
            source_has_audio=source_has_audio,
            loop_total_duration=loop_total_duration,
        )
    log.info("renderer.ffmpeg cmd=%s", shlex.join(cmd))

    # stderr → tempfile (not PIPE) so ffmpeg's output doesn't deadlock us
    # when the OS pipe buffer fills up. We read it at the end for diagnostics.
    stderr_file = tempfile.TemporaryFile(mode="w+b")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
    )
    assert proc.stdin is not None

    frames_written = {"n": 0}
    pipe_closed = {"v": False}

    def on_frame(png: bytes) -> None:
        # Once ffmpeg has closed its stdin (e.g. it hit -shortest because
        # one of the audio streams was shorter than the video), every further
        # write raises BrokenPipeError. That's not a render failure — the
        # encoder finished cleanly with whatever frames we already piped.
        # Mark the pipe as closed and drop subsequent frames silently;
        # `_capture_frames` reads the flag (via `pipe_closed`) and stops the
        # Chromium loop early so we don't spend extra seconds rendering PNGs
        # that go nowhere.
        if pipe_closed["v"]:
            return
        try:
            proc.stdin.write(png)
            frames_written["n"] += 1
        except (BrokenPipeError, ValueError):
            pipe_closed["v"] = True

    async def _drive() -> int:
        return await _capture_frames(
            state=state,
            target_w=target_w,
            target_h=target_h,
            duration=duration,
            fps=fps,
            segments=segments,
            style=style,
            on_frame=on_frame,
            on_progress=on_progress,
        )

    err_tail = ""
    try:
        total = asyncio.run(_drive())
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        log.info(
            "renderer.frames piped=%d total=%d pipe_closed_early=%s — waiting for ffmpeg mux",
            frames_written["n"], total, pipe_closed["v"],
        )
        proc.wait()
        log.info("renderer.ffmpeg exit=%d", proc.returncode)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        raise
    finally:
        stderr_file.seek(0)
        err_tail = stderr_file.read().decode("utf-8", errors="replace")[-3000:]
        stderr_file.close()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (code {proc.returncode}):\n{err_tail}")
    if on_progress is not None:
        on_progress(100)
