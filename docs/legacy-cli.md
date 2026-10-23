# ISC integration engineering platform

A runnable first slice of the architecture discussed in chat: evidence retrieval,
specialist planning and review, a deterministic change compiler, approval policy,
and checkpointed execution. Python 3.11+, standard library only.

**Version 0.1 is a local engineering prototype. Execution only updates a simulated
tenant in SQLite. It does not connect to SailPoint ISC.** The optional OpenAI mode
uses real model calls for the architect and independent reviewer; offline mode
uses explicit requirements and structural checks without making model calls.

## Run it

From this directory:

```bash
python -m unittest discover -s tests -v
python -m examples.demo
```

The demo creates a plan for the example HR source, displays the change, approves it
as `demo-operator`, executes it in the simulator, and writes
`work/demo-report.json`. Automatic approval is confined to this demonstration.
Running it again creates another run against the existing simulated state.

For a manual review/approval flow:

```bash
python -m isc_agents plan \
  --client example --tenant example_sandbox --environment sandbox \
  --prompt 'Simulate an authoritative delimited-file HR source configuration.' \
  --requirements examples/hr_source.json
```

Read the returned `plan`, including evidence, previous and desired configuration,
limitations and review findings. Copy the returned `id` and `digest` into the
commands below:

```bash
python -m isc_agents approve \
  --client example --tenant example_sandbox --environment sandbox \
  --run RUN_ID --digest PLAN_DIGEST --actor your-name

python -m isc_agents execute \
  --client example --tenant example_sandbox --environment sandbox \
  --run RUN_ID

python -m isc_agents show \
  --client example --tenant example_sandbox --environment sandbox \
  --run RUN_ID
```

Approval expires after one hour. To renew before execution, use `revoke` with the
same scope and run ID, then approve again. Rejected model reviews require a new
plan; there is no override command. Drift also requires a new plan and approval.

Global options go **before** the command:

```bash
python -m isc_agents --db work/another.db --knowledge knowledge plan --help
```

You can optionally install the CLI with `python -m pip install -e .`, then use
`isc-agents` instead of `python -m isc_agents`.

## Enable real planning agents

Configure `OPENAI_API_KEY` through your normal secret mechanism and choose a model
supporting the Responses API and structured JSON outputs. Then add:

```text
--provider openai --model YOUR_MODEL_ID
```

to the `plan` command. There is no automatic fallback to offline mode when a model
call fails. Each plan makes at most two model calls, with bounded output and a
90-second timeout per call. This prototype uses the public OpenAI API origin only;
Azure OpenAI and alternate providers are not implemented.

The architect receives the prompt, requirements and retrieved evidence. Supplied
requirements are binding: a model change to them is rejected. The independent
reviewer can reject unsupported requests or unresolved decisions. Review approval
is advisory and cannot authorize execution. Model agents receive no execution
tools or SailPoint credentials.

**Data handling:** OpenAI mode sends the prompt, source specification and selected
document contents to the provider with `store: false`. Use approved, sanitized
client data and your organization's provider policy. Offline mode sends nothing.
The local database and reports contain these artifacts in plaintext; do not put
credentials or employee account data in prompts or knowledge documents.

The provider path is covered with mocked HTTP tests. No live model request was
made while building this release because no credentials were configured.

## What is implemented

| Architecture element | Implementation |
| --- | --- |
| Orchestration | `Engine.plan`: research → architecture → compile → independent review |
| Research | Local keyword retrieval, content hashes, origin/version/trust metadata, evidence size budget |
| Architect | Model-backed role, or deterministic requirements validation in offline mode |
| Implementation | Deterministic compiler into one allowlisted simulated operation |
| Validation | Schema and business invariants; optional independent model review |
| Policy | Scope checks, exact-plan digest approval, expiry, revocation, explicit operator action |
| Workflow | SQLite transactions, persisted apply/verify checkpoints, resumable local execution |
| Tool gateway | One registered simulator operation; no arbitrary HTTP, shell, or dynamic tools |
| Audit | Persistent plan, model identity, evidence snapshots, review, approval and execution events |

Only a **delimited-file source specification** is supported. `authoritative` and
correlation fields record design intent. They do not create an ISC identity
profile, execute correlation, ingest a CSV, configure provisioning, or implement
joiner/mover/leaver behavior. The test suite verifies platform invariants, not
SailPoint business behavior.

Offline mode does not interpret arbitrary natural language: it validates and
compiles the requirements file. Its review explicitly records that limitation.
This is an engineering starting point, not a completed autonomous integrator.

## Add knowledge

Place curated JSON documents in `knowledge/` with these fields:

```json
{
  "id": "your-document-id",
  "title": "Connector prerequisites",
  "origin": "https://your-approved-source.example/document",
  "version": "your-reviewed-document-version",
  "trust": "client",
  "scope": {"client": "example", "tenant": "example_sandbox", "environment": "sandbox"},
  "content": "Approved text or an appropriately sized document chunk."
}
```

Trust values are `official`, `client`, `community`, and `fixture`. Client documents
are filtered by exact scope before retrieval. Scope must be provided for client
documents; missing scope excludes them. Global official/community/fixture
documents must contain no private client data. Labels are set by the trusted
local curator; the importer does not authenticate publishers. Do not let an agent
or untrusted user promote documents to official status.

Documents must be at most 32 KB. Retrieval uses up to five matching documents and
a total content budget of 24,000 characters. Oversized material should be chunked
with separate IDs. Every plan embeds the evidence snapshot and its hash.

The included document is explicitly a project-owned **fixture**, not a copy of
SailPoint documentation. There is no web crawler, embedding index or API-spec
ingestion in this release. Keyword relevance does not establish evidence quality
or freshness, so live execution must not rely on this retrieval implementation.

## Execution and recovery guarantees

Simulator mutation and the apply checkpoint commit in the same SQLite transaction.
A crash before commit rolls both back. A crash after commit resumes at verification
without reapplying. Concurrent local workers serialize mutations using
`BEGIN IMMEDIATE`. A stored expected-state hash prevents a stale plan overwriting
a changed source. Re-running a completed run returns its recorded result.

An already applied run may finish read-only verification after approval expires;
expiry blocks any new mutation. Verification compares the exact simulated desired
configuration. If another run changes it before verification, the workflow pauses
with an error. Inspect the state and create a new plan; there is no automatic
rollback or blind retry. The old configuration is retained in the plan for review.

These guarantees depend on mutation and checkpoint sharing one local database.
They do **not** imply exactly-once execution against a remote API. See
[the architecture and next milestones](docs/architecture.md) before adding live tools.

## Trust boundary

Run this CLI only as a trusted local operator. Scope strings are selectors, not
authenticated claims. Anyone with database access can edit approvals and events.
The audit log is persistent but not tamper-proof. Filesystem isolation, authenticated
authorization, a secrets broker, encrypted storage and centralized audit are
required before hosted or multi-user deployment. The `production` scope label still
uses simulation and cannot reach production ISC.

## Project layout

```text
isc_agents/
  agents.py       Role prompts and optional Responses API client
  cli.py          Trusted local operator interface
  engine.py       Compiler, policy, tool gateway and workflow
  knowledge.py    Scoped local evidence retrieval
  models.py       Strict JSON contracts and semantic validation
  store.py        SQLite state, checkpoints and events
examples/         Example requirements and runnable demonstration
knowledge/        Clearly labelled simulated evidence
tests/            Platform and provider contract tests
docs/             Architecture and production milestones
```
