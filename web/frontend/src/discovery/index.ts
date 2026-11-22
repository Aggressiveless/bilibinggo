import { fetchJSON } from "../api/client";
import { requireSetup } from "../account/index";
import { loadSummary } from "../activities/index";
import { notifyJobStartError, startPolling } from "../jobs/index";
import { showToast } from "../shell/toast";

interface DiscoveryStatus {
  job: null | {
    job_id: number;
    state: string;
    cancel_requested: boolean;
    progress?: { message?: string };
  };
  keywords: string[];
  summary: {
    videos_checked?: number;
    verification_remaining?: number;
    eligible?: number;
    requests_used?: number;
    completion_reason?: string;
    phase?: string;
  };
  candidates: { dynamic_id?: string; prize?: string; publisher?: string; raw?: { prize?: string } }[];
  resume_available: boolean;
  stop_message?: string;
}

const phases: Record<string, string> = {
  search: "搜索视频", scan_videos: "扫描视频", resolve_sources: "解析活动入口", verify_candidates: "核验候选活动",
};

function element<T extends HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}

function setText(id: string, text: string | number) {
  const target = element(id);
  if (target) target.textContent = String(text);
}

export function renderDiscovery(status: DiscoveryStatus, submitting = false) {
  const job = status.job;
  const running = job?.state === "running";
  const stopping = running && job.cancel_requested;
  const start = element<HTMLButtonElement>("discovery-start");
  const stop = element<HTMLButtonElement>("discovery-stop");
  const input = element<HTMLInputElement>("discovery-keywords");
  if (start) {
    start.disabled = running || submitting;
    start.textContent = running ? "检索中…" : status.resume_available ? "继续检索" : "开始检索";
  }
  if (stop) {
    stop.disabled = !running || stopping || submitting;
    stop.textContent = stopping ? "正在停止…" : "停止检索";
  }
  if (input) input.disabled = running || submitting;
  const summary = status.summary;
  setText("discovery-videos", summary.videos_checked || 0);
  setText("discovery-remaining", summary.verification_remaining || 0);
  setText("discovery-eligible", summary.eligible || 0);
  setText("discovery-requests", summary.requests_used || 0);
  let badge = "待开始";
  let message = "输入关键词后开始检索。";
  if (running) {
    badge = stopping ? "正在停止" : phases[summary.phase || ""] || "检索中";
    message = stopping ? "正在停止检索，等待当前请求返回并保存进度…" : job.progress?.message || "正在准备检索…";
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
      message = `已找到 ${summary.eligible || 0} 条可参与活动。`;
    }
    if (status.resume_available) message += "进度已保存，点击「继续检索」接着处理。";
  }
  setText("discovery-badge", badge);
  setText("discovery-status", message);
  const results = element<HTMLUListElement>("discovery-results");
  if (results) {
    results.replaceChildren();
    for (const row of status.candidates) {
      if (!/^\d{15,22}$/.test(row.dynamic_id || "")) continue;
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = `https://www.bilibili.com/opus/${row.dynamic_id}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = row.prize || row.raw?.prize || "查看活动动态";
      item.append(link);
      results.append(item);
    }
    results.hidden = !results.childElementCount;
  }
}

export function bindDiscovery() {
  const form = element<HTMLFormElement>("discovery-form");
  const input = element<HTMLInputElement>("discovery-keywords");
  const stop = element<HTMLButtonElement>("discovery-stop");
  if (!form || !input || !stop) return;
  let snapshot: DiscoveryStatus = { job: null, keywords: ["转发抽奖"], summary: {}, candidates: [], resume_available: false };
  let submitting = false;
  let reading = false;
  let revision = 0;
  let initialized = false;

  const apply = (value: DiscoveryStatus) => {
    snapshot = value;
    if (!initialized || value.job?.state === "running") input.value = value.keywords.join("，");
    initialized = true;
    renderDiscovery(value, submitting);
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
    renderDiscovery(snapshot, true);
    try {
      apply(await fetchJSON<DiscoveryStatus>(path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      }));
      loadSummary().then((job) => { if (job?.state === "running") startPolling(); }).catch(() => {});
    } catch (error) {
      notifyJobStartError(error, "control_discover");
    } finally {
      submitting = false;
      renderDiscovery(snapshot);
      refresh();
    }
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitting || snapshot.job?.state === "running" || !requireSetup("control_discover")) return;
    const words = [...new Set(input.value.split(/[,，\n]/).map((word) => word.trim()).filter(Boolean))];
    if (!words.length || words.length > 3 || words.some((word) => word.length > 80)) {
      showToast("请输入 1–3 个关键词，每个不超过 80 字", "info");
      input.focus();
      return;
    }
    submit("/api/discovery/start", { keywords: words });
  });
  stop.addEventListener("click", () => {
    if (submitting || snapshot.job?.state !== "running" || snapshot.job.cancel_requested) return;
    submit("/api/discovery/stop", { job_id: snapshot.job.job_id });
  });
  refresh();
  window.setInterval(refresh, 3000);
  window.addEventListener("pageshow", () => refresh());
}
