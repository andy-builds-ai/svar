"""The grounding probe and its answer set, without AWS: what it creates, measures and deletes."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from scripts import evaluate, grounding_probe
from tests.test_topic_probe import FakeBedrock

QUESTION_SETS = {
    "en": evaluate.QUESTIONS_FILE,
    "de": evaluate.QUESTIONS_FILE.with_name("evaluation-questions-de.json"),
    "kalvenstett": evaluate.QUESTIONS_FILE.with_name("evaluation-questions-kalvenstett.json"),
}


@pytest.fixture(scope="module")
def answers() -> list[dict[str, Any]]:
    return grounding_probe.load_answers(grounding_probe.ANSWERS_FILE)


# --- The answer set ------------------------------------------------------------------------


def test_every_answer_belongs_to_an_answer_question_of_an_evaluation_set(
    answers: list[dict[str, Any]],
) -> None:
    """Same question, workspace and language: the passages it is checked on are the ones the
    evaluation retrieves."""
    asked = {
        (q["workspace"], q["question"], q.get("language", "en"))
        for path in QUESTION_SETS.values()
        for q in evaluate.load_questions(path)
        if q["kind"] == "answer"
    }
    for answer in answers:
        assert (answer["workspace"], answer["question"], answer["language"]) in asked, answer["id"]


def test_the_sources_of_kalvenstett_are_german_and_all_others_english(
    answers: list[dict[str, Any]],
) -> None:
    for answer in answers:
        expected = "de" if answer["workspace"] == "kalvenstett" else "en"
        assert answer["source_language"] == expected, answer["id"]


def test_every_made_up_german_answer_on_english_sources_has_its_english_twin(
    answers: list[dict[str, Any]],
) -> None:
    """The same claim in English on the same documents: what the check withholds in English
    is what a German answer is compared with."""
    made_up = Counter(
        (a["workspace"], a["kind"], a["language"])
        for a in answers
        if not a["grounded"] and a["source_language"] == "en"
    )
    for (workspace, kind, language), count in made_up.items():
        other = "en" if language == "de" else "de"
        assert made_up[(workspace, kind, other)] == count, (workspace, kind)


def test_every_group_has_answers_of_every_kind(answers: list[dict[str, Any]]) -> None:
    groups = Counter((a["language"], a["source_language"], a["kind"]) for a in answers)
    for language, source_language, grounded in grounding_probe.GROUPS:
        kinds = ["right"] if grounded else list(grounding_probe.KINDS[1:])
        for kind in kinds:
            assert groups[(language, source_language, kind)] >= 10, (language, kind)


def test_the_answers_the_check_withheld_on_04_and_06_october_are_in_the_set(
    answers: list[dict[str, Any]],
) -> None:
    """de11 in its three forms (04.10.2026) and de03 as the model wrote it on 06.10.2026."""
    right = {a["answer"] for a in answers if a["grounded"]}
    assert "Die Garantie gilt für 5 Jahre [1][4]." in right
    assert "Die Garantie gilt für 5 Jahre [4]." in right
    assert any(a.startswith("Die Garantie gilt für 5 Jahre, wenn") for a in right)
    assert "25 % [1]" in right


# --- The answers to partial questions ------------------------------------------------------

PARTIAL_ANSWERS_FILE = grounding_probe.ANSWERS_FILE.with_name("grounding-partial-answers.json")


@pytest.fixture(scope="module")
def partial_answers() -> list[dict[str, Any]]:
    return grounding_probe.load_answers(PARTIAL_ANSWERS_FILE)


def test_every_partial_answer_belongs_to_a_partial_question_of_the_control_set(
    partial_answers: list[dict[str, Any]],
) -> None:
    control = {
        (q["workspace"], q["question"], q["language"])
        for q in json.loads(
            evaluate.QUESTIONS_FILE.with_name("evaluation-questions-control.json").read_text(
                encoding="utf-8"
            )
        )
        if q.get("partial")
    }
    for answer in partial_answers:
        assert (answer["workspace"], answer["question"], answer["language"]) in control, answer[
            "id"
        ]


def test_every_partial_question_with_german_sources_or_in_english_has_a_made_up_answer(
    partial_answers: list[dict[str, Any]],
) -> None:
    """The right part and an invented rest: what the check must still withhold. German answers
    on English sources are withheld for their language already (06.10.2026)."""
    kinds: dict[str, set[str]] = {}
    for answer in partial_answers:
        kinds.setdefault(answer["question"], set()).add(answer["kind"])
        if answer["kind"] != "right":
            assert answer["kind"] == "added-claim", answer["id"]
    for answer in partial_answers:
        if (answer["language"], answer["source_language"]) != ("de", "en"):
            assert kinds[answer["question"]] == {"right", "added-claim"}, answer["id"]


def test_c10_is_in_the_partial_set_as_qwen_wrote_it(
    partial_answers: list[dict[str, Any]],
) -> None:
    """Withheld at 0.8 on 07.10.2026, passed in four runs at 0.7 on 06.10.2026."""
    written = (
        "Auf ein neues Bad geben wir fünf Jahre Gewährleistung ab dem Tag der Abnahme [3]. "
        "Die durchschnittliche Haltbarkeit eines neuen Bads wird in den Quellen nicht genannt. "
        "Die Quellen beantworten diese Frage nicht."
    )
    assert written in {a["answer"] for a in partial_answers if a["grounded"]}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"kind": "guessed"}, "unknown kind"),
        ({"grounded": False}, "a right answer is grounded"),
        ({"grounded": "yes"}, "a right answer is grounded"),
        ({"language": "fr"}, "unknown language"),
        ({"source_language": None}, "unknown source_language"),
        ({"answer": " "}, "answer must be a non-empty text"),
    ],
)
def test_a_malformed_answer_is_rejected(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps([{**SAMPLE[0], **change}]), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        grounding_probe.load_answers(path)


def test_an_id_used_twice_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps([SAMPLE[0], SAMPLE[0]]), encoding="utf-8")
    with pytest.raises(ValueError, match="used twice"):
        grounding_probe.load_answers(path)


# --- A run ---------------------------------------------------------------------------------

SAMPLE = [
    {
        "id": "r1",
        "language": "de",
        "workspace": "pellwick",
        "source_language": "en",
        "question": "Wie lange gilt die Garantie?",
        "answer": "Die Garantie gilt für 5 Jahre [1].",
        "grounded": True,
        "kind": "right",
    },
    {
        "id": "x1",
        "language": "de",
        "workspace": "pellwick",
        "source_language": "en",
        "question": "Wie lange gilt die Garantie?",
        "answer": "Die Garantie gilt für 10 Jahre [1].",
        "grounded": False,
        "kind": "wrong-value",
    },
    {
        "id": "x2",
        "language": "en",
        "workspace": "pellwick",
        "source_language": "en",
        "question": "How long is the guarantee?",
        "answer": "The guarantee lasts 10 years [1].",
        "grounded": False,
        "kind": "wrong-value",
    },
    {
        "id": "k1",
        "language": "de",
        "workspace": "kalvenstett",
        "source_language": "de",
        "question": "Wie lange gilt die Gewährleistung?",
        "answer": "Fünf Jahre [1].",
        "grounded": True,
        "kind": "right",
    },
]

#: What the German question is translated to: not the English answer's own question.
ENGLISH_QUESTION = "How long does the guarantee last?"

#: The English each German answer is translated to, and every text's grounding score.
TRANSLATIONS = {
    "Die Garantie gilt für 5 Jahre [1].": "The guarantee lasts 5 years [1].",
    "Die Garantie gilt für 10 Jahre [1].": "The guarantee lasts 10 years [1].",
}
SCORES = {
    "Die Garantie gilt für 5 Jahre [1].": 0.54,
    "The guarantee lasts 5 years [1].": 0.81,
    "Die Garantie gilt für 10 Jahre [1].": 0.63,
    "The guarantee lasts 10 years [1].": 0.76,
    "Fünf Jahre [1].": 0.98,
}


class FakeAgent:
    """Retrieve: one passage in the workspace asked for, and one of another workspace."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        workspace = kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"][
            "equals"
        ]["value"]
        return {
            "retrievalResults": [
                passage(workspace, "The guarantee lasts 5 years."),
                passage("other", "Somebody else's document."),
            ]
        }


