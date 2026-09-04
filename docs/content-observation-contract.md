# Content observation contract

AI替身 (Your Personal AI Delegate) distinguishes observed content from facts about the account owner.
Text seen in a chat, search result, document, video, or application is evidence
with provenance; it is not automatically the owner's belief, commitment, or
problem.

## Required fact fields

Content-bearing events should add these keys under `facts`:

- `content_kind`: `chat`, `search`, `web_page`, `video`, `document`,
  `system_notice`, `app_ui`, `user_input`, `audio`, or `unknown`.
- `speaker`: `user`, `counterparty`, `author`, `system`, `assistant`, or
  `unknown`.
- `message_direction`: `inbound`, `outbound`, `self`, or `unknown`.
- `visible_only`: whether the observation is limited to content currently
  exposed on screen.
- `evidence_strength`: `direct`, `corroborated`, `contextual`, or `inferred`.
- `session_key`: a stable, non-secret identifier for one conversation, search
  task, page, video, or foreground work session.
- `content_hash`: a lowercase SHA-256 prefix or digest used for local and
  server-side duplicate suppression.
- `resolution_state`: `unknown`, `unresolved`, or `resolved`.

Clients must not put passwords, authentication tokens, payment-card data, or
private keys in these fields. A `session_key` must be derived from non-secret
metadata and must not contain a contact name, full URL query string, or message
body.

## Interpretation rules

1. `speaker=user` plus an explicit problem, commitment, decision, or request is
   direct evidence and may enter the immediate intervention path.
2. `speaker=system` is direct evidence only for an actual runtime or account
   state emitted by the system. Source code, documentation, and search results
   containing words such as "failed" or "deadline" are not runtime failures.
3. `speaker=counterparty` or `speaker=author` is contextual evidence about the
   owner unless another independent observation corroborates it.
4. A search query or video view is normally an interest or intent signal, not
   proof that the described condition has occurred.
5. Content fragments are grouped by `user_id + device_id + session_key` before
   semantic review. Repeated screen frames update the same window instead of
   generating repeated advice.
6. Immediate advice requires a direct owner/system fact or multiple independent
   corroborating observations. Context-only content may update goals, topics,
   and the next brief but must not interrupt by itself.

## Platform boundary

Collectors observe only user-authorized, currently visible, played, shared, or
imported content. They do not bypass application sandboxes, secure windows,
password controls, DRM, or platform capture consent. A failed or expired
capture grant must be surfaced to the user; it must never be reported as active
collection.
