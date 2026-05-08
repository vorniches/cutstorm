import { useStore } from "../store";

function fmtTimestamp(t: number): string {
  if (!Number.isFinite(t) || t < 0) t = 0;
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  const cs = Math.floor((t - Math.floor(t)) * 100);
  return `${m}:${s.toString().padStart(2, "0")}.${cs.toString().padStart(2, "0")}`;
}

export function SegmentList() {
  const segments = useStore((s) => s.segments);
  const segmentsSource = useStore((s) => s.segmentsSource);
  const segmentsExtra = useStore((s) => s.segmentsExtra);
  const extras = useStore((s) => s.audio.extras);
  const subtitleTrack = useStore((s) => s.subtitleTrack);
  const setSubtitleTrack = useStore((s) => s.setSubtitleTrack);
  const updateSegment = useStore((s) => s.updateSegment);
  const deleteSegment = useStore((s) => s.deleteSegment);
  const currentTime = useStore((s) => s.currentTime);
  const hasVideo = useStore((s) => !!s.videoUrl);
  const subsStreaming = useStore((s) => s.subsStreaming);
  const extraSubsStreamingId = useStore((s) => s.extraSubsStreamingId);
  const progressPhase = useStore((s) => s.progressPhase);
  const progressPercent = useStore((s) => s.progressPercent);
  if (!hasVideo) return null;

  const activeIdx = segments.findIndex(
    (seg) => currentTime >= seg.start && currentTime <= seg.end,
  );

  // The strip + spinner indicators show whichever track is currently being
  // transcribed AND is the active tab in the editor.
  const sourceTranscribing = subsStreaming && progressPhase === "transcribe";
  const activeExtraTranscribing =
    subtitleTrack !== "source" && extraSubsStreamingId === subtitleTrack;
  const transcribing =
    subtitleTrack === "source" ? sourceTranscribing : activeExtraTranscribing;

  return (
    <div className="pane scroll" data-testid="segments-panel">
      <div className="pane-header">
        <h2>Transcript</h2>
        <span className="topbar-meta">{segments.length}</span>
      </div>
      <div className="subtitle-track-tabs" data-testid="subtitle-track-tabs">
        <button
          type="button"
          className={`subtitle-track-tab${subtitleTrack === "source" ? " active" : ""}`}
          data-testid="subtitle-track-source"
          aria-pressed={subtitleTrack === "source"}
          onClick={() => setSubtitleTrack("source")}
        >
          Source <span className="subtitle-track-count">{segmentsSource.length}</span>
          {sourceTranscribing && <span className="subtitle-track-dot" aria-label="transcribing" />}
        </button>
        {extras.map((track, i) => {
          const segs = segmentsExtra[track.id] ?? [];
          const trackStreaming = extraSubsStreamingId === track.id;
          const enabled = segs.length > 0 || trackStreaming;
          const label = track.name ?? `Extra ${i + 1}`;
          return (
            <button
              key={track.id}
              type="button"
              className={`subtitle-track-tab${subtitleTrack === track.id ? " active" : ""}`}
              data-testid={`subtitle-track-extra-${track.id}`}
              aria-pressed={subtitleTrack === track.id}
              disabled={!enabled}
              onClick={() => setSubtitleTrack(track.id)}
              title={enabled ? `Switch to ${label} captions` : `Generate captions from ${label} first`}
            >
              {label} <span className="subtitle-track-count">{segs.length}</span>
              {trackStreaming && <span className="subtitle-track-dot" aria-label="transcribing" />}
            </button>
          );
        })}
      </div>
      <div className="pane-body compact">
        {transcribing && segments.length > 0 && (
          <div className="transcribing-strip" data-testid="transcribing-strip">
            <span className="transcribing-dot" aria-hidden />
            <span>Transcribing… {segments.length} segment{segments.length === 1 ? "" : "s"} so far · {progressPercent}%</span>
          </div>
        )}
        {segments.length === 0 ? (
          transcribing ? (
            <div className="transcribing-empty" data-testid="transcribing-empty">
              <div className="transcribing-spinner-wrap" aria-hidden>
                <div className="transcribing-spinner" />
                <span className="transcribing-percent">{progressPercent}%</span>
              </div>
              <div className="transcribing-title">Transcribing with Whisper…</div>
              <div className="transcribing-hint">Segments will appear here as they're recognised.</div>
            </div>
          ) : (
            <p style={{ color: "var(--fg-muted)", fontSize: 13 }}>
              No speech detected yet.
            </p>
          )
        ) : (
          <div className="segments" data-testid="segments-list">
            {segments.map((seg, i) => (
              <div
                key={i}
                className={`segment${i === activeIdx ? " active" : ""}`}
                data-testid={`segment-${i}`}
                data-active={i === activeIdx ? "1" : "0"}
              >
                <div className="segment-time">
                  <input
                    type="number"
                    step="0.1"
                    value={seg.start}
                    data-testid={`segment-${i}-start`}
                    aria-label={`start ${fmtTimestamp(seg.start)}`}
                    onChange={(e) =>
                      updateSegment(i, { start: Number(e.target.value) })
                    }
                  />
                  <input
                    type="number"
                    step="0.1"
                    value={seg.end}
                    data-testid={`segment-${i}-end`}
                    aria-label={`end ${fmtTimestamp(seg.end)}`}
                    onChange={(e) =>
                      updateSegment(i, { end: Number(e.target.value) })
                    }
                  />
                </div>
                <input
                  type="text"
                  className="segment-text"
                  value={seg.text}
                  data-testid={`segment-${i}-text`}
                  onChange={(e) => updateSegment(i, { text: e.target.value })}
                />
                <button
                  className="segment-del"
                  onClick={() => deleteSegment(i)}
                  data-testid={`segment-${i}-delete`}
                  aria-label={`delete segment ${i}`}
                  title="Delete segment (Del at playhead)"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
