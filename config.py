from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # OpenAI or OpenRouter (same API format)
    openai_api_key: str
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    whisper_model: str = "whisper-1"

    # amoCRM OAuth2 (private integration)
    amo_client_id: str
    amo_client_secret: str
    amo_redirect_uri: str = "https://example.com"
    amo_pipeline_id: int = 8166506

    # Spam stage
    amo_spam_status_id: int = 143
    amo_spam_loss_reason_id: int = 22501350

    # Files
    tokens_file: str = "tokens.json"
    db_file: str = "conversations.db"
    scenario_file: str = "scenario.md"

    port: int = 8003

    class Config:
        env_file = ".env"


settings = Settings()
