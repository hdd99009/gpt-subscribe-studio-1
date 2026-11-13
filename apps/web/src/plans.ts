import type { Plan } from "./api/client";

export type Family = "plus" | "go" | "pro" | "team";

export const FAMILIES: { id: Family; label: string; hint: string }[] = [
  { id: "plus", label: "Plus", hint: "官方 Plus" },
  { id: "go", label: "Go", hint: "官方 Go" },
  { id: "pro", label: "Pro", hint: "三档用量" },
  { id: "team", label: "Team", hint: "托管长链" },
];

export const PRO_TIERS: { id: Plan; label: string; hint: string }[] = [
  { id: "pro_lite", label: "Standard", hint: "$100" },
  { id: "pro", label: "More", hint: "$200" },
  { id: "pro_max", label: "Max", hint: "$500" },
];

export const SEAT_PRESETS = [2, 3, 5, 8, 10];

export const REGIONS: { code: string; label: string }[] = [
  { code: "US", label: "美国" },
  { code: "PH", label: "菲律宾" },
  { code: "CL", label: "智利" },
  { code: "EG", label: "埃及" },
  { code: "JP", label: "日本" },
  { code: "GB", label: "英国" },
  { code: "TR", label: "土耳其" },
  { code: "BR", label: "巴西" },
  { code: "MX", label: "墨西哥" },
  { code: "IN", label: "印度" },
  { code: "ID", label: "印尼" },
  { code: "VN", label: "越南" },
  { code: "TH", label: "泰国" },
  { code: "MY", label: "马来西亚" },
  { code: "SG", label: "新加坡" },
  { code: "KR", label: "韩国" },
  { code: "AU", label: "澳大利亚" },
  { code: "CA", label: "加拿大" },
  { code: "DE", label: "德国" },
  { code: "NL", label: "荷兰" },
  { code: "PL", label: "波兰" },
  { code: "AR", label: "阿根廷" },
  { code: "SA", label: "沙特" },
  { code: "AE", label: "阿联酋" },
  { code: "ZA", label: "南非" },
];

export function isProFamily(value: Plan): boolean {
  return value === "pro_lite" || value === "pro" || value === "pro_max";
}

export function isPlusOrGo(value: string | undefined): boolean {
  return value === "plus" || value === "go";
}

export function familyOf(value: Plan): Family {
  if (value === "go") return "go";
  if (value === "team") return "team";
  if (isProFamily(value)) return "pro";
  return "plus";
}

export function planLabel(value: string): string {
  const tier = PRO_TIERS.find((item) => item.id === value);
  if (tier) return `Pro ${tier.label}`;
  const family = FAMILIES.find((item) => item.id === value);
  return family ? family.label : value;
}

export function regionLabel(code: string): string {
  const region = REGIONS.find((item) => item.code === code);
  return region ? `${region.label} (${region.code})` : code;
}
