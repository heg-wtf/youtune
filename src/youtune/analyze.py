"""Measure a video so an editor can pick its moments without watching all of it.

``analyze`` writes contact sheets (timestamped thumbnails), scene cuts, loudness
peaks (shouts, laughter, music hits) and a timestamped transcript, plus an
``analysis.json`` index. ``extract_frames`` pulls exact frames for a closer look.
"""

import json
import math
import re
import statistics
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from youtune.errors import MediaError
from youtune.fonts import find_font, load_font
from youtune.media import CommandRunner, MediaInformation, find_tool, probe, run_ffmpeg
from youtune.subtitles import Cue, read_srt

THUMBNAIL_WIDTH = 320
SHEET_COLUMNS = 4
SHEET_ROWS = 5
TARGET_FRAME_COUNT = 120
MINIMUM_FRAME_INTERVAL = 2.0
MAXIMUM_FRAME_INTERVAL = 30.0
SCENE_THRESHOLD = 0.35
LOUDNESS_SAMPLE_RATE = 8000
LOUDNESS_WINDOW_SECONDS = 0.5
SILENCE_DECIBELS = -120.0
PEAK_MARGIN_DECIBELS = 6.0
PEAK_SPACING_SECONDS = 4.0
MAXIMUM_PEAKS = 15
PROFILE_BUCKET_SECONDS = 10.0
ANALYSIS_NAME = "analysis.json"
TRANSCRIPT_NAME = "transcript.txt"
_SHOWINFO_TIME = re.compile(r"Parsed_showinfo.*?\bpts_time:\s*([0-9.]+)")
_LOUDNESS_TIME = re.compile(r"\bpts_time:\s*([0-9.]+)")
_LOUDNESS_LEVEL = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(\S+)")


@dataclass(frozen=True, slots=True)
class Sheet:
    path: str
    times: list[float]


@dataclass(frozen=True, slots=True)
class Peak:
    time: float
    decibels: float


@dataclass(frozen=True, slots=True)
class Analysis:
    video: str
    width: int
    height: int
    frame_rate: float
    duration: float
    frame_interval: float
    sheets: list[Sheet]
    scenes: list[float]
    peaks: list[Peak]
    median_decibels: float
    loudness_profile: list[tuple[float, float]]
    transcript: str | None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def frame_interval(duration: float) -> float:
    """Seconds between contact-sheet frames: about 120 frames, 2-30 s apart."""
    interval = duration / TARGET_FRAME_COUNT
    return round(min(max(interval, MINIMUM_FRAME_INTERVAL), MAXIMUM_FRAME_INTERVAL), 2)


