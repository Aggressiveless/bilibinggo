import { fetchJSON } from "../api/client";
import "./styles.css";

type Currency = "CNY" | "USD";
type Metric = "bilibili_requests" | "llm_requests" | "total_tokens" | "cost";
type Cost = { reported: number; estimated: number };
type Totals = {
  bilibili_requests: number;
  llm_requests: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  missing_token_calls: number;
  priced_calls: number;
  unpriced_calls: number;
  costs: Record<Currency, Cost>;
};
type Day = Totals & { day: string };
type Pricing = {
  model_key: string;
  model: string;
  host: string | null;
  currency: Currency;
  input_rate: string | null;
  cached_input_rate: string | null;
  output_rate: string | null;
};
type Usage = { recorded_from: string | null; totals: Totals; daily: Day[]; pricing: Pricing | null };

const labels: Record<Metric, string> = {
  bilibili_requests: "B站请求",
  llm_requests: "模型请求",
  total_tokens: "模型 Token",
  cost: "模型花费",
};
const currencies: Currency[] = ["CNY", "USD"];
const integer = new Intl.NumberFormat("zh-CN");
const money = (value: number, currency: Currency) => {
  if (value > 0 && value < 0.000001) return currency === "CNY" ? "< ¥0.000001" : "< US$0.000001";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 6,
  }).format(value);
};
const costValue = (row: Totals, currency: Currency) => row.costs[currency].reported + row.costs[currency].estimated;

