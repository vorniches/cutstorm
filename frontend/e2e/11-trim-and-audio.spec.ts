import { test, expect } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import fs from "node:fs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(HERE, "output");
const ROOT = path.resolve(HERE, "../..");
const IMG_4934 = path.resolve(ROOT, "IMG_4934.MP4");
const BG_TONE = "/tmp/bg_tone.mp3";

test.describe.configure({ mode: "serial" });

test("persist migrate: v2 shape upgrades to current version without crashing", async ({
  page,
}) => {
  await page.goto("/");
  await page.evaluate(() => {
    // Seed a v2 payload. No `trimRange`, no `audio`. After reload the migrate
    // function must fill them with defaults — otherwise the preview crashes
    // when TrimTimeline reads `trimRange.in_sec`.
    const v2 = {
      state: {
        videoId: null,
        videoUrl: null,
        duration: 0,
        videoW: 0,
        videoH: 0,
        segments: [],
        style: { mode: "phrase" },
        position: { x_pct: 10, y_pct: 80 },
        size: { w_pct: 80, h_pct: 15 },
        trim: { enabled: false, threshold_sec: 0.4, padding_sec: 0.08 },
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
      },
      version: 2,
    };
    localStorage.setItem("cutstorm-state", JSON.stringify(v2));
  });
  await page.reload();
  // Uploader must render — no crash reading trimRange/audio on fresh boot.
  await expect(page.getByTestId("file-input")).toBeVisible({ timeout: 10_000 });
  // Force a write-back by toggling a persisted field, so the new version + new
  // fields land in localStorage. Then verify the migrated shape.
  await page.evaluate(() => {
    // `useStore` isn't on window, but persist writes on any set(); trigger a
    // no-op: clicking the already-checked "use subs" toggle twice.
  });
  // Toggle the "generate subs" switch twice — persisted field, triggers a write.
  const toggle = page.getByTestId("generate-subs-toggle");
  await toggle.click();
  await toggle.click();
  // Give the storage listener one tick.
  await page.waitForTimeout(50);
  const migrated = await page.evaluate(() => {
    const raw = localStorage.getItem("cutstorm-state");
    return raw ? JSON.parse(raw) : null;
  });
  expect(migrated?.version).toBe(9);
  // v8 added trimRange.loop; v9 keeps it.
  expect(migrated?.state?.trimRange).toEqual({ in_sec: 0, out_sec: 0, loop: false });
  // v9 audio shape: { sourceVolume, extras: [] } — legacy fields are absorbed.
  expect(migrated?.state?.audio).toMatchObject({
    sourceVolume: 1.0,
    extras: [],
  });
  // v8 seeded per-track segment lists; v9 turned segmentsExtra into a Record.
  expect(Array.isArray(migrated?.state?.segmentsSource)).toBe(true);
  expect(migrated?.state?.segmentsExtra).toEqual({});
  expect(migrated?.state?.subtitleTrack).toBe("source");
});

test("trim in=10 out=30 via UI → ffprobe exported duration ≈ 20s", async ({
  page,
}) => {
  test.skip(!fs.existsSync(IMG_4934), "IMG_4934.MP4 missing");

  fs.mkdirSync(OUT_DIR, { recursive: true });
  const outPath = path.join(OUT_DIR, "trim_ui.mp4");
  if (fs.existsSync(outPath)) fs.unlinkSync(outPath);

  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.getByTestId("file-input").setInputFiles(IMG_4934);

  // Wait for preview to mount; with generate_subs=true the SegmentList will
  // eventually fill but we don't need segments for a trim-only export.
  await expect(page.getByTestId("preview-video")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 30_000 });

  // Set trim via the store API (UI drag is flaky in a narrow viewport).
  await page.evaluate(() => {
    const store = (window as any).__store ?? null;
  });
  await page.evaluate(() => {
    const s = (window as any).useStore ?? null;
    // Fall back to the global only if exposed; otherwise dispatch via DOM.
  });
  // Drag the in-handle to ~10s of the clip.
  const bar = page.getByTestId("trim-bar");
  const box = await bar.boundingBox();
  if (!box) throw new Error("trim-bar bbox missing");
  // The IMG_4934 clip is ~110s. 10/110 ≈ 9.1% and 30/110 ≈ 27.3% of bar width.
  const inX = box.x + box.width * 0.091;
  const outX = box.x + box.width * 0.273;

  // Drag in-handle from 0% → 9.1%.
  const inHandle = page.getByTestId("trim-handle-in");
  const ih = await inHandle.boundingBox();
  if (!ih) throw new Error("in handle bbox missing");
  await page.mouse.move(ih.x + ih.width / 2, ih.y + ih.height / 2);
  await page.mouse.down();
  await page.mouse.move(inX, box.y + box.height / 2, { steps: 20 });
  await page.mouse.up();

  // Drag out-handle from 100% → 27.3%.
  const outHandle = page.getByTestId("trim-handle-out");
  const oh = await outHandle.boundingBox();
  if (!oh) throw new Error("out handle bbox missing");
  await page.mouse.move(oh.x + oh.width / 2, oh.y + oh.height / 2);
  await page.mouse.down();
  await page.mouse.move(outX, box.y + box.height / 2, { steps: 20 });
  await page.mouse.up();

  // Turn off subtitles so the export dispatches via filter_only (fast path).
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
  // Drag accuracy ±1s on a 1.5×-wide trim bar is plenty.
  expect(dur).toBeGreaterThan(18);
  expect(dur).toBeLessThan(22);
});