def format_clock(seconds: float) -> str:
    """``mm:ss`` (``h:mm:ss`` past an hour)."""
    whole = max(0, int(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes:02}:{secs:02}"


def format_transcript(cues: Sequence[Cue]) -> str:
    """One ``[mm:ss.s] text`` line per cue."""
    lines = []
    for cue in cues:
        seconds = cue.start_milliseconds / 1000
        lines.append(f"[{format_clock(seconds)}.{int(seconds * 10) % 10}] {cue.text}")
    return "\n".join(lines) + ("\n" if lines else "")


def parse_scene_times(log: str) -> list[float]:
    return [round(float(match.group(1)), 2) for match in _SHOWINFO_TIME.finditer(log)]


def parse_loudness(text: str) -> list[tuple[float, float]]:
    """(window start, RMS dBFS) pairs from ``ametadata=print`` output."""
    windows: list[tuple[float, float]] = []
    time: float | None = None
    for line in text.splitlines():
        if (time_match := _LOUDNESS_TIME.search(line)) is not None:
            time = float(time_match.group(1))
        elif (level_match := _LOUDNESS_LEVEL.search(line)) is not None and time is not None:
            try:
                level = float(level_match.group(1))
            except ValueError:
                level = SILENCE_DECIBELS
            if not math.isfinite(level):
                level = SILENCE_DECIBELS
            windows.append((round(time, 2), round(max(level, SILENCE_DECIBELS), 1)))
            time = None
    return windows


def pick_peaks(windows: Sequence[tuple[float, float]]) -> tuple[list[Peak], float]:
    """The loudest moments: windows at least 6 dB over the median that are local
    maxima, at least 4 s apart, at most 15, in time order. Also returns the median."""
    audible = [level for _, level in windows if level > SILENCE_DECIBELS]
    if not audible:
        return [], SILENCE_DECIBELS
    median = statistics.median(audible)
    candidates = []
    for index, (time, level) in enumerate(windows):
        neighbours = windows[max(0, index - 2) : index + 3]
        if level >= median + PEAK_MARGIN_DECIBELS and level >= max(
            value for _, value in neighbours
        ):
            candidates.append(Peak(time, level))
    chosen: list[Peak] = []
    for peak in sorted(candidates, key=lambda entry: entry.decibels, reverse=True):
        if all(abs(peak.time - other.time) >= PEAK_SPACING_SECONDS for other in chosen):
            chosen.append(peak)
        if len(chosen) == MAXIMUM_PEAKS:
            break
    return sorted(chosen, key=lambda entry: entry.time), round(median, 1)


def loudness_profile(windows: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """The loudest window of every 10 s bucket."""
    buckets: dict[int, float] = {}
    for time, level in windows:
        bucket = int(time // PROFILE_BUCKET_SECONDS)
        buckets[bucket] = max(buckets.get(bucket, SILENCE_DECIBELS), level)
    return [(bucket * PROFILE_BUCKET_SECONDS, level) for bucket, level in sorted(buckets.items())]


def build_sheets(
    frames: Sequence[Path], times: Sequence[float], output_directory: Path
) -> list[Sheet]:
    """Tile thumbnails four across, five down per sheet, each stamped with its time."""
    font = load_font(find_font(), 22)
    per_sheet = SHEET_COLUMNS * SHEET_ROWS
    sheets = []
    for sheet_index in range(0, len(frames), per_sheet):
        chunk = list(
            zip(frames[sheet_index : sheet_index + per_sheet], times[sheet_index:], strict=False)
        )
        thumbnails = [Image.open(path).convert("RGB") for path, _ in chunk]
        cell_width = THUMBNAIL_WIDTH
        cell_height = max(image.height for image in thumbnails)
        rows = math.ceil(len(chunk) / SHEET_COLUMNS)
        sheet = Image.new("RGB", (cell_width * SHEET_COLUMNS, cell_height * rows), (16, 16, 16))
        draw = ImageDraw.Draw(sheet)
        for position, ((_, time), image) in enumerate(zip(chunk, thumbnails, strict=True)):
            x = (position % SHEET_COLUMNS) * cell_width
            y = (position // SHEET_COLUMNS) * cell_height
            sheet.paste(image, (x, y))
            label = format_clock(time)
            draw.rectangle((x, y, x + 8 + int(font.getlength(label)) + 8, y + 32), fill=(0, 0, 0))
            draw.text((x + 8, y + 4), label, font=font, fill=(255, 225, 77))
        name = f"sheet-{sheet_index // per_sheet + 1:02d}.jpg"
        sheet.save(output_directory / name, quality=82)
        sheets.append(Sheet(name, [round(time, 2) for _, time in chunk]))
    return sheets


def _frames_and_scenes(
    video_path: Path, directory: Path, interval: float, runner: CommandRunner
) -> tuple[list[Path], list[float]]:
    """One decode for both: thumbnails every ``interval`` s and scene-cut times."""
    graph = (
        f"[0:v]scale={THUMBNAIL_WIDTH}:-2,split=2[thumbnails][scenes_in];"
        f"[thumbnails]fps=1/{interval}[frames];"
        f"[scenes_in]select='gt(scene,{SCENE_THRESHOLD})',showinfo[scenes]"
    )
    command = [
        find_tool("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostats",
        "-y",
        "-i",
        str(video_path.resolve()),
        "-filter_complex",
        graph,
        "-map",
        "[frames]",
        "-q:v",
        "4",
        str(directory / "frame%05d.jpg"),
        "-map",
        "[scenes]",
        "-f",
        "null",
        "-",
    ]
    completed = runner(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise MediaError(f"ffmpeg failed reading {video_path.name}: {completed.stderr[-500:]}")
    return sorted(directory.glob("frame*.jpg")), parse_scene_times(completed.stderr)


def _loudness(
    video_path: Path, directory: Path, runner: CommandRunner
) -> list[tuple[float, float]]:
    samples = int(LOUDNESS_SAMPLE_RATE * LOUDNESS_WINDOW_SECONDS)
    metadata_path = directory / "loudness.txt"
    run_ffmpeg(
        [
            "-i",
            str(video_path.resolve()),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(LOUDNESS_SAMPLE_RATE),
            "-af",
            f"asetnsamples=n={samples}:p=0,"
            "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
            f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file={metadata_path}",
            "-f",
            "null",
            "-",
        ],
        description=f"measuring the loudness of {video_path.name}",
        runner=runner,
    )
    return (
        parse_loudness(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else []
    )


def analyze(
    video_path: Path,
    output_directory: Path,
    *,
    subtitle_path: Path | None = None,
    interval: float | None = None,
    information: MediaInformation | None = None,
    runner: CommandRunner = subprocess.run,
) -> Analysis:
    """Write sheets, ``transcript.txt`` and ``analysis.json`` into ``output_directory``."""
    information = information or probe(video_path, runner=runner)
    interval = interval or frame_interval(information.duration)
    output_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="youtune-analyze-") as temporary:
        directory = Path(temporary)
        frames, scenes = _frames_and_scenes(video_path, directory, interval, runner)
        times = [round(index * interval, 2) for index in range(len(frames))]
        sheets = build_sheets(frames, times, output_directory)
        windows = _loudness(video_path, directory, runner) if information.has_audio else []
    peaks, median = pick_peaks(windows)
    transcript = None
    if subtitle_path is not None:
        transcript = TRANSCRIPT_NAME
        (output_directory / TRANSCRIPT_NAME).write_text(
            format_transcript(read_srt(subtitle_path)), encoding="utf-8"
        )
    analysis = Analysis(
        video=str(video_path.resolve()),
        width=information.width,
        height=information.height,
        frame_rate=round(float(information.frame_rate), 3),
        duration=round(information.duration, 2),
        frame_interval=interval,
        sheets=sheets,
        scenes=scenes,
        peaks=peaks,
        median_decibels=median,
        loudness_profile=loudness_profile(windows),
        transcript=transcript,
    )
    (output_directory / ANALYSIS_NAME).write_text(analysis.to_json(), encoding="utf-8")
    return analysis


def frame_name(time: float) -> str:
    return f"frame-{time:09.3f}.jpg"


def extract_frames(
    video_path: Path,
    times: Sequence[float],
    output_directory: Path,
    *,
    width: int = 720,
    runner: CommandRunner = subprocess.run,
) -> list[Path]:
    """One JPEG per requested second, ``width`` pixels wide.

    Raises:
        MediaError: A time lies past the end of the video.
    """
    duration = probe(video_path, runner=runner).duration
    late = [time for time in times if time >= duration]
    if late:
        listed = ", ".join(f"{time:.2f}s" for time in late)
        raise MediaError(f"{listed} past the end of {video_path.name} ({duration:.2f}s)")
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for time in times:
        path = output_directory / frame_name(time)
        run_ffmpeg(
            [
                "-ss",
                f"{max(0.0, time):.3f}",
                "-i",
                str(video_path.resolve()),
                "-frames:v",
                "1",
                "-vf",
                f"scale={width}:-2",
                "-q:v",
                "3",
                str(path.resolve()),
            ],
            description=f"extracting the frame at {time:.2f}s",
            runner=runner,
        )
        if not path.is_file():
            raise MediaError(f"no frame was written at {time:.2f}s of {video_path.name}")
        paths.append(path)
    return paths
