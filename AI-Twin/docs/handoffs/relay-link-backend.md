# Task: relay-link backend and local desktop

- Tool: Codex
- Branch: `ai/backend/relay-link`
- Starting commit: `1d42bdb79cbe87e1b41875d74aef532403f04bff`
- Created (UTC): 2026-09-10T06:47:47+00:00
- Status: implementation and local verification complete; awaiting integration
- Issue: #3

## Goal and acceptance criteria

Connect cloud transport with local business processing and the existing desktop UI while preserving normal account authentication, evidence gates, and atomic replay behavior.

## Scope, owner and dependencies

Backend and local desktop startup/profile handling belong to this task. The primary task owner handles relay transport and another task handles Android.

## Completed work

The `rules` model provider blocks model calls in the gateway, ignores cloud-approval headers during event ingestion, and disables background model consumers. Existing deterministic advice and mandatory-review holds remain in effect.

`POST /v1/relay/import` accepts normal account sessions only from direct loopback requests in rules mode. It supports goal/event/feedback operations and bounded goal/advice snapshots using `ai-twin.sync/v1`. The authenticated account determines tenancy. Business changes and replay receipts share one SQLite transaction; same-ID different-content requests return HTTP 409. Account deletion includes the new receipts.

`python desktop/local_mode.py run` provisions an independent Windows profile with a normal account, DPAPI-protected credentials, a loopback backend, and the original desktop UI. It does not inherit collection consent or alter the previously installed EXE. Named mutexes are isolated by explicit profile directory.

## Verification evidence

- Full backend suite: 541 passed; the focused import suite subsequently passed 14 tests after adding account-deletion coverage and strict JSON validation.
- Full desktop suite: 164 passed; the local-mode and single-instance suites subsequently passed 9 tests after adding profile isolation coverage.
- Application `examples/demo.py`: passed with synthetic data.
- `git diff --check`: passed.
- Tests use disposable directories and synthetic records. This task has not launched a real user profile or modified the deployed server.

## Remaining work and next step

The primary task owner integrates the Android and relay changes, provisions the private local profile, and tests actual transport with synthetic records. Read `desktop/LOCAL-MODE.md` for exact startup commands and the private `bridge-config.json` contract. The normal session endpoint is `/v1/session`.

Snapshots contain at most 100 goals and 100 advice records within 512 KiB. Truncation is explicit; pagination is not implemented. Arbitrary model-based questions remain disabled. Business SQLite is in the private user directory without extra database encryption; desktop cache and credentials use DPAPI. Expired/revoked sessions stop imports and require local account reauthentication.

## Handoff

The starting commit above is the last inherited completed commit. This record is committed with the implementation; obtain its own commit from Git history. The primary task owner reports actual deployment separately from source publication.
