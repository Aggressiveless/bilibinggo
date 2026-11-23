import { fetchJSON } from "../api/client";
import { requireSetup } from "../account/index";
import { loadActivities, loadSummary } from "../activities/index";
import { notifyJobStartError, startPolling } from "../jobs/index";
import { notifyTaskQueueChanged, showTaskNotice } from "../jobs/task-logs";
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

interface Candidate {
  dynamic_id: string;
  prize: string;
  publisher: string;
  publisher_uid: string | null;
  followers: number | null;
  triple_requirement: "required" | "optional" | "not_mentioned" | "unknown";
}

const videoStates = { pending: "检测未完成", source: "入口已处理", rejected: "核验不通过", ready: "待导入", handled: "已处理", video_only: "视频抽奖" };
const modelStates = { lottery: "有抽奖", entry: "抽奖入口", collection: "抽奖合集", not_lottery: "未发现抽奖", uncertain: "证据不足" };
type VideoState = keyof typeof videoStates;

interface ActivityCheck {
  dynamic_id: string;
  status: VideoState;
  reason: string;
  prize?: string;
  rules_summary?: string;
  model_reason?: string;
  model_confidence?: number | null;
  followers?: number | null;
}

interface VideoRecord {
  bvid: string;
  title: string;
  first_seen: string;
  last_seen: string;
  status: VideoState;
  repeat_count: number;
  repeat_count_incomplete: boolean;
  source_reason?: string;
  activities?: ActivityCheck[];
  reasons: { message: string; model_reason?: string; model_assessment?: keyof typeof modelStates; kind?: string; count: number;
    model_confidence?: number | null; rules_summary?: string }[];
}

interface DiscoveryStatus {
  job: DiscoveryJob | null;
  queue_entry?: { state: string; label: string; message: string };
  queue_job?: DiscoveryJob | null;
  queue?: { total: number; imported: number; existing: number; expired: number; skipped: number; remaining: number; message: string };
  keywords: string[];
  mode?: "standard" | "comprehensive_page";
  target_new_videos?: number;
  summary: {
    videos_checked?: number;
    verification_remaining?: number;
    eligible?: number;
    requests_used?: number;
    completion_reason?: string;
    phase?: string;
    target_new_videos?: number;
    new_videos_checked?: number;
    duplicates_skipped?: number;
    search_exhausted?: boolean;
  };
  bv_count?: number;
  pending_selection?: number;
  video_checks?: { total: number; counts: Partial<Record<VideoState, number>> };
  candidates?: Candidate[];
  filter?: { id?: string; settings?: FilterSettings; matched?: number; total?: number; unknown_metadata?: number };
  resume_available: boolean;
  stop_message?: string;
}

const phases: Record<string, string> = {
  search: "搜索视频", scan_videos: "扫描视频", resolve_sources: "解析活动入口", verify_candidates: "核验候选活动",
  publisher_stats: "读取发布者资料", import: "正在导入活动",
};

