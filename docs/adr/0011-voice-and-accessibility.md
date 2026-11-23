# ADR 0011: Voice assistant and accessibility for blind and low-vision users

- Status: Accepted (roadmap). Implementation is spread across Phases 4, 5, 6b, 7 and 8.
- Date: 2026-11-23

## Context

The product owner wants DocuMind to serve blind and low-vision professionals:
asking questions by voice, and studying documents by listening. Blind
professionals already rely on screen readers (JAWS, NVDA, VoiceOver, TalkBack),
tuned to their preferred voices and speeds. A product that is usable *only*
through its own voice would be slower and less familiar to them than their
own tools.

## Decision

Deliver in two layers. The first layer is a prerequisite for the second.

### Layer 1: excellent screen-reader and keyboard support (required)

- WCAG 2.2 AA across the application.
- Every action reachable by keyboard, with visible focus and logical order.
- Streamed answers announced through a polite live region, once per sentence
  or paragraph rather than token by token.
- Citations are real links ("Source 1: Halcyon MSA, page 14").
- Documents are navigable by heading and page in the viewer.
- Verified with automated checks (axe) **and** manual testing in NVDA,
  JAWS and VoiceOver.

### Layer 2: built-in voice (opt-in)

- **Ask by voice.** Speech-to-text, then the same RAG pipeline as typed
  questions, with the same tenant and permission checks.
- **Answers read aloud with spoken sources.** Text-to-speech, streamed sentence
  by sentence as the answer arrives, with citations spoken ("According to
  Halcyon MSA, page 14 …").
- **Reading mode for studying documents.** "Read page 14", "next section",
  "go back", "summarize chapter 3", "what does it say about notice periods?".
  Adjustable speed; pause and stop on keyboard shortcuts.
- **Never talk over the user's screen reader.** Voice output is opt-in, never
  auto-plays, and can always be stopped instantly.
- **Spoken confirmation for destructive actions** (delete, share, remove member).

### Architecture

- New ports beside `LLMProvider`: `SpeechToTextProvider` and
  `TextToSpeechProvider`. The first adapter uses OpenAI's speech models, with
  the browser's built-in Web Speech API as a no-cost fallback.
- Voice is a client of the existing conversation and RAG services. No separate
  authorization path.
- Audio is **not stored by default**. Each workspace has a setting that lets
  admins disable voice, or restrict it to the browser-only fallback, for
  confidential workspaces.

## Changes to the phase plan

| Phase | Change |
| ----- | ------ |
| 3 | No change. Upload validation already rejects files we can't process. |
| 4 | **OCR becomes required**, not optional: scanned PDFs are otherwise inaccessible. Extraction must preserve reading order, headings and page numbers. Add text descriptions for figures and charts (vision model). |
| 5 | Add `SpeechToTextProvider` / `TextToSpeechProvider` abstractions. Citation format includes speakable labels. |
| 6b (new) | **Voice and accessible reading**: voice questions, spoken streamed answers, document reading mode, voice settings. Built after Phase 6 (chat). |
| 7 | Accessibility (Layer 1) becomes an acceptance criterion, with screen-reader testing. |
| 8 | Track audio minutes as usage; per-workspace voice policy in admin settings. |
| 9 | Accessibility audit plus testing sessions with blind users before release. |

## Consequences

- Extra provider cost (audio minutes) and a new privacy surface (voice data
  sent to a provider). Both are made explicit and controllable per workspace.
- Investment in Layer 1 also benefits keyboard-only users, users with motor
  impairments, and power users.
- Testing with real blind users is required; automated checks alone are not
  sufficient.
