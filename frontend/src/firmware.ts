/** The fleet's CIMC baseline: the last M4 release the guide asks for. */
export const TARGET_FIRMWARE = '4.1(2f)'

function parts(version: string): Array<number | string> {
  return (version.match(/\d+|[a-z]+/gi) ?? []).map((p) => (/^\d+$/.test(p) ? Number(p) : p.toLowerCase()))
}

/** True when `version` is older than the baseline. Newer is fine; unknown is not flagged. */
export function firmwareBelowTarget(version: string | null | undefined): boolean {
  if (!version) return false
  const a = parts(version)
  const b = parts(TARGET_FIRMWARE)
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? 0
    const y = b[i] ?? 0
    if (x === y) continue
    if (typeof x === 'number' && typeof y === 'number') return x < y
    return String(x) < String(y)
  }
  return false
}
