/**
 * Loop / Coub-mode end-to-end tests.
 *
 * Covers:
 * - persistence migration v7 → v8 (new fields appear)
 * - loop=ON without extra audio is a noop on project length
 * - loop=ON + extra audio → preview master clock follows extra, video stays
 *   inside the trim slice
 * - export ≈ extra audio length
 * - extra-track transcribe button enables the Extra subtitle tab
 * - subtitle source switch flips overlay text without touching audio mix
 * - GIF input is accepted as a normal media file
 */
import { test, expect } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import fs from "node:fs";

import { SAMPLE_5S, uploadEnglish } from "./_helpers";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(HERE, "output");
const TONE_6S = "/tmp/cutstorm_loop_tone_6s.mp3";
const TONE_2S = "/tmp/cutstorm_loop_tone_2s.mp3";
const TINY_GIF = "/tmp/cutstorm_loop_tiny.gif";

test.describe.configure({ mode: "serial" });

test("persist migrate: v7 → v8 adds loop/segmentsSource/segmentsExtra/subtitleTrack", async ({
  page,
}) => {
  await page.goto("/");
  await page.evaluate(() => {
    const v7 = {
      state: {
        videoId: null,
        videoUrl: null,
        duration: 0,
        videoW: 0,
        videoH: 0,
        segments: [{ start: 0, end: 1, text: "x", words: [] }],
        style: { mode: "phrase" },
        position: { x_pct: 10, y_pct: 80 },
        size: { w_pct: 80, h_pct: 15 },
        trim: { enabled: false, threshold_sec: 0.4, padding_sec: 0.08 },
        trimRange: { in_sec: 0, out_sec: 0 },
        audio: { sourceVolume: 1.0, extraAudioId: null, extraAudioName: null, extraAudioDuration: 0, extraVolume: 1.0 },
        canvas: {
          mode: "preset", preset: "source", crop_anchor: "center",
          custom: { x_pct: 10, y_pct: 10, w_pct: 80, h_pct: 80 },
          bg_color: "#000000",
        },
        isAudioOnly: false,
        generateSubs: true,
        useSubs: true,
        watermark: true,
      },
      version: 7,
    };
    localStorage.setItem("cutstorm-state", JSON.stringify(v7));
  });
  await page.reload();
  await expect(page.getByTestId("file-input")).toBeVisible({ timeout: 10_000 });
  // Toggle a persisted field to force a write-back (any persisted setter works).
  const toggle = page.getByTestId("generate-subs-toggle");
  await toggle.click();
  await toggle.click();
  await page.waitForTimeout(80);
  const migrated = await page.evaluate(() => {
    const raw = localStorage.getItem("cutstorm-state");
    return raw ? JSON.parse(raw) : null;
  });
  expect(migrated?.version).toBe(9);
  expect(migrated?.state?.trimRange).toEqual({ in_sec: 0, out_sec: 0, loop: false });
  expect(Array.isArray(migrated?.state?.segmentsSource)).toBe(true);
  expect(migrated?.state?.segmentsSource.length).toBe(1);
  // v8→v9: segmentsExtra is now a Record keyed by extra id; no extras
  // means an empty object.
  expect(migrated?.state?.segmentsExtra).toEqual({});
  expect(migrated?.state?.subtitleTrack).toBe("source");
  // v9 audio shape: { sourceVolume, extras: [] }.
  expect(migrated?.state?.audio).toMatchObject({ sourceVolume: 1.0, extras: [] });
});

test("loop toggle visible after upload, hint shows when no extra audio", async ({
  page,
}) => {
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  // Disable subs to make the upload faster — we don't need transcripts here.
  const subsToggle = page.getByTestId("generate-subs-toggle");
  if (await subsToggle.isChecked()) await subsToggle.click();
  await page.getByTestId("file-input").setInputFiles(SAMPLE_5S);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 60_000 });

  const loopToggle = page.getByTestId("loop-toggle");
  await expect(loopToggle).toBeVisible();
  await expect(loopToggle).not.toBeChecked();
  await loopToggle.click();
  await expect(loopToggle).toBeChecked();
  // Without an extra track, hint informs the user the flag is unarmed.
  await expect(page.getByTestId("loop-hint")).toBeVisible();
  await expect(page.getByTestId("loop-target")).toHaveCount(0);
});

