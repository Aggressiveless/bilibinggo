
import { fetchJSON } from "../api/client";
import { showToast } from "../shell/toast";
import { loadActivities, loadSummary } from "../activities";
import "./styles.css";

type Counts = Record<string, number>;
type Node = {
  id: string; name: string; online: boolean; host: boolean; work: number; kind?: string;
  key?: string; completed: number; model_calls: number;
};
type Status = {
  role: "host" | "member" | null;
  address?: string;
  node?: string;
  connected: boolean;
  worker_ready: boolean;
  last_sync: number;
  local_cursor: number;
  error: string;
  status: {
    round?: number; target?: number; counts?: Record<string, Counts>; nodes?: Node[];
    phase?: string; paused?: boolean; now?: number; next_round?: number; cooldown_until?: number; active?: number; sequence?: number;
  };
};

const byId = <T extends HTMLElement>(name: string) => document.getElementById("cluster-" + name) as T;
const set = (name: string, value: string | number) => { byId(name).textContent = String(value); };
const time = (stamp: number) => new Date(stamp * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
const kinds: Record<string, string> = { search: "检索", video: "视频检查", dynamic: "动态核验", short: "展开短链", article: "检查专栏", author: "检查发布者" };
const phases: Record<string, string> = { complete: "本轮完成", incomplete: "本轮有任务未完成", exhausted: "本轮可用搜索页已查完", running: "协作检索中" };

let current: Status | undefined;
let busy = false;
let refreshing = false;

function render(snapshot: Status) {
  const priorRole = current?.role;
  current = snapshot;
  const status = snapshot.status;
  const connected = Boolean(snapshot.role);
  if (priorRole !== snapshot.role) byId("invite-text").hidden = true;
  const nav = byId("nav-indicator");
  nav.hidden = !connected;
  nav.classList.toggle("cluster-dot-offline", !snapshot.connected);
  byId("setup").hidden = connected;
  byId("monitor").hidden = !connected;
  if (!connected) return;

  byId("host-actions").hidden = snapshot.role !== "host";
  const pause = byId<HTMLButtonElement>("pause");
  pause.textContent = status.paused ? "继续检索" : "暂停检索";
  pause.dataset.paused = String(Boolean(status.paused));
  set("leave", snapshot.role === "host" ? "解散集群" : "退出集群");
  set("role", snapshot.role === "host" ? "主节点" : "工作节点");
  set("address", snapshot.address || "");
  set("link-state", snapshot.error || (snapshot.connected ? "同步正常" : "等待同步"));
  set("sync-time", snapshot.last_sync ? "上次同步 " + time(snapshot.last_sync) : "");
  byId("worker-note").hidden = snapshot.worker_ready;
  const counts = status.counts || {};
  const video = counts.video || {};
  const found = Object.values(video).reduce((a, b) => a + b, 0);
  const target = status.target || 1000;
  const done = video.done || 0;
  set("progress-value", done + " / " + target);
  const progress = byId<HTMLProgressElement>("progress");
  progress.max = target;
  progress.value = done;
  set("found", found);
  set("active", status.active ?? "—");
  const totals = Object.values(counts);
  set("pending", totals.reduce((sum, row) => sum + (row.pending || 0), 0));
  set("failed", totals.reduce((sum, row) => sum + (row.failed || 0), 0));
  const cooldown = status.cooldown_until || 0;
  const next = status.next_round || 0;
  const phase = status.paused ? "检索已暂停"
    : cooldown > (status.now || 0) ? "平台限流，暂停至 " + time(cooldown)
    : (phases[status.phase || ""] || "等待节点");
  set("phase", [status.round ? "第 " + status.round + " 轮" : "", phase,
    next && !status.paused ? "下一轮 " + time(next) : ""].filter(Boolean).join(" · "));

  const nodes = status.nodes || [];
  set("online", nodes.filter((node) => node.online).length + " / " + nodes.length + " 在线");
  const tbody = byId("nodes");
  tbody.replaceChildren();
  for (const node of nodes) {
    const row = document.createElement("tr");
    const fields = [
      node.name + (node.id === snapshot.node ? "（本机）" : ""),
      node.host ? "主节点" : "工作节点",
      node.online ? (node.kind ? kinds[node.kind] + " · " + (node.key || "") : node.work ? "等待任务" : "仅同步") : "离线",
      String(node.completed),
      String(node.model_calls),
    ];
    for (const label of fields) {
      const cell = document.createElement("td");
      cell.textContent = label;
      row.appendChild(cell);
    }
    tbody.appendChild(row);
  }
}

async function refresh() {
  if (busy || refreshing) return;
  refreshing = true;
  try {
    const snapshot = await fetchJSON<Status>("/api/cluster/status");
    if (!busy) {
      const prior = current?.local_cursor;
      render(snapshot);
      if (prior !== undefined && prior !== snapshot.local_cursor) {
        void loadSummary().catch(() => {});
        void loadActivities().catch(() => {});
      }
    }
  } catch (error) {
    set("link-state", "无法读取集群状态");
  } finally {
    refreshing = false;
  }
}

async function action(path: string, data?: object) {
  if (busy) return;
  busy = true;
  byId("root").setAttribute("aria-busy", "true");
  const buttons = [...byId("root").querySelectorAll("button")];
  buttons.forEach((button) => button.disabled = true);
  try {
    const result = await fetchJSON<Status & { invite?: string }>("/api/cluster/" + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: data ? JSON.stringify(data) : undefined,
    });
    if (result.invite) {
      byId<HTMLInputElement>("invite-text").value = result.invite;
      byId("invite-text").hidden = false;
      byId<HTMLInputElement>("invite-text").select();
    } else {
      render(result);
      if (path === "join") byId<HTMLInputElement>("invite").value = "";
    }
  } catch (error) {
    showToast(error instanceof Error ? error.message : "集群操作失败", "error");
  } finally {
    buttons.forEach((button) => button.disabled = false);
    byId("root").removeAttribute("aria-busy");
    busy = false;
  }
}

export function bindCluster() {
  if (!byId("root")) return;
  for (const mode of ["host", "join"]) {
    byId<HTMLFormElement>(mode + "-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.currentTarget as HTMLFormElement));
      void action(mode, data);
    });
  }
  byId("pause").addEventListener("click", () => void action("pause?paused=" + String(byId("pause").dataset.paused !== "true")));
  byId("show-invite").addEventListener("click", () => void action("invite"));
  byId("leave").addEventListener("click", () => {
    if (current?.role === "host" && !window.confirm("解散后所有节点将断开。确定解散集群？")) return;
    void action("leave");
  });
  document.querySelector('[data-section="cluster"]')?.addEventListener("click", () => void refresh());
  void refresh();
  window.setInterval(() => { if (!document.hidden) void refresh(); }, 5000);
}
