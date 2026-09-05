"""GeminiProvider using direct HTTP to Google AI Studio API (https://generativelanguage.googleapis.com/v1beta)."""

from collections.abc import Sequence
from typing import Any

import httpx

from app.llm.base import LLMProvider, LLMResponse, Message, ToolCall, ToolSchema, classify_llm_error


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, base_url: str | None = None, model: str | None = None, **kwargs: Any):
        self.api_key = api_key
        self.model = model or "gemini-2.0-flash"
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

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
        model_name = model or "gemini-2.0-flash"
        # Strip model prefix if passed (e.g. "gemini/gemini-2.0-flash" -> "gemini-2.0-flash")
        if "/" in model_name:
            model_name = model_name.split("/")[-1]

        system_instruction = None
        contents = []

        for m in messages:
            role = m.role if isinstance(m, Message) else m.get("role")
            content = m.content if isinstance(m, Message) else m.get("content")

            if role == "system":
                if content:
                    system_instruction = {"parts": [{"text": content}]}
                continue

            gemini_role = "user" if role == "user" else "model"
            parts = []
            if content:
                parts.append({"text": content})

            # Handle tool calls / responses if present
            if isinstance(m, Message) and m.tool_calls:
                for tc in m.tool_calls:
                    parts.append({
                        "functionCall": {
                            "name": tc.get("name") if isinstance(tc, dict) else tc.name,
                            "args": tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {}),
                        }
                    })

            if parts:
                contents.append({"role": gemini_role, "parts": parts})

        body: dict[str, Any] = {"contents": contents}
        if system_instruction:
            body["systemInstruction"] = system_instruction

        generation_config: dict[str, Any] = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if top_p is not None:
            generation_config["topP"] = top_p
        if generation_config:
            body["generationConfig"] = generation_config

        if tools:
            declarations = []
            for t in tools:
                if isinstance(t, ToolSchema):
                    declarations.append({
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    })
                else:
                    fn = t.get("function", t)
                    declarations.append({
                        "name": fn.get("name"),
                        "description": fn.get("description"),
                        "parameters": fn.get("parameters", {}),
                    })
            if declarations:
                body["tools"] = [{"functionDeclarations": declarations}]

        headers = {}
        if self.api_key.startswith("AQ."):
            headers["Authorization"] = f"Bearer {self.api_key}"
            url = f"{self.base_url}/models/{model_name}:generateContent"
        else:
            url = f"{self.base_url}/models/{model_name}:generateContent?key={self.api_key}"

        try:
            with httpx.Client(timeout=60.0, headers=headers) as client:
                res = client.post(url, json=body)
                if res.status_code != 200:
                    raise RuntimeError(f"Gemini API error ({res.status_code}): {res.text}")
                data = res.json()
        except Exception as exc:
            raise RuntimeError(classify_llm_error(exc)) from exc

        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini API returned no candidates.")

        candidate = candidates[0]
        content_obj = candidate.get("content", {})
        parts = content_obj.get("parts", [])

        text_parts = []
        tool_calls = []

        for p in parts:
            if "text" in p:
                text_parts.append(p["text"])
            if "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append(ToolCall(name=fc.get("name"), args=fc.get("args", {})))

        text = "".join(text_parts)

        usage_meta = data.get("usageMetadata", {})
        usage_dict = {
            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens": usage_meta.get("totalTokenCount", 0),
        }

        return LLMResponse(
            model=model_name,
            provider="gemini",
            text=text,
            content=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            finish_reason=candidate.get("finishReason"),
        )