export function bindUsageOverview() {
  const panel = document.getElementById("usage-panel");
  if (!panel) return;
  const period = panel.querySelector<HTMLElement>("#usage-period")!;
  const range = panel.querySelector<HTMLSelectElement>("#usage-range")!;
  const cards = Array.from(panel.querySelectorAll<HTMLButtonElement>("[data-usage-metric]"));
  const chartTitle = panel.querySelector<HTMLElement>("#usage-chart-title")!;
  const chart = panel.querySelector<HTMLElement>("#usage-chart-body")!;
  const currencyLabel = panel.querySelector<HTMLElement>("#usage-chart-currency-label")!;
  const currencyInput = panel.querySelector<HTMLSelectElement>("#usage-chart-currency")!;
  const pricingModel = panel.querySelector<HTMLElement>("#usage-pricing-model")!;
  const pricingForm = panel.querySelector<HTMLFormElement>("#usage-pricing-form")!;
  const pricingStatus = panel.querySelector<HTMLElement>("#usage-pricing-status")!;
  const pricingButton = pricingForm.querySelector<HTMLButtonElement>("button[type=submit]")!;
  let snapshot: Usage | undefined;
  let metric: Metric = "bilibili_requests";
  let currency: Currency = "CNY";
  let selectedDate = "";
  let lastResponse = "";
  let pricingSignature = "";
  let loadId = 0;

  function describeDay(day: Day): string {
    if (metric === "cost") {
      const cost = day.costs[currency];
      const unknown = day.unpriced_calls ? ` · ${integer.format(day.unpriced_calls)} 次缺少计费数据` : "";
      return `${day.day}　返回金额 ${money(cost.reported, currency)} · 估算 ${money(cost.estimated, currency)}${unknown}`;
    }
    if (metric === "total_tokens") {
      const unknown = day.missing_token_calls ? ` · ${integer.format(day.missing_token_calls)} 次用量不完整` : "";
      return `${day.day}　${integer.format(day.total_tokens)} Token · 输入 ${integer.format(day.input_tokens)} · 输出 ${integer.format(day.output_tokens)}${unknown}`;
    }
    return `${day.day}　${integer.format(day[metric])} 次`;
  }

  function renderChart() {
    chartTitle.textContent = `${labels[metric]} · 每日趋势`;
    currencyLabel.hidden = metric !== "cost";
    if (!snapshot) return;
    chart.setAttribute("aria-busy", "false");
    const days = snapshot.daily;
    const values = days.map((day) => metric === "cost" ? costValue(day, currency) : day[metric]);
    const maximum = Math.max(0, ...values);
    const focused = chart.contains(document.activeElement);
    chart.replaceChildren();
    const content = document.createElement("div");
    content.className = "usage-plot";
    content.dataset.metric = metric;
    const axis = document.createElement("div");
    axis.className = "usage-axis";
    const top = document.createElement("span");
    top.textContent = metric === "cost" ? money(maximum, currency) : integer.format(maximum);
    const zero = document.createElement("span");
    zero.textContent = "0";
    axis.append(top, zero);
    const scroll = document.createElement("div");
    scroll.className = "usage-plot-scroll";
    scroll.style.setProperty("--usage-days", String(days.length));
    const bars = document.createElement("div");
    bars.className = "usage-bars";
    bars.setAttribute("role", "group");
    bars.setAttribute("aria-label", "每日数据，使用左右方向键选择日期");
    const footer = document.createElement("p");
    footer.className = "caption usage-day-detail";
    footer.setAttribute("aria-live", "polite");
    const buttons: HTMLButtonElement[] = [];
    function selectDay(index: number, focus = false) {
      selectedDate = days[index].day;
      footer.textContent = describeDay(days[index]);
      buttons.forEach((button, position) => {
        button.tabIndex = index === position ? 0 : -1;
        button.setAttribute("aria-pressed", String(index === position));
      });
      if (focus) buttons[index].focus();
    }
    days.forEach((day, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "usage-bar";
      button.title = describeDay(day);
      button.setAttribute("aria-label", button.title);
      button.style.setProperty("--usage-height", `${maximum ? 100 * values[index] / maximum : 0}%`);
      if (values[index] === 0) button.dataset.empty = "";
      button.addEventListener("click", () => selectDay(index));
      button.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const next = index + (event.key === "ArrowRight" ? 1 : -1);
        if (next >= 0 && next < days.length) selectDay(next, true);
      });
      buttons.push(button);
      bars.append(button);
    });
    const dates = document.createElement("div");
    dates.className = "usage-dates";
    for (const index of [0, Math.floor((days.length - 1) / 2), days.length - 1]) {
      const label = document.createElement("span");
      label.textContent = days[index].day.slice(5);
      dates.append(label);
    }
    const selected = days.findIndex((day) => day.day === selectedDate);
    scroll.append(bars, dates);
    content.append(axis, scroll);
    chart.append(content, footer);
    selectDay(selected < 0 ? days.length - 1 : selected, focused);
  }

  function renderTotals(data: Usage) {
    period.textContent = data.recorded_from
      ? `自 ${data.recorded_from} 起累计 · 趋势按本机日期`
      : "暂无本机记录 · 从本版开始统计";
    for (const card of cards) {
      const key = card.dataset.usageMetric as Metric;
      const value = card.querySelector<HTMLElement>("[data-usage-total]")!;
      const note = card.querySelector<HTMLElement>("[data-usage-note]");
      if (key === "total_tokens") {
        const totals = data.totals;
        value.textContent = integer.format(totals.total_tokens);
        note!.textContent = totals.missing_token_calls
          ? `${integer.format(totals.missing_token_calls)} 次返回的用量不完整`
          : `输入 ${integer.format(totals.input_tokens)} · 输出 ${integer.format(totals.output_tokens)}`;
      } else if (key === "cost") {
        const totals = data.totals;
        const amounts = currencies.filter((unit) => costValue(totals, unit) > 0)
          .map((unit) => money(costValue(totals, unit), unit));
        value.textContent = amounts.join(" / ") || (totals.priced_calls ? "0" : "—");
        const estimated = currencies.some((unit) => totals.costs[unit].estimated > 0);
        const unknown = totals.unpriced_calls ? `${integer.format(totals.unpriced_calls)} 次缺计费数据` : "";
        note!.textContent = [estimated ? "含估算" : "", unknown].filter(Boolean).join(" · ") || "累计已计金额";
      } else {
        value.textContent = integer.format(data.totals[key]);
      }
    }
  }

  function renderPricing(pricing: Pricing | null) {
    const signature = JSON.stringify(pricing);
    if (signature === pricingSignature) return;
    pricingSignature = signature;
    pricingForm.hidden = !pricing;
    pricingModel.textContent = pricing
      ? [pricing.model, pricing.host].filter(Boolean).join(" · ")
      : "请先完成概览下方的 LLM 配置";
    if (!pricing) return;
    for (const key of ["currency", "input_rate", "cached_input_rate", "output_rate"] as const) {
      const input = pricingForm.elements.namedItem(key) as HTMLInputElement | HTMLSelectElement;
      input.value = pricing[key] ?? "";
    }
  }

  async function refresh() {
    const id = ++loadId;
    try {
      const data = await fetchJSON<Usage>(`/api/usage?days=${Number(range.value)}`, { timeoutMs: 5000 });
      if (id !== loadId) return;
      const encoded = JSON.stringify(data);
      if (encoded === lastResponse) {
        renderTotals(data);
        return;
      }
      lastResponse = encoded;
      if (!snapshot && (data.pricing?.currency === "USD" || (costValue(data.totals, "USD") > 0 && costValue(data.totals, "CNY") === 0))) {
        currencyInput.value = currency = "USD";
      }
      snapshot = data;
      renderTotals(data);
      renderPricing(data.pricing);
      renderChart();
    } catch {
      if (id !== loadId) return;
      chart.setAttribute("aria-busy", "false");
      period.textContent = snapshot ? "统计刷新失败，当前显示上次结果" : "暂时无法读取统计";
    }
  }

  range.addEventListener("change", () => void refresh());
  currencyInput.addEventListener("change", () => {
    currency = currencyInput.value as Currency;
    renderChart();
  });
  for (const card of cards) {
    card.addEventListener("click", () => {
      metric = card.dataset.usageMetric as Metric;
      for (const other of cards) other.setAttribute("aria-pressed", String(card === other));
      renderChart();
    });
  }
  pricingForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!snapshot?.pricing) return;
    const fields = Object.fromEntries(new FormData(pricingForm));
    pricingButton.disabled = true;
    pricingStatus.textContent = "保存中…";
    try {
      await fetchJSON("/api/usage/pricing", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...fields, model_key: snapshot.pricing.model_key,
          cached_input_rate: fields.cached_input_rate || null,
        }),
      });
      pricingStatus.textContent = "已保存";
      await refresh();
    } catch (error) {
      pricingStatus.textContent = error instanceof Error ? error.message : "保存失败";
    } finally {
      pricingButton.disabled = false;
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && panel.closest(".view-section.active")) void refresh();
  });
  window.setInterval(() => {
    if (!document.hidden && panel.closest(".view-section.active")) void refresh();
  }, 15000);
  void refresh();
}
