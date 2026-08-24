import os
from dataclasses import dataclass


@dataclass(frozen=True)
class HyperCLOVASemanticParserSettings:
    """Environment-backed settings for the isolated semantic parser client."""

    api_key: str | None = None
    base_url: str = "https://clovastudio.stream.ntruss.com"
    model: str = "HCX-007"
    timeout_seconds: float = 30.0
    max_completion_tokens: int = 2_048

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> "HyperCLOVASemanticParserSettings":
        return cls(
            api_key=os.getenv("CLOVASTUDIO_API_KEY") or None,
            base_url=os.getenv(
                "HYPERCLOVA_BASE_URL",
                "https://clovastudio.stream.ntruss.com",
            ).rstrip("/"),
            model=os.getenv("HYPERCLOVA_MODEL", "HCX-007"),
            timeout_seconds=float(os.getenv("HYPERCLOVA_TIMEOUT_SECONDS", "30")),
            max_completion_tokens=int(
                os.getenv("HYPERCLOVA_MAX_COMPLETION_TOKENS", "2048")
            ),
        )

    def validate(self) -> None:
        if self.model != "HCX-007":
            raise ValueError(
                "HyperCLOVA structured semantic parsing requires HCX-007"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("HYPERCLOVA_TIMEOUT_SECONDS must be positive")
        if not 1 <= self.max_completion_tokens <= 32_768:
            raise ValueError(
                "HYPERCLOVA_MAX_COMPLETION_TOKENS must be between 1 and 32768"
            )
