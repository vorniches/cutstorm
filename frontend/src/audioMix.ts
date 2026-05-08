/**
 * WebAudio-based preview mixer with N extra tracks.
 *
 * Goal: what the user hears in preview matches what the export produces —
 * source video at `sourceVolume`, plus every extra track at its own volume,
 * all summed into the destination.
 *
 * Graph:
 *   [video]  → MediaElementSource → srcGain ──┐
 *   [extra A] → MediaElementSource → gain_A ──┤
 *   [extra B] → MediaElementSource → gain_B ──┼─→ destination
 *   ...                                       ┘
 *
 * Caveats:
 * - `createMediaElementSource` can be called ONCE per element for the
 *   lifetime of the AudioContext. We cache source nodes keyed by element.
 * - AudioContext starts suspended in modern browsers; resume on first
 *   user-gesture-triggered play().
 * - Extra elements are kept in sync with the video on each event tick;
 *   the FIRST extra (the loop driver) becomes the master in loop mode.
 */

export type ExtraNode = {
  id: string;
  url: string;
  el: HTMLAudioElement;
  node: MediaElementAudioSourceNode;
  gain: GainNode;
};

type NodeSet = {
  ctx: AudioContext;
  srcNode: MediaElementAudioSourceNode;
  srcGain: GainNode;
  extras: Map<string, ExtraNode>;
};

let current: NodeSet | null = null;

const wrappedElements = new WeakMap<HTMLMediaElement, MediaElementAudioSourceNode>();

function getOrCreateCtx(): AudioContext {
  if (current?.ctx) return current.ctx;
  const Ctor: typeof AudioContext =
    (window as any).AudioContext || (window as any).webkitAudioContext;
  return new Ctor();
}

export function getAudioMix(): NodeSet | null {
  return current;
}

/**
 * Reconcile the WebAudio graph against `extras`: add nodes for new ids,
 * disconnect / drop nodes for ids no longer present, update gains for
 * existing ones. Idempotent — safe to call on every render.
 */
export function attachAudioMix(
  videoEl: HTMLMediaElement,
  extras: Array<{ id: string; url: string; volume: number }>,
): NodeSet {
  const ctx = getOrCreateCtx();
  let srcNode = wrappedElements.get(videoEl);
  if (!srcNode) {
    srcNode = ctx.createMediaElementSource(videoEl);
    wrappedElements.set(videoEl, srcNode);
  }
  const srcGain = current?.srcGain ?? ctx.createGain();
  try { srcNode.disconnect(); } catch { /* first wire */ }
  srcNode.connect(srcGain).connect(ctx.destination);

  const map = current?.extras ?? new Map<string, ExtraNode>();

  // 1. Drop nodes for ids no longer present.
  const incomingIds = new Set(extras.map((e) => e.id));
  for (const [id, node] of map.entries()) {
    if (!incomingIds.has(id)) {
      try { node.node.disconnect(); } catch { /* */ }
      try { node.gain.disconnect(); } catch { /* */ }
      try { node.el.pause(); } catch { /* */ }
      map.delete(id);
    }
  }

  // 2. Add or update nodes for incoming ids.
  for (const e of extras) {
    let entry = map.get(e.id);
    if (entry && entry.url !== e.url) {
      // URL changed (re-uploaded same id with a different blob) — rebuild.
      try { entry.node.disconnect(); } catch { /* */ }
      try { entry.gain.disconnect(); } catch { /* */ }
      try { entry.el.pause(); } catch { /* */ }
      map.delete(e.id);
      entry = undefined;
    }
    if (!entry) {
      const el = new Audio();
      el.preload = "auto";
      // No crossOrigin: blob URLs are same-origin and a stray "anonymous"
      // taints the WebAudio node, silencing the track.
      el.src = e.url;
      el.load();
      const node = ctx.createMediaElementSource(el);
      const gain = ctx.createGain();
      gain.gain.value = Math.max(0, Math.min(2, e.volume));
      node.connect(gain).connect(ctx.destination);
      el.addEventListener(
        "loadedmetadata",
        () => {
          if (!current) return;
          window.dispatchEvent(new CustomEvent("cutstorm:extra-ready", { detail: { id: e.id } }));
        },
        { once: true },
      );
      map.set(e.id, { id: e.id, url: e.url, el, node, gain });
    } else {
      entry.gain.gain.value = Math.max(0, Math.min(2, e.volume));
    }
  }

  current = { ctx, srcNode, srcGain, extras: map };
  // Debug handle for e2e tests / DevTools.
  (window as any).__cutstorm_mix = current;
  return current;
}

