# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-09-08

### Added
- `write_mode` setting: `full` (default), `operator_only` (only `/netbox` and `hermes netbox` may apply or roll
  back; model calls are refused with the exact command to relay), `read_only` (no writes; the apply and rollback
  tools are not registered for the model).
- Plan retention: `plan_retention_days` (default 90, 0 disables) with an hourly opportunistic sweep, plus
  `/netbox prune` and `hermes netbox prune [--days N] [--dry-run]`. Pruning is audited as `plans_pruned`.
- Cross-process claim lock: apply and rollback claim a plan under a file lock, so the gateway and the CLI cannot
  run the same plan at the same time. A held lock refuses with a retry hint rather than blocking indefinitely.
- Reports show creation, apply and rollback times in the Hermes-configured time zone with the zone name, and who
  acted (`model via signal for Andrew in dm Andrew`, `operator via cli (hermes)`). Storage stays UTC.
- `netbox_query` with endpoint `status` also reports the plugin version and write mode.
- Audit trail for SIEM ingestion: an actor record (model vs operator, platform, chat, user, session, triggering
  message) on every plan, apply and rollback; an `audit` block per plan (host, OS user, Hermes, plugin and NetBox
  versions); the HTTP method, path and status behind every journal entry; and an append-only `audit.jsonl` event
  stream covering creation, checks, refusals, rejections, every step and every revert. Settings `audit_log`,
  `audit_log_path`, `audit_include_request`. See `docs/audit.md`.

### Changed
- `netbox_apply` and `netbox_rollback` return a per-step summary instead of the full journal (snapshots and inverse
  payloads stay on disk; `netbox_plans` with a `plan_id` returns everything). Keeps large plans under Hermes' tool
  result budget.
- CI actions bumped to `actions/checkout@v5` and `actions/setup-python@v6`.
- `/netbox` and `hermes netbox` accept an unambiguous fragment of a plan id (`apply 4f1a`); ambiguous fragments list the candidates.
- A new plan file is created exclusively; if a generated id already exists on disk the plan gets a fresh id instead of overwriting.
- Plan ids use an ISO 8601 basic UTC timestamp: `nbp-20260908T193012Z-4f1a` instead of `nbp-20260908-193012-4f1a`. Existing plan files keep working; only newly created ids change.

## [0.1.0] - 2026-09-08

### Added
- `netbox_query`: filtered list, detail by id, `fields`/`brief`, automatic pagination, `status` check.
- `netbox_plan`: operation validation, target resolution by id or match, upsert via `create` + `match`,
  field-level diff with NetBox-aware equality, `last_updated` preconditions, persisted plan with a
  human-readable rendering.
- `netbox_apply`: preflight (status, age, URL, delete policy), per-step precondition re-check,
  journaled inverses, automatic reverse-order rollback on failure, `dry_run`.
- `netbox_rollback`: journal replay newest first, post-apply modification detection, `force`,
  re-create from snapshot with automatic dropping of fields NetBox rejects.
- `netbox_plans`: list and inspect plans.
- `/netbox` slash command and `hermes netbox` CLI with `status`, `plans`, `show`, `check`, `apply`, `rollback`.
- Bundled `netbox:workflow` skill.
- Operator settings: `allow_delete`, `max_operations`, `max_query_results`, `max_plan_age_hours`.
- Test suite with an in-memory fake NetBox and a loader test through Hermes' `PluginManager`.
