"""Local, bounded media processing with FFmpeg and faster-whisper."""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
import wave
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

from .captions import write_ass

VIDEO_FORMATS = {".mp4": "mov", ".mov": "mov", ".mkv": "matroska"}
MUSIC_FORMATS = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".m4a": "mov",
    ".aac": "aac",
    ".ogg": "ogg",
    ".flac": "flac",
}
MUSIC_FALLBACK_WARNING = "A trilha sonora falhou. O vídeo foi renderizado sem música."
SAMPLE_RATE = 16_000
FPS = 30
MIN_SECONDS = 61
MAX_SECONDS = 180
CancelCheck = Callable[[], bool]
Progress = Callable[[float], None]


class MediaError(RuntimeError):
    """The source or an external media operation failed."""


class MediaCancelled(MediaError):
    """Processing was cancelled before publishing an asset."""


def _check_cancel(cancel: CancelCheck | None) -> None:
    if cancel and cancel():
        raise MediaCancelled("Processamento cancelado.")


def _run(
    args: list[str],
    failure: str,
    timeout: float,
    *,
    cancel: CancelCheck | None = None,
    working_dir: Path | None = None,
    on_poll: Callable[[], None] | None = None,
) -> str:
    _check_cancel(cancel)
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=working_dir,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise MediaError(f"Não foi possível iniciar {args[0]}. Confira a instalação.") from exc

        try:
            while process.poll() is None:
                _check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise MediaError(f"{failure} O tempo limite foi atingido.")
                if on_poll:
                    on_poll()
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            _check_cancel(cancel)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        if process.returncode:
            stderr.seek(max(0, stderr.tell() - 2048))
            raw_error = stderr.read().decode("utf-8", errors="replace")
            lines = [line for line in raw_error.splitlines() if line.strip()]
            detail = lines[-1].strip() if lines else f"Código {process.returncode}."
            raise MediaError(f"{failure} {detail}")
        stdout.seek(0)
        return stdout.read().decode("utf-8", errors="replace")


def _input(path: Path, formats: dict[str, str]) -> list[str]:
    demuxer = formats.get(path.suffix.lower())
    if not demuxer:
        raise MediaError("Formato de arquivo não aceito.")
    arguments = ["-protocol_whitelist", "file", "-f", demuxer]
    if demuxer == "mov":
        arguments.extend(["-enable_drefs", "0", "-use_absolute_path", "0"])
    return [*arguments, "-i", str(path.resolve())]


def _ffprobe(path: Path, formats: dict[str, str], cancel: CancelCheck | None) -> dict:
    entries = (
        "format=duration,format_name:"
        "stream=codec_type,codec_name,width,height,duration,r_frame_rate,"
        "avg_frame_rate,sample_aspect_ratio,display_aspect_ratio:"
        "stream_disposition=attached_pic"
    )
    raw = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            entries,
            "-of",
            "json",
            *_input(path, formats),
        ],
        "Não foi possível ler o arquivo de mídia.",
        45,
        cancel=cancel,
    )
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MediaError("FFprobe retornou metadados inválidos.") from exc


def _duration(item: dict) -> float | None:
    try:
        value = float(item["duration"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _streams(info: dict) -> tuple[dict, dict]:
    streams = info.get("streams", [])
    video = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic")
        ),
        {},
    )
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    return video, audio


def _decoding_sample(
    path: Path, formats: dict[str, str], maps: list[str], cancel: CancelCheck | None
) -> None:
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            *_input(path, formats),
            *maps,
            "-t",
            "1",
            "-f",
            "null",
            "-",
        ],
        "O arquivo contém dados que não puderam ser decodificados.",
        60,
        cancel=cancel,
    )


