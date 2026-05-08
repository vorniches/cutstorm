import { saveProjectState, updateTranscript } from "./api";
import { useStore } from "./store";

/**
 * Debounced backend PUTs for both transcript edits (segments) and project
 * state (style / position / size / canvas / trim / audio / toggles). Runs
 * whenever the relevant slice of the store changes AND there's a videoId.
 */

let segTimer: ReturnType<typeof setTimeout> | null = null;
let projTimer: ReturnType<typeof setTimeout> | null = null;
let lastSegSerialized: string | null = null;
let lastProjSerialized: string | null = null;

function projectSnapshot(s: ReturnType<typeof useStore.getState>) {
  return {
    style: s.style,
    position: s.position,
    size: s.size,
    canvas: s.canvas,
    trim_range: s.trimRange,
    audio: {
      source_volume: s.audio.sourceVolume,
      extras: s.audio.extras.map((e) => ({
        id: e.id,
        volume: e.volume,
        name: e.name,
        duration: e.duration,
      })),
    },
    use_subs: s.useSubs,
    display_mode: s.style.mode,
    extra_segments: s.segmentsExtra,
    subtitle_track: s.subtitleTrack,
  };
}

export function setupAutosave() {
  useStore.subscribe((state, prev) => {
    const id = state.videoId;
    if (!id) return;

    // --- source segments ---
    // The on-disk transcript is the SOURCE-track whisper output. Edits made
    // while viewing the extra track land in `segmentsExtra` (project state),
    // not the transcript file, so autosave must follow `segmentsSource`.
    if (state.segmentsSource !== prev.segmentsSource) {
      const ser = JSON.stringify(state.segmentsSource);
      if (ser !== lastSegSerialized) {
        if (segTimer) clearTimeout(segTimer);
        segTimer = setTimeout(async () => {
          try {
            await updateTranscript(id, state.segmentsSource);
            lastSegSerialized = ser;
          } catch (err) {
            console.warn("autosave.segments failed:", err);
          }
        }, 1200);
      }
    }

    // --- project state ---
    const projectChanged =
      state.style !== prev.style ||
      state.position !== prev.position ||
      state.size !== prev.size ||
      state.canvas !== prev.canvas ||
      state.trimRange !== prev.trimRange ||
      state.audio !== prev.audio ||
      state.useSubs !== prev.useSubs ||
      state.segmentsExtra !== prev.segmentsExtra ||
      state.subtitleTrack !== prev.subtitleTrack ||
      state.videoId !== prev.videoId;
    if (!projectChanged) return;

    const snap = projectSnapshot(state);
    const ser = JSON.stringify(snap);
    if (ser === lastProjSerialized) return;

    if (projTimer) clearTimeout(projTimer);
    projTimer = setTimeout(async () => {
      try {
        await saveProjectState(id, { ...snap, updated_at: Date.now() / 1000 });
        lastProjSerialized = ser;
      } catch (err) {
        console.warn("autosave.project failed:", err);
      }
    }, 500);
  });
}
