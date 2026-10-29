from pathlib import Path
import math
import os

from dotenv import load_dotenv

DEFAULT_MODEL = "nebius:moonshotai/Kimi-K2.7-Code"
NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_REVIEW_TIMEOUT = 480


def review_timeout() -> float:
    """Leave headroom inside the worker's 900-second queue lease."""
    value = float(os.environ.get("REVIEW_TIMEOUT_SECONDS", str(DEFAULT_REVIEW_TIMEOUT)))
    if not math.isfinite(value) or not 1 <= value <= 600:
        raise ValueError("REVIEW_TIMEOUT_SECONDS must be between 1 and 600")
    return value


def request_timeout(deadline: float) -> float:
    # Two request attempts plus the SDK's short retry delay fit the total budget.
    return min(210, deadline * 0.45)


def load_environment(env_file: Path | None = None) -> None:
    # Editable source installs can find the project's .env from any working directory.
    # Explicit environment variables take precedence. Do not search parent directories.
    path = env_file if env_file is not None else Path(__file__).resolve().parents[2] / ".env"
    if env_file is not None and not path.is_file():
        raise ValueError("The requested environment file does not exist")
    load_dotenv(path, override=False, interpolate=False)
