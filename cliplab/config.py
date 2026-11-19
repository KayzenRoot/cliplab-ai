import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024**2
VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov"})
MUSIC_SUFFIXES = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"})
MUSIC_MAX_BYTES = 128 * MIB


@dataclass(frozen=True)
class Config:
    data: Path
    max_upload: int = 12 * 1024 * MIB
    max_seconds: int = 8 * 3600
    min_free: int = 768 * MIB
    whisper_model: str = "base"
    ollama_model: str = ""
    ollama_url: str = "http://127.0.0.1:11434"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(ROOT / ".env")
        data = Path(os.getenv("CLIPLAB_DATA", "data"))
        return cls(
            data=(ROOT / data).resolve(),
            max_upload=positive("CLIPLAB_MAX_UPLOAD_GIB", 12, 1024 * MIB),
            max_seconds=positive("CLIPLAB_MAX_HOURS", 8, 3600),
            min_free=positive("CLIPLAB_MIN_FREE_MIB", 768, MIB),
            whisper_model=os.getenv("CLIPLAB_WHISPER_MODEL", "base"),
            ollama_model=os.getenv("CLIPLAB_OLLAMA_MODEL", ""),
            ollama_url=os.getenv("CLIPLAB_OLLAMA_URL", "http://127.0.0.1:11434"),
        )

    def prepare(self) -> None:
        for directory in ("input", "output", "cache", "logs", "music", "tmp"):
            (self.data / directory).mkdir(parents=True, exist_ok=True)


def positive(name: str, default: float, scale: int) -> int:
    try:
        value = int(float(os.getenv(name, str(default))) * scale)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} precisa ser um número positivo.") from exc
    if value <= 0:
        raise ValueError(f"{name} precisa ser um número positivo.")
    return value
