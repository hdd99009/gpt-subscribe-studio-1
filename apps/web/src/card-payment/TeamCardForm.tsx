import { PasswordInput, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { CardImport } from "./CardImport";

export interface TeamCardDraft {
  number: string;
  exp_month: string;
  exp_year: string;
  cvc: string;
}

export function emptyTeamCard(): TeamCardDraft {
  return { number: "", exp_month: "", exp_year: "", cvc: "" };
}

interface Props {
  value: TeamCardDraft;
  onChange: (value: TeamCardDraft) => void;
  disabled?: boolean;
}

/** Card fields are sent once to this service, which tokenizes them with Stripe. */
export function TeamCardForm({ value, onChange, disabled }: Props) {
  const change = (field: keyof TeamCardDraft) => (event: React.ChangeEvent<HTMLInputElement>) =>
    onChange({ ...value, [field]: event.currentTarget.value });
  return (
    <Stack gap="sm" data-testid="team-card-form">
      <Text fw={600}>Team 卡片信息</Text>
      <Text size="xs" c="dimmed">卡号只用于本次向 Stripe 换取支付标识，不写入订单记录。</Text>
      <CardImport onImport={onChange} disabled={disabled} />
      <TextInput label="卡号" value={value.number} onChange={change("number")}
        inputMode="numeric" autoComplete="off" disabled={disabled} data-testid="team-card-number" />
      <SimpleGrid cols={{ base: 1, xs: 3 }}>
        <TextInput label="到期月份" placeholder="MM" value={value.exp_month} onChange={change("exp_month")}
          inputMode="numeric" autoComplete="off" maxLength={2} disabled={disabled} />
        <TextInput label="到期年份" placeholder="YYYY" value={value.exp_year} onChange={change("exp_year")}
          inputMode="numeric" autoComplete="off" maxLength={4} disabled={disabled} />
        <PasswordInput label="CVC" value={value.cvc} onChange={change("cvc")}
          inputMode="numeric" autoComplete="off" maxLength={4} disabled={disabled} />
      </SimpleGrid>
    </Stack>
  );
}