const tripleLabels = { required: "是", optional: "否（可选）", not_mentioned: "未提及", unknown: "未知" };

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
  const targetInput = element<HTMLInputElement>("discovery-target");
  if (start) {
    start.disabled = running || submitting;
    start.textContent = filtering ? "筛选中…" : importing ? "导入中…" : running ? "检索中…" : status.resume_available ? "继续检索" : "开始检索";
  }
  if (stop) {
    stop.disabled = !running || stopping || submitting;
    stop.textContent = stopping ? "正在停止…" : filtering ? "停止筛选" : importing ? "停止导入" : "停止检索";
  }
  if (input) input.disabled = running || submitting;
  if (targetInput) targetInput.disabled = running || submitting;
  const summary = status.summary;
  setText("discovery-videos", summary.videos_checked || 0);
  setText("discovery-duplicates", summary.duplicates_skipped || 0);
  const target = summary.target_new_videos || status.target_new_videos;
  setText("discovery-videos-label", job && !target ? "上次扫描视频" : "本次已检索新视频");
  const targetPanel = element("discovery-target-progress");
  if (targetPanel) targetPanel.hidden = !target;
  if (target) {
    const checked = summary.new_videos_checked || 0;
    setText("discovery-target-label", `新视频 ${checked} / ${target}`);
    const meter = element<HTMLProgressElement>("discovery-target-meter");
    if (meter) { meter.max = target; meter.value = checked; }
  }
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
    } else if (reason === "search_exhausted") {
      badge = "新视频不足";
      message = `搜索结果已耗尽，本次检索 ${summary.new_videos_checked || 0} / ${target} 条新视频。可更换关键词或排序后继续找新视频。`;
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
      message = target ? `已检索 ${summary.new_videos_checked || 0} 条新视频，找到 ${summary.eligible || 0} 条可参与活动。`
        : `已找到 ${summary.eligible || 0} 条可参与活动。`;
    }
    if (status.resume_available) message += "进度已保存，使用相同关键词和排序可继续。";
  }
  setText("discovery-badge", badge);
  setText("discovery-status", message);
  const videoSummary = element("discovery-video-summary");
  if (videoSummary) videoSummary.hidden = !status.video_checks?.total;
  if (status.video_checks?.total) {
    const counts = status.video_checks.counts;
    const labels = (Object.keys(videoStates) as VideoState[]).filter((key) => counts[key]);
    setText("discovery-video-summary-counts", labels.map((key) => `${videoStates[key]} ${counts[key]} 条`).join(" · "));
  }
  const queue = status.queue;
  const queuePanel = element("discovery-queue");
  if (queuePanel) queuePanel.hidden = !queue?.total;
  if (queue?.total) {
    const skipped = queue.existing + queue.expired + queue.skipped;
    setText("discovery-queue-status", `已入库 ${queue.imported} 条 · 跳过 ${skipped} 条 · 可选择 ${status.pending_selection || 0} 条${queue.message ? `。${queue.message}` : ""}`);
  }
}

