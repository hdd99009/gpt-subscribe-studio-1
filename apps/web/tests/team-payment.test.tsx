import assert from "node:assert/strict";
import test from "node:test";
import { MantineProvider } from "@mantine/core";
import { renderToStaticMarkup } from "react-dom/server";

import { quoteTeam, prepareTeam, pollTeam, type Order, type TeamHostedContext } from "../src/api/client";
import { loadHistory, normalizeHistoryError } from "../src/api/history";
import { hostedCaptcha } from "../src/card-payment/hostedCaptcha";
import { TeamPaymentPanel } from "../src/components/TeamPaymentPanel";
import { ProtocolPayPanel } from "../src/components/ProtocolPayPanel";
import { emptyTeamCard } from "../src/card-payment/TeamCardForm";
import { PlanPicker } from "../src/components/PlanPicker";
import { OrderSummary } from "../src/components/OrderSummary";
import { billingExampleError, randomBillingExample, TAX_FREE_STATES } from "../src/card-payment/billingExamples";
import { elementPaymentMethod } from "../src/card-payment/stripePaymentMethod";

const billing = { name: "", line1: "", city: "", state: "", postal_code: "", country: "US" };
const blankOrder: Order = {
  id: "order_fixture", plan: "team", billing_country: "EG", status: "awaiting_payment",
  checkout_url: "https://example.invalid/team", publishable_key: null,
};
function panel(order: Order | null) {
  return renderToStaticMarkup(
    <MantineProvider><TeamPaymentPanel card={emptyTeamCard()} onCard={() => {}}
      billing={billing} onBilling={() => {}} order={order} busy={false} /></MantineProvider>,
  );
}

test("Team protocol offers Stripe direct routing and blocks repeat submission while awaiting verification", () => {
  const html = renderToStaticMarkup(<MantineProvider><ProtocolPayPanel
    orderId="order_fixture" plan="team" country="EG" pmMode="server" amount="1150" currency="EGP"
    submissionDisabled initialBilling={{ name: "Fixture Holder", country: "US" }}
  /></MantineProvider>);
  assert.ok(html.includes("Stripe 支付直连"));
  assert.ok(html.includes("沿用创建结账时的订单代理"));
  assert.ok(html.includes('data-testid="card-import"'));
  const buttons = Array.from(html.matchAll(/<button\b[^>]*>.*?<\/button>/g), (match) => match[0]);
  const paymentButton = buttons.find((button) => button.includes("确认支付"));
  assert.ok(paymentButton?.includes("disabled"));
});

test("Team panel before checkout reserves space but does not collect raw card fields", () => {
  const html = panel(null);
  assert.ok(html.includes('data-testid="team-card-form"'));
  assert.ok(html.includes("卡号只用于本次向 Stripe 换取支付标识"));
  assert.ok(html.includes("Team 账单地址"));
  assert.ok(html.includes('data-testid="team-card-number"'));
  assert.ok(html.includes('data-testid="card-import"'));
  assert.ok(html.includes("一键导入"));
});

test("Team card fields stay available regardless of checkout state", () => {
  for (const order of [blankOrder, { ...blankOrder, publishable_key: "pk_test_fixture" }]) {
    const html = panel(order);
    assert.ok(html.includes('data-testid="team-card-number"'));
    assert.ok(!html.includes("尚未取得 Stripe 公钥"));
  }
  assert.ok(!panel(null).includes('data-testid="stripe-payment-element"'));
});

test("Team card payload carries only the four card fields", () => {
  const card = { ...emptyTeamCard(), number: "4242424242424242", cvc: "123", exp_month: "12", exp_year: "2030" };  // release-check: ignore
  assert.deepEqual(Object.keys(card).sort(), ["cvc", "exp_month", "exp_year", "number"]);
  assert.equal(emptyTeamCard().number, "");
});

test("Live captcha widget executes with current site key and rqdata and is removed", async () => {
  const originalWindow = globalThis.window;
  const originalDocument = globalThis.document;
  const events: unknown[] = [];
  const container = { remove: () => events.push("container_removed") };
  Object.assign(globalThis, {
    window: { hcaptcha: {
      render: (_, options) => { events.push(options); return "widget_fixture"; },
      execute: async (_, options) => { events.push(options); return { response: "fresh_fixture_token" }; },
      remove: () => events.push("widget_removed"),
    } },
    document: { createElement: () => container, body: { appendChild: () => {} } },
  });
  try {
    const token = await hostedCaptcha({ passive_captcha: { site_key: "site_fixture", rqdata: "current_rqdata" } } as TeamHostedContext);
    assert.equal(token, "fresh_fixture_token");
    assert.deepEqual(events[0], { sitekey: "site_fixture", size: "invisible" });
    assert.deepEqual(events[1], { async: true, rqdata: "current_rqdata" });
    assert.deepEqual(events.slice(2), ["widget_removed", "container_removed"]);
  } finally { Object.assign(globalThis, { window: originalWindow, document: originalDocument }); }
});

test("Team polling uses read-only Hosted state route and never resubmits a card", async () => {
  const original = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = async (url, init) => { calls.push(String(url)); assert.equal(init?.body, undefined);
    return new Response(JSON.stringify({ ...blankOrder, status: "delivered" }), { status: 200 }); };
  try { assert.equal((await pollTeam("fixture")).status, "delivered"); assert.deepEqual(calls, ["/api/orders/fixture/team/poll"]); }
  finally { globalThis.fetch = original; }
});