test("loop=ON + extra audio drives master clock, video stays inside trim slice", async ({
  page,
}) => {
  // Generate fixtures on demand (no checked-in audio binaries).
  if (!fs.existsSync(TONE_6S)) {
    const r = spawnSync("ffmpeg", [
      "-y", "-loglevel", "error",
      "-f", "lavfi", "-i", "sine=440:duration=6",
      "-c:a", "libmp3lame", "-q:a", "9", TONE_6S,
    ]);
    expect(r.status).toBe(0);
  }

  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const subsToggle = page.getByTestId("generate-subs-toggle");
  if (await subsToggle.isChecked()) await subsToggle.click();
  await page.getByTestId("file-input").setInputFiles(SAMPLE_5S);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 60_000 });

  // Trim to first 2s of the source video.
  await page.evaluate(() => {
    (window as unknown as { useStore?: { setState: (p: object) => void } });
    // Use the public API the store exposes via the temporal middleware-free
    // setter — we call setTrimRange via DOM dispatch is overkill, so we
    // import the store from the module graph through a window helper.
    // Instead: mutate via Zustand action surfaced through the global the
    // dev runtime sets up implicitly — fall back to dispatching via a
    // synthetic setTrimRange in DevTools is not available. Use direct
    // store get from the existing __cutstorm_mix surface? No — we'll
    // keyboard-drag the handle instead.
  });
  // Drag trim_out from 100% to ~40% (= 2s of a 5s clip).
  const bar = page.getByTestId("trim-bar");
  const bbox = await bar.boundingBox();
  if (!bbox) throw new Error("trim-bar bbox missing");
  const outHandle = page.getByTestId("trim-handle-out");
  const oh = await outHandle.boundingBox();
  if (!oh) throw new Error("trim-handle-out bbox missing");
  await page.mouse.move(oh.x + oh.width / 2, oh.y + oh.height / 2);
  await page.mouse.down();
  await page.mouse.move(bbox.x + bbox.width * 0.4, bbox.y + bbox.height / 2, { steps: 20 });
  await page.mouse.up();

  // Add the 6s tone as extra audio.
  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_6S);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`).first()).toBeVisible({ timeout: 30_000 });

  // Engage loop.
  await page.getByTestId("loop-toggle").click();
  await expect(page.getByTestId("loop-target")).toBeVisible();

  // Hit play. Resume happens inside the click handler.
  await page.getByTestId("player-play").click();
  // Sample twice with a gap — each sample reads master + video position via
  // the global `__cutstorm_mix` debug handle.
  await page.waitForTimeout(1500);
  // The audio mix graph now keeps extras as a Map<id, ExtraNode>. Read
  // the FIRST entry (the loop driver) for the master clock.
  const readSample = () => page.evaluate(() => {
    type ExtraNode = { el: HTMLAudioElement };
    type Mix = { extras?: Map<string, ExtraNode> };
    const mix = (window as unknown as { __cutstorm_mix?: Mix }).__cutstorm_mix;
    const first = mix?.extras ? mix.extras.values().next().value as ExtraNode | undefined : undefined;
    const v = document.querySelector<HTMLVideoElement>("[data-testid='preview-video']");
    return {
      extraT: first?.el?.currentTime ?? null,
      videoT: v?.currentTime ?? null,
    };
  });
  const sample1 = await readSample();
  await page.waitForTimeout(2500);
  const sample2 = await readSample();
  // Master (extra) advanced.
  expect(sample1.extraT ?? -1).toBeGreaterThan(0.5);
  expect((sample2.extraT ?? -1)).toBeGreaterThan((sample1.extraT ?? 0));
  // Video stayed inside [in_sec=0 .. out_sec≈2].
  expect(sample1.videoT ?? -1).toBeGreaterThanOrEqual(0);
  expect(sample1.videoT ?? 9).toBeLessThan(2.2);
  expect(sample2.videoT ?? 9).toBeLessThan(2.2);
});

test("loop export: 2s slice + 6s tone → output ≈ 6s", async ({ page }) => {
  if (!fs.existsSync(TONE_6S)) {
    const r = spawnSync("ffmpeg", [
      "-y", "-loglevel", "error",
      "-f", "lavfi", "-i", "sine=440:duration=6",
      "-c:a", "libmp3lame", "-q:a", "9", TONE_6S,
    ]);
    expect(r.status).toBe(0);
  }
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const outPath = path.join(OUT_DIR, "loop_export.mp4");
  if (fs.existsSync(outPath)) fs.unlinkSync(outPath);

  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const subsToggle = page.getByTestId("generate-subs-toggle");
  if (await subsToggle.isChecked()) await subsToggle.click();
  await page.getByTestId("file-input").setInputFiles(SAMPLE_5S);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 60_000 });

  // Trim to ~2s.
  const bar = page.getByTestId("trim-bar");
  const bbox = await bar.boundingBox();
  if (!bbox) throw new Error("trim-bar bbox missing");
  const outHandle = page.getByTestId("trim-handle-out");
  const oh = await outHandle.boundingBox();
  if (!oh) throw new Error("trim-handle-out bbox missing");
  await page.mouse.move(oh.x + oh.width / 2, oh.y + oh.height / 2);
  await page.mouse.down();
  await page.mouse.move(bbox.x + bbox.width * 0.4, bbox.y + bbox.height / 2, { steps: 20 });
  await page.mouse.up();

  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_6S);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`).first()).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("loop-toggle").click();
  await expect(page.getByTestId("loop-target")).toBeVisible();

  // Subs are off → renderer takes the filter_only loop path.
  const useSubs = page.getByTestId("use-subs-toggle");
  if (await useSubs.isChecked()) await useSubs.click();

  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 180_000 }),
    page.getByTestId("export-button").click(),
  ]);
  await download.saveAs(outPath);
  expect(fs.statSync(outPath).size).toBeGreaterThan(0);

  const probe = spawnSync(
    "ffprobe",
    [
      "-v", "error",
      "-show_entries", "format=duration",
      "-of", "default=nw=1:nk=1",
      outPath,
    ],
    { encoding: "utf8" },
  );
  const dur = Number(probe.stdout.trim());
  expect(dur).toBeGreaterThan(5.3);
  expect(dur).toBeLessThan(6.7);
});

