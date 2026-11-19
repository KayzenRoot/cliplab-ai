"""Media and caption tests using generated, rights-free fixtures."""

import json
import shutil
import subprocess
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from cliplab.captions import caption_blocks, write_ass, write_srt
from cliplab.media import (
    MUSIC_FALLBACK_WARNING,
    MediaCancelled,
    MediaError,
    Transcriber,
    extract_audio,
    probe_music,
    probe_source,
    render_clip,
    verify_output,
)


@pytest.fixture(scope="module")
def fixture_video(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe are required for real media integration")
    folder = tmp_path_factory.mktemp("synthetic media")
    path = folder / "autoria própria.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=160:sample_rate=48000",
            "-t",
            "68",
            "-c:v",
            "libx264",
            "-threads:v",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path


@pytest.fixture
def timed_transcript():
    return {
        "duration": 68,
        "segments": [
            {
                "start": 1.05,
                "end": 4.3,
                "text": "Essa palavra não entra. Uma descoberta em Leonida.",
                "words": [
                    {"start": 1.05, "end": 1.55, "text": "Cortar"},
                    {"start": 2.00, "end": 2.39, "text": "Uma"},
                    {"start": 2.45, "end": 3.10, "text": "descoberta"},
                    {"start": 3.12, "end": 3.43, "text": "em"},
                    {"start": 3.46, "end": 4.30, "text": "Leonida."},
                ],
            }
        ],
    }


@pytest.mark.media
def test_source_probes_media_and_enforces_limits(fixture_video, tmp_path):
    assert probe_source(fixture_video, 300)["duration"] == 68
    with pytest.raises(MediaError, match="excede o limite de duração"):
        probe_source(fixture_video, 67)

    with pytest.raises(MediaError, match="Formato de arquivo"):
        probe_source(tmp_path / "disfarçado.avi", 300)

    invalid = tmp_path / "corrompido.mp4"
    invalid.write_bytes(b"incomplete file")
    with pytest.raises(MediaError, match="Não foi possível ler"):
        probe_source(invalid, 300)


@pytest.mark.media
@pytest.mark.parametrize("suffix", [".mkv", ".mov"])
def test_probe_supports_alternative_containers(fixture_video, tmp_path, suffix):
    copy = tmp_path / f"video{suffix}"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(fixture_video), "-c", "copy", str(copy)],
        check=True,
        timeout=20,
    )
    assert probe_source(copy, 300)["duration"] == pytest.approx(68.0, abs=0.04)


@pytest.mark.media
def test_extract_pcm_and_cancel(fixture_video, tmp_path):
    path = tmp_path / "audio.wav"
    with pytest.raises(MediaCancelled):
        extract_audio(fixture_video, path, duration=68, cancel=lambda: True)
    assert not path.exists()
    assert not list(tmp_path.iterdir())

    extract_audio(fixture_video, path, duration=12)
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() / 16000 == pytest.approx(12.0, abs=0.05)
        assert len(set(wav.readframes(16000))) > 10


@pytest.mark.media
def test_probe_preserves_recording_timeline_with_delayed_audio(fixture_video, tmp_path):
    delayed = tmp_path / "delayed.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(fixture_video),
            "-itsoffset",
            "10",
            "-i",
            str(fixture_video),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-t",
            "68",
            "-c",
            "copy",
            str(delayed),
        ],
        check=True,
        timeout=60,
    )
    media = probe_source(delayed, 100)
    assert media["duration"] == pytest.approx(68.0, abs=0.08)
    wav_path = tmp_path / "offset.wav"
    extract_audio(delayed, wav_path, duration=media["duration"])
    with wave.open(str(wav_path)) as wav:
        assert wav.getnframes() / 16000 == pytest.approx(68.0, abs=0.08)
        assert not any(wav.readframes(9 * 16000))


def _word(start, end, text, probability=0.98):
    return SimpleNamespace(
        start=np.float64(start),
        end=np.float64(end),
        word=np.str_(text),
        probability=np.float32(probability),
    )


def _segment(words):
    return SimpleNamespace(
        start=words[0].start,
        end=words[-1].end,
        text="".join(word.word for word in words),
        words=words,
        avg_logprob=-0.1,
    )


