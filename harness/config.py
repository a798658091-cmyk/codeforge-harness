from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    workspace: Path
    model: str
    api_key: str
    base_url: str | None
    max_tokens: int = 4096
    request_timeout: float = 60.0

    @classmethod
    def from_env(
        cls,
        *,
        workspace: str | Path = ".",
        model: str | None = None,
        base_url: str | None = None,
    ) -> "Settings":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass

        resolved_model = (
            model
            or os.getenv("CODEFORGE_MODEL")
            or os.getenv("MODEL_ID")
            or os.getenv("OPENAI_MODEL")
        )
        api_key = (
            os.getenv("CODEFORGE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
        )
        resolved_base_url = (
            base_url
            or os.getenv("CODEFORGE_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
        )
        if not resolved_model:
            raise ConfigurationError(
                "missing model: set CODEFORGE_MODEL"
            )
        if not api_key:
            raise ConfigurationError(
                "missing API key: set CODEFORGE_API_KEY, "
                "OPENAI_API_KEY, or DEEPSEEK_API_KEY"
            )
        return cls(
            workspace=Path(workspace).expanduser().resolve(),
            model=resolved_model,
            api_key=api_key,
            base_url=resolved_base_url,
            max_tokens=int(os.getenv("CODEFORGE_MAX_TOKENS", "4096")),
            request_timeout=float(
                os.getenv("CODEFORGE_REQUEST_TIMEOUT", "60")
            ),
        )
