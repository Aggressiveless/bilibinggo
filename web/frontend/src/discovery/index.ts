import { fetchJSON } from "../api/client";
import { requireSetup } from "../account/index";
import { loadActivities, loadSummary } from "../activities/index";
import { notifyJobStartError, startPolling } from "../jobs/index";
import { showToast } from "../shell/toast";

interface DiscoveryJob {
  job_id: number;
  action?: string;
  state: string;
  cancel_requested: boolean;
  progress?: { message?: string };
  result?: { message?: string };
}

interface FilterSettings {
  coins: string;
  min_followers: number;
  min_account_days: number;
  include_unknown: boolean;
}

interface CoinBalance {
  balance: number | null;
  checked_at: number | null;
  estimated: boolean;
  refresh_after_coin: boolean;
}

interface DiscoveryStatus {
  job: DiscoveryJob | null;
  queue_job?: DiscoveryJob | null;
  queue?: { total: number; imported: number; existing: number; expired: number; skipped: number; remaining: number; message: string };
  keywords: string[];
  mode?: "standard" | "comprehensive_page";
  summary: {
    videos_checked?: number;
    verification_remaining?: number;
    eligible?: number;
    requests_used?: number;
    completion_reason?: string;
    phase?: string;
  };
  bv_count?: number;
  pending_selection?: number;
  filter?: { id?: string; settings?: FilterSettings; matched?: number; total?: number; unknown_metadata?: number };
  resume_available: boolean;
  stop_message?: string;
}

const phases: Record<string, string> = {
  search: "搜索视频", scan_videos: "扫描视频", resolve_sources: "解析活动入口", verify_candidates: "核验候选活动",
  import: "正在导入活动",
};

function activeJob(status: DiscoveryStatus) {
  return status.queue_job?.state === "running" ? status.queue_job : status.job;
}

function element<T extends HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}

function setText(id: string, text: string | number) {
  const target = element(id);
  if (target) target.textContent = String(text);
}

export function renderDiscovery(status: DiscoveryStatus, submitting = false) {
  const job = activeJob(status);
  const running = job?.state === "running";
  const stopping = running && job.cancel_requested;
  const importing = running && job === status.queue_job && job.action === "control_import";
  const filtering = running && job === status.queue_job && job.action === "control_discovery_filter";
  const start = element<HTMLButtonElement>("discovery-start");
  const stop = element<HTMLButtonElement>("discovery-stop");
  const input = element<HTMLInputElement>("discovery-keywords");
  if (start) {
    start.disabled = running || submitting;
    start.textContent = filtering ? "筛选中…" : importing ? "导入中…" : running ? "检索中…" : status.resume_available ? "继续检索" : "开始检索";
  }
  if (stop) {
    stop.disabled = !running || stopping || submitting;
    stop.textContent = stopping ? "正在停止…" : filtering ? "停止筛选" : importing ? "停止导入" : "停止检索";
  }
  if (input) input.disabled = running || submitting;
  const summary = status.summary;
  setText("discovery-videos", summary.videos_checked || 0);
  setText("discovery-remaining", summary.verification_remaining || 0);
  setText("discovery-eligible", summary.eligible || 0);
  setText("discovery-requests", summary.requests_used || 0);
  setText("discovery-bv-count", status.bv_count || 0);
  let badge = "待开始";
  let message = "输入关键词后开始检索。";
  if (running) {
    badge = stopping ? "正在停止" : filtering ? "正在筛选活动" : importing ? "正在导入活动" : phases[summary.phase || ""] || "检索中";
    message = stopping ? "正在停止任务，等待当前请求返回并保存进度…" : job.progress?.message || "正在准备任务…";
  } else if (job) {
    const reason = summary.completion_reason;
    if (reason === "request_budget") {
      badge = "本轮额度已用完";
      message = "本轮请求额度已用完，检索暂停。";
    } else if (job.state === "cancelled" || reason === "cancelled") {
      badge = "已停止";
      message = "已停止检索。";
    } else if (reason === "platform_paused") {
      badge = "平台限制，已暂停";
      message = status.stop_message || "平台暂时限制请求，请稍后继续。";
    } else if (job.state === "error" || reason === "error") {
      badge = "检索中断";
      message = job.progress?.message || "检索失败，请稍后重试。";
    } else if (job.state === "interrupted") {
      badge = "检索中断";
      message = "上次检索因程序重启中断。";
    } else {
      badge = status.resume_available ? "本轮已结束" : "检索完成";
      message = `已找到 ${summary.eligible || 0} 条可参与活动。${status.pending_selection ? "可直接加入，或先筛选。" : ""}`;
    }
    if (status.resume_available) message += "进度已保存，使用相同关键词和排序可继续。";
  }
  setText("discovery-badge", badge);
  setText("discovery-status", message);
  const queue = status.queue;
  const queuePanel = element("discovery-queue");
  const importButton = element<HTMLButtonElement>("discovery-import");
  if (queuePanel) queuePanel.hidden = !queue?.total;
  if (importButton) {
    importButton.hidden = !status.pending_selection || !!running;
    importButton.disabled = submitting;
    importButton.textContent = `不筛选，加入全部 ${status.pending_selection || 0} 条`;
  }
  if (queue?.total) {
    const skipped = queue.existing + queue.expired + queue.skipped;
    setText("discovery-queue-status", `已入库 ${queue.imported} 条 · 跳过 ${skipped} 条 · 可选择 ${status.pending_selection || 0} 条${queue.message ? `。${queue.message}` : ""}`);
  }
}

