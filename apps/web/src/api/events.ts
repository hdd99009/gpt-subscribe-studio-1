/**
 * SSE (Server-Sent Events) 实时订单状态订阅
 */

export interface OrderUpdateEvent {
  type?: "deleted";
  order_id: string;
  status: string;
  timestamp: number;
  payment_status?: string;
  subscription_active?: boolean;
  error?: string;
}

export type OrderUpdateCallback = (event: OrderUpdateEvent) => void;

/**
 * 订阅订单状态更新流
 * @param orderId 订单 ID
 * @param onUpdate 接收到更新时的回调
 * @returns 取消订阅的函数
 */
export function subscribeOrderUpdates(
  orderId: string,
  onUpdate: OrderUpdateCallback,
): () => void {
  const eventSource = new EventSource(`/api/events/orders/${orderId}/stream`);

  eventSource.addEventListener("message", (e) => {
    try {
      const data = JSON.parse(e.data);
      
      // 忽略初始化事件
      if (data.type === "init") {
        console.log(`[SSE] Subscribed to order ${orderId}, initial status: ${data.status}`);
        return;
      }
      if (data.type === "deleted" || data.error) eventSource.close();

      // 触发回调
      onUpdate(data as OrderUpdateEvent);
    } catch (err) {
      console.error("[SSE] Failed to parse event data:", err, e.data);
    }
  });

  eventSource.addEventListener("error", (e) => {
    console.warn("[SSE] Connection error or closed:", e);
    // EventSource 会自动重连，除非显式关闭
  });

  // 返回取消订阅函数
  return () => {
    eventSource.close();
  };
}
