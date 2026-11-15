import { Alert, Button, Group, Stack, Textarea } from "@mantine/core";
import { useState } from "react";

import type { TeamCardPayload } from "../api/client";
import { parseProtocolCardText } from "./protocolBind";

interface Props {
  disabled?: boolean;
  onImport: (card: TeamCardPayload) => void;
}

export function CardImport({ disabled = false, onImport }: Props) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);

  function importCard() {
    if (disabled) return;
    try {
      const card = parseProtocolCardText(text);
      onImport(card);
      setText("");
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "卡片信息格式不正确。");
    }
  }

  return (
    <Stack gap="xs" data-testid="card-import">
      <Textarea label="粘贴卡片信息" placeholder="卡号:完整卡号;CVV123;有效期:11/28"
        value={text} disabled={disabled} autoComplete="off" spellCheck={false}
        autosize minRows={2} maxRows={4}
        onChange={(event) => { setText(event.currentTarget.value); setError(null); }} />
      <Group justify="flex-end">
        <Button type="button" variant="light" size="xs" disabled={disabled || !text.trim()}
          onClick={importCard}>一键导入</Button>
      </Group>
      {error && <Alert color="red" role="alert">{error}</Alert>}
    </Stack>
  );
}
