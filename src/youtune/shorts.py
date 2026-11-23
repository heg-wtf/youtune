"""Render 1080x1920 Shorts from a source video and a ``ShortSpecification``.

Each segment is cut, put on an exact frame grid, panned (a crop that follows the
subject), zoomed / shaken (``zoompan``) and frozen (``tpad``); the segments are
concatenated, the audio is loudness-normalised, and the text is composited on
top: title, labels, the Korean subtitles (one image track), pop text and white
flashes (PNG sequences). Every piece of text is drawn by Pillow because the
Homebrew ffmpeg has neither ``drawtext`` nor libass.

Frame timing matters here: a pop or flash that lands two frames late reads as a
mistake. Three things keep every layer on the same frames (found while making the
first Shorts by hand): ``trim=end_frame`` instead of a duration, one
``settb``/``setpts=N`` grid on both picture layers, and pop/flash offsets given
in whole frames.
"""

import itertools
import math
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw

from youtune.errors import RenderError
from youtune.fonts import FontFace, find_font, load_font
from youtune.image_track import TimedImage, concat_input_arguments, write_image_track
from youtune.media import (
    CommandRunner,
    MediaInformation,
    format_frame_rate,
    output_frame_rate,
    probe,
    run_ffmpeg,
    timebase_filter,
)
from youtune.shorts_spec import Keyframes, Layout, Segment, ShortSpecification, TimedText
from youtune.subtitles import Cue, write_srt
from youtune.typography import (
    LINE_HEIGHT_RATIO,
    WHITE,
    YELLOW,
    Color,
    TextLine,
    fit_text,
    paste_centered,
    render_lines,
    stroke_width_for,
    wrap_text,
)

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
SQUARE_SIZE = 1080
SQUARE_TOP = 400
TITLE_TOP = 110
TITLE_FONT_SIZE = 88
SUBTITLE_FONT_SIZE = 78
SUBTITLE_MAXIMUM_LINES = 2
POP_FONT_SIZE = 150
LABEL_FONT_SIZE = 56
LABEL_LEFT = 40
TEXT_MAXIMUM_WIDTH = 960
#: Where the subtitles end in the ``full`` layout: above the app's caption area.
FULL_SUBTITLE_BOTTOM = 1600
MINIMUM_CUE_SECONDS = 0.3
POP_SCALES = (1.9, 1.45, 1.12, 0.94, 1.0)
POP_FADE_OUT = (0.0, 0.35, 0.7)
POP_ROTATION_DEGREES = 4
POP_MARGIN = 20
FLASH_ALPHAS = (0.9, 0.62, 0.4, 0.22, 0.1, 0.0)
#: Shake amplitude as a share of the crop's side (26 px on a 1080 px crop).
SHAKE_AMPLITUDE_RATIO = 26 / 1080
#: A shake needs room to move inside the crop, so it zooms in at least this much.
SHAKE_MINIMUM_ZOOM = 1.06
BACKGROUND_FILTER = (
    "scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
    f"gblur=sigma=10,scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},eq=brightness=-0.12"
)
LOUDNESS_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"
AUDIO_SAMPLE_RATE = 48000
#: Extra source seconds read per segment so trimming never runs short of frames.
INPUT_MARGIN_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class Placement:
    """The geometry of one layout on the 1080x1920 canvas, plus the source crop."""

    layout: Layout
    crop_width: int  # source pixels
    crop_height: int
    foreground_width: int  # output pixels
    foreground_height: int
    foreground_top: int
    subtitle_top: int
    subtitle_box_height: int
    pop_center_y: int
    pop_minimum_top: int
    label_top: int


@dataclass(frozen=True, slots=True)
class TimedOverlay:
    start: float  # output seconds
    end: float
    text: str
    color: Color = WHITE


@dataclass(frozen=True, slots=True)
class Timeline:
    """A short's overlays moved onto the output clock."""

    cues: list[TimedOverlay]
    pops: list[TimedOverlay]
    labels: list[TimedOverlay]
    flashes: list[float]
    duration: float


@dataclass(frozen=True, slots=True)
class RenderedShort:
    name: str
    video_path: Path
    subtitle_path: Path | None
    poster_path: Path
    duration: float


# ---------------------------------------------------------------- geometry


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def subtitle_box_height() -> int:
    return (
        round(SUBTITLE_FONT_SIZE * LINE_HEIGHT_RATIO) * (SUBTITLE_MAXIMUM_LINES + 1)
        + stroke_width_for(SUBTITLE_FONT_SIZE) * 2
        + 4
    )