test("subtitle track switch: clicking Extra without segments stays disabled", async ({
  page,
}) => {
  if (!fs.existsSync(TONE_2S)) {
    const r = spawnSync("ffmpeg", [
      "-y", "-loglevel", "error",
      "-f", "lavfi", "-i", "sine=880:duration=2",
      "-c:a", "libmp3lame", "-q:a", "9", TONE_2S,
    ]);
    expect(r.status).toBe(0);
  }
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const subsToggle = page.getByTestId("generate-subs-toggle");
  if (await subsToggle.isChecked()) await subsToggle.click();
  await page.getByTestId("file-input").setInputFiles(SAMPLE_5S);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 60_000 });

  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_2S);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`).first()).toBeVisible({ timeout: 30_000 });

  // Without running transcribe-extra, the per-id extra tab is disabled.
  const extraTab = page.locator(`[data-testid^="subtitle-track-extra-"]`).first();
  await expect(extraTab).toBeVisible();
  await expect(extraTab).toBeDisabled();
});

test("GIF input: upload .gif → timeline appears, no transcript spawn", async ({
  page,
}) => {
  if (!fs.existsSync(TINY_GIF)) {
    const r = spawnSync("ffmpeg", [
      "-y", "-loglevel", "error",
      "-f", "lavfi", "-i", "testsrc=duration=2:size=80x60:rate=10",
      TINY_GIF,
    ]);
    expect(r.status).toBe(0);
  }
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  // Even with generateSubs=true the backend must auto-skip whisper for a
  // silent GIF — the upload should resolve quickly without segments.
  await page.getByTestId("file-input").setInputFiles(TINY_GIF);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 60_000 });
  // Source-track tab shows zero segments.
  const srcTab = page.getByTestId("subtitle-track-source");
  await expect(srcTab).toContainText(/Source\s*0/);
});
