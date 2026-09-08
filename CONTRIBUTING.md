# Contributing

Thanks for helping. This plugin ships as a standalone repository by design: Hermes does not merge
third-party product integrations into its core tree, so this is the place for NetBox work.

## Ground rules

- Keep the engine (`client`, `diff`, `planner`, `executor`, `store`) free of Hermes imports at
  module load. That is what keeps it testable without a Hermes checkout.
- Every write path needs a journaled inverse and a test that exercises its rollback.
- Never hard-code NetBox model names or field lists. Infer from the API representation.
- Handlers return JSON strings and never raise.
- Match the existing style: type hints, short docstrings explaining *why*, no dead code.

## Workflow

```bash
git clone https://github.com/andrewvieyra/hermes-plugin-netbox ~/.hermes/plugins/netbox
cd ~/.hermes/plugins/netbox
make test
make lint
make doctor                       # needs the hermes CLI on PATH
HERMES_AGENT_ROOT=~/.hermes/hermes-agent make test-hermes
```

`make test` runs against the fake NetBox in `tests/fake_netbox.py`. If you need a behaviour the
fake does not model, extend the fake rather than mocking around it; keep it faithful to what NetBox
actually returns.

To test against a real NetBox, `netbox-docker` is the quickest path. Point `NETBOX_URL` and
`NETBOX_TOKEN` at it and use `/netbox status` from a Hermes session.

## Pull requests

- One change per PR with a short description of the behaviour, not the diff.
- Add a line under `[Unreleased]` in `CHANGELOG.md`.
- CI must be green: tests on every supported Python, ruff clean.

## Reporting security issues

See [SECURITY.md](SECURITY.md).
