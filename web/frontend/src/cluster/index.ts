
import { fetchJSON } from "../api/client";
import { showToast } from "../shell/toast";
import { loadActivities, loadSummary } from "../activities";
import { mountDetectedActivities } from "./detections";
import "./styles.css";

type Counts = Record<string, number>;
type Conversion = { checked: number; matched: number };
type Node = {
  id: string; name: string; online: boolean; host: boolean; work: number; kind?: string;
  key?: string; completed: number; model_calls: number; task_label?: string;
};
type ClusterEvent = {
  id: number;
  at: number;
  level: "info" | "warning" | "error";
  node: string | null;
  node_name: string | null;
  message: string;
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
    round?: number; target?: number; counts?: Record<string, Counts>; nodes?: Node[]; events?: ClusterEvent[];
    local_work?: Record<"pages" | "found" | "videos" | "activities", number>;
    local_events?: ClusterEvent[];
    phase?: string; paused?: boolean; now?: number; next_round?: number; cooldown_until?: number;
    search_pages_total?: number; search_history_start?: number; concurrency_limit?: number; two_stage?: boolean;
    conversion?: Conversion; local_conversion?: Conversion; detected_open?: number; sequence?: number;
  };
};

const byId = <T extends HTMLElement>(name: string) => document.getElementById("cluster-" + name) as T;
const set = (name: string, value: string | number) => {
  const element = byId(name);
  const label = String(value);
  if (element.textContent !== label) element.textContent = label;
};
const number = new Intl.NumberFormat("zh-CN");
const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });
const count = (value?: number) => value === undefined ? "—" : number.format(value);
const time = (stamp: number) => new Date(stamp * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
const kinds: Record<string, string> = { search: "检索页", video: "视频检测", dynamic: "动态核验", short: "短链展开", article: "专栏检查", author: "发布者检查" };
const phases: Record<string, string> = {
  collecting: "协作检索中", detecting: "分配检测中", running: "协作检索中",
  complete: "本轮完成", incomplete: "本轮有任务未完成", exhausted: "本轮完成，收录未达目标",
};

let current: Status | undefined;
let busy = false;
let refreshing = false;
let updateDetections: ReturnType<typeof mountDetectedActivities>;
const eventPanels: Array<(snapshot: Status) => void> = [];
const eventTime = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

function eventRow(event: ClusterEvent, showNode = true) {
  const item = document.createElement("li");
  item.dataset.level = event.level;
  const date = document.createElement("time");
  date.dateTime = new Date(event.at * 1000).toISOString();
  date.textContent = eventTime.format(event.at * 1000);
  item.append(date);
  if (showNode) {
    const node = document.createElement("span");
    node.className = "cluster-event-node";
    node.textContent = event.node_name || "集群";
    item.append(node);
  }
  const message = document.createElement("span");
  message.textContent = event.level === "error" ? "失败 · " + event.message
    : event.level === "warning" ? "异常 · " + event.message : event.message;
  item.append(message);
  return item;
}

let lastDiscoveryEvents = "";
function renderConversion(prefix: string, conversion: Conversion | undefined, completed: number) {
  const id = prefix ? prefix + "-" : "";
  byId(id + "yield").hidden = conversion === undefined;
  if (!conversion) return;
  const { matched, checked } = conversion;
  set(id + "conversion", checked ? percent.format(matched / checked) : "—");
  const missing = Math.max(0, completed - checked);
  set(id + "conversion-detail", "命中 " + count(matched) + " / 已统计 " + count(checked) + " 个视频" +
    (missing ? "，另有 " + count(missing) + " 个早期结果缺少明细" : ""));
}

function renderDiscoveryWork(snapshot: Status) {
  const panel = byId("discovery-work");
  if (!panel) return;
  panel.hidden = !snapshot.role;
  if (!snapshot.role) return;
  const status = snapshot.status;
  const node = status.nodes?.find((record) => record.id === snapshot.node);
  set("discovery-note", [node?.name, status.round ? "第 " + status.round + " 轮" : ""].filter(Boolean).join(" · "));
  let task: string;
  if (!snapshot.connected) {
    task = snapshot.error || "等待同步本机集群状态";
  } else if (node?.kind) {
    task = node.task_label || kinds[node.kind] + " · " + (node.key || "");
  } else if (status.paused) {
    task = "集群已暂停领取新任务";
  } else if ((status.cooldown_until || 0) > (status.now || 0)) {
    task = "平台限流，暂停至 " + time(status.cooldown_until!);
  } else if (status.next_round) {
    task = "本轮已结束，下一轮 " + time(status.next_round);
  } else {
    task = snapshot.worker_ready ? "本机等待分配任务" : "本机仅同步活动";
  }
  const currentTask = byId("discovery-current");
  if (currentTask.textContent !== task) currentTask.textContent = task;
  for (const metric of ["pages", "found", "videos", "activities"] as const) {
    set("discovery-" + metric, status.local_work?.[metric] ?? "—");
  }
  renderConversion("local", status.local_conversion, status.local_work?.videos || 0);

  const events = status.local_events ?? (status.events || []).filter((event) => event.node === snapshot.node).slice(0, 6);
  const list = byId("discovery-events");
  list.hidden = events.length === 0;
  const signature = JSON.stringify(events);
  if (signature !== lastDiscoveryEvents) {
    list.replaceChildren(...events.map((event) => eventRow(event, false)));
    lastDiscoveryEvents = signature;
  }
}

function mountEventPanels() {
  const template = document.getElementById("cluster-log-template") as HTMLTemplateElement | null;
  if (!template) return;
  for (const mount of document.querySelectorAll<HTMLElement>("[data-cluster-log]")) {
    const panel = template.content.firstElementChild!.cloneNode(true) as HTMLElement;
    const scope = panel.querySelector<HTMLSelectElement>("[data-event-scope]")!;
    const errors = panel.querySelector<HTMLInputElement>("[data-event-errors]")!;
    const meta = panel.querySelector<HTMLElement>("[data-event-meta]")!;
    const empty = panel.querySelector<HTMLElement>("[data-event-empty]")!;
    const list = panel.querySelector<HTMLOListElement>("[data-event-list]")!;
    scope.value = mount.dataset.clusterLog || "all";
    let rendered = "";
    const update = (snapshot: Status) => {
      panel.hidden = !snapshot.role;
      if (!snapshot.role) return;
      const events = snapshot.status.events;
      meta.textContent = snapshot.error || (!snapshot.connected ? "等待与主节点同步" : "最近 80 条事件");
      const visible = (events || []).filter((event) =>
        (scope.value === "all" || !event.node || event.node === snapshot.node) &&
        (!errors.checked || event.level !== "info"));
      empty.hidden = visible.length !== 0;
      empty.textContent = !events && snapshot.connected ? "主节点版本尚未提供日志"
        : errors.checked ? "暂无异常事件" : "暂无集群事件";
      list.hidden = !empty.hidden;
      const signature = JSON.stringify(visible);
      if (signature === rendered) return;
      rendered = signature;
      list.replaceChildren(...visible.map((event) => eventRow(event)));
    };
    for (const control of [scope, errors]) {
      control.addEventListener("change", () => { if (current) update(current); });
    }
    mount.append(panel);
    eventPanels.push(update);
  }
}

function render(snapshot: Status) {
  const priorRole = current?.role;
  current = snapshot;
  updateDetections({ node: snapshot.node, total: snapshot.status.detected_open, sequence: snapshot.status.sequence });
  renderDiscoveryWork(snapshot);
  eventPanels.forEach((update) => update(snapshot));
  const status = snapshot.status;
  const joined = Boolean(snapshot.role);
  if (priorRole !== snapshot.role) {
    byId<HTMLDetailsElement>("manage").open = false;
    hideInvite();
  }
  const nav = byId("nav-indicator");
  nav.hidden = !joined;
  nav.classList.toggle("cluster-dot-offline", !snapshot.connected);
  byId("setup").hidden = joined;
  byId("monitor").hidden = !joined;
  if (!joined) return;

  byId("host-actions").hidden = snapshot.role !== "host";
  const pause = byId<HTMLButtonElement>("pause");
  pause.hidden = snapshot.role !== "host";
  pause.textContent = status.paused ? "继续协作" : "暂停协作";
  pause.dataset.paused = String(Boolean(status.paused));
  set("leave", snapshot.role === "host" ? "解散集群" : "退出集群");
  set("role", snapshot.role === "host" ? "主节点" : "工作节点");
  set("address", snapshot.address || "");
  set("link-state", snapshot.error || (snapshot.connected ? "同步正常" : "等待同步"));
  byId("link-state").dataset.connected = String(snapshot.connected && !snapshot.error);
  set("sync-time", snapshot.last_sync ? "上次同步 " + time(snapshot.last_sync) : "");
  byId("worker-note").hidden = snapshot.worker_ready;
  const counts = status.counts || {};
  const video = counts.video || {};
  const found = Object.values(video).reduce((a, b) => a + b, 0);
  const target = status.target;
  set("progress-value", target ? count(found) + " / " + count(target) : "—");
  const progress = byId<HTMLProgressElement>("progress");
  progress.max = target || 1;
  progress.value = found;
  set("video-pending", count(status.counts ? video.pending || 0 : undefined));
  set("video-running", count(status.counts ? video.leased || 0 : undefined));
  set("video-done", count(status.counts ? video.done || 0 : undefined));
  renderConversion("", status.conversion, video.done || 0);
  set("detected-open", count(status.detected_open));
  byId("video-failed").hidden = !video.failed;
  set("video-failed", "本轮视频检测失败 " + count(video.failed) + " 项");
  set("search-round", count(status.counts ? counts.search?.done || 0 : undefined));
  set("search-total", count(status.search_pages_total));
  set("search-history", status.search_pages_total === undefined
    ? (status.counts ? "主节点版本尚未提供" : "等待同步")
    : "自第 " + (status.search_history_start || 1) + " 轮起的成功检索页");
  const cooldown = status.cooldown_until || 0;
  const next = status.next_round || 0;
  const phase = status.paused ? "协作已暂停"
    : cooldown > (status.now || 0) ? "平台限流，暂停至 " + time(cooldown)
    : (phases[status.phase || ""] || "等待节点");
  set("phase", [status.round ? "第 " + status.round + " 轮" : "", phase,
    next && !status.paused ? "下一轮 " + time(next) : ""].filter(Boolean).join(" · "));

  renderNodes(snapshot, found);
}

function renderNodes(snapshot: Status, found: number) {
  const status = snapshot.status;
  const nodes = [...(status.nodes || [])].sort((a, b) => Number(b.host) - Number(a.host) ||
    Number(b.online) - Number(a.online) || a.name.localeCompare(b.name, "zh-CN"));
  const online = nodes.filter((node) => node.online).length;
  set("online", status.nodes ? online + " / " + nodes.length + " 在线" : "等待同步");
  const limit = status.concurrency_limit;
  const rule = status.phase === "collecting"
    ? (status.target && found >= status.target ? "已达收录目标，等待各节点交回已领取的检索。"
      : "达到收录目标或搜完可用结果页后，开始分配检测。")
    : status.two_stage && status.phase === "detecting" && !nodes.some((node) => node.online && node.work === 2)
      ? "等待支持两阶段协作且模型已就绪的节点。"
      : "就绪的空闲节点每次领取一个视频" + (limit ? "，最多 " + limit + " 个并行检测。" : "。");
  set("dispatch-rule", status.counts ? rule : "");
  byId("nodes-empty").hidden = nodes.length > 0;
  byId("nodes").replaceChildren(...nodes.map((node) => {
    const row = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = node.name;
    const role = document.createElement("span");
    role.className = "caption";
    role.textContent = (node.host ? "主节点" : "工作节点") + (node.id === snapshot.node ? "（本机）" : "");
    const state = document.createElement("span");
    state.className = "cluster-pill";
    state.dataset.state = !node.online ? "offline" : node.kind ? "working" : node.work ? "idle" : "sync";
    const canWork = status.two_stage && status.phase === "detecting" ? node.work === 2 : Boolean(node.work);
    state.textContent = !node.online ? "离线" : node.kind === "search" ? "检索中" : node.kind ? "检测中"
      : !canWork ? "仅同步" : status.paused ? "已暂停" : "空闲";
    row.append(name, role, state);
    return row;
  }));
}

function hideInvite() {
  const invite = byId<HTMLInputElement>("invite-text");
  invite.hidden = true;
  invite.value = "";
  set("show-invite", "显示邀请码");
  byId("show-invite").setAttribute("aria-expanded", "false");
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
    byId("link-state").dataset.connected = "false";
  } finally {
    refreshing = false;
  }
}

