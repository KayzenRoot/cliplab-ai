import json
import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from .captions import write_srt
from .config import MIB, Config
from .highlights import HighlightEngine
from .media import (
    MUSIC_FALLBACK_WARNING,
    MediaCancelled,
    MediaError,
    Transcriber,
    extract_audio,
    probe_source,
    render_clip,
    verify_output,
)
from .store import Conflict, Store, clip_by_id, identity, revision_files
from .youtube import YouTubeUnavailable, download_video

logger = logging.getLogger(__name__)


class Blocked(MediaError):
    pass


def require_disk(path: Path, required: int) -> None:
    if shutil.disk_usage(path).free < required:
        raise Blocked("Espaço em disco insuficiente para esta operação.")


def read_transcript(root: Path) -> dict:
    return json.loads((root / "transcript.json").read_text(encoding="utf-8"))


class Worker:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.transcriber = Transcriber(config.whisper_model, config.data / "cache" / "models")
        self.thread = threading.Thread(target=self.loop, name="cliplab-worker", daemon=True)

    def loop(self) -> None:
        while not self.stop.is_set():
            try:
                work = self.store.next_work()
                if work:
                    job_id, clip_id = work
                    if clip_id is None:
                        self.process(job_id)
                    else:
                        self.process_clip(job_id, clip_id)
                    continue
            except Exception:
                logger.exception("Falha no worker local")
            self.wake.wait(0.5)
            self.wake.clear()

    def log(self, job_id: str, message: str) -> None:
        path = self.config.data / "logs" / f"{job_id}.log"
        try:
            with path.open("a", encoding="utf-8") as stream:
                signed = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                stream.write(f"{signed} {message}\n")
        except OSError:
            logger.exception("Não foi possível gravar o log de %s", job_id)

    def stage(self, job_id: str, state: str, progress: int, message: str, **values) -> dict:
        def change(job: dict) -> None:
            if state == "VALIDATING" and job["state"] != "CREATED":
                raise Conflict("O trabalho não está na fila.")
            if job["cancel_requested"] or job["state"] == "CANCELLED":
                raise MediaCancelled("Processamento cancelado.")
            job.update(state=state, progress=progress, message=message, **values)

        result = self.store.update(job_id, change)
        self.log(job_id, f"{state} {message}")
        return result

    def cancellation(self, job_id: str) -> Callable[[], bool]:
        checked_at = 0.0
        cancelled = False

        def check() -> bool:
            nonlocal checked_at, cancelled
            if self.stop.is_set() or cancelled:
                return True
            current = time.monotonic()
            if current - checked_at >= 0.3:
                checked_at = current
                cancelled = self.store.get(job_id)["cancel_requested"]
            return cancelled

        return check

    def progress(self, job_id: str, low: int, high: int) -> Callable[[float], None]:
        previous = low

        def report(percentage: float) -> None:
            nonlocal previous
            value = low + int((high - low) * max(0, min(100, percentage)) / 100)
            if value > previous:
                previous = value
                self.store.update(job_id, lambda job: job.update(progress=value))

        return report

    def import_youtube(self, job, directory, cancel):
        job_id = job["id"]
        data = self.config.data
        self.stage(job_id, "IMPORTING", 5, "Importando o vídeo público do YouTube.")
        require_disk(data, self.config.min_free)
        with TemporaryDirectory(prefix="youtube-", dir=data / "tmp") as temporary:
            try:
                staged, title, author = download_video(
                    job["source"]["video_id"],
                    Path(temporary),
                    self.config,
                    job["settings"]["minimum"],
                    cancel=cancel,
                    progress=self.progress(job_id, 5, 25),
                )
            except YouTubeUnavailable as exc:
                raise Blocked(str(exc)) from exc
            probe = probe_source(staged, self.config.max_seconds, cancel=cancel)
            if cancel():
                raise MediaCancelled("Importação cancelada.")
            directory.mkdir(exist_ok=True)
            staged.replace(directory / staged.name)
            job = self.store.update(
                job_id,
                lambda row: row.update(
                    stored_name=staged.name,
                    filename=title or row["filename"],
                    source={**row["source"], "author": author},
                ),
            )
        return job, probe

    def process(self, job_id: str) -> None:
        cancel = self.cancellation(job_id)
        data = self.config.data
        try:
            job = self.stage(job_id, "VALIDATING", 3, "Validando vídeo e áudio.")
            directory = data / "input" / job_id
            stored_name = job["stored_name"]
            require_disk(data, self.config.min_free)
            if job["source"].get("kind") == "youtube" and not stored_name:
                job, probe = self.import_youtube(job, directory, cancel)
            else:
                probe = probe_source(
                    directory / stored_name, self.config.max_seconds, cancel=cancel
                )
            source = directory / job["stored_name"]
            output = data / "output" / job_id
            output.mkdir(exist_ok=True)
            duration = probe["duration"]
            settings = job["settings"]
            if duration < settings["minimum"]:
                raise Blocked("A gravação é mais curta que a duração mínima escolhida.")
            transcript_path = output / "transcript.json"
            if transcript_path.is_file():
                transcript = read_transcript(output)
            else:
                require_disk(data, self.config.min_free + int(duration * 64_000))
                self.stage(
                    job_id,
                    "EXTRACTING_AUDIO",
                    28,
                    "Extraindo áudio da gravação.",
                    duration=duration,
                )
                with TemporaryDirectory(prefix="audio-", dir=data / "tmp") as temporary:
                    work = Path(temporary)
                    audio = work / "audio.wav"
                    extract_audio(source, audio, duration=duration, cancel=cancel)
                    self.stage(job_id, "TRANSCRIBING", 32, "Transcrevendo a fala no computador.")
                    transcript = self.transcriber.transcribe(
                        audio,
                        settings["language"],
                        cancel=cancel,
                        progress=self.progress(job_id, 32, 58),
                    )
                    srt = work / "transcript.srt"
                    write_srt(srt, transcript)
                    srt.replace(output / "transcript.srt")
                    staged = work / "transcript.json"
                    staged.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
                    staged.replace(transcript_path)
            warnings = list(transcript.get("warnings", []))
            self.stage(
                job_id,
                "ANALYZING",
                60,
                "Analisando trechos da transcrição.",
                duration=duration,
                transcript_available=True,
                warnings=warnings,
            )
            clips = job["clips"]
            if not clips:
                engine = HighlightEngine(
                    ollama_model=self.config.ollama_model if settings["use_ollama"] else "",
                    ollama_url=self.config.ollama_url,
                )
                candidates = engine.select(
                    transcript,
                    duration,
                    minimum=settings["minimum"],
                    maximum=settings["maximum"],
                    count=settings["count"],
                    cancel=cancel,
                )
                if cancel():
                    raise MediaCancelled("Processamento cancelado.")
                if engine.warning:
                    warnings.append(engine.warning)
                if not candidates:
                    raise Blocked("Não há trechos de fala coerentes nos limites escolhidos.")
                clips = [
                    dict(
                        candidate,
                        id=identity(),
                        revision=1,
                        status="pending",
                        operation=None,
                        preview_ready=False,
                        final_ready=False,
                        music_applied=False,
                        warnings=[],
                        error=None,
                    )
                    for candidate in candidates
                ]
            if len(clips) < settings["count"]:
                warnings.append(f"Foram encontrados {len(clips)} cortes válidos na gravação.")
            self.stage(
                job_id,
                "SELECTING",
                65,
                "Seleção editorial concluída.",
                clips=clips,
                warnings=warnings,
            )
            for index, clip in enumerate(clips):
                if clip["preview_ready"] and revision_files(output, clip)[0].is_file():
                    continue
                low = 66 + index * 30 // len(clips)
                high = 66 + (index + 1) * 30 // len(clips)
                self.stage(
                    job_id, "RENDERING", low, f"Renderizando prévia {index + 1} de {len(clips)}."
                )
                try:
                    result = self.render(
                        source,
                        output,
                        settings,
                        clip,
                        transcript,
                        True,
                        cancel,
                        self.progress(job_id, low, high - 1),
                        lambda: self.stage(job_id, "VERIFYING", high, "Verificando a prévia."),
                    )
                except MediaError as exc:
                    if isinstance(exc, (MediaCancelled, Blocked)):
                        raise
                    self.log(job_id, f"Prévia {clip['id']} falhou: {exc}")
                    result = {"error": str(exc), "preview_ready": False}
                self.store.update(job_id, lambda row: clip_by_id(row, clip["id"]).update(result))
            if not any(clip["preview_ready"] for clip in self.store.get(job_id)["clips"]):
                raise MediaError(
                    "Nenhuma prévia pôde ser renderizada. Consulte o log e tente novamente."
                )
            self.stage(job_id, "READY_FOR_REVIEW", 100, "Cortes disponíveis para revisão.")
        except Conflict:
            return
        except (MediaError, OSError, ValueError) as exc:
            self.fail(job_id, exc)
        except Exception as exc:
            logger.exception("Falha inesperada ao processar %s", job_id)
            self.fail(job_id, exc)

    def fail(self, job_id: str, exc: Exception) -> None:
        if self.stop.is_set():
            return
        state = "FAILED"
        if isinstance(exc, MediaCancelled):
            state = "CANCELLED"
        elif isinstance(exc, Blocked):
            state = "BLOCKED"
        message = str(exc) if isinstance(exc, MediaError) else "Falha de processamento local."

        def change(job: dict) -> None:
            if job["cancel_requested"]:
                job.update(state="CANCELLED", message="Processamento cancelado.", error=None)
            else:
                job.update(
                    state=state, message=message, error=None if state == "CANCELLED" else message
                )
            job["cancel_requested"] = False

        self.log(job_id, f"{state} {type(exc).__name__}: {exc}")
        self.store.update(job_id, change)

    def process_clip(self, job_id: str, clip_id: str) -> None:
        job = self.store.get(job_id)
        clip = clip_by_id(job, clip_id)
        operation = clip["operation"]
        if not operation:
            return
        output = self.config.data / "output" / job_id
        source = self.config.data / "input" / job_id / job["stored_name"]
        self.log(job_id, f"{operation} iniciado para {clip_id}")
        try:
            result = self.render(
                source,
                output,
                job["settings"],
                clip,
                read_transcript(output),
                operation == "preview",
                self.stop.is_set,
                lambda value: self.store.update(
                    job_id,
                    lambda row: clip_by_id(row, clip_id).update(operation_progress=int(value)),
                ),
            )
            if operation == "preview":
                for obsolete in output.glob(f"{clip_id}-*"):
                    if obsolete not in revision_files(output, clip):
                        try:
                            obsolete.unlink()
                        except OSError:
                            self.log(
                                job_id, f"Não foi possível limpar a revisão antiga {obsolete.name}"
                            )
        except Exception as exc:
            if self.stop.is_set():
                return
            self.log(job_id, f"{operation} falhou para {clip_id}: {type(exc).__name__}: {exc}")
            result = {"error": str(exc) if isinstance(exc, MediaError) else "Falha ao renderizar."}
        self.store.update(
            job_id,
            lambda row: clip_by_id(row, clip_id).update(
                result,
                operation=None,
                operation_progress=None,
            ),
        )

    def render(
        self,
        source: Path,
        output: Path,
        settings: dict,
        clip: dict,
        transcript: dict,
        preview: bool,
        cancel: Callable[[], bool],
        progress: Callable[[float], None],
        before_verify: Callable[[], object] | None = None,
    ) -> dict:
        data = self.config.data
        require_disk(data, self.config.min_free + (64 if preview else 256) * MIB)
        music_path = None
        if settings["music_mode"] == "licensed":
            track = self.store.music(settings["music_id"])
            music_path = data / "music" / track["id"] / track["stored_name"]
        start, end = clip["start"], clip["end"]
        preview_path, final_path, srt_path = revision_files(output, clip)
        with TemporaryDirectory(prefix="render-", dir=data / "tmp") as temporary:
            work = Path(temporary)
            video = work / "clip.mp4"
            warnings = render_clip(
                source,
                video,
                transcript,
                start,
                end,
                title=clip["title"],
                hook=clip["hook"],
                preset=settings["preset"],
                emphasis=settings["emphasis"],
                preview=preview,
                music_mode=settings["music_mode"],
                music_volume=settings["music_volume"],
                music_path=music_path,
                cta=clip.get("question", ""),
                cancel=cancel,
                progress=progress,
            )
            if before_verify:
                before_verify()
            info = verify_output(video, end - start, preview=preview, cancel=cancel)
            if not settings["minimum"] <= info["duration"] <= settings["maximum"]:
                raise MediaError("A duração medida do MP4 ficou fora dos limites escolhidos.")
            if cancel():
                raise MediaCancelled("Processamento cancelado.")
            if not preview or not clip["final_ready"]:
                srt = work / "clip.srt"
                write_srt(srt, transcript, start, end)
                srt.replace(srt_path)
            video.replace(preview_path if preview else final_path)
        result = {
            "preview_ready" if preview else "final_ready": True,
            "warnings": warnings,
            "error": None,
        }
        if not preview:
            result["music_applied"] = (
                settings["music_mode"] != "off"
                and settings["music_volume"] > 0
                and MUSIC_FALLBACK_WARNING not in warnings
            )
            result["output_info"] = info
        return result
