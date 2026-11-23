import { readFile } from "node:fs/promises";
import { expect, test } from "./offline-test";
import { confirmModal, gotoOverview, openActivities } from "./helpers";

test("recipient can preview, confirm and export a shared activity", async ({ page }) => {
  const timestamp = Math.floor(Date.now() / 1000);
  const activity = (id: string, lotteryTime: number, prize: string) => ({
    dynamic_id: id,
    source_url: `https://www.bilibili.com/opus/${id}`,
    lottery_type: "互动抽奖",
    lottery_time: lotteryTime,
    prize,
  });
  const future = activity("1234567890123456789", timestamp + 86400, "活动分享测试奖品");
  const expired = activity("1234567890123456790", timestamp - 1, "已过期奖品");
  const format = "binggo.activities.v1";
  const contents = Buffer.from(JSON.stringify({ format, activities: [future, future, expired] }));

  await gotoOverview(page);
  await openActivities(page);
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: "导入活动" }).click();
  await (await chooser).setFiles({ name: "binggo-activities.json", mimeType: "application/json", buffer: contents });
  await expect(page.locator("#app-confirm-title")).toHaveText("导入 1 条活动？");
  await expect(page.locator("#app-confirm-desc")).toContainText("重复 1 条，已过开奖时间 1 条");
  await expect(page.locator("#activities-body")).not.toContainText(future.prize);
  await confirmModal(page);
  await expect(page.locator("#activities-body")).toContainText(future.prize);

  await page.locator("#filter-q").fill(future.prize);
  await expect(page.locator("#activities-body tr")).toHaveCount(1);
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出活动" }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toMatch(/^binggo-activities-.*\.json$/);
  expect(JSON.parse(await readFile(await download.path(), "utf-8"))).toEqual({ format, activities: [future] });
});
