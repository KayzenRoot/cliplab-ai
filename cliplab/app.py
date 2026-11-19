import logging
import os
import re
import shutil
import tempfile
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import MIB, MUSIC_MAX_BYTES, MUSIC_SUFFIXES, VIDEO_SUFFIXES, Config
from .exports import create_zip
from .highlights import editorial_metadata
from .media import MediaError, probe_music
from .store import (
    ACTIVE,
    RETRYABLE,
    Conflict,
    MissingRecord,
    Store,
    clip_by_id,
    identity,
    now,
    revision_files,
)
from .worker import Blocked, Worker, read_transcript, require_disk

PACKAGE = Path(__file__).resolve().parent
ID = re.compile(r"[a-f0-9]{32}")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
Text = Annotated[str, Form(min_length=1, max_length=500)]
CutText = Annotated[str, Field(min_length=1, max_length=90)]
Status = Literal["pending", "approved", "discarded"]


class ClipPatch(BaseModel):
    start: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    end: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    title: CutText | None = None
    hook: Annotated[str, Field(max_length=140)] | None = None
    description: Annotated[str, Field(max_length=1200)] | None = None
    status: Status | None = None


class LocalRequests:
    def __init__(self, app: ASGIApp, config: Config):
        self.app = app
        self.config = config
        self.upload = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        if headers.get(b"x-cliplab") != b"1":
            await JSONResponse({"detail": "Requisição local não autorizada."}, 403)(
                scope,
                receive,
                send,
            )
            return
        path = scope["path"]
        is_upload = scope["method"] == "POST" and path.rstrip("/") in {"/api/jobs", "/api/music"}
        size = MUSIC_MAX_BYTES if path.rstrip("/") == "/api/music" else self.config.max_upload
        limit = size + MIB if is_upload else 65_536
        length = headers.get(b"content-length", b"0")
        if not length.isdigit() or len(length) > 15 or int(length) > limit:
            await JSONResponse({"detail": "O envio excede o limite permitido."}, 413)(
                scope,
                receive,
                send,
            )
            return
        if is_upload and not self.upload.acquire(blocking=False):
            await JSONResponse({"detail": "Já há um upload em andamento."}, 429)(
                scope,
                receive,
                send,
            )
            return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                # A parser must release multipart spool files when input aborts.
                from starlette.formparsers import MultiPartException

                if is_upload:
                    raise MultiPartException("O envio excede o limite permitido.")
                raise HTTPException(413, "O envio excede o limite permitido.")
            return message

        try:
            if is_upload:
                required = int(length) or limit
                try:
                    # Multipart parsing can temporarily occupy the system temp volume.
                    require_disk(Path(tempfile.gettempdir()), self.config.min_free + required)
                    require_disk(self.config.data, self.config.min_free + 2 * required)
                except Blocked as exc:
                    await JSONResponse({"detail": str(exc)}, 507)(scope, receive, send)
                    return
            await self.app(scope, bounded_receive, send)
        finally:
            if is_upload:
                self.upload.release()


@contextmanager
def process_lock(path: Path) -> Iterator[None]:
    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Outra instância do ClipLab já usa este diretório.") from exc
        yield


def present(record: dict) -> dict:
    return {
        key: value
        for key, value in record.items()
        if key not in {"stored_name", "cancel_requested"}
    }


def reference_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.hostname not in YOUTUBE_HOSTS
            or url.port
            or url.username
            or url.password
            or not url.path.strip("/")
            or any(char.isspace() or ord(char) <= 32 for char in value)
        ):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "Use um URL HTTPS de referência do YouTube.") from exc
    return value


