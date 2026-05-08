"""Fast-path exporters used when the full Chromium-driven renderer isn't needed.

Three-way dispatch lives in `main.api_export`:

- `run_stream_copy` — source as-is: `ffmpeg -c copy`. Near-instant, zero
  re-encode. Chosen when canvas is a no-op, there's no trim, no overlay, and
  the input is not audio-only.
- `run_filter_only` — canvas and/or trim but no overlay text. Still
  re-encodes video, but skips Chromium entirely.
- Full renderer lives in `renderer.render_export` (case C).
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

ProgressCb = Callable[[int], None]


def run_stream_copy(
    source: Path,
    out: Path,
    on_progress: Optional[ProgressCb] = None,
) -> None:
    """Case A: canvas=source, no trim, no overlay, not audio-only."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-nostats", "-loglevel", "error",
        "-i", str(source),
        "-c", "copy",
        str(out),
    ]
    log.info("simple_export.stream_copy cmd=%s", " ".join(cmd))
    _run(cmd)
    if on_progress is not None:
        on_progress(100)


def run_filter_only(
    source: Path,
    out: Path,
    canvas_filter: str,
    target_w: int,
    target_h: int,
    select_expr: str | None,
    on_progress: Optional[ProgressCb] = None,
    trim_in: float = 0.0,
    trim_duration: float | None = None,
    source_volume: float = 1.0,
    extras: list[tuple[Path, float]] | None = None,
    watermark_path: Path | None = None,
    source_has_audio: bool = True,
    loop_total_duration: float | None = None,
    fps: int = 30,
) -> None:
    """Case B: canvas transform and/or trim, but no subtitle overlay.

    Re-encodes video through the same libx264 settings as the renderer path
    (crf=16, preset=slow, yuv420p) so outputs stay visually consistent.

    Inputs:
        [0:v] / [0:a]   — source video / source audio
        [1:a], [2:a]... — extras (in order)
        [N+1:v]         — watermark PNG (if enabled), N = len(extras)
    """
    extras = list(extras or [])
    n_extras = len(extras)
    out.parent.mkdir(parents=True, exist_ok=True)

    pre = f"select='{select_expr}',setpts=N/FRAME_RATE/TB," if select_expr else ""
    chain_main = canvas_filter if canvas_filter else f"scale={target_w}:{target_h}"

    loop_active = (
        loop_total_duration is not None
        and loop_total_duration > 0
        and trim_duration is not None
        and trim_duration > 0
    )
    loop_video_suffix = ""
    loop_audio_suffix = ""
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

    has_watermark = watermark_path is not None and watermark_path.exists()
    if has_watermark:
        # Watermark is the LAST input, after all extras.
        wm_input_idx = 1 + n_extras
        wm_w = max(1, int(target_w * 0.16))
        margin_x = max(1, int(target_w * 0.02))
        margin_y = max(1, int(target_h * 0.03))
        video_chain = (
            f"[0:v]{pre}{chain_main}{loop_video_suffix}[vbase];"
            f"[{wm_input_idx}:v]scale={wm_w}:-1[wm];"
            f"[vbase][wm]overlay=W-w-{margin_x}:H-h-{margin_y}:shortest=1:repeatlast=0[v]"
        )
    else:
        video_chain = f"[0:v]{pre}{chain_main}{loop_video_suffix}[v]"

    needs_audio_encode = (
        n_extras > 0
        or (source_has_audio and abs(source_volume - 1.0) > 1e-3)
        or (source_has_audio and select_expr is not None)
        or (source_has_audio and loop_active)
    )

    audio_lanes: list[str] = []
    audio_labels: list[str] = []
    if source_has_audio and needs_audio_encode:
        if select_expr:
            src_chain = (
                f"[0:a]aselect='{select_expr}',asetpts=N/SR/TB"
                f"{loop_audio_suffix},volume={source_volume:.3f},apad[a_src]"
            )
        elif loop_active:
            src_chain = (
                f"[0:a]{loop_audio_suffix.lstrip(',')}"
                f",volume={source_volume:.3f},apad[a_src]"
            )
        else:
            src_chain = f"[0:a]volume={source_volume:.3f},apad[a_src]"
        audio_lanes.append(src_chain)
        audio_labels.append("[a_src]")

    for i, (_p, vol) in enumerate(extras):
        idx = 1 + i  # extras start at input index 1 (after source)
        label = f"[a_e{i}]"
        audio_lanes.append(f"[{idx}:a]volume={vol:.3f},apad{label}")
        audio_labels.append(label)

    if not audio_labels:
        audio_filter = ""
        audio_map = ["-an"]
    elif len(audio_labels) == 1:
        only = audio_lanes[0]
        renamed = only.rsplit("[", 1)[0] + "[a]"
        audio_filter = ";" + renamed
        audio_map = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    else:
        amix_in = "".join(audio_labels)
        audio_filter = (
            ";" + ";".join(audio_lanes)
            + f";{amix_in}amix=inputs={len(audio_labels)}:duration=longest:normalize=0[a]"
        )
        audio_map = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]

    if not needs_audio_encode and source_has_audio:
        # Copy source audio stream through unchanged.
        audio_filter = ""
        audio_map = ["-map", "0:a?", "-c:a", "copy"]

    filter_complex = video_chain + audio_filter

    cmd = ["ffmpeg", "-y", "-nostats", "-loglevel", "error"]
    if trim_in > 0.0:
        cmd += ["-ss", f"{trim_in:.3f}"]
    if trim_duration is not None and trim_duration > 0.0:
        cmd += ["-t", f"{trim_duration:.3f}"]
    cmd += ["-i", str(source)]
    for path, _vol in extras:
        # In loop mode the FIRST extra drives total length, do not cap any
        # extra. In non-loop mode each extra is padded by `apad`, so capping
        # would needlessly truncate them — skip the -t too.
        cmd += ["-i", str(path)]
    if has_watermark:
        cmd += ["-loop", "1", "-i", str(watermark_path)]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]", *audio_map,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "slow",
        "-crf", "16",
    ]
    # -shortest keeps output bounded: the watermark loop / aloop chains
    # are infinite, the source video and (in loop mode) the looped video
    # chain are not. Without -shortest the encoder would never reach EOF.
    cmd += ["-shortest", str(out)]
    log.info("simple_export.filter_only cmd=%s", " ".join(cmd))
    _run(cmd)
    if on_progress is not None:
        on_progress(100)


def _run(cmd: list[str]) -> None:
    stderr_file = tempfile.TemporaryFile(mode="w+b")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_file)
        rc = proc.wait()
        stderr_file.seek(0)
        err = stderr_file.read().decode("utf-8", errors="replace")[-3000:]
    finally:
        stderr_file.close()
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed (code {rc}):\n{err}")
