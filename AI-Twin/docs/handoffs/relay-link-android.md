# Android relay link

- Issue: #3
- Executor: Codex Android subtask
- Branch: `ai/android/relay-link`
- Starting commit: `1d42bdb79cbe87e1b41875d74aef532403f04bff`
- Scope: `android/`; this task's handoff. Other platform implementations belong to their separate executors.

## Implemented

An explicit “电脑本地处理” activity is available from its launcher and the original login page. It uses its own encrypted SQLCipher/Room database, Keystore-protected connection settings, and record partition per relay origin / phone / processing computer. It does not claim or read another backend account's existing data.

The owner can submit goals, notes, reviewed text shares, and advice feedback. Text shares start as drafts and are contextual evidence unless the owner confirms a real problem. Notes use the backend's `thought.note` schema and explicit domain. Computer responses display goals, evidence, next steps, and advice state. No automatic sensitive collector is enabled by this entry.

Requests use `ai-twin.sync/v1`, stable application message IDs, and expiring transport IDs. Cloud upload does not mark application completion. A new transport is created after the 24-hour retry window or an expired upload reservation. Incoming results are bounded to 512 KiB / 100 records per list, checked against source / request / operation, and transactionally committed before cloud ACK. Duplicate or old responses cannot overwrite newer local records. Invalid application payloads are retained on the relay and do not block other messages.

Foreground polling, manual synchronization, WorkManager background scheduling and pause/resume use the same serialized pipeline and configuration-change fence. No original backend worker, database schema, account session or original synced marker is replaced.

## Verification

- Synthetic `examples/demo.py`: passed locally.
- `git diff --check`: passed locally.
- Added 18 JVM tests for protocol identity / shape / size / strict input and transport/configuration boundaries.
- Added 4 instrumentation tests for transactional receipts, replay, isolation and stale snapshot protection; requires a device or emulator.
- Android APK build and JVM suite: running during implementation; final results will be recorded before handoff.

## Remaining device validation

Install the APK on a test phone, enter private configuration, verify HTTPS connectivity and the goal → event → computer response → feedback cycle. Verify background behavior on that device's Android variant. Real phone authorization, automatic capture migration, raw audio handling and legacy record import are outside this explicit-input mode and are not claimed as completed.

Configuration files, credentials, real device names, server addresses, local data and logs are excluded from the repository. The final change's commit is located through Git history for this file.