test("Legacy blocked history drops unsupported region/card advice but preserves record data", () => {
  const old = "ChatGPT 未批准这次支付（blocked）。不是发卡行拒卡，是账号/地区风控。账单国家必须和 session 导出国家一致。";
  const expected = "ChatGPT 未批准本次结账（blocked），上游未提供明确原因。尚未确认扣款，请先核对账单。";
  assert.equal(normalizeHistoryError(old), expected);
  assert.ok(normalizeHistoryError(`risk_blocked。${old}`)?.includes("上游信息：risk_blocked"));
  assert.equal(normalizeHistoryError("card_declined: insufficient_funds"), "card_declined: insufficient_funds");
  const original = globalThis.localStorage;
  const record = { id: "old_fixture", plan: "pro_lite", country: "PH", status: "failed", at: 123, error: old };
  const values = new Map([["gss.history.v1", JSON.stringify([record])]]);
  Object.assign(globalThis, { localStorage: { getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value) } });
  try {
    assert.deepEqual(loadHistory(), [{ ...record, error: expected }]);
    assert.equal(JSON.parse(values.get("gss.history.v1"))[0].error, expected);
  } finally { Object.assign(globalThis, { localStorage: original }); }
});


test("Quote route reads amount without card submission or payment confirmation", async () => {
  const originalFetch = globalThis.fetch;
  const originalNavigator = globalThis.navigator;
  const calls: {url: string; body: unknown}[] = [];
  Object.defineProperty(globalThis, "navigator", {value: {language: "en-US"}, configurable: true});
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), body: JSON.parse(String(init?.body))});
    return new Response(JSON.stringify({...blankOrder, amount: "1150", currency: "EGP"}), {status: 200});
  };
  try {
    const got = await quoteTeam("fixture");
    assert.equal(got.amount, "1150");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/orders/fixture/team/quote");
    assert.ok(!JSON.stringify(calls[0].body).includes("card"));
  } finally {globalThis.fetch = originalFetch; Object.defineProperty(globalThis, "navigator", {value: originalNavigator, configurable: true});}
});

test("Summary renders quote transport failure instead of suggesting a payment attempt", () => {
  const order = {...blankOrder, quote_error: "Stripe 初始化结账失败；本步骤未提交付款确认"};
  const html = renderToStaticMarkup(<MantineProvider><OrderSummary plan="team" country="EG" order={order} seats={2}/></MantineProvider>);
  assert.ok(html.includes("尚未取得 Stripe 报价"));
  assert.ok(html.includes("本步骤未提交付款确认"));
});


test("Local example generator produces plausible tax-free US addresses", () => {
  const current = { ...billing, name: "Actual User", email: "fixture@example.invalid" };
  const seen = new Set<string>();
  for (let i = 0; i < 80; i++) {
    const value = randomBillingExample(current);
    seen.add(value.state);
    assert.equal(value.country, "US");
    assert.ok(TAX_FREE_STATES.includes(value.state), `unexpected state ${value.state}`);
    assert.ok(value.city.length > 2);
    assert.match(value.postal_code, /^\d{5}$/);
    // Real US house number + genuine street name, optional unit suffix.
    assert.match(value.line1, /^\d{3,4} [A-Za-z0-9][A-Za-z0-9 .\-]*( Apt \d{1,2})?$/);
    assert.ok(!/\[示例\]|Example Street/i.test(value.line1));
    assert.equal(value.name, current.name);
    assert.equal(value.email, current.email);
  }
  assert.ok(seen.size >= 4, "should spread across tax-free states");
});

test("Generated addresses cover every tax-free state", () => {
  const states = new Set<string>();
  for (let i = 0; i < 400; i++) states.add(randomBillingExample(billing).state);
  assert.deepEqual([...states].sort(), [...TAX_FREE_STATES].sort());
});

test("Generated address is ready to submit without extra confirmation", () => {
  const example = randomBillingExample(billing, () => 0);
  assert.equal(billingExampleError(), null);
  assert.match(example.line1, /^\d+ [A-Za-z]/);
});

test("Team example controls describe the address as directly usable", () => {
  const html = renderToStaticMarkup(<MantineProvider><TeamPaymentPanel card={emptyTeamCard()} onCard={() => {}}
    billing={billing} onBilling={() => {}} order={null} busy={false} onExample={() => {}}
    hasExample /></MantineProvider>);
  assert.ok(html.includes("随机示例地址"));
  assert.ok(html.includes("已填充虚构示例"));
  assert.ok(html.includes("可直接用于本次支付"));
  assert.ok(!html.includes("已替换示例并确认是实际账单地址"));
});


test("Quote and prepare deadlines report explicit nonpayment timeout instead of hanging", async () => {
  const originalFetch = globalThis.fetch;
  const originalNavigator = globalThis.navigator;
  Object.defineProperty(globalThis, "navigator", {value: {language: "en-US"}, configurable: true});
  let sawSignal = false;
  let calls = 0;
  globalThis.fetch = (_url, init) => {
    calls += 1;
    // Quote uses a fixed 25s deadline; fail it immediately here to keep the suite fast.
    if (calls > 1) return Promise.reject(new Error("fixture quote transport failure"));
    sawSignal = Boolean(init?.signal);
    return new Promise((_, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }) as Promise<Response>;
  };
  try {
    await assert.rejects(prepareTeam("fixture", billing, {timeoutMs: 100}), /超时.*未提交付款确认/);
    await assert.rejects(quoteTeam("fixture"), /fixture quote transport failure|超时/);
    assert.ok(sawSignal);
  } finally {
    globalThis.fetch = originalFetch;
    Object.defineProperty(globalThis, "navigator", {value: originalNavigator, configurable: true});
  }
});