export function bindDiscovery() {
  const form = element<HTMLFormElement>("discovery-form");
  const input = element<HTMLInputElement>("discovery-keywords");
  const mode = element<HTMLSelectElement>("discovery-mode");
  const stop = element<HTMLButtonElement>("discovery-stop");
  const importButton = element<HTMLButtonElement>("discovery-import");
  const filterPanel = element<HTMLDetailsElement>("discovery-filters");
  const filterForm = element<HTMLFormElement>("discovery-filter-form");
  const filterFields = element<HTMLFieldSetElement>("discovery-filter-fields");
  const previewButton = element<HTMLButtonElement>("discovery-preview");
  const filteredImport = element<HTMLButtonElement>("discovery-import-filtered");
  if (!form || !input || !mode || !stop || !importButton || !filterPanel || !filterForm || !filterFields || !previewButton || !filteredImport) return;
  let snapshot: DiscoveryStatus = { job: null, keywords: ["转发抽奖"], summary: {}, resume_available: false };
  let submitting = false;
  let reading = false;
  let revision = 0;
  let initialized = false;
  let shownImportCount = 0;
  let shownFilterId: string | undefined;

  const keywords = () => [...new Set(input.value.split(/[,，\n]/).map((word) => word.trim()).filter(Boolean))];
  const filterSettings = (): FilterSettings => ({
    coins: (filterForm.elements.namedItem("coins") as HTMLSelectElement).value,
    min_followers: Number((filterForm.elements.namedItem("min_followers") as HTMLInputElement).value),
    min_account_days: Number((filterForm.elements.namedItem("min_account_days") as HTMLInputElement).value),
    include_unknown: (filterForm.elements.namedItem("include_unknown") as HTMLInputElement).checked,
  });
  const matchesPreview = () => {
    const saved = snapshot.filter?.settings;
    const current = filterSettings();
    return !!saved && (Object.keys(current) as (keyof FilterSettings)[]).every(key => current[key] === saved[key]);
  };
  const render = () => {
    const sameSearch = mode.value === (snapshot.mode || "standard") &&
      JSON.stringify(keywords()) === JSON.stringify(snapshot.keywords);
    renderDiscovery(snapshot, submitting);
    const start = element<HTMLButtonElement>("discovery-start");
    if (start && !start.disabled && !sameSearch) start.textContent = "开始检索";
    mode.disabled = input.disabled;
    filterPanel.hidden = !snapshot.pending_selection;
    filterFields.disabled = input.disabled;
    previewButton.disabled = input.disabled;
    filteredImport.disabled = input.disabled || !matchesPreview() || !snapshot.filter?.matched;
    const issue = snapshot.queue_job?.action === "control_discovery_filter" ? snapshot.queue_job.result?.message : undefined;
    setText("discovery-filter-result", matchesPreview()
      ? `匹配 ${snapshot.filter?.matched || 0} / ${snapshot.filter?.total || 0} 条 · 发布者资料未知 ${snapshot.filter?.unknown_metadata || 0} 条`
      : issue || "调整条件后预览匹配数量，再确认加入。");
    setText("discovery-help", mode.value === "comprehensive_page"
      ? "每轮搜索 1 个关键词，检查综合排序第一页，最多 36 个视频。每轮最多 180 次请求。"
      : "最多 3 个关键词，用逗号分隔。每轮最多 180 次请求，未完成时可继续上次进度。");
  };
  const apply = (value: DiscoveryStatus) => {
    const imported = value.queue?.imported || 0;
    if (imported && (value.job?.job_id !== snapshot.job?.job_id || imported > shownImportCount)) {
      loadActivities().catch(() => {});
    }
    shownImportCount = imported;
    const filterId = value.filter?.id;
    if (filterId && filterId !== shownFilterId && value.filter?.settings) {
      for (const [key, val] of Object.entries(value.filter.settings)) {
        const field = filterForm.elements.namedItem(key) as HTMLInputElement | HTMLSelectElement;
        if (field instanceof HTMLInputElement && field.type === "checkbox") field.checked = Boolean(val);
        else field.value = String(val);
      }
    }
    shownFilterId = filterId;
    snapshot = value;
    if (!initialized || value.job?.state === "running") {
      input.value = value.keywords.join("，");
      mode.value = value.mode || "standard";
    }
    initialized = true;
    render();
  };
  const refresh = async () => {
    if (submitting || reading) return;
    reading = true;
    const expected = revision;
    try {
      const value = await fetchJSON<DiscoveryStatus>("/api/discovery/current");
      if (expected === revision) apply(value);
    } catch {
      // Keep the last visible state and retry without starting another job.
      if (!initialized) setText("discovery-status", "检索状态暂时无法读取，正在重试…");
    } finally {
      reading = false;
    }
  };
  const submit = async (path: string, body: object) => {
    submitting = true;
    revision += 1;
    render();
    try {
      apply(await fetchJSON<DiscoveryStatus>(path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      }));
      loadSummary().then((job) => { if (job?.state === "running") startPolling(); }).catch(() => {});
    } catch (error) {
      notifyJobStartError(error, "control_discover");
    } finally {
      submitting = false;
      render();
      refresh();
    }
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitting || activeJob(snapshot)?.state === "running" || !requireSetup("control_discover")) return;
    const words = keywords();
    if (!words.length || words.length > 3 || words.some((word) => word.length > 80)) {
      showToast("请输入 1–3 个关键词，每个不超过 80 字", "info");
      input.focus();
      return;
    }
    if (mode.value === "comprehensive_page" && words.length !== 1) {
      showToast("综合排序每轮检索一个关键词", "info");
      input.focus();
      return;
    }
    submit("/api/discovery/start", { keywords: words, mode: mode.value });
  });
  input.addEventListener("input", render);
  mode.addEventListener("change", render);
  stop.addEventListener("click", () => {
    const job = activeJob(snapshot);
    if (submitting || job?.state !== "running" || job.cancel_requested) return;
    submit("/api/discovery/stop", { job_id: job.job_id });
  });
  importButton.addEventListener("click", () => {
    if (submitting || activeJob(snapshot)?.state === "running" || !snapshot.job || !snapshot.pending_selection || !requireSetup("control_discover")) return;
    submit("/api/discovery/import", { job_id: snapshot.job.job_id, selection: "all" });
  });
  filterForm.addEventListener("input", render);
  filterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitting || activeJob(snapshot)?.state === "running" || !snapshot.job || !snapshot.pending_selection) return;
    submit("/api/discovery/filter", { job_id: snapshot.job.job_id, settings: filterSettings() });
  });
  filteredImport.addEventListener("click", () => {
    if (filteredImport.disabled || !snapshot.job || !requireSetup("control_discover")) return;
    submit("/api/discovery/import", { job_id: snapshot.job.job_id, selection: "filtered", filter_id: snapshot.filter?.id });
  });
  bindCoinBalance();
  refresh();
  window.setInterval(refresh, 3000);
  window.addEventListener("pageshow", () => refresh());
}

function bindCoinBalance() {
  const toggle = element<HTMLInputElement>("discovery-coins-live");
  if (!toggle) return;
  let busy = false;
  let live = false;
  const update = async (setting?: boolean) => {
    if (busy) return;
    busy = true;
    toggle.disabled = true;
    try {
      const balance = await fetchJSON<CoinBalance>("/api/discovery/coins", setting === undefined ? {} : {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_after_coin: setting }),
      });
      live = balance.refresh_after_coin;
      setText("discovery-coins", balance.balance === null ? "—" : `${balance.estimated ? "约 " : ""}${balance.balance}`);
      const time = balance.checked_at ? new Date(balance.checked_at * 1000).toLocaleString() : "尚未取得余额";
      setText("discovery-coin-note", `${time} · 默认每 12 小时联网校准`);
    } catch {
      if (setting !== undefined) showToast("暂时无法保存硬币刷新设置", "error");
    } finally {
      busy = false;
      toggle.checked = live;
      toggle.disabled = false;
    }
  };
  toggle.addEventListener("change", () => update(toggle.checked));
  window.addEventListener("binggo:coin-balance-check", () => update());
  update();
  window.setInterval(() => update(), 60_000);
}
