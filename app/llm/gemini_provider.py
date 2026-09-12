"""GeminiProvider using official google-genai SDK."""

from collections.abc import Sequence
from typing import Any

from google import genai
from google.genai import types

from app.llm.base import LLMProvider, LLMResponse, Message, ToolCall, ToolSchema, classify_llm_error


def _function_declaration(
    name: str | None, description: str | None, parameters: dict[str, Any] | None
) -> dict[str, Any]:
    """One Gemini functionDeclaration from an OpenAI-style tool schema.

    Gemini rejects an OBJECT-typed `parameters` with no properties
    ("should be non-empty for OBJECT type", HTTP 400) — and it rejects the whole
    request, not just that declaration, so a single no-argument tool in the list
    (`crm_list_campaigns`, `finance_list_accounts`, …) breaks every call. For a
    tool that takes no arguments the field is omitted entirely.
    """
    declaration: dict[str, Any] = {"name": name, "description": description}
    params = parameters or {}
    if params.get("properties"):
        declaration["parameters"] = params
    return declaration


def _is_function_call(part: dict[str, Any]) -> bool:
    return isinstance(part, dict) and "functionCall" in part


def _drop_trailing_unanswered_calls(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip trailing model turns left with calls but no results.

    Gemini rejects a history ending on a "model" turn (400: "Requests ending
    with a model turn are not supported"). A guardrail ending a run mid-batch
    leaves exactly that: an assistant turn declaring calls whose results never
    came, which would then poison every later turn replaying the thread. This
    mirrors the OpenAI/Anthropic providers' unanswered-call cleanup — strip
    the orphaned calls (responses always follow their calls, so a *trailing*
    call is by definition unanswered) and drop turns left empty.
    """
    while contents and contents[-1].get("role") == "model":
        last = contents[-1]
        kept = [p for p in last.get("parts", []) if not _is_function_call(p)]
        if kept:
            last["parts"] = kept
            break
        contents.pop()
    return contents


class GeminiProvider(LLMProvider):
    DEFAULT_MODEL = "gemini-3.6-flash"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = base_url

        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["http_options"] = {"baseUrl": base_url}
        self.client = genai.Client(**client_kwargs)

    def chat(
        self,
        messages: Sequence[Message | dict[str, Any]],
        tools: Sequence[ToolSchema | dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        # The caller's saved model/decoding settings are the defaults; a per-call
        # argument overrides them (never the other way round).
        model_name = model or self.model
        # Strip model prefix if passed (e.g. "gemini/gemini-3.6-flash" -> "gemini-3.6-flash")
        if "/" in model_name:
            model_name = model_name.split("/")[-1]

        if temperature is None:
            temperature = self.temperature
        if max_tokens is None:
            max_tokens = self.max_tokens

        system_instruction = None
        contents = []

        for m in messages:
            if isinstance(m, Message):
                role = m.role
                content = m.content
                raw_calls = list(m.tool_calls or [])
                name = str(getattr(m, "name", "") or "")
            else:
                role = str(m.get("role", ""))
                content = m.get("content")
                raw_calls = list(m.get("tool_calls") or [])
                name = str(m.get("name", "") or "")

            if role == "system":
                if content:
                    system_instruction = content
                continue

            if role == "tool":
                # Tool execution result is always a "user" turn for Gemini:
                # a named result becomes a functionResponse, an unnamed one
                # plain text. (A previous mapping sent these as "model"
                # turns, and a history ending on one fails with 400
                # "Requests ending with a model turn are not supported".)
                parts = []
                if name:
                    parts.append({
                        "functionResponse": {
                            "name": name,
                            "response": {"result": content or ""},
                        }
                    })
                elif content:
                    parts.append({"text": content})
                if parts:
                    contents.append({"role": "user", "parts": parts})
                continue

            if role == "assistant" and raw_calls:
                # Assistant tool calls become functionCall parts on a "model"
                # turn (plus any text). Entries typed as tool results are
                # routed to a following "user" turn instead.
                call_parts = []
                result_parts = []
                if content:
                    call_parts.append({"text": content})
                for tc in raw_calls:
                    if isinstance(tc, dict):
                        if tc.get("type") == "tool_result":
                            result_parts.append({
                                "functionResponse": {
                                    "name": str(tc.get("name", "")),
                                    "response": {"result": tc.get("content") or ""},
                                }
                            })
                            continue
                        tc_name = str(tc.get("name", ""))
                        tc_args = tc.get("args") or tc.get("input") or {}
                    else:
                        if getattr(tc, "type", None) == "tool_result":
                            result_parts.append({
                                "functionResponse": {
                                    "name": str(getattr(tc, "name", "")),
                                    "response": {"result": getattr(tc, "content", "") or ""},
                                }
                            })
                            continue
                        tc_name = str(getattr(tc, "name", ""))
                        tc_args = getattr(tc, "args", {}) or {}
                    call_parts.append({"functionCall": {"name": tc_name, "args": tc_args}})
                if call_parts:
                    contents.append({"role": "model", "parts": call_parts})
                if result_parts:
                    contents.append({"role": "user", "parts": result_parts})
                continue

            # Plain user/assistant turns. Gemini names the assistant role "model".
            if content:
                contents.append({
                    "role": "user" if role == "user" else "model",
                    "parts": [{"text": content}],
                })

        contents = _drop_trailing_unanswered_calls(contents)

        if not contents:
            raise ValueError(
                f"LLM provider contents are required: at least one user/assistant message with content needed. "
                f"Got model={model_name}, provider=gemini."
            )

        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if temperature is not None:
            config_kwargs["temperature"] = temperature
        if max_tokens is not None:
            config_kwargs["max_output_tokens"] = max_tokens
        if top_p is not None:
            config_kwargs["top_p"] = top_p

        if tools:
            declarations = []
            for t in tools:
                if isinstance(t, ToolSchema):
                    declarations.append(_function_declaration(t.name, t.description, t.parameters))
                else:
                    fn = t.get("function", t)
                    declarations.append(
                        _function_declaration(
                            fn.get("name"), fn.get("description"), fn.get("parameters")
                        )
                    )
            if declarations:
                config_kwargs["tools"] = [types.Tool(function_declarations=declarations)]

        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            raise RuntimeError(
                classify_llm_error(exc, provider="gemini", model=model_name)
            ) from exc

        text = getattr(response, "text", "") or ""
        tool_calls = []
        if getattr(response, "function_calls", None):
            for fc in response.function_calls:
                tool_calls.append(ToolCall(name=fc.name, args=fc.args or {}))

        usage_meta = getattr(response, "usage_metadata", None)
        usage_dict = {
            "prompt_tokens": getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0,
            "completion_tokens": getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0,
            "total_tokens": getattr(usage_meta, "total_token_count", 0) if usage_meta else 0,
        }

        finish_reason = None
        if getattr(response, "candidates", None) and response.candidates:
            finish_reason = getattr(response.candidates[0], "finish_reason", None)

        return LLMResponse(
            model=model_name,
            provider="gemini",
            text=text,
            content=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            finish_reason=str(finish_reason) if finish_reason else None,
        )

        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if temperature is not None:
            config_kwargs["temperature"] = temperature
        if max_tokens is not None:
            config_kwargs["max_output_tokens"] = max_tokens
        if top_p is not None:
            config_kwargs["top_p"] = top_p

        if tools:
            declarations = []
            for t in tools:
                if isinstance(t, ToolSchema):
                    declarations.append(_function_declaration(t.name, t.description, t.parameters))
                else:
                    fn = t.get("function", t)
                    declarations.append(
                        _function_declaration(
                            fn.get("name"), fn.get("description"), fn.get("parameters")
                        )
                    )
            if declarations:
                config_kwargs["tools"] = [types.Tool(function_declarations=declarations)]

        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            raise RuntimeError(
                classify_llm_error(exc, provider="gemini", model=model_name)
            ) from exc

        text = getattr(response, "text", "") or ""
        tool_calls = []
        if getattr(response, "function_calls", None):
            for fc in response.function_calls:
                tool_calls.append(ToolCall(name=fc.name, args=fc.args or {}))

        usage_meta = getattr(response, "usage_metadata", None)
        usage_dict = {
            "prompt_tokens": getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0,
            "completion_tokens": getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0,
            "total_tokens": getattr(usage_meta, "total_token_count", 0) if usage_meta else 0,
        }

        finish_reason = None
        if getattr(response, "candidates", None) and response.candidates:
            finish_reason = getattr(response.candidates[0], "finish_reason", None)

        return LLMResponse(
            model=model_name,
            provider="gemini",
            text=text,
            content=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            finish_reason=str(finish_reason) if finish_reason else None,
        )
