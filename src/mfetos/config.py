from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MFETOS_", env_file=".env", extra="ignore", populate_by_name=True
    )
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
    allow_licensed_retention: bool = False
    pdf_parser: Literal["pdfplumber", "docling"] = "pdfplumber"
    docling_artifacts_path: Path | None = None
    docling_timeout: float = Field(default=600, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: Path | None = None
    langsmith_tracing: bool = Field(default=False, validation_alias="LANGSMITH_TRACING")
    langsmith_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="mfetos", validation_alias="LANGSMITH_PROJECT")
    langsmith_endpoint: str = Field(
        default="https://api.smith.langchain.com", validation_alias="LANGSMITH_ENDPOINT"
    )

    @field_validator("log_dir", "docling_artifacts_path", mode="before")
    @classmethod
    def optional_directory(cls, value):
        return None if value == "" else value
