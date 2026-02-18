from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-20250514"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    xai_api_key: str = ""
    google_api_key: str = ""
    recgov_availability_url: str = "https://www.recreation.gov/api/camps/availability/campground"
    request_timeout: int = 30

    model_config = {"env_file": ".env"}


settings = Settings()
