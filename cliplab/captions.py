"""Render synchronized, clip-relative captions and editorial overlays."""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


def caption_blocks(transcript: dict, start: float, end: float) -> list[list[Word]]:
    words = []
    for segment in transcript["segments"]:
        for item in segment.get("words", []):
            text = " ".join(item["text"].split())
            if text and start <= item["start"] < item["end"] <= end:
                words.append(Word(item["start"] - start, item["end"] - start, text))
    words.sort(key=lambda word: word.start)

    blocks: list[list[Word]] = []
    block: list[Word] = []
    for word in words:
        if block and (
            len(block) == 5
            or word.start - block[-1].end > 0.45
            or word.end - block[0].start > 2.8
            or (len(block) >= 2 and block[-1].text.endswith((".", "!", "?")))
        ):
            blocks.append(block)
            block = []
        block.append(word)
    if block:
        blocks.append(block)
    return blocks


def _srt_time(seconds: float) -> str:
    total = round(seconds * 1000)
    minutes, milliseconds = divmod(total, 60_000)
    hours, minutes = divmod(minutes, 60)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def _ass_time(seconds: float) -> str:
    minutes, centiseconds = divmod(round(seconds * 100), 6000)
    hours, minutes = divmod(minutes, 60)
    seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{centiseconds:02}"


def _plain_words(block: list[Word]) -> str:
    return " ".join(word.text for word in block)


def write_srt(path: Path, transcript: dict, start: float = 0, end: float | None = None) -> None:
    end = transcript["duration"] if end is None else end
    lines = []
    blocks = caption_blocks(transcript, start, end)
    for index, block in enumerate(blocks):
        finish = (
            min(block[-1].end, blocks[index + 1][0].start)
            if (index + 1 < len(blocks))
            else block[-1].end
        )
        if _srt_time(block[0].start) == _srt_time(finish):
            continue
        text = _plain_words(block).replace("<", "‹").replace(">", "›")
        lines.append(
            f"{len(lines) + 1}\n{_srt_time(block[0].start)} --> {_srt_time(finish)}\n{text}\n"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _ass_text(value: str) -> str:
    # ASS override sequences must not come from the transcript or editable metadata.
    value = value.replace("\\", "＼").replace("{", "(").replace("}", ")")
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()


def _wrap(value: str, width: int, lines: int) -> str:
    return r"\N".join(textwrap.wrap(_ass_text(value), width=width, max_lines=lines))


def write_ass(
    path: Path,
    transcript: dict,
    start: float,
    end: float,
    *,
    title: str,
    hook: str,
    preset: str,
    emphasis: str,
    cta: str = "",
) -> None:
    dossier = preset in {"dossier", "dossie", "leonida", "dossie-leonida", "dossie_leonida"}
    active = "&HF5E741&" if dossier else "&H30E4FF&"
    style_size = {"low": 57, "medium": 62, "high": 67}.get(emphasis, 62)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Caption,Arial,{style_size},&H00FFFFFF,&H00FFFFFF,&H00141118,&H76000000,"
            "-1,0,0,0,100,100,0,0,1,5,2,2,96,130,460,1"
        ),
        (
            "Style: Title,Arial,68,&H00FFFFFF,&H00FFFFFF,&H00141118,&H84000000,"
            "-1,0,0,0,100,100,0,0,1,4,2,8,94,130,263,1"
        ),
        (
            "Style: Hook,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00141118,&H84000000,"
            "-1,0,0,0,100,100,0,0,1,3,1,8,90,128,447,1"
        ),
        (
            "Style: Label,Arial,35,&H00F5E741,&H00F5E741,&H00141118,&H84000000,"
            "-1,0,0,0,100,100,3,0,1,2,1,8,90,128,184,1"
        ),
        (
            "Style: Focus,Arial,32,&H00F5E741,&H00F5E741,&H00141118,&H84000000,"
            "-1,0,0,0,100,100,2,0,1,2,1,2,96,130,334,1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def dialogue(begin: float, finish: float, style: str, text: str) -> None:
        begin_time, finish_time = _ass_time(begin), _ass_time(finish)
        if begin_time != finish_time and text:
            lines.append(f"Dialogue: 0,{begin_time},{finish_time},{style},,0,0,0,,{text}")

    duration = end - start
    transition = {"low": 360, "medium": 260, "high": 170}.get(emphasis, 260)
    opening = rf"{{\fad({transition},380)}}"
    opening_title = _wrap(title, 26, 2)
    opening_hook = _wrap(hook, 39, 2)
    if dossier:
        dialogue(0, min(duration, 5), "Label", opening + "DOSSIÊ LEONIDA")
    if opening_title:
        dialogue(0, min(duration, 4.8), "Title", opening + opening_title)
    if opening_hook:
        dialogue(0.45, min(duration, 5.2), "Hook", opening + opening_hook)
    if dossier and emphasis != "low":
        focus = next(
            (
                segment["start"] - start
                for segment in transcript["segments"]
                if start + duration * 0.22 <= segment.get("start", 0) <= end - 6
                and segment.get("confidence", 0) >= 0.65
                and ("!" in segment.get("text", "") or "?" in segment.get("text", ""))
            ),
            None,
        )
        if focus is not None:
            dialogue(focus, focus + 2.2, "Focus", opening + "●  TRECHO EM FOCO")
    if dossier and cta.strip():
        dialogue(
            max(0, duration - 4.8),
            duration,
            "Title",
            opening + _wrap(cta, 28, 2),
        )

    blocks = caption_blocks(transcript, start, end)
    for block_index, block in enumerate(blocks):
        block_end = block[-1].end
        if block_index + 1 < len(blocks):
            block_end = min(block_end, blocks[block_index + 1][0].start)
        for index, word in enumerate(block):
            next_start = (
                min(block[index + 1].start, block_end) if (index + 1 < len(block)) else block_end
            )
            active_end = min(word.end, next_start)
            if active_end <= word.start:
                continue
            styled = [_ass_text(item.text) for item in block]
            sweep = max(1, min(16, round((active_end - word.start) * 100)))
            styled[index] = rf"{{\1c{active}\2c&H00FFFFFF&\kf{sweep}}}{styled[index]}{{\r}}"
            dialogue(word.start, active_end, "Caption", " ".join(styled))
            if active_end < next_start:
                dialogue(active_end, next_start, "Caption", _ass_text(_plain_words(block)))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