def probe_source(path: Path, max_seconds: float, *, cancel: CancelCheck | None = None) -> dict:
    """Inspect streams and decode media before accepting the source."""
    info = _ffprobe(path, VIDEO_FORMATS, cancel)
    video, audio = _streams(info)
    if not (video and audio):
        raise MediaError("A gravação precisa conter uma faixa de vídeo e uma faixa de áudio.")
    container_duration = _duration(info.get("format", {}))
    if container_duration is None:
        raise MediaError("Não foi possível verificar a duração da gravação.")
    if container_duration > max_seconds:
        raise MediaError("A gravação excede o limite de duração configurado.")
    durations = [container_duration, _duration(video), _duration(audio)]
    duration = min(value for value in durations if value is not None)
    if duration < MIN_SECONDS:
        raise MediaError("A gravação precisa ter pelo menos 61 segundos de vídeo e áudio.")
    width, height = video.get("width", 0), video.get("height", 0)
    if width <= 0 or height <= 0 or width * height > 33_177_600:
        raise MediaError("A resolução da gravação é inválida ou excede o limite de 8K.")
    _decoding_sample(path, VIDEO_FORMATS, ["-map", "0:V:0", "-map", "0:a:0"], cancel)
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "video_codec": video["codec_name"],
        "audio_codec": audio["codec_name"],
    }


def probe_music(path: Path, *, cancel: CancelCheck | None = None) -> dict:
    info = _ffprobe(path, MUSIC_FORMATS, cancel)
    _, audio = _streams(info)
    duration = _duration(info.get("format", {}))
    if not audio or duration is None:
        raise MediaError("A trilha precisa conter uma faixa de áudio válida.")
    _decoding_sample(path, MUSIC_FORMATS, ["-map", "0:a:0"], cancel)
    return {"duration": duration, "codec": audio["codec_name"]}


def extract_audio(source: Path, output: Path, *, cancel: CancelCheck | None = None) -> None:
    """Stream a 16 kHz PCM waveform on the original source timeline."""
    with tempfile.TemporaryDirectory(prefix="audio-", dir=output.parent) as scratch:
        temporary = Path(scratch) / "audio.wav"
        _run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                *_input(source, VIDEO_FORMATS),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-af",
                "aresample=16000:async=1:first_pts=0",
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                str(temporary),
            ],
            "Não foi possível extrair o áudio.",
            7200,
            cancel=cancel,
        )
        temporary.replace(output)


class Transcriber:
    """Load one CPU/int8 model lazily and process long waveforms in bounded chunks."""

    chunk_seconds = 480
    overlap_seconds = 3

    def __init__(self, model_name: str, download_root: Path):
        self.model_name = model_name
        self.download_root = download_root
        self._model = None

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self.download_root.mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=min(8, max(1, os.cpu_count() or 1)),
                download_root=str(self.download_root),
            )
        return self._model

    def transcribe(
        self,
        audio: Path,
        language: str,
        *,
        cancel: CancelCheck | None = None,
        progress: Progress | None = None,
    ) -> dict:
        _check_cancel(cancel)
        requested = {"auto": None, "pt-BR": "pt", "pt-br": "pt"}.get(language, language)
        try:
            return self._transcribe(audio, requested, cancel, progress)
        except MediaCancelled:
            raise
        except Exception as exc:
            raise MediaError(
                "Não foi possível transcrever o áudio. Confira o modelo faster-whisper, "
                f"o cache local e a conexão do primeiro download. {exc}"
            ) from exc

    def _transcribe(
        self,
        audio: Path,
        requested: str | None,
        cancel: CancelCheck | None,
        progress: Progress | None,
    ) -> dict:
        import numpy as np

        model = self._load_model()
        segments: list[dict] = []
        detected = requested
        language_probability = 1.0 if requested else None

        with wave.open(str(audio)) as stream:
            frame_count = stream.getnframes()
            if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (
                SAMPLE_RATE,
                1,
                2,
            ):
                raise MediaError("O áudio de transcrição precisa ser PCM mono de 16 kHz.")
            duration = frame_count / SAMPLE_RATE
            chunk = self.chunk_seconds * SAMPLE_RATE
            overlap = self.overlap_seconds * SAMPLE_RATE
            for begin in range(0, frame_count, chunk):
                _check_cancel(cancel)
                last = min(frame_count, begin + chunk)
                read_start = max(0, begin - overlap)
                offset = read_start / SAMPLE_RATE
                stream.setpos(read_start)
                raw = stream.readframes(min(frame_count, last + overlap) - read_start)
                signal = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
                produced, info = model.transcribe(
                    signal,
                    language=detected,
                    task="transcribe",
                    word_timestamps=True,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                    beam_size=3,
                    condition_on_previous_text=False,
                )

                for segment in produced:
                    _check_cancel(cancel)
                    record = self._record(
                        segment, offset, begin / SAMPLE_RATE, last / SAMPLE_RATE, duration
                    )
                    if record:
                        segments.append(record)
                        if detected is None:
                            detected = str(info.language)
                            probability = float(info.language_probability)
                            language_probability = (
                                probability if math.isfinite(probability) else None
                            )
                        if progress:
                            progress(100 * min(last / SAMPLE_RATE, record["end"]) / duration)

                if progress:
                    progress(100 * last / frame_count)

        pauses = _pauses(segments, duration)
        low_confidence = sum(1 for segment in segments if segment["confidence"] < 0.58)
        warnings = []
        if not segments:
            warnings.append("A transcrição não detectou fala utilizável.")
        if low_confidence:
            warnings.append(
                f"{low_confidence} segmento(s) com baixa confiança precisam de revisão."
            )
        return {
            "language": detected or "und",
            "language_probability": language_probability,
            "duration": round(duration, 3),
            "model": str(self.model_name),
            "segments": segments,
            "pauses": pauses,
            "low_confidence_segments": low_confidence,
            "warnings": warnings,
        }

    @staticmethod
    def _record(segment, offset: float, begin: float, end: float, duration: float) -> dict | None:
        words = []
        for word in segment.words or ():
            start, finish = float(word.start) + offset, float(word.end) + offset
            if not (math.isfinite(start) and math.isfinite(finish)):
                continue
            start, finish = max(0.0, start), min(duration, finish)
            if start < finish and begin <= (start + finish) / 2 < end:
                probability = float(word.probability)
                words.append(
                    {
                        "start": round(start, 3),
                        "end": round(finish, 3),
                        "text": str(word.word),
                        "confidence": round(probability, 4) if math.isfinite(probability) else 0.0,
                    }
                )

        if words:
            text = "".join(word["text"] for word in words).strip()
            start, finish = words[0]["start"], words[-1]["end"]
            confidence = sum(word["confidence"] for word in words) / len(words)
        elif segment.words:
            return None
        else:
            start, finish = float(segment.start) + offset, float(segment.end) + offset
            if not (math.isfinite(start) and math.isfinite(finish)):
                return None
            start, finish = max(begin, start), min(end, finish)
            text = str(segment.text).strip()
            logprob = float(segment.avg_logprob)
            confidence = math.exp(min(0.0, logprob)) if math.isfinite(logprob) else 0.0

        if not text or start >= finish:
            return None
        return {
            "start": round(start, 3),
            "end": round(finish, 3),
            "text": text,
            "confidence": round(confidence, 4),
            "words": words,
        }


