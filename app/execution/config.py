import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionSettings:
    step_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "ExecutionSettings":
        value = float(os.getenv("RETRIEVAL_STEP_TIMEOUT_SECONDS", "10"))
        if value <= 0:
            raise ValueError("RETRIEVAL_STEP_TIMEOUT_SECONDS must be positive")
        return cls(step_timeout_seconds=value)

