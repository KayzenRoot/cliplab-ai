import json
import os
import tempfile
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .config import MIB, Config
from .store import Conflict, MissingRecord, Store, revision_files
from .worker import require_disk


def create_zip(config: Config, store: Store, job: dict, directory: Path) -> FileResponse:
    approved = [
        clip for clip in job["clips"] if clip["status"] == "approved" and clip["final_ready"]
    ]
    if not approved:
        raise Conflict("Exporte ao menos um corte aprovado antes de baixar o ZIP.")

    settings = job["settings"]
    manifest = {"source": job["source"], "settings": settings, "clips": []}
    files = {}
    credits = []
    for index, clip in enumerate(approved, 1):
        _, video, captions = revision_files(directory, clip)
        if not video.is_file() or not captions.is_file():
            raise MissingRecord("Um arquivo exportado não está disponível no armazenamento local.")
        name = f"corte-{index:02d}"
        files.update({f"{name}.mp4": video, f"{name}.srt": captions})
        detail = {
            key: clip.get(key)
            for key in (
                "start",
                "end",
                "title",
                "hook",
                "description",
                "hashtags",
                "summary",
                "question",
                "score",
                "rationale",
                "warnings",
                "output_info",
            )
        }
        detail["mp4"] = f"{name}.mp4"
        detail["srt"] = f"{name}.srt"
        detail["music"] = None
        if clip["music_applied"] and settings["music_mode"] == "licensed":
            track = store.music(settings["music_id"])
            detail["music"] = {
                key: track[key] for key in ("title", "author", "license", "origin", "attribution")
            }
            credits.append(
                f"{name}: {track['title']} — {track['author']}\n"
                f"Licença: {track['license']}\nOrigem: {track['origin']}\n{track['attribution']}"
            )
        elif clip["music_applied"]:
            detail["music"] = {"title": "Trilha ambiente procedural do ClipLab AI"}
        manifest["clips"].append(detail)
    for filename in ("transcript.json", "transcript.srt"):
        path = directory / filename
        if path.is_file():
            files[filename] = path

    fd, filename = tempfile.mkstemp(prefix="zip-", suffix=".zip", dir=config.data / "tmp")
    os.close(fd)
    archive = Path(filename)
    try:
        require_disk(
            config.data,
            config.min_free + sum(path.stat().st_size for path in files.values()) + 4 * MIB,
        )
        with ZipFile(archive, "w", compression=ZIP_STORED) as package:
            for name, path in files.items():
                package.write(path, name)
            package.writestr("editorial.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            if credits:
                package.writestr("atribuicao-musical.txt", "\n\n".join(credits))
    except FileNotFoundError as exc:
        archive.unlink(missing_ok=True)
        raise Conflict("Os arquivos mudaram durante a exportação. Tente novamente.") from exc
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"cliplab-{job['id'][:8]}.zip",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(archive.unlink, missing_ok=True),
    )
