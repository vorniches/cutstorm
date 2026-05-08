import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  attachAudioMix,
  resumeAudioContext,
  setExtraVolume,
  setSourceVolume,
  syncExtraToVideo,
} from "../audioMix";
import { getExtraAudioPlaybackUrl } from "../extraBlobs";
import { useStore } from "../store";
import { PreviewToolbar } from "./PreviewToolbar";
import { SubtitleOverlay } from "./SubtitleOverlay";
import { Watermark } from "./Watermark";
import { Timeline } from "./Timeline";

const PRESET_TARGETS: Record<string, [number, number]> = {
  "9:16": [1080, 1920],
  "16:9": [1920, 1080],
  "1:1": [1080, 1080],
  "4:5": [1080, 1350],
};

export function AudioPreview() {
  const audioUrl = useStore((s) => s.videoUrl);
  const setCurrentTime = useStore((s) => s.setCurrentTime);
  const setVideoEl = useStore((s) => s.setVideoEl);
  const canvas = useStore((s) => s.canvas);
  const useSubs = useStore((s) => s.useSubs);
  const watermark = useStore((s) => s.watermark);
  const trimRange = useStore((s) => s.trimRange);
  const sourceVolume = useStore((s) => s.audio.sourceVolume);
  const extras = useStore((s) => s.audio.extras);
  const audioRef = useRef<HTMLAudioElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);

  const presetKey = canvas.preset === "source" ? "9:16" : canvas.preset;
  const [targetW, targetH] = PRESET_TARGETS[presetKey] ?? [1080, 1920];

  useLayoutEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      const s = Math.min(r.width / targetW, r.height / targetH);
      setScale(Math.max(0.01, s));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [targetW, targetH]);

  useEffect(() => {
    if (audioRef.current && audioUrl) {
      audioRef.current.load();
    }
    setVideoEl(audioRef.current);
    return () => setVideoEl(null);
  }, [audioUrl, setVideoEl]);

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
    const a = audioRef.current;
    if (!a || !audioUrl) return;
    const mix = attachAudioMix(a, extrasResolved);
    mix.srcGain.gain.value = Math.max(0, Math.min(2, sourceVolume));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [audioUrl, extrasResolvedKey]);

  useEffect(() => { setSourceVolume(sourceVolume); }, [sourceVolume]);
  useEffect(() => {
    for (const e of extras) setExtraVolume(e.id, e.volume);
  }, [extras]);

  useEffect(() => {
    const a = audioRef.current;
    if (!a) return;
    const onTime = () => {
      if (trimRange.in_sec > 0 && a.currentTime < trimRange.in_sec - 0.05) {
        a.currentTime = trimRange.in_sec;
      }
      if (trimRange.out_sec > 0 && a.currentTime > trimRange.out_sec) {
        a.pause();
        a.currentTime = trimRange.out_sec;
      }
      setCurrentTime(a.currentTime);
      syncExtraToVideo(a, trimRange.in_sec);
    };
    const onPlay = () => {
      if (trimRange.in_sec > 0 && a.currentTime < trimRange.in_sec) {
        a.currentTime = trimRange.in_sec;
      }
      resumeAudioContext();
      syncExtraToVideo(a, trimRange.in_sec);
    };
    const onPause = () => syncExtraToVideo(a, trimRange.in_sec);
    const onExtraReady = () => syncExtraToVideo(a, trimRange.in_sec);
    a.addEventListener("timeupdate", onTime);
    a.addEventListener("seeked", onTime);
    a.addEventListener("play", onPlay);
    a.addEventListener("pause", onPause);
    window.addEventListener("cutstorm:extra-ready", onExtraReady);
    onTime();
    return () => {
      a.removeEventListener("timeupdate", onTime);
      a.removeEventListener("seeked", onTime);
      a.removeEventListener("play", onPlay);
      a.removeEventListener("pause", onPause);
      window.removeEventListener("cutstorm:extra-ready", onExtraReady);
    };
  }, [setCurrentTime, audioUrl, trimRange.in_sec, trimRange.out_sec]);

  if (!audioUrl) return null;

  return (
    <div className="pane preview-pane">
      <div className="preview-stage" ref={stageRef}>
        <div
          style={{
            width: targetW * scale + "px",
            height: targetH * scale + "px",
            position: "relative",
          }}
        >
          <div
            className="preview-frame"
            data-testid="preview-wrap"
            data-audio-only="1"
            style={{
              width: targetW + "px",
              height: targetH + "px",
              maxWidth: "none",
              maxHeight: "none",
              background: canvas.bg_color,
              transform: `scale(${scale})`,
              transformOrigin: "top left",
              position: "absolute",
              top: 0,
              left: 0,
            }}
          >
            <audio ref={audioRef} src={audioUrl} preload="metadata" />
            {useSubs && <SubtitleOverlay videoRef={audioRef} />}
            {watermark && <Watermark />}
          </div>
        </div>
      </div>
      <PreviewToolbar />
      <Timeline />
    </div>
  );
}
