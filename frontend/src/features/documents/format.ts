import type { BadgeTone } from '../../components/ui'
import type { DocumentStatus } from '../../lib/api/types'

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' })
const dateTimeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })

export function formatDate(iso: string | null | undefined): string {
  return iso ? dateFormat.format(new Date(iso)) : '—'
}

export function formatDateTime(iso: string | null | undefined): string {
  return iso ? dateTimeFormat.format(new Date(iso)) : '—'
}

export function formatRelative(iso: string, now = Date.now()): string {
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  const abs = Math.abs(seconds)
  if (abs < 60) return rtf.format(seconds, 'second')
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), 'minute')
  if (abs < 86_400) return rtf.format(Math.round(seconds / 3600), 'hour')
  if (abs < 30 * 86_400) return rtf.format(Math.round(seconds / 86_400), 'day')
  return formatDate(iso)
}

export const STATUS_LABEL: Record<DocumentStatus, string> = {
  pending: 'Queued',
  processing: 'Processing',
  ready: 'Ready',
  failed: 'Failed',
}

export const STATUS_TONE: Record<DocumentStatus, BadgeTone> = {
  pending: 'neutral',
  processing: 'accent',
  ready: 'success',
  failed: 'danger',
}

export const FORMAT_LABEL: Record<string, string> = {
  pdf: 'PDF',
  docx: 'Word',
  txt: 'Text',
  markdown: 'Markdown',
}

const WARNINGS: Record<string, string> = {
  no_page_numbers: "This format has no pages, so citations point to sections instead of page numbers.",
  page_numbers_from_word_layout: 'Page numbers come from Word\'s saved layout and may differ slightly from print.',
  ocr_unavailable: "Some pages are scanned images, but text recognition (OCR) isn't available, so they weren't read.",
  ocr_failed_on_some_pages: 'Text recognition failed on some scanned pages; they may be missing from answers.',
}

/** Plain-language extraction notes (codes come from the processing pipeline). */
export function describeWarning(code: string): string {
  const page = code.match(/^no_text_layer:page=(\d+)/)
  if (page) return `Page ${page[1]} has no text layer and couldn't be read.`
  const count = code.match(/^(\w+):count=(\d+)$/)
  if (count) {
    const n = Number(count[2])
    const figures = `${n} ${n === 1 ? 'figure' : 'figures'}`
    if (count[1] === 'figures_described') return `${figures} described in words, so they can be searched and read aloud.`
    if (count[1] === 'figure_descriptions_failed') return `${figures} couldn't be described.`
    if (count[1] === 'figures_not_described') return `${figures} weren't described (limit per document reached).`
  }
  const ocr = code.match(/^ocr_applied:pages=([\d,]+)$/)
  if (ocr) return `Text recognition (OCR) was used on page${ocr[1]?.includes(',') ? 's' : ''} ${ocr[1]?.replace(/,/g, ', ')}.`
  return WARNINGS[code] ?? code.replace(/_/g, ' ')
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat().format(value)
}
