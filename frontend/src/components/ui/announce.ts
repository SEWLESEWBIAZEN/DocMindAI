/**
 * Ask for a message to be spoken aloud (spoken confirmations, ADR 0011).
 * The voice feature listens for this event and speaks only when the user has
 * turned voice on; otherwise nothing happens. Screen readers already announce
 * dialogs on their own.
 */
export const ANNOUNCE_EVENT = 'documind:announce'

export function announce(text: string): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(ANNOUNCE_EVENT, { detail: { text } }))
}
