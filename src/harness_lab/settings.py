from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model_name: str
    pg_dsn: str
    openrouter_api_key: str = ""
    auditor_model: str = "openai/gpt-5.4"
    jev_model: str = "typesafe/jev-1.13"

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Settings":
        load_dotenv(dotenv_path=env_file, override=False)
        return cls(
            api_key=os.getenv("API_KEY", ""),
            base_url=os.getenv("BASE_URL", "").rstrip("/"),
            model_name=os.getenv("MODEL_NAME", ""),
            pg_dsn=os.getenv(
                "PG_DSN",
                "postgresql://workshop:workshop@localhost:55432/workshop",
            ),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            auditor_model=os.getenv("OPENROUTER_AUDITOR_MODEL", "openai/gpt-5.4"),
            jev_model=os.getenv("OPENROUTER_JEV_MODEL", "typesafe/jev-1.13"),
        )

    def validate_model(self) -> list[str]:
        missing = [
            name
            for name, value in (
                ("API_KEY", self.api_key),
                ("BASE_URL", self.base_url),
                ("MODEL_NAME", self.model_name),
            )
            if not value
        ]
        return missing
