import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import { temporal } from "zundo";

export type Word = { start: number; end: number; text: string };

export type Segment = {
  start: number;
  end: number;
  text: string;
  words?: Word[];
};

export type DisplayMode = "phrase" | "word" | "karaoke";

export type Style = {
  font_family: string;
  font_size: number;
  bold: boolean;
  italic: boolean;
  uppercase: boolean;
  text_color: string;
  outline_color: string;
  outline_width: number;
  shadow_offset: number;
  shadow_color: string;
  bg_color: string;
  bg_opacity: number;
  bg_padding: number;
  bg_radius: number;
  alignment: "left" | "center" | "right";
  fade_in_ms: number;
  fade_out_ms: number;
  mode: DisplayMode;
  words_per_chunk: number;
  active_word_color: string;
};

export type Position = { x_pct: number; y_pct: number };
export type Size = { w_pct: number; h_pct: number };

export type ProgressPhase =
  | "idle"
  | "upload"
  | "download"
  | "transcribe"
  | "align"
  | "encode"
  | "done";

export type TrimConfig = {
  enabled: boolean;
  threshold_sec: number;
  padding_sec: number;
};

export type AspectPreset = "source" | "9:16" | "16:9" | "1:1" | "4:5";
export type CanvasMode = "preset" | "custom";
export type CropAnchor = "left" | "center" | "right" | "top" | "bottom";

export type CustomCrop = {
  x_pct: number;
  y_pct: number;
  w_pct: number;
  h_pct: number;
};

export type CanvasConfig = {
  mode: CanvasMode;
  preset: AspectPreset;
  crop_anchor: CropAnchor;
  custom: CustomCrop;
  bg_color: string;
};

/** Keep-range selected by the trim handles under the preview. out_sec=0 => "to end".
 * `loop` (Coub mode): when true and an extra audio track is loaded, the
 * [in_sec..out_sec] slice is repeated to cover the extra audio's full
 * duration in both preview and export. */
export type TrimRange = { in_sec: number; out_sec: number; loop: boolean };

/** A single extra audio track. Multiple can stack under the source video,
 * each with its own volume and (optionally) its own subtitle transcript. */
export type ExtraTrack = {
  id: string;
  name: string | null;
  duration: number;
  volume: number;            // 0.0..2.0
};

/** Source-video audio + ordered list of extra tracks. The first entry of
 * `extras`, when present, is the loop driver (its duration sets total
 * length in Coub mode). */
export type AudioConfig = {
  sourceVolume: number;        // 0.0..2.0, default 1.0
  extras: ExtraTrack[];
};

/** Which transcript drives the on-screen captions and the burned subtitles
 * at export. "source" = whisper on the original video; otherwise an
 * extra-audio id present in `audio.extras`. */
export type SubtitleTrack = string;

type State = {
  videoId: string | null;
  videoUrl: string | null;
  duration: number;
  videoW: number;
  videoH: number;
  /** Active transcript — mirrored from `segmentsSource` or
   * `segmentsExtra[subtitleTrack]` so that legacy components reading
   * `s.segments` keep working without a manual selector. */
  segments: Segment[];
  /** Whisper-on-source-video transcript. Filled by the upload flow. */
  segmentsSource: Segment[];
  /** Per-track whisper transcripts, keyed by extra_audio_id. Each entry is
   * filled by the explicit Generate-subs button on that track. */
  segmentsExtra: Record<string, Segment[]>;
  /** Which transcript is active in the editor (and used for export):
   * "source" or any id in `audio.extras`. */
  subtitleTrack: SubtitleTrack;
  /** Id of the extra track currently being transcribed via /api/transcribe-extra
   * (only one runs at a time on the backend; new requests preempt). */
  extraSubsStreamingId: string | null;
  style: Style;
  position: Position;
  size: Size;
  busy: "idle" | "uploading" | "exporting";
  error: string | null;
  progressPhase: ProgressPhase;
  progressPercent: number;
  currentTime: number;
  videoEl: HTMLMediaElement | null;
  trim: TrimConfig;
  trimRange: TrimRange;
  audio: AudioConfig;
  canvas: CanvasConfig;
  isAudioOnly: boolean;
  /** Upload-screen toggle: generate subtitles via whisper after upload. Persisted. */
  generateSubs: boolean;
  /** Editor toggle: include subtitles in preview & export. Persisted. Off hides overlay. */
  useSubs: boolean;
  /** Editor toggle: burn the Cut/Storm watermark into the exported video. */
  watermark: boolean;
  /** True while background transcription is running (after upload, before done). */
  subsStreaming: boolean;
  /** Persisted across reloads so we can reconnect to the progress WS for an
   * in-flight whisper run. Cleared on reset / replace. */
  jobId: string | null;
};