def choose_layout(requested: Layout | None, information: MediaInformation) -> Layout:
    if requested is not None:
        return requested
    return Layout.FULL if information.is_portrait else Layout.SQUARE


def placement_for(layout: Layout, information: MediaInformation) -> Placement:
    width, height = information.width, information.height
    box = subtitle_box_height()
    if layout is Layout.SQUARE:
        side = _even(min(width, height))
        return Placement(
            layout=layout,
            crop_width=side,
            crop_height=side,
            foreground_width=SQUARE_SIZE,
            foreground_height=SQUARE_SIZE,
            foreground_top=SQUARE_TOP,
            subtitle_top=SQUARE_TOP + SQUARE_SIZE + 20,
            subtitle_box_height=box,
            pop_center_y=SQUARE_TOP + 280,
            pop_minimum_top=SQUARE_TOP - 50,
            label_top=SQUARE_TOP + 36,
        )
    if width / height >= 9 / 16:
        crop_height = _even(height)
        crop_width = _even(crop_height * 9 / 16)
    else:
        crop_width = _even(width)
        crop_height = _even(min(height, crop_width * 16 / 9))
    return Placement(
        layout=layout,
        crop_width=crop_width,
        crop_height=crop_height,
        foreground_width=OUTPUT_WIDTH,
        foreground_height=OUTPUT_HEIGHT,
        foreground_top=0,
        subtitle_top=FULL_SUBTITLE_BOTTOM - box,
        subtitle_box_height=box,
        pop_center_y=820,
        pop_minimum_top=380,
        label_top=380,
    )


# ---------------------------------------------------------------- expressions


def piecewise_linear_expression(
    keyframes: Keyframes, origin: float, variable: str, default: float
) -> str:
    """An ffmpeg expression interpolating (source seconds, value) keyframes linearly.

    ``origin`` turns source seconds into the segment-local clock that ``variable``
    measures; values hold outside the keyframe range.
    """
    if not keyframes:
        return f"{default:.4f}"
    points = [(time - origin, value) for time, value in keyframes]
    expression = f"{points[-1][1]:.4f}"
    for (start_time, start_value), (end_time, end_value) in reversed(
        list(itertools.pairwise(points))
    ):
        slope = (end_value - start_value) / (end_time - start_time)
        expression = (
            f"if(lt({variable},{end_time:.3f}),"
            f"{start_value:.4f}+({slope:.5f})*({variable}-({start_time:.3f})),{expression})"
        )
    return f"if(lt({variable},{points[0][0]:.3f}),{points[0][1]:.4f},{expression})"


def _between_terms(segment: Segment, variable: str) -> str:
    return "+".join(
        f"between({variable},{start - segment.start:.3f},{end - segment.start:.3f})"
        for start, end in segment.shakes
    )


def shake_expression(segment: Segment, variable: str, frequency: int, amplitude: float) -> str:
    terms = [
        f"{amplitude:.2f}*sin({variable}*{frequency})*between({variable},"
        f"{start - segment.start:.3f},{end - segment.start:.3f})"
        for start, end in segment.shakes
    ]
    return "+".join(terms) if terms else "0"


def zoom_expression(segment: Segment) -> str:
    zoom = piecewise_linear_expression(segment.zoom, segment.start, "it", 1.0)
    if not segment.shakes:
        return zoom
    return f"max({zoom},if(gt({_between_terms(segment, 'it')},0),{SHAKE_MINIMUM_ZOOM},1))"


