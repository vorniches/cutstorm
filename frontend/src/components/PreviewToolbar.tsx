import { useEffect, useState } from "react";
import { getAudioMix, resumeAudioContext } from "../audioMix";
import { useStore } from "../store";

/**
 * Custom video/audio controls rendered BELOW the preview frame (not inside
 * the media element). Native `<video controls>` overlays the bottom strip
 * and collides with the crop editor in custom-crop mode — this toolbar sits
 * outside the render area so controls stay reachable at every zoom.
 *
 * Includes: play/pause, stop (seek back to trim-in), scrubbable progress bar
 * constrained to the keep-range. Volume is intentionally omitted — the
 * Timeline below hosts per-track volume controls.
 */
export function PreviewToolbar() {
  const videoEl = useStore((s) => s.videoEl);
  const duration = useStore((s) => s.duration);
  const currentTime = useStore((s) => s.currentTime);
  const trimRange = useStore((s) => s.trimRange);
  const extras = useStore((s) => s.audio.extras);
  const [playing, setPlaying] = useState(false);

  // Loop driver = first extra. Its duration sets the master timeline.
  const driver = extras[0];
  const driverDuration = driver?.duration ?? 0;

  const trimIn = Math.max(0, trimRange.in_sec);
  const trimOut = trimRange.out_sec > 0 ? Math.min(trimRange.out_sec, duration || 0) : (duration || 0);
  const loopClipDuration = Math.max(0, trimOut - trimIn);
  const loopActive =
    !!trimRange.loop &&
    !!driver &&
    driverDuration > 0 &&
    loopClipDuration > 0;
  // In loop mode the master timeline is the driver track (0 .. driverDur);
  // currentTime already mirrors driver.currentTime via the rAF loop.
  const effectiveDuration = loopActive
    ? Math.max(0.01, driverDuration)
    : Math.max(0.01, trimOut - trimIn);
  const progressVal = loopActive
    ? Math.max(0, Math.min(effectiveDuration, currentTime))
    : Math.max(0, Math.min(effectiveDuration, currentTime - trimIn));

  useEffect(() => {
    const v = videoEl;
    if (!v) { setPlaying(false); return; }
    setPlaying(!v.paused);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    v.addEventListener("play", onPlay);
    v.addEventListener("pause", onPause);
    return () => {
      v.removeEventListener("play", onPlay);
      v.removeEventListener("pause", onPause);
    };
  }, [videoEl]);

  function togglePlay() {
    const v = videoEl;
    if (!v) return;
    // Resume the AudioContext INSIDE the click handler (user-gesture tick),
    // otherwise Chromium keeps it suspended and the extra track silently
    // drops all samples.
    resumeAudioContext();
    if (v.paused) v.play().catch(() => {});
    else v.pause();
  }

  function stop() {
    const v = videoEl;
    if (!v) return;
    v.pause();
    if (loopActive && driver) {
      const driverEl = getAudioMix()?.extras.get(driver.id)?.el;
      if (driverEl) try { driverEl.currentTime = 0; } catch { /* */ }
      v.currentTime = trimIn;
      return;
    }
    v.currentTime = trimIn;
  }

  function onSeek(e: React.ChangeEvent<HTMLInputElement>) {
    const v = videoEl;
    if (!v) return;
    const rel = Number(e.target.value);
    if (loopActive && driver) {
      const driverEl = getAudioMix()?.extras.get(driver.id)?.el;
      if (driverEl) {
        const m = Math.max(0, Math.min(driverDuration, rel));
        try { driverEl.currentTime = m; } catch { /* */ }
        // Re-seat video into phase immediately for visual responsiveness.
        const phase = loopClipDuration > 0 ? (m % loopClipDuration) : 0;
        try { v.currentTime = trimIn + phase; } catch { /* */ }
      }
      return;
    }
    v.currentTime = Math.max(trimIn, Math.min(trimOut, trimIn + rel));
  }

  if (!videoEl) return null;

  return (
    <div className="player-toolbar" data-testid="player-toolbar">
      <button
        type="button"
        className="player-btn"
        data-testid="player-play"
        aria-label={playing ? "Pause" : "Play"}
        onClick={togglePlay}
      >
        {playing ? (
          <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden>
            <rect x="6" y="5" width="4" height="14" rx="1" />
            <rect x="14" y="5" width="4" height="14" rx="1" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden>
            <path d="M7 5 L19 12 L7 19 Z" />
          </svg>
        )}
      </button>
      <button
        type="button"
        className="player-btn"
        data-testid="player-stop"
        aria-label="Stop (return to trim in)"
        title="Stop — rewind to trim in"
        onClick={stop}
      >
        <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden>
          <rect x="6" y="6" width="12" height="12" rx="1" />
        </svg>
      </button>
      <input
        type="range"
        className="player-scrub"
        data-testid="player-scrub"
        min={0}
        max={effectiveDuration}
        step={0.05}
        value={progressVal}
        onChange={onSeek}
        aria-label="Seek"
      />
      <span className="player-time" data-testid="player-time">
        {fmt(currentTime)}{" "}
        <span className="player-time-sep">/</span>{" "}
        {fmt(loopActive ? driverDuration : duration)}
      </span>
    </div>
  );
}

function fmt(t: number): string {
  if (!Number.isFinite(t) || t < 0) t = 0;
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}
