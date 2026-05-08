import { CSSProperties, ReactNode, useEffect, useRef, useState } from "react";
import { Segment, Word, useStore } from "../store";
import { styleToCss } from "../styleToCss";
import { chunksBySentence } from "../sentenceChunks";

type Props = {
  videoRef: React.RefObject<HTMLMediaElement>;
  /** Offline frame-render mode: no drag, no rAF polling, currentTime from prop. */
  renderMode?: boolean;
  currentTimeOverride?: number;
};

type DragMode =
  | { kind: "move"; startX: number; startY: number; origX: number; origY: number }
  | {
      kind: "resize";
      corner: "tl" | "tr" | "bl" | "br";
      startX: number;
      startY: number;
      origX: number;
      origY: number;
      origW: number;
      origH: number;
    }
  | null;

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

function wordsOf(seg: Segment): Word[] {
  if (seg.words && seg.words.length) return seg.words;
  const toks = seg.text.trim().split(/\s+/).filter(Boolean);
  if (!toks.length) return [];
  const total = Math.max(seg.end - seg.start, 0.01);
  const step = total / toks.length;
  return toks.map((t, i) => ({
    start: seg.start + i * step,
    end: seg.start + (i + 1) * step,
    text: t,
  }));
}

function activeSegment(segments: Segment[], t: number): Segment | null {
  return segments.find((s) => t >= s.start && t <= s.end) ?? null;
}

