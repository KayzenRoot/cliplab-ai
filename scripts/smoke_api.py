"""Smoke com servidor Uvicorn, Whisper e FFmpeg reais."""

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from zipfile import ZipFile

import httpx

HEADERS = {"X-ClipLab": "1"}
FIELDS = {
    "author": "ClipLab AI",
    "origin": "Fixture sintética local em português",
    "license": "Fixture de teste criada no projeto",
    "authorized": "true",
    "count": "1",
    "minimum": "75",
    "maximum": "100",
    "language": "pt",
    "preset": "dossier",
    "music_mode": "procedural",
}


def checked(response: httpx.Response) -> httpx.Response:
    if not response.is_success:
        raise RuntimeError(f"HTTP {response.status_code} {response.request.url}: {response.text}")
    return response


def wait(client, url, predicate, states):
    deadline = time.monotonic() + 600
    last = None
    while time.monotonic() < deadline:
        job = checked(client.get(url)).json()
        status = (job["state"], job["message"])
        if status != last:
            states.append(job["state"])
            print(f"{job['state']} · {job['progress']}% · {job['message']}", flush=True)
            last = status
        if job["state"] in {"FAILED", "CANCELLED", "BLOCKED"}:
            raise RuntimeError(f"Projeto {job['id']} {job['state']}: {job['error']}")
        if predicate(job):
            return job
        time.sleep(0.6)
    raise TimeoutError(f"O projeto não chegou à etapa esperada: {url}")


def download(client, url, output):
    with client.stream("GET", url) as response:
        if not response.is_success:
            response.read()
            checked(response)
        with output.open("wb") as stream:
            for block in response.iter_bytes():
                stream.write(block)


def command(*args):
    result = subprocess.run(args, check=True, capture_output=True, text=True, encoding="utf-8")
    return result.stdout


def cue_seconds(cue: str) -> float:
    hours, minutes, seconds, milliseconds = map(int, re.split("[:,]", cue))
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def smoke(args):
    args.output.mkdir(parents=True, exist_ok=True)
    states = []
    with httpx.Client(base_url=args.url, headers=HEADERS, timeout=60) as client:
        assert "ClipLab AI" in checked(client.get("/")).text
        if args.job:
            project = f"/api/jobs/{args.job}"
        else:
            with args.input.open("rb") as source:
                created = checked(
                    client.post(
                        "/api/jobs",
                        data=FIELDS,
                        files={"file": (args.input.name, source, "video/mp4")},
                    )
                ).json()
            print(f"Projeto {created['id']}", flush=True)
            project = f"/api/jobs/{created['id']}"
        job = wait(client, project, lambda j: j["state"] == "READY_FOR_REVIEW", states)
        transcript = checked(client.get(f"{project}/transcript.json")).json()
        words = [word for segment in transcript["segments"] for word in segment["words"]]
        assert transcript["language"] == "pt" and len(words) >= 70
        assert all(0 <= word["start"] < word["end"] <= job["duration"] for word in words)
        clip = job["clips"][0]
        assert clip["preview_ready"] and 75 <= clip["end"] - clip["start"] <= 100
        endpoint = f"{project}/clips/{clip['id']}"
        preview = checked(client.get(f"{endpoint}/preview", headers={"Range": "bytes=0-1023"}))
        assert preview.status_code == 206
        title = "A rota alternativa no desafio"
        edited = checked(
            client.patch(
                endpoint,
                json={
                    "title": title,
                    "description": "Uma rota alternativa é testada durante o desafio.",
                },
            )
        ).json()
        if edited["operation"]:
            job = wait(client, project, lambda j: not j["clips"][0]["operation"], states)
        checked(client.patch(endpoint, json={"status": "approved"}))
        checked(client.post(f"{endpoint}/export"))
        job = wait(client, project, lambda j: not j["clips"][0]["operation"], states)
        clip = job["clips"][0]
        assert clip["final_ready"], clip["error"]
        assert clip["music_applied"], clip["warnings"]

        video = args.output / "ClipLab-exemplo.mp4"
        captions = args.output / "ClipLab-exemplo.srt"
        archive = args.output / "ClipLab-exemplo.zip"
        download(client, f"{endpoint}/mp4", video)
        download(client, f"{endpoint}/srt", captions)
        download(client, f"{project}/zip", archive)

    probe = json.loads(
        command(
            "ffprobe",
            "-v",
            "error",
            "-of",
            "json",
            "-show_entries",
            "stream=codec_name,codec_type,width,height,display_aspect_ratio,"
            "avg_frame_rate,duration:format=duration",
            str(video),
        )
    )
    video_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert (video_stream["width"], video_stream["height"]) == (1080, 1920)
    assert (video_stream["codec_name"], audio_stream["codec_name"]) == ("h264", "aac")
    assert (
        video_stream["display_aspect_ratio"] == "9:16" and video_stream["avg_frame_rate"] == "30/1"
    )
    duration = float(probe["format"]["duration"])
    assert 75 <= duration <= 100
    command("ffmpeg", "-v", "error", "-xerror", "-i", str(video), "-f", "null", "-")
    cues = re.findall(r"([0-9:,]+) --> ([0-9:,]+)", captions.read_text(encoding="utf-8-sig"))
    clip_words = [
        word for word in words if clip["start"] <= word["start"] and word["end"] <= clip["end"]
    ]
    assert len(cues) > 20
    assert abs(cue_seconds(cues[0][0]) - (clip_words[0]["start"] - clip["start"])) <= 0.05
    assert all(0 <= cue_seconds(start) < cue_seconds(end) <= duration for start, end in cues)
    with ZipFile(archive) as package:
        assert package.testzip() is None
        manifest = json.loads(package.read("editorial.json"))
        assert manifest["clips"][0]["title"] == title
        assert manifest["clips"][0]["music"]["title"].startswith("Trilha ambiente procedural")
        assert sorted(package.namelist()) == [
            "corte-01.mp4",
            "corte-01.srt",
            "editorial.json",
            "transcript.json",
            "transcript.srt",
        ]
        members = package.namelist()
    report = {
        "job_id": job["id"],
        "states_sampled": states,
        "source_duration": job["duration"],
        "whisper_language": transcript["language"],
        "segments": len(transcript["segments"]),
        "words": len(words),
        "clip": {key: clip[key] for key in ("start", "end", "score", "warnings", "music_applied")},
        "ffprobe": probe,
        "ffmpeg_full_decode": "passed",
        "srt_cues": len(cues),
        "srt_first_cue": cues[0],
        "srt_last_cue": cues[-1],
        "zip": members,
        "sha256": {path.name: digest(path) for path in (video, captions, archive)},
    }
    response = json.dumps(report, ensure_ascii=False, indent=2)
    (args.output / "ClipLab-evidencia.json").write_text(response + "\n", encoding="utf-8")
    print(response)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--input", type=Path, default=Path("data/smoke/input.mp4"))
    parser.add_argument("--output", type=Path, default=Path("data/smoke/export"))
    parser.add_argument("--job", help="Reutiliza um projeto existente para verificar a exportação.")
    smoke(parser.parse_args())