def crop_filter(
    segment: Segment, information: MediaInformation, *, width: int, height: int
) -> str:
    """Crop ``width`` x ``height`` (centred vertically), panning horizontally with
    the segment's center keyframes."""
    if width >= information.width:
        x = "0"
    else:
        center = piecewise_linear_expression(segment.center, segment.start, "t", 0.5)
        x = f"clip(({center})*{information.width}-{width / 2},0,{information.width - width})"
    y = max(0, (information.height - height) // 2)
    return f"crop={width}:{height}:x='{x}':y={y}"


def motion_filter(segment: Segment, placement: Placement, frame_rate: Fraction) -> str:
    """Scale the crop to the foreground size, through ``zoompan`` when it moves."""
    size = f"{placement.foreground_width}x{placement.foreground_height}"
    if not segment.zoom and not segment.shakes:
        return f"scale={placement.foreground_width}:{placement.foreground_height}"
    amplitude = SHAKE_AMPLITUDE_RATIO * min(placement.crop_width, placement.crop_height)
    focus_x, focus_y = segment.focus
    shake_x = shake_expression(segment, "it", 53, amplitude)
    shake_y = shake_expression(segment, "it", 41, amplitude)
    x = f"clip({focus_x}*iw-iw/zoom/2+({shake_x}),0,iw-iw/zoom)"
    y = f"clip({focus_y}*ih-ih/zoom/2+({shake_y}),0,ih-ih/zoom)"
    return (
        f"zoompan=z='{zoom_expression(segment)}':x='{x}':y='{y}':d=1:"
        f"s={size}:fps={format_frame_rate(frame_rate)}"
    )


# ---------------------------------------------------------------- timeline


def segment_frames(segment: Segment, frame_rate: Fraction) -> tuple[int, int]:
    """(video frames, freeze frames) of a segment on the output grid."""
    rate = float(frame_rate)
    return (
        max(1, math.ceil(segment.video_duration * rate - 1e-6)),
        round(segment.freeze * rate),
    )


def segment_duration(segment: Segment, frame_rate: Fraction) -> float:
    video_frames, freeze_frames = segment_frames(segment, frame_rate)
    return (video_frames + freeze_frames) / float(frame_rate)


def build_timeline(
    specification: ShortSpecification, cues: Sequence[Cue], frame_rate: Fraction
) -> Timeline:
    """Move cues, pops, labels and flashes from source seconds to output seconds.

    The audio keeps playing through a freeze, so source time keeps running there
    too; anything past the segment's end is clamped to it.
    """
    timed_cues: list[TimedOverlay] = []
    pops: list[TimedOverlay] = []
    labels: list[TimedOverlay] = []
    flashes: list[float] = []
    offset = 0.0
    for segment in specification.segments:
        length = segment_duration(segment, frame_rate)
        to_output = _output_clock(offset, length, segment.start)

        for cue in cues:
            start, end = cue.start_milliseconds / 1000, cue.end_milliseconds / 1000
            if end <= segment.start or start >= segment.start + length:
                continue
            mapped_start, mapped_end = to_output(start), to_output(end)
            if mapped_end - mapped_start >= MINIMUM_CUE_SECONDS:
                highlighted = any(word in cue.text for word in specification.highlight)
                timed_cues.append(
                    TimedOverlay(
                        mapped_start, mapped_end, cue.text, YELLOW if highlighted else WHITE
                    )
                )
        pops += [_moved(pop, to_output) for pop in segment.pops]
        labels += [_moved(label, to_output) for label in segment.labels]
        flashes += [to_output(flash) for flash in segment.flashes]
        offset += length
    return Timeline(timed_cues, pops, labels, flashes, round(offset, 3))


def _output_clock(offset: float, length: float, start: float) -> Callable[[float], float]:
    """Source seconds → output seconds for the segment starting at ``start``."""

    def to_output(source_time: float) -> float:
        return round(offset + min(max(source_time - start, 0.0), length), 3)

    return to_output


def _moved(text: TimedText, to_output: Callable[[float], float]) -> TimedOverlay:
    return TimedOverlay(to_output(text.start), to_output(text.end), text.text, text.color)


def default_poster_time(specification: ShortSpecification, timeline: Timeline) -> float:
    if specification.poster_at is not None:
        return specification.poster_at
    if timeline.pops:
        return min(timeline.pops[0].start + 0.25, timeline.duration - 0.1)
    return min(1.0, timeline.duration / 2)


# ---------------------------------------------------------------- text images


def render_title(specification: ShortSpecification, face: FontFace, path: Path) -> int:
    """The title lines as one full-width image; returns its height."""
    lines = [
        TextLine(line, color)
        for text, color in specification.title
        for line in wrap_text(text, load_font(face, TITLE_FONT_SIZE), TEXT_MAXIMUM_WIDTH)
    ]
    layer = render_lines(lines, face, TITLE_FONT_SIZE)
    paste_centered(layer, (OUTPUT_WIDTH, layer.height)).save(path)
    return layer.height


def render_label(text: str, face: FontFace, path: Path) -> None:
    font = load_font(face, LABEL_FONT_SIZE)
    padding_x, padding_y = 28, 16
    width = int(font.getlength(text)) + padding_x * 2
    height = LABEL_FONT_SIZE + padding_y * 2
    label = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(label)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=18, fill=(0, 0, 0, 150))
    draw.text((padding_x, padding_y), text, font=font, fill=WHITE)
    label.save(path)


