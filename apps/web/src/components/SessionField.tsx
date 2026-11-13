import { Textarea } from "@mantine/core";

interface Props {
  value: string;
  onChange: (value: string) => void;
  team: boolean;
  disabled?: boolean;
}

export function SessionField({ value, onChange, team, disabled }: Props) {
  return (
    <Textarea
      label="ChatGPT session"
      description={
        team
          ? "Team 长链一般只需要 accessToken，有整段 JSON 也可以贴。"
          : "请粘贴整段 session JSON（accessToken、sessionToken、cookies / oai-did）。Plus / Go / Pro 页内填卡只贴 accessToken 经常不够。"
      }
      placeholder='{"accessToken": "…"}'
      autosize
      minRows={team ? 3 : 4}
      maxRows={team ? 6 : 10}
      value={value}
      onChange={(event) => onChange(event.currentTarget.value)}
      disabled={disabled}
      styles={{ input: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 } }}
    />
  );
}
