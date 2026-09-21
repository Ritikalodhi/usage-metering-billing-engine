from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    STRIPE_API_KEY: str
    STRIPE_WEBHOOK_SECRET: str
    STRIPE_PRICE_PRO: str
    APP_ENV: str
    LOG_LEVEL: str


settings = Settings()
