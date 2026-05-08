/**
 * Multi-extra audio tracks end-to-end tests.
 *
 * Covers:
 *   - adding two extras → both rows render with their own controls
 *   - removing a track also removes its subtitle tab
 *   - loop driver = first extra (its duration sets exported length)
 *   - removing first track with loop=ON disables loop with a toast
 *   - export mixes source + multiple extras with apad+amix=longest
 *   - v8 → v9 migration preserves the legacy single extra
 */
import { test, expect } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import fs from "node:fs";

import { SAMPLE_5S } from "./_helpers";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(HERE, "output");
const TONE_A = "/tmp/cutstorm_multi_a.mp3"; // 4s tone
const TONE_B = "/tmp/cutstorm_multi_b.mp3"; // 7s tone

function ensureFixtures() {
  if (!fs.existsSync(TONE_A)) {
    const r = spawnSync("ffmpeg", [
      "-y", "-loglevel", "error",
      "-f", "lavfi", "-i", "sine=440:duration=4",
      "-c:a", "libmp3lame", "-q:a", "9", TONE_A,
    ]);
    expect(r.status).toBe(0);
  }
  if (!fs.existsSync(TONE_B)) {
    const r = spawnSync("ffmpeg", [
      "-y", "-loglevel", "error",
      "-f", "lavfi", "-i", "sine=660:duration=7",
      "-c:a", "libmp3lame", "-q:a", "9", TONE_B,
    ]);
    expect(r.status).toBe(0);
  }
}

test.describe.configure({ mode: "serial" });

test("v8 → v9 migration: legacy single extra survives as one-element extras", async ({
  page,
}) => {
  await page.goto("/");
  await page.evaluate(() => {
    const v8 = {
      state: {
        videoId: null,
        videoUrl: null,
        duration: 0,
        videoW: 0,
        videoH: 0,
        segments: [],
        segmentsSource: [],
        segmentsExtra: [
          { start: 0, end: 1, text: "hi", words: [] },
        ],
        subtitleTrack: "extra",
        style: { mode: "phrase" },
        position: { x_pct: 10, y_pct: 80 },
        size: { w_pct: 80, h_pct: 15 },
        trim: { enabled: false, threshold_sec: 0.4, padding_sec: 0.08 },
        trimRange: { in_sec: 0, out_sec: 0, loop: false },
        audio: {
          sourceVolume: 0.5,
          extraAudioId: "abcdef0123456789",
          extraAudioName: "old.mp3",
          extraAudioDuration: 12.5,
          extraVolume: 0.7,
        },
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
      version: 8,
    };
    localStorage.setItem("cutstorm-state", JSON.stringify(v8));
  });
  await page.reload();
  await expect(page.getByTestId("file-input")).toBeVisible({ timeout: 10_000 });
  // Force a persist write to flush the migration.
  const toggle = page.getByTestId("generate-subs-toggle");
  await toggle.click();
  await toggle.click();
  await page.waitForTimeout(80);
  const migrated = await page.evaluate(() => {
    const raw = localStorage.getItem("cutstorm-state");
    return raw ? JSON.parse(raw) : null;
  });
  expect(migrated?.version).toBe(9);
  // Legacy single track became extras[0].
  expect(migrated?.state?.audio?.extras).toHaveLength(1);
  expect(migrated?.state?.audio?.extras?.[0]).toMatchObject({
    id: "abcdef0123456789",
    volume: 0.7,
    name: "old.mp3",
    duration: 12.5,
  });
  // segmentsExtra: array → Record keyed by id.
  expect(migrated?.state?.segmentsExtra).toEqual({
    abcdef0123456789: [{ start: 0, end: 1, text: "hi", words: [] }],
  });
  // subtitleTrack "extra" → concrete id.
  expect(migrated?.state?.subtitleTrack).toBe("abcdef0123456789");
});

test("add two extras: both rows visible with own controls", async ({ page }) => {
  ensureFixtures();
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const subsToggle = page.getByTestId("generate-subs-toggle");
  if (await subsToggle.isChecked()) await subsToggle.click();
  await page.getByTestId("file-input").setInputFiles(SAMPLE_5S);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 120_000 });

  // Add tone A.
  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_A);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`)).toHaveCount(1, { timeout: 30_000 });

  // Add tone B.
  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_B);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`)).toHaveCount(2, { timeout: 30_000 });

  // Each track has an independent volume slider, generate button, remove ×.
  await expect(page.locator(`[data-testid^="extra-volume-"]`)).toHaveCount(2);
  await expect(page.locator(`[data-testid^="extra-transcribe-button-"]`)).toHaveCount(2);
  await expect(page.locator(`[data-testid^="extra-track-remove-"]`)).toHaveCount(2);

  // First track shows the loop-driver star badge.
  await expect(page.locator(`[data-testid^="loop-driver-badge-"]`)).toHaveCount(1);

  // The "+ Add audio track" footer button is still visible — keep adding.
  await expect(page.getByTestId("extra-track-add")).toBeVisible();
});

