# Testing with blind and low-vision users (Phase 9, ADR 0011)

## Goal

Confirm that blind and low-vision professionals can study and question their documents with DocuMind
independently, and find what blocks or slows them. Automated audits can't measure this.

## Participants

- 6–8 people:
  - at least 3 who are blind and use a screen reader daily (a mix of NVDA, JAWS and VoiceOver);
  - at least 2 with low vision (magnification at 200–400%, high contrast or dark mode);
  - at least 1 who mainly uses voice input.
- A mix of professions that read long documents (legal, compliance, research, education).
- Paid at a fair rate, recruited through accessibility organizations, with informed consent.
  Sessions are recorded only with consent.

## Sessions

Remote sessions of 60–75 minutes, on the participant's own setup.

1. **Warm-up (5 min).** Their tools and settings.
2. **Tasks (45 min), thinking aloud, no help unless they're stuck for more than 3 minutes:**
   1. Create an account and sign in.
   2. Upload a supplied PDF contract, and tell us when it is ready.
   3. Ask "How much notice is needed to terminate?", and find the page it came from.
   4. Ask a follow-up question by voice.
   5. Listen to page 2 of the document, pause, then summarize pages 1–3.
   6. Find how many questions the workspace has left this month.
   7. (Admins) Invite a colleague as a viewer.
3. **Debrief (15 min).**
   - System Usability Scale.
   - What was hardest, what they would pay for, and what they would tell a colleague.

## Measures

- Task completion: unaided, with help, or failed. Time on task, and errors.
- Number of times the participant lost their place, or the speech overlapped.
- SUS score (target ≥ 75), and confidence ratings.
- Every issue gets a severity:
  - **blocker:** the task can't be done;
  - **major:** the task is done with significant workaround or frustration;
  - **minor:** friction.

## Exit criteria for Phase 9

- No open blockers.
- Majors are fixed, or have a dated plan.
- Results are summarised in `docs/accessibility/user-testing-results.md`, with consented quotes only
  and no personal data.

Status: **not yet run.** It needs recruited participants and real devices, which aren't available in
this development environment.
