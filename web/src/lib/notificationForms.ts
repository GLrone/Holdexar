export function amountToMinor(input: string): number | null {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(input.trim())
  if (!match) return null
  const minor = Number(match[1]) * 100 + Number((match[2] ?? '').padEnd(2, '0'))
  return Number.isSafeInteger(minor) ? minor : null
}

export function minorToInput(value: number | null | undefined): string {
  if (value == null) return ''
  return `${Math.trunc(value / 100)}.${String(value % 100).padStart(2, '0')}`
}
