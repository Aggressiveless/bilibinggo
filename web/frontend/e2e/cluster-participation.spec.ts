import { expect, test } from "./offline-test";
import { gotoOverview, setE2EState } from "./helpers";

test("cluster shows per-node counts, unknown reports, and the current working state", async ({ page, request }) => {
  await setE2EState(request, { account: "logged_in", llm: "ready" });
  const snapshot = {
    role: "host", node: "host", connected: true, worker_ready: true,
    status: {
      round: 12, target: 1000, phase: "collecting", counts: {},
      work_assignments: { watch_nodes: ["host"], collection_nodes: ["worker", "old"] },
      watch_repeating: true,
      nodes: [
        { id: "host", name: "主节点", host: true, online: true, work: 2, completed: 0, model_calls: 0,
          participation_pending: 0, participation_enabled: true },
        { id: "worker", name: "工作节点", online: true, work: 2, completed: 0, model_calls: 0,
          local_work: "participating", participation_pending: 7, participation_enabled: true },
        { id: "old", name: "旧版节点", online: true, work: 2, completed: 0, model_calls: 0 },
      ],
    },
  };
  await page.route("**/api/cluster/status", route => route.fulfill({ json: snapshot }));
  await page.route("**/api/cluster/watch-stop", route => {
    expect(route.request().method()).toBe("POST");
    snapshot.status.watch_repeating = false;
    return route.fulfill({ json: snapshot });
  });
  await gotoOverview(page);
  await page.locator('[data-section="cluster"]').click();
  const node = (name: string) => page.locator("#cluster-nodes > li").filter({
    has: page.locator("strong").filter({ hasText: new RegExp(`^${name}$`) }),
  });
  await expect(node("工作节点")).toContainText("参与活动中");
  await expect(node("工作节点").locator("dl > div").filter({ hasText: "待参与活动" })).toContainText("7");
  await expect(node("主节点").locator("dl > div").filter({ hasText: "待参与活动" })).toContainText("0");
  await expect(node("旧版节点")).toContainText("等待节点上报");
  await page.getByRole("button", { name: "停止追踪轮转" }).click();
  await expect(page.getByRole("button", { name: "开始追踪" })).toBeVisible();
});