def passage(workspace: str, text: str) -> dict[str, Any]:
    return {
        "content": {"text": text},
        "location": {"s3Location": {"uri": f"s3://bucket/workspaces/{workspace}/human/g--01.md"}},
        "metadata": {"workspace": workspace, "layer": "human"},
        "score": 0.8,
    }


class FakeRuntime:
    """ApplyGuardrail scores a text by `SCORES` against 0.7; converse translates by
    `TRANSLATIONS`. ``fail`` names a call that raises."""

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.checks: list[dict[str, Any]] = []
        self.translations: list[dict[str, Any]] = []

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.checks.append(kwargs)
        if self.fail == "check" and kwargs["content"][1]["text"]["text"] == ENGLISH_QUESTION:
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "ApplyGuardrail")
        if self.fail == "written" and kwargs["content"][1]["text"]["text"] == SAMPLE[2]["question"]:
            raise ClientError({"Error": {"Code": "ValidationException"}}, "ApplyGuardrail")
        score = SCORES[kwargs["content"][2]["text"]["text"]]
        filters = [
            {"type": "GROUNDING", "score": score, "action": "BLOCKED" if score < 0.7 else "NONE"},
            {"type": "RELEVANCE", "score": 1.0, "action": "NONE"},
        ]
        return {
            "action": "GUARDRAIL_INTERVENED" if score < 0.7 else "NONE",
            "assessments": [{"contextualGroundingPolicy": {"filters": filters}}],
            "usage": {"contextualGroundingPolicyUnits": 2, "sensitiveInformationPolicyUnits": 1},
        }

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.translations.append(kwargs)
        if self.fail == "translation":
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse")
        if self.fail == "network":
            raise EndpointConnectionError(endpoint_url="https://bedrock-runtime.example")
        asked = kwargs["messages"][0]["content"][0]["text"]
        question, _, answer = asked.partition("\nAnswer: ")
        english_question = ENGLISH_QUESTION
        text = f"Question: {english_question}\nAnswer: {TRANSLATIONS[answer]}"
        assert question.startswith("Question: ")
        return {
            "output": {"message": {"content": [{"text": text}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 110, "outputTokens": 40},
            "trace": {
                "guardrail": {
                    "inputAssessment": {
                        "probe1": {"invocationMetrics": {"usage": {"topicPolicyUnits": 1}}}
                    },
                    "outputAssessments": {},
                }
            },
        }


ENVIRONMENT = {
    "KNOWLEDGE_BASE_ID": "KB1",
    "MODEL_ID": "eu.amazon.nova-pro-v1:0",
    "REGION": "eu-west-1",
    "NUMBER_OF_RESULTS": "10",
    "API_KEY_PARAMETER_PATH": "/andur/api-keys",
    "BUDGET_TABLE_NAME": "andur-key-budgets",
    "QUESTION_LIMIT": "10",
    "WINDOW_HOURS": "48",
    "KEEP_DAYS": "365",
    "GUARDRAIL_ID": "answer1",
    "GUARDRAIL_VERSION": "4",
    "EXCERPT_GUARDRAIL_ID": "excerpt1",
    "EXCERPT_GUARDRAIL_VERSION": "2",
    "TOPIC_GUARDRAIL_ID": "topic1",
    "TOPIC_GUARDRAIL_VERSION": "3",
    "PRICE_INPUT_PER_1K_TOKENS": "0.00092",
    "PRICE_OUTPUT_PER_1K_TOKENS": "0.00368",
    "PRICE_TOPIC_PER_1K_UNITS": "0.15",
    "PRICE_PII_PER_1K_UNITS": "0.10",
    "PRICE_GROUNDING_PER_1K_UNITS": "0.10",
    "PRICE_DATE": "2026-09-28",
}


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bedrock: FakeBedrock,
    runtime: FakeRuntime,
    *args: str,
) -> int:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(SAMPLE), encoding="utf-8")
    clients = {"bedrock": bedrock, "bedrock-runtime": runtime, "bedrock-agent-runtime": FakeAgent()}
    monkeypatch.setattr(grounding_probe, "client", lambda service, region: clients[service])
    monkeypatch.setattr(grounding_probe, "deployed_environment", lambda region: ENVIRONMENT)
    monkeypatch.setattr(
        grounding_probe.topic_probe, "wait_until_ready", lambda b, g, sleep=None, name="": None
    )
    return grounding_probe.main(["--answers", str(path), *args])


