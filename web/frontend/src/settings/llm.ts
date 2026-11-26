import { isLoggedIn, renderAccountViews, renderOnboardingPanel } from "../account/index";
import { fetchJSON, type FetchJSONOptions } from "../api/client";
import { llmActionFeedback } from "../dom";
import { openAppConfirm } from "../shell/confirm";
import { setInlineFeedback } from "../shell/toast";
import { state } from "../state";
import { clearSaveDirty, flashButtonSuccess, markSaveDirty, setButtonLoading } from "../utils/motion";
import { escapeHtml } from "../utils/text";

interface LlmProfile {
  id: string;
  name: string;
  api_key_hint: string;
  base_url: string;
  model_name: string;
  test_passed: boolean;
  exhausted: boolean;
}

interface LlmSettings {
  configured: boolean;
  test_passed: boolean;
  base_url: string;
  model_name: string;
  api_key_hint: string;
  active_profile_id: string | null;
  environment_override: boolean;
  profiles: LlmProfile[];
  saved_profile_id?: string;
}

interface LlmSettingsResponse {
  llm: LlmSettings;
  setup_complete: boolean;
  message?: string;
}

const getButton = (id: string) => document.getElementById(id) as HTMLButtonElement | null;
const getInput = (id: string) => document.getElementById(id) as HTMLInputElement | null;
const nameInput = getInput("llm-profile-name");
const baseInput = getInput("llm-base-url-input");
const modelInput = getInput("llm-model-name-input");
const keyInput = getInput("llm-api-key-input");
const keyToggle = getButton("llm-api-key-toggle");
const saveButton = getButton("save-llm-settings");
const testButton = getButton("test-llm-settings");
const deleteButton = getButton("llm-profile-delete");
const refreshButton = getButton("refresh-llm-settings");
const addButton = getButton("llm-profile-add");
const list = document.getElementById("llm-profile-list");
const inputs = [nameInput, baseInput, modelInput, keyInput];

let current: LlmSettings | undefined;
let selectedId: string | null | undefined;
let busy = false;

function showKey(show: boolean) {
  if (!keyInput || !keyToggle) return;
  keyInput.type = show ? "text" : "password";
  keyToggle.textContent = show ? "隐藏" : "显示";
  keyToggle.setAttribute("aria-label", show ? "隐藏 API Key" : "显示 API Key");
  keyToggle.setAttribute("aria-pressed", String(show));
}

function renderProfileList(profiles: LlmProfile[]) {
  if (!list) return;
  list.hidden = profiles.length === 0;
  list.innerHTML = profiles.map((profile, index) => {
    const name = escapeHtml(profile.name);
    const active = profile.id === current?.active_profile_id;
    const metadata = [profile.model_name, profile.api_key_hint].filter(Boolean).map(escapeHtml).join(" · ");
    const status = profile.exhausted
      ? '<span class="llm-profile-exhausted">额度已用尽</span>'
      : active
        ? '<span class="llm-profile-active">当前优先使用</span>'
        : profile.test_passed ? "已通过测试" : "未测试";
    return `
      <li class="llm-profile-row" data-profile-id="${escapeHtml(profile.id)}">
        <button type="button" class="llm-profile-edit" data-profile-action="edit" aria-pressed="${profile.id === selectedId}">
          <span class="llm-profile-position">${index + 1}</span>
          <span class="llm-profile-details"><strong>${name}</strong>
            <span class="caption">${metadata} · <span class="llm-profile-status">${status}</span></span>
          </span>
        </button>
        <div class="llm-profile-actions">
          <button type="button" class="btn btn-secondary btn-compact" data-profile-action="up" aria-label="上移 ${name}" ${index === 0 ? "disabled" : ""}>上移</button>
          <button type="button" class="btn btn-secondary btn-compact" data-profile-action="down" aria-label="下移 ${name}" ${index === profiles.length - 1 ? "disabled" : ""}>下移</button>
        </div>
      </li>`;
  }).join("");
}

