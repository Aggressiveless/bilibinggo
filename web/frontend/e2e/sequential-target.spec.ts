import { expect, test } from "./offline-test";
import { gotoOverview, setE2EState } from "./helpers";

test("new-video target survives reload, shows duplicate skips and never calls a short run complete", async ({ page, request }) => {
  await setE2EState(request, { account: "logged_in", llm: "ready" });
  let snapshot: any = { job: null, keywords: ["转发抽奖"], summary: {}, resume_available: false };
  let starts = 0;
  await page.route("**/api/discovery/*", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/start")) {
      starts++;
      expect(route.request().postDataJSON()).toEqual({
        keywords: ["转发抽奖"], keyword_match: "combined", mode: "standard", target_new_videos: 175, min_interval_seconds: 2,
      });
      snapshot = { ...snapshot, target_new_videos: 175,
        job: { job_id: 88, state: "running", cancel_requested: false, progress: { message: "翻页检索中" } },
        summary: { target_new_videos: 175, new_videos_checked: 72, videos_checked: 72, duplicates_skipped: 48 } };
    }
    await route.fulfill({ json: snapshot });
  });
  await gotoOverview(page);
  await page.locator('[data-section="sources"]').click();
  await page.getByLabel("目标新视频数").fill("175");
  await page.locator("#discovery-start").click();
  await expect(page.locator("#discovery-target-label")).toHaveText("新视频 72 / 175");
  await expect(page.locator("#discovery-duplicates")).toHaveText("48");
  await expect(page.getByLabel("目标新视频数")).toBeDisabled();
  await page.reload();
  await page.locator('[data-section="sources"]').click();
  await expect(page.getByLabel("目标新视频数")).toHaveValue("175");
  await expect(page.locator("#discovery-stop")).toBeEnabled();
  snapshot = { ...snapshot, job: { ...snapshot.job, state: "success" },
    summary: { ...snapshot.summary, completion_reason: "search_exhausted", eligible: 2, search_exhausted: true } };
  await page.reload();
  await page.locator('[data-section="sources"]').click();
  await expect(page.locator("#discovery-badge")).toHaveText("新视频不足");
  await expect(page.locator("#discovery-status")).toContainText("72 / 175");
  await expect(page.locator("#discovery-status")).not.toContainText("完成");
  expect(starts).toBe(1);
});

test("automatic sequential participation is compact on desktop and mobile", async ({ page, request }, testInfo) => {
  await setE2EState(request, { account: "logged_in", llm: "ready" });
  let submitted: any;
  await page.route("**/api/jobs?enqueue=true", async route => {
    submitted = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, job: {}, queue_entry: { state: "queued", label: "自动顺序参与" } } });
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 1440, height: 960 });
  await gotoOverview(page);
  await page.locator('[data-section="activities"]').click();
  const bar = page.locator("#sequential-participate-bar");
  await expect(bar.getByRole("heading")).toHaveText("自动顺序参与");
  await expect(bar).not.toContainText("三连");
  await expect(page.locator("#triple-participate-bar, .triple-target-chip")).toHaveCount(0);
  await expect(bar.getByRole("button")).toHaveText("开始顺序参与");
  const count = await page.locator("#sequential-participate-targets").textContent();
  expect(count).toMatch(/\d+ 个活动待参与/);
  await bar.getByRole("button").click();
  await expect.poll(() => submitted?.action).toBe("participate_sequential");
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 960 });
    if (width === 390) await expect(page.locator("#sidebar")).not.toBeInViewport();
    await expect(bar).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`participation-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.locator('[data-section="sources"]').click();
  await expect(page.getByLabel("目标新视频数")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("discovery-1440.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 960 });
  await expect(page.locator("#sidebar")).not.toBeInViewport();
  await expect(page.getByLabel("目标新视频数")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("discovery-390.png"), fullPage: true });
});