def test_a_run_checks_every_answer_and_only_a_withheld_german_one_in_english(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bedrock, runtime = FakeBedrock(), FakeRuntime()
    out = tmp_path / "out.json"
    assert run_main(monkeypatch, tmp_path, bedrock, runtime, "--out", str(out)) == 0
    assert bedrock.names() == ["list_guardrails", "create_guardrail", "get_guardrail"] + [
        "delete_guardrail"
    ]
    assert bedrock.calls[1][1] == grounding_probe.probe_request()
    assert bedrock.guardrails == {}
    # Every check on the probe's draft, with the workspace's passages only.
    assert {
        (c["guardrailIdentifier"], c["guardrailVersion"], c["source"]) for c in runtime.checks
    } == {("probe1", "DRAFT", "OUTPUT")}
    first = runtime.checks[0]["content"]
    assert first[0]["text"]["qualifiers"] == ["grounding_source"]
    assert "The guarantee lasts 5 years." in first[0]["text"]["text"]
    assert "Somebody else" not in first[0]["text"]["text"]
    assert first[1] == {"text": {"text": SAMPLE[0]["question"], "qualifiers": ["query"]}}
    assert first[2] == {"text": {"text": SAMPLE[0]["answer"], "qualifiers": ["guard_content"]}}
    # Two German answers withheld as written: two translations, through the probe.
    assert len(runtime.translations) == 2
    for call in runtime.translations:
        assert call["guardrailConfig"]["guardrailIdentifier"] == "probe1"
        assert call["modelId"] == ENVIRONMENT["MODEL_ID"]
        assert call["system"] == [{"text": grounding_probe.TRANSLATION_PROMPT}]
    saved = json.loads(out.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in saved["rows"]}
    assert (rows["r1"]["as_written"], rows["r1"]["with_english"]) == (False, True)
    assert (rows["x1"]["as_written"], rows["x1"]["with_english"]) == (False, True)
    assert (rows["x2"]["as_written"], rows["x2"]["with_english"]) == (True, True)
    assert rows["x2"]["english"] is None  # English answers are never checked again
    assert rows["k1"]["english"] is None  # nor German ones the check passed
    assert rows["r1"]["english"]["translation"] == "The guarantee lasts 5 years [1]."
    groups = saved["summary"]["groups"]
    assert groups["right, de on en sources"] == {
        "answers": 1,
        "passed_as_written": 0,
        "passed_with_english": 1,
        "checked_in_english": 1,
        "translation_unusable": 0,
        "by_kind": {},
    }
    assert groups["made up, de on en sources"]["by_kind"]["wrong-value"] == {
        "answers": 1,
        "withheld_as_written": 1,
        "withheld_with_english": 0,
    }
    printed = capsys.readouterr().out
    assert "| right, de on de sources | 1 | 1 | 1 | 0 | 0 |" in printed
    assert "| x1 | withheld | withheld | passed | 0.63 / 1.0 | 0.76 / 1.0 |" in printed


def test_the_cost_of_a_run_counts_both_checks_and_the_translation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.json"
    run_main(monkeypatch, tmp_path, FakeBedrock(), FakeRuntime(), "--out", str(out))
    rows = {row["id"]: row for row in json.loads(out.read_text(encoding="utf-8"))["rows"]}
    # Two checks of 2 grounding and 1 PII units each, and a translation of 110 tokens in, 40
    # out and 1 topic unit.
    expected = (110 * 0.00092 + 40 * 0.00368 + 1 * 0.15 + 4 * 0.10 + 2 * 0.10) / 1000
    assert rows["r1"]["cost_usd"] == round(expected, 6)
    assert rows["k1"]["cost_usd"] == round((2 * 0.10 + 1 * 0.10) / 1000, 6)


@pytest.mark.parametrize("failing", ["translation", "check", "network"])
def test_a_failed_english_check_is_named_and_fails_the_run_after_its_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing: str,
) -> None:
    """A failed call is no verdict: it is not counted as a withheld answer without saying so."""
    bedrock = FakeBedrock()
    monkeypatch.setattr(grounding_probe.random, "uniform", lambda low, high: 0)
    code = run_main(monkeypatch, tmp_path, bedrock, FakeRuntime(fail=failing))
    assert code == 2
    printed = capsys.readouterr().out
    assert "Failed calls, counted as withheld: r1, x1" in printed
    assert bedrock.guardrails == {}


