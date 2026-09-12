# Plan Mk6 — Official Google GenAI SDK Integration for Gemini Provider

## Objective
Migrate `GeminiProvider` from direct `httpx` HTTP requests to the official Google GenAI Python SDK (`google-genai`), ensuring seamless support for Google AI Studio API keys (`AIza...`) and any valid Gemini credentials while maintaining strict isolation so that all other providers (`openai`, `anthropic`, `groq`, `openrouter`, `custom_openai`) remain completely unaffected.

---

## Phases & Steps

### Phase 1: Dependency & Specification
1. **Dependency Addition**: Add `google-genai` to `pyproject.toml` dependencies.
2. **SDK Verification**: Verify `google-genai` client initialization (`from google import genai`) and type definitions (`from google.genai import types`).

### Phase 2: Refactoring `GeminiProvider` (`app/llm/gemini_provider.py`)
1. **Client Initialization**:
   - Instantiate `genai.Client(api_key=api_key)` (or handle base URL / extra kwargs if provided).
2. **Message Translation**:
   - Convert project `Message` objects / dictionaries into contents structure suitable for `client.models.generate_content`.
   - Separate system instructions (`system_instruction`) into `types.GenerateContentConfig`.
3. **Tool & Configuration Translation**:
   - Translate OpenAI-style tool schemas into Gemini function declarations / `types.Tool`.
   - Handle no-argument tools gracefully (omitting empty `parameters` properties to prevent API 400 errors).
   - Map decoding config (`temperature`, `max_output_tokens`, `top_p`) to `types.GenerateContentConfig`.
4. **Response Parsing**:
   - Extract text response, tool calls (`function_call`), usage metadata (`promptTokenCount`, `candidatesTokenCount`, `totalTokenCount`), and finish reason.
   - Wrap any SDK exceptions in `classify_llm_error` for clean HTTP 502 propagation.

### Phase 3: Isolation & Safety Verification
1. **Provider Isolation**: Ensure `OpenAIProvider`, `AnthropicProvider`, `GroqProvider`, `OpenRouterProvider`, and `CustomOpenAICompatibleProvider` do not import or reference `google-genai`.
2. **Error Handling & Precedence**: Preserve user BYOK precedence chain (`MemberStore` active key > global config > MockLLM).

### Phase 4: Unit Testing & Verification (`tests/test_llm.py`)
1. **Mock SDK Tests**: Update existing Gemini unit tests in `tests/test_llm.py` to mock `google-genai` client responses (`client.models.generate_content`).
2. **Regression & Quality Checks**: Run pytest test suite (`pytest`) and linter (`ruff check .`) to ensure 100% test pass rate and clean code.