def _pauses(segments: list[dict], duration: float) -> list[dict]:
    pauses = []
    cursor = 0.0
    for segment in segments:
        if segment["start"] - cursor >= 0.5:
            pauses.append({"start": round(cursor, 3), "end": segment["start"]})
        cursor = max(cursor, segment["end"])
    if duration - cursor >= 0.5:
        pauses.append({"start": round(cursor, 3), "end": round(duration, 3)})
    return pauses


def _size(preview: bool) -> tuple[int, int]:
    return (540, 960) if preview else (1080, 1920)


def _fit_video(width: int, height: int, *, fill: bool = False) -> str:
    # Scale using display aspect ratio, including anamorphic footage on older FFmpeg versions.
    if fill:
        sw = f"if(gte(dar,{width}/{height}),ceil({height}*dar/2)*2,{width})"
        sh = f"if(gte(dar,{width}/{height}),{height},ceil({width}/dar/2)*2)"
    else:
        sw = f"if(gte(dar,{width}/{height}),{width},max(2,trunc({height}*dar/2)*2))"
        sh = f"if(gte(dar,{width}/{height}),max(2,trunc({width}/dar/2)*2),{height})"
    return f"scale=w='{sw}':h='{sh}':flags=bilinear,setsar=1"


def _video_graph(width: int, height: int, preset: str, emphasis: str) -> str:
    dossier = preset in {"dossier", "dossie", "leonida", "dossie-leonida", "dossie_leonida"}
    if dossier:
        left = width // 24
        top = height // 3
        strip = max(2, width // 190)
        accent = {"low": 0.35, "medium": 0.62, "high": 0.9}.get(emphasis, 0.62)
        decorations = (
            f"drawbox=x={left}:y={top}:w={width // 8}:h={strip}:color=0xFC3EA9@{accent}:t=fill,"
            f"drawbox=x={width - left - width // 8}:y={top}:w={width // 8}:h={strip}:"
            f"color=0x41E7F5@{accent}:t=fill,"
            f"drawbox=x={left}:y={height * 7 // 9}:w={width - 2 * left}:h={strip}:"
            f"color=0x41E7F5@{accent}:t=fill"
        )
        return (
            f"color=c=0x161821:s={width}x{height}:r={FPS}[back];"
            f"[0:V:0]fps={FPS},{_fit_video(width - 2 * left, height // 2)}[front];"
            f"[back][front]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1,"
            f"{decorations},ass=clip.ass,format=yuv420p[v]"
        )

    small_width, small_height = 180, 320
    return (
        f"[0:V:0]fps={FPS},split=2[large][center];"
        f"[large]{_fit_video(small_width, small_height, fill=True)},"
        f"crop={small_width}:{small_height},boxblur=12:2,eq=brightness=-0.22:saturation=0.65,"
        f"scale={width}:{height}:flags=bilinear[back];"
        f"[center]{_fit_video(width, height * 11 // 20)}[front];"
        "[back][front]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1,"
        "ass=clip.ass,format=yuv420p[v]"
    )


def _music_input(mode: str, music: Path | None, duration: float) -> list[str]:
    if mode == "licensed":
        if music is None:
            raise MediaError("Trilha licenciada indisponível no disco.")
        return ["-stream_loop", "-1", *_input(music, MUSIC_FORMATS)]
    # Additive ambient pad wholly synthesized by FFmpeg. No samples or melody recordings.
    pad = (
        "(0.22*sin(2*PI*73.42*t)+0.12*sin(2*PI*110*t)"
        "+0.05*sin(2*PI*146.83*t)+0.045*sin(2*PI*174.61*t)"
        "*(0.5+0.5*sin(2*PI*0.11*t)))*(0.68+0.30*sin(2*PI*0.071*t))"
    )
    return ["-f", "lavfi", "-i", f"aevalsrc={pad}:s=48000:d={duration:.6f}"]


def _audio_graph(with_music: bool, volume: float, duration: float) -> str:
    voice = (
        "[0:a:0]aresample=48000:async=1:first_pts=0,highpass=f=55,"
        "loudnorm=I=-16:LRA=11:TP=-1.5,"
        "aformat=sample_rates=48000:channel_layouts=stereo,apad"
    )
    limiter = "alimiter=limit=0.89:level=false:latency=1[a]"
    if not with_music:
        return f"{voice},{limiter}"
    return (
        f"{voice},asplit=2[voice][key];"
        "[1:a:0]aformat=sample_rates=48000:channel_layouts=stereo,"
        f"afade=t=in:st=0:d=1.1,afade=t=out:st={max(0, duration - 1.3):.3f}:d=1.3,"
        f"volume={volume:.4f}[music];"
        "[music][key]sidechaincompress=threshold=0.018:ratio=8:attack=16:release=370[bed];"
        f"[voice][bed]amix=inputs=2:duration=first:normalize=0,{limiter}"
    )


def _render(
    source: Path,
    output: Path,
    span: tuple[float, float],
    selection: tuple[str, str, bool],
    soundtrack: tuple[str, float, Path | None],
    cancel: CancelCheck | None,
    progress: Progress | None,
) -> None:
    start, duration = span
    preset, emphasis, preview = selection
    music_mode, volume, music_path = soundtrack
    width, height = _size(preview)
    with_music = music_mode != "off" and volume > 0
    music_inputs = _music_input(music_mode, music_path, duration) if with_music else []
    progress_file = output.parent / "progress.txt"
    progress_file.unlink(missing_ok=True)

    def on_poll() -> None:
        if not progress or not progress_file.exists():
            return
        with progress_file.open("rb") as progress_stream:
            size = progress_stream.seek(0, os.SEEK_END)
            progress_stream.seek(max(0, size - 1024))
            lines = progress_stream.read(1024).decode("ascii", errors="replace").splitlines()
        for line in reversed(lines):
            value = line.removeprefix("out_time_us=")
            if value == line or not value.isdecimal():
                continue
            progress(min(99.0, 100 * int(value) / 1_000_000 / duration))
            break

    graph = f"{_video_graph(width, height, preset, emphasis)};{_audio_graph(with_music, volume, duration)}"
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-progress",
            "progress.txt",
            "-nostats",
            "-y",
            "-ss",
            f"{start:.6f}",
            *_input(source, VIDEO_FORMATS),
            *music_inputs,
            "-filter_complex_threads",
            "2",
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:v",
            "libx264",
            "-threads:v",
            "2",
            "-preset",
            "veryfast",
            "-crf",
            "26" if preview else "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k" if preview else "192k",
            "-movflags",
            "+faststart",
            "-t",
            f"{duration:.6f}",
            str(output),
        ],
        "Não foi possível renderizar o corte.",
        max(150, duration * 20),
        cancel=cancel,
        working_dir=output.parent,
        on_poll=on_poll,
    )


