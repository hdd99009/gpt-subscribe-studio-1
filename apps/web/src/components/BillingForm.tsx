import { Button, Group, Select, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { IconMapPin } from "@tabler/icons-react";

import type { BillingInput } from "../api/client";
import { randomBillingExample, TAX_FREE_STATE_LABEL } from "../card-payment/billingExamples";
import { REGIONS } from "../plans";

const US_STATES = (
  "AL:Alabama|AK:Alaska|AZ:Arizona|AR:Arkansas|CA:California|CO:Colorado|CT:Connecticut|DE:Delaware|"
  + "DC:District of Columbia|FL:Florida|GA:Georgia|HI:Hawaii|ID:Idaho|IL:Illinois|IN:Indiana|IA:Iowa|"
  + "KS:Kansas|KY:Kentucky|LA:Louisiana|ME:Maine|MD:Maryland|MA:Massachusetts|MI:Michigan|MN:Minnesota|"
  + "MS:Mississippi|MO:Missouri|MT:Montana|NE:Nebraska|NV:Nevada|NH:New Hampshire|NJ:New Jersey|NM:New Mexico|"
  + "NY:New York|NC:North Carolina|ND:North Dakota|OH:Ohio|OK:Oklahoma|OR:Oregon|PA:Pennsylvania|"
  + "RI:Rhode Island|SC:South Carolina|SD:South Dakota|TN:Tennessee|TX:Texas|UT:Utah|VT:Vermont|"
  + "VA:Virginia|WA:Washington|WV:West Virginia|WI:Wisconsin|WY:Wyoming"
).split("|").map((entry) => {
  const [value, name] = entry.split(":");
  return { value, label: `${name} (${value})` };
});

interface Props {
  value: BillingInput;
  onChange: (value: BillingInput) => void;
  disabled?: boolean;
  independentCountry?: boolean;
  compact?: boolean;
  /** Show a one-click US tax-free address generator. */
  randomize?: boolean;
}

export function BillingForm({ value, onChange, disabled, independentCountry = false, compact = false, randomize = false }: Props) {
  const set = (key: keyof BillingInput) => (event: React.ChangeEvent<HTMLInputElement>) =>
    onChange({ ...value, [key]: event.currentTarget.value });
  const billingCountry = independentCountry ? (
    <Select label="支付账单国家" description={compact ? undefined : "独立于创建结账地区；不会将 EGP 结账改成 USD。"}
      data={REGIONS.map((item) => ({ value: item.code, label: `${item.label} (${item.code})` }))}
      value={value.country} searchable disabled={disabled}
      onChange={(country) => {
        if (country && country !== value.country) onChange({ ...value, country, state: "", city: "", postal_code: "", line1: "" });
      }} />
  ) : (
    <TextInput label="国家" description="需和所选地区一致" value={value.country} readOnly disabled />
  );
  const nameInput = <TextInput label="持卡人姓名" value={value.name} onChange={set("name")} disabled={disabled} />;
  return (
    <Stack gap={compact ? "xs" : "sm"}>
      <Text size={compact ? "xs" : "sm"} c="dimmed">
        {independentCountry ? "支付账单国家独立于创建结账地区；请填写实际地址，税费由 Stripe 计算。" : "下面是自动生成的示例地址。用自己的真实卡时，请改成发卡行登记的账单地址。"}
      </Text>
      {randomize && (
        <Group justify="space-between" gap="xs" wrap="wrap">
          <Text size="xs" c="dimmed">
            美国免税州：{TAX_FREE_STATE_LABEL}（真实城市与街道，邮编匹配）
          </Text>
          <Button type="button" size="xs" variant="light" leftSection={<IconMapPin size={14} />}
            data-testid="random-tax-free-address"
            disabled={disabled}
            onClick={() => onChange(randomBillingExample(value))}>
            随机免税州地址
          </Button>
        </Group>
      )}
      {compact ? <SimpleGrid cols={{ base: 1, xs: 2 }}>{nameInput}{billingCountry}</SimpleGrid> : nameInput}
      <TextInput label="街道地址" value={value.line1} onChange={set("line1")} disabled={disabled} />
      <SimpleGrid cols={{ base: 1, sm: 3 }}>
        <TextInput label="城市" value={value.city} onChange={set("city")} disabled={disabled} />
        {independentCountry && value.country === "US" ? (
          <Select label="州" data={US_STATES} value={value.state || null} onChange={(state) => onChange({ ...value, state: state || "" })} disabled={disabled} searchable />
        ) : (
          <TextInput label="州 / 省" value={value.state} onChange={set("state")} disabled={disabled} />
        )}
        <TextInput label="邮编" value={value.postal_code} onChange={set("postal_code")} disabled={disabled} />
      </SimpleGrid>
      {!compact && billingCountry}
    </Stack>
  );
}
