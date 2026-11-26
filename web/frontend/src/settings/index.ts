// @ts-nocheck
/* eslint-disable */
/** Migrated from web/static/app.js — logic preserved. */

import { state } from "../state";
import { fetchJSON } from "../api/client";
import { isLoggedIn } from "../account/index";
import { participateTextFeedback } from "../dom";
import { setInlineFeedback, showToast } from "../shell/toast";
import { clearSaveDirty, flashButtonSuccess, markSaveDirty, prefersReducedMotion, setButtonLoading } from "../utils/motion";
import { renderLlmSettings } from "./llm";

export async function loadSettings() {
  const settings = await fetchJSON("/api/settings");
  state.settings = settings;
  renderParticipateSettings(settings);
  renderLlmSettings(settings.llm);
  return settings;
}

export function getParticipateTextDefaults(settings) {
  return {
    custom: settings?.default_participate_text || "好运连连！",
    fallback: settings?.default_participate_fallback_text || settings?.default_participate_text || "好运连连！",
  };
}

export function getParticipateTextForMode(settings, mode) {
  const defaults = getParticipateTextDefaults(settings || {});
  if (mode === "random_comment") {
    return settings?.participate_fallback_text || defaults.fallback;
  }
  return settings?.participate_text || defaults.custom;
}

export function updateParticipateTextUI(mode) {
  const isRandom = mode === "random_comment";
  const label = document.getElementById("participate-text-label");
  const hint = document.getElementById("participate-text-hint");
  const note = document.getElementById("participate-random-note");
  const saveBtn = document.getElementById("save-participate-text");
  const resetBtn = document.getElementById("reset-participate-text");
  const desc = document.getElementById("participate-settings-desc");
  const fields = document.getElementById("participate-text-fields");
  if (label) label.textContent = isRandom ? "兜底文案" : "当前文案";
  if (hint) {
    hint.textContent = isRandom ? "评论不足时使用 · 最多 233 字" : "最多 233 字";
  }
  if (note) note.hidden = !isRandom;
  if (fields) fields.classList.toggle("participate-text-fields--random", isRandom);
  if (saveBtn) saveBtn.textContent = isRandom ? "保存兜底文案" : "保存文案";
  if (resetBtn) resetBtn.textContent = isRandom ? "恢复默认兜底" : "恢复默认";
  if (desc) {
    desc.innerHTML = isRandom
      ? "参与时从活动评论区<strong>第 6～65 条</strong>中随机抽取一条，作为转发与评论内容。"
      : '用于转发与评论，建议格式：<strong>@好友昵称 + 一句话</strong>。例如 <code>@小明 好运连连！</code>';
  }
}

export function renderParticipateSettings(settings) {
  const mode =
    settings?.participate_text_mode || settings?.default_participate_text_mode || "custom";
  const defaults = getParticipateTextDefaults(settings);
  const input = document.getElementById("participate-text-input");
  if (input) {
    input.value = getParticipateTextForMode(settings, mode);
    input.placeholder =
      mode === "random_comment" ? defaults.fallback : defaults.custom;
  }
  document.querySelectorAll("[data-participate-mode]").forEach((button) => {
    const active = button.dataset.participateMode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  updateParticipateTextUI(mode);
}

export async function saveParticipateTextMode(mode) {
  if (!isLoggedIn()) {
    showToast("请先扫码登录", "info", "登录后才能修改参与文案模式");
    return false;
  }
  const result = await fetchJSON("/api/settings/participate-text", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ participate_text_mode: mode }),
  });
  if (state.settings) {
    if (result.participate_text_mode) {
      state.settings.participate_text_mode = result.participate_text_mode;
    }
    if (result.participate_text) {
      state.settings.participate_text = result.participate_text;
    }
    if (result.participate_fallback_text) {
      state.settings.participate_fallback_text = result.participate_fallback_text;
    }
  }
  renderParticipateSettings(state.settings || { participate_text_mode: result.participate_text_mode || mode });
  clearSaveDirty(document.getElementById("save-participate-text"));
  const label = mode === "random_comment" ? "随机借用评论" : "自定义文案";
  setInlineFeedback(participateTextFeedback, `已切换为「${label}」`, "success");
  return true;
}

