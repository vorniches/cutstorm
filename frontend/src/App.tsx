import { useEffect, useState } from "react";
import { getTranscript } from "./api";
import { AudioPreview } from "./components/AudioPreview";
import { ProgressBar } from "./components/ProgressBar";
import { RenderPage } from "./components/RenderPage";
import { Sidebar } from "./components/Sidebar";
import { Uploader } from "./components/Uploader";
import { SegmentList } from "./components/SegmentList";
import { StylePanel } from "./components/StylePanel";
import { TopBar } from "./components/TopBar";
import { VideoPreview } from "./components/VideoPreview";
import { useHotkeys } from "./hotkeys";
import { openProgressWs, progressHeartbeat } from "./progress";
import { useStore } from "./store";

function isRenderMode(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("render") === "1";
}

export function App() {
  if (isRenderMode()) return <RenderPage />;

  const error = useStore((s) => s.error);
  const clearError = useStore((s) => s.setError);
  const hasVideo = useStore((s) => !!s.videoUrl);
  const isAudioOnly = useStore((s) => s.isAudioOnly);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  useHotkeys();

  // One-shot recovery after page reload. Backend meta.json is the source of
  // truth — we ignore the persisted subsStreaming flag and always ask the
  // server what state this video is in. That way old localStorage shapes,
  // cross-tab drift, or a user who closed the tab mid-transcribe all resolve
  // the same way: whatever the backend says.
  useEffect(() => {
    const s = useStore.getState();
    if (!s.videoId) return;
    const { videoId } = s;
    let ws: WebSocket | null = null;
    (async () => {
      let meta;
      try {
        meta = await getTranscript(videoId);
      } catch {
        // 404 → video was deleted outside this session. Leave the editor
        // as-is; user can Replace to start over.
        useStore.getState().setSubsStreaming(false);
        useStore.getState().setJobId(null);
        useStore.getState().setProgress("idle", 0);
        return;
      }
      const status = meta.status ?? null;
      if (status === "done" || status == null) {
        // `null` = legacy meta (pre-status field). Treat as done.
        useStore.getState().mergeSegments(meta.segments);
        useStore.getState().setSubsStreaming(false);
        useStore.getState().setJobId(null);
        if (meta.segments.length > 0) useStore.getState().setProgress("done", 100);
        return;
      }
      if (status === "cancelled" || status === "stale" || status === "error") {
        useStore.getState().mergeSegments(meta.segments);
        useStore.getState().setSubsStreaming(false);
        useStore.getState().setJobId(null);
        useStore.getState().setProgress("idle", 0);
        if (status === "stale") {
          useStore.getState().setError("Transcription was interrupted (server restarted). Reload the video to try again.");
        } else if (status === "error") {
          useStore.getState().setError(meta.error ?? "Transcription failed.");
        }
        return;
      }
      // pending — reconnect to live progress. Prefer job_id from meta
      // (canonical), fall back to persisted jobId.
      const jobId = meta.job_id ?? s.jobId;
      if (!jobId) {
        // No way to resubscribe — just show the partial snapshot, don't
        // animate the spinner (misleading without live updates).
        useStore.getState().mergeSegments(meta.segments);
        useStore.getState().setSubsStreaming(false);
        useStore.getState().setJobId(null);
        useStore.getState().setProgress("idle", 0);
        return;
      }
      ws = await openProgressWs(jobId);
      useStore.getState().setJobId(jobId);
      useStore.getState().mergeSegments(meta.segments);
      useStore.getState().setProgress("transcribe", meta.percent ?? 0);
      useStore.getState().setSubsStreaming(true);
      // Catch-up read after WS is listening — covers segments that landed
      // during the WS handshake. mergeSegments is idempotent by index.
      try {
        const meta2 = await getTranscript(videoId);
        useStore.getState().mergeSegments(meta2.segments);
        if (meta2.status === "done") {
          useStore.getState().setSubsStreaming(false);
          useStore.getState().setJobId(null);
          useStore.getState().setProgress("done", 100);
          setTimeout(() => ws?.close(), 100);
        }
      } catch {
        // benign — live WS will keep us current.
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Stuck-transcribe watchdog: if the background task stops sending progress
  // for 5 minutes, surface a toast so the user can cancel and start over
  // instead of staring at a frozen spinner. Triggers for either source-track
  // or extra-track transcription, since both stream through the same WS and
  // bump `progressHeartbeat`.
  useEffect(() => {
    const STUCK_AFTER_MS = 5 * 60 * 1000;
    let alreadyWarned = false;
    const id = setInterval(() => {
      const s = useStore.getState();
      const streaming = s.subsStreaming || s.extraSubsStreamingId !== null;
      if (!streaming) {
        alreadyWarned = false;
        return;
      }
      if (alreadyWarned) return;
      const last = progressHeartbeat.at;
      if (!last) return;
      if (Date.now() - last > STUCK_AFTER_MS) {
        alreadyWarned = true;
        s.setError(
          "Transcription seems stuck — no progress for 5 minutes. Cancel and try again.",
        );
      }
    }, 30_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="shell">
      <TopBar onOpenSidebar={() => setSidebarOpen(true)} />
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      {hasVideo ? (
        <div className="editor">
          <StylePanel />
          {isAudioOnly ? <AudioPreview /> : <VideoPreview />}
          <SegmentList />
        </div>
      ) : (
        <Uploader />
      )}

      <ProgressBar />

      {error && (
        <div
          className="toast"
          data-testid="error-toast"
          role="alert"
          onClick={() => clearError(null)}
          title="click to dismiss"
        >
          {error}
        </div>
      )}
    </div>
  );
}
