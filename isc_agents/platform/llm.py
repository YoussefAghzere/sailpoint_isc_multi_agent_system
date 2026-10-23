"""Provider adapters with one validated JSON contract and no execution permissions."""
import json
import os
import re
from urllib.parse import quote, urlsplit

import httpx
import jsonschema

from ..models import Rejected, canonical
from .network import Ambiguous


PROVIDERS = {
    "local": {"label": "Local · llama.cpp (Qwen / Llama)", "key_env": None},
    "openai": {"label": "OpenAI", "key_env": "OPENAI_API_KEY"},
    "anthropic": {"label": "Claude · Anthropic", "key_env": "ANTHROPIC_API_KEY"},
    "gemini": {"label": "Gemini · Google", "key_env": "GEMINI_API_KEY"},
}


def local_base_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise Rejected("Invalid local model server URL") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in {"", "/v1"}):
        raise Rejected("Use a llama.cpp server origin or /v1 URL, e.g. http://127.0.0.1:8080/v1")
    host = parsed.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    origin = f"{parsed.scheme}://{authority}" + (f":{port}" if port else "")
    allowed = {v.strip().rstrip("/") for v in os.environ.get("ISC_LLM_LOCAL_ORIGINS", "").split(",") if v.strip()}
    if host not in {"localhost", "127.0.0.1", "::1"} and origin not in allowed:
        raise Rejected("Local models must use loopback. Remote/LAN origins require ISC_LLM_LOCAL_ORIGINS on the server")
    if port == 0:
        raise Rejected("Choose a valid model server port")
    return origin + "/v1"


def validate_config(config):
    if set(config) - {"provider", "model", "base_url", "max_output_tokens", "max_input_chars", "disable_thinking", "max_planning_turns"}:
        raise Rejected("Unknown model configuration fields")
    provider = config.get("provider")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise Rejected("Choose OpenAI, Claude, Gemini, or local llama.cpp")
    model = config.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model):
        raise Rejected("Provide the model ID or the alias configured in llama.cpp")
    model = model.strip()
    if provider == "gemini":
        model = model.removeprefix("models/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", model):
            raise Rejected("Gemini model ID must be a model name, not a URL")
    turns = config.get("max_planning_turns", 16)
    if type(turns) is not int or not 4 <= turns <= 32:
        raise Rejected("Planning turns must be an integer from 4 to 32")
    output = config.get("max_output_tokens", 4096 if provider == "local" else 6000)
    budget = config.get("max_input_chars", 48000 if provider == "local" else 100000)
    if type(output) is not int or not 512 <= output <= 12000:
        raise Rejected("Output token budget must be 512–12000")
    if type(budget) is not int or not 16000 <= budget <= 100000:
        raise Rejected("Input character budget must be 16000–100000")
    if type(config.get("disable_thinking", True)) is not bool:
        raise Rejected("Thinking setting must be a boolean")
    if provider != "local" and config.get("base_url"):
        raise Rejected("Cloud provider endpoints are fixed; only local profiles accept a server URL")
    return {"provider": provider, "model": model,
            "base_url": local_base_url(config.get("base_url") or "http://127.0.0.1:8080/v1") if provider == "local" else "",
            "max_output_tokens": output, "max_input_chars": budget, "max_planning_turns": turns, "disable_thinking": config.get("disable_thinking", True)}


class ProviderModel:
    def __init__(self, http, config, api_key=""):
        self.http, self.config, self.api_key = http, validate_config(config), api_key
        self.provider, self.model = self.config["provider"], self.config["model"]
        self.max_input_chars = self.config["max_input_chars"]
        if self.provider != "local" and not api_key:
            raise Rejected("This cloud provider requires an API key. Local llama.cpp does not require a cloud account")

    def generate(self, role, context, schema=None):
        from .specialists import ACTION_SCHEMA, SYSTEM
        schema = schema or ACTION_SCHEMA
        text = canonical(context)
        if len(text) > self.max_input_chars:
            raise Rejected("Model context budget exceeded; narrow the request or increase the profile's input/context capacity")
        system = SYSTEM + "\nYour role: " + role
        limit = self.config["max_output_tokens"]
        if self.provider == "openai":
            url = "https://api.openai.com/v1/responses"
            headers = {"Authorization": "Bearer " + self.api_key}
            body = {"model": self.model, "instructions": system, "input": text, "store": False,
                    "max_output_tokens": limit,
                    "text": {"format": {"type": "json_schema", "name": "next_action", "strict": True, "schema": schema}}}
        elif self.provider == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
            body = {"model": self.model, "max_tokens": limit, "system": system,
                    "messages": [{"role": "user", "content": text}],
                    "tools": [{"name": "structured_result", "description": "Return the requested structured JSON only", "input_schema": schema}],
                    "tool_choice": {"type": "tool", "name": "structured_result"}}
        elif self.provider == "gemini":
            url = "https://generativelanguage.googleapis.com/v1beta/models/" + quote(self.model, safe="") + ":generateContent"
            headers = {"x-goog-api-key": self.api_key}
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": text}]}],
                    "generationConfig": {"maxOutputTokens": limit, "responseMimeType": "application/json", "responseJsonSchema": schema}}
        else:
            url = self.config["base_url"] + "/chat/completions"
            headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
            body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                    "max_tokens": limit, "stream": False, "temperature": 0.1,
                    "response_format": {"type": "json_object", "schema": schema}}
            if self.config["disable_thinking"]:
                body["chat_template_kwargs"] = {"enable_thinking": False}
        headers["Content-Type"] = "application/json"
        try:
            response = self.http.request("POST", url, headers=headers, json=body,
                timeout=httpx.Timeout(180 if self.provider == "local" else 90, connect=10), max_bytes=2_000_000)
        except (Rejected, Ambiguous):
            message = "Cannot reach llama.cpp. Launch llama-server on the same machine as Studio and check the saved URL" if self.provider == "local" else "Model provider request failed; check credentials, connectivity and provider availability"
            raise Rejected(message + ". No tenant action was executed") from None
        if response.status_code >= 400:
            raise Rejected(f"{PROVIDERS[self.provider]['label']} returned HTTP {response.status_code}; check the model ID, credentials and structured-output support")
        try:
            payload = response.json()
            if self.provider == "openai":
                parts = [p["text"] for item in payload.get("output", []) for p in item.get("content", []) if p.get("type") == "output_text"]
                if payload.get("status") != "completed" or len(parts) != 1:
                    raise ValueError()
                result = json.loads(parts[0])
            elif self.provider == "anthropic":
                parts = [p["input"] for p in payload.get("content", []) if p.get("type") == "tool_use" and p.get("name") == "structured_result"]
                if payload.get("stop_reason") != "tool_use" or len(parts) != 1:
                    raise ValueError()
                result = parts[0]
            elif self.provider == "gemini":
                candidates = payload.get("candidates", [])
                if len(candidates) != 1 or candidates[0].get("finishReason") != "STOP":
                    raise ValueError()
                result = json.loads("".join(p["text"] for p in candidates[0].get("content", {}).get("parts", []) if "text" in p and not p.get("thought")))
            else:
                choices = payload.get("choices", [])
                if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
                    raise ValueError()
                result = json.loads(choices[0]["message"]["content"])
            jsonschema.validate(result, schema)
            return result
        except (ValueError, TypeError, KeyError, AttributeError, jsonschema.ValidationError):
            raise Rejected("Model output was invalid, incomplete or refused. Select a model supporting JSON schemas, check its context window, and retry planning") from None
