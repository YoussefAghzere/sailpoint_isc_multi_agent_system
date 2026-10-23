# Model profiles and local inference

## Qwen or Llama on the same Mac

1. Install llama.cpp and download an instruction-tuned GGUF that fits your machine.
   Studio does not download weights or start the model server automatically.
2. Launch the model in a separate terminal:

   ```bash
   brew install llama.cpp
   llama-server -m /path/to/model.gguf --alias qwen --jinja \
     --host 127.0.0.1 --port 8080 --ctx-size 32768 --parallel 1
   ```

3. Run Studio on the same Mac, sign in, and open **Models & appearance**.
4. Create a profile: provider **Local · llama.cpp**, model **qwen**, URL
   **http://127.0.0.1:8080/v1**, no API key unless you configured server authentication.
5. Click **Save & use model**, then **Test saved connection**. The test generates a
   tiny schema-constrained response without tenant data. It verifies reachability and
   structured output, not the quality of integration reasoning.
6. Select your tenant environment and create a plan in Workspace. The same model also
   performs the independent review. Tenant credentials remain in the API gateway.

Qwen and Llama are model families; llama.cpp is the inference server. The alias in
the UI must match your server's alias. You may call a Llama profile `llama` instead;
set the server's `--alias llama` and the profile model ID accordingly.

Use recent llama.cpp with `/v1/chat/completions`, `response_format` JSON-schema support
and the model's correct chat template. The adapter uses
`{"type":"json_object","schema":...}` and, by default,
`chat_template_kwargs: {"enable_thinking": false}` for local structured actions.
These options are documented in the official
[llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
Thinking disablement can be changed in advanced profile options.

The local default is 48,000 input characters and 4,096 output tokens. Characters are
not tokens: choose a context window large enough for input, chat-template overhead and
output. Older tool-result history can be dropped to fit the profile, but the platform
does not silently truncate the user's request. Oversized inputs and incomplete/invalid
responses fail planning without approving or executing tenant changes. Smaller models
may satisfy JSON grammar while still producing poor plans; engineer review remains necessary.

## Cloud alternatives

Profiles support native provider protocols:

| Profile | Endpoint | Credential | Structured output |
| --- | --- | --- | --- |
| OpenAI | `/v1/responses` on `api.openai.com` | Bearer API key | Strict JSON schema |
| Claude | `/v1/messages` on `api.anthropic.com` | `x-api-key` | Forced `structured_result` tool with an input schema |
| Gemini | `/v1beta/models/{id}:generateContent` on `generativelanguage.googleapis.com` | `x-goog-api-key` | JSON MIME type and `responseJsonSchema` |
| Local | `/v1/chat/completions` on your saved server | None, or optional server token | llama.cpp JSON schema |

Choose a current model ID supported by your account and the requested structured
output features. The app does not guess the newest model or silently switch models.
All returned outputs receive the same server-side JSON schema validation. Refusals,
truncation and malformed responses fail safely. Cloud error bodies and API keys are
not echoed into job errors. Claude's structured-result tool is a response format;
it cannot directly execute SailPoint actions.

## Storage, selection and network boundaries

- Profiles and model selections belong to the authenticated user. Others cannot list,
  select, edit, test or delete them, including administrators acting as another user.
- Keys use the existing encrypted vault. GET responses never include keys; an empty
  key retains it on edits to the same provider. Changing provider never carries the
  previous provider's key to the new one. Changing a local server URL also does not
  forward its old stored key. Local profiles may have no key.
- A planning job records the selected profile ID, revision, provider and model. Editing
  it before dispatch rejects that job; selecting another profile does not retarget it.
- Cloud endpoint origins are fixed. Local endpoints permit loopback by default and
  only a root or `/v1` path. Credentials in URLs, query strings and arbitrary internal
  endpoints are rejected. Additional LAN/remote origins must be explicitly listed in
  `ISC_LLM_LOCAL_ORIGINS` by the server operator. TLS verification remains enabled.
- Localhost means the backend host. A browser on your Mac with a hosted backend cannot
  reach your Mac's model through the hosted backend's `127.0.0.1`. Co-locate the backend
  and model or configure an approved authenticated private connection.
- External research and live ISC operations are separate from inference. Local model
  selection does not make those calls offline or supply a Brave search key.

Existing `ISC_MODEL` plus `OPENAI_API_KEY` deployments remain supported as server
defaults. For a keyless local server default, use `ISC_LLM_PROVIDER=local`,
`ISC_MODEL=qwen` and optionally `ISC_LLM_BASE_URL`. UI profiles take precedence.

## Appearance

Light, Dark and System preferences are stored in browser localStorage, never in model
credentials. The saved preference applies before the first paint, synchronizes across
tabs, and tracks OS changes in System mode. If browser storage is blocked, the current
session still works with the OS preference. All controls use the same blue/navy tokens.

### Planning limits

Planning turns (4–32, default 16) controls orchestrator replies per plan, independently
of output tokens and input characters. Existing profiles use 16 when this field is
absent. Specialists retain their own three-turn limit and share an additional bounded
allowance of 12 model calls; the independent final review remains separate.
The last turn asks the model to finish or request missing information. Three identical
tool requests within one agent stop planning to avoid loops. Activity in Background
activity survives failed jobs and shows completed, rejected, and blocked actions,
without storing tool arguments or response bodies there.

For Qwen, start with 16 planning turns, 4096 output tokens, and 48000 input characters.
Raise turns toward 24 only for tasks making progress; more turns can increase latency
and cloud costs. Match input/output capacity to the context configured in llama.cpp.
A failed job must be submitted again; it does not resume automatically.
