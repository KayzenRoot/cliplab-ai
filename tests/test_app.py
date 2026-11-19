import io
import json
import shutil
import time
import wave
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cliplab.app import create_app
from cliplab.config import MIB, Config
from cliplab.store import revision_files

HEADERS = {"X-ClipLab": "1"}
LICENSE = {"author": "Autora de teste", "origin": "Síntese local", "license": "Conteúdo próprio"}


@pytest.fixture
def client(tmp_path):
    config = Config(tmp_path / "data", max_upload=128, min_free=MIB)
    app = create_app(config, run_worker=False)
    with TestClient(app, base_url="http://127.0.0.1") as http:
        yield http, config, app.state.store


def upload(http, name="teste.mp4", content=b"fixture video", **fields):
    return http.post(
        "/api/jobs",
        headers=HEADERS,
        files={"file": (name, content)},
        data={**LICENSE, "authorized": "true", **fields},
    )


def test_home_and_local_guard(client):
    http, _, _ = client
    assert "ClipLab AI" in http.get("/").text
    assert http.get("/static/studio.js").status_code == 200
    assert http.get("/api/config").json()["max_upload_bytes"] == 128
    assert http.get("/api/jobs", headers={"Host": "external.example"}).status_code == 400
    assert http.post("/api/jobs").status_code == 403


@pytest.mark.parametrize(
    "name, data, status",
    [
        ("cam.mp4", {"authorized": "false"}, 422),
        ("cam.webm", {}, 415),
        ("cam.mp4", {"minimum": 101, "maximum": 100}, 422),
        ("cam.mov", {"youtube_url": "https://youtube.com.evil.example/watch?v=a"}, 422),
        ("cam.mkv", {"youtube_url": "https://youtube.com@evil.example/watch?v=a"}, 422),
        ("cam.mp4", {"license": "    "}, 422),
    ],
)
def test_rejects_invalid_metadata(client, name, data, status):
    http, config, _ = client
    assert upload(http, name, **data).status_code == status
    assert not list((config.data / "input").iterdir())


def test_upload_limit_and_empty_file(client):
    http, config, _ = client
    assert upload(http, content=b"x" * 129).status_code == 413
    assert upload(http, content=b"").status_code == 422
    assert not list((config.data / "tmp").iterdir())
    assert not list((config.data / "input").iterdir())


