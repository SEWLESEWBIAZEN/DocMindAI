export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="8" fill="var(--color-accent)" />
      <path
        d="M10 8h7a7 7 0 0 1 0 14h-7z"
        fill="none"
        stroke="var(--color-on-accent)"
        strokeWidth="2.6"
        strokeLinejoin="round"
      />
      <circle cx="15" cy="15" r="2.2" fill="var(--color-on-accent)" />
    </svg>
  )
}