def test_the_probe_is_deleted_when_the_measurement_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bedrock = FakeBedrock()

    class Broken(FakeRuntime):
        def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("the network went away")

    with pytest.raises(RuntimeError, match="network"):
        run_main(monkeypatch, tmp_path, bedrock, Broken())
    assert bedrock.names()[-1] == "delete_guardrail"
    assert bedrock.guardrails == {}


def test_a_probe_left_by_an_earlier_run_stops_the_run_before_anything_is_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bedrock = FakeBedrock(existing=[{"id": "old1", "name": grounding_probe.PROBE_NAME}])
    runtime = FakeRuntime()
    assert run_main(monkeypatch, tmp_path, bedrock, runtime) == 2
    assert "create_guardrail" not in bedrock.names()
    assert "delete_guardrail" not in bedrock.names()
    assert runtime.checks == []
    assert "old1" in capsys.readouterr().err


@pytest.mark.parametrize(
    "reply",
    [
        "The guarantee lasts 5 years [1].",
        "Question: How long?\nAnswer: Five [1].\nAnswer: Again.",
        "Question: How long?\nAnswer:  ",
        "Answer: Five [1].\nQuestion: How long?",
        "Question:\nAnswer: Five [1].",
    ],
)
def test_a_translation_not_in_the_form_asked_for_passes_nothing_and_is_no_failure(
    reply: str,
) -> None:
    class Replies(FakeRuntime):
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            response = super().converse(**kwargs)
            response["output"]["message"]["content"] = [{"text": reply}]
            return response

    app = evaluate.import_app()
    settings = app.config.load_settings(ENVIRONMENT)
    runtime = Replies()
    english = grounding_probe.check_in_english(
        app, runtime, settings, SAMPLE[0]["question"], SAMPLE[0]["answer"], "Sources:"
    )
    assert english["passed"] is False
    assert english["problem"] == grounding_probe.UNUSABLE_TRANSLATION
    assert runtime.checks == []


