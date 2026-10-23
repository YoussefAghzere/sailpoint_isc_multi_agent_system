# ISC Integration Studio

An authenticated workspace for SailPoint ISC integration engineering: selectable
client environments, encrypted credentials, documentation ingestion, contract-driven
API tools, bounded specialist agents, exact-plan approvals and persistent execution.

**Version 0.3 is a working engineering platform, not a certified autonomous integrator.**
Live API execution is implemented and tested against synthetic tenant HTTP responses.
No customer credentials were supplied, so no customer integration, correlation or
provisioning outcome has been verified. Start in a sandbox.

## Start the UI

Python 3.11+ is required. From this repository:

```bash
python -m pip install -e .
python -m isc_agents.platform.server serve
```

Open **http://127.0.0.1:8000**. Sign in as `admin`. On first initialization, a generated
password is written to `work/platform-v2/bootstrap-password.txt` with owner-only
permissions. Read it locally; never put it in an integration prompt. Alternatively,
provision `ISC_BOOTSTRAP_PASSWORD` before the first initialization. It does not reset
an existing account.

This workspace has a clearly labelled **Demo sandbox** environment and an imported
official v2026 API contract. A fresh checkout starts without environments. For an
offline demonstration, create a Simulator environment, submit the sample requirements,
inspect the plan, approve it, then execute it. Simulated changes never reach ISC.

## Configure real agents and environments

Open **Models & appearance** after signing in. Create a model profile, save it,
test its structured JSON connection, and select it in Workspace. Supported providers:

- **Local llama.cpp:** run an instruction-tuned Qwen or Llama GGUF on your own machine.
  No OpenAI account or cloud API key is required.
- **OpenAI, Claude, Gemini:** enter the appropriate provider API key and model ID.
  Keys are encrypted, write-only and private to your signed-in user.

For a local model on a Mac, in a separate terminal:

```bash
brew install llama.cpp
llama-server -m /path/to/model.gguf --alias qwen --jinja \
  --host 127.0.0.1 --port 8080 --ctx-size 32768 --parallel 1
```

Replace the file path with your downloaded model. Keep llama-server running. In
Studio, use provider **Local · llama.cpp**, model ID **qwen**, and base URL
**http://127.0.0.1:8080/v1**. Save and test. You can use the same setup with a Llama
GGUF; the model ID is the server alias, not a model download instruction.

The backend must run on the same machine to reach that machine's localhost. A remotely
hosted Studio cannot reach your Mac through its own localhost. Explicit remote/LAN
origins need server configuration via `ISC_LLM_LOCAL_ORIGINS`; do not expose an
unauthenticated llama.cpp server publicly. See [provider setup](docs/model-providers.md).

You can switch profiles without restarting the app. Jobs pin the chosen model and
profile revision when queued. Editing a queued job's profile requires a new plan;
switching your current selection does not retarget that job. Approved execution
continues to use the reviewed API change set; it does not call another model.

Optional server-level defaults and research settings:

| Setting | Purpose |
| --- | --- |
| `ISC_LLM_PROVIDER` | `local`, `openai`, `anthropic`, or `gemini`; default `openai` |
| `ISC_MODEL` | Provider model ID or local server alias |
| `ISC_LLM_BASE_URL` | Local server URL; default `http://127.0.0.1:8080/v1` |
| `ISC_LLM_API_KEY` | Optional local server authentication key |
| `OPENAI_API_KEY` | OpenAI server-default credential, only for OpenAI |
| `ANTHROPIC_API_KEY` | Claude server-default credential |
| `GEMINI_API_KEY` | Gemini server-default credential |
| `BRAVE_SEARCH_API_KEY` | Optional Brave internet search credential |
| `ISC_MASTER_KEY` | Recommended: externally supplied Fernet encryption key |
| `ISC_RESEARCH_HOSTS` | Optional additional exact HTTPS research hosts, comma separated |

Restart only after changing server environment variables. Saved UI profiles take
precedence over server defaults. Without a model, simulation and manually authored
live workflows remain available. There is no automatic paid-provider fallback.

**Appearance:** choose Light, Dark or System in the header or Models & appearance.
The blue/navy palette is inspired by Magellan Group. The choice persists in this
browser, applies to sign-in, and follows OS changes when System is selected.
Existing environments remain in `work/platform-v2/platform.db`; upgrading does not
rename the data directory or delete existing records.

In **Environments**, an administrator enters the name, client identifier, stage,
backend, tenant base URL, client ID, client secret, API contract and explicit write
operation allowlist. Credentials are encrypted and write-only. Blank client ID/secret
fields retain stored values. Named connector secrets are replaced only when submitted;
`{}` clears them. Use a least-privilege ISC API client.

