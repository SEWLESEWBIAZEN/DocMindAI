import { apiClient } from '../../lib/api/client'
import { ApiError } from '../../lib/api/errors'
import { CSRF_HEADER, expireSession, refreshSession, validAccessToken } from '../../lib/api/session'
import { readSse } from '../../lib/api/sse'
import type { ChatMessage, Conversation, ConversationDetail, Schemas } from '../../lib/api/types'

export const conversationKeys = {
  all: (ws: string) => ['ws', ws, 'conversations'] as const,
  list: (ws: string, q: string, offset: number, limit: number) =>
    ['ws', ws, 'conversations', 'list', { q, offset, limit }] as const,
  detail: (ws: string, id: string) => ['ws', ws, 'conversations', 'detail', id] as const,
  messages: (ws: string, id: string) => ['ws', ws, 'conversations', 'messages', id] as const,
}

const base = (ws: string) => `/workspaces/${ws}/conversations`

export async function listConversations(
  ws: string,
  { q = '', offset = 0, limit = 25 }: { q?: string; offset?: number; limit?: number } = {},
): Promise<Schemas['ConversationPageResponse']> {
  const { data } = await apiClient.get<Schemas['ConversationPageResponse']>(base(ws), {
    params: { q: q || undefined, offset, limit },
  })
  return data
}

export async function createConversation(
  ws: string,
  body: { title?: string; document_ids?: string[]; collection_ids?: string[] } = {},
): Promise<ConversationDetail> {
  return (await apiClient.post<ConversationDetail>(base(ws), body)).data
}

export async function getConversation(ws: string, id: string): Promise<ConversationDetail> {
  return (await apiClient.get<ConversationDetail>(`${base(ws)}/${id}`)).data
}

export async function renameConversation(ws: string, id: string, title: string): Promise<ConversationDetail> {
  return (await apiClient.patch<ConversationDetail>(`${base(ws)}/${id}`, { title })).data
}

export async function setConversationSources(
  ws: string,
  id: string,
  body: { document_ids: string[]; collection_ids: string[] },
): Promise<ConversationDetail> {
  return (await apiClient.put<ConversationDetail>(`${base(ws)}/${id}/sources`, body)).data
}

export async function deleteConversation(ws: string, id: string): Promise<void> {
  await apiClient.delete(`${base(ws)}/${id}`)
}

export async function listMessages(ws: string, id: string, before?: number): Promise<Schemas['MessagePageResponse']> {
  const { data } = await apiClient.get<Schemas['MessagePageResponse']>(`${base(ws)}/${id}/messages`, {
    params: { before, limit: 50 },
  })
  return data
}

export async function cancelReply(ws: string, conversationId: string, messageId: string): Promise<void> {
  await apiClient.post(`${base(ws)}/${conversationId}/messages/${messageId}/cancel`)
}

export type StreamEvent =
  | { type: 'message.created'; user_message: ChatMessage | null; assistant_message: ChatMessage }
  | { type: 'answer.delta'; message_id: string; text: string }
  | { type: 'answer.completed'; message: ChatMessage }
  | { type: 'answer.cancelled'; message: ChatMessage | null }
  | { type: 'answer.error'; code: string; detail: string; message: ChatMessage | null }

async function errorFrom(response: Response): Promise<ApiError> {
  let detail = `Request failed (${response.status})`
  let code: string | null = null
  try {
    const body = (await response.json()) as { detail?: unknown; code?: unknown }
    if (typeof body.detail === 'string') detail = body.detail
    else if (Array.isArray(body.detail)) detail = 'Please check your message.'
    if (typeof body.code === 'string') code = body.code
  } catch {
    // not JSON
  }
  if (response.status === 429) detail = 'Too many questions in a short time. Please wait a moment.'
  return new ApiError(detail, response.status, response.headers.get('x-request-id'), code)
}

/**
 * POST and read the answer as Server-Sent Events. Aborting `signal` closes
 * the connection; the server then stops generating and saves the partial
 * answer as cancelled.
 */
export async function streamReply(
  path: string,
  body: unknown,
  { signal, onEvent }: { signal?: AbortSignal; onEvent: (event: StreamEvent) => void },
): Promise<void> {
  const send = async (token: string | null) =>
    fetch(`/api/v1${path}`, {
      method: 'POST',
      headers: {
        Accept: 'text/event-stream',
        'Content-Type': 'application/json',
        [CSRF_HEADER]: '1',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  let response = await send(await validAccessToken())
  if (response.status === 401) {
    try {
      response = await send((await refreshSession()).access_token)
    } catch {
      expireSession()
    }
  }
  if (!response.ok) throw await errorFrom(response)
  for await (const event of readSse(response)) {
    try {
      onEvent({ type: event.event, ...JSON.parse(event.data) } as StreamEvent)
    } catch {
      // ignore malformed events
    }
  }
}

export function sendMessage(
  ws: string,
  conversationId: string,
  content: string,
  options: { signal?: AbortSignal; onEvent: (event: StreamEvent) => void },
): Promise<void> {
  return streamReply(`${base(ws)}/${conversationId}/messages`, { content }, options)
}

export function regenerate(
  ws: string,
  conversationId: string,
  messageId: string,
  options: { signal?: AbortSignal; onEvent: (event: StreamEvent) => void },
): Promise<void> {
  return streamReply(`${base(ws)}/${conversationId}/messages/${messageId}/regenerate`, undefined, options)
}

export type { Conversation }

export type FeedbackReason = NonNullable<Schemas['MessageFeedbackRequest']['reasons']>[number]
export type AnswerQuality = Schemas['AnswerQualityResponse']

/** Thumbs up or down on an answer (ADR 0038); rating again replaces it. */
export async function rateAnswer(
  ws: string,
  conversationId: string,
  messageId: string,
  body: Schemas['MessageFeedbackRequest'],
): Promise<ChatMessage> {
  return (await apiClient.put<ChatMessage>(`${base(ws)}/${conversationId}/messages/${messageId}/feedback`, body)).data
}

export async function clearRating(ws: string, conversationId: string, messageId: string): Promise<void> {
  await apiClient.delete(`${base(ws)}/${conversationId}/messages/${messageId}/feedback`)
}

export async function getAnswerQuality(ws: string, days: number): Promise<AnswerQuality> {
  return (await apiClient.get<AnswerQuality>(`/workspaces/${ws}/answer-quality`, { params: { days } })).data
}
