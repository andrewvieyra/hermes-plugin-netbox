# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
