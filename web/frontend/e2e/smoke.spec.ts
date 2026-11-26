import { expect, test } from "./offline-test";
import {
  confirmModal,
  gotoOverview,
  openActivities,
  setE2EState,
  waitForSseConnect,
} from "./helpers";

test.describe("Binggo smoke @smoke", () => {
  test.describe.configure({ mode: "serial" });

  test("1 opens console with hashed assets", async ({ page }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (err) => pageErrors.push(String(err)));

    const assetOk = page.waitForResponse(
      (res) => res.url().includes("/assets/") && res.status() === 200,
      { timeout: 20_000 },
    );
    const sseOk = waitForSseConnect(page);
    await page.goto("/");
    await assetOk;
    await sseOk;
    await expect(page).toHaveTitle(/Binggo/);
    await expect(page.getByRole("heading", { name: "概览" })).toBeVisible({ timeout: 15_000 });
    await expect(page.locator("#stats-grid")).toBeVisible();
    expect(pageErrors, `uncaught page errors: ${pageErrors.join("; ")}`).toEqual([]);
  });

  test("2 activities list and filter", async ({ page }) => {
    await gotoOverview(page);
    await openActivities(page);
    await expect(page.locator("#activities-body tr").first()).toBeVisible({ timeout: 15_000 });
    await page.locator("[data-filter-status='未参加']").click();
    await expect(page.locator("#pagination")).toBeVisible();
    await expect(page.locator("#activities-body")).toBeVisible();
  });

  test("3a AUTH gate blocks refresh_status before request", async ({ page, request }) => {
    // 前端 requireSetup 在未登录时直接 toast，不发 POST（与现网行为一致）
    await setE2EState(request, { account: "logged_out", llm: "not_ready" });
    await gotoOverview(page);

    let sawJobsPost = false;
    page.on("request", (req) => {
      if (req.method() === "POST" && req.url().includes("/api/jobs") && !req.url().includes("cancel")) {
        sawJobsPost = true;
      }
    });

    await page.locator('[data-action="refresh_status"]').first().click();
    await expect(page.locator(".toast-message").filter({ hasText: /登录/ })).toBeVisible({
      timeout: 8_000,
    });
    expect(sawJobsPost).toBe(false);
  });

  test("3b LLM_NOT_READY on refresh_all via API", async ({ page, request }) => {
    // settings 文件为 ready，前端放行；后端 hook llm=not_ready → 契约码
    await setE2EState(request, { account: "logged_in", llm: "not_ready" });
    await gotoOverview(page);

    const jobRespPromise = page.waitForResponse(
      (res) =>
        res.url().includes("/api/jobs") &&
        res.request().method() === "POST" &&
        !res.url().includes("cancel"),
      { timeout: 20_000 },
    );
    await page.locator('[data-action="refresh_all"]').first().click();
    await confirmModal(page);
    const jobResp = await jobRespPromise;
    expect(jobResp.status()).toBe(401);
    const body = await jobResp.json();
    expect(body.error?.code).toBe("LLM_NOT_READY");
    await expect(
      page.locator("#task-notice-message").filter({ hasText: /LLM 配置.*连接测试/ }),
    ).toBeVisible({ timeout: 8_000 });
  });

  test("4 job start success for refresh_status", async ({ page, request }) => {
    await setE2EState(request, { account: "logged_in", llm: "ready" });
    await gotoOverview(page);

    const jobRespPromise = page.waitForResponse(
      (res) =>
        res.url().includes("/api/jobs") &&
        res.request().method() === "POST" &&
        !res.url().includes("cancel"),
      { timeout: 15_000 },
    );
    await page.locator('[data-action="refresh_status"]').first().click();
    const jobResp = await jobRespPromise;
    expect(jobResp.status()).toBe(200);
    const body = await jobResp.json();
    expect(body.ok).toBeTruthy();
    expect(body.job?.id).toBeTruthy();

    await expect
      .poll(
        async () => {
          const current = await request.get("/api/jobs/current");
          expect(current.ok()).toBeTruthy();
          const job = await current.json();
          return job.state;
        },
        { timeout: 15_000, intervals: [100, 200, 500] },
      )
      .toBe("success");
  });

  test("5 settings panels load", async ({ page, request }) => {
    await setE2EState(request, { account: "logged_in", llm: "ready" });
    await gotoOverview(page);
    await expect(page.locator("#llm-settings-panel")).toBeVisible();
    await expect(page.locator("#participate-text-input")).toBeVisible();
    await expect(page.locator("#save-llm-settings")).toBeVisible();
    await expect(page.locator("#save-participate-text")).toBeVisible();

    const settingsResp = page.waitForResponse(
      (res) => res.url().includes("/api/settings/llm") && res.request().method() === "GET",
      { timeout: 10_000 },
    );
    await page.locator("#refresh-llm-settings").click();
    const resp = await settingsResp;
    expect(resp.status()).toBe(200);
  });

  test("6 manages LLM profiles from the overview", async ({ page, request }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await setE2EState(request, { account: "logged_in", llm: "ready" });
    await gotoOverview(page);
    const rows = page.locator("#llm-profile-list > li");
    await expect(rows).toHaveCount(1);

    await page.getByRole("button", { name: "新增方案" }).click();
    await page.getByLabel("方案名称").fill("备用接口");
    await page.locator("#llm-api-key-input").fill("offline-backup-api-key");
    await page.getByLabel("接口地址").fill("https://backup.example/v1");
    await page.getByLabel("模型名称").fill("backup-model");
    await page.getByRole("button", { name: "保存配置" }).click();

    await expect(rows).toHaveCount(2);
    await expect(rows.nth(1)).toContainText("备用接口");
    await expect(rows.nth(1)).not.toContainText("offline-backup-api-key");
    await expect(page.locator("#llm-api-key-input")).toHaveValue("");
    await page.getByRole("button", { name: "上移 备用接口" }).click();
    await expect(rows.first()).toContainText("备用接口");
    await expect(rows.first()).toContainText("当前优先使用");

    await page.reload();
    await expect(rows.first()).toContainText("备用接口");
    await expect(page.getByLabel("方案名称")).toHaveValue("备用接口");
    const panel = page.locator("#llm-settings-panel");
    await page.setViewportSize({ width: 390, height: 850 });
    await expect.poll(() => panel.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.getByRole("button", { name: "删除方案" }).click();
    await confirmModal(page);
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("原有配置");
  });
});
