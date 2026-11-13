import { Alert, Badge, Button, Code, Group, Paper, Stack, Switch, Table, Text, Title } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { getDebugSettings, setDebugEnabled, type DebugSettings as Settings } from "../api/client";

export function DebugSettings({ active }: { active: boolean }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const busyRef = useRef(false);

  async function update(enabled?: boolean) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      setSettings(await (enabled === undefined ? getDebugSettings() : setDebugEnabled(enabled)));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "调试设置读取失败，请重试");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  useEffect(() => {
    if (active) void update();
  }, [active]);

  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Title order={4}>调试模式</Title>
        <Button variant="default" loading={busy} onClick={() => void update()}>刷新记录</Button>
      </Group>
      {error && <Alert color="red" role="alert">{error}</Alert>}
      {settings?.last_error && <Alert color="red" role="alert">{settings.last_error}</Alert>}
      <Paper withBorder p="md" radius="md">
        <Stack gap="sm">
          <Switch
            label="记录完整请求与响应"
            checked={settings?.enabled ?? false}
            disabled={busy || !settings}
            onChange={(event) => void update(event.currentTarget.checked)}
          />
          <Text size="sm" c="dimmed">
            记录后端业务接口，以及 ChatGPT、Stripe、Sentinel 和验证码服务的请求头、正文、响应、耗时与代理信息。
            每次开启建立一组新记录，关闭后已发出的请求仍会补齐响应。运行中可直接切换。
          </Text>
          <Text size="sm" c="orange">
            完整日志包含 Session、Cookie、代理凭据和支付字段。HAR 下载文件中也包含这些内容。
          </Text>
          {settings && <Text size="sm">本机保存目录 <Code style={{ overflowWrap: "anywhere" }}>{settings.directory}</Code></Text>}
          <Text size="xs" c="dimmed">浏览器内 Stripe.js 的独立请求可通过浏览器 HAR 补充。下载包含当前已经收到的数据，进行中的请求会标注状态。</Text>
        </Stack>
      </Paper>
      {settings && settings.captures.length === 0 && <Text c="dimmed">开启调试后，再执行需要排查的操作。</Text>}
      {settings && settings.captures.length > 0 && (
        <Table.ScrollContainer minWidth={680}>
          <Table verticalSpacing="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>开始时间</Table.Th><Table.Th>请求数</Table.Th><Table.Th>进行中</Table.Th>
                <Table.Th>失败响应 / 异常</Table.Th><Table.Th>文件大小</Table.Th><Table.Th>下载</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {settings.captures.map((capture) => (
                <Table.Tr key={capture.id}>
                  <Table.Td>
                    <Text size="sm">{new Date(capture.started_at).toLocaleString()}</Text>
                    {settings.enabled && settings.capture_id === capture.id && <Badge size="xs" color="green">正在记录</Badge>}
                  </Table.Td>
                  <Table.Td>{capture.count}</Table.Td><Table.Td>{capture.pending}</Table.Td>
                  <Table.Td>{capture.errors}</Table.Td><Table.Td>{(capture.size_bytes / 1024 / 1024).toFixed(2)} MB</Table.Td>
                  <Table.Td>
                    <Button component="a" size="xs" variant="light" download
                      href={`/api/settings/debug/captures/${encodeURIComponent(capture.id)}/har`}>下载 HAR</Button>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
    </Stack>
  );
}