def test_a_check_that_reports_no_score_passes_nothing() -> None:
    class NoScores:
        def apply_guardrail(self, **_: Any) -> dict[str, Any]:
            return {"action": "NONE", "assessments": [], "usage": {}}

    app = evaluate.import_app()
    settings = app.config.load_settings(ENVIRONMENT)
    check = grounding_probe.grounding_check(app, NoScores(), settings, "S", "Q?", "A [1].")
    assert check["passed"] is False


def test_a_throttled_check_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    pauses: list[float] = []

    def throttled_once() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "ApplyGuardrail")
        return "done"

    assert grounding_probe.retrying(throttled_once, sleep=pauses.append) == "done"
    assert len(calls) == 2 and len(pauses) == 1


def test_workers_must_be_at_least_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tmp_path, FakeBedrock(), FakeRuntime(), "--workers", "0")


def test_a_region_outside_the_eu_profile_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bedrock = FakeBedrock()
    assert run_main(monkeypatch, tmp_path, bedrock, FakeRuntime(), "--region", "us-east-1") == 2
    assert bedrock.calls == []


def test_a_failed_check_as_written_is_named_and_the_rest_of_the_run_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The calls already paid for are reported and saved; the answer counts as withheld."""
    bedrock = FakeBedrock()
    out = tmp_path / "out.json"
    runtime = FakeRuntime(fail="written")
    assert run_main(monkeypatch, tmp_path, bedrock, runtime, "--out", str(out)) == 2
    assert "Failed calls, counted as withheld: x2" in capsys.readouterr().out
    rows = {row["id"]: row for row in json.loads(out.read_text(encoding="utf-8"))["rows"]}
    assert rows["x2"]["error"].startswith("ValidationException")
    assert (rows["x2"]["as_written"], rows["x2"]["english"]) == (False, None)
    assert rows["k1"]["as_written"] is True
    assert bedrock.guardrails == {}


def test_a_translation_not_in_the_form_asked_for_is_counted_per_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """So a made-up answer withheld only because its translation was unusable is not hidden
    among those the English check withheld."""

    class Unusable(FakeRuntime):
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            response = super().converse(**kwargs)
            response["output"]["message"]["content"] = [{"text": "No translation."}]
            return response

    out = tmp_path / "out.json"
    assert run_main(monkeypatch, tmp_path, FakeBedrock(), Unusable(), "--out", str(out)) == 0
    groups = json.loads(out.read_text(encoding="utf-8"))["summary"]["groups"]
    assert groups["made up, de on en sources"]["translation_unusable"] == 1
    assert groups["right, de on en sources"]["translation_unusable"] == 1
    assert groups["right, de on de sources"]["translation_unusable"] == 0