export function bindDiscovery() {
  const form = element<HTMLFormElement>("discovery-form");
  const input = element<HTMLInputElement>("discovery-keywords");
  const mode = element<HTMLSelectElement>("discovery-mode");
  const targetInput = element<HTMLInputElement>("discovery-target");
  const stop = element<HTMLButtonElement>("discovery-stop");
  const importButton = element<HTMLButtonElement>("discovery-import");
  const selectedButton = element<HTMLButtonElement>("discovery-import-selected");
  const selectionPanel = element("discovery-selection");
  const candidateBody = element<HTMLTableSectionElement>("discovery-candidates");
  const selectAll = element<HTMLInputElement>("discovery-select-all");
  const filterPanel = element<HTMLDetailsElement>("discovery-filters");
  const filterForm = element<HTMLFormElement>("discovery-filter-form");
  const filterFields = element<HTMLFieldSetElement>("discovery-filter-fields");
  const previewButton = element<HTMLButtonElement>("discovery-preview");
  const filteredImport = element<HTMLButtonElement>("discovery-import-filtered");
  if (!form || !input || !mode || !targetInput || !stop || !importButton || !selectedButton || !selectionPanel || !candidateBody ||
      !selectAll || !filterPanel || !filterForm || !filterFields || !previewButton || !filteredImport) return;
  let snapshot: DiscoveryStatus = { job: null, keywords: ["转发抽奖"], summary: {}, resume_available: false };
  let submitting = false;
  let reading = false;
  let revision = 0;
  let initialized = false;
  let shownImportCount = 0;
  let shownFilterId: string | undefined;
  let shownCandidates = "";
  const selected = new Set<string>();

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
  const renderSelection = () => {
    const candidates = snapshot.candidates || [];
    const blocked = input.disabled;
    const signature = JSON.stringify(candidates);
    selectionPanel.hidden = !candidates.length;
    if (signature !== shownCandidates) {
      shownCandidates = signature;
      const pending = new Set(candidates.map((row) => row.dynamic_id));
      selected.forEach((id) => { if (!pending.has(id)) selected.delete(id); });
      candidateBody.replaceChildren(...candidates.map((candidate) => {
        const row = document.createElement("tr");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = candidate.dynamic_id;
        checkbox.setAttribute("aria-label", `选择 ${candidate.prize || candidate.publisher || candidate.dynamic_id}`);
        row.insertCell().append(checkbox);
        const link = document.createElement("a");
        link.href = `https://www.bilibili.com/opus/${encodeURIComponent(candidate.dynamic_id)}`;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = candidate.prize || "查看活动";
        row.insertCell().append(link);
        row.insertCell().textContent = candidate.publisher || "未知";
        row.insertCell().textContent = candidate.publisher_uid || "—";
        row.insertCell().textContent = candidate.followers?.toLocaleString() ?? "未知";
        row.insertCell().textContent = tripleLabels[candidate.triple_requirement] || "未知";
        return row;
      }));
    }
    candidateBody.querySelectorAll<HTMLInputElement>("input").forEach((checkbox) => {
      checkbox.checked = selected.has(checkbox.value);
      checkbox.disabled = blocked;
    });
    selectAll.disabled = blocked || !candidates.length;
    selectAll.checked = candidates.length > 0 && selected.size === candidates.length;
    selectAll.indeterminate = selected.size > 0 && !selectAll.checked;
    selectedButton.disabled = blocked || !selected.size;
    selectedButton.textContent = selected.size ? `导入所选 ${selected.size} 条` : "导入所选";
    importButton.disabled = blocked || !candidates.length;
    importButton.textContent = `导入全部 ${candidates.length} 条`;
  };
  const render = () => {
    const sameSearch = mode.value === (snapshot.mode || "standard") &&
      Number(targetInput.value) === (snapshot.target_new_videos || snapshot.summary.target_new_videos || 100) &&
      JSON.stringify(keywords()) === JSON.stringify(snapshot.keywords);
    renderDiscovery(snapshot, submitting);
    const start = element<HTMLButtonElement>("discovery-start");
    if (start && !start.disabled && !sameSearch) start.textContent = "开始检索";
    mode.disabled = input.disabled;
    renderSelection();
    filterPanel.hidden = !snapshot.pending_selection;
    filterFields.disabled = input.disabled;
    previewButton.disabled = input.disabled;
    filteredImport.disabled = input.disabled || !matchesPreview() || !snapshot.filter?.matched;
    const issue = snapshot.queue_job?.action === "control_discovery_filter" ? snapshot.queue_job.result?.message : undefined;
    setText("discovery-filter-result", matchesPreview()
      ? `匹配 ${snapshot.filter?.matched || 0} / ${snapshot.filter?.total || 0} 条 · 发布者资料未知 ${snapshot.filter?.unknown_metadata || 0} 条`
      : issue || "调整条件后预览匹配数量，再确认加入。");
    setText("discovery-help", mode.value === "comprehensive_page"
      ? "搜索 1 个关键词，按综合排序持续翻页；已有和本轮重复 BV 均跳过，不计入目标。"
      : "最多 3 个关键词，用逗号分隔。按最新发布持续翻页；已有和本轮重复 BV 均跳过，不计入目标。");
  };
  const apply = (value: DiscoveryStatus) => {
    if (value.job?.job_id !== snapshot.job?.job_id) selected.clear();
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
      targetInput.value = String(value.target_new_videos || value.summary.target_new_videos || 100);
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
      const status = await fetchJSON<DiscoveryStatus>(path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      apply(status);
      notifyTaskQueueChanged();
      const entry = status.queue_entry;
      if (entry?.state === "queued" || entry?.state === "error") {
        showTaskNotice(entry.state === "queued" ? `已排队 · ${entry.label}` : entry.message);
      }
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
    const target = Number(targetInput.value);
    if (!Number.isInteger(target) || target < 1 || target > 10000) {
      showToast("目标新视频数请输入 1–10000 的整数", "info");
      targetInput.focus();
      return;
    }
    submit("/api/discovery/start?enqueue=true", { keywords: words, mode: mode.value, target_new_videos: target });
  });
  input.addEventListener("input", render);
  mode.addEventListener("change", render);
  targetInput.addEventListener("input", render);
  stop.addEventListener("click", () => {
    const job = activeJob(snapshot);
    if (submitting || job?.state !== "running" || job.cancel_requested) return;
    submit("/api/discovery/stop", { job_id: job.job_id });
  });
  importButton.addEventListener("click", () => {
    if (importButton.disabled || !snapshot.job || !requireSetup("control_discover")) return;
    submit("/api/discovery/import", { job_id: snapshot.job.job_id, selection: "all" });
  });
  selectedButton.addEventListener("click", () => {
    if (selectedButton.disabled || !snapshot.job || !requireSetup("control_discover")) return;
    submit("/api/discovery/import", { job_id: snapshot.job.job_id, selection: "selected", selected_ids: [...selected] });
  });
  selectAll.addEventListener("change", () => {
    selected.clear();
    if (selectAll.checked) snapshot.candidates?.forEach((row) => selected.add(row.dynamic_id));
    renderSelection();
  });
  candidateBody.addEventListener("change", (event) => {
    const checkbox = event.target;
    if (!(checkbox instanceof HTMLInputElement)) return;
    if (checkbox.checked) selected.add(checkbox.value);
    else selected.delete(checkbox.value);
    renderSelection();
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
  const openVideoRecords = bindSourceViews();
  element("discovery-open-video-checks")?.addEventListener("click", () => {
    if (snapshot.job) openVideoRecords?.(snapshot.job.job_id);
  });
  refresh();
  window.setInterval(refresh, 3000);
  window.addEventListener("pageshow", () => refresh());
}

function bindCoinBalance() {
  const toggle = element<HTMLInputElement>("account-coin-live");
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
      setText("account-coin-balance", balance.balance === null ? "—" : `${balance.estimated ? "约 " : ""}${balance.balance}`);
      const time = balance.checked_at ? new Date(balance.checked_at * 1000).toLocaleString() : "尚未取得余额";
      setText("account-coin-note", `${time} · 默认每 12 小时联网校准`);
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

function bindSourceViews() {
  const buttons = document.querySelectorAll<HTMLButtonElement>("[data-source-view]");
  const body = element<HTMLTableSectionElement>("discovery-video-items");
  const table = element("discovery-video-table");
  const pagination = element("discovery-video-pages");
  const prev = element<HTMLButtonElement>("discovery-video-prev");
  const next = element<HTMLButtonElement>("discovery-video-next");
  const all = element<HTMLButtonElement>("discovery-video-all");
  const videosButton = document.querySelector<HTMLButtonElement>('[data-source-view="videos"]');
  if (!body || !table || !pagination || !prev || !next || !all || !videosButton) return;
  const pageSize = 50;
  let offset = 0;
  let total = 0;
  let jobId: number | null = null;
  let requestId = 0;
  const load = async (start: number) => {
    const request = ++requestId;
    prev.disabled = next.disabled = true;
    setText("discovery-video-status", "正在读取 BV 记录…");
    try {
      const result = await fetchJSON<{ total: number; items: VideoRecord[] }>(
        `/api/discovery/videos?offset=${start}&limit=${pageSize}${jobId === null ? "" : `&job_id=${jobId}`}`,
      );
      if (request !== requestId) return;
      offset = start;
      total = result.total;
      body.replaceChildren(...result.items.map((item) => {
        const row = document.createElement("tr");
        const link = document.createElement("a");
        link.href = `https://www.bilibili.com/video/${encodeURIComponent(item.bvid)}`;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = item.title || item.bvid;
        const identity = row.insertCell();
        identity.append(link);
        if (item.title) {
          const bv = document.createElement("span");
          bv.className = "video-record-detail";
          bv.textContent = item.bvid;
          identity.append(bv);
        }
        const resultCell = row.insertCell();
        const status = document.createElement("strong");
        status.dataset.videoState = item.status;
        status.textContent = videoStates[item.status] || videoStates.pending;
        resultCell.append(status);
        if (item.source_reason) {
          const sourceReason = document.createElement("span");
          sourceReason.className = "video-record-detail";
          sourceReason.textContent = item.source_reason;
          resultCell.append(sourceReason);
        }
        const reasons = (item.reasons || []).map((reason) => {
          const detail = document.createElement("span");
          detail.className = "video-record-detail";
          const model = reason.model_assessment ? modelStates[reason.model_assessment] : "";
          const scope = reason.kind === "video_comment" ? "视频" : "关联动态";
          const confidence = typeof reason.model_confidence === "number" ? ` · 线索可信度 ${reason.model_confidence}/100` : "";
          detail.textContent = `${reason.message}${reason.count > 1 ? `（${reason.count} 个）` : ""}`
            + (reason.model_reason ? `。模型（${scope}${model ? ` · ${model}` : ""}${confidence}）${reason.model_reason}` : "")
            + (reason.rules_summary ? `。规则摘要：${reason.rules_summary}` : "");
          return detail;
        });
        resultCell.append(...reasons.slice(0, 2));
        if (reasons.length > 2) {
          const more = document.createElement("details");
          const toggle = document.createElement("summary");
          toggle.textContent = `其余 ${reasons.length - 2} 项原因`;
          more.append(toggle, ...reasons.slice(2));
          resultCell.append(more);
        }
        if (item.activities?.length) {
          const linked = document.createElement("details");
          const toggle = document.createElement("summary");
          toggle.textContent = `逐条查看 ${item.activities.length} 个关联动态`;
          linked.append(toggle);
          for (const activity of item.activities) {
            const entry = document.createElement("div");
            entry.className = "video-record-detail";
            const target = document.createElement("a");
            target.href = `https://www.bilibili.com/opus/${encodeURIComponent(activity.dynamic_id)}`;
            target.target = "_blank";
            target.rel = "noopener noreferrer";
            target.textContent = activity.prize || activity.dynamic_id;
            const details = [videoStates[activity.status], activity.reason];
            if (typeof activity.model_confidence === "number") details.push(`线索可信度 ${activity.model_confidence}/100`);
            if (typeof activity.followers === "number") details.push(`${activity.followers.toLocaleString()} 粉丝`);
            if (activity.rules_summary) details.push(`规则摘要：${activity.rules_summary}`);
            entry.append(target, ` · ${details.join(" · ")}`);
            linked.append(entry);
          }
          resultCell.append(linked);
        }
        const repeats = row.insertCell();
        repeats.textContent = item.repeat_count_incomplete ? (item.repeat_count ? `≥ ${item.repeat_count}` : "未统计") : String(item.repeat_count ?? 0);
        if (item.repeat_count_incomplete) repeats.title = "此视频的旧版重复抓取次数未统计";
        const dates = row.insertCell();
        for (const [label, value] of [["首次", item.first_seen], ["最近", item.last_seen]]) {
          const date = document.createElement("span");
          date.className = "video-record-detail";
          date.textContent = `${label} ${new Date(value).toLocaleString()}`;
          dates.append(date);
        }
        return row;
      }));
      table.hidden = !result.items.length;
      pagination.hidden = total <= pageSize;
      setText("discovery-video-status", result.items.length
        ? `${jobId === null ? "全部" : "本轮"}共 ${total} 条 · 第 ${offset + 1}–${offset + result.items.length} 条` : "暂无已收录的视频。");
    } catch {
      if (request === requestId) setText("discovery-video-status", "暂时无法读取 BV 记录，点击「BV 记录」重试。");
    } finally {
      if (request === requestId) {
        prev.disabled = offset === 0;
        next.disabled = offset + pageSize >= total;
      }
    }
  };
  const show = (button: HTMLButtonElement) => {
    buttons.forEach((item) => {
      const active = item === button;
      item.setAttribute("aria-pressed", String(active));
      const panel = element(item.getAttribute("aria-controls") || "");
      if (panel) panel.hidden = !active;
    });
  };
  const openRecords = (job: number | null) => {
    if (jobId !== job) {
      offset = total = 0;
      table.hidden = pagination.hidden = true;
    }
    jobId = job;
    all.hidden = jobId === null;
    show(videosButton);
    void load(offset);
  };
  buttons.forEach((button) => button.addEventListener("click", () => {
    if (button === videosButton) openRecords(null);
    else show(button);
  }));
  all.addEventListener("click", () => openRecords(null));
  prev.addEventListener("click", () => load(offset - pageSize));
  next.addEventListener("click", () => load(offset + pageSize));
  return openRecords;
}
