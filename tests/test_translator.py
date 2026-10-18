import json
import re
import subprocess
from typing import Any

import pytest

from youtune.errors import TranslationError
from youtune.subtitles import Cue
from youtune.translator import ClaudeCodeTranslator, TranslatorSettings

LINE = re.compile(r"^\[(\d+)\] (.*)$", re.MULTILINE)


def completed(
    stdout: str, *, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def success(translations: list[dict[str, Any]]) -> subprocess.CompletedProcess[str]:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": {"translations": translations},
    }
    return completed(json.dumps(payload))


def echo_translations(prompt: str) -> list[dict[str, Any]]:
    section = prompt.split("Lines to translate:\n", 1)[1]
    return [{"index": int(index), "text": f"KO {text}"} for index, text in LINE.findall(section)]


class FakeRunner:
    """Replays queued responses; when the queue is empty, echoes a valid translation."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **keyword_arguments: Any) -> Any:
        self.calls.append({"command": command, **keyword_arguments})
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return success(echo_translations(keyword_arguments["input"]))


@pytest.fixture(autouse=True)
def claude_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("youtune.translator.shutil.which", lambda name: f"/usr/local/bin/{name}")


def cues(count: int) -> list[Cue]:
    return [Cue(index * 1000, index * 1000 + 900, f"line {index + 1}") for index in range(count)]


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def translator(
    runner: FakeRunner, sleep: RecordingSleep | None = None, **settings: Any
) -> ClaudeCodeTranslator:
    return ClaudeCodeTranslator(
        TranslatorSettings(**{"workers": 1, **settings}),
        runner=runner,
        sleep=sleep or RecordingSleep(),
    )


def test_translates_every_cue_and_keeps_timings() -> None:
    runner = FakeRunner()

    result = translator(runner).translate(cues(3), source_language="en", title="Talk")

    assert result == [
        Cue(0, 900, "KO line 1"),
        Cue(1000, 1900, "KO line 2"),
        Cue(2000, 2900, "KO line 3"),
    ]


def test_splits_into_batches_with_context() -> None:
    runner = FakeRunner()

    result = translator(runner, batch_size=2, context_lines=1).translate(
        cues(5), source_language="en", title="Talk"
    )

    assert len(result) == 5
    prompts = sorted((call["input"] for call in runner.calls), key=len)
    assert len(prompts) == 3
    second_batch = next(prompt for prompt in prompts if "[3] line 3" in prompt)
    assert "Video title: Talk" in second_batch
    assert "Preceding lines (context only, do not translate):\nline 2" in second_batch
    assert "[1]" not in second_batch


def test_runs_isolated_headless_session() -> None:
    runner = FakeRunner()

    translator(runner, model="opus").translate(cues(1), source_language="ja", title="T")

    call = runner.calls[0]
    command = call["command"]
    assert command[0] == "/usr/local/bin/claude"
    for flag in ("--print", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in command
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--setting-sources") + 1] == ""
    assert command[command.index("--model") + 1] == "opus"
    assert "from ja into natural, fluent Korean" in command[command.index("--system-prompt") + 1]
    assert json.loads(command[command.index("--json-schema") + 1])["required"] == ["translations"]
    # Prompt goes through stdin, never through argv.
    assert "line 1" not in " ".join(command)
    assert "youtune-" in call["cwd"]


def test_retries_on_index_mismatch() -> None:
    runner = FakeRunner(success([{"index": 1, "text": "only one"}]))
    sleep = RecordingSleep()

    result = translator(runner, sleep).translate(cues(2), source_language="en", title="T")

    assert [cue.text for cue in result] == ["KO line 1", "KO line 2"]
    assert len(runner.calls) == 2
    assert sleep.delays == [2.0]


@pytest.mark.parametrize(
    "failure",
    [
        completed("", returncode=1, stderr="Not logged in"),
        completed(json.dumps({"subtype": "error_during_execution", "is_error": True})),
        completed("[]"),
        completed(json.dumps({"subtype": "success", "is_error": False, "result": "not json"})),
        subprocess.TimeoutExpired(cmd="claude", timeout=1),
        FileNotFoundError("claude"),
    ],
)
def test_gives_up_after_maximum_attempts(failure: Any) -> None:
    runner = FakeRunner(failure, failure, failure)
    sleep = RecordingSleep()
    instance = translator(runner, sleep, maximum_attempts=3, retry_delay_seconds=1.0)

    with pytest.raises(TranslationError, match="batch 1 failed after 3 attempts"):
        instance.translate(cues(1), source_language="en", title="T")

    assert len(runner.calls) == 3
    assert sleep.delays == [1.0, 2.0]


def test_reads_result_text_when_structured_output_missing() -> None:
    payload = {"subtype": "success", "is_error": False, "result": json.dumps(
        {"translations": [{"index": 1, "text": "translated hello"}]}
    )}  # fmt: skip
    runner = FakeRunner(completed(json.dumps(payload)))

    result = translator(runner).translate(cues(1), source_language="en", title="T")

    assert result[0].text == "translated hello"


def test_empty_translation_keeps_source_text() -> None:
    runner = FakeRunner(success([{"index": 1, "text": "  "}]))

    result = translator(runner).translate(cues(1), source_language="en", title="T")

    assert result[0].text == "line 1"


@pytest.mark.parametrize("folded_text", ["", ".", " ... "])
def test_folded_line_extends_previous_cue(folded_text: str) -> None:
    runner = FakeRunner(
        success(
            [
                {"index": 1, "text": "first and second lines combined"},
                {"index": 2, "text": folded_text},
                {"index": 3, "text": "third line"},
            ]
        )
    )

    result = translator(runner).translate(cues(3), source_language="en", title="T")

    assert result == [
        Cue(0, 1900, "first and second lines combined"),
        Cue(2000, 2900, "third line"),
    ]


def test_folding_works_across_batch_boundaries() -> None:
    runner = FakeRunner(
        success([{"index": 1, "text": "one"}, {"index": 2, "text": "two"}]),
        success([{"index": 3, "text": ""}]),
    )

    result = translator(runner, batch_size=2).translate(cues(3), source_language="en", title="T")

    assert result == [Cue(0, 900, "one"), Cue(1000, 2900, "two")]


def test_prompt_allows_folding_but_forbids_echo() -> None:
    runner = FakeRunner()

    translator(runner).translate(cues(1), source_language="en", title="T")

    system_prompt = runner.calls[0]["command"][
        runner.calls[0]["command"].index("--system-prompt") + 1
    ]
    assert "return an empty string" in system_prompt
    assert "Never copy the source text untranslated" in system_prompt


def test_empty_input_skips_cli() -> None:
    runner = FakeRunner()

    assert translator(runner).translate([], source_language="en", title="T") == []
    assert runner.calls == []


def test_missing_cli_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("youtune.translator.shutil.which", lambda name: None)

    with pytest.raises(TranslationError, match="not found on PATH"):
        translator(FakeRunner()).translate(cues(1), source_language="en", title="T")


def test_parallel_workers_translate_all_batches() -> None:
    runner = FakeRunner()

    result = translator(runner, batch_size=3, workers=4).translate(
        cues(20), source_language="en", title="T"
    )

    assert [cue.text for cue in result] == [f"KO line {index}" for index in range(1, 21)]
    assert len(runner.calls) == 7
