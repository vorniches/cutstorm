import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  attachAudioMix,
  getAudioMix,
  resumeAudioContext,
  setExtraVolume,
  setSourceVolume,
  syncExtraToVideo,
  syncVideoToLoopedExtra,
} from "../audioMix";
import { resolveCanvas } from "../canvas";
import { getExtraAudioPlaybackUrl } from "../extraBlobs";
import { useStore } from "../store";
import { CropEditor } from "./CropEditor";
import { PreviewToolbar } from "./PreviewToolbar";
import { SubtitleOverlay } from "./SubtitleOverlay";
import { Watermark } from "./Watermark";
import { Timeline } from "./Timeline";

export function VideoPreview() {
  const videoUrl = useStore((s) => s.videoUrl);
  const setCurrentTime = useStore((s) => s.setCurrentTime);
  const setVideoEl = useStore((s) => s.setVideoEl);
  const canvas = useStore((s) => s.canvas);
  const videoW = useStore((s) => s.videoW);
  const videoH = useStore((s) => s.videoH);
  const useSubs = useStore((s) => s.useSubs);
  const watermark = useStore((s) => s.watermark);
  const trimRange = useStore((s) => s.trimRange);
  const sourceVolume = useStore((s) => s.audio.sourceVolume);
  const extras = useStore((s) => s.audio.extras);
  const duration = useStore((s) => s.duration);
  const videoRef = useRef<HTMLVideoElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);

  // Loop driver = first extra. Its duration sets the master timeline.
  const driver = extras[0];
  const trimOut = trimRange.out_sec > 0 ? trimRange.out_sec : duration;
  const loopClipDuration = Math.max(0, trimOut - trimRange.in_sec);
  const loopActive =
    !!trimRange.loop &&
    !!driver &&
    driver.duration > 0 &&
    loopClipDuration > 0;

  const resolved = resolveCanvas(canvas, videoW, videoH, false);
  const frameW = canvas.mode === "custom" ? (videoW || resolved.targetW) : resolved.targetW;
  const frameH = canvas.mode === "custom" ? (videoH || resolved.targetH) : resolved.targetH;

  useLayoutEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      const s = Math.min(r.width / frameW, r.height / frameH);
      setScale(Math.max(0.01, s));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [frameW, frameH]);

  useEffect(() => {
    if (videoRef.current && videoUrl) {
      videoRef.current.load();
    }
    setVideoEl(videoRef.current);
    return () => setVideoEl(null);
  }, [videoUrl, setVideoEl]);

  // Build / rebuild the WebAudio mix graph when the <video> element or
  // the set of extra tracks changes (add/remove/reorder/url change).
  // `attachAudioMix` reconciles incrementally — already-attached tracks
  // stay connected, only deltas change.
  const extrasResolved = useMemo(
    () => extras
      .map((e) => {
        const url = getExtraAudioPlaybackUrl(e.id);
        return url ? { id: e.id, url, volume: e.volume } : null;
      })
      .filter((x): x is { id: string; url: string; volume: number } => x !== null),
    [extras],
  );
  const extrasResolvedKey = extrasResolved.map((e) => `${e.id}|${e.url}`).join(";");

  useEffect(() => {
    const v = videoRef.current;
    if (!v || !videoUrl) return;
    const mix = attachAudioMix(v, extrasResolved);
    mix.srcGain.gain.value = Math.max(0, Math.min(2, sourceVolume));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoUrl, extrasResolvedKey]);

  useEffect(() => { setSourceVolume(sourceVolume); }, [sourceVolume]);
  // Push every track's individual volume on each change.
  useEffect(() => {
    for (const e of extras) {
      setExtraVolume(e.id, e.volume);
    }
  }, [extras]);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (loopActive) return; // loop branch in the next effect owns the clock.
    const onTime = () => {
      if (trimRange.in_sec > 0 && v.currentTime < trimRange.in_sec - 0.05) {
        v.currentTime = trimRange.in_sec;
      }
      if (trimRange.out_sec > 0 && v.currentTime > trimRange.out_sec) {
        v.pause();
        v.currentTime = trimRange.out_sec;
      }
      setCurrentTime(v.currentTime);
      syncExtraToVideo(v, trimRange.in_sec);
    };
    const onPlay = () => {
      if (trimRange.in_sec > 0 && v.currentTime < trimRange.in_sec) {
        v.currentTime = trimRange.in_sec;
      }
      resumeAudioContext();
      syncExtraToVideo(v, trimRange.in_sec);
    };
    const onPause = () => syncExtraToVideo(v, trimRange.in_sec);
    const onExtraReady = () => syncExtraToVideo(v, trimRange.in_sec);
    v.addEventListener("timeupdate", onTime);
    v.addEventListener("seeked", onTime);
    v.addEventListener("play", onPlay);
    v.addEventListener("pause", onPause);
    window.addEventListener("cutstorm:extra-ready", onExtraReady);
    onTime();
    return () => {
      v.removeEventListener("timeupdate", onTime);
      v.removeEventListener("seeked", onTime);
      v.removeEventListener("play", onPlay);
      v.removeEventListener("pause", onPause);
      window.removeEventListener("cutstorm:extra-ready", onExtraReady);
    };
  }, [setCurrentTime, videoUrl, trimRange.in_sec, trimRange.out_sec, loopActive]);

  // Loop-mode preview: the driver track (extras[0]) is the master clock.
  // The video is reseated every animation frame to
  // `trimIn + (driverTime % loopClipDur)`. Every other extra rides the
  // driver clock too (handled inside `syncVideoToLoopedExtra`).
  useEffect(() => {
    if (!loopActive || !driver) return;
    const v = videoRef.current;
    if (!v) return;
    const mix = getAudioMix();
    if (!mix?.extras.get(driver.id)) return;
    const driverEl = mix.extras.get(driver.id)!.el;
    let raf = 0;
    let alive = true;

    const tick = () => {
      if (!alive) return;
      const r = syncVideoToLoopedExtra(v, trimRange.in_sec, loopClipDuration, driver.id);
      if (r) {
        setCurrentTime(r.master);
        if (driver.duration > 0 && r.master >= driver.duration - 0.02) {
          // Reached the end of the soundtrack — stop everything; play
          // restarts from 0 next time.
          if (!v.paused) v.pause();
          for (const node of mix.extras.values()) {
            if (!node.el.paused) node.el.pause();
          }
          try { driverEl.currentTime = 0; } catch { /* */ }
          return;
        }
      }
      raf = requestAnimationFrame(tick);
    };

    const onPlay = () => {
      resumeAudioContext();
      const r = syncVideoToLoopedExtra(v, trimRange.in_sec, loopClipDuration, driver.id);
      if (r) setCurrentTime(r.master);
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(tick);
    };
    const onPause = () => {
      for (const node of mix.extras.values()) {
        if (!node.el.paused) node.el.pause();
      }
      cancelAnimationFrame(raf);
    };
    v.addEventListener("play", onPlay);
    v.addEventListener("pause", onPause);
    raf = requestAnimationFrame(tick);
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
      v.removeEventListener("play", onPlay);
      v.removeEventListener("pause", onPause);
    };
  }, [
    loopActive, driver?.id, driver?.duration,
    trimRange.in_sec, loopClipDuration,
    setCurrentTime, videoUrl,
  ]);

  if (!videoUrl) return null;

  return (
    <div className="pane preview-pane">
      <div className="preview-stage" ref={stageRef}>
        <div
          style={{
            width: frameW * scale + "px",
            height: frameH * scale + "px",
            position: "relative",
          }}
        >
          <div
            className="preview-frame"
            data-testid="preview-wrap"
            data-canvas-mode={canvas.mode}
            style={{
              width: frameW + "px",
              height: frameH + "px",
              maxWidth: "none",
              maxHeight: "none",
              transform: `scale(${scale})`,
              transformOrigin: "top left",
              position: "absolute",
              top: 0,
              left: 0,
            }}
          >
            <video
              ref={videoRef}
              src={videoUrl}
              preload="metadata"
              data-testid="preview-video"
              style={{
                width: "100%",
                height: "100%",
                objectFit: resolved.sourceFit,
                objectPosition: resolved.sourceObjectPosition,
                background: canvas.bg_color,
              }}
            />
            {canvas.mode === "custom" ? (
              <CropEditor videoRef={videoRef as React.RefObject<HTMLMediaElement>} />
            ) : (
              useSubs && <SubtitleOverlay videoRef={videoRef as React.RefObject<HTMLMediaElement>} />
            )}
            {canvas.mode !== "custom" && watermark && <Watermark />}
          </div>
        </div>
      </div>
      <PreviewToolbar />
      <Timeline />
    </div>
  );
}
