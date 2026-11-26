import { useSyncExternalStore } from 'react'

/** Light, dark, or whatever the device uses. Remembered per browser. */
export type ThemeChoice = 'light' | 'dark' | 'system'

// public/theme.js reads the same key before the app loads.
const KEY = 'documind.theme'
const listeners = new Set<() => void>()

function read(): ThemeChoice {
  try {
    const value = window.localStorage.getItem(KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch {
    return 'system'
  }
}

let current: ThemeChoice = typeof window === 'undefined' ? 'system' : read()

function apply(choice: ThemeChoice) {
  const root = document.documentElement
  if (choice === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', choice)
}

export function setTheme(choice: ThemeChoice) {
  current = choice
  try {
    if (choice === 'system') window.localStorage.removeItem(KEY)
    else window.localStorage.setItem(KEY, choice)
  } catch {
    // Private mode: the choice lasts until reload.
  }
  apply(choice)
  listeners.forEach((notify) => notify())
}

if (typeof window !== 'undefined') {
  apply(current)
  // Another tab changed the theme.
  window.addEventListener('storage', (event) => {
    if (event.key !== KEY && event.key !== null) return
    current = read()
    apply(current)
    listeners.forEach((notify) => notify())
  })
}

function subscribe(notify: () => void) {
  listeners.add(notify)
  return () => listeners.delete(notify)
}

export function useTheme(): ThemeChoice {
  return useSyncExternalStore(subscribe, () => current, () => 'system')
}
