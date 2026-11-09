import { Badge, Code, Table, Text } from "@mantine/core";

import { formatAmount } from "../api/client";
import { FAILED_STATUSES, SUCCESS_STATUSES, type HistoryRecord } from "../api/history";
import { planLabel, regionLabel } from "../plans";
import { STATUS_LABELS, statusColor } from "./OrderSummary";

export function HistoryTable({ items }: { items: HistoryRecord[] }) {
  if (items.length === 0) {
    return (
      <Text c="dimmed" ta="center" py="xl">
        还没有开通记录。
      </Text>
    );
  }
  return (
    <>
      <Text size="xs" c="dimmed" mb="xs">
        记录仅保存在当前浏览器，最多 50 条。
      </Text>
      <Table.ScrollContainer minWidth={900}>
        <Table verticalSpacing="sm" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>状态</Table.Th>
              <Table.Th>邮箱</Table.Th>
              <Table.Th>卡号后4位</Table.Th>
              <Table.Th>套餐</Table.Th>
              <Table.Th>地区</Table.Th>
              <Table.Th>金额</Table.Th>
              <Table.Th>时间</Table.Th>
              <Table.Th>订单号</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {items.map((item) => {
              const failed = FAILED_STATUSES.includes(item.status);
              const label = SUCCESS_STATUSES.includes(item.status)
                ? "成功"
                : failed
                  ? "失败"
                  : (STATUS_LABELS[item.status] ?? item.status);
              return (
                <Table.Tr key={item.id}>
                  <Table.Td>
                    <Badge color={statusColor(item.status)} variant="light">
                      {label}
                    </Badge>
                    {failed && item.error && (
                      <Text size="xs" c="red" mt={4} maw={260}>
                        {item.error}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>{item.email || "—"}</Table.Td>
                  <Table.Td><Code>{item.card_last4 || "—"}</Code></Table.Td>
                  <Table.Td>{planLabel(item.plan)}</Table.Td>
                  <Table.Td>{regionLabel(item.country)}</Table.Td>
                  <Table.Td>{formatAmount(item.amount, item.currency) ?? "-"}</Table.Td>
                  <Table.Td>
                    <Text size="sm" style={{ whiteSpace: "nowrap" }}>
                      {new Date(item.at).toLocaleString()}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Code>{item.id.slice(0, 8)}</Code>
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </>
  );
}
