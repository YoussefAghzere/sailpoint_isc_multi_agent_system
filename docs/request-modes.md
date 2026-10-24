# Agent requests and outcomes

All live natural-language requests go to the selected model's agent loop. There are
no identity-specific routes, keyword-selected API calls, or separate documentation
answer pipelines. Workspace examples only fill in editable prompt text.

The model sees the user's goal, public environment configuration, attachment metadata,
active tool manifests, and available capabilities. It chooses which tools to call;
server code validates arguments, permissions and budgets. It may:

- Search indexed documentation or perform opted-in web research.
- Discover API operations and inspect their pinned OpenAPI schemas.
- Call controlled GET APIs or existing active tools.
- Create a reusable API wrapper. GET wrappers can be used immediately; write wrappers
  remain pending review and cannot execute during reasoning.
- Generate Python, save it as a draft, or execute it in the isolated Docker runtime.
- Delegate bounded, read-only specialist work.
- Return an answer, ask clarification, or propose an executable workflow.

Answers cite observed tool IDs or retrieved sources. Citation checks establish source
membership, not semantic correctness of every generated claim. Uncited model guidance
is labelled unverified. Clarification questions support a follow-up reply in the UI;
the original request and reply go back through the same agent loop. No hidden lookup
recipe is introduced when the user chooses a person or provides an ID.

Proposed changes still require the workflow's validation and approval. Approved
execution uses the reviewed API change set. Python cannot bypass this gateway.

The model's size, quality, context capacity and inference speed affect correctness and
latency. Parsed API contracts remain cached. The model is instructed to choose short
paths for simple tasks and delegate only when useful. Timings, actions and tool
observations are visible; there is no zero-model-call shortcut for special requests.
The worker remains single-threaded, so queueing behind another task can add latency.

Old queued jobs from the retired fixed identity/documentation paths fail with a
resubmit message; they cannot make automatic API calls. Stored prior answers remain
available. Resubmit a prompt to use the new agent loop.

See [agent runtime](agent-runtime.md) for Python setup, boundaries and verification.
