export const PASSWORD_MIN = 10
export const PASSWORD_MAX = 128

/** 0–4: length and character variety. Guidance only; the server enforces the policy. */
export function passwordScore(password: string): number {
  if (!password) return 0
  let score = password.length >= PASSWORD_MIN ? 1 : 0
  if (password.length >= 14) score += 1
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score += 1
  if (/\d/.test(password) || /[^A-Za-z0-9]/.test(password)) score += 1
  return Math.max(1, Math.min(score, 4))
}

export function validatePassword(password: string): string | null {
  if (password.length < PASSWORD_MIN) return `Use at least ${PASSWORD_MIN} characters.`
  if (password.length > PASSWORD_MAX) return `Use at most ${PASSWORD_MAX} characters.`
  return null
}
