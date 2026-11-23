"""Subcommands that work on a downloaded video: analyze, frames, shorts, burn, report.

``youtune <url>`` (in ``cli.py``) stays the download-and-translate command; these
take files it produced, so an editor can measure the video, cut Shorts, burn the
subtitles and write the report step by step.
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from youtune.analyze import analyze, extract_frames
from youtune.burn import burn_subtitles
from youtune.errors import SpecificationError
from youtune.media import probe
from youtune.report import load_manifest, write_report
from youtune.shorts import render_short
from youtune.shorts_spec import load_specifications
from youtune.subtitles import read_srt

SUBCOMMANDS = ("analyze", "frames", "shorts", "burn", "report")


def _existing_file(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"no such file: {value}")
    return path


def _seconds(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"seconds must be ≥ 0, got {value}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtune",
        description="Work on a downloaded video: measure it, cut Shorts, burn subtitles, report.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logs")
    commands = parser.add_subparsers(dest="command", required=True)

    analyze_parser = commands.add_parser(
        "analyze", help="contact sheets, scene cuts, loudness peaks and a transcript"
    )
    analyze_parser.add_argument("video", type=_existing_file)
    analyze_parser.add_argument("--subtitles", type=_existing_file, help="Korean .srt")
    analyze_parser.add_argument("-o", "--output-dir", type=Path, required=True)
    analyze_parser.add_argument("--interval", type=_seconds, help="seconds between thumbnails")

    frames_parser = commands.add_parser("frames", help="exact frames at given seconds")
    frames_parser.add_argument("video", type=_existing_file)
    frames_parser.add_argument("--at", type=_seconds, nargs="+", required=True)
    frames_parser.add_argument("-o", "--output-dir", type=Path, required=True)
    frames_parser.add_argument("--width", type=int, default=720)

    shorts_parser = commands.add_parser("shorts", help="render 1080x1920 Shorts from a spec")
    shorts_parser.add_argument("video", type=_existing_file)
    shorts_parser.add_argument("--spec", type=_existing_file, required=True)
    shorts_parser.add_argument("--subtitles", type=_existing_file, help="Korean .srt")
    shorts_parser.add_argument("-o", "--output-dir", type=Path, required=True)
    shorts_parser.add_argument("--only", nargs="+", help="render only these short names")

    burn_parser = commands.add_parser("burn", help="burn an .srt into the video")
    burn_parser.add_argument("video", type=_existing_file)
    burn_parser.add_argument("subtitles", type=_existing_file)
    burn_parser.add_argument("-o", "--output", type=Path, help="default: <video>.burned.mp4")
    burn_parser.add_argument(
        "--software", action="store_true", help="encode with libx264, not VideoToolbox"
    )

    report_parser = commands.add_parser("report", help="HTML report from a manifest")
    report_parser.add_argument("manifest", type=_existing_file)
    report_parser.add_argument("-o", "--output", type=Path, help="default: report.html beside it")
    return parser


def _print_json(payload: object) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _analyze(arguments: argparse.Namespace) -> None:
    result = analyze(
        arguments.video,
        arguments.output_dir,
        subtitle_path=arguments.subtitles,
        interval=arguments.interval,
    )
    _print_json(
        {
            "analysis": str(arguments.output_dir / "analysis.json"),
            "sheets": [str(arguments.output_dir / sheet.path) for sheet in result.sheets],
            "scenes": len(result.scenes),
            "peaks": [[peak.time, peak.decibels] for peak in result.peaks],
            "duration": result.duration,
        }
    )


def _frames(arguments: argparse.Namespace) -> None:
    paths = extract_frames(
        arguments.video, arguments.at, arguments.output_dir, width=arguments.width
    )
    _print_json([str(path) for path in paths])


def _shorts(arguments: argparse.Namespace) -> None:
    information = probe(arguments.video)
    specifications = load_specifications(arguments.spec, video_duration=information.duration)
    if arguments.only:
        unknown = set(arguments.only) - {specification.name for specification in specifications}
        if unknown:
            raise SpecificationError(f"no short named {', '.join(sorted(unknown))} in the spec")
        specifications = [
            specification
            for specification in specifications
            if specification.name in arguments.only
        ]
    cues = read_srt(arguments.subtitles) if arguments.subtitles else []
    rendered = []
    for specification in specifications:
        short = render_short(
            specification, arguments.video, cues, arguments.output_dir, information=information
        )
        rendered.append(
            {
                "name": short.name,
                "video": str(short.video_path),
                "subtitles": str(short.subtitle_path) if short.subtitle_path else None,
                "poster": str(short.poster_path),
                "duration": short.duration,
            }
        )
    _print_json(rendered)


def _burn(arguments: argparse.Namespace) -> None:
    output = arguments.output or arguments.video.with_name(f"{arguments.video.stem}.burned.mp4")
    path = burn_subtitles(
        arguments.video, arguments.subtitles, output, prefer_hardware=not arguments.software
    )
    _print_json({"video": str(path)})


def _report(arguments: argparse.Namespace) -> None:
    manifest = load_manifest(arguments.manifest)
    output = arguments.output or arguments.manifest.with_name("report.html")
    path = write_report(manifest, output)
    _print_json({"report": str(path), "bytes": path.stat().st_size})


HANDLERS: dict[str, Callable[[argparse.Namespace], None]] = {
    "analyze": _analyze,
    "frames": _frames,
    "shorts": _shorts,
    "burn": _burn,
    "report": _report,
}


def parse(argv: Sequence[str]) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def run(arguments: argparse.Namespace) -> None:
    HANDLERS[arguments.command](arguments)
