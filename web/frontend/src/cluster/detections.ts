import { fetchJSON } from "../api/client";

type DetectedActivity = {
  dynamic_id: string;
  lottery_type: string;
  lottery_time: number;
  deadline: number | null;
  prize: string;
};
type DetectionPage = { page: number; pages: number; total: number; items: DetectedActivity[] };
type CatalogStatus = { node?: string; total?: number; sequence?: number };

const date = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});

function activityRow(activity: DetectedActivity) {
  const row = document.createElement("li");
  const title = document.createElement("strong");
  title.textContent = activity.prize || "奖品信息待补充";
  const meta = document.createElement("span");
  meta.className = "caption";
  const draw = "开奖 " + date.format(activity.lottery_time * 1000);
  const deadline = activity.deadline ? "截止 " + date.format(activity.deadline * 1000) : "";
  meta.textContent = [activity.lottery_type, deadline, draw].filter(Boolean).join("　");
  row.append(title, meta);
  if (/^[0-9]{18,19}$/.test(activity.dynamic_id)) {
    const link = document.createElement("a");
    link.href = "https://www.bilibili.com/opus/" + activity.dynamic_id;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "打开原动态";
    row.append(link);
  }
  return row;
}

export function mountDetectedActivities(byId: <T extends HTMLElement>(name: string) => T) {
  const results = byId("detected-results");
  const panel = byId<HTMLDetailsElement>("detected");
  const summary = panel.querySelector("summary");
  const sourceSection = results.closest(".view-section");
  const discoveryView = document.querySelector<HTMLButtonElement>('[data-source-view="discovery"]');
  const list = byId("detected-rows");
  const message = byId("detected-status");
  const pager = byId("detected-pager");
  const prev = byId<HTMLButtonElement>("detected-prev");
  const next = byId<HTMLButtonElement>("detected-next");
  let state: CatalogStatus = {};
  let page = 1;
  let generation = 0;
  let pending = false;
  let lastRequest = 0;
  let lastVersion = "";
  let signature = "";
  let jumpToResults = false;

  function revealResults() {
    if (!jumpToResults || results.hidden || !sourceSection?.classList.contains("active")) return;
    jumpToResults = false;
    results.scrollIntoView({ block: "start" });
    summary?.focus({ preventScroll: true });
  }

  async function refresh(requestedPage = page, force = false) {
    if (results.hidden || !panel.open || pending) return;
    if (!force && (!sourceSection?.classList.contains("active") || panel.closest("#discovery-panel")?.hasAttribute("hidden"))) return;
    const version = String(state.sequence) + "/" + String(state.total);
    if (!force && version === lastVersion && Date.now() - lastRequest < 20000) return;
    const request = ++generation;
    pending = true;
    lastRequest = Date.now();
    lastVersion = version;
    if (!list.childElementCount) message.textContent = "正在读取集群检出活动…";
    try {
      const data = await fetchJSON<DetectionPage>("/api/cluster/detected-activities?page=" + requestedPage, {
        headers: { "X-Binggo-Dashboard": "1" }, timeoutMs: 10000,
      });
      if (request !== generation) return;
      page = data.page;
      const content = JSON.stringify(data.items);
      if (content !== signature) {
        list.replaceChildren(...data.items.map(activityRow));
        signature = content;
      }
      message.textContent = data.total ? "" : "暂无仍开放的集群检出活动";
      pager.hidden = data.pages <= 1;
      byId("detected-page").textContent = "第 " + page + " / " + data.pages + " 页";
      prev.disabled = page <= 1;
      next.disabled = page >= data.pages;
    } catch (error) {
      if (request === generation) {
        message.textContent = list.childElementCount ? "暂时无法更新，列表保留上次读取的活动。"
          : "暂时无法读取活动，请重新展开重试。";
      }
    } finally {
      if (request === generation) pending = false;
    }
  }

  prev.addEventListener("click", () => void refresh(page - 1, true));
  next.addEventListener("click", () => void refresh(page + 1, true));
  panel.addEventListener("toggle", () => { if (panel.open) void refresh(page, true); });
  document.querySelectorAll("[data-cluster-show-detections]").forEach((button) => button.addEventListener("click", () => {
    if (discoveryView?.getAttribute("aria-pressed") === "false") discoveryView.click();
    jumpToResults = true;
    if (panel.open) void refresh(page, true);
    else panel.open = true;
    revealResults();
  }));
  if (sourceSection) {
    const observer = new MutationObserver(revealResults);
    observer.observe(sourceSection, { attributes: true, attributeFilter: ["class"] });
  }

  return (updated: CatalogStatus) => {
    if (updated.node !== state.node) {
      generation++;
      pending = false;
      lastRequest = 0;
      page = 1;
      signature = "";
      list.replaceChildren();
      pager.hidden = true;
      panel.open = false;
      message.textContent = "";
    }
    state = updated;
    results.hidden = !state.node || state.total === undefined;
    byId("detected-summary").textContent = (state.total ?? 0) + " 项仍开放";
    revealResults();
    void refresh();
  };
}
