import json
import subprocess
from pathlib import Path

import pytest
from support import make_video, needs_ffmpeg, needs_font

from youtune.analyze import (
    ANALYSIS_NAME,
    MAXIMUM_PEAKS,
    SILENCE_DECIBELS,
    Peak,
    analyze,
    extract_frames,
    format_clock,
    format_transcript,
    frame_interval,
    loudness_profile,
    parse_loudness,
    parse_scene_times,
    pick_peaks,
)
from youtune.errors import MediaError
from youtune.subtitles import Cue


@pytest.mark.parametrize(
    ("duration", "expected"), [(30.0, 2.0), (600.0, 5.0), (3600.0, 30.0), (36000.0, 30.0)]
)
def test_frame_interval_targets_about_120_frames(duration: float, expected: float) -> None:
    assert frame_interval(duration) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"), [(0.0, "00:00"), (65.9, "01:05"), (3725.0, "1:02:05"), (-3, "00:00")]
)
def test_format_clock(seconds: float, expected: str) -> None:
    assert format_clock(seconds) == expected


def test_transcript_lines_carry_tenths() -> None:
    text = format_transcript([Cue(443_400, 445_000, "SAR 나왔다!"), Cue(5_050, 6_000, "안녕")])

    assert text == "[07:23.4] SAR 나왔다!\n[00:05.0] 안녕\n"


def test_parse_scene_times_reads_showinfo_lines() -> None:
    log = (
        "[Parsed_showinfo_3 @ 0x1] n:   0 pts:  10010 pts_time:10.01   duration:1\n"
        "[Parsed_scale_0 @ 0x2] pts_time:99\n"
        "[Parsed_showinfo_3 @ 0x1] n:   1 pts:  33100 pts_time:33.1 duration:1\n"
    )

    assert parse_scene_times(log) == [10.01, 33.1]


def test_parse_loudness_pairs_times_with_levels() -> None:
    text = (
        "frame:0    pts:0       pts_time:0\nlavfi.astats.Overall.RMS_level=-30.25\n"
        "frame:1    pts:4000    pts_time:0.5\nlavfi.astats.Overall.RMS_level=-inf\n"
        "frame:2    pts:8000    pts_time:1\nlavfi.astats.Overall.RMS_level=nonsense\n"
    )

    assert parse_loudness(text) == [
        (0.0, -30.2),
        (0.5, SILENCE_DECIBELS),
        (1.0, SILENCE_DECIBELS),
    ]


class TestPeaks:
    def test_picks_loud_local_maxima_spaced_apart(self) -> None:
        windows = [(index * 0.5, -40.0) for index in range(60)]
        windows[10] = (5.0, -20.0)
        windows[11] = (5.5, -22.0)  # same shout, half a second later: not a second peak
        windows[40] = (20.0, -25.0)
        windows[30] = (15.0, -37.0)  # louder than the median, but by less than 6 dB

        peaks, median = pick_peaks(windows)

        assert median == -40.0
        assert peaks == [Peak(5.0, -20.0), Peak(20.0, -25.0)]

    def test_caps_the_count_and_keeps_time_order(self) -> None:
        windows = [(index * 0.5, -10.0 if index % 10 == 0 else -50.0) for index in range(400)]

        peaks, _ = pick_peaks(windows)

        assert len(peaks) == MAXIMUM_PEAKS
        assert [peak.time for peak in peaks] == sorted(peak.time for peak in peaks)

    def test_silence_has_no_peaks(self) -> None:
        assert pick_peaks([(0.0, SILENCE_DECIBELS)]) == ([], SILENCE_DECIBELS)

    def test_profile_keeps_the_loudest_window_per_ten_seconds(self) -> None:
        assert loudness_profile([(1.0, -30.0), (9.5, -20.0), (12.0, -25.0)]) == [
            (0.0, -20.0),
            (10.0, -25.0),
        ]


@needs_ffmpeg
@needs_font
def test_analyze_writes_sheets_transcript_and_index(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc='0.02*sin(2*PI*440*t)*(1+20*between(t,4,4.5))':s=48000",
            "-t",
            "8",
            "-vf",
            "drawbox=enable='gte(t,5)':color=black:t=fill",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ],
        check=True,
    )
    subtitles = tmp_path / "clip.ko.srt"
    subtitles.write_text("1\n00:00:01,000 --> 00:00:02,000\n자막\n", encoding="utf-8")

    result = analyze(video, tmp_path / "analysis", subtitle_path=subtitles)

    assert result.frame_interval == 2.0
    assert result.sheets[0].times == [0.0, 2.0, 4.0, 6.0]
    assert (tmp_path / "analysis" / result.sheets[0].path).is_file()
    assert any(4.5 <= scene <= 5.5 for scene in result.scenes)
    assert any(3.5 <= peak.time <= 5.0 for peak in result.peaks)
    assert (tmp_path / "analysis" / "transcript.txt").read_text(encoding="utf-8") == (
        "[00:01.0] 자막\n"
    )
    index = json.loads((tmp_path / "analysis" / ANALYSIS_NAME).read_text(encoding="utf-8"))
    assert index["duration"] == pytest.approx(8.0, abs=0.1)
    assert index["transcript"] == "transcript.txt"


@needs_ffmpeg
def test_extract_frames_refuses_times_past_the_end(tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4", size="320x240", seconds=2.0, audio=False)

    with pytest.raises(MediaError, match="past the end"):
        extract_frames(video, [0.5, 9.0], tmp_path / "frames")

    paths = extract_frames(video, [0.5, 1.25], tmp_path / "frames", width=160)
    assert [path.name for path in paths] == ["frame-00000.500.jpg", "frame-00001.250.jpg"]
