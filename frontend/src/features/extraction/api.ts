/** Structured extraction and deadlines (ADR 0034). */
import { apiClient } from '../../lib/api/client'
import type { Schemas } from '../../lib/api/types'

export type Template = Schemas['TemplateResponse']
export type FieldSpec = Schemas['FieldSpec']
export type Run = Schemas['RunResponse']
export type RunDetail = Schemas['RunDetailResponse']
export type CitedValue = Schemas['CitedValue']
export type Deadline = Schemas['DeadlineResponse']
export type StartRun = Schemas['StartExtractionRequest']
export type NewDeadline = Schemas['DeadlineRequest']
export type DeadlineChanges = Schemas['DeadlineUpdate']

const ws = (id: string) => `/workspaces/${id}`

export const extractionKeys = {
  templates: (w: string) => ['ws', w, 'extraction-templates'] as const,
  runs: (w: string) => ['ws', w, 'extractions'] as const,
  run: (w: string, id: string) => ['ws', w, 'extractions', id] as const,
  deadlines: (w: string, status: string) => ['ws', w, 'deadlines', status] as const,
  allDeadlines: (w: string) => ['ws', w, 'deadlines'] as const,
}

export async function getTemplates(w: string): Promise<Template[]> {
  return (await apiClient.get<Template[]>(`${ws(w)}/extraction-templates`)).data
}

export async function listRuns(w: string): Promise<Run[]> {
  return (await apiClient.get<Run[]>(`${ws(w)}/extractions`)).data
}

export async function getRun(w: string, id: string): Promise<RunDetail> {
  return (await apiClient.get<RunDetail>(`${ws(w)}/extractions/${id}`)).data
}

export async function startRun(w: string, body: StartRun): Promise<Run> {
  return (await apiClient.post<Run>(`${ws(w)}/extractions`, body)).data
}

export async function deleteRun(w: string, id: string): Promise<void> {
  await apiClient.delete(`${ws(w)}/extractions/${id}`)
}

async function download(path: string, filename: string): Promise<void> {
  const { data } = await apiClient.get<Blob>(path, { responseType: 'blob' })
  const url = URL.createObjectURL(data)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.append(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function downloadCsv(w: string, run: Run): Promise<void> {
  const name = run.title.replace(/[^\w\- ]+/g, '').trim().replace(/\s+/g, '-').toLowerCase() || 'extraction'
  return download(`${ws(w)}/extractions/${run.id}/export.csv`, `${name}.csv`)
}

export function downloadCalendar(w: string): Promise<void> {
  return download(`${ws(w)}/deadlines/calendar.ics`, 'documind-deadlines.ics')
}

export async function listDeadlines(w: string, status: 'open' | 'done' | 'all'): Promise<Deadline[]> {
  return (await apiClient.get<Deadline[]>(`${ws(w)}/deadlines`, { params: { status } })).data
}

export async function createDeadline(w: string, body: NewDeadline): Promise<Deadline> {
  return (await apiClient.post<Deadline>(`${ws(w)}/deadlines`, body)).data
}

export async function updateDeadline(w: string, id: string, body: DeadlineChanges): Promise<Deadline> {
  return (await apiClient.patch<Deadline>(`${ws(w)}/deadlines/${id}`, body)).data
}

export async function deleteDeadline(w: string, id: string): Promise<void> {
  await apiClient.delete(`${ws(w)}/deadlines/${id}`)
}

export const TEMPLATE_ICONS: Record<string, 'clock' | 'scale' | 'file' | 'edit'> = {
  obligations: 'clock',
  contract_terms: 'scale',
  invoice: 'file',
  custom: 'edit',
}

export const WARNING_LABELS: Record<string, string> = {
  not_ready: 'Not ready or archived, so it was skipped.',
  not_found: 'This document no longer exists.',
  partial: 'Very long: only the first part was read.',
  unreadable: "The AI's answer for this document couldn't be read. Try again.",
}

export const ERROR_LABELS: Record<string, string> = {
  interrupted: 'The extraction stopped unexpectedly. Run it again.',
  llm_rate_limited: 'The AI service is busy. Try again in a minute.',
  llm_unavailable: 'The AI service was unavailable. Try again later.',
  llm_misconfigured: 'The AI service is misconfigured. Ask an admin to check the AI provider settings.',
  provider_key_invalid: "The workspace's AI key was rejected. An admin can fix it on the AI providers page.",
  quota_exceeded: "This workspace has used this month's AI allowance.",
}

export function reminderLabel(days: number): string {
  return days === 0 ? 'On the day' : days === 1 ? '1 day before' : `${days} days before`
}
