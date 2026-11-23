import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from support import evaluate, make_video, needs_ffmpeg, needs_font

from youtune.errors import SpecificationError
from youtune.fonts import FontFace
from youtune.media import MediaInformation, probe
from youtune.shorts import (
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    SHAKE_MINIMUM_ZOOM,
    TimedOverlay,
    build_timeline,
    choose_layout,
    crop_filter,
    default_poster_time,
    motion_filter,
    piecewise_linear_expression,
    placement_for,
    render_pop_frames,
    render_short,
    segment_frames,
    zoom_expression,
)
from youtune.shorts_spec import (
    Layout,
    Segment,
    TimedText,
    load_specifications,
    parse_specifications,
)
from youtune.subtitles import Cue
from youtune.typography import WHITE, YELLOW

RATE = Fraction(30)
LANDSCAPE = MediaInformation(1920, 1080, Fraction(30), 120.0, "aac")
PORTRAIT = MediaInformation(1080, 1920, Fraction(60), 60.0, None)

VALID: dict[str, Any] = {
    "shorts": [
        {
            "name": "first-short",
            "title": [{"text": "제목", "color": "white"}, {"text": "둘째 줄", "color": "yellow"}],
            "segments": [
                {
                    "start": 10.0,
                    "end": 16.0,
                    "center": [[10.0, 0.4], [16.0, 0.6]],
                    "zoom": [[11.0, 1.0], [11.3, 1.4]],
                    "shakes": [[11.0, 11.5]],
                    "flashes": [11.0],
                    "freeze": 1.0,
                    "pops": [{"start": 11.0, "end": 12.0, "text": "왔다!!"}],
                    "labels": [{"start": 10.0, "end": 11.0, "text": "10초 전"}],
                },
                {"start": 30.0, "end": 34.0},
            ],
        }
    ]
}


def spec_with(**changes: Any) -> dict[str, Any]:
    document = copy.deepcopy(VALID)
    document["shorts"][0].update(changes)
    return document


def segment_with(**changes: Any) -> dict[str, Any]:
    document = copy.deepcopy(VALID)
    document["shorts"][0]["segments"][0].update(changes)
    return document


