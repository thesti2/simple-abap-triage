"""LLM factory: OpenRouter through the OpenAI-compatible ChatOpenAI client."""

from langchain_openai import ChatOpenAI

from app.config import Settings, get_settings


def get_llm(settings: Settings | None = None, temperature: float = 0.0) -> ChatOpenAI:
    s = settings or get_settings()
    if not s.openrouter_api_key or not s.model:
        raise RuntimeError("OPENROUTER_API_KEY and MODEL must be set in .env")
    return ChatOpenAI(
        model=s.model,
        api_key=s.openrouter_api_key,
        base_url=s.openrouter_base_url,
        temperature=temperature,
        timeout=s.llm_timeout_s,
        max_retries=2,
    )
