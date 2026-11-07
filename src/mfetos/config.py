from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MFETOS_", env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    llm_provider: Literal["deterministic", "ollama"] = "deterministic"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = ""
    http_timeout: float = Field(default=30, gt=0)
    model_timeout: float = Field(default=120, gt=0)
    max_retrieval_rounds: int = Field(default=2, ge=0, le=5)
    worker_concurrency: int = Field(default=2, ge=1, le=8)
    upstox_token: SecretStr = SecretStr("")
    upstox_license_ack: bool = False
    trendlyne_url: str = ""
    trendlyne_token: SecretStr = SecretStr("")
    trendlyne_license_ack: bool = False
    allow_licensed_retention: bool = False
