# Official ISC API catalog

The API documentation entry point is https://developer.sailpoint.com/docs/api.
Studio imports the machine-readable unified ISC OpenAPI document published in
SailPoint's official repository:

https://raw.githubusercontent.com/sailpoint-oss/api-specs/main/dereferenced/deref-sailpoint-api.json

It is labelled **ISC API · resource versions** in the UI (`isc` internally).
That label is a catalog type, not a URL prefix. Its `info.version` is document
metadata; individual resources carry versions in their paths. For example, the
verified bundle exposes `listIdentitiesV1` at `/identities/v1`, and `getSourceV1`
at `/sources/v1/{id}`. Other resource versions and unversioned well-known paths
are used exactly as published. No `/isc`, `/v1`, or `/v2026` prefix is prepended.

**Knowledge & research → Sync official ISC API** imports and indexes this unified
catalog. It does not select the numerically newest year from a build workflow.
The human-readable documentation portal and machine-readable OpenAPI artifact
serve different purposes; Studio retains the artifact URL, checksum and fetch time.

## Updating an existing environment

1. Sync the official ISC API catalog and wait for the job to complete.
2. Edit the environment and select **ISC API · resource versions**. Review its allowed
   write operation IDs before saving: unified IDs can differ from legacy IDs (for
   example `createSourceV1` versus `createSource`). If needed, temporarily disable
   writes and clear the old allowlist so you can save and inspect the new catalog.
3. Inspect the catalog and reconfigure only the write operations you intend to allow.
4. Submit a fresh agent request. The agent discovers operations in that catalog.

Existing environment selections are not silently changed. Previous snapshots,
including legitimate year-versioned bundles such as v2026, remain available.
Changing an environment rotates its revision and invalidates old approvals.
Old API tools remain stored, but tools from another catalog snapshot are excluded
from the agent context and rejected if invoked. Review/recreate tools as needed.

The default retrieval evaluation cases target the unified catalog. Choose a fixture
matching the selected contract when evaluating a legacy snapshot. Synthetic tests
continue to use year-versioned contracts to check compatibility; those fixtures do
not determine which production API catalog the app imports.
