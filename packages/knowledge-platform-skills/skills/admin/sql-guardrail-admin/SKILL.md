---
name: sql-guardrail-admin
description: Draft and explicitly activate a bounded SQL Guardrail in an authorized Platform Space.
operations:
  - POST /v1/database/guardrails
  - POST /v1/database/guardrails/{guardrail_id}:decision
---

# SQL Guardrail Admin

Install or expose this Skill only to a caller with explicit SQL Guardrail
Admin and Space scope.

Creating a rule stores a normalized, digest-bound definition in
`waiting_for_confirmation`; it does not change the active rule set. Use only
the supported rule types and bounded JSON parameters. Never send a local path,
credential, host task identifier or raw document as a rule parameter.

Show the rule definition, scope, action and digest before asking for a
decision. Confirm with the exact guardrail identifier, Space and expected
status. Reject or confirm is atomic and idempotent; only confirmation changes
the status to `active`. A draft or rejected rule must not be supplied to a
Database Query validator.
