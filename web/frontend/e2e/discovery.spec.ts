import { expect, test } from "./offline-test";
import { gotoOverview, setE2EState } from "./helpers";

test("deferred videos can be reviewed from the completed search", async ({ page, request }) => {
  await setE2EState(request, { account: "logged_in", llm: "ready" });
  let snapshot: any = { job: null, keywords: ["转发抽奖"], summary: {}, resume_available: false };
  await page.route("**/api/discovery/current", route => route.fulfill({ json: snapshot }));
  await page.route("**/api/discovery/review-deferred", route => {
    expect(route.request().postDataJSON()).toEqual({ job_id: 41 });
    snapshot = { ...snapshot, job: { job_id: 42, state: "running" }, review_deferred_available: false };
    return route.fulfill({ json: snapshot });
  });
  await gotoOverview(page);
  await page.locator('[data-section="sources"]').click();
  const review = page.locator("#discovery-review-deferred");
  await expect(review).toBeHidden();
  snapshot = { ...snapshot, job: { job_id: 41, state: "success" }, review_deferred_available: true,
    summary: { completion_reason: "target_reached", deferred_videos: 3, comment_reads_saved: 2 } };
  await page.reload();
  await page.locator('[data-section="sources"]').click();
  await expect(review).toHaveText("补查低信号视频（3）");
  await expect(page.locator("#discovery-status")).toContainText("省去 2 次评论读取");
  await review.click();
  await expect(review).toBeHidden();
  await expect(page.locator("#discovery-stop")).toBeEnabled();
});

test("users start, stop, reload and resume discovery", async ({ page, request }) => {
  await setE2EState(request, { account: "logged_in", llm: "ready" });
  let snapshot: any = { job: null, keywords: ["转发抽奖"], summary: {}, candidates: [], resume_available: false };
  let starts = 0;
  let stops = 0;
  await page.route("**/api/discovery/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/start")) {
      starts += 1;
      expect(route.request().postDataJSON()).toEqual({ keywords: ["转发抽奖"], mode: "comprehensive_page", target_new_videos: 100 });
      snapshot = { ...snapshot, mode: "comprehensive_page", job: { job_id: starts, state: "running", cancel_requested: false,
        progress: { message: "扫描视频 2/36" } }, summary: { videos_checked: 2, requests_used: 5, verification_remaining: 8 } };
    } else if (path.endsWith("/stop")) {
      stops += 1;
      expect(route.request().postDataJSON()).toEqual({ job_id: starts });
      snapshot = { ...snapshot, job: { ...snapshot.job, state: "cancelled" },
        summary: { ...snapshot.summary, completion_reason: "cancelled", eligible: 1 }, resume_available: true,
        candidates: [] };
    }
    await route.fulfill({ json: snapshot });
  });
  await gotoOverview(page);
  await expect(page.locator("#discovery-panel")).toBeHidden();
  await page.locator('[data-section="sources"]').click();
  await expect(page.locator("#discovery-stop")).toBeDisabled();
  await page.getByLabel("视频排序").selectOption("comprehensive_page");
  await page.locator("#discovery-start").click();
  await expect(page.locator("#discovery-start")).toBeDisabled();
  await expect(page.locator("#discovery-stop")).toBeEnabled();
  await expect(page.locator("#discovery-keywords")).toBeDisabled();
  await expect(page.getByLabel("视频排序")).toBeDisabled();
  await expect(page.locator("#discovery-videos")).toHaveText("2");
  await page.locator("#discovery-stop").click();
  await expect(page.locator("#discovery-start")).toHaveText("继续检索");
  await expect(page.locator("#discovery-stop")).toBeDisabled();
  await expect(page.locator("#discovery-badge")).toHaveText("已停止");
  await expect(page.locator("#discovery-panel a[href*='/opus/']")).toHaveCount(0);
  await page.reload();
  await page.locator('[data-section="sources"]').click();
  await expect(page.locator("#discovery-start")).toHaveText("继续检索");
  await expect(page.getByLabel("视频排序")).toHaveValue("comprehensive_page");
  await page.getByLabel("视频排序").selectOption("standard");
  await expect(page.locator("#discovery-start")).toHaveText("开始检索");
  await page.getByLabel("视频排序").selectOption("comprehensive_page");
  await page.locator("#discovery-start").click();
  await expect(page.locator("#discovery-stop")).toBeEnabled();
  expect(starts).toBe(2);
  expect(stops).toBe(1);
});

test("request budget shows a pause, and stopping remains available after reload", async ({ page, request }) => {
  await setE2EState(request, { account: "logged_in", llm: "ready" });
  let snapshot: any = { job: { job_id: 44, state: "success", cancel_requested: false }, keywords: ["转发抽奖"],
    summary: { completion_reason: "request_budget", videos_checked: 36, verification_remaining: 133, requests_used: 180, eligible: 4 },
    candidates: [], resume_available: true };
  await page.route("**/api/discovery/current", (route) => route.fulfill({ json: snapshot }));
  await gotoOverview(page);
  await page.locator('[data-section="sources"]').click();
  await expect(page.locator("#discovery-badge")).toHaveText("本轮额度已用完");
  await expect(page.locator("#discovery-remaining")).toHaveText("133");
  await expect(page.locator("#discovery-videos-label")).toHaveText("上次扫描视频");
  await expect(page.locator("#discovery-start")).toHaveText("继续检索");
  snapshot = { ...snapshot, job: { job_id: 45, state: "running", cancel_requested: false }, summary: {} };
  await page.reload();
  await page.locator('[data-section="sources"]').click();
  await expect(page.locator("#discovery-stop")).toBeEnabled();
  snapshot = { ...snapshot, job: { ...snapshot.job, cancel_requested: true } };
  await page.reload();
  await page.locator('[data-section="sources"]').click();
  await expect(page.locator("#discovery-stop")).toHaveText("正在停止…");
  await expect(page.locator("#discovery-stop")).toBeDisabled();
  await expect(page.locator("#discovery-start")).toBeDisabled();
});
