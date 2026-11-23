import { fetchJSON } from "../api/client";
import { sanitizeUserText } from "../utils/text";

type JobState = "queued" | "starting" | "running" | "success" | "error" | "cancelled" | "interrupted";

interface Entry {
  id: string;
  label: string;
  state: JobState;
  message: string;
  job_id: number | null;
}

interface Job {
  id: number;
  label: string;
  state: JobState;
  message: string;
  log?: string;
}

interface QueueSnapshot {
  entries: Entry[];
  jobs: Job[];
  pause_reason: string;
  current_job: Job;
}

const labels: Record<JobState, string> = {
  queued: "等待中", starting: "启动中", running: "运行中", success: "已完成",
  error: "失败", cancelled: "已取消", interrupted: "已中断",
};

const element = (id: string) => document.getElementById(id)!;

export function notifyTaskQueueChanged() {
  window.dispatchEvent(new Event("task-queue-changed"));
}

export function showTaskNotice(message: string, detail = "") {
  element("task-notice-message").textContent = sanitizeUserText(message);
  element("task-notice-detail").textContent = sanitizeUserText(detail);
  element("task-notice").hidden = false;
}

function taskButton(label: string, description: string, click: () => void) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-secondary btn-compact";
  button.textContent = label;
  button.title = description;
  button.setAttribute("aria-label", description);
  button.addEventListener("click", click);
  return button;
}

function taskRow(label: string, status: JobState, message: string) {
  const item = document.createElement("li");
  item.className = "task-row";
  const name = document.createElement("strong");
  name.textContent = sanitizeUserText(label);
  const badge = document.createElement("span");
  badge.className = "task-status";
  badge.dataset.state = status;
  badge.textContent = labels[status] || status;
  const head = document.createElement("div");
  head.className = "task-row-heading";
  head.append(name, badge);
  item.append(head);
  if (message) {
    const detail = document.createElement("p");
    detail.textContent = sanitizeUserText(message);
    item.append(detail);
  }
  return item;
}

