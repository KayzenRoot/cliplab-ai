import logging
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.globals import plugin_dirs
from yt_dlp.utils import DownloadError

from .config import VIDEO_SUFFIXES, Config
from .media import MediaCancelled, MediaError

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
logger = logging.getLogger(__name__)


class YouTubeUnavailable(MediaError):
    pass


def video_id(value: str) -> str:
    value = value.strip()
    if any(char.isspace() or ord(char) < 33 for char in value):
        raise ValueError("Cole o link completo de um vídeo do YouTube.")
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or url.hostname not in HOSTS
        or url.port is not None
        or url.username is not None
        or url.password is not None
    ):
        raise ValueError("Cole o link completo de um vídeo do YouTube.")
    path = url.path.strip("/").split("/")
    if url.hostname == "youtu.be" and len(path) == 1:
        candidate = path[0]
    elif url.hostname != "youtu.be" and path == ["watch"]:
        ids = parse_qs(url.query).get("v", [])
        candidate = ids[0] if len(ids) == 1 else ""
    elif (
        url.hostname != "youtu.be"
        and len(path) == 2
        and path[0] in {"shorts", "live", "embed", "v"}
    ):
        candidate = path[1]
    else:
        candidate = ""
    if not VIDEO_ID.fullmatch(candidate):
        raise ValueError("Cole o link de um vídeo individual do YouTube.")
    return candidate


def watch_url(identifier: str) -> str:
    return f"https://www.youtube.com/watch?v={identifier}"


def download_video(
    identifier: str,
    staging: Path,
    config: Config,
    minimum: int,
    *,
    cancel: Callable[[], bool],
    progress: Callable[[float], None],
) -> tuple[Path, str, str]:
    if not VIDEO_ID.fullmatch(identifier):
        raise YouTubeUnavailable("Identificador de vídeo inválido.")
    runtimes = {name: {} for name in ("deno", "node") if shutil.which(name)}
    if not runtimes:
        raise YouTubeUnavailable("Instale Node.js 22+ ou Deno 2.3+ e reinicie o ClipLab.")
    # This app never loads user-installed download plugins or browser credentials.
    plugin_dirs.value = []
    deadline = time.monotonic() + 45 * 60
    received: dict[str, tuple[int, int]] = {}
    refusal = None

    def check() -> None:
        if cancel():
            raise MediaCancelled("Importação cancelada.")
        if time.monotonic() >= deadline:
            raise MediaError("O tempo limite para importar o vídeo do YouTube foi atingido.")

    def match(info: dict, *, incomplete: bool = False) -> str | None:
        nonlocal refusal
        check()
        if incomplete:
            return None
        if info.get("id") != identifier or info.get("availability") != "public":
            refusal = "O link precisa apontar para um vídeo público disponível sem login."
        elif info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            refusal = "Aguarde o YouTube disponibilizar a gravação da transmissão."
        duration = info.get("duration")
        if (
            refusal is None
            and duration is not None
            and not minimum <= duration <= config.max_seconds
        ):
            refusal = "A duração do vídeo está fora dos limites configurados para o projeto."
        return refusal

    def monitor(event: dict) -> None:
        check()
        name = event.get("filename")
        if not name:
            return
        count, estimate = received.get(name, (0, 0))
        count = event.get("downloaded_bytes") or count
        estimate = event.get("total_bytes") or event.get("total_bytes_estimate") or estimate
        received[name] = count, estimate
        total = sum(size for size, _ in received.values())
        if total > config.max_upload:
            raise YouTubeUnavailable("O vídeo ultrapassa o limite de tamanho do ClipLab.")
        # Separate audio and video can coexist with the newly merged file during remux.
        reserved = max(total, sum(size for _, size in received.values()))
        if shutil.disk_usage(staging).free < config.min_free + reserved:
            raise YouTubeUnavailable("Espaço em disco insuficiente para importar o vídeo.")
        if estimate or event.get("status") == "finished":
            fraction = min(1.0, count / estimate) if estimate else 1.0
            progress(70 * fraction if len(received) == 1 else 70 + 25 * fraction)

    options = {
        "allowed_extractors": ["youtube"],
        "format": "bv[height<=1080]+ba/b[height<=1080][ext=mp4]",
        "merge_output_format": "mkv",
        "outtmpl": str(staging / "source.%(ext)s"),
        "noplaylist": True,
        "cachedir": False,
        "proxy": "",
        "js_runtimes": runtimes,
        "socket_timeout": 12,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 1,
        "skip_unavailable_fragments": False,
        "max_filesize": config.max_upload,
        "match_filter": match,
        "progress_hooks": [monitor],
        "logger": logger,
        "quiet": True,
        "noprogress": True,
        "no_color": True,
    }
    check()
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(watch_url(identifier), download=True)
    except DownloadError as exc:
        if refusal:
            raise YouTubeUnavailable(refusal) from exc
        logger.warning("Falha ao importar YouTube %s: %s", identifier, exc)
        raise MediaError(
            "O YouTube não disponibilizou o vídeo para importação. "
            "Verifique se ele abre sem login e tente novamente."
        ) from exc
    check()
    if refusal:
        raise YouTubeUnavailable(refusal)
    sources = [path for path in staging.glob("source.*") if path.suffix in VIDEO_SUFFIXES]
    if not info or len(sources) != 1 or sources[0].stat().st_size == 0:
        raise MediaError(
            "A importação não produziu uma gravação. Confira o link e o limite de tamanho."
        )
    if sources[0].stat().st_size > config.max_upload:
        raise YouTubeUnavailable("O vídeo ultrapassa o limite de tamanho do ClipLab.")
    progress(100)
    return sources[0], clean_text(info.get("title")), clean_text(info.get("uploader"))


def clean_text(value: str | None) -> str:
    return " ".join("".join(char for char in (value or "") if char.isprintable()).split())[:200]
