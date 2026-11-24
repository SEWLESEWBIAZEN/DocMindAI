# Screen-reader verification protocol (Phase 9)

Automated checks don't replace listening to the product. Axe runs on every page in light and dark mode,
alongside keyboard-only tests (`frontend/e2e/a11y.spec.ts`). This protocol is for testers to run by hand
with real screen readers. Record results in the table at the end. Every **Fail** gets an issue with the
exact announcement heard.

## Matrix

| Screen reader | Browser | Platform |
|---------------|---------|----------|
| NVDA (latest) | Firefox and Chrome | Windows 11 |
| JAWS (latest) | Chrome and Edge | Windows 11 |
| VoiceOver | Safari | macOS (latest) |
| VoiceOver | Safari | iOS (latest), 390 px |
| TalkBack (optional) | Chrome | Android |

Run each scenario with the voice features **off**, then **on** (Profile → Voice and reading), so
DocuMind's own speech and the screen reader are checked together.

## Scenarios and pass criteria

1. **Sign up and sign in.**
   - Every field announces its label, whether it is required, and its hint.
   - Errors are announced when they appear, and focus moves to the first invalid field.
   - The password-strength meter is announced as text, not only colour.
2. **Landmarks and navigation.**
   - The Skip to content link works.
   - Landmarks are announced: banner, navigation "Main", main and complementary.
   - Headings list (H key, rotor): one H1 per page, and a logical order.
3. **Upload a document.**
   - The file input is labelled, and the upload tray progress is announced (polite live region).
   - "Ready" status is reachable in the documents table, with a row header for each document.
4. **Ask a question.**
   - The composer is labelled.
   - While an answer streams, the screen reader doesn't read every token; it announces when the answer
     is complete.
   - Citations are read as "Source 1, vendor-policy, page 2…", using the spoken label rather than "[1]".
   - "Open source" moves focus to the highlighted passage in the viewer.
5. **Listening mode.**
   - Listen, pause and resume (Ctrl+Shift+Space) and stop (Escape) don't fight the screen reader.
   - The reading bar is reachable and announced.
6. **Dialogs.**
   - Archive, delete (2FA) and the AI-key step-up dialogs:
     - announce their title and description;
     - trap focus;
     - close with Escape;
     - return focus to the button that opened them.
7. **Menus.**
   - The workspace switcher and row actions announce "menu", the item count, and the selected item.
   - Arrow keys, Home/End and Escape behave as described.
8. **Usage page.**
   - Quota meters announce their label and value, for example "AI answer tokens, 12%".
   - The chart has a table alternative.
9. **AI providers and Infrastructure.**
   - Radio cards are announced as a radio group, with disabled reasons.
   - Validation results say "Passed" or "Failed" (not only a ✓ or ✕).
   - The worker token is announced and selectable.
10. **Errors and limits.**
    - A 402 quota message, a 503 "database can't be reached" message and a toast are each announced
      once, through the live regions.

## Results

| Date | Tester | SR / browser / OS | Scenario | Result | Issue |
|------|--------|-------------------|----------|--------|-------|
| | | | | | |