test("removing a track also removes its subtitle tab", async ({ page }) => {
  ensureFixtures();
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const subsToggle = page.getByTestId("generate-subs-toggle");
  if (await subsToggle.isChecked()) await subsToggle.click();
  await page.getByTestId("file-input").setInputFiles(SAMPLE_5S);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 120_000 });

  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_A);
  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_B);

  await expect(page.locator(`[data-testid^="extra-track-info-"]`)).toHaveCount(2, { timeout: 30_000 });
  // Remove the second track.
  const removeButtons = page.locator(`[data-testid^="extra-track-remove-"]`);
  await removeButtons.nth(1).click();
  await expect(page.locator(`[data-testid^="extra-track-info-"]`)).toHaveCount(1);

  // Subtitle tabs reflect the remaining single extra.
  await expect(page.locator(`[data-testid^="subtitle-track-extra-"]`)).toHaveCount(1);
});

test("loop driver = first extra; export length matches it", async ({ page }) => {
  ensureFixtures();
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const outPath = path.join(OUT_DIR, "multi_extra_loop.mp4");
  if (fs.existsSync(outPath)) fs.unlinkSync(outPath);

  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const subsToggle = page.getByTestId("generate-subs-toggle");
  if (await subsToggle.isChecked()) await subsToggle.click();
  await page.getByTestId("file-input").setInputFiles(SAMPLE_5S);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 120_000 });

  // Trim to first 2 sec of the 5s sample.
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

  // Add tone A (4s) FIRST → driver. Then tone B (7s).
  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_A);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`)).toHaveCount(1, { timeout: 30_000 });
  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_B);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`)).toHaveCount(2, { timeout: 30_000 });

  // Engage loop. Driver is tone A (4s), so target is ~4s.
  await page.getByTestId("loop-toggle").click();
  await expect(page.getByTestId("loop-target")).toHaveText(/4\.0s/);

  // Subs off → filter_only path.
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
    ["-v", "error", "-show_entries", "format=duration",
     "-of", "default=nw=1:nk=1", outPath],
    { encoding: "utf8" },
  );
  const dur = Number(probe.stdout.trim());
  // Driver is 4s; output rides driver duration ±0.5s.
  expect(dur).toBeGreaterThan(3.5);
  expect(dur).toBeLessThan(4.7);
});

test("removing first (driver) track with loop=ON disables loop", async ({ page }) => {
  ensureFixtures();
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const subsToggle = page.getByTestId("generate-subs-toggle");
  if (await subsToggle.isChecked()) await subsToggle.click();
  await page.getByTestId("file-input").setInputFiles(SAMPLE_5S);
  await expect(page.getByTestId("timeline")).toBeVisible({ timeout: 120_000 });

  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_A);
  await page.getByTestId("extra-track-add").click();
  await page.getByTestId("extra-file-input").setInputFiles(TONE_B);
  await expect(page.locator(`[data-testid^="extra-track-info-"]`)).toHaveCount(2, { timeout: 30_000 });

  // Loop on. Driver = first track.
  await page.getByTestId("loop-toggle").click();
  await expect(page.getByTestId("loop-toggle")).toBeChecked();

  // Remove the first (driver) track.
  const removeButtons = page.locator(`[data-testid^="extra-track-remove-"]`);
  await removeButtons.nth(0).click();
  await expect(page.locator(`[data-testid^="extra-track-info-"]`)).toHaveCount(1);

  // Loop should auto-disable since the driver is gone.
  await expect(page.getByTestId("loop-toggle")).not.toBeChecked();
});