def render_clip(
    source: Path,
    output: Path,
    transcript: dict,
    start: float,
    end: float,
    *,
    title: str,
    hook: str = "",
    preset: str = "dossier",
    emphasis: str = "medium",
    preview: bool = False,
    music_mode: str = "off",
    music_volume: float = 0.12,
    music_path: Path | None = None,
    cta: str = "",
    cancel: CancelCheck | None = None,
    progress: Progress | None = None,
) -> list[str]:
    duration = end - start
    if not (0 <= start and MIN_SECONDS <= round(duration, 6) <= MAX_SECONDS):
        raise MediaError("O corte precisa ter duração entre 61 e 180 segundos.")
    if music_mode not in {"off", "procedural", "licensed"} or not 0 <= music_volume <= 1:
        raise MediaError("A configuração da trilha sonora é inválida.")

    high_water = 0.0

    def report(value: float) -> None:
        nonlocal high_water
        if progress and value > high_water:
            high_water = value
            progress(value)

    render_progress = report if progress else None
    warnings = []
    with tempfile.TemporaryDirectory(prefix="render-", dir=output.parent) as scratch:
        staging = Path(scratch).resolve()
        write_ass(
            staging / "clip.ass",
            transcript,
            start,
            end,
            title=title,
            hook=hook,
            preset=preset,
            emphasis=emphasis,
            cta=cta,
        )
        candidate = staging / "candidate.mp4"
        span = (start, duration)
        selection = (preset, emphasis, preview)
        try:
            _render(
                source,
                candidate,
                span,
                selection,
                (music_mode, music_volume, music_path),
                cancel,
                render_progress,
            )
        except MediaCancelled:
            raise
        except MediaError:
            if music_mode == "off" or music_volume == 0:
                raise
            _render(source, candidate, span, selection, ("off", 0, None), cancel, render_progress)
            warnings.append(MUSIC_FALLBACK_WARNING)
        candidate.replace(output)

    report(100)
    return warnings


