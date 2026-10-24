# Agent-selected tools and Python execution

The selected LLM chooses its next action on every turn. The backend validates and
executes capabilities, returns observations, and lets the model decide what to do next.
The request is not mapped to a business-specific procedure by server-side heuristics.

## Enable Python on your Mac

1. Install and start Docker Desktop. Studio and llama.cpp keep their existing setup.
2. From this repository, build the minimal local runtime image:

   ```bash
   docker build -t isc-agent-python:local -f sandbox/Dockerfile .
   ```

3. Restart Studio after updating the code. Ask, for example:
   `Write and run Python to summarize these counts by department: IT=4, HR=2, IT=3.
   Show the computed result and execution evidence.`
4. Follow the agent's actions in Background activity. Source and execution status
   appear in **Tools & specialists → Python artifacts**. **View execution result**
   shows stdout, stderr, JSON result, source hash, image and execution ID.

The image contains Python's standard library. The agent cannot download dependencies or
select its own image. An operator may build a reviewed image with extra dependencies
and set `ISC_PYTHON_IMAGE`. The default is `isc-agent-python:local`; execution uses
`--pull=never`, so model output cannot cause an implicit image download. No Docker
or missing image produces a tool error that the agent can report or work around using
other available tools. There is never a host-Python execution fallback.

Server settings:

| Setting | Default | Purpose |
| --- | --- | --- |
| `ISC_ENABLE_PYTHON` | `1` | Set `0` to disable generated-code execution |
| `ISC_PYTHON_IMAGE` | `isc-agent-python:local` | Operator-controlled local runtime image |
| `ISC_PYTHON_DOCKER_HOST` | `unix:///var/run/docker.sock` | Local Unix socket; TCP/remote Docker endpoints are rejected |

If Docker Desktop uses a per-user socket, set `ISC_PYTHON_DOCKER_HOST` to its Unix
URL, for example `unix:///Users/YOUR_USER/.docker/run/docker.sock`, before starting
Studio.

Docker contexts/TLS endpoint selectors are removed from the child CLI environment so
they cannot redirect execution to a different daemon. Registry configuration stays
with the trusted Docker client and is never mounted into the generated-code runtime.

## Tool choices

`create_tool` accepts a name, description and a discovered operation ID. It creates a
scoped declarative API wrapper. A non-deprecated GET can be activated and invoked in
that request. Write wrappers remain pending review, and invoking one during reasoning
is rejected by the gateway. Creating a tool never increases tenant permissions.

`draft_code` saves a syntactically valid Python artifact without executing it.
`run_python` accepts a filename, source and optional JSON inputs, saves the source and
executes it in a fresh container. Code can use stdlib imports, `inputs`, and `tools`.
Set `result` to a JSON-serializable value and/or use `print`:

```python
import collections
counts = collections.Counter()
for row in inputs["rows"]:
    counts[row["department"]] += row["count"]
result = dict(counts)
```

The model can compose runtime reads through the SDK, for example:

```python
response = tools.call("read_api", {
    "operation_id": inputs["operation_id"],
    "arguments": inputs.get("arguments", {})
})
records = response["result"]["data"]
result = {"count": len(records), "evidence": response["observation_id"]}
```

The operation must come from the pinned catalog. SDK responses are envelopes with an
`observation_id` and a `result`. Available SDK capabilities are `catalog`, `research`,
`read_api`, `invoke_tool` and `validate_data`. The SDK uses the same environment,
external-research consent, API validation, GET-only reasoning policy and shared tool
budget as the agent. Code cannot directly call external URLs or read host CSV files;
JSON inputs and controlled tools are its data interfaces. CSV validation uses scoped
attachment IDs and returns quality summaries without employee values.

## Execution boundaries

Each invocation has:

- No network, host mounts, Docker socket, tenant credentials or cloud model keys.
- Non-root UID 65534, read-only root, dropped capabilities and no-new-privileges.
- 128 MiB memory, one CPU, 32 PIDs, bounded file descriptors and an 8 MiB temporary fs.
- A 30-second container deadline, 200 KB output limit, 100 KB line limit and eight
  proxied tool calls, which also consume the shared agent budget.
- Source up to 50,000 characters and JSON inputs up to 100,000 characters.

A watchdog removes timed-out containers. Proxied HTTP reads also have their own
bounded network timeouts; if one is already in flight, the host may finish handling
that read after the container deadline. The bridge cannot approve a plan, execute a
mutation, recursively launch Python, or exceed the environment's capabilities.

Execution outputs are encrypted in `code_runs`; retrieval requires environment access.
Draft source, status and source hash remain inspectable. Environment changes invalidate
in-flight results before publication. Output is treated as untrusted data, not proof
that the user's entire task succeeded. An agent can still make reasoning mistakes.
Docker isolation shares the host kernel; a hostile multi-tenant service should use a
separately administered runner or microVM boundary rather than broad Docker privileges
on the application host. The web application must be able to use its local Docker
socket; that privileged host capability is never exposed to generated code.

## Verification

```bash
python -m unittest discover -s tests
ISC_TEST_DOCKER=1 python -m unittest discover -s tests
node tests/models-ui.test.cjs
node tests/theme.test.cjs
```

The opt-in suite runs actual containers, including generated-code computation, SDK
round trips, host/secret/network isolation, time/output/tool limits, and an end-to-end
agent → Python container → controlled SailPoint gateway test with a synthetic tenant.
Scripted model responses test orchestration choices; they are not measurements of live
Qwen quality or successful integration against a client's tenant.
