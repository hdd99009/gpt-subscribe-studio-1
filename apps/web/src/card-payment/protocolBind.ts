import type { BindAndPayProtocolResult, BillingInput, ProtocolPayResult, TeamCardPayload } from "../api/client";

export const PROTOCOL_STAGE_LABELS = {
  setup: "支付方式准备",
  sync: "付款状态同步",
  checkout: "结账与金额确认",
  payment: "支付确认",
};

/** Payment or subscription wording is informational; only the paid state is final. */
export function protocolIsPaid(result: ProtocolPayResult): boolean {
  return result.state === "paid";
}

export function protocolStateLabel(state: string): string {
  if (state === "integration_error") return "商户集成接口未启用";
  if (state === "request_rejected") return "Stripe 已明确拒绝当前请求";
  return state;
}

/** A further POST is explicit and is only offered before starting or after an updated quote. */
export function protocolPostReady(result: BindAndPayProtocolResult | null): boolean {
  return result === null || result.state === "not_started" || (result.state === "pending" && result.stage === "checkout");
}

export function protocolBillingError(billing: BillingInput): string | null {
  if (!billing.name.trim() || !billing.line1.trim() || !billing.city.trim() || !/^[A-Z]{2}$/.test(billing.country)) {
    return "请填写持卡人的真实姓名、街道、城市和两位账单国家代码。";
  }
  if (!(billing.email || "").trim()) return "请填写账单邮箱。";
  if (["US", "CA"].includes(billing.country) && (!billing.state.trim() || !billing.postal_code.trim())) {
    return "请填写账单地址的州或省和邮编。";
  }
  return null;
}

export function protocolCardError(card: TeamCardPayload): string | null {
  if (!/^\d{12,19}$/.test(card.number.replace(/[\s-]/g, ""))) return "请填写有效卡号。";
  if (!/^\d{3,4}$/.test(card.cvc)) return "请填写 3 至 4 位 CVC。";
  if (!/^\d{1,2}$/.test(card.exp_month) || Number(card.exp_month) < 1 || Number(card.exp_month) > 12) return "请填写 01 至 12 的到期月份。";
  if (!/^(?:\d{2}|\d{4})$/.test(card.exp_year)) return "请填写两位或四位到期年份。";
  return null;
}

export function parseProtocolCardText(text: string): TeamCardPayload {
  const normalized = text.normalize("NFKC").trim();
  const numbers = [...normalized.matchAll(/(?:^|[;\r\n])\s*卡号\s*:\s*([\d -]+)\s*(?=[;\r\n]|$)/g)];
  const codes = [...normalized.matchAll(/(?:^|[;\r\n])\s*(?:CVV|CVC)\s*:?\s*(\d+)\s*(?=[;\r\n]|$)/gi)];
  const expiries = [...normalized.matchAll(/(?:^|[;\r\n])\s*有效期\s*:\s*(\d{1,2})\s*\/\s*(\d{2}|\d{4})\s*(?=[;\r\n]|$)/g)];
  if (numbers.length !== 1 || codes.length !== 1 || expiries.length !== 1) {
    throw new Error("请填写一张卡的完整信息，格式为“卡号:完整卡号;CVV123;有效期:11/28”。");
  }
  const year = expiries[0][2];
  const card = {
    number: numbers[0][1].replace(/[\s-]/g, ""),
    cvc: codes[0][1],
    exp_month: expiries[0][1].padStart(2, "0"),
    exp_year: year.length === 2 ? "20" + year : year,
  };
  const error = protocolCardError(card);
  if (error) throw new Error(error);
  return card;
}
