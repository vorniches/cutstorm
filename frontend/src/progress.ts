import { useStore } from "./store";

export function newJobId(): string {
  return "job-" + Math.random().toString(36).slice(2) + "-" + Date.now();
}

// Last time the backend sent us any progress-like event. The stuck-transcribe
// detector in App.tsx reads this to decide whether whisper has gone silent.
export const progressHeartbeat = { at: 0 };

export function openProgressWs(jobId: string): Promise<WebSocket> {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${window.location.host}/ws/progress/${jobId}`);
  ws.onmessage = (ev) => {
    try {
      progressHeartbeat.at = Date.now();
      const msg = JSON.parse(ev.data);
      const store = useStore.getState();

      // Streamed segment from background transcription.
      if (msg?.phase === "segment" && msg.segment && typeof msg.index === "number") {
        store.appendSegment(msg.segment, msg.index);
        // Update progress bar alongside.
        if (typeof msg.percent === "number") {
          store.setProgress("transcribe", Math.max(0, Math.min(99, msg.percent)));
        }
        store.setSubsStreaming(true);
        return;
      }

      if (msg?.phase === "transcribe_done") {
        store.setSubsStreaming(false);
        store.setJobId(null);
        store.setProgress("done", 100);
        setTimeout(() => ws.close(), 100);
        return;
      }

      if (msg?.phase === "transcribe_cancelled" || msg?.phase === "transcribe_error") {
        store.setSubsStreaming(false);
        store.setJobId(null);
        setTimeout(() => ws.close(), 100);
        return;
      }

      // Extra-track transcription mirrors the source-track flow above. Every
      // event from the backend carries `extra_audio_id` so we can route the
      // segment into the right per-track list without confusing the source
      // progress bar.
      if (msg?.phase === "extra_segment" && msg.segment && typeof msg.index === "number") {
        const id: string | undefined = msg.extra_audio_id;
        if (typeof id === "string") {
          store.appendExtraSegment(id, msg.segment, msg.index);
          store.setExtraSubsStreamingId(id);
        }
        if (typeof msg.percent === "number") {
          store.setProgress("transcribe", Math.max(0, Math.min(99, msg.percent)));
        }
        return;
      }

      if (msg?.phase === "extra_transcribe") {
        // Pure progress tick (no segment payload). Surface via the global
        // progress bar so the user sees something is happening even before
        // the first segment lands.
        if (typeof msg.percent === "number") {
          store.setProgress("transcribe", Math.max(0, Math.min(99, msg.percent)));
        }
        if (typeof msg.extra_audio_id === "string") {
          store.setExtraSubsStreamingId(msg.extra_audio_id);
        }
        return;
      }

      if (msg?.phase === "extra_transcribe_done") {
        store.setExtraSubsStreamingId(null);
        store.setJobId(null);
        store.setProgress("done", 100);
        setTimeout(() => ws.close(), 100);
        return;
      }

      if (
        msg?.phase === "extra_transcribe_cancelled" ||
        msg?.phase === "extra_transcribe_error"
      ) {
        store.setExtraSubsStreamingId(null);
        store.setJobId(null);
        setTimeout(() => ws.close(), 100);
        return;
      }

      // URL-import: yt-dlp finished pulling bytes. Leave bar as-is; Uploader
      // will flip to "Transcribing…" once the POST /api/fetch-url returns.
      if (msg?.phase === "download_done") return;

      if (msg?.phase === "download_error") return;

      if (msg && typeof msg.percent === "number") {
        if (store.progressPhase === "done") return;
        store.setProgress(msg.phase ?? "idle", Math.max(0, Math.min(100, msg.percent)));
      }
    } catch {
      /* ignore */
    }
  };
  return new Promise((resolve, reject) => {
    ws.onopen = () => resolve(ws);
    ws.onerror = (e) => reject(e);
  });
}
