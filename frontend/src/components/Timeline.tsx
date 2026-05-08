import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelTranscribeExtra,
  transcribeExtra,
  uploadExtraAudio,
} from "../api";
import { clearExtraBlob, getExtraBlob, setExtraBlob } from "../extraBlobs";
import { newJobId, openProgressWs } from "../progress";
import type { ExtraTrack as ExtraTrackData } from "../store";
import { useStore } from "../store";
import { computePeaks } from "../waveform";

function fmt(t: number): string {
  if (!Number.isFinite(t) || t < 0) t = 0;
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  const cs = Math.floor((t - Math.floor(t)) * 100);
  return `${m}:${s.toString().padStart(2, "0")}.${cs.toString().padStart(2, "0")}`;
}

const THUMB_COUNT = 40;
const THUMB_WIDTH = 160;

export function Timeline() {
  const duration = useStore((s) => s.duration);
  const videoUrl = useStore((s) => s.videoUrl);
  const videoId = useStore((s) => s.videoId);
  const isAudioOnly = useStore((s) => s.isAudioOnly);
  const currentTime = useStore((s) => s.currentTime);
  const trimRange = useStore((s) => s.trimRange);
  const setTrimRange = useStore((s) => s.setTrimRange);
  const audio = useStore((s) => s.audio);
  const setAudio = useStore((s) => s.setAudio);
  const setError = useStore((s) => s.setError);
  const setLoop = useStore((s) => s.setLoop);

  if (!videoUrl || !duration) return null;

  const outSec = trimRange.out_sec > 0 ? trimRange.out_sec : duration;
  const inSec = trimRange.in_sec;
  const kept = Math.max(0, outSec - inSec);
  const thumbsUrl = videoId && !isAudioOnly
    ? `/api/thumbnails/${videoId}?count=${THUMB_COUNT}&width=${THUMB_WIDTH}`
    : null;
  const driver = audio.extras[0];
  const loopArmed = !!trimRange.loop;
  const loopActive = loopArmed && !!driver && driver.duration > 0;

  return (
    <div className="timeline" data-testid="timeline">
      <TrimBar
        duration={duration}
        inSec={inSec}
        outSec={outSec}
        currentTime={currentTime}
        outStored={trimRange.out_sec}
        thumbsUrl={thumbsUrl}
        thumbCount={THUMB_COUNT}
        onChange={(patch) => setTrimRange(patch)}
      />
      <div className="timeline-meta" data-testid="timeline-meta">
        <span>{fmt(inSec)}</span>
        <span style={{ opacity: 0.4 }}>—</span>
        <span>{fmt(outSec)}</span>
        <span style={{ opacity: 0.4 }}>·</span>
        <span>{kept.toFixed(2)}s kept</span>
        <span style={{ opacity: 0.4 }}>·</span>
        <label className="loop-toggle" data-testid="loop-toggle-label" title="Loop the selected slice across the first extra audio's full duration (Coub mode)">
          <input
            type="checkbox"
            data-testid="loop-toggle"
            checked={loopArmed}
            onChange={(e) => setLoop(e.target.checked)}
          />
          <span>Loop</span>
          {loopActive && (
            <span className="loop-target" data-testid="loop-target">
              → {driver.duration.toFixed(1)}s
            </span>
          )}
          {loopArmed && !loopActive && (
            <span className="loop-hint" data-testid="loop-hint">(needs extra audio)</span>
          )}
        </label>
      </div>

      <SourceTrack
        videoId={videoId}
        volume={audio.sourceVolume}
        onVolume={(v) => setAudio({ sourceVolume: v })}
        currentTime={currentTime}
        duration={duration}
        inSec={inSec}
        outSec={outSec}
      />
      {audio.extras.map((track, i) => (
        <ExtraTrackRow
          key={track.id}
          track={track}
          index={i}
          isDriver={i === 0}
          duration={duration}
          setError={setError}
        />
      ))}
      <AddExtraTrackButton setError={setError} />
    </div>
  );
}

// ---------- Trim handles ----------

