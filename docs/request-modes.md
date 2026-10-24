# Requests, answers, and integration plans

Workspace offers **Automatic**, **Find an identity**, **Ask about documentation**,
and **Plan an integration or change**. Editable examples populate the task and inputs.

Automatic routing is deliberately conservative and uses patterns, not an extra LLM
call. For example, `Retrieve information about Youssef Aghzere`, `Who is Jane Doe?`,
and `Find identity Jane Doe` enter identity lookup. `Explain account correlation`
enters documentation answering. Other requests retain integration planning. Explicit
selection overrides automatic routing. Complex requests, access investigations, and
languages outside the supported English patterns should use the task selector.

## Identity lookup

Select a live ISC environment, choose **Find an identity**, and supply a full name,
email, or identity ID. A model profile and running llama.cpp server are not required.
The prompt's name can be extracted automatically; the dedicated search field takes
precedence when supplied.

The backend checks the selected environment's pinned contracts and performs:

1. `listIdentities`: exact name/alias, email, or first/last-name filters, with a bounded
   21-result page and `defaultFilter=NONE` when supported by the contract.
2. No results: report the lack of an exact match and suggest an email or identity ID.
   This does not prove absence from the tenant.
3. One result: retrieve `getIdentity` and verify the returned identity ID.
4. Multiple results: display at most 20 candidates and require selection. Selection is
   checked against the original answer and environment revision. It never chooses a
   person by guesswork. An identity ID query goes directly to `getIdentity`.

The **Answers & identity lookups** view shows common profile fields, including status
and manager when returned, API evidence, observation time, model-call count and elapsed
worker time. This is a profile lookup: it does not query accounts, entitlements, role
mining, or provisioning. Only allowlisted profile fields are shown. For v2026, first/last names are read from
`attributes`, email from `emailAddress`, and manager from `managerRef`; other custom
attributes are excluded from the summary. Search is
exact and bounded; spelling variants and complex names may require email/ID refinement.
No POST search exception or mutation authorization is added to the API gateway. OAuth
still obtains a token through its normal endpoint; the tenant operations are GETs.

Answer bodies and candidate lists use the vault's encryption at rest. List endpoints
and job results expose metadata and an answer ID, not employee details. The answer API
requires access to the selected environment. Queued reads reject environment revisions
changed since submission. A fresh lookup is required to refresh old answer data.

## Documentation questions

A saved model is required. The worker retrieves up to four scoped indexed documentation
chunks and generates one answer with citations. It drops lower-ranked chunks to fit the
profile's input-character limit. Citation IDs must refer to supplied evidence; this
check establishes source membership, not semantic correctness of every generated claim.
No relevant evidence produces guidance to ingest documentation, without a model call.
This mode does not query the tenant, execute integration actions, delegate agents, or
perform live web research. The external research option applies to integration planning.

## Integration planning

Integration requests continue through the bounded orchestrator, optional read-only
specialists, contract validation and independent model review. Approved execution stays
a separate action. A prompt instruction explicitly distinguishes identity profiles from
role mining and tells agents to discover technical IDs before asking users for them.
This improves guidance but does not guarantee correct reasoning from a small model.

## Performance and verification

Identity profiles use zero inference calls and normally one list plus one detail call,
while documentation answers use one inference call. Neither incurs the integration
planner's multi-turn orchestration or independent review. OAuth, network latency, rate
limits, and time waiting behind a running job still affect completion time; the worker
remains single-threaded. The UI polls once per second while a request is pending and every six seconds
otherwise; it prevents overlapping refreshes.

Parsed pinned OpenAPI documents and operation indexes are cached per Catalog instance,
bounded to three documents. Metadata such as freshness is still read from the database.
Tenant identity responses are not cached as fresh reads. Answers record elapsed worker
time; integration action events also record individual model-call time. These timings
exclude queue wait and should not be interpreted as a guaranteed speedup on a client's
hardware.

`python -m unittest discover -s tests -p test_requests.py` exercises routing, unique and
ambiguous identity outcomes, no matches, result bounds, encrypted storage, environment
scoping, changed environments, mismatched IDs, citations and parse-cache behavior using
synthetic API responses. These are outcome tests, not a measured evaluation of a real
Qwen model or a live tenant. UI interactions are covered in `tests/models-ui.test.cjs`.
