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
            role = m.role if isinstance(m, Message) else m.get("role")
            content = m.content if isinstance(m, Message) else m.get("content")

            if role == "system":
                if content:
                    system_instruction = content
                continue

            # Determine the Gemini role: tool results are always sent as "user" role.
            # Regular user/assistant turns keep their role; Gemini maps "assistant" -> "model".
            is_tool_result = isinstance(m, Message) and m.tool_calls is not None and any(
                tc.get("type") == "tool_result" if isinstance(tc, dict) else hasattr(tc, "type") and tc.type == "tool_result"
                for tc in (m.tool_calls or [])
            )

            gemini_role = "user" if is_tool_result or role == "user" else "model"
            parts = []
            if content:
                parts.append({"text": content})

            # Handle tool calls / responses if present
            if isinstance(m, Message) and m.tool_calls:
                for tc in m.tool_calls:
                    # tc may be a dict with {"type": "tool_result", "name": ..., "args": ..., "content": ...}
                    # or {"name": ..., "args": ..., "type": "function", ...}
                    if isinstance(tc, dict) and tc.get("type") == "tool_result":
                        # This is a tool result from the agent; send as functionResponse under "user" role
                        parts.append({
                            "functionResponse": {
                                "name": tc["name"],
                                "response": {"result": tc["content"]},
                            }
                        })
                    else:
                        # Regular function call from the assistant
                        parts.append({
                            "functionCall": {
                                "name": tc.get("name") if isinstance(tc, dict) else tc.name,
                                "args": tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {}),
                            }
                        })

                    if parts:
                        contents.append({"role": gemini_role, "parts": parts})
            elif isinstance(m, Message) and hasattr(m, "tool_calls") and m.tool_calls is not None:
                # Legacy/alternative path: treat as function calls only
                for tc in m.tool_calls:
                    if hasattr(tc, "type") and tc.type == "tool_result":
                        parts.append({
                            "functionResponse": {
                                "name": getattr(tc, "name", ""),
                                "response": {"result": getattr(tc, "content", "") or ""},
                            }
                        })
                    else:
                        parts.append({
                            "functionCall": {
                                "name": getattr(tc, "name", ""),
                                "args": getattr(tc, "args", {}),
                            }
                        })
                    if parts:
                        contents.append({"role": gemini_role, "parts": parts})
            elif isinstance(m, Message) and getattr(m, "role", None) == "tool":
                # Backwards-compat: treat a Message with role="tool" as a function result response
                name = getattr(m, "name", "")
                tool_content = m.content or ""
                if name:
                    parts.append({
                        "functionResponse": {
                            "name": name,
                            "response": {"result": tool_content},
                        }
                    })
                else:
                    parts.append({"text": tool_content})
                if parts:
                    contents.append({"role": "user", "parts": parts})
            else:
                # Regular user/assistant message (dict or Message without tool calls):
                # parts was already set up with content at lines 106-108 above.
                # Append to contents now if parts has content.
                if parts:
                    contents.append({"role": gemini_role, "parts": parts})

            if parts and gemini_role == "model" and not is_tool_result:
                # Regular assistant turn with content + possible function_calls already appended above
                # No extra append needed; parts were already added inside the loop per message.
                pass

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
