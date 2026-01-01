from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Project Configuration
    PROJECT_ID: str = "ib"
    
    # IBKR Connection (Required by API, optional for Bot)
    IB_PORT: int = 4003
    IB_CLIENT_ID: int = 1
    
    @property
    def IB_HOST(self) -> str:
        return f"{self.PROJECT_ID}-gateway"
    
    # API Security
    API_KEY: str
    
    # Database (Only required by Bot)
    DB_URL: str = ""
    
    # Telegram Bot (Only required by Bot)
    TELEGRAM_TOKEN: str = ""
    TELEGRAM_ALLOWED_IDS: str = ""  # Comma separated list of IDs
    
    @property
    def WEB_SERVICE_URL(self) -> str:
        return f"http://{self.PROJECT_ID}-api:8000"
    
    @property
    def allowed_ids_list(self) -> list[int]:
        if not self.TELEGRAM_ALLOWED_IDS:
            return []
        try:
            return [int(x.strip()) for x in self.TELEGRAM_ALLOWED_IDS.split(",") if x.strip()]
        except ValueError:
            return []
    CHECK_INTERVAL: int = 300
    DB_INSERT_INTERVAL: int = 1800

    # Flex Query & Email (Only required by Bot)
    IB_FLEX_TOKEN: str = ""
    IB_FLEX_QUERY_ID: str = ""
    IB_FLEX_TOKEN_EXPIRY: str = ""
    IB_FLEX_SCHEDULE_TIME: str = "07:30"
    
    EMAIL_SENDER: str = ""
    EMAIL_RECIPIENT: str = ""
    EMAIL_SMTP_SERVER: str = ""
    EMAIL_SMTP_PORT: int = 587
    EMAIL_SMTP_USER: str = ""
    EMAIL_SMTP_PASSWORD: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

settings = Settings()
