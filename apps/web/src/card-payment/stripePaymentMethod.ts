import type { BillingDetails } from "./CardFields";

export interface ElementPaymentMethodClient {
  createPaymentMethod?: (data: unknown) => Promise<{
    paymentMethod?: { id: string; card?: { last4?: string } };
    error?: { message?: string };
  }>;
}

/** Use Stripe.js with its Elements instance, never an application-owned card object. */
export async function elementPaymentMethod(
  stripe: ElementPaymentMethodClient,
  elements: unknown,
  billing: BillingDetails,
  saveForReuse: boolean,
  onLast4?: (last4: string) => void,
): Promise<string> {
  if (!stripe.createPaymentMethod) throw new Error("当前 Stripe.js 不支持 createPaymentMethod，请刷新页面重试");
  const result = await stripe.createPaymentMethod({
    elements,
    params: {
      ...(saveForReuse ? { allow_redisplay: "always" } : {}),
      billing_details: {
        name: billing.name,
        email: billing.email,
        address: {
          line1: billing.line1, city: billing.city, state: billing.state,
          postal_code: billing.postal_code, country: billing.country,
        },
      },
    },
  });
  if (result.error || !result.paymentMethod?.id?.startsWith("pm_")) {
    throw new Error(result.error?.message || "创建支付方式失败");
  }
  onLast4?.(result.paymentMethod.card?.last4 || "");
  return result.paymentMethod.id;
}
