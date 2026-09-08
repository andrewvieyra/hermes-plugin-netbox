# Security

## Reporting

Report vulnerabilities privately through GitHub's security advisory form for this repository
rather than a public issue. You will get an acknowledgement within a few days.

## Threat model in brief

- The API token is read from the environment at call time and never written to plan files, logs,
  or tool output. Plan files contain object data exactly as NetBox returned it; treat the
  `plugin-data` directory with the same care as NetBox itself.
- The model can only write through `netbox_apply` and `netbox_rollback`, and those only act on a
  saved plan. There is no free-form write tool.
- Deletes are disabled by default.
- Scope the NetBox token to the least privilege the agent needs. A read-only token turns this
  plugin into a read-only integration: planning still works, apply fails at the first write and
  has nothing to roll back.
- Sink credentials (for example a Splunk HEC token) are referenced as `${VAR}` placeholders in
  `config.yaml` and read from the environment at send time; the value is never written to disk by
  the plugin.
- Registration performs no network I/O. Hermes' Plugin Doctor verifies this.