Supported default tenant origins are `https://TENANT.api.identitynow.com` and
`https://TENANT.api.identitynow-demo.com`. Use the API hostname containing `.api.`,
not the SailPoint web UI hostname. The internal client identifier cannot contain
spaces (for example `magellan_lab`); it is separate from the OAuth Client ID. Other approved
suffixes require explicit server configuration through `ISC_TENANT_SUFFIXES`.
Client-credential OAuth uses the selected origin's `/oauth/token`. Tokens stay in
server memory and are not returned to agents. Discovery reads a bounded first page
of sources; agents can perform additional validated GET calls to resolve IDs and
capabilities. A published API does not establish tenant availability or authorization.

## Request lifecycle

1. Select an environment and describe the connector, business rules, ownership and
   acceptance criteria. Opt into external research only when permitted for the client.
2. The orchestrator retrieves evidence and API contracts. It can research approved
   pages/forums, perform GET calls, inspect schemas, validate uploaded CSV data, and
   delegate bounded tasks to approved specialists.
3. Missing client decisions produce clarification questions. Add answers to the
   prompt and create a new plan. Required identities and business rules are not guessed.
4. The compiler validates operations, arguments, references, permissions, secret
   handling and verification steps. Existing-resource PATCH/PUT operations capture
   a GET baseline; source creation checks for duplicate names.
5. An independent model reviewer assesses the request and proposal. Agent-authored
   live plans require fresh official evidence. Manual workflows receive structural
   checks and require authenticated human review.
6. A reviewer approves the exact plan hash. Production requires a different
   authenticated reviewer from the creator. Approval expires after one hour.
7. An operator requests execution. The worker performs persisted steps and evaluates
   expected-value checks. It cannot invent additional unapproved writes at execution time.

The platform supports serial workflows of up to twenty operations: JSON, JSON Patch,
CSV multipart requests, prior-output references, read-only polling and JSON Pointer
assertions. Connector configuration, schemas, correlation, identity-profile and
provisioning-related operations can be composed when present in the pinned contract
and explicitly permitted. This is not a prevalidated recipe library for every connector.
See [workflow contracts and recovery](docs/workflows.md).

## Documentation and internet