type Actions = {
  setUploaded: (r: {
    video_id: string;
    duration: number;
    width: number;
    height: number;
    segments: Segment[];
    url: string;
    is_audio_only?: boolean;
  }) => void;
  loadProject: (r: {
    video_id: string;
    duration: number;
    width: number;
    height: number;
    segments: Segment[];
    url: string;
    is_audio_only?: boolean;
    project?: {
      style?: Style;
      position?: Position;
      size?: Size;
      canvas?: CanvasConfig;
      trim_range?: TrimRange;
      audio?: {
        source_volume: number;
        // Multi-track form (preferred):
        extras?: Array<{ id: string; volume: number; name?: string | null; duration?: number }>;
        // Legacy single-track form (still accepted on read):
        extra_audio_id?: string | null;
        extra_volume?: number;
      };
      use_subs?: boolean;
      display_mode?: DisplayMode;
      extra_segments?: Segment[] | Record<string, Segment[]>;
      subtitle_track?: SubtitleTrack;
    } | null;
  }) => void;
  setLoop: (v: boolean) => void;
  setSubtitleTrack: (track: SubtitleTrack) => void;
  /** Add a new extra track (append to the end of audio.extras). */
  addExtraTrack: (track: ExtraTrack) => void;
  /** Patch a single track by id (e.g. update volume, name, duration). */
  setExtraTrack: (id: string, patch: Partial<ExtraTrack>) => void;
  /** Remove a track by id. Cleans up its segments and falls back to
   * "source" if the active subtitle track was this one. Disables loop if
   * the removed track was the loop driver (extras[0]). */
  removeExtraTrack: (id: string) => void;
  /** Replace per-track segments for one extra. */
  setExtraSegments: (id: string, segs: Segment[]) => void;
  setExtraSubsStreamingId: (id: string | null) => void;
  appendExtraSegment: (id: string, seg: Segment, index: number) => void;
  mergeExtraSegments: (id: string, segs: Segment[]) => void;
  setStyle: (patch: Partial<Style>) => void;
  setPosition: (p: Position) => void;
  setSize: (s: Size) => void;
  updateSegment: (i: number, patch: Partial<Segment>) => void;
  deleteSegment: (i: number) => void;
  setBusy: (b: State["busy"]) => void;
  setError: (msg: string | null) => void;
  setProgress: (phase: ProgressPhase, percent: number) => void;
  setCurrentTime: (t: number) => void;
  setVideoEl: (el: HTMLMediaElement | null) => void;
  setTrim: (patch: Partial<TrimConfig>) => void;
  setTrimRange: (patch: Partial<TrimRange>) => void;
  setAudio: (patch: Partial<AudioConfig>) => void;
  setCanvas: (patch: Partial<CanvasConfig>) => void;
  setCustomCrop: (patch: Partial<CustomCrop>) => void;
  setGenerateSubs: (v: boolean) => void;
  setUseSubs: (v: boolean) => void;
  setWatermark: (v: boolean) => void;
  setSubsStreaming: (v: boolean) => void;
  setJobId: (id: string | null) => void;
  mergeSegments: (segs: Segment[]) => void;
  appendSegment: (seg: Segment, index: number) => void;
  newProject: () => Promise<void>;
  playPause: () => void;
  nudge: (deltaSec: number) => void;
  splitAtCurrent: () => void;
  deleteCurrent: () => void;
  reset: () => void;
};

