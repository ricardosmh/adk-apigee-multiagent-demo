from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DB_USER: str = "ecommerce-sa"
    DB_PASSWORD: str = "password"
    DB_HOST: str = "10.0.0.5"  # Placeholder for PSC Endpoint IP
    DB_PORT: int = 3306
    DB_NAME: str = "database"
    CLOUD_SQL_CONNECTION_NAME: Optional[str] = None
    USE_IAM_AUTH: bool = True
    DB_HOST_IS_PSC: bool = True
    PRODUCTS_SERVICE_URL: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