export function SubtitleOverlay({ videoRef, renderMode = false, currentTimeOverride }: Props) {
  const segments = useStore((s) => s.segments);
  const style = useStore((s) => s.style);
  const position = useStore((s) => s.position);
  const size = useStore((s) => s.size);
  const setPosition = useStore((s) => s.setPosition);
  const setSize = useStore((s) => s.setSize);
  const subtitleTrack = useStore((s) => s.subtitleTrack);
  const trimRange = useStore((s) => s.trimRange);
  const extras = useStore((s) => s.audio.extras);
  const storeCurrentTime = useStore((s) => s.currentTime);

  const [currentTime, setLocalTime] = useState(0);
  const dragRef = useRef<DragMode>(null);
  const overlayRef = useRef<HTMLDivElement>(null);

  // Loop driver = first extra; its duration is the master timeline length.
  const driverDuration = extras[0]?.duration ?? 0;
  const trimOut = trimRange.out_sec > 0 ? trimRange.out_sec : 0;
  const loopClipDuration = Math.max(0, trimOut - trimRange.in_sec);
  const loopActive = !!trimRange.loop
    && extras.length > 0
    && driverDuration > 0
    && loopClipDuration > 0;

  // requestAnimationFrame loop while playing → smooth karaoke updates
  // (~16 ms granularity), independent of the browser's lazy `timeupdate`
  // which fires only every ~250 ms and skips short words.
  useEffect(() => {
    if (renderMode) return; // offline render: no rAF, time comes from prop
    if (loopActive) return; // loop branch reads master from the store.
    const v = videoRef.current;
    if (!v) return;
    let raf = 0;
    let alive = true;
    const tick = () => {
      if (!alive) return;
      setLocalTime(v.currentTime);
      raf = requestAnimationFrame(tick);
    };
    const start = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(tick);
    };
    const stop = () => {
      cancelAnimationFrame(raf);
      setLocalTime(v.currentTime);
    };
    v.addEventListener("play", start);
    v.addEventListener("pause", stop);
    v.addEventListener("ended", stop);
    v.addEventListener("seeked", () => setLocalTime(v.currentTime));
    setLocalTime(v.currentTime);
    if (!v.paused) start();
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
      v.removeEventListener("play", start);
      v.removeEventListener("pause", stop);
      v.removeEventListener("ended", stop);
    };
  }, [videoRef, renderMode, loopActive]);

  // In loop mode the master clock is the extra <audio>; the VideoPreview
  // rAF loop publishes it to the store as `currentTime`. Mirror it here.
  useEffect(() => {
    if (!loopActive || renderMode) return;
    setLocalTime(storeCurrentTime);
  }, [loopActive, renderMode, storeCurrentTime]);

  // Map the master clock onto the timeline of the active subtitle track:
  // - extra subs ride the master extra-audio timeline (0..extraDur).
  // - source subs (loop=ON) need phase mapping: master mod loopClipDur,
  //   then offset by trim_in to land in the original video's coordinates.
  // - source subs (loop=OFF) read the video's currentTime directly.
  const masterTime = renderMode ? (currentTimeOverride ?? 0) : currentTime;
  let effectiveTime = masterTime;
  if (loopActive && subtitleTrack === "source" && loopClipDuration > 0) {
    const phase = ((masterTime % loopClipDuration) + loopClipDuration) % loopClipDuration;
    effectiveTime = trimRange.in_sec + phase;
  }

  function parentRect(): { w: number; h: number } | null {
    const parent = overlayRef.current?.parentElement;
    if (!parent) return null;
    const r = parent.getBoundingClientRect();
    return { w: r.width, h: r.height };
  }

  function onPointerMove(e: PointerEvent) {
    const mode = dragRef.current;
    if (!mode) return;
    const rect = parentRect();
    if (!rect) return;

    const dxPct = ((e.clientX - mode.startX) / rect.w) * 100;
    const dyPct = ((e.clientY - mode.startY) / rect.h) * 100;

    if (mode.kind === "move") {
      const newX = clamp(mode.origX + dxPct, 0, 100 - size.w_pct);
      const newY = clamp(mode.origY + dyPct, 0, 100 - size.h_pct);
      setPosition({ x_pct: newX, y_pct: newY });
    } else {
      let { origX, origY, origW, origH } = mode;
      let newX = origX;
      let newY = origY;
      let newW = origW;
      let newH = origH;
      if (mode.corner === "br") {
        newW = origW + dxPct;
        newH = origH + dyPct;
      } else if (mode.corner === "tr") {
        newW = origW + dxPct;
        newH = origH - dyPct;
        newY = origY + dyPct;
      } else if (mode.corner === "bl") {
        newW = origW - dxPct;
        newH = origH + dyPct;
        newX = origX + dxPct;
      } else if (mode.corner === "tl") {
        newW = origW - dxPct;
        newH = origH - dyPct;
        newX = origX + dxPct;
        newY = origY + dyPct;
      }
      newW = clamp(newW, 5, 100);
      newH = clamp(newH, 5, 100);
      newX = clamp(newX, 0, 100 - newW);
      newY = clamp(newY, 0, 100 - newH);
      setPosition({ x_pct: newX, y_pct: newY });
      setSize({ w_pct: newW, h_pct: newH });
    }
  }

  function onPointerUp() {
    dragRef.current = null;
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
  }

  function startMove(e: React.PointerEvent) {
    e.stopPropagation();
    dragRef.current = {
      kind: "move",
      startX: e.clientX,
      startY: e.clientY,
      origX: position.x_pct,
      origY: position.y_pct,
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
  }

  function startResize(corner: "tl" | "tr" | "bl" | "br") {
    return (e: React.PointerEvent) => {
      e.stopPropagation();
      dragRef.current = {
        kind: "resize",
        corner,
        startX: e.clientX,
        startY: e.clientY,
        origX: position.x_pct,
        origY: position.y_pct,
        origW: size.w_pct,
        origH: size.h_pct,
      };
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", onPointerUp);
    };
  }

  const seg = activeSegment(segments, effectiveTime);

  let body: ReactNode = "";
  let rawText = "";

  if (seg) {
    if (style.mode === "phrase") {
      body = seg.text;
      rawText = seg.text;
    } else if (style.mode === "word") {
      const words = wordsOf(seg);
      const chunkSize = Math.max(1, style.words_per_chunk);
      // Build all chunks, then pick the latest one whose start <= effectiveTime.
      // Same anti-skip logic as karaoke — guarantees no chunk is missed.
      const chunks: { start: number; end: number; text: string }[] =
        chunksBySentence(words, chunkSize).map((c) => ({
          start: c[0].start,
          end: c[c.length - 1].end,
          text: c.map((w) => w.text).join(" "),
        }));
      let activeChunkIdx = -1;
      for (let i = 0; i < chunks.length; i++) {
        if (chunks[i].start <= effectiveTime) activeChunkIdx = i;
        else break;
      }
      if (activeChunkIdx >= 0) {
        body = chunks[activeChunkIdx].text;
        rawText = chunks[activeChunkIdx].text;
      }
    } else if (style.mode === "karaoke") {
      // Chunk words into N-per-line windows. Show only the current chunk and
      // highlight the active word inside it. Same "latest start ≤ now" logic
      // as word mode, applied both at chunk level and word-within-chunk level
      // to guarantee smooth highlight with no gaps.
      const words = wordsOf(seg);
      const chunkSize = Math.max(1, style.words_per_chunk);
      const chunks: Word[][] = chunksBySentence(words, chunkSize);
      let activeChunkIdx = -1;
      for (let i = 0; i < chunks.length; i++) {
        if (chunks[i][0].start <= effectiveTime) activeChunkIdx = i;
        else break;
      }
      const activeChunk = chunks[activeChunkIdx] ?? [];
      let activeWordIdx = -1;
      for (let i = 0; i < activeChunk.length; i++) {
        if (activeChunk[i].start <= effectiveTime) activeWordIdx = i;
        else break;
      }
      rawText = activeChunk.map((w) => w.text).join(" ");
      body = (
        <>
          {activeChunk.map((w, i) => {
            const active = i === activeWordIdx;
            return (
              <span
                key={i}
                data-active={active ? "1" : "0"}
                style={{
                  color: active ? style.active_word_color : undefined,
                  fontWeight: active ? 700 : undefined,
                  transition: renderMode ? "none" : "color 80ms ease",
                }}
              >
                {w.text}
                {i < activeChunk.length - 1 ? " " : ""}
              </span>
            );
          })}
        </>
      );
    }
  }

  const containerStyle: CSSProperties = {
    left: `${position.x_pct}%`,
    top: `${position.y_pct}%`,
    width: `${size.w_pct}%`,
    height: `${size.h_pct}%`,
    cursor: renderMode ? "default" : "move",
  };

  const textStyle = styleToCss(style);

  return (
    <div
      ref={overlayRef}
      className={`overlay${renderMode ? " overlay-render" : ""}`}
      style={containerStyle}
      data-testid="subtitle-overlay"
      data-x={position.x_pct.toFixed(2)}
      data-y={position.y_pct.toFixed(2)}
      data-w={size.w_pct.toFixed(2)}
      data-h={size.h_pct.toFixed(2)}
      data-mode={style.mode}
      onPointerDown={renderMode ? undefined : startMove}
    >
      {!renderMode && <div className="overlay-frame" aria-hidden />}
      <div
        style={{ ...textStyle, width: "100%" }}
        data-testid="subtitle-text"
        data-raw={rawText}
      >
        {body}
      </div>
      {!renderMode && (
        <>
          <div className="overlay-handle tl" data-testid="overlay-handle-tl" onPointerDown={startResize("tl")} />
          <div className="overlay-handle tr" data-testid="overlay-handle-tr" onPointerDown={startResize("tr")} />
          <div className="overlay-handle bl" data-testid="overlay-handle-bl" onPointerDown={startResize("bl")} />
          <div className="overlay-handle br" data-testid="overlay-handle-br" onPointerDown={startResize("br")} />
        </>
      )}
    </div>
  );
}