class TestSpecification:
    def test_parses_a_valid_spec(self) -> None:
        (short,) = parse_specifications(VALID, video_duration=120.0)

        assert short.name == "first-short"
        assert short.title == (("제목", WHITE), ("둘째 줄", YELLOW))
        first = short.segments[0]
        assert first.center == ((10.0, 0.4), (16.0, 0.6))
        assert first.pops == (TimedText(11.0, 12.0, "왔다!!", YELLOW),)
        assert first.labels[0].color == WHITE
        assert short.duration == pytest.approx(11.0)
        assert short.layout is None

    def test_reads_a_file(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(VALID), encoding="utf-8")

        assert load_specifications(path, video_duration=120.0)[0].name == "first-short"

    def test_unreadable_file(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.json"
        path.write_text("{nope", encoding="utf-8")

        with pytest.raises(SpecificationError, match="cannot read"):
            load_specifications(path, video_duration=120.0)

    @pytest.mark.parametrize(
        ("document", "fragment"),
        [
            (spec_with(name="Bad Name"), "shorts[0].name"),
            (spec_with(layout="diagonal"), "shorts[0].layout"),
            (spec_with(segments=[]), "shorts[0].segments: expected a non-empty list"),
            (spec_with(segments=[{"start": 1.0, "end": 2.0}]), "outside 5-180s"),
            (spec_with(poster_at=99.0), "shorts[0].poster_at"),
            (spec_with(title=[{"text": ""}]), "shorts[0].title[0].text"),
            (segment_with(end=500.0), "needs 0 ≤ start < end"),
            (segment_with(center=[[12.0, 0.5], [11.0, 0.6]]), "times must increase"),
            (segment_with(center=[[12.0, 1.5]]), "value within"),
            (segment_with(zoom=[[11.0, 0.5]]), "value within"),
            (segment_with(focus=[0.5]), "segments[0].focus"),
            (segment_with(freeze=9), "segments[0].freeze"),
            (segment_with(flashes=[40.0]), "must lie inside the segment"),
            (segment_with(shakes=[[12.0, 11.0]]), "start < end"),
            (
                segment_with(pops=[{"start": 11.0, "end": 12.0, "text": "x", "color": "mauve"}]),
                "unknown color",
            ),
            (
                segment_with(pops=[{"start": 11.0, "end": 12.0, "text": "가" * 41}]),
                "at most 40 characters",
            ),
        ],
    )
    def test_reports_each_problem_with_its_path(
        self, document: dict[str, Any], fragment: str
    ) -> None:
        with pytest.raises(SpecificationError) as error:
            parse_specifications(document, video_duration=120.0)

        assert fragment in str(error.value)

    def test_reports_every_problem_at_once(self) -> None:
        document = copy.deepcopy(VALID)
        document["shorts"].append(copy.deepcopy(VALID["shorts"][0]))
        document["shorts"][1]["segments"][1]["start"] = "soon"

        with pytest.raises(SpecificationError) as error:
            parse_specifications(document, video_duration=120.0)

        message = str(error.value)
        assert "duplicate name" in message
        assert "start and end must be numbers" in message

    def test_freeze_extends_the_overlay_window(self) -> None:
        document = segment_with(pops=[{"start": 16.5, "end": 17.0, "text": "정지!"}])

        (short,) = parse_specifications(document, video_duration=120.0)

        assert short.segments[0].pops[0].start == 16.5


def keyframe_value(keyframes: tuple[tuple[float, float], ...], local: float) -> float:
    return evaluate(piecewise_linear_expression(keyframes, 10.0, "t", 0.5), t=local)


class TestExpressions:
    keyframes = ((10.0, 1.0), (12.0, 2.0), (14.0, 2.0))

    @pytest.mark.parametrize(
        ("local", "expected"),
        [(-1.0, 1.0), (0.0, 1.0), (1.0, 1.5), (2.0, 2.0), (3.0, 2.0), (9.0, 2.0)],
    )
    def test_interpolates_relative_to_the_segment(self, local: float, expected: float) -> None:
        assert keyframe_value(self.keyframes, local) == pytest.approx(expected, abs=1e-3)

    def test_no_keyframes_is_the_default(self) -> None:
        assert piecewise_linear_expression((), 0.0, "t", 0.5) == "0.5000"

    def test_shake_forces_a_minimum_zoom_inside_its_window(self) -> None:
        segment = Segment(10.0, 16.0, shakes=((11.0, 11.5),))
        expression = zoom_expression(segment)

        assert evaluate(expression, it=1.2) == pytest.approx(SHAKE_MINIMUM_ZOOM)
        assert evaluate(expression, it=3.0) == pytest.approx(1.0)

    def test_crop_pans_with_the_center_and_stays_inside(self) -> None:
        segment = Segment(10.0, 16.0, center=((10.0, 0.0), (16.0, 1.0)))
        crop = crop_filter(segment, LANDSCAPE, width=1080, height=1080)
        x_expression = crop.split("x='")[1].split("'")[0]

        assert evaluate(x_expression, t=0.0) == 0.0
        # Slopes are printed to five decimals: within half a pixel.
        assert evaluate(x_expression, t=3.0) == pytest.approx(1920 / 2 - 540, abs=0.5)
        assert evaluate(x_expression, t=6.0) == 1920 - 1080

    def test_motion_is_a_plain_scale_without_zoom_or_shake(self) -> None:
        placement = placement_for(Layout.SQUARE, LANDSCAPE)

        assert motion_filter(Segment(0.0, 5.0), placement, RATE) == "scale=1080:1080"
        assert motion_filter(Segment(0.0, 5.0, zoom=((1.0, 1.2),)), placement, RATE).startswith(
            "zoompan="
        )


class TestLayout:
    def test_landscape_defaults_to_square(self) -> None:
        assert choose_layout(None, LANDSCAPE) is Layout.SQUARE
        assert choose_layout(None, PORTRAIT) is Layout.FULL
        assert choose_layout(Layout.FULL, LANDSCAPE) is Layout.FULL

    def test_square_crops_the_short_side(self) -> None:
        placement = placement_for(Layout.SQUARE, LANDSCAPE)

        assert (placement.crop_width, placement.crop_height) == (1080, 1080)
        assert placement.subtitle_top > placement.foreground_top + placement.foreground_height

    def test_full_crops_nine_by_sixteen(self) -> None:
        landscape = placement_for(Layout.FULL, LANDSCAPE)
        portrait = placement_for(Layout.FULL, PORTRAIT)
        narrow = placement_for(Layout.FULL, MediaInformation(600, 1920, RATE, 5.0, None))

        assert (landscape.crop_width, landscape.crop_height) == (606, 1080)
        assert (portrait.crop_width, portrait.crop_height) == (1080, 1920)
        assert (narrow.crop_width, narrow.crop_height) == (600, 1066)
        assert portrait.subtitle_top + portrait.subtitle_box_height == 1600


class TestTimeline:
    def test_frames_are_quantised_up(self) -> None:
        assert segment_frames(Segment(0.0, 1.01, freeze=0.5), RATE) == (31, 15)

    def test_moves_everything_onto_the_output_clock(self) -> None:
        (short,) = parse_specifications(VALID, video_duration=120.0)
        cues = [
            Cue(9_000, 11_000, "앞이 잘리는 자막"),
            Cue(15_500, 16_800, "정지 구간까지"),
            Cue(17_000, 17_100, "너무 짧음"),
            Cue(31_000, 33_000, "두 번째 구간"),
        ]

        timeline = build_timeline(short, cues, RATE)

        assert [(cue.start, cue.end, cue.text) for cue in timeline.cues] == [
            (0.0, 1.0, "앞이 잘리는 자막"),
            (5.5, 6.8, "정지 구간까지"),
            (8.0, 10.0, "두 번째 구간"),
        ]
        assert timeline.pops == [TimedOverlay(1.0, 2.0, "왔다!!", YELLOW)]
        assert timeline.labels == [TimedOverlay(0.0, 1.0, "10초 전", WHITE)]
        assert timeline.flashes == [1.0]
        assert timeline.duration == pytest.approx(11.0)

    def test_highlight_words_turn_a_cue_yellow(self) -> None:
        document = spec_with(highlight=["대박"])
        (short,) = parse_specifications(document, video_duration=120.0)

        timeline = build_timeline(short, [Cue(10_000, 12_000, "이건 대박")], RATE)

        assert timeline.cues[0].color == YELLOW

    def test_poster_defaults_to_just_after_the_first_pop(self) -> None:
        (short,) = parse_specifications(VALID, video_duration=120.0)
        timeline = build_timeline(short, [], RATE)

        assert default_poster_time(short, timeline) == pytest.approx(1.25)


@needs_font
def test_pop_frames_never_clip_and_stay_below_the_title(tmp_path: Path, font: FontFace) -> None:
    placement = placement_for(Layout.SQUARE, LANDSCAPE)
    pop = TimedOverlay(0.0, 1.0, "아주 긴 팝 문구도 잘리지 않아야 해요!!", YELLOW)

    top = render_pop_frames(pop, font, tmp_path, placement, RATE)

    frames = sorted(tmp_path.glob("*.png"))
    assert len(frames) == 30
    assert top >= placement.pop_minimum_top
    for frame_path in frames[:5]:
        with Image.open(frame_path) as frame:
            box = frame.getbbox()
            assert box is not None
            assert box[0] > 0 and box[1] > 0
            assert box[2] < frame.width and box[3] < frame.height
    with Image.open(frames[-1]) as last:
        assert last.getchannel("A").getextrema()[1] == 0


@needs_ffmpeg
@needs_font
def test_renders_a_short_end_to_end(tmp_path: Path, font: FontFace) -> None:
    video = make_video(tmp_path / "source.mp4", size="1280x720", seconds=6.0)
    document = {
        "shorts": [
            {
                "name": "clip",
                "title": [{"text": "테스트"}],
                "segments": [
                    {
                        "start": 0.5,
                        "end": 3.0,
                        "zoom": [[1.0, 1.0], [1.3, 1.3]],
                        "flashes": [1.0],
                        "freeze": 0.5,
                        "pops": [{"start": 1.0, "end": 2.0, "text": "짠!"}],
                    },
                    {"start": 4.0, "end": 6.0},
                ],
            }
        ]
    }
    (short,) = parse_specifications(document, video_duration=probe(video).duration)

    rendered = render_short(
        short, video, [Cue(1_000, 2_500, "자막입니다")], tmp_path / "out", face=font
    )

    output = probe(rendered.video_path)
    assert (output.width, output.height) == (OUTPUT_WIDTH, OUTPUT_HEIGHT)
    assert output.frame_rate == RATE
    assert output.duration == pytest.approx(5.0, abs=0.1)
    assert rendered.duration == pytest.approx(5.0, abs=0.05)
    assert rendered.subtitle_path is not None
    assert "자막입니다" in rendered.subtitle_path.read_text(encoding="utf-8")
    assert rendered.poster_path.stat().st_size > 0
    assert not (tmp_path / "out" / "clip.partial.mp4").exists()
