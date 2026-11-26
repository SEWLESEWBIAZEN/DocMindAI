import type { FeedbackReason } from './api'

export const REASON_LABELS: Record<FeedbackReason, string> = {
  incorrect: 'Wrong or made up',
  incomplete: 'Incomplete',
  bad_citation: "Sources don't support it",
  missed_document: 'Missed a document that has the answer',
  unclear: 'Hard to understand',
  other: 'Something else',
}
