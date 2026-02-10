from langchain_core.language_models import BaseChatModel

from camping_agent.config import settings


def get_llm() -> BaseChatModel:
    """Instantiate the configured LLM provider.

    Only the selected provider's package needs to be installed.
    """
    provider = settings.llm_provider.lower()
    model = settings.llm_model

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, api_key=settings.anthropic_api_key)
    elif provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key=settings.openai_api_key)
    elif provider == "xai":
        from langchain_xai import ChatXAI

        return ChatXAI(model=model, api_key=settings.xai_api_key)
    elif provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model, google_api_key=settings.google_api_key)
    else:
        raise ValueError(
            f"Unsupported LLM provider: {provider}. "
            "Supported: anthropic, openai, xai, google"
        )
