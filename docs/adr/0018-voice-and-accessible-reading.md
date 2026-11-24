# ADR 0018: Voice and accessible reading (Phase 6b)

- Status: Accepted
- Date: 2026-11-24
- Implements: ADR 0011, layer 2. Completes the deferred figure descriptions from ADR 0015.

## Context

ADR 0011 committed DocuMind to blind and low-vision professionals:

- ask by voice;
- hear answers with their sources;
- study documents by listening;
- describe figures in words;
- never talk over a screen reader.

Layer 1 (keyboard and screen-reader support) shipped in Phase 7. This phase
adds the voice layer and figure descriptions.

## Decision

### Opt-in, user-controlled voice

- Voice is **off by default** for every user. When turned on, it adds Listen
  and microphone buttons. **Nothing plays unless the user presses one**.
  The exception is "read answers aloud as they arrive", a separate opt-in that
  speaks only after the user asks a question.
- Preferences stay in the browser:
  - engine, voice, speed (0.5–2×);
  - whether to speak sources;
  - auto-read, send-after-dictation, dictation language.

  They are conveniences, not data: losing them only resets defaults.
- **Keyboard:**
  - Escape stops reading.
  - Ctrl+Shift+Space pauses or resumes reading. When nothing is being read on
    the Ask page, it starts or stops dictation.
  - Ctrl+Shift+↑/↓ changes speed.

  These keys avoid screen-reader modifiers (Insert, CapsLock), are listed in
  Profile, and are exposed through `aria-keyshortcuts`. A reading bar offers
  Pause, Stop, Slower and Faster, plus position and engine.
- Starting dictation stops any reading, so the app never records its own voice.

### Two engines, one policy

| Engine | Speech to text | Text to speech | Data flow |
|---|---|---|---|
| Browser | Web Speech `SpeechRecognition` | `speechSynthesis` | DocuMind never receives audio. Some browsers (Chrome) recognise speech on their vendor's servers; the UI says so. Most synthesis voices are local. |
| DocuMind | `POST /voice/transcribe` (MediaRecorder clip, ≤ 10 MB, ≤ 60 s) | `POST /voice/speech` (streamed MP3, ≤ 4096 characters, 11 voices) | Audio passes through memory to the speech provider (`SpeechToTextProvider` / `TextToSpeechProvider`; OpenAI adapter). **Never stored.** |

Each workspace has a **voice policy**, `workspaces.voice_policy` (migration
0008), that admins set in Settings:

- `enabled`: DocuMind voice and browser voice.
- `browser`: browser voice only; the server speech endpoints return 403
  `server_voice_not_allowed`.
- `disabled`: no voice; 403 `voice_disabled`, and the UI hides every voice
  control.

Changes are audited (`workspace.voice_policy_changed`). The deployment-wide
`SPEECH_PROVIDER` decides whether DocuMind voice exists at all; `none` is the
default. "Automatic" engine selection prefers DocuMind voice where allowed and
falls back to the browser.

Speech endpoints:

- need workspace membership: `conversations.use` for transcription,
  `documents.read` for speech;
- validate type, size, language, voice and speed;
- are rate-limited per user (`RATE_LIMIT_VOICE_*`);
- turn provider failures into 503 **before** any audio is streamed.

Transcribed text goes through the normal conversation endpoints, so there is
no separate authorization path.

### Spoken answers with sources

- Answers are read sentence by sentence.
- Markdown is stripped, and each `[n]` becomes "(Source: Vendor Policy, page
  3.)" using the spoken labels from Phase 5. Markers placed after the full
  stop stay with their sentence.
- While streaming, complete sentences are read as they arrive (markers
  dropped, since citations aren't verified yet). When the answer completes,
  "Sources. 1: …" is read.
- Stopping the answer also stops the voice.

### Listening mode for documents

The viewer's **Listen** opens reading controls:

- read a page, previous or next page, next section (the next page with a new
  heading), restart;
- **Summarize** the page or document: `POST /documents/{id}/summary`, a
  context of that page range in reading order with verified citations, using
  the same prompt and citation checks as answers;
- **Ask about it**.

Reading continues to the next page automatically. The current sentence is
highlighted and kept in view. Lines are spoken separately, so headings don't
run into the next sentence.

### Spoken confirmations

Confirmation dialogs and the 2FA step-up dialog fire a UI-kit `announce()`
event when they open. The voice layer speaks it only if the user turned voice
on. Screen readers already announce dialogs, so nothing is doubled for
non-voice users.

### Figure and chart descriptions

When `FIGURE_DESCRIPTIONS_ENABLED`:

- The PDF extractor captures raster images of at least 72 pt, in reading
  order, rendered as PNG including vector labels drawn over them. Icons and
  full-page scans (OCR handles those) are skipped. There is a per-document cap
  (`FIGURE_MAX_PER_DOCUMENT`).
- Before chunking, an `ImageDescriber` (an OpenAI vision model, versioned
  prompt `figure-describe-v1`) receives the image plus context: page, section
  and a neighbouring caption. It writes a 2–5 sentence description "for
  people who cannot see it". Text inside the image is treated as data.
- The description becomes a normal paragraph, "Figure: …", so it is
  embedded, searchable, citable and read aloud like any other text.
- Failures drop that figure only. Notes are recorded:
  `figures_described:count`, `figure_descriptions_failed:count`,
  `figures_not_described:count`.
- The local provider never invents content: it says an image exists, with its
  caption and size.

## Alternatives considered

- **Always-on voice or auto-play.** Rejected: it competes with screen readers,
  which blind professionals have tuned to their needs.
- **Storing recordings for quality review.** Rejected by default (privacy).
  If ever needed, it would be a separate, explicit, per-workspace opt-in.
- **Server-only voice.** Rejected: browser speech costs nothing, works without
  a provider, and lets confidential workspaces keep audio off DocuMind.
- **WebSocket streaming STT.** Deferred: push-to-talk clips are simpler and
  sufficient for questions.
- **Describing vector-drawn charts.** Deferred: they aren't images. Detecting
  them needs layout analysis; their text labels are already extracted.

## Consequences

- Audio minutes are a new cost. Usage tracking for them belongs to Phase 8.
- Voice quality in browser mode depends on the user's browser and OS.
- Real-user testing with blind and low-vision professionals remains required
  before release (Phase 9). The automated tests simulate speech APIs and
  can't judge comprehension.