async function action(path: string, data?: object) {
  if (busy) return;
  busy = true;
  byId("root").setAttribute("aria-busy", "true");
  const buttons = [...byId("root").querySelectorAll<HTMLButtonElement>(
    "#cluster-setup button, #cluster-manage button, #cluster-pause",
  )];
  buttons.forEach((button) => button.disabled = true);
  try {
    const result = await fetchJSON<Status & { invite?: string }>("/api/cluster/" + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: data ? JSON.stringify(data) : undefined,
    });
    if (result.invite) {
      if (byId<HTMLDetailsElement>("manage").open) {
        const invite = byId<HTMLInputElement>("invite-text");
        invite.value = result.invite;
        invite.hidden = false;
        invite.select();
        set("show-invite", "隐藏邀请码");
        byId("show-invite").setAttribute("aria-expanded", "true");
      }
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
  mountEventPanels();
  updateDetections = mountDetectedActivities(byId);
  for (const mode of ["host", "join"]) {
    byId<HTMLFormElement>(mode + "-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.currentTarget as HTMLFormElement));
      void action(mode, data);
    });
  }
  byId("pause").addEventListener("click", () => void action("pause?paused=" + String(byId("pause").dataset.paused !== "true")));
  byId("show-invite").addEventListener("click", () => {
    if (byId("invite-text").hidden) void action("invite");
    else hideInvite();
  });
  byId("manage").addEventListener("toggle", (event) => {
    if (!(event.currentTarget as HTMLDetailsElement).open) hideInvite();
  });
  byId("leave").addEventListener("click", () => {
    if (current?.role === "host" && !window.confirm("解散后所有节点将断开。确定解散集群？")) return;
    void action("leave");
  });
  document.querySelectorAll('[data-section="logs"], [data-section="sources"], [data-source-view="discovery"]')
    .forEach((button) => button.addEventListener("click", () => void refresh()));
  const section = byId("root").closest(".view-section");
  if (section) {
    const observer = new MutationObserver(() => {
      if (section.classList.contains("active")) void refresh();
      else byId<HTMLDetailsElement>("manage").open = false;
    });
    observer.observe(section, { attributes: true, attributeFilter: ["class"] });
  }
  void refresh();
  window.setInterval(() => { if (!document.hidden) void refresh(); }, 5000);
}