export const defaultStyle: Style = {
  font_family: "Anton",
  font_size: 48,
  bold: false,
  italic: false,
  uppercase: false,
  text_color: "#FFFFFF",
  outline_color: "#000000",
  outline_width: 2,
  shadow_offset: 0,
  shadow_color: "#000000",
  bg_color: "#000000",
  bg_opacity: 0,
  bg_padding: 8,
  bg_radius: 0,
  alignment: "center",
  fade_in_ms: 0,
  fade_out_ms: 0,
  mode: "karaoke",
  words_per_chunk: 4,
  active_word_color: "#FFD400",
};

export const useStore = create<State & Actions>()(
  temporal(
    persist(
      (set) => ({
      videoId: null,
      videoUrl: null,
      duration: 0,
      videoW: 0,
      videoH: 0,
      segments: [],
      segmentsSource: [],
      segmentsExtra: {},
      subtitleTrack: "source" as SubtitleTrack,
      extraSubsStreamingId: null,
      style: { ...defaultStyle },
      position: { x_pct: 10, y_pct: 80 },
      size: { w_pct: 80, h_pct: 15 },
      busy: "idle",
      error: null,
      progressPhase: "idle",
      progressPercent: 0,
      currentTime: 0,
      videoEl: null,
      trim: { enabled: false, threshold_sec: 0.4, padding_sec: 0.08 },
      trimRange: { in_sec: 0, out_sec: 0, loop: false },
      audio: {
        sourceVolume: 1.0,
        extras: [],
      },
      canvas: {
        mode: "preset",
        preset: "source",
        crop_anchor: "center",
        custom: { x_pct: 10, y_pct: 10, w_pct: 80, h_pct: 80 },
        bg_color: "#000000",
      },
      isAudioOnly: false,
      generateSubs: true,
      useSubs: true,
      watermark: true,
      subsStreaming: false,
      jobId: null,
      setUploaded: (r) =>
        set((s) => {
          const isAudio = !!r.is_audio_only || (r.width === 0 && r.height === 0);
          return {
            videoId: r.video_id,
            videoUrl: r.url,
            duration: r.duration,
            videoW: r.width,
            videoH: r.height,
            segments: r.segments,  // may be empty initially — bg stream fills via appendSegment
            segmentsSource: r.segments,
            segmentsExtra: {},
            subtitleTrack: "source" as SubtitleTrack,
            extraSubsStreamingId: null,
            busy: "idle",
            error: null,
            isAudioOnly: isAudio,
            // A new upload represents a fresh edit: reset trim range and extra audio.
            trimRange: { in_sec: 0, out_sec: 0, loop: false },
            audio: {
              sourceVolume: 1.0,
              extras: [],
            },
            canvas: isAudio && s.canvas.preset === "source"
              ? { ...s.canvas, preset: "9:16", bg_color: s.canvas.bg_color === "#000000" ? "#00B140" : s.canvas.bg_color }
              : s.canvas,
          };
        }),
      loadProject: (r) => set((s) => {
        const isAudio = !!r.is_audio_only || (r.width === 0 && r.height === 0);
        const p = r.project ?? null;

        // Resolve audio.extras from new form (preferred) or legacy form
        // (one-element fallback). Names/durations are best-effort —
        // hydrated later from /api/extra-audio/{id}/info on mount.
        let extras: ExtraTrack[] = [];
        let sourceVolume = 1.0;
        if (p?.audio) {
          sourceVolume = p.audio.source_volume ?? 1.0;
          if (Array.isArray(p.audio.extras) && p.audio.extras.length > 0) {
            extras = p.audio.extras.map((e) => ({
              id: e.id,
              name: e.name ?? null,
              duration: typeof e.duration === "number" ? e.duration : 0,
              volume: typeof e.volume === "number" ? e.volume : 1.0,
            }));
          } else if (p.audio.extra_audio_id) {
            extras = [{
              id: p.audio.extra_audio_id,
              name: null,
              duration: 0,
              volume: typeof p.audio.extra_volume === "number" ? p.audio.extra_volume : 1.0,
            }];
          }
        }

        // Resolve segmentsExtra (Record). Old projects stored a flat list
        // for the single extra track; promote to {id: list}.
        let segmentsExtra: Record<string, Segment[]> = {};
        const rawExtraSegs = p?.extra_segments;
        if (Array.isArray(rawExtraSegs)) {
          if (extras.length > 0) {
            segmentsExtra = { [extras[0].id]: rawExtraSegs };
          }
        } else if (rawExtraSegs && typeof rawExtraSegs === "object") {
          segmentsExtra = rawExtraSegs as Record<string, Segment[]>;
        }

        // Subtitle track: "source", or one of extras[].id. Legacy "extra"
        // string maps to the first extra (if any), else "source".
        let track: SubtitleTrack = p?.subtitle_track ?? "source";
        if (track === "extra") {
          track = extras.length > 0 ? extras[0].id : "source";
        }
        if (track !== "source" && !extras.find((e) => e.id === track)) {
          track = "source";
        }

        const activeSegs = track === "source"
          ? r.segments
          : segmentsExtra[track] ?? [];

        return {
          videoId: r.video_id,
          videoUrl: r.url,
          duration: r.duration,
          videoW: r.width,
          videoH: r.height,
          segments: activeSegs,
          segmentsSource: r.segments,
          segmentsExtra,
          subtitleTrack: track,
          extraSubsStreamingId: null,
          busy: "idle",
          error: null,
          isAudioOnly: isAudio,
          // Honour saved project state when present; fall back to current
          // store defaults otherwise. Unlike setUploaded, do NOT reset trim
          // and audio — that's the whole point of the history restore.
          style: p?.style ?? s.style,
          position: p?.position ?? s.position,
          size: p?.size ?? s.size,
          canvas: p?.canvas ?? s.canvas,
          trimRange: p?.trim_range
            ? { in_sec: p.trim_range.in_sec, out_sec: p.trim_range.out_sec, loop: !!p.trim_range.loop }
            : { in_sec: 0, out_sec: 0, loop: false },
          audio: { sourceVolume, extras },
          useSubs: p?.use_subs ?? s.useSubs,
        };
      }),
      setStyle: (patch) => set((s) => ({ style: { ...s.style, ...patch } })),
      setPosition: (p) => set({ position: p }),
      setSize: (sz) => set({ size: sz }),
      setLoop: (v) => set((s) => ({ trimRange: { ...s.trimRange, loop: v } })),
      setSubtitleTrack: (track) => set((s) => ({
        subtitleTrack: track,
        // Mirror the active store-level alias so existing readers (overlay,
        // segment list) update without further plumbing.
        segments: track === "source"
          ? s.segmentsSource
          : (s.segmentsExtra[track] ?? []),
      })),
      addExtraTrack: (track) => set((s) => ({
        audio: { ...s.audio, extras: [...s.audio.extras, track] },
        // New track has no segments yet — initialise an empty list so the
        // UI tab is enabled the moment whisper streams the first segment.
        segmentsExtra: { ...s.segmentsExtra, [track.id]: s.segmentsExtra[track.id] ?? [] },
      })),
      setExtraTrack: (id, patch) => set((s) => ({
        audio: {
          ...s.audio,
          extras: s.audio.extras.map((t) => (t.id === id ? { ...t, ...patch } : t)),
        },
      })),
      removeExtraTrack: (id) => set((s) => {
        const wasDriver = s.audio.extras[0]?.id === id;
        const remaining = s.audio.extras.filter((t) => t.id !== id);
        const remainingSegs = { ...s.segmentsExtra };
        delete remainingSegs[id];
        // If the deleted track was active in the editor, fall back to source.
        let nextTrack: SubtitleTrack = s.subtitleTrack;
        let nextSegs: Segment[] = s.segments;
        if (s.subtitleTrack === id) {
          nextTrack = "source";
          nextSegs = s.segmentsSource;
        }
        // If we just removed the loop driver and loop was on, disable loop
        // — without a driver track its duration has nothing to anchor to.
        const trimRange =
          s.trimRange.loop && wasDriver
            ? { ...s.trimRange, loop: false }
            : s.trimRange;
        return {
          audio: { ...s.audio, extras: remaining },
          segmentsExtra: remainingSegs,
          subtitleTrack: nextTrack,
          segments: nextSegs,
          trimRange,
          // Cancel any in-flight transcribe that targeted this id.
          extraSubsStreamingId:
            s.extraSubsStreamingId === id ? null : s.extraSubsStreamingId,
        };
      }),
      setExtraSegments: (id, segs) => set((s) => {
        const nextMap = { ...s.segmentsExtra, [id]: segs };
        return {
          segmentsExtra: nextMap,
          segments: s.subtitleTrack === id ? segs : s.segments,
        };
      }),
      setExtraSubsStreamingId: (id) => set({ extraSubsStreamingId: id }),
      appendExtraSegment: (id, seg, index) => set((s) => {
        const prev = s.segmentsExtra[id] ?? [];
        const next = [...prev];
        next[index] = seg;
        for (let i = 0; i < next.length; i++) {
          if (next[i] === undefined) {
            next[i] = { start: 0, end: 0, text: "…", words: [] };
          }
        }
        const nextMap = { ...s.segmentsExtra, [id]: next };
        return {
          segmentsExtra: nextMap,
          segments: s.subtitleTrack === id ? next : s.segments,
        };
      }),
      mergeExtraSegments: (id, incoming) => set((s) => {
        const prev = s.segmentsExtra[id] ?? [];
        const next = [...prev];
        incoming.forEach((seg, i) => {
          if (!next[i] || next[i].text !== seg.text || next[i].start !== seg.start) {
            next[i] = seg;
          }
        });
        const nextMap = { ...s.segmentsExtra, [id]: next };
        return {
          segmentsExtra: nextMap,
          segments: s.subtitleTrack === id ? next : s.segments,
        };
      }),
      updateSegment: (i, patch) =>
        set((s) => {
          const next = s.segments.map((seg, idx) => {
            if (idx !== i) return seg;
            const merged: Segment = { ...seg, ...patch };
            if (patch.text !== undefined && patch.text !== seg.text) {
              merged.words = undefined;
            }
            return merged;
          });
          if (s.subtitleTrack === "source") {
            return { segments: next, segmentsSource: next };
          }
          return {
            segments: next,
            segmentsExtra: { ...s.segmentsExtra, [s.subtitleTrack]: next },
          };
        }),
      deleteSegment: (i) =>
        set((s) => {
          const next = s.segments.filter((_, idx) => idx !== i);
          if (s.subtitleTrack === "source") {
            return { segments: next, segmentsSource: next };
          }
          return {
            segments: next,
            segmentsExtra: { ...s.segmentsExtra, [s.subtitleTrack]: next },
          };
        }),
      setBusy: (b) => set({ busy: b }),
      setError: (msg) => set({ error: msg }),
      setProgress: (phase, percent) => set({ progressPhase: phase, progressPercent: percent }),
      setCurrentTime: (t) => set({ currentTime: t }),
      setVideoEl: (el) => set({ videoEl: el }),
      setTrim: (patch) => set((s) => ({ trim: { ...s.trim, ...patch } })),
      setTrimRange: (patch) => set((s) => {
        const dur = s.duration || 0;
        const next: TrimRange = { ...s.trimRange, ...patch };
        // Clamp to [0, duration]. out_sec=0 stays as sentinel for "to end".
        next.in_sec = Math.max(0, Math.min(next.in_sec, Math.max(0, dur - 0.1)));
        if (next.out_sec > 0) {
          next.out_sec = Math.max(next.in_sec + 0.1, Math.min(next.out_sec, dur));
        }
        return { trimRange: next };
      }),
      setAudio: (patch) => set((s) => ({ audio: { ...s.audio, ...patch } })),
      setCanvas: (patch) => set((s) => ({ canvas: { ...s.canvas, ...patch } })),
      setGenerateSubs: (v) => set({ generateSubs: v }),
      setUseSubs: (v) => set({ useSubs: v }),
      setWatermark: (v) => set({ watermark: v }),
      setSubsStreaming: (v) => set({ subsStreaming: v }),
      setJobId: (id) => set({ jobId: id }),
      mergeSegments: (serverSegs) => set((s) => {
        // Merge server snapshot into the SOURCE transcript without truncating.
        // Source segments are filled by the upload-time whisper pass; the
        // extra-track transcript has its own merge path via setExtraSegments.
        const next = [...s.segmentsSource];
        serverSegs.forEach((seg, i) => {
          if (!next[i] || (next[i] && (next[i].text !== seg.text || next[i].start !== seg.start))) {
            next[i] = seg;
          }
        });
        return s.subtitleTrack === "source"
          ? { segments: next, segmentsSource: next }
          : { segmentsSource: next };
      }),
      newProject: async () => {
        // Best-effort server-side cancellation of any in-flight whisper for
        // the current video, then reset the frontend. We never block the
        // reset on the network — the user expects "New project" to be instant.
        const s = useStore.getState();
        if (s.videoId && s.subsStreaming) {
          const { cancelTranscribe } = await import("./api");
          void cancelTranscribe(s.videoId);
        }
        useStore.getState().reset();
      },
      appendSegment: (seg, index) => set((s) => {
        const next = [...s.segmentsSource];
        next[index] = seg;
        // If the backend emitted indices out of order somehow, fill any gaps
        // with placeholders (extremely unlikely; defensive only).
        for (let i = 0; i < next.length; i++) {
          if (next[i] === undefined) {
            next[i] = { start: 0, end: 0, text: "…", words: [] };
          }
        }
        return s.subtitleTrack === "source"
          ? { segments: next, segmentsSource: next }
          : { segmentsSource: next };
      }),
      setCustomCrop: (patch) => set((s) => {
        const next = { ...s.canvas.custom, ...patch };
        // Clamp so x+w <= 100, y+h <= 100 — UI may race faster than backend validator.
        if (next.x_pct + next.w_pct > 100) next.w_pct = Math.max(5, 100 - next.x_pct);
        if (next.y_pct + next.h_pct > 100) next.h_pct = Math.max(5, 100 - next.y_pct);
        if (next.w_pct < 5) next.w_pct = 5;
        if (next.h_pct < 5) next.h_pct = 5;
        if (next.x_pct < 0) next.x_pct = 0;
        if (next.y_pct < 0) next.y_pct = 0;
        if (next.x_pct > 95) next.x_pct = 95;
        if (next.y_pct > 95) next.y_pct = 95;
        return { canvas: { ...s.canvas, custom: next } };
      }),
      playPause: () => {
        const el = useStore.getState().videoEl;
        if (!el) return;
        if (el.paused) el.play().catch(() => {});
        else el.pause();
      },
      nudge: (deltaSec) => {
        const el = useStore.getState().videoEl;
        if (!el) return;
        const next = Math.max(0, Math.min(el.duration || 0, el.currentTime + deltaSec));
        el.currentTime = next;
      },
      splitAtCurrent: () =>
        set((s) => {
          const t = s.currentTime;
          const idx = s.segments.findIndex((seg) => t > seg.start && t < seg.end);
          if (idx < 0) return {};
          const seg = s.segments[idx];
          const leftWords = seg.words?.filter((w) => w.end <= t) ?? [];
          const rightWords = seg.words?.filter((w) => w.start >= t) ?? [];
          const leftText = leftWords.length
            ? leftWords.map((w) => w.text).join(" ")
            : seg.text;
          const rightText = rightWords.length
            ? rightWords.map((w) => w.text).join(" ")
            : seg.text;
          const left = { ...seg, end: t, text: leftText, words: leftWords.length ? leftWords : undefined };
          const right = { ...seg, start: t, text: rightText, words: rightWords.length ? rightWords : undefined };
          const next = [...s.segments.slice(0, idx), left, right, ...s.segments.slice(idx + 1)];
          if (s.subtitleTrack === "source") {
            return { segments: next, segmentsSource: next };
          }
          return {
            segments: next,
            segmentsExtra: { ...s.segmentsExtra, [s.subtitleTrack]: next },
          };
        }),
      deleteCurrent: () =>
        set((s) => {
          const t = s.currentTime;
          const idx = s.segments.findIndex((seg) => t >= seg.start && t <= seg.end);
          if (idx < 0) return {};
          const next = s.segments.filter((_, i) => i !== idx);
          if (s.subtitleTrack === "source") {
            return { segments: next, segmentsSource: next };
          }
          return {
            segments: next,
            segmentsExtra: { ...s.segmentsExtra, [s.subtitleTrack]: next },
          };
        }),
      reset: () =>
        set({
          videoId: null,
          videoUrl: null,
          duration: 0,
          videoW: 0,
          videoH: 0,
          segments: [],
          segmentsSource: [],
          segmentsExtra: {},
          subtitleTrack: "source" as SubtitleTrack,
          extraSubsStreamingId: null,
          busy: "idle",
          error: null,
          progressPhase: "idle",
          progressPercent: 0,
          currentTime: 0,
          isAudioOnly: false,
          subsStreaming: false,
          jobId: null,
          watermark: true,
          trimRange: { in_sec: 0, out_sec: 0, loop: false },
          audio: { sourceVolume: 1.0, extras: [] },
        }),
    }),
    {
      name: "cutstorm-state",
      storage: createJSONStorage(() => localStorage),
      // Only persist the project state. Skip transient runtime state
      // (busy, error, progress, currentTime) so a refresh doesn't restore
      // a half-finished upload spinner or stale error toast.
      partialize: (s) => ({
        videoId: s.videoId,
        videoUrl: s.videoUrl,
        duration: s.duration,
        videoW: s.videoW,
        videoH: s.videoH,
        segments: s.segments,
        segmentsSource: s.segmentsSource,
        segmentsExtra: s.segmentsExtra,
        subtitleTrack: s.subtitleTrack,
        style: s.style,
        position: s.position,
        size: s.size,
        trim: s.trim,
        trimRange: s.trimRange,
        audio: s.audio,
        canvas: s.canvas,
        isAudioOnly: s.isAudioOnly,
        generateSubs: s.generateSubs,
        useSubs: s.useSubs,
        watermark: s.watermark,
        subsStreaming: s.subsStreaming,
        jobId: s.jobId,
      }),
        version: 9,
        // Historical fields migrate forward:
        //   v1→v2: `canvas` gained mode/crop_anchor/custom (Feature 1).
        //   v2→v3: `trimRange` added (Feature Trim in/out).
        //   v3→v4: `audio` added (Feature Volume + extra track).
        // zustand's default merge is shallow — persisted fields REPLACE the
        // defaults — so missing keys must be filled explicitly to avoid
        // undefined reads in the UI.
        migrate: (persisted: unknown, version: number) => {
          if (!persisted || typeof persisted !== "object") return persisted;
          const p = persisted as Record<string, unknown>;
          if (version < 2 && p.canvas && typeof p.canvas === "object") {
            p.canvas = {
              mode: "preset",
              crop_anchor: "center",
              custom: { x_pct: 10, y_pct: 10, w_pct: 80, h_pct: 80 },
              ...(p.canvas as Record<string, unknown>),
            };
          }
          if (version < 3) {
            p.trimRange = p.trimRange ?? { in_sec: 0, out_sec: 0 };
          }
          if (version < 4) {
            p.audio = p.audio ?? {
              sourceVolume: 1.0,
              extraAudioId: null,
              extraAudioName: null,
              extraAudioDuration: 0,
              extraVolume: 1.0,
            };
          }
          if (version < 5) {
            // v4→v5: jobId + subsStreaming are now persisted so a page reload
            // mid-transcribe can reconnect to the progress WS.
            p.jobId = p.jobId ?? null;
            p.subsStreaming = p.subsStreaming ?? false;
          }
          if (version < 6) {
            // v5→v6: Style gained an `uppercase` toggle (renders captions
            // in ALL CAPS regardless of the chosen font).
            if (p.style && typeof p.style === "object") {
              (p.style as Record<string, unknown>).uppercase =
                (p.style as Record<string, unknown>).uppercase ?? false;
            }
          }
          if (version < 7) {
            // v6→v7: watermark toggle (on by default for new projects).
            p.watermark = p.watermark ?? true;
          }
          if (version < 8) {
            // v7→v8: Coub-mode fields.
            //   trimRange.loop : new boolean (default false).
            //   segmentsSource : copy of legacy `segments` (the only track
            //                    that existed before extra-track transcribe).
            //   segmentsExtra  : empty list — extra subs are only filled by
            //                    explicit user action.
            //   subtitleTrack  : "source" — preserves prior behaviour.
            const tr = (p.trimRange as Record<string, unknown> | undefined) ?? {};
            p.trimRange = {
              in_sec: typeof tr.in_sec === "number" ? tr.in_sec : 0,
              out_sec: typeof tr.out_sec === "number" ? tr.out_sec : 0,
              loop: typeof tr.loop === "boolean" ? tr.loop : false,
            };
            const segs = Array.isArray(p.segments) ? p.segments : [];
            p.segmentsSource = (p.segmentsSource as unknown) ?? segs;
            p.segmentsExtra = (p.segmentsExtra as unknown) ?? [];
            p.subtitleTrack = (p.subtitleTrack as unknown) ?? "source";
          }
          if (version < 9) {
            // v8→v9: multi-extra tracks.
            //   audio: { sourceVolume, extras: [{id,name,duration,volume}] }
            //          (was: extraAudioId/Name/Duration + extraVolume)
            //   segmentsExtra: Record<id, Segment[]>  (was: Segment[] for the
            //                                          single legacy track)
            //   subtitleTrack: "source" | <extra_id>  (was: "source" | "extra")
            //   extraSubsStreamingId: string | null   (was: extraSubsStreaming bool)
            const oldAudio = (p.audio as Record<string, unknown> | undefined) ?? {};
            const legacyId = oldAudio.extraAudioId as string | null | undefined;
            const legacyName = oldAudio.extraAudioName as string | null | undefined;
            const legacyDuration = oldAudio.extraAudioDuration as number | undefined;
            const legacyVolume = oldAudio.extraVolume as number | undefined;
            const sourceVolume = typeof oldAudio.sourceVolume === "number"
              ? (oldAudio.sourceVolume as number)
              : 1.0;
            const extras = legacyId
              ? [{
                  id: legacyId,
                  name: legacyName ?? null,
                  duration: typeof legacyDuration === "number" ? legacyDuration : 0,
                  volume: typeof legacyVolume === "number" ? legacyVolume : 1.0,
                }]
              : [];
            p.audio = { sourceVolume, extras };

            // segmentsExtra: array → Record. Legacy array belongs to the
            // single legacy track id (if any).
            const oldSegsExtra = p.segmentsExtra;
            if (Array.isArray(oldSegsExtra)) {
              p.segmentsExtra = legacyId ? { [legacyId]: oldSegsExtra } : {};
            } else if (!oldSegsExtra || typeof oldSegsExtra !== "object") {
              p.segmentsExtra = {};
            }

            // subtitleTrack: "extra" → legacyId; otherwise stay or fall back.
            const oldTrack = p.subtitleTrack as string | undefined;
            if (oldTrack === "extra") {
              p.subtitleTrack = legacyId ?? "source";
            } else if (typeof oldTrack !== "string") {
              p.subtitleTrack = "source";
            }

            // extraSubsStreaming bool → extraSubsStreamingId.
            p.extraSubsStreamingId = (p as Record<string, unknown>).extraSubsStreaming
              ? legacyId ?? null
              : null;
            delete (p as Record<string, unknown>).extraSubsStreaming;
          }
          return p;
        },
      },
    ),
    {
      partialize: (s) => ({
        segments: s.segments,
        segmentsSource: s.segmentsSource,
        segmentsExtra: s.segmentsExtra,
        subtitleTrack: s.subtitleTrack,
        style: s.style,
        position: s.position,
        size: s.size,
        trim: s.trim,
        trimRange: s.trimRange,
        audio: s.audio,
        canvas: s.canvas,
      }),
      limit: 50,
    },
  ),
);