export function bindTaskLogs(onRunning: () => void) {
  const section = document.getElementById("section-logs");
  if (!section) return;
  const queueList = element("task-queue-list");
  const historyList = element("task-history-list");
  const resume = element("task-queue-resume") as HTMLButtonElement;
  const liveLog = element("task-live-log");
  const badge = element("task-nav-count");
  let reading = false;
  let saving = false;
  let revision = 0;
  let logRevision = 0;
  let lastSignature = "";

  const feedback = (message: string) => {
    element("task-queue-feedback").textContent = sanitizeUserText(message);
    element("task-queue-feedback").hidden = !message;
  };

  const showLive = () => {
    ++logRevision;
    element("task-log-live").hidden = false;
    element("task-log-history").hidden = true;
    liveLog.hidden = true;
    element("task-log-heading").textContent = "当前任务";
  };

  const openLog = async (id: number) => {
    const request = ++logRevision;
    element("task-log-live").hidden = true;
    element("task-log-history").hidden = false;
    liveLog.hidden = false;
    element("task-log-heading").textContent = `任务 #${id}`;
    element("task-history-message").textContent = "正在读取日志…";
    element("task-history-log").textContent = "";
    try {
      const log = await fetchJSON<Job>(`/api/task-queue/logs/${id}`);
      if (request !== logRevision) return;
      element("task-history-message").textContent = sanitizeUserText(`${log.label} · ${log.message}`);
      element("task-history-log").textContent = sanitizeUserText(log.log || "此任务暂无日志明细。");
    } catch (error) {
      if (request === logRevision) element("task-history-message").textContent = sanitizeUserText(String(error));
    }
  };

  const mutate = async (url: string, options: RequestInit) => {
    if (saving) return;
    saving = true;
    ++revision;
    feedback("");
    try {
      await fetchJSON(url, options);
    } catch (error) {
      feedback(String(error));
    } finally {
      saving = false;
      await refresh();
    }
  };

  const change = (id: string, operation: "up" | "down" | "remove") => mutate(`/api/task-queue/${id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operation }),
  });

  const render = (snapshot: QueueSnapshot) => {
    const waiting = snapshot.entries.filter(entry => entry.state === "queued");
    const running = snapshot.entries.find(entry => entry.state === "starting" || entry.state === "running");
    const externalJob = !running && snapshot.current_job?.state === "running" ? snapshot.current_job : null;
    if (snapshot.current_job?.state === "running") onRunning();
    badge.hidden = !waiting.length && !running && !externalJob;
    badge.textContent = waiting.length ? String(waiting.length) : "•";
    badge.title = waiting.length ? `${waiting.length} 个任务等待执行` : "有任务正在运行";
    if (!section.classList.contains("active")) return;
    const signature = JSON.stringify([snapshot.entries, snapshot.jobs, snapshot.pause_reason, externalJob?.id]);
    if (signature === lastSignature) return;
    lastSignature = signature;
    element("task-queue-summary").textContent = snapshot.pause_reason || (waiting.length
      ? `${waiting.length} 个任务等待，可调整顺序` : running || externalJob ? "当前任务正在执行" : "没有等待中的任务");
    resume.hidden = !snapshot.pause_reason;
    queueList.replaceChildren();
    if (externalJob) {
      queueList.append(taskRow(externalJob.label, "running", externalJob.message));
    }
    if (running) {
      const row = taskRow(running.label, running.state, running.message);
      row.append(taskButton("当前日志", `查看${running.label}当前日志`, showLive));
      queueList.append(row);
    }
    waiting.forEach((entry, index) => {
      const row = taskRow(entry.label, entry.state, "");
      const controls = document.createElement("div");
      controls.className = "task-row-controls";
      const up = taskButton("上移", `上移${entry.label}`, () => void change(entry.id, "up"));
      const down = taskButton("下移", `下移${entry.label}`, () => void change(entry.id, "down"));
      up.disabled = index === 0;
      down.disabled = index === waiting.length - 1;
      controls.append(up, down, taskButton("移出", `移出${entry.label}`, () => void change(entry.id, "remove")));
      row.append(controls);
      queueList.append(row);
    });
    historyList.replaceChildren();
    const notStarted = snapshot.entries.filter(entry => !entry.job_id && ["error", "interrupted", "cancelled"].includes(entry.state));
    notStarted.slice(-5).reverse().forEach(entry => historyList.append(taskRow(entry.label, entry.state, entry.message)));
    snapshot.jobs.filter(job => job.state !== "running").forEach(job => {
      const entry = snapshot.entries.find(item => item.job_id === job.id);
      const row = taskRow(entry?.label || job.label, entry?.state === "interrupted" ? entry.state : job.state,
        entry?.state === "interrupted" ? entry.message : job.message);
      row.append(taskButton("查看日志", `查看任务 ${job.id} 日志`, () => void openLog(job.id)));
      historyList.append(row);
    });
    if (!historyList.children.length) {
      const empty = document.createElement("li");
      empty.className = "caption";
      empty.textContent = "暂无已结束的任务";
      historyList.append(empty);
    }
  };

  async function refresh() {
    if (reading || saving) return;
    reading = true;
    const request = revision;
    try {
      const snapshot = await fetchJSON<QueueSnapshot>("/api/task-queue");
      if (request === revision) render(snapshot);
    } catch (error) {
      if (!lastSignature && section!.classList.contains("active")) feedback(String(error));
    } finally {
      reading = false;
    }
  }

  resume.addEventListener("click", () => void mutate("/api/task-queue/resume", { method: "POST" }));
  liveLog.addEventListener("click", showLive);
  window.addEventListener("task-queue-changed", () => void refresh());
  window.setInterval(() => void refresh(), 2500);
  void refresh();
}
