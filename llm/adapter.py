import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    provider: str


class LLMAdapter:
    def __init__(self):
        self.provider = os.getenv("LLM_PROVIDER", "gemini").lower()
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.smart_model = os.getenv("LLM_MODEL_SMART", "gemini-1.5-pro")
        self.fast_model = os.getenv("LLM_MODEL_FAST", "gemini-1.5-flash")

        if not self.api_key:
            raise EnvironmentError(
                "LLM_API_KEY is not set. Copy .env.example to .env and fill in your API key."
            )

        if self.provider == "gemini":
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        elif self.provider in ("openai", "anthropic", "azure-openai"):
            raise NotImplementedError(
                f"Provider '{self.provider}' is configured but not yet implemented. "
                "Only 'gemini' is supported. Set LLM_PROVIDER=gemini in .env."
            )
        else:
            raise ValueError(
                f"Unknown LLM provider: '{self.provider}'. Supported values: gemini"
            )

    def _model_name(self, model_tier: str) -> str:
        return self.smart_model if model_tier == "smart" else self.fast_model

    def complete(self, prompt: str, model_tier: str = "fast") -> LLMResponse:
        model_name = self._model_name(model_tier)
        if self.provider == "gemini":
            return self._complete_gemini(prompt, model_name)
        raise NotImplementedError(f"Provider '{self.provider}' not implemented.")

    def _complete_gemini(self, prompt: str, model_name: str) -> LLMResponse:
        response = self._client.models.generate_content(
            model=model_name,
            contents=prompt,
        )

        prompt_tokens = 0
        completion_tokens = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            prompt_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
            completion_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

        text = ""
        if response.candidates and response.candidates[0].content.parts:
            text = response.candidates[0].content.parts[0].text

        return LLMResponse(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model_name,
            provider=self.provider,
        )

    def test_connection(self) -> bool:
        try:
            response = self.complete("Reply with just: OK", model_tier="fast")
            return bool(response.text.strip())
        except Exception:
            return False