def verify_output(
    path: Path, duration: float, *, preview: bool = False, cancel: CancelCheck | None = None
) -> dict:
    info = _ffprobe(path, VIDEO_FORMATS, cancel)
    video, audio = _streams(info)
    measured = _duration(info.get("format", {}))
    video_duration = _duration(video)
    audio_duration = _duration(audio)
    try:
        fps = float(Fraction(video.get("avg_frame_rate", "0")))
    except (ValueError, ZeroDivisionError):
        fps = 0
    width, height = _size(preview)
    expected_streams = (
        (video.get("width"), video.get("height")) == (width, height)
        and video.get("codec_name") == "h264"
        and audio.get("codec_name") == "aac"
        and video.get("sample_aspect_ratio") == "1:1"
        and abs(fps - FPS) <= 0.025
    )
    if not expected_streams:
        raise MediaError("O MP4 não possui o vídeo vertical H.264 e o áudio AAC esperados.")
    if (
        measured is None
        or not MIN_SECONDS <= measured <= MAX_SECONDS
        or abs(measured - duration) > 0.10
        or video_duration is None
        or audio_duration is None
        or abs(video_duration - duration) > 0.10
        or abs(audio_duration - duration) > 0.10
    ):
        raise MediaError("A duração do MP4 não corresponde ao corte solicitado.")
    return {
        "duration": measured,
        "video_duration": video_duration,
        "audio_duration": audio_duration,
        "width": width,
        "height": height,
        "aspect_ratio": "9:16",
        "fps": fps,
        "video_codec": video["codec_name"],
        "audio_codec": audio["codec_name"],
        "format": "mp4",
    }