- **API ingestion:** discovers the newest published year-versioned bundle in
  [SailPoint's official repository](https://github.com/sailpoint-oss/api-specs), downloads
  it, stores a content-hash snapshot and indexes operation documentation. URL, version,
  checksum and retrieval time are retained. Environments remain explicitly pinned;
  refresh does not silently upgrade them. Beta/deprecated operations are disabled.
- **Product docs:** official seed URLs and XML sitemaps; robots rules; same-section
  links; page/response budgets; ETag and Last-Modified; chunking; persistent page
  checkpoints; optional daily refresh. JavaScript-only pages and PDF extraction are
  unsupported. Canonical URLs are required when destinations redirect.
- **Retrieval:** ranked SQLite FTS5 keyword search, environment-isolated client
  documents and API excerpts filtered to the pinned content hash. No vector index.
- **Internet:** optional Brave search returns cited snippets. Page fetches use an
  exact-host allowlist. Official/community evidence is labelled separately and cannot
  authorize an action. Search results cannot expand the fetch allowlist.

Default research hosts: `documentation.sailpoint.com`, `developer.sailpoint.com`,
`community.sailpoint.com`, `stackoverflow.com`. The runtime also needs HTTPS egress
to `raw.githubusercontent.com`, `api.search.brave.com`, the selected cloud provider
(`api.openai.com`, `api.anthropic.com`, or `generativelanguage.googleapis.com`), and the exact
ISC tenant origin. This workspace permits the official GitHub repository but does
not currently list the documentation, forum, model or search destinations. Configure
the runtime's supported network policy; application allowlists cannot override it.

Pinned specs and indexed evidence must be fresh within seven days. Changes to indexed
evidence used by an approved plan invalidate execution. Product pages are snapshotted
when retrieved; this does not prove an external page stayed unchanged afterwards.

## Dynamic agents and tools

The orchestrator can instantiate researcher, connector-architect and validator
specialists with independent contexts. Shared limits: five agents, depth two,
twelve model calls and twelve tool actions. The independent final reviewer adds
one call. Input-character and output budgets also apply. Specialists are read-only.

Agents can propose declarative API wrappers, validated against the pinned contract,
stored with a digest and activated by an administrator. They cannot grant permissions.
Agents can invoke activated GET wrappers; mutations require a normal approved workflow.
Workflow tool hashes are pinned and deactivation invalidates execution.

Generated Python drafts receive syntax checks and import reports, remain unexecuted,
and can be inspected/downloaded. Arbitrary generated code never runs with credentials.
An isolated generated-code test/deployment service remains a future extension.

## Security and recovery

- PBKDF2 passwords, expiring sessions, login throttling, logout, CSRF, strict same-site
  cookies, host checks and a restrictive content security policy.
- Administrator/operator/reviewer roles with per-environment grants. Only admins
  manage environments, users, ingestion and tool activation.
- Plans bind environment/credential revision, API hash, reviewed evidence and tool
  manifests. Environment changes invalidate approvals.
- Fernet-encrypted credentials and CSV files. Without `ISC_MASTER_KEY`, a protected
  local `vault.key` is generated. Back up keys separately. A co-located key does not
  protect against compromise of the server account.
- UTF-8 CSV uploads are environment-scoped and limited to 5 MB. Quality tools return
  row indexes/counts, not employee values. Secrets use `${secrets.NAME}` references and
  are resolved only in the gateway. Response redaction is heuristic, not a guarantee
  that every arbitrary field is non-sensitive.
- Audit events are hash-chained but not immutable against an administrator with
  database/server access. Export to independent immutable storage for production.
  Prompts, plans, documents and masked results rely on filesystem/disk protection;
  they are not field-encrypted.

OpenAI requests use `store: false`. Cloud profiles send selected context and evidence
to that provider under its policies. Local inference calls only the saved local model
endpoint; it does not forward a configured cloud key. Tenant API calls and opted-in
external research remain separate network activity. The UI describes this data flow.

Jobs, leases, steps and results are durable in SQLite. A `sending` checkpoint precedes
each remote mutation. Crashes, uncertain timeouts, server errors and invalid mutation
responses pause for read-only reconciliation; mutations are never automatically replayed.
Read-only calls have bounded retries and Retry-After handling. A shared environment
budget allows sixty HTTP attempts per minute, including OAuth and retries. Polling
checkpoints every attempt and is limited to twenty attempts.

Environment locks prevent overlapping workflows and remain held after failure because
earlier steps may have changed state. An administrator can confirm a mutation through
a fresh GET and exact expected result, then obtain fresh approval, or abandon the run
and release its lock. Abandonment does not undo previous effects. No automatic rollback
of provisioning is claimed. DELETE is disabled; non-DELETE mutations can still have
consequential effects and require substantive review.

## Checks, workers and deployment

```bash
python -m unittest discover -s tests -v
node --check isc_agents/platform/static/app.js
node tests/theme.test.cjs
node tests/models-ui.test.cjs
python -m isc_agents.platform.evaluate
python -m isc_agents.platform.server sync-specs
python -m isc_agents.platform.server worker --once
```

The UI server runs a background worker; `worker` without `--once` runs an additional
local worker continuously. Sandboxes restricting event-loop sockets may require local
socket permission for TestClient tests even though synthetic tenant tests use no internet.

For remote access, use a TLS reverse proxy, `ISC_PUBLIC_ORIGIN=https://your-host`,
`ISC_ALLOWED_HOSTS=your-host`, and the intended bind interface. Remote binding without
HTTPS configuration is refused. This does not configure TLS or SSO automatically.

Tests cover provider-specific protocols, keyless local inference, profile ownership,
encrypted model keys, queued-model pinning, database migration, appearance behavior,
authentication, CSRF, grants, encryption, API contracts, source workflows,
drift, approvals, polling, ambiguous outcomes, reconciliation, delegation, tools,
crawling, compression, multipart requests, CSV quality and the original CLI. Model
responses are mocked. Real connector business outcomes still need sandbox validation.
No real Qwen/Llama model or paid provider was available in this execution environment;
use the connection test to verify your selected running model before planning.

The six-case retrieval baseline records API selection recall against the exact pinned
specification hash. Extend `evaluations/api-retrieval.json` with regression cases for
your supported integrations; this is not a substitute for model and IAM outcome evaluation.

Before enterprise deployment: organization SSO/MFA, managed key/secret storage,
immutable external audit, backup/restore drills, distributed workflow coordination,
provider spend controls, operational monitoring and connector acceptance tests.
See [architecture and readiness boundaries](docs/architecture.md).

The original `python -m isc_agents` simulation CLI remains available. Its instructions
are preserved in [legacy-cli.md](docs/legacy-cli.md).