export function renderLlmSettings(llm: LlmSettings, resetEditor = true) {
  current = llm;
  const profiles = llm?.profiles || [];
  if (selectedId === undefined || (selectedId !== null && !profiles.some((profile) => profile.id === selectedId))) {
    selectedId = llm?.active_profile_id || profiles[0]?.id || null;
    resetEditor = true;
  }
  const selected = profiles.find((profile) => profile.id === selectedId);
  renderProfileList(profiles);
  if (resetEditor) {
    const source = selected || (profiles.length === 0 ? llm : undefined);
    if (nameInput) nameInput.value = selected?.name || "";
    if (baseInput) baseInput.value = source?.base_url || "";
    if (modelInput) modelInput.value = source?.model_name || "";
    if (keyInput) {
      keyInput.value = "";
      keyInput.placeholder = source?.api_key_hint ? "留空保留已保存的 Key" : "请输入 API Key";
      showKey(false);
    }
    clearSaveDirty(saveButton);
  }

  const keyHint = document.getElementById("llm-api-key-hint");
  const baseHint = document.getElementById("llm-base-url-hint");
  const status = document.getElementById("llm-settings-status");
  if (keyHint) keyHint.textContent = selected?.api_key_hint ? `已保存 ${selected.api_key_hint}` : "";
  if (baseHint) baseHint.textContent = selected?.base_url ? `已保存 ${selected.base_url}` : "";
  if (status) {
    if (!isLoggedIn()) status.textContent = "登录后可管理 API 方案";
    else if (!profiles.length) status.textContent = "保存第一套方案后，请测试连接";
    else if (profiles.every((profile) => profile.exhausted)) status.textContent = "所有方案的额度均已用尽。充值后可测试连接恢复使用";
    else if (selected?.exhausted) status.textContent = "额度已用尽，充值后可测试连接恢复使用";
    else if (!llm?.test_passed) status.textContent = "请至少测试通过一套方案，完成 LLM 设置";
    else status.textContent = selected ? (selected.test_passed ? "连接测试已通过" : "此方案尚未测试") : "新增 API 方案";
  }
  document.getElementById("llm-env-override")?.toggleAttribute("hidden", !llm?.environment_override);
  if (deleteButton) deleteButton.hidden = !selected;
  if (testButton) testButton.disabled = !selected || busy;
  renderOnboardingPanel();
}

function applyResponse(result: LlmSettingsResponse, resetEditor: boolean) {
  state.settings = { ...state.settings, llm: result.llm, setup_complete: result.setup_complete };
  renderLlmSettings(result.llm, resetEditor);
  if (state.account) renderAccountViews(state.account);
}

function requestSettings(path = "", options?: FetchJSONOptions) {
  return fetchJSON<LlmSettingsResponse>(`/api/settings/llm${path}`, options);
}