def test_transcription_preserves_chunk_offsets_and_flags_low_confidence(monkeypatch, tmp_path):
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 16000 * 6)

    transcriber = Transcriber("tiny", tmp_path)
    transcriber.chunk_seconds = 3
    transcriber.overlap_seconds = 1
    results = iter(
        [
            [_segment([_word(1.1, 1.5, "Primeiro"), _word(3.1, 3.4, " depois")])],
            [_segment([_word(0.3, 0.5, "descartar"), _word(1.1, 1.4, "depois", 0.45)])],
        ]
    )

    def fake_transcribe(signal, **options):
        assert 16000 < len(signal) <= 4 * 16000
        assert options["word_timestamps"] and options["vad_filter"]
        return next(results), SimpleNamespace(
            language=np.str_("pt"), language_probability=np.float32(0.997)
        )

    monkeypatch.setattr(
        transcriber, "_load_model", lambda: SimpleNamespace(transcribe=fake_transcribe)
    )
    progress = []
    report = transcriber.transcribe(audio, "auto", progress=progress.append)
    assert report["language"] == "pt"
    assert report["language_probability"] == pytest.approx(0.997)
    assert [segment["text"] for segment in report["segments"]] == ["Primeiro", "depois"]
    assert report["segments"][1]["words"][0]["start"] == 3.1
    assert report["low_confidence_segments"] == 1
    assert json.loads(json.dumps(report, allow_nan=False)) == report
    assert report["warnings"]
    assert len(report["pauses"]) == 3
    assert progress == sorted(progress)
    assert progress[-1] == 100


def test_captions_are_clip_relative_and_escape_ass_controls(tmp_path, timed_transcript):
    path = tmp_path / "captions.srt"
    write_srt(path, timed_transcript, 1.2, 65)
    text = path.read_text(encoding="utf-8")
    assert "00:00:00,800 --> 00:00:03,100" in text
    assert "Uma descoberta em Leonida." in text
    assert "Cortar" not in text
    assert max(map(len, caption_blocks(timed_transcript, 1.2, 65))) <= 5

    path = tmp_path / "captions.ass"
    write_ass(
        path,
        timed_transcript,
        1.2,
        65,
        title="Título {\\fs999}",
        hook="",
        preset="dossier",
        emphasis="medium",
    )
    text = path.read_text(encoding="utf-8")
    assert r"{\fs999}" not in text
    assert r"\kf" in text
    assert "0:00:00.80" in text
    assert "Cortar" not in text


@pytest.mark.media
@pytest.mark.parametrize(
    ("preset", "preview", "music", "width", "height"),
    [
        ("dossier", True, "procedural", 540, 960),
        ("gameplay", False, "off", 1080, 1920),
    ],
)
def test_ffmpeg_vertical_mp4_end_to_end(
    fixture_video, timed_transcript, tmp_path, preset, preview, music, width, height
):
    output = tmp_path / "export.mp4"
    progress = []
    assert (
        render_clip(
            fixture_video,
            output,
            timed_transcript,
            1.32,
            62.32,
            title="Uma descoberta",
            preset=preset,
            preview=preview,
            music_mode=music,
            progress=progress.append,
        )
        == []
    )
    actual = verify_output(output, 61, preview=preview)
    assert (actual["width"], actual["height"], actual["fps"]) == (width, height, 30)
    assert actual["duration"] == 61.0
    assert actual["aspect_ratio"] == "9:16"
    assert actual["video_codec"] == "h264"
    assert actual["audio_codec"] == "aac"
    assert progress[-1] == 100
    assert progress == sorted(progress)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-threads",
            "2",
            "-i",
            str(output),
            "-t",
            "1",
            "-f",
            "null",
            "-",
        ],
        check=True,
        timeout=30,
    )


@pytest.mark.media
def test_licensed_music_and_render_fallback(fixture_video, timed_transcript, tmp_path):
    track = tmp_path / "faixa autorizada.wav"
    with wave.open(str(track), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 16000 * 2)
    assert probe_music(track)["duration"] == 2

    output = tmp_path / "export.mp4"
    assert (
        render_clip(
            fixture_video,
            output,
            timed_transcript,
            1,
            62,
            title="Título",
            preview=True,
            music_mode="licensed",
            music_path=track,
        )
        == []
    )
    assert verify_output(output, 61, preview=True)["duration"] == 61

    track.unlink()
    assert render_clip(
        fixture_video,
        output,
        timed_transcript,
        1,
        62,
        title="Título",
        preview=True,
        music_mode="licensed",
        music_path=track,
    ) == [MUSIC_FALLBACK_WARNING]
    assert verify_output(output, 61, preview=True)["duration"] == 61
    assert not list(tmp_path.glob("render-*"))
