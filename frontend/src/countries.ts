const displayNames = new Intl.DisplayNames(["zh-CN"], { type: "region" });

export function countryFlag(countryCode: string): string {
  const normalized = countryCode.trim().toUpperCase();
  if (!/^[A-Z]{2}$/.test(normalized)) return "🌐";
  return String.fromCodePoint(
    ...Array.from(normalized, (letter) => 0x1f1e6 + letter.charCodeAt(0) - 65),
  );
}

export function countryNameZh(countryCode: string, fallback: string): string {
  const normalized = countryCode.trim().toUpperCase();
  if (!/^[A-Z]{2}$/.test(normalized)) return fallback || countryCode;
  return displayNames.of(normalized) ?? fallback ?? normalized;
}