function TrimBar({
  duration,
  inSec,
  outSec,
  currentTime,
  outStored,
  thumbsUrl,
  onChange,
}: {
  duration: number;
  inSec: number;
  outSec: number;
  currentTime: number;
  outStored: number;
  thumbsUrl: string | null;
  thumbCount: number;
  onChange: (patch: { in_sec?: number; out_sec?: number }) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ which: "in" | "out"; startX: number; startIn: number; startOut: number } | null>(null);

  const onPointerMove = useCallback((e: PointerEvent) => {
    const d = dragRef.current;
    const root = rootRef.current;
    if (!d || !root) return;
    const rect = root.getBoundingClientRect();
    if (rect.width <= 0) return;
    const dx = (e.clientX - d.startX) / rect.width;
    const dt = dx * duration;
    if (d.which === "in") {
      let next = Math.max(0, Math.min(duration - 0.1, d.startIn + dt));
      const cap = outStored > 0 ? d.startOut : duration;
      next = Math.min(next, cap - 0.1);
      onChange({ in_sec: next });
    } else {
      let next = Math.max(d.startIn + 0.1, Math.min(duration, d.startOut + dt));
      onChange({ out_sec: next });
    }
  }, [duration, outStored, onChange]);

  const onPointerUp = useCallback(() => {
    dragRef.current = null;
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
  }, [onPointerMove]);

  function startDrag(which: "in" | "out") {
    return (e: React.PointerEvent) => {
      e.preventDefault();
      e.stopPropagation();
      dragRef.current = {
        which,
        startX: e.clientX,
        startIn: inSec,
        startOut: outStored > 0 ? outStored : duration,
      };
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", onPointerUp);
    };
  }

  const inPct = Math.max(0, Math.min(100, (inSec / duration) * 100));
  const outPct = Math.max(0, Math.min(100, (outSec / duration) * 100));
  const ctPct = Math.max(0, Math.min(100, (currentTime / duration) * 100));

  const thumbStyle: React.CSSProperties = thumbsUrl
    ? {
        backgroundImage: `url(${thumbsUrl})`,
        backgroundRepeat: "no-repeat",
        backgroundSize: "100% 100%",
      }
    : {};

  return (
    <div className="trim-bar" data-testid="trim-bar" ref={rootRef}>
      <div className="trim-thumbs" style={thumbStyle} />
      <div className="trim-dim trim-dim-left" style={{ width: `${inPct}%` }} />
      <div
        className="trim-dim trim-dim-right"
        style={{ left: `${outPct}%`, width: `${Math.max(0, 100 - outPct)}%` }}
      />
      <div
        className="trim-keep-frame"
        style={{ left: `${inPct}%`, width: `${Math.max(0, outPct - inPct)}%` }}
      />
      <div className="trim-playhead" style={{ left: `${ctPct}%` }} data-testid="trim-playhead" />
      <div
        className="trim-handle trim-handle-in"
        style={{ left: `${inPct}%` }}
        onPointerDown={startDrag("in")}
        data-testid="trim-handle-in"
        title={`In ${inSec.toFixed(2)}s`}
      />
      <div
        className="trim-handle trim-handle-out"
        style={{ left: `${outPct}%` }}
        onPointerDown={startDrag("out")}
        data-testid="trim-handle-out"
        title={`Out ${outSec.toFixed(2)}s`}
      />
    </div>
  );
}

// ---------- Source waveform + volume ----------

function SourceTrack({
  videoId,
  volume,
  onVolume,
  currentTime,
  duration,
  inSec,
  outSec,
}: {
  videoId: string | null;
  volume: number;
  onVolume: (v: number) => void;
  currentTime: number;
  duration: number;
  inSec: number;
  outSec: number;
}) {
  const peaks = useServerPeaks(videoId);
  const inPct = (inSec / duration) * 100;
  const outPct = (outSec / duration) * 100;
  const ctPct = (currentTime / duration) * 100;
  return (
    <div className="track-row" data-testid="source-track">
      <div className="track-label">
        <span className="track-label-text">Audio</span>
        <VolumeSlider value={volume} onChange={onVolume} testId="source-volume" />
      </div>
      <div className="track-body">
        <WaveformBar
          peaks={peaks}
          widthPct={100}
          inPct={inPct}
          outPct={outPct}
          currentPct={ctPct}
        />
      </div>
    </div>
  );
}

