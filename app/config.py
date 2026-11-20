"""Settings from environment / .env (git-ignored). No values are hard-coded here."""

from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Export .env into os.environ so LangChain/LangSmith pick up LANGSMITH_* automatically.
load_dotenv(override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM (OpenRouter, OpenAI-compatible API)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model: str = ""
    # how to get structured output from the model: "json_schema" or "function_calling"
    structured_output_method: str = "json_schema"  # smoke test: json_schema was more accurate than function_calling
    llm_timeout_s: float = 60.0

    # xMatters -> backend webhook
    xmatters_webhook_token: str = ""

    # Database (SQLite locally, Cloud SQL Postgres in GCP).
    # DATABASE_URL wins if set (e.g. fix worker via Cloud SQL Auth Proxy:
    # postgresql+psycopg://triage:<pw>@127.0.0.1:5432/triage). On Cloud Run, Terraform sets DB_SOCKET/DB_NAME/
    # DB_USER/DB_PASSWORD instead and the URL is built from them (see `db_url`).
    database_url: str = ""
    db_socket: str = ""  # /cloudsql/<project>:<region>:<instance>
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""

    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel_id: str = ""
    slack_app_token: str = ""  # xapp-..., Socket Mode only
    slack_mode: str = "socket"  # "socket" (local dev) | "http" (Cloud Run) | "off"

    # SAP (only shown in messages)
    sap_uat_sid: str = ""

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL: DATABASE_URL > Cloud SQL unix socket > local SQLite file."""
        if self.database_url:
            return self.database_url
        if self.db_socket:
            from sqlalchemy import URL

            return URL.create("postgresql+psycopg", username=self.db_user, password=self.db_password,
                              database=self.db_name, query={"host": self.db_socket}).render_as_string(hide_password=False)
        return "sqlite:///./triage.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
