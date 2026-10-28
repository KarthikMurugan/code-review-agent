from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL = "nebius:moonshotai/Kimi-K2.7-Code"
NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"


def load_environment(env_file: Path | None = None) -> None:
    # Editable source installs can find the project's .env from any working directory.
    # Explicit environment variables take precedence. Do not search parent directories.
    path = env_file if env_file is not None else Path(__file__).resolve().parents[2] / ".env"
    if env_file is not None and not path.is_file():
        raise ValueError("The requested environment file does not exist")
    load_dotenv(path, override=False, interpolate=False)