export function setSourceVolume(v: number): void {
  if (!current) return;
  current.srcGain.gain.value = Math.max(0, Math.min(2, v));
}

export function setExtraVolume(id: string, v: number): void {
  if (!current) return;
  const node = current.extras.get(id);
  if (!node) return;
  node.gain.gain.value = Math.max(0, Math.min(2, v));
}

export async function resumeAudioContext(): Promise<void> {
  if (!current) return;
  if (current.ctx.state === "suspended") {
    try { await current.ctx.resume(); } catch { /* */ }
  }
}

/**
 * Sync every extra <audio> element to the video's current time / play
 * state. Call from the video element's play/pause/seek/timeupdate events.
 *
 * Each extra plays unconditionally while the video plays; if a track is
 * shorter than the video (or shorter than the cursor), the element ends
 * naturally and emits silence through the WebAudio graph.
 */
export function syncExtraToVideo(video: HTMLMediaElement, trimIn: number = 0): void {
  const c = current;
  if (!c) return;
  const target = Math.max(0, video.currentTime - trimIn);
  for (const node of c.extras.values()) {
    const extra = node.el;
    if (Number.isFinite(target) && Math.abs(extra.currentTime - target) > 0.15) {
      try { extra.currentTime = target; } catch { /* */ }
    }
    if (video.paused) {
      if (!extra.paused) extra.pause();
    } else {
      if (extra.paused) extra.play().catch(() => {});
    }
  }
}

/**
 * Inverse of {@link syncExtraToVideo}: in loop mode the FIRST extra
 * (driver) provides the master clock. The video is repositioned each
 * frame to `trimIn + (master % loopClipDuration)`. All other extras are
 * synced to the master clock as well — they're independent tracks but
 * must stay in sync with the loop iteration the user is hearing.
 */
export function syncVideoToLoopedExtra(
  video: HTMLMediaElement,
  trimIn: number,
  loopClipDuration: number,
  driverId: string,
): { master: number; videoTarget: number } | null {
  const c = current;
  if (!c) return null;
  const driver = c.extras.get(driverId);
  if (!driver) return null;
  const master = driver.el.currentTime;
  if (!Number.isFinite(master)) return null;
  const phase = loopClipDuration > 0 ? (master % loopClipDuration) : 0;
  const target = trimIn + phase;
  if (Number.isFinite(target) && Math.abs(video.currentTime - target) > 0.05) {
    try { video.currentTime = target; } catch { /* */ }
  }
  // Mirror play/pause from the video element to all extras (video play
  // button is the user-visible control; extras follow).
  for (const node of c.extras.values()) {
    const extra = node.el;
    if (extra === driver.el) {
      // Driver itself is the master — only mirror play state, not seek.
      if (video.paused) {
        if (!extra.paused) extra.pause();
      } else {
        if (extra.paused) extra.play().catch(() => {});
      }
      continue;
    }
    // Non-driver extras follow the master clock so they stay in sync with
    // the iteration the user is hearing.
    if (Number.isFinite(master) && Math.abs(extra.currentTime - master) > 0.15) {
      try { extra.currentTime = master; } catch { /* */ }
    }
    if (video.paused) {
      if (!extra.paused) extra.pause();
    } else {
      if (extra.paused) extra.play().catch(() => {});
    }
  }
  return { master, videoTarget: target };
}
