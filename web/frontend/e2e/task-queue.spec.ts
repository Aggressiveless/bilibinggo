import { expect, test } from "./offline-test";
import { confirmModal, gotoOverview, setE2EState } from "./helpers";

test("page actions enqueue while busy and logs own queue controls and history", async ({ page, request }) => {
  await setE2EState(request, { account: "logged_in", llm: "ready" });
  const active = { id: 900, action: "refresh_all", label: "更新信息源", state: "running", message: "模拟更新中", log: "模拟读取信息源" };
  const history = { id: 899, action: "participate_sequential", label: "模拟参与结果", state: "success", message: "模拟完成", log: "模拟结果：成功 1，失败 0" };
  const failed = { id: 898, action: "refresh_source", label: "模拟更新失败", state: "error", message: "已保存进度" };
  const entries: any[] = [{ ...active, id: "active", job_id: active.id },
    { ...failed, id: "failed", job_id: failed.id, can_retry: true }];
  const submitted: string[] = [];
  const snapshot = () => ({ entries, current_job: active, jobs: [active, history, failed], pause_reason: "" });
  await page.route("**/api/summary", async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), job: active } });
  });
  await page.route("**/api/events", route => route.fulfill({
    contentType: "text/event-stream",
    body: `event: job.snapshot\ndata: ${JSON.stringify(active)}\n\n`,
  }));
  // All starts and mutations are intercepted. No participation worker is invoked.
  await page.route("**/api/jobs**", async route => {
    if (route.request().method() !== "POST") {
      await route.fulfill({ json: active });
      return;
    }
    expect(new URL(route.request().url()).searchParams.get("enqueue")).toBe("true");
    const { action } = route.request().postDataJSON();
    submitted.push(action);
    const entry = { id: action, label: action === "participate_sequential" ? "参与未参加活动" : "更新信息源（等待）", state: "queued", job_id: null };
    entries.push(entry);
    await route.fulfill({ json: { ok: true, job: active, queue_entry: entry } });
  });
  await page.route("**/api/discovery/*", async route => {
    let queue_entry;
    if (new URL(route.request().url()).pathname.endsWith("/start")) {
      expect(new URL(route.request().url()).searchParams.get("enqueue")).toBe("true");
      submitted.push("control_discover");
      queue_entry = { id: "search", label: "检索新视频", state: "queued", job_id: null };
      entries.push(queue_entry);
    }
    await route.fulfill({ json: { job: null, keywords: ["转发抽奖"], summary: {}, candidates: [], resume_available: false, queue_entry } });
  });
  await page.route("**/api/task-queue**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/logs/899")) {
      await route.fulfill({ json: history });
      return;
    }
    if (path.endsWith("/failed/retry")) {
      expect(route.request().method()).toBe("POST");
      expect(route.request().headers()["x-binggo-dashboard"]).toBe("1");
      entries.find(entry => entry.id === "failed").can_retry = false;
      entries.push({ id: "retry", label: failed.label, state: "queued", job_id: null });
    }
    if (route.request().method() === "PATCH") {
      const id = path.split("/").pop();
      const index = entries.findIndex(entry => entry.id === id);
      const { operation } = route.request().postDataJSON();
      expect(entries[index].state).toBe("queued");
      if (operation === "remove") entries[index].state = "cancelled";
      else {
        const other = index + (operation === "up" ? -1 : 1);
        [entries[index], entries[other]] = [entries[other], entries[index]];
      }
    }
    await route.fulfill({ json: snapshot() });
  });
  const pageErrors: string[] = [];
  page.on("pageerror", error => pageErrors.push(String(error)));
  await gotoOverview(page);
  await page.locator('[data-section="sources"]').click();
  await page.getByRole("button", { name: "监控与合集", exact: true }).click();
  await page.locator('#section-sources [data-action="refresh_all"]').click();
  await confirmModal(page);
  await expect(page.locator("#task-notice-message")).toContainText("已排队");
  await expect(page.locator("#section-sources")).toHaveClass(/active/);
  await page.getByRole("button", { name: "主动检索", exact: true }).click();
  await page.locator("#discovery-start").click();
  await expect.poll(() => submitted.length).toBe(2);
  await page.locator('[data-section="activities"]').click();
  await expect(page.locator('[data-action="participate_sequential"]')).toBeEnabled();
  await page.locator('[data-action="participate_sequential"]').click();
  await expect.poll(() => submitted).toEqual(["refresh_all", "control_discover", "participate_sequential"]);
  await expect(page.locator("#section-activities")).toHaveClass(/active/);
  await expect(page.locator("#log-dock, #auto-dock, #auto-panel")).toHaveCount(0);
  await expect(page.locator("#section-logs #progress-banner")).toHaveCount(1);
  await expect(page.locator("#section-logs #job-result-banner")).toHaveCount(1);
  await page.locator('[data-section="logs"]').click();
  await expect(page.locator("#progress-banner")).toBeVisible();
  await expect(page.locator("#job-log")).toHaveText(active.log);
  const rows = page.locator("#task-queue-list > li");
  await expect(rows).toHaveCount(4);
  await expect(rows.first().getByRole("button", { name: /上移|下移|移出/ })).toHaveCount(0);
  await page.getByRole("button", { name: "上移参与未参加活动", exact: true }).click();
  await expect(rows.nth(2)).toContainText("参与未参加活动");
  await page.getByRole("button", { name: "下移参与未参加活动", exact: true }).click();
  await expect(rows.nth(3)).toContainText("参与未参加活动");
  await page.getByRole("button", { name: "移出检索新视频", exact: true }).click();
  await expect(rows).toHaveCount(3);
  await expect(page.locator("#task-history-list")).toContainText("检索新视频");
  await page.getByRole("button", { name: "重试模拟更新失败", exact: true }).click();
  await expect(rows.last()).toContainText("模拟更新失败");
  await expect(page.getByRole("button", { name: "重试模拟更新失败", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "查看任务 899 日志", exact: true }).click();
  await expect(page.locator("#task-history-log")).toHaveText(history.log);
  expect(pageErrors).toEqual([]);
});
