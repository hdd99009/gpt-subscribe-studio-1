import { Alert, Badge, Button, Code, Group, Modal, Stack, Table, Text, Title } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { clearMemoryOrders, formatAmount, listMemoryOrders, type MemoryOrder } from "../api/client";
import { planLabel, regionLabel } from "../plans";
import { STATUS_LABELS, statusColor } from "./OrderSummary";

interface Props {
  active: boolean;
  onCleared: (orderIds: string[]) => void;
}

export function MemoryOrders({ active, onCleared }: Props) {
  const [items, setItems] = useState<MemoryOrder[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<"load" | "clear" | null>(null);
  const [message, setMessage] = useState<{ color: "red" | "green" | "yellow"; text: string } | null>(null);
  const [confirmation, setConfirmation] = useState<MemoryOrder[] | null>(null);
  const busyRef = useRef(false);

  async function refresh() {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy("load");
    try {
      setItems(await listMemoryOrders());
      setLoaded(true);
      setMessage(null);
    } catch (error) {
      setMessage({ color: "red", text: error instanceof Error ? error.message : "读取内存订单失败，请刷新重试" });
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  }

  useEffect(() => {
    if (active) void refresh();
  }, [active]);

  async function clearConfirmed() {
    if (!confirmation || busyRef.current) return;
    busyRef.current = true;
    setBusy("clear");
    setMessage(null);
    try {
      const result = await clearMemoryOrders(confirmation.map((item) => item.id));
      const clearedIds = [...result.deleted_ids, ...result.missing_ids];
      setItems((previous) => previous.filter((item) => !clearedIds.includes(item.id)));
      onCleared(clearedIds);
      setConfirmation(null);
      let text = `已清除 ${result.deleted_ids.length} 条内存订单。`;
      if (result.missing_ids.length) text += ` ${result.missing_ids.length} 条订单已被清除。`;
      if (result.skipped.length) text += ` ${result.skipped.length} 条订单仍在处理，请稍后刷新重试。`;
      setMessage({ color: result.skipped.length ? "yellow" : "green", text });
      try {
        setItems(await listMemoryOrders());
      } catch {
        setMessage({ color: "yellow", text: `${text} 列表刷新失败，请点击刷新。` });
      }
    } catch (error) {
      setMessage({ color: "red", text: error instanceof Error ? error.message : "清除结果未取得，请刷新列表核实后重试" });
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  }

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-start">
        <div>
          <Group gap="xs">
            <Title order={4}>内存订单</Title>
            {loaded && <Badge variant="light">{items.length} 条</Badge>}
          </Group>
          <Text size="sm" c="dimmed" mt={4}>当前后台进程保存的订单，重启后台后清空。点击刷新读取最新状态。</Text>
        </div>
        <Group gap="xs">
          <Button variant="default" loading={busy === "load"} disabled={busy === "clear"} onClick={() => void refresh()}>刷新</Button>
          <Button color="red" variant="light" disabled={busy !== null || items.length === 0}
            onClick={() => setConfirmation([...items])}>清除全部</Button>
        </Group>
      </Group>
      {message && <Alert color={message.color} role="status">{message.text}</Alert>}
      {loaded && items.length === 0 && <Text c="dimmed" ta="center" py="xl">当前后台没有内存订单。</Text>}
      {items.length > 0 && (
        <Table.ScrollContainer minWidth={1120}>
          <Table verticalSpacing="sm" highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>订单状态</Table.Th>
                <Table.Th>邮箱 / 卡号后4位</Table.Th>
                <Table.Th>套餐 / 地区</Table.Th>
                <Table.Th>金额</Table.Th>
                <Table.Th>付款 / 自动续费</Table.Th>
                <Table.Th>创建时间 / 订单号</Table.Th>
                <Table.Th>操作</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((item) => (
                <Table.Tr key={item.id}>
                  <Table.Td>
                    <Badge color={statusColor(item.status)} variant="light">{STATUS_LABELS[item.status] || item.status}</Badge>
                    {item.last_error && <Text size="xs" c="red" maw={240} mt={4}>{item.last_error}</Text>}
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{item.session_email || "—"}</Text>
                    <Code>{item.card_last4 || "—"}</Code>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{planLabel(item.plan)}</Text>
                    <Text size="xs" c="dimmed">{regionLabel(item.billing_country)}</Text>
                  </Table.Td>
                  <Table.Td>{formatAmount(item.amount, item.currency) || "—"}</Table.Td>
                  <Table.Td>
                    <Text size="sm">{item.payment_confirmed ? "付款已确认" : "付款待确认"}</Text>
                    <Text size="xs" c={item.renewal?.status === "cancelled" ? "green" : "dimmed"} maw={240} mt={4}>
                      {item.renewal?.message || "续费状态待核实"}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" style={{ whiteSpace: "nowrap" }}>{new Date(item.created_at).toLocaleString()}</Text>
                    <Code style={{ display: "block", wordBreak: "break-all", whiteSpace: "normal", maxWidth: 210 }}>{item.id}</Code>
                  </Table.Td>
                  <Table.Td>
                    <Button size="xs" color="red" variant="subtle" disabled={busy !== null || Boolean(item.deletion_blocked_reason)}
                      onClick={() => setConfirmation([item])} aria-label={`清除订单 ${item.id}`}>清除</Button>
                    {item.deletion_blocked_reason && <Text size="xs" c="dimmed" maw={200}>{item.deletion_blocked_reason}</Text>}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
      <Modal opened={confirmation !== null} onClose={() => { if (busy !== "clear") setConfirmation(null); }}
        title="清除内存订单" centered closeOnClickOutside={busy !== "clear"} closeOnEscape={busy !== "clear"}
        withCloseButton={busy !== "clear"}>
        <Stack gap="md">
          {message?.color === "red" && <Alert color="red">{message.text}</Alert>}
          <Text>将清除 {confirmation?.length || 0} 条后台订单及对应的会话凭证。待支付订单清除后需要重新创建结账。</Text>
          <Text size="sm" c="dimmed">正在执行请求的订单会跳过，处理结束后可再次清除。</Text>
          {confirmation?.length === 1 && <Code>{confirmation[0].id}</Code>}
          <Group justify="flex-end">
            <Button variant="default" disabled={busy === "clear"} onClick={() => setConfirmation(null)}>取消</Button>
            <Button color="red" loading={busy === "clear"} onClick={() => void clearConfirmed()}>确认清除</Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