// ---------- Extra track row (one per track) ----------

function ExtraTrackRow({
  track,
  isDriver,
  duration,
  setError,
}: {
  track: ExtraTrackData;
  index: number;
  isDriver: boolean;
  duration: number;
  setError: (msg: string | null) => void;
}) {
  const peakKey = `extra:${track.id}`;
  const blobUrl = useExtraBlobUrl(track.id);
  const serverPeaks = useServerExtraPeaks(track.id);
  const decodedPeaks = useWaveform(peakKey, blobUrl);

  const setExtraTrack = useStore((s) => s.setExtraTrack);
  const removeExtraTrack = useStore((s) => s.removeExtraTrack);
  const setExtraSegments = useStore((s) => s.setExtraSegments);
  const setSubtitleTrack = useStore((s) => s.setSubtitleTrack);
  const setExtraSubsStreamingId = useStore((s) => s.setExtraSubsStreamingId);
  const setProgress = useStore((s) => s.setProgress);
  const setJobId = useStore((s) => s.setJobId);
  const extraSubsStreamingId = useStore((s) => s.extraSubsStreamingId);
  const segmentsExtra = useStore((s) => s.segmentsExtra);

  const isStreaming = extraSubsStreamingId === track.id;
  const someoneElseStreaming = extraSubsStreamingId !== null && !isStreaming;
  const segs = segmentsExtra[track.id] ?? [];

  // Rehydrate name/duration from /info on mount when missing (after reload).
  useEffect(() => {
    if (track.duration > 0 && track.name) return;
    let cancelled = false;
    fetch(`/api/extra-audio/${encodeURIComponent(track.id)}/info`)
      .then((r) => (r.ok ? r.json() : null))
      .then((info) => {
        if (cancelled || !info) return;
        setExtraTrack(track.id, {
          duration: Number(info.duration) || 0,
          name: track.name ?? `extra.${info.ext ?? "audio"}`,
        });
      })
      .catch(() => { /* */ });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [track.id]);

  function clear() {
    clearExtraBlob(track.id);
    removeExtraTrack(track.id);
    setExtraSegments(track.id, []);
  }

  async function onTranscribeExtra() {
    setExtraSegments(track.id, []);
    setSubtitleTrack(track.id);
    setExtraSubsStreamingId(track.id);
    setProgress("transcribe", 0);
    setError(null);

    const jobId = newJobId();
    setJobId(jobId);
    let ws: WebSocket | null = null;
    try {
      ws = await openProgressWs(jobId);
      const res = await transcribeExtra(track.id, { language: "en", jobId });
      if (Array.isArray(res.segments) && res.segments.length > 0) {
        useStore.getState().mergeExtraSegments(track.id, res.segments);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setExtraSubsStreamingId(null);
      setProgress("idle", 0);
      setJobId(null);
    } finally {
      if (useStore.getState().extraSubsStreamingId !== track.id) {
        try { ws?.close(); } catch { /* */ }
      }
    }
  }

  function onCancelExtraTranscribe() {
    void cancelTranscribeExtra(track.id);
    // progress.ts clears extraSubsStreamingId on extra_transcribe_cancelled.
  }

  const widthPct = Math.min(100, (track.duration / Math.max(0.01, duration)) * 100);
  const hasExtraSubs = segs.length > 0;
  return (
    <div className="track-row" data-testid={`extra-track-${track.id}`}>
      <div className="track-label">
        <span className="track-label-text">
          Extra
          {isDriver && (
            <span className="loop-driver-badge" data-testid={`loop-driver-badge-${track.id}`} title="Loop driver — its duration sets the looped output length">
              ★
            </span>
          )}
        </span>
        <VolumeSlider
          value={track.volume}
          onChange={(v) => setExtraTrack(track.id, { volume: v })}
          testId={`extra-volume-${track.id}`}
        />
      </div>
      <div className="track-body">
        <WaveformBar
          peaks={serverPeaks ?? decodedPeaks}
          variant="extra"
          widthPct={widthPct}
          inPct={0}
          outPct={100}
          currentPct={null}
        />
        <div className="track-extra-info" data-testid={`extra-track-info-${track.id}`}>
          <span className="track-extra-name" title={track.name ?? ""}>
            🎵 {track.name ?? "extra"}
          </span>
          <span className="track-extra-dur">{track.duration.toFixed(1)}s</span>
          {isStreaming ? (
            <button
              type="button"
              className="extra-transcribe-button extra-transcribe-cancel"
              data-testid={`extra-transcribe-cancel-${track.id}`}
              tabIndex={-1}
              onMouseDown={(e) => e.preventDefault()}
              onClick={onCancelExtraTranscribe}
              title="Stop transcribing this track"
            >
              Cancel
            </button>
          ) : (
            <button
              type="button"
              className="extra-transcribe-button"
              data-testid={`extra-transcribe-button-${track.id}`}
              tabIndex={-1}
              onMouseDown={(e) => e.preventDefault()}
              onClick={onTranscribeExtra}
              disabled={someoneElseStreaming}
              title={someoneElseStreaming
                ? "Another track is being transcribed — wait or cancel it"
                : "Run whisper on this audio track and add a separate subtitle track"}
            >
              {hasExtraSubs ? "Re-generate subs" : "Generate subs"}
            </button>
          )}
          <button
            type="button"
            className="track-extra-remove"
            data-testid={`extra-track-remove-${track.id}`}
            tabIndex={-1}
            onMouseDown={(e) => e.preventDefault()}
            onClick={clear}
            title="Remove this track"
          >
            ×
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------- "+ Add audio track" footer button ----------

function AddExtraTrackButton({ setError }: { setError: (m: string | null) => void }) {
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const addExtraTrack = useStore((s) => s.addExtraTrack);

  async function onFile(file: File) {
    setUploading(true);
    setError(null);
    try {
      const res = await uploadExtraAudio(file);
      setExtraBlob(res.extra_audio_id, URL.createObjectURL(file));
      addExtraTrack({
        id: res.extra_audio_id,
        name: res.name,
        duration: res.duration,
        volume: 1.0,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="track-row track-row-empty" data-testid="extra-track-add-row">
      <div className="track-label">Extra</div>
      <button
        type="button"
        className="track-add"
        data-testid="extra-track-add"
        tabIndex={-1}
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => inputRef.current?.click()}
        disabled={uploading}
      >
        {uploading ? "Uploading…" : "+ Add audio track (mp3/wav/m4a/ogg/flac/aac)"}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept="audio/*,.mp3,.wav,.m4a,.ogg,.flac,.aac"
        data-testid="extra-file-input"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onFile(f);
          e.target.value = "";
        }}
      />
    </div>
  );
}

// ---------- Supporting primitives ----------

function VolumeSlider({
  value,
  onChange,
  testId,
}: {
  value: number;
  onChange: (v: number) => void;
  testId: string;
}) {
  const icon = value < 0.01 ? "🔇" : value < 0.6 ? "🔈" : value < 1.2 ? "🔉" : "🔊";
  return (
    <div className="vol-wrap">
      <span className="vol-icon" aria-hidden>{icon}</span>
      <input
        type="range"
        min={0}
        max={2}
        step={0.01}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        data-testid={testId}
        className="vol-slider"
      />
      <label className="vol-label">{Math.round(value * 100)}%</label>
    </div>
  );
}

function WaveformBar({
  peaks,
  widthPct,
  inPct,
  outPct,
  currentPct,
  variant,
}: {
  peaks: Float32Array | null;
  widthPct: number;
  inPct: number;
  outPct: number;
  currentPct: number | null;
  variant?: "source" | "extra";
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const parent = canvas.parentElement;
    if (!parent) return;
    const dpr = window.devicePixelRatio || 1;
    const render = () => {
      const wFull = parent.clientWidth;
      const h = parent.clientHeight;
      if (!wFull || !h) return;
      canvas.width = Math.round(wFull * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = wFull + "px";
      canvas.style.height = h + "px";
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, wFull, h);
      const mid = h / 2;
      ctx.fillStyle = "rgba(255, 255, 255, 0.08)";
      ctx.fillRect(0, mid - 1, wFull, 2);

      const drawW = Math.max(0, Math.min(wFull, (wFull * widthPct) / 100));
      if (peaks && peaks.length > 0 && drawW > 0) {
        const fill = variant === "extra" ? "rgba(255, 196, 0, 0.85)" : "rgba(124, 92, 255, 0.85)";
        ctx.fillStyle = fill;
        const barW = Math.max(1, drawW / peaks.length);
        for (let i = 0; i < peaks.length; i++) {
          const amp = peaks[i] * (h * 0.9) * 0.5;
          const x = i * barW;
          ctx.fillRect(x, mid - amp, Math.max(1, barW - 1), Math.max(2, amp * 2));
        }
      }
    };
    render();
    const ro = new ResizeObserver(render);
    ro.observe(parent);
    return () => ro.disconnect();
  }, [peaks, variant, widthPct]);

  return (
    <div className="wave-wrap">
      <canvas ref={canvasRef} className="wave-canvas" />
      <div className="wave-dim wave-dim-left" style={{ width: `${Math.max(0, inPct)}%` }} />
      <div
        className="wave-dim wave-dim-right"
        style={{ left: `${outPct}%`, width: `${Math.max(0, 100 - outPct)}%` }}
      />
      {currentPct !== null && (
        <div className="wave-playhead" style={{ left: `${currentPct}%` }} />
      )}
    </div>
  );
}

// ---------- Hooks ----------

const peakCache = new Map<string, Float32Array | null>();

function useWaveform(key: string | null, url: string | null): Float32Array | null {
  const [peaks, setPeaks] = useState<Float32Array | null>(
    key ? peakCache.get(key) ?? null : null,
  );
  useEffect(() => {
    if (!key || !url) {
      setPeaks(null);
      return;
    }
    const cached = peakCache.get(key);
    if (cached !== undefined) {
      setPeaks(cached);
      return;
    }
    let cancelled = false;
    computePeaks(url).then((p) => {
      peakCache.set(key, p);
      if (!cancelled) setPeaks(p);
    });
    return () => { cancelled = true; };
  }, [key, url]);
  return peaks;
}

function useExtraBlobUrl(extraId: string | null): string | null {
  return getExtraBlob(extraId);
}

const serverPeaksCache = new Map<string, Float32Array | null>();

function fetchServerPeaks(url: string, cacheKey: string): Promise<Float32Array | null> {
  const cached = serverPeaksCache.get(cacheKey);
  if (cached !== undefined) return Promise.resolve(cached);
  return fetch(url)
    .then((r) => (r.ok ? r.json() : null))
    .then((data: { peaks: number[] } | null) => {
      const arr = data?.peaks ? Float32Array.from(data.peaks) : null;
      serverPeaksCache.set(cacheKey, arr);
      return arr;
    })
    .catch(() => {
      serverPeaksCache.set(cacheKey, null);
      return null;
    });
}

function useServerPeaks(videoId: string | null): Float32Array | null {
  const [peaks, setPeaks] = useState<Float32Array | null>(null);
  useEffect(() => {
    if (!videoId) { setPeaks(null); return; }
    let cancelled = false;
    fetchServerPeaks(`/api/peaks/${videoId}?bins=500`, `video:${videoId}`).then((p) => {
      if (!cancelled) setPeaks(p);
    });
    return () => { cancelled = true; };
  }, [videoId]);
  return peaks;
}

function useServerExtraPeaks(extraId: string | null): Float32Array | null {
  const [peaks, setPeaks] = useState<Float32Array | null>(null);
  useEffect(() => {
    if (!extraId) { setPeaks(null); return; }
    let cancelled = false;
    fetchServerPeaks(`/api/peaks/extra/${extraId}?bins=500`, `extra:${extraId}`).then((p) => {
      if (!cancelled) setPeaks(p);
    });
    return () => { cancelled = true; };
  }, [extraId]);
  return peaks;
}
