import type { Order } from "./client";

export interface HistoryRecord {
  id: string;
  plan: string;
  country: string;
  amount?: string | null;
  currency?: string | null;
  status: string;
  error?: string | null;
  email?: string | null;
  card_last4?: string | null;
  at: number;
}

const KEY = "gss.history.v1";
const MAX = 50;

export const SUCCESS_STATUSES = ["delivered", "active"];
export const FAILED_STATUSES = ["failed", "rejected"];

export function isSettled(status: string): boolean {
  return SUCCESS_STATUSES.includes(status) || FAILED_STATUSES.includes(status);
}

// 改名前的旧键，只读一次用来保留已有的开通记录。
const LEGACY_KEY = "dcp.history.v1";

export function normalizeHistoryError(error: string | null | undefined): string | null | undefined {
  if (typeof error !== "string") return error;
  const marker = "ChatGPT 未批准这次支付（blocked）。";
  const start = error.indexOf(marker);
  if (start < 0) return error;
  const suffix = error.slice(start + marker.length);
  if (!suffix.startsWith("不是发卡行拒卡。") && !suffix.startsWith("不是发卡行拒卡，是账号/地区风控。")) return error;
  // Replace only the known application-generated legacy explanation, not upstream details.
  const reason = error.slice(0, start).trim().replace(/[。\s]+$/, "");
  return reason
    ? `ChatGPT 未批准本次结账（blocked）。上游信息：${reason}。尚未确认扣款，请先核对账单。`
    : "ChatGPT 未批准本次结账（blocked），上游未提供明确原因。尚未确认扣款，请先核对账单。";
}

export function loadHistory(): HistoryRecord[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) ?? localStorage.getItem(LEGACY_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    const normalized = (parsed as HistoryRecord[]).map((item) => ({ ...item, error: normalizeHistoryError(item.error) }));
    if (normalized.some((item, i) => item.error !== parsed[i].error)) {
      try { localStorage.setItem(KEY, JSON.stringify(normalized)); } catch { /* display normalized data even when storage is full */ }
    }
    return normalized;
  } catch {
    return [];
  }
}

export function recordOrder(order: Order): HistoryRecord[] {
  const previous = loadHistory();
  const existing = previous.find((item) => item.id === order.id);
  const last4 = order.card_last4 || existing?.card_last4;
  const next: HistoryRecord = {
    id: order.id,
    plan: order.plan,
    country: order.billing_country,
    amount: order.amount,
    currency: order.currency,
    status: order.status,
    error: normalizeHistoryError(order.last_error),
    email: order.session_email || existing?.email || order.billing?.email || null,
    card_last4: last4 && /^\d{4}$/.test(last4) ? last4 : null,
    at: Date.now(),
  };
  const list = [next, ...previous.filter((item) => item.id !== order.id)].slice(0, MAX);
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch {
    /* storage unavailable: records stay in memory only */
  }
  return list;
}