function jsonRequest(method: string, body: object): FetchJSONOptions {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function formValues() {
  return {
    profile_id: selectedId ?? null,
    api_key: keyInput?.value || "",
    base_url: baseInput?.value || "",
    model_name: modelInput?.value || "",
  };
}

async function runAction(button: HTMLButtonElement | null, loading: string, action: () => Promise<string>) {
  if (busy) return;
  busy = true;
  for (const input of inputs) if (input) input.readOnly = true;
  setButtonLoading(button, true, { label: loading });
  setInlineFeedback(llmActionFeedback, "", "info");
  try {
    const message = await action();
    setInlineFeedback(llmActionFeedback, message, "success");
  } catch (error) {
    setInlineFeedback(llmActionFeedback, error instanceof Error ? error.message : String(error), "error");
  } finally {
    busy = false;
    for (const input of inputs) if (input) input.readOnly = false;
    setButtonLoading(button, false);
    if (testButton) testButton.disabled = !selectedId;
  }
}

function canManage() {
  if (busy) return false;
  if (isLoggedIn()) return true;
  setInlineFeedback(llmActionFeedback, "请先扫码登录", "info", { autoHide: false });
  return false;
}

function editProfile(id: string | null) {
  if (!current || selectedId === id) return true;
  if (saveButton?.classList.contains("is-dirty") && !window.confirm("放弃尚未保存的方案修改？")) return false;
  selectedId = id;
  renderLlmSettings(current);
  setInlineFeedback(llmActionFeedback, "", "info");
  return true;
}

export function bindLlmSettings() {
  keyToggle?.addEventListener("click", () => showKey(keyInput?.type === "password"));
  for (const input of inputs) input?.addEventListener("input", () => markSaveDirty(saveButton));

  addButton?.addEventListener("click", () => {
    if (canManage() && editProfile(null)) nameInput?.focus();
  });
  list?.addEventListener("click", (event) => {
    if (busy || !(event.target instanceof Element)) return;
    const button = event.target.closest<HTMLButtonElement>("button[data-profile-action]");
    const id = button?.closest<HTMLElement>("[data-profile-id]")?.dataset.profileId;
    if (!button || !id) return;
    const action = button.dataset.profileAction;
    if (action === "edit") {
      if (editProfile(id)) nameInput?.focus();
      return;
    }
    if (!canManage() || !current) return;
    const ids = current.profiles.map((profile) => profile.id);
    const from = ids.indexOf(id);
    const to = from + (action === "up" ? -1 : 1);
    if (from < 0 || to < 0 || to >= ids.length) return;
    [ids[from], ids[to]] = [ids[to], ids[from]];
    void runAction(button, "调整中…", async () => {
      applyResponse(await requestSettings("/profiles/order", jsonRequest("PUT", { profile_ids: ids })), false);
      list?.querySelector<HTMLButtonElement>(`[data-profile-id="${CSS.escape(id)}"] .llm-profile-edit`)?.focus();
      return "已更新方案使用顺序";
    });
  });
  refreshButton?.addEventListener("click", () => void runAction(refreshButton, "刷新中…", async () => {
    applyResponse(await requestSettings(), !saveButton?.classList.contains("is-dirty"));
    return "已刷新方案";
  }));
  saveButton?.addEventListener("click", () => {
    if (!canManage()) return;
    void runAction(saveButton, "保存中…", async () => {
      const payload = { ...formValues(), name: nameInput?.value || "" };
      const result = await requestSettings("/profiles", jsonRequest("POST", payload));
      selectedId = result.llm.saved_profile_id;
      applyResponse(result, true);
      flashButtonSuccess(saveButton);
      return "已保存方案";
    });
  });
  testButton?.addEventListener("click", () => {
    if (!canManage()) return;
    if (saveButton?.classList.contains("is-dirty")) {
      setInlineFeedback(llmActionFeedback, "请先保存方案，再测试连接", "info");
      return;
    }
    void runAction(testButton, "测试中…", async () => {
      try {
        const result = await requestSettings("/test", { ...jsonRequest("POST", formValues()), timeoutMs: 100000 });
        applyResponse(result, true);
        return result.message || "连接测试已通过";
      } catch (error) {
        await requestSettings().then((result) => applyResponse(result, false), () => {});
        throw error;
      }
    });
  });
  deleteButton?.addEventListener("click", async () => {
    if (!canManage() || !selectedId) return;
    const id = selectedId;
    const name = current?.profiles.find((profile) => profile.id === id)?.name;
    const confirmed = await openAppConfirm({
      title: `删除「${name}」？`,
      desc: "删除后，Binggo 将从使用顺序中移除这套方案及其保存的 API Key。",
      confirmLabel: "删除方案",
      danger: true,
    });
    if (!confirmed) return;
    await runAction(deleteButton, "删除中…", async () => {
      const result = await requestSettings(`/profiles/${encodeURIComponent(id)}`, { method: "DELETE" });
      selectedId = undefined;
      applyResponse(result, true);
      return "已删除方案";
    });
  });
}