test("extra audio plays through WebAudio mix in preview", async ({ page }) => {
  test.skip(!fs.existsSync(IMG_4934), "IMG_4934.MP4 missing");
  if (!fs.existsSync(BG_TONE)) {
    const gen = spawnSync("ffmpeg", [
      "-y", "-f", "lavfi", "-i", "sine=880:d=10",
      "-c:a", "libmp3lame", "-q:a", "4", BG_TONE,
    ]);
    expect(gen.status).toBe(0);
  }

  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.getByTestId("file-input").setInputFiles(IMG_4934);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 60_000 });

  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(BG_TONE);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`).first()).toBeVisible({ timeout: 30_000 });

  // Click our custom toolbar play. resumeAudioContext fires inside the
  // click handler — same user-gesture tick — so the AudioContext should
  // transition from 'suspended' to 'running'.
  await page.getByTestId("player-play").click();

  await page.waitForTimeout(1200);
  const mixState = await page.evaluate(() => {
    const mix = (window as any).__cutstorm_mix;
    const video = document.querySelector<HTMLVideoElement>("[data-testid='preview-video']");
    return {
      hasExtra: !!mix?.extraEl,
      extraCurrentTime: mix?.extraEl?.currentTime ?? null,
      extraPaused: mix?.extraEl?.paused ?? null,
      ctxState: mix?.ctx?.state ?? null,
      videoPaused: video?.paused ?? null,
    };
  });
  expect(mixState.hasExtra).toBe(true);
  expect(mixState.ctxState).toBe("running");
  expect(mixState.videoPaused).toBe(false);
  expect(mixState.extraPaused).toBe(false);
  expect(mixState.extraCurrentTime).toBeGreaterThan(0.3);
});

test("extra audio upload → exported mp4 contains mixed audio", async ({
  page,
}) => {
  test.skip(!fs.existsSync(IMG_4934), "IMG_4934.MP4 missing");

  // Generate the tone fixture if needed.
  if (!fs.existsSync(BG_TONE)) {
    const gen = spawnSync("ffmpeg", [
      "-y", "-f", "lavfi", "-i", "sine=880:d=10",
      "-c:a", "libmp3lame", "-q:a", "4", BG_TONE,
    ]);
    expect(gen.status).toBe(0);
  }
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const outPath = path.join(OUT_DIR, "audio_mix.mp4");
  if (fs.existsSync(outPath)) fs.unlinkSync(outPath);

  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.getByTestId("file-input").setInputFiles(IMG_4934);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 60_000 });

  // Click "+ Add audio track" and pick the fixture.
  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(BG_TONE);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`).first()).toBeVisible({ timeout: 30_000 });

  // Pin the trim to the first 10s so duration matches the tone length.
  const bar = page.getByTestId("trim-bar");
  const bbox = await bar.boundingBox();
  if (!bbox) throw new Error("trim-bar bbox missing");
  const outHandle = page.getByTestId("trim-handle-out");
  const oh = await outHandle.boundingBox();
  if (!oh) throw new Error("out handle bbox missing");
  await page.mouse.move(oh.x + oh.width / 2, oh.y + oh.height / 2);
  await page.mouse.down();
  await page.mouse.move(bbox.x + bbox.width * (10 / 110), bbox.y + bbox.height / 2, { steps: 20 });
  await page.mouse.up();

  // Disable subs to use the fast filter_only path.
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
      "-select_streams", "a:0",
      "-show_entries", "stream=codec_name,channels:format=duration",
      "-of", "default=nw=1",
      outPath,
    ],
    { encoding: "utf8" },
  );
  expect(probe.stdout).toMatch(/codec_name=aac/);
  // Duration should be ~10s (our trim cap) — extra track ≥ video trim.
  const durMatch = probe.stdout.match(/duration=([\d.]+)/);
  expect(durMatch).not.toBeNull();
  const dur = Number(durMatch![1]);
  expect(dur).toBeGreaterThan(8);
  expect(dur).toBeLessThan(12);
});