def render_cue_images(
    cues: Sequence[TimedOverlay], face: FontFace, directory: Path, box_height: int
) -> list[TimedImage]:
    images = []
    for number, cue in enumerate(cues, start=1):
        lines, size = fit_text(
            cue.text,
            cue.color,
            face,
            font_size=SUBTITLE_FONT_SIZE,
            maximum_width=TEXT_MAXIMUM_WIDTH,
            maximum_lines=SUBTITLE_MAXIMUM_LINES,
        )
        name = f"cue{number:04d}.png"
        layer = render_lines(lines, face, size)
        # Top-aligned: one- and two-line cues start on the same line under the video.
        canvas = Image.new("RGBA", (OUTPUT_WIDTH, box_height), (0, 0, 0, 0))
        canvas.paste(layer, ((OUTPUT_WIDTH - layer.width) // 2, 0), layer)
        canvas.save(directory / name)
        images.append(TimedImage(cue.start, cue.end, name))
    return images


def frame_count(duration: float, frame_rate: Fraction) -> int:
    return max(1, round(duration * float(frame_rate)))


def render_pop_layer(lines: Sequence[TextLine], face: FontFace, scale: float) -> Image.Image:
    """Pop text drawn at the scaled size (never a bitmap upscale), then tilted and
    held inside the frame's width."""
    layer = render_lines(lines, face, round(POP_FONT_SIZE * scale))
    layer = layer.rotate(POP_ROTATION_DEGREES, resample=Image.Resampling.BICUBIC, expand=True)
    maximum_width = OUTPUT_WIDTH - 2 * POP_MARGIN
    if layer.width > maximum_width:
        ratio = maximum_width / layer.width
        layer = layer.resize(
            (maximum_width, round(layer.height * ratio)), Image.Resampling.LANCZOS
        )
    return layer


def render_pop_frames(
    pop: TimedOverlay,
    face: FontFace,
    directory: Path,
    placement: Placement,
    frame_rate: Fraction,
) -> int:
    """A punch-in that settles, holds, then fades; returns the canvas's top y.

    Every frame shares one canvas tall enough for the biggest step, so the punch
    never clips, and the canvas is centred on the layout's pop line without
    climbing above ``pop_minimum_top`` (the title's space)."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        TextLine(line, pop.color)
        for line in wrap_text(pop.text, load_font(face, POP_FONT_SIZE), TEXT_MAXIMUM_WIDTH)
    ]
    total = frame_count(pop.end - pop.start, frame_rate)
    scales = [POP_SCALES[index] if index < len(POP_SCALES) else 1.0 for index in range(total)]
    layers = {scale: render_pop_layer(lines, face, scale) for scale in set(scales)}
    canvas_height = max(layer.height for layer in layers.values()) + 2 * POP_MARGIN
    canvas_top = max(placement.pop_minimum_top, placement.pop_center_y - canvas_height // 2)
    for index, scale in enumerate(scales):
        layer = layers[scale]
        frames_left = total - 1 - index
        alpha = POP_FADE_OUT[frames_left] if frames_left < len(POP_FADE_OUT) else 1.0
        if alpha < 1.0:
            layer = layer.copy()
            layer.putalpha(
                layer.getchannel("A").point(lambda value, factor=alpha: round(value * factor))
            )
        frame = Image.new("RGBA", (OUTPUT_WIDTH, canvas_height), (0, 0, 0, 0))
        frame.paste(
            layer,
            ((OUTPUT_WIDTH - layer.width) // 2, (canvas_height - layer.height) // 2),
            layer,
        )
        frame.save(directory / f"{index:04d}.png")
    return canvas_top


def render_flash_frames(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index, alpha in enumerate(FLASH_ALPHAS):
        Image.new("RGBA", (OUTPUT_WIDTH, OUTPUT_HEIGHT), (255, 255, 255, round(alpha * 255))).save(
            directory / f"{index:04d}.png"
        )


# ---------------------------------------------------------------- ffmpeg graph


def _foreground_crop(segment: Segment, placement: Placement, information: MediaInformation) -> str:
    return crop_filter(
        segment, information, width=placement.crop_width, height=placement.crop_height
    )


def segment_filters(
    index: int,
    segment: Segment,
    placement: Placement,
    information: MediaInformation,
    frame_rate: Fraction,
    audio_input: str,
) -> list[str]:
    video_frames, freeze_frames = segment_frames(segment, frame_rate)
    duration = (video_frames + freeze_frames) / float(frame_rate)
    samples = round(duration * AUDIO_SAMPLE_RATE)
    freeze = f",tpad=stop_mode=clone:stop={freeze_frames}" if freeze_frames else ""
    grid = timebase_filter(frame_rate)
    source = (
        f"[{index}:v:0]fps={format_frame_rate(frame_rate)},trim=end_frame={video_frames},"
        "setpts=PTS-STARTPTS"
    )
    foreground = (
        f"{_foreground_crop(segment, placement, information)}{freeze},"
        f"{motion_filter(segment, placement, frame_rate)},{grid}"
    )
    audio = (
        f"[{audio_input}]asetpts=PTS-STARTPTS,aresample={AUDIO_SAMPLE_RATE},"
        f"atrim=end_sample={samples},apad=whole_len={samples},"
        f"afade=t=in:d=0.03,afade=t=out:st={max(0.0, duration - 0.06):.3f}:d=0.06[audio{index}]"
    )
    if placement.layout is Layout.FULL:
        return [
            f"{source},{foreground},setsar=1,format=yuv420p[video{index}]",
            audio,
        ]
    background_crop = crop_filter(
        segment,
        information,
        width=min(information.width, _even(information.height * 9 / 16)),
        height=information.height,
    )
    return [
        f"{source},split=2[background_source{index}][foreground_source{index}]",
        f"[background_source{index}]{background_crop},{BACKGROUND_FILTER}{freeze},{grid}"
        f"[background{index}]",
        f"[foreground_source{index}]{foreground}[foreground{index}]",
        f"[background{index}][foreground{index}]overlay=0:{placement.foreground_top},"
        f"setsar=1,format=yuv420p[video{index}]",
        audio,
    ]


class _Graph:
    """Collects inputs and the overlay chain of one render."""

    def __init__(self, segment_count: int, frame_rate: Fraction) -> None:
        self.arguments: list[str] = []
        self.filters: list[str] = []
        self.next_input = segment_count
        self.layer = 0
        self.frame_rate = frame_rate

    def add_input(self, arguments: Sequence[str]) -> int:
        self.arguments += arguments
        index = self.next_input
        self.next_input += 1
        return index

    def overlay_static(
        self, image: Path, x: int, y: int, window: tuple[float, float] | None = None
    ) -> None:
        index = self.add_input(["-i", str(image)])
        enable = f":enable='between(t,{window[0]:.3f},{window[1]:.3f})'" if window else ""
        self.filters.append(
            f"[layer{self.layer}][{index}:v]overlay={x}:{y}{enable}[layer{self.layer + 1}]"
        )
        self.layer += 1

    def overlay_track(self, list_path: Path, x: int, y: int) -> None:
        index = self.add_input(concat_input_arguments(list_path))
        self.filters.append(f"[{index}:v]format=rgba[track{index}]")
        self.filters.append(
            f"[layer{self.layer}][track{index}]overlay={x}:{y}:eof_action=pass"
            f"[layer{self.layer + 1}]"
        )
        self.layer += 1

    def overlay_sequence(self, directory: Path, x: int, y: int, start: float) -> None:
        index = self.add_input(
            ["-framerate", format_frame_rate(self.frame_rate), "-i", str(directory / "%04d.png")]
        )
        start_frames = round(start * float(self.frame_rate))
        self.filters.append(
            f"[{index}:v]format=rgba,tpad=start={start_frames}:start_mode=add:"
            f"color=0x00000000,{timebase_filter(self.frame_rate)}[sequence{index}]"
        )
        self.filters.append(
            f"[layer{self.layer}][sequence{index}]overlay={x}:{y}:eof_action=pass"
            f"[layer{self.layer + 1}]"
        )
        self.layer += 1


def render_short(
    specification: ShortSpecification,
    video_path: Path,
    cues: Sequence[Cue],
    output_directory: Path,
    *,
    information: MediaInformation | None = None,
    face: FontFace | None = None,
    runner: CommandRunner = subprocess.run,
) -> RenderedShort:
    """Render one short to ``<output>/<name>.mp4`` with its ``.srt`` and ``.jpg`` poster.

    Raises:
        RenderError: ffmpeg failed.
        MediaError: ffmpeg / ffprobe missing or the video unreadable.
    """
    information = information or probe(video_path, runner=runner)
    face = face or find_font()
    frame_rate = output_frame_rate(information.frame_rate)
    layout = choose_layout(specification.layout, information)
    placement = placement_for(layout, information)
    timeline = build_timeline(specification, cues, frame_rate)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"{specification.name}.mp4"
    partial = output_directory / f"{specification.name}.partial.mp4"

    with tempfile.TemporaryDirectory(prefix=f"youtune-{specification.name}-") as temporary:
        work = Path(temporary)
        graph = _Graph(len(specification.segments), frame_rate)
        inputs: list[str] = []
        audio_inputs: list[str] = []
        for segment in specification.segments:
            read_seconds = segment.duration + INPUT_MARGIN_SECONDS
            inputs += ["-ss", f"{segment.start:.3f}", "-t", f"{read_seconds:.3f}"]
            inputs += ["-i", str(video_path.resolve())]
        for index, segment in enumerate(specification.segments):
            if information.has_audio:
                audio_inputs.append(f"{index}:a:0")
            else:
                silent = graph.add_input(
                    [
                        "-f",
                        "lavfi",
                        "-t",
                        f"{segment.duration + INPUT_MARGIN_SECONDS:.3f}",
                        "-i",
                        f"anullsrc=r={AUDIO_SAMPLE_RATE}:cl=stereo",
                    ]
                )
                audio_inputs.append(f"{silent}:a:0")
        for index, segment in enumerate(specification.segments):
            graph.filters += segment_filters(
                index, segment, placement, information, frame_rate, audio_inputs[index]
            )
        count = len(specification.segments)
        pairs = "".join(f"[video{index}][audio{index}]" for index in range(count))
        graph.filters.append(f"{pairs}concat=n={count}:v=1:a=1[layer0][audio_concat]")
        graph.filters.append(
            f"[audio_concat]{LOUDNESS_FILTER},aresample={AUDIO_SAMPLE_RATE}[audio_out]"
        )

        if specification.title:
            title_path = work / "title.png"
            render_title(specification, face, title_path)
            graph.overlay_static(title_path, 0, TITLE_TOP)
        for index, label in enumerate(timeline.labels):
            label_path = work / f"label{index:03d}.png"
            render_label(label.text, face, label_path)
            graph.overlay_static(
                label_path, LABEL_LEFT, placement.label_top, (label.start, label.end)
            )
        if timeline.cues:
            track_directory = work / "cues"
            track_directory.mkdir()
            images = render_cue_images(
                timeline.cues, face, track_directory, placement.subtitle_box_height
            )
            track = write_image_track(
                images,
                track_directory,
                size=(OUTPUT_WIDTH, placement.subtitle_box_height),
                total_duration=timeline.duration,
            )
            graph.overlay_track(track, 0, placement.subtitle_top)
        for index, pop in enumerate(timeline.pops):
            pop_directory = work / f"pop{index:03d}"
            top = render_pop_frames(pop, face, pop_directory, placement, frame_rate)
            graph.overlay_sequence(pop_directory, 0, top, pop.start)
        if timeline.flashes:
            flash_directory = work / "flash"
            render_flash_frames(flash_directory)
            for flash in timeline.flashes:
                graph.overlay_sequence(flash_directory, 0, 0, flash)

        filter_path = work / "filter_graph.txt"
        filter_path.write_text(";\n".join(graph.filters), encoding="utf-8")
        run_ffmpeg(
            [
                *inputs,
                *graph.arguments,
                # ffmpeg 7+ reads an option's value from a file with the "-/" prefix.
                "-/filter_complex",
                str(filter_path),
                "-map",
                f"[layer{graph.layer}]",
                "-map",
                "[audio_out]",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-r",
                format_frame_rate(frame_rate),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(partial.resolve()),
            ],
            description=f"rendering short {specification.name}",
            runner=runner,
        )
    partial.replace(output_path)

    subtitle_path = None
    if timeline.cues:
        subtitle_path = write_srt(
            [
                Cue(round(cue.start * 1000), round(cue.end * 1000), cue.text)
                for cue in timeline.cues
            ],
            output_directory / f"{specification.name}.ko.srt",
        )
    poster_path = output_directory / f"{specification.name}.jpg"
    run_ffmpeg(
        [
            "-ss",
            f"{default_poster_time(specification, timeline):.3f}",
            "-i",
            str(output_path.resolve()),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(poster_path.resolve()),
        ],
        description=f"extracting the poster of {specification.name}",
        runner=runner,
    )
    if not poster_path.is_file():
        raise RenderError(f"no poster frame was written for {specification.name}")
    return RenderedShort(
        specification.name, output_path, subtitle_path, poster_path, timeline.duration
    )
