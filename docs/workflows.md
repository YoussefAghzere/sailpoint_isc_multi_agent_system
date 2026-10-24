# Workflow contracts and recovery

Live workflows contain `title`, `questions`, `steps` and `checks`. Nonempty questions
produce an unexecutable clarification artifact. Maximum length is twenty serial steps.

This example reads an existing source. Replace `SOURCE_ID` with an observed identifier:

```json
{
  "title": "Verify an existing HR source",
  "questions": [],
  "steps": [
    {"id": "read_source", "operation_id": "getSourceV1", "arguments": {"path": {"id": "SOURCE_ID"}}}
  ],
  "checks": [
    {"step_id": "read_source", "pointer": "/name", "equals": "Expected HR source name"}
  ]
}
```

This example uses the unified ISC catalog’s `getSourceV1` operation. Every plan
resolves IDs and paths against its selected contract; inspect that catalog instead
of assuming the names used by a legacy year-versioned bundle.

## Arguments and references

- Arguments may contain `path`, `query`, `body`, `form` and `files`. No custom headers,
  cookies, arbitrary URLs or executable expressions are exposed to the agent.
- `${steps.create_source.id}` references an earlier response's `id` field. It must
  occupy the entire JSON value. Numeric array indexes are supported.
- `${secrets.hr_password}` resolves a named environment secret only in the gateway.
  Inline credential-like values, including JSON Patch values, are rejected.
- `files: {"file": "ATTACHMENT_ID"}` references an encrypted, environment-scoped CSV.
  `form` fields are multipart even when no file is present.
- Optional `tool_id` pins an activated wrapper. Its operation, contract and manifest
  digest must match; deactivation prevents execution.

Schema checks for data-dependent values occur after outputs/secrets are resolved.
Resolved requests are always validated before transmission, but a later step can
fail after an earlier approved mutation. A workflow is not a remote atomic transaction.
Combine multiple static modifications to the same existing resource into one step.

## Polling and checks

A read-only step can include a bounded wait condition:

```json
{
  "id": "wait_for_task",
  "operation_id": "getTaskStatusV1",
  "arguments": {"path": {"id": "${steps.aggregate.task.id}"}},
  "wait": {
    "pointer": "/completionStatus",
    "equals": "SUCCESS",
    "fail_values": ["ERROR"],
    "max_attempts": 20,
    "interval_seconds": 10
  }
}
```

The reference, status field and values illustrate the format. Verify the actual
initiating response and status schema; different job APIs use different shapes.
Each poll is persisted and scheduled separately. Checks compare JSON Pointer values
against response data. Verify actual configuration/business outcomes, not just task IDs.

## Reconciliation

`needs_reconciliation` means a write may have happened. Do not submit it again as a
new plan until its outcome is understood. An administrator inspects state using a
fresh GET, then submits reconciliation JSON in the run detail:

```json
{
  "operation_id": "getSource",
  "arguments": {"path": {"id": "OBSERVED_SOURCE_ID"}},
  "expected": {"replace": "with the complete expected masked response data"},
  "decision": "confirm_applied"
}
```

The complete observed result must equal `expected`. The ambiguous step is checkpointed
with that evidence and needs fresh approval before continuation. The evidence must
be compatible with subsequent output references. The platform does not reconstruct
lost POST responses or guess new object IDs.

`decision: "abandon"` records inspected state and releases the environment lock.
It does not undo prior effects. Definitively failed runs also require inspected-state
abandonment before another workflow can use the locked environment.

Expired approvals can be revoked/renewed between steps. Revocation is refused while
a remote step is in flight. Environment changes require a new plan and approval.
