import { fetchJSON } from "../api/client";
import { openAppConfirm } from "../shell/confirm";
import { showToast } from "../shell/toast";
import { buildActivityFilterQueryParams, loadActivities, loadSummary } from "./index";

interface ImportPreview {
  new: number;
  duplicates: number;
  expired: number;
  imported: number;
  examples: string[];
}

function downloadShareFile(bundle: object) {
  const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const date = new Date().toISOString().slice(0, 10);
  link.href = url;
  link.download = `binggo-activities-${date}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function uploadShareFile(file: File, preview: boolean) {
  return fetchJSON<ImportPreview>(`/api/activities/import?preview=${preview}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: file,
  });
}

export function bindActivitySharing() {
  const exportButton = document.querySelector<HTMLButtonElement>("#activity-share-export")!;
  const importButton = document.querySelector<HTMLButtonElement>("#activity-share-import")!;
  const fileInput = document.querySelector<HTMLInputElement>("#activity-share-file")!;

  exportButton.addEventListener("click", async () => {
    exportButton.disabled = true;
    try {
      const filters = buildActivityFilterQueryParams();
      const bundle = await fetchJSON<{ activities: object[] }>(`/api/activities/export?${filters}`);
      downloadShareFile(bundle);
      showToast(`已导出 ${bundle.activities.length} 条活动`, "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "导出失败", "error");
    } finally {
      exportButton.disabled = false;
    }
  });

  importButton.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    fileInput.value = "";
    if (!file) return;

    importButton.disabled = true;
    try {
      if (file.size > 4 * 1024 * 1024) throw new Error("活动分享文件不能超过 4 MB");
      const preview = await uploadShareFile(file, true);
      const skips = `重复 ${preview.duplicates} 条，已过开奖时间 ${preview.expired} 条。`;
      if (!preview.new) {
        showToast("没有可新增的活动", "info", skips);
        return;
      }
      if (!await openAppConfirm({
        title: `导入 ${preview.new} 条活动？`,
        desc: `${skips} 请以原动态公布的规则和时间为准。`,
        bullets: preview.examples,
        confirmLabel: "确认导入",
      })) return;

      const result = await uploadShareFile(file, false);
      showToast(`已导入 ${result.imported} 条活动`, "success", `重复 ${result.duplicates} 条，已过开奖时间 ${result.expired} 条。`);
      await Promise.allSettled([loadActivities(), loadSummary()]);
    } catch (error) {
      showToast(error instanceof Error ? error.message : "导入失败", "error");
    } finally {
      importButton.disabled = false;
    }
  });
}