def label(value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise HTTPException(422, "Preencha os campos de autoria, origem e licença.")
    return cleaned


def upload_name(file: UploadFile, suffixes: frozenset[str]) -> tuple[str, str]:
    original = unicodedata.normalize("NFC", (file.filename or "").replace("\\", "/"))
    original = original.rsplit("/", 1)[-1]
    suffix = Path(original).suffix.lower()
    if suffix not in suffixes:
        raise HTTPException(415, "Formato de arquivo não aceito.")
    name = "".join(char for char in original if char.isprintable() and char not in '<>:"|?*').strip(
        " ."
    )
    return name[:200] or f"arquivo{suffix}", suffix


def copy_upload(file: UploadFile, output: Path, limit: int, config: Config) -> None:
    if file.size is not None and file.size > limit:
        raise HTTPException(413, "O arquivo excede o limite permitido.")
    count = 0
    with output.open("xb") as target:
        while chunk := file.file.read(MIB):
            count += len(chunk)
            if count > limit:
                raise HTTPException(413, "O arquivo excede o limite permitido.")
            require_disk(config.data, config.min_free + len(chunk))
            target.write(chunk)
    if not count:
        raise HTTPException(422, "O arquivo está vazio.")


def valid_id(value: str) -> str:
    if not ID.fullmatch(value):
        raise MissingRecord("Registro não encontrado.")
    return value


def create_app(config: Config | None = None, *, run_worker: bool = True) -> FastAPI:
    config = config or Config.from_env()
    config.prepare()
    store = Store(config.data / "cliplab.sqlite3")
    worker: Worker | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal worker
        with process_lock(config.data / "instance.lock"):
            store.initialize()
            store.recover()
            for old in (config.data / "tmp").iterdir():
                if old.name.startswith(("audio-", "render-", "upload-", "zip-")):
                    try:
                        if old.is_dir() and not old.is_symlink():
                            shutil.rmtree(old)
                        else:
                            old.unlink()
                    except OSError:
                        logging.getLogger(__name__).warning("Temporário indisponível: %s", old.name)
            for directory, known in (
                ("input", {row["id"] for row in store.list_jobs()}),
                ("music", {row["id"] for row in store.tracks()}),
            ):
                for old in (config.data / directory).iterdir():
                    if ID.fullmatch(old.name) and old.name not in known and old.is_dir():
                        shutil.rmtree(old, ignore_errors=True)
            if run_worker:
                worker = Worker(store, config)
                worker.thread.start()
            try:
                yield
            finally:
                if worker:
                    worker.stop.set()
                    worker.wake.set()
                    worker.thread.join()
                    worker = None

    app = FastAPI(
        title="ClipLab AI",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.store = store
    app.add_middleware(LocalRequests, config=config)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE / "templates")

    def signal() -> None:
        if worker:
            worker.wake.set()

    @app.exception_handler(MissingRecord)
    async def missing(_request: Request, exc: MissingRecord):
        return JSONResponse({"detail": str(exc)}, 404)

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, exc: Conflict):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(Blocked)
    async def blocked(_request: Request, exc: Blocked):
        return JSONResponse({"detail": str(exc)}, 507)

    @app.exception_handler(MediaError)
    async def invalid_media(_request: Request, exc: MediaError):
        return JSONResponse({"detail": str(exc)}, 422)

    @app.get("/")
    def home(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @app.get("/api/config")
    def settings():
        return {"ollama_enabled": bool(config.ollama_model), "max_upload_bytes": config.max_upload}

    @app.get("/api/jobs")
    def jobs():
        return [present(job) for job in store.list_jobs()]

    @app.post("/api/jobs", status_code=202)
    def upload_video(
        file: Annotated[UploadFile, File()],
        author: Text,
        origin: Text,
        license: Text,
        authorized: Annotated[bool, Form()],
        youtube_url: Annotated[str, Form(max_length=2048)] = "",
        count: Annotated[int, Form(ge=1, le=5)] = 3,
        minimum: Annotated[int, Form(ge=61, le=180)] = 75,
        maximum: Annotated[int, Form(ge=61, le=180)] = 100,
        language: Annotated[Literal["auto", "pt"], Form()] = "auto",
        preset: Annotated[Literal["gameplay", "dossier"], Form()] = "dossier",
        emphasis: Annotated[Literal["low", "medium", "high"], Form()] = "medium",
        music_mode: Annotated[Literal["off", "procedural", "licensed"], Form()] = "off",
        music_volume: Annotated[float, Form(ge=0, le=0.30)] = 0.12,
        music_id: Annotated[str | None, Form(max_length=32)] = None,
        use_ollama: Annotated[bool, Form()] = False,
    ):
        if not authorized:
            raise HTTPException(422, "Confirme a titularidade ou a licença comercial da gravação.")
        if maximum < minimum:
            raise HTTPException(422, "A duração máxima deve ser igual ou maior que a mínima.")
        if music_mode == "licensed":
            store.music(valid_id(music_id or ""))
        source = {
            "author": label(author),
            "origin": label(origin),
            "license": label(license),
            "youtube_url": reference_url(youtube_url),
            "authorized": authorized,
        }
        filename, suffix = upload_name(file, VIDEO_SUFFIXES)
        job_id = identity()
        stored_name = f"source{suffix}"
        record = {
            "id": job_id,
            "filename": filename,
            "stored_name": stored_name,
            "created_at": now(),
            "state": "CREATED",
            "progress": 0,
            "duration": None,
            "message": "Na fila de processamento.",
            "error": None,
            "cancel_requested": False,
            "clips": [],
            "warnings": [],
            "transcript_available": False,
            "source": source,
            "settings": {
                "count": count,
                "minimum": minimum,
                "maximum": maximum,
                "language": language,
                "preset": preset,
                "emphasis": emphasis,
                "music_mode": music_mode,
                "music_volume": music_volume,
                "music_id": music_id if music_mode == "licensed" else None,
                "use_ollama": use_ollama,
            },
        }
        save_upload(file, config, "input", record, config.max_upload, store.create)
        signal()
        return present(record)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return present(store.get(valid_id(job_id)))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        def change(job: dict) -> None:
            if job["state"] == "CREATED":
                job.update(state="CANCELLED", message="Processamento cancelado.")
            elif job["state"] in ACTIVE:
                job.update(cancel_requested=True, message="Cancelamento solicitado.")
            else:
                raise Conflict("Este projeto não tem processamento inicial em andamento.")

        result = store.update(valid_id(job_id), change)
        signal()
        return present(result)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str):
        def change(job: dict) -> None:
            if job["state"] not in RETRYABLE:
                raise Conflict("Este projeto não está aguardando reprocessamento.")
            job.update(
                state="CREATED",
                message="Na fila de reprocessamento.",
                error=None,
                progress=0,
                cancel_requested=False,
            )

        result = store.update(valid_id(job_id), change)
        signal()
        return present(result)

    @app.get("/api/jobs/{job_id}/log")
    def job_log(job_id: str):
        job = store.get(valid_id(job_id))
        log = config.data / "logs" / f"{job['id']}.log"
        if not log.is_file():
            return PlainTextResponse("O processamento ainda não começou.")
        return FileResponse(log, media_type="text/plain; charset=utf-8")

    @app.patch("/api/jobs/{job_id}/clips/{clip_id}")
    def update_clip(job_id: str, clip_id: str, patch: ClipPatch):
        valid_id(clip_id)
        job_id = valid_id(job_id)
        changes = patch.model_dump(exclude_none=True)
        if "title" in changes and not changes["title"].strip():
            raise HTTPException(422, "O título do corte não pode ficar vazio.")
        for key in ("title", "hook", "description"):
            if key in changes:
                changes[key] = " ".join(changes[key].split())
        transcript = (
            read_transcript(config.data / "output" / job_id)
            if any(key in changes for key in ("start", "end"))
            and store.get(job_id)["transcript_available"]
            else None
        )

        def change(job: dict) -> None:
            clip = editable(job, clip_id)
            start = round(changes.get("start", clip["start"]), 3)
            end = round(changes.get("end", clip["end"]), 3)
            duration = round(end - start, 3)
            cfg = job["settings"]
            if not (cfg["minimum"] <= duration <= cfg["maximum"] and end <= job["duration"]):
                raise HTTPException(
                    422, "Ajuste os tempos aos limites do vídeo e da duração escolhida."
                )
            timing = (start, end) != (clip["start"], clip["end"])
            visual = timing or any(
                key in changes and changes[key] != clip[key] for key in ("title", "hook")
            )
            new_status = changes.get("status", clip["status"])
            if changes.get("status") == "approved" and (visual or not clip["preview_ready"]):
                raise Conflict("Aguarde uma prévia atualizada antes de aprovar.")
            if timing and transcript is not None:
                refreshed = editorial_metadata(transcript, start, end)
                clip.update({key: refreshed[key] for key in ("summary", "hashtags", "question")})
                clip.update(score=None, rationale="Intervalo ajustado manualmente.")
            if visual:
                clip.update(
                    revision=clip["revision"] + 1,
                    preview_ready=False,
                    final_ready=False,
                    warnings=[],
                    music_applied=False,
                    output_info=None,
                )
                new_status = "discarded" if new_status == "discarded" else "pending"
            clip.update(changes, start=start, end=end, status=new_status)
            if not clip["preview_ready"] and new_status != "discarded":
                clip.update(operation="preview", operation_progress=0, error=None)

        updated = store.update(job_id, change)
        signal()
        return clip_by_id(updated, clip_id)

    @app.post("/api/jobs/{job_id}/clips/{clip_id}/export", status_code=202)
    def export_clip(job_id: str, clip_id: str):
        def change(job: dict) -> None:
            clip = editable(job, valid_id(clip_id))
            if clip["status"] != "approved" or not clip["preview_ready"]:
                raise Conflict("Aprove a prévia antes de exportar.")
            final = revision_files(config.data / "output" / job["id"], clip)[1]
            if not clip["final_ready"] or not final.is_file():
                clip.update(operation="export", operation_progress=0, error=None, final_ready=False)

        updated = store.update(valid_id(job_id), change)
        signal()
        return clip_by_id(updated, clip_id)

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/{artifact}")
    def download_clip(job_id: str, clip_id: str, artifact: Literal["preview", "mp4", "srt"]):
        job = store.get(valid_id(job_id))
        clip = clip_by_id(job, valid_id(clip_id))
        preview, final, captions = revision_files(config.data / "output" / job_id, clip)
        if artifact == "preview" and clip["preview_ready"]:
            path, media_type, filename = preview, "video/mp4", None
        elif artifact == "mp4" and clip["final_ready"] and clip["status"] == "approved":
            path, media_type, filename = final, "video/mp4", f"cliplab-{clip_id[:8]}.mp4"
        elif artifact == "srt" and (clip["preview_ready"] or clip["final_ready"]):
            path, media_type, filename = (
                captions,
                "application/x-subrip",
                f"cliplab-{clip_id[:8]}.srt",
            )
        else:
            raise MissingRecord("Este arquivo ainda não está disponível.")
        return serve_file(path, media_type, filename)

    @app.get("/api/jobs/{job_id}/{artifact}")
    def job_artifact(job_id: str, artifact: Literal["transcript.srt", "transcript.json", "zip"]):
        job = store.get(valid_id(job_id))
        directory = config.data / "output" / job_id
        if artifact == "zip":
            return create_zip(config, store, job, directory)
        if not job["transcript_available"]:
            raise MissingRecord("A transcrição ainda não está disponível.")
        media_type = "application/json" if artifact.endswith(".json") else "application/x-subrip"
        return serve_file(directory / artifact, media_type, artifact)

    @app.get("/api/music")
    def music():
        return [present(track) for track in store.tracks()]

    @app.post("/api/music", status_code=201)
    def register_track(
        file: Annotated[UploadFile, File()],
        title: Text,
        author: Text,
        license: Text,
        origin: Text,
        authorized: Annotated[bool, Form()],
        attribution: Annotated[str, Form(max_length=1200)] = "",
    ):
        if not authorized:
            raise HTTPException(422, "Confirme a autorização comercial da trilha.")
        _, suffix = upload_name(file, MUSIC_SUFFIXES)
        record = {
            "id": identity(),
            "stored_name": f"track{suffix}",
            "title": label(title),
            "author": label(author),
            "license": label(license),
            "origin": label(origin),
            "attribution": attribution.strip(),
            "authorized": authorized,
            "created_at": now(),
        }
        save_upload(file, config, "music", record, MUSIC_MAX_BYTES, store.add_music, probe_music)
        return present(record)

    return app


def editable(job: dict, clip_id: str) -> dict:
    if job["state"] != "READY_FOR_REVIEW":
        raise Conflict("Aguarde o projeto ficar pronto para revisão.")
    clip = clip_by_id(job, clip_id)
    if clip["operation"]:
        raise Conflict("Aguarde a renderização em andamento.")
    return clip


def save_upload(file, config, category, record, limit, persist, validate=None) -> None:
    destination = config.data / category / record["id"]
    with tempfile.TemporaryDirectory(prefix="upload-", dir=config.data / "tmp") as temporary:
        staged = Path(temporary) / record["stored_name"]
        copy_upload(file, staged, limit, config)
        if validate:
            validate(staged)
        destination.mkdir()
        try:
            staged.replace(destination / record["stored_name"])
            persist(record)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise


def serve_file(path: Path, media_type: str, filename: str | None = None) -> FileResponse:
    if not path.is_file():
        raise MissingRecord("Arquivo não encontrado no armazenamento local.")
    return FileResponse(
        path, media_type=media_type, filename=filename, headers={"Cache-Control": "no-store"}
    )