export function bindParticipateSettings() {
  document.querySelectorAll("[data-participate-mode]").forEach((button) => {
    if (button.dataset.bound === "true") return;
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      if (button.classList.contains("active")) return;
      const mode = button.dataset.participateMode || "custom";
      const group = button.closest(".participate-mode-group");
      const originalDisabled = button.disabled;
      button.disabled = true;
      group?.classList.add("is-switching");
      button.classList.add("is-pending");
      try {
        const ok = await saveParticipateTextMode(mode);
        if (ok && !prefersReducedMotion()) {
          const active = document.querySelector("[data-participate-mode].active");
          active?.classList.add("is-just-switched");
          window.setTimeout(() => active?.classList.remove("is-just-switched"), 720);
        }
      } catch (error) {
        setInlineFeedback(participateTextFeedback, String(error.message || error), "error");
      } finally {
        group?.classList.remove("is-switching");
        button.classList.remove("is-pending");
        button.disabled = originalDisabled;
      }
    });
  });
}

export function bindSettingsDirtyTracking() {
  const participateInput = document.getElementById("participate-text-input");
  const participateSave = document.getElementById("save-participate-text");
  if (participateInput && participateInput.dataset.dirtyBound !== "true") {
    participateInput.dataset.dirtyBound = "true";
    participateInput.addEventListener("input", () => markSaveDirty(participateSave));
  }
}

export async function saveParticipateText() {
  if (!isLoggedIn()) {
    setInlineFeedback(participateTextFeedback, "请先扫码登录后再保存", "info", { autoHide: false });
    return;
  }
  const input = document.getElementById("participate-text-input");
  const button = document.getElementById("save-participate-text");
  const value = input?.value?.trim() || "";
  setButtonLoading(button, true, { label: "保存中…" });
  const mode = state.settings?.participate_text_mode || "custom";
  const payload =
    mode === "random_comment"
      ? { participate_fallback_text: value }
      : { participate_text: value };
  setInlineFeedback(participateTextFeedback, "", "info");
  try {
    const result = await fetchJSON("/api/settings/participate-text", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (input) {
      input.value = getParticipateTextForMode(
        {
          ...(state.settings || {}),
          participate_text: result.participate_text,
          participate_fallback_text: result.participate_fallback_text,
        },
        mode
      );
    }
    if (state.settings) {
      if (result.participate_text) state.settings.participate_text = result.participate_text;
      if (result.participate_fallback_text) {
        state.settings.participate_fallback_text = result.participate_fallback_text;
      }
    }
    const savedText =
      mode === "random_comment"
        ? result.participate_fallback_text || value
        : result.participate_text || value;
    setInlineFeedback(
      participateTextFeedback,
      mode === "random_comment" ? `兜底文案已保存：${savedText}` : `参与文案已保存：${savedText}`,
      "success"
    );
    setButtonLoading(button, false);
    clearSaveDirty(button);
    flashButtonSuccess(button);
  } catch (error) {
    setButtonLoading(button, false);
    setInlineFeedback(participateTextFeedback, String(error.message || error), "error");
    throw error;
  }
}

export async function resetParticipateText() {
  if (!isLoggedIn()) {
    setInlineFeedback(participateTextFeedback, "请先扫码登录后再恢复", "info", { autoHide: false });
    return;
  }
  const mode = state.settings?.participate_text_mode || "custom";
  const defaults = getParticipateTextDefaults(state.settings || {});
  const payload =
    mode === "random_comment"
      ? { participate_fallback_text: defaults.fallback }
      : { participate_text: defaults.custom };
  setInlineFeedback(participateTextFeedback, "", "info");
  try {
    const result = await fetchJSON("/api/settings/participate-text", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const input = document.getElementById("participate-text-input");
    if (input) {
      input.value = getParticipateTextForMode(
        {
          ...(state.settings || {}),
          participate_text: result.participate_text,
          participate_fallback_text: result.participate_fallback_text,
        },
        mode
      );
    }
    if (state.settings) {
      if (result.participate_text) state.settings.participate_text = result.participate_text;
      if (result.participate_fallback_text) {
        state.settings.participate_fallback_text = result.participate_fallback_text;
      }
    }
    const restoredText =
      mode === "random_comment"
        ? result.participate_fallback_text || defaults.fallback
        : result.participate_text || defaults.custom;
    setInlineFeedback(
      participateTextFeedback,
      mode === "random_comment" ? `已恢复默认兜底：${restoredText}` : `已恢复默认文案：${restoredText}`,
      "success"
    );
  } catch (error) {
    setInlineFeedback(participateTextFeedback, String(error.message || error), "error");
    throw error;
  }
}