def test_streaming_body_is_bounded_without_content_length(client):
    http, config, _ = client
    boundary = "cliplab-boundary"

    def chunks():
        yield (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="sample.mp4"\r\nContent-Type: video/mp4\r\n\r\n'
        ).encode()
        yield b"x" * (MIB + 300)
        yield f"\r\n--{boundary}--\r\n".encode()

    response = http.post(
        "/api/jobs",
        content=chunks(),
        headers={**HEADERS, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code in {400, 413}
    assert not list((config.data / "input").iterdir())


def test_persistent_queue_recovery_and_cancel(client):
    http, config, store = client
    one = upload(http, r"..\..\minha live.MOV", youtube_url="https://youtu.be/exemplo").json()
    two = upload(http, "dois.mkv").json()
    assert one["filename"] == "minha live.MOV"
    assert "stored_name" not in one
    assert one["source"]["youtube_url"] == "https://youtu.be/exemplo"
    assert (config.data / "input" / one["id"] / "source.mov").is_file()
    assert http.get("/api/jobs/%2e%2e").status_code == 404
    assert http.get("/api/jobs/%2e%2e%2fsecret").status_code in {404, 422}
    assert http.get(f"/api/jobs/{one['id']}/zip").status_code == 409
    assert store.next_work() == (one["id"], None)
    store.update(one["id"], lambda job: job.update(state="TRANSCRIBING", progress=30))
    store.update(two["id"], lambda job: job.update(state="RENDERING", cancel_requested=True))
    reloaded = type(store)(store.path)
    reloaded.recover()
    assert reloaded.get(one["id"])["state"] == "CREATED"
    assert reloaded.get(two["id"])["state"] == "CANCELLED"
    assert (
        http.post(f"/api/jobs/{one['id']}/cancel", headers=HEADERS).json()["state"] == "CANCELLED"
    )
    assert http.post(f"/api/jobs/{one['id']}/retry", headers=HEADERS).status_code == 202
    assert reloaded.next_work() == (one["id"], None)


@pytest.mark.media
@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg ausente"
)
def test_licensed_track_and_zip(client):
    http, config, store = client
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 32000)
    fields = {**LICENSE, "title": "Trilha de teste", "attribution": "Crédito: Autora de teste"}
    for authorized, expected in (("false", 422), ("true", 201)):
        response = http.post(
            "/api/music",
            headers=HEADERS,
            files={"file": ("sound.wav", audio.getvalue())},
            data={**fields, "authorized": authorized},
        )
        assert response.status_code == expected, response.text
    track = response.json()
    assert http.get("/api/music").json() == [track]
    job = upload(http, music_mode="licensed", music_id=track["id"]).json()
    clip = {
        "id": "f" * 32,
        "revision": 1,
        "status": "approved",
        "preview_ready": True,
        "final_ready": True,
        "music_applied": True,
        "operation": None,
        "start": 0.0,
        "end": 75.0,
        "title": "Instância de teste",
        "hook": "",
        "description": "",
    }
    job_id = job["id"]
    output = config.data / "output" / job_id
    output.mkdir()
    preview, video, captions = revision_files(output, clip)
    preview.write_bytes(b"test preview")
    video.write_bytes(b"test video")
    captions.write_text("test captions", encoding="utf-8")
    (output / "transcript.json").write_text('{"segments":[]}', encoding="utf-8")
    store.update(
        job_id,
        lambda row: row.update(
            state="READY_FOR_REVIEW",
            clips=[clip],
            duration=160,
            transcript_available=True,
        ),
    )
    base = f"/api/jobs/{job_id}/clips/{clip['id']}"
    assert http.get(f"{base}/preview", headers={"Range": "bytes=0-3"}).status_code == 206
    assert http.get(f"{base}/mp4").content == b"test video"
    assert http.get(f"{base}/srt").text == "test captions"
    assert http.get(f"/api/jobs/{job_id}/transcript.json").json()["segments"] == []
    archive = http.get(f"/api/jobs/{job_id}/zip")
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as exported:
        assert exported.testzip() is None
        metadata = json.loads(exported.read("editorial.json"))
        assert metadata["clips"][0]["music"]["license"] == LICENSE["license"]
        assert "Crédito" in exported.read("atribuicao-musical.txt").decode()
        assert exported.read("corte-01.mp4") == b"test video"
        assert exported.read("corte-01.srt") == b"test captions"
    assert not list((config.data / "tmp").iterdir())
    assert http.patch(base, headers=HEADERS, json={"start": 20, "end": 70}).status_code == 422
    assert http.patch(base, headers=HEADERS, json={"status": "discarded"}).status_code == 200
    assert http.get(f"{base}/mp4").status_code == 404
    assert http.post(f"{base}/export", headers=HEADERS).status_code == 409
    edited = http.patch(
        base,
        headers=HEADERS,
        json={"title": "Novo título", "status": "pending"},
    ).json()
    assert (
        edited["revision"] == 2 and not edited["final_ready"] and edited["operation"] == "preview"
    )
    assert http.get(f"{base}/preview").status_code == 404


def test_corruption_is_recoverable_in_worker(tmp_path: Path):
    app = create_app(Config(tmp_path, min_free=MIB))
    with TestClient(app, base_url="http://127.0.0.1") as http:
        job = upload(http, content=b"not an mp4").json()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            job = http.get(f"/api/jobs/{job['id']}").json()
            if job["state"] == "FAILED":
                break
            time.sleep(0.1)
        assert job["state"] == "FAILED"
        assert job["error"]
        assert "FAILED" in http.get(f"/api/jobs/{job['id']}/log").text
        assert http.post(f"/api/jobs/{job['id']}/retry", headers=HEADERS).status_code == 202
