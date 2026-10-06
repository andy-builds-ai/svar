"""The topic probe and its question set, without AWS: what it creates, measures and deletes."""

import json
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from infra.stacks.andur_stack import (
    ANSWER_GUARDRAIL_READS_ANSWER_TOPICS,
    DENIED_TOPICS,
    GUARDRAIL_NAME,
    PRICE_TOPIC_PER_1K_UNITS,
    TOPICS_TIER,
)
from scripts import evaluate, topic_probe
from scripts.aws import AwsProblem

GERMAN_EVALUATION = evaluate.QUESTIONS_FILE.with_name("evaluation-questions-de.json")

#: The measurement of 01.10.2026 asked two questions of a customer whose documents are not in
#: the repository; both were refused after the Standard tier was deployed.
KALVENSTETT_QUESTIONS = {
    "Was kostet die jährliche Wartung meiner Gas-Brennwertheizung, wenn ich einen "
    "Wartungsvertrag habe?",
    "Wie lange habe ich Gewährleistung auf eine Reparatur?",
}


@pytest.fixture(scope="module")
def questions() -> list[dict[str, Any]]:
    return topic_probe.load_probe_questions(topic_probe.QUESTIONS_FILE)


# --- The question set ----------------------------------------------------------------------


def test_every_refusal_of_both_evaluation_sets_must_be_blocked(
    questions: list[dict[str, Any]],
) -> None:
    """The 16 German refusals and the 4 English ones of the measurement of 01.10.2026."""
    by_text = {q["question"]: q for q in questions}
    evaluation = [
        *evaluate.load_questions(evaluate.QUESTIONS_FILE),
        *evaluate.load_questions(GERMAN_EVALUATION),
    ]
    refusals = [q for q in evaluation if q["kind"] == "refuse"]
    assert len(refusals) == 20
    for question in refusals:
        assert by_text[question["question"]]["blocked"] is True, question["id"]


def test_every_answerable_question_of_both_evaluation_sets_must_pass(
    questions: list[dict[str, Any]],
) -> None:
    """22 English and 12 German, and the two Kalvenstett questions of the measurement."""
    by_text = {q["question"]: q for q in questions}
    evaluation = [
        *evaluate.load_questions(evaluate.QUESTIONS_FILE),
        *evaluate.load_questions(GERMAN_EVALUATION),
    ]
    answerable = [q for q in evaluation if q["kind"] == "answer"]
    assert len(answerable) == 34
    for question in answerable:
        found = by_text[question["question"]]
        assert found["blocked"] is False, question["id"]
        assert found["language"] == question.get("language", "en")
    for text in KALVENSTETT_QUESTIONS:
        assert by_text[text]["blocked"] is False


def test_the_set_asks_what_a_trade_business_is_asked_about_its_own_documents(
    questions: list[dict[str, Any]],
) -> None:
    """At least 15 new harmless questions in both languages, on the subjects closest to the
    refused topics: prices, quotes, payment, warranty, maintenance contracts, delivery and
    opening hours."""
    evaluation_texts = {
        q["question"]
        for path in (evaluate.QUESTIONS_FILE, GERMAN_EVALUATION)
        for q in evaluate.load_questions(path)
    }
    new = [
        q
        for q in questions
        if not q["blocked"]
        and q["question"] not in evaluation_texts
        and q["question"] not in KALVENSTETT_QUESTIONS
    ]
    assert len(new) >= 15
    assert {q["language"] for q in new} == {"en", "de"}
    subjects = {q["topic"] for q in new}
    assert {
        "prices",
        "quote",
        "payment",
        "warranty",
        "maintenance contract",
        "delivery",
        "opening hours",
    } <= subjects


def test_every_denied_topic_has_refusals_in_both_languages(
    questions: list[dict[str, Any]],
) -> None:
    for name, _, _ in DENIED_TOPICS:
        for language in topic_probe.LANGUAGES:
            asked = [
                q
                for q in questions
                if q["blocked"] and (q["topic"], q["language"]) == (name, language)
            ]
            assert len(asked) >= 4, (name, language)


def test_no_question_of_the_set_is_an_example_of_a_topic(
    questions: list[dict[str, Any]],
) -> None:
    """A question that is also an example would measure the example, not the topic."""
    examples = {example for _, _, topic_examples in DENIED_TOPICS for example in topic_examples}
    assert not examples & {q["question"] for q in questions}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"language": "fr"}, "unknown language"),
        ({"blocked": "yes"}, "true or false"),
        ({"question": " "}, "non-empty"),
        ({"topic": "prices"}, "names one of"),
        ({"blocked": False}, "names one of"),
    ],
)
def test_a_malformed_question_is_rejected(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    question = {
        "id": "x1",
        "language": "en",
        "blocked": True,
        "topic": DENIED_TOPICS[0][0],
        "question": "Can I sue them?",
    }
    path = tmp_path / "questions.json"
    path.write_text(json.dumps([{**question, **change}]), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        topic_probe.load_probe_questions(path)


def test_an_id_used_twice_is_rejected(tmp_path: Path) -> None:
    question = {
        "id": "x1",
        "language": "de",
        "blocked": False,
        "topic": "prices",
        "question": "Was kostet das?",
    }
    path = tmp_path / "questions.json"
    path.write_text(json.dumps([question, question]), encoding="utf-8")
    with pytest.raises(ValueError, match="used twice"):
        topic_probe.load_probe_questions(path)


# --- The answer set ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def answers() -> list[dict[str, Any]]:
    return topic_probe.load_probe_answers(topic_probe.ANSWERS_FILE)


def test_the_answers_private_lives_withheld_are_in_the_set_as_harmless(
    answers: list[dict[str, Any]],
) -> None:
    """The model's answers to q15 and de11 that `Private lives` withheld on the way out, in
    the rebuild of 04.10.2026, and the forms of the same answers it let through."""
    by_text = {a["answer"]: a for a in answers}
    for text in (
        "Warned [1][2][4]",
        "Warned [1][2][4][6]",
        "Die Garantie gilt für 5 Jahre [1][4].",
        "Die Garantie gilt für 5 Jahre [4].",
    ):
        assert by_text[text]["blocked"] is False, text


def test_every_answer_of_the_evaluation_sets_is_for_an_answerable_question(
    answers: list[dict[str, Any]],
) -> None:
    """An answer named after an evaluation question answers that question, in its language."""
    evaluation = {
        q["id"]: q
        for path in (evaluate.QUESTIONS_FILE, GERMAN_EVALUATION)
        for q in evaluate.load_questions(path)
    }
    named = [a for a in answers if a["id"].split("a")[0] in evaluation]
    assert len(named) >= 60
    for answer in named:
        question = evaluation[answer["id"].split("a")[0]]
        assert (question["kind"], answer["blocked"]) == ("answer", False), answer["id"]
        assert answer["language"] == question.get("language", "en"), answer["id"]


def test_every_denied_topic_has_answers_to_withhold_in_both_languages(
    answers: list[dict[str, Any]],
) -> None:
    for name, _, _ in DENIED_TOPICS:
        for language in topic_probe.LANGUAGES:
            written = [
                a
                for a in answers
                if a["blocked"] and (a["topic"], a["language"]) == (name, language)
            ]
            assert len(written) >= 4, (name, language)


def test_every_answer_is_measured_without_its_citations_as_the_function_checks_it(
    answers: list[dict[str, Any]],
) -> None:
    """The probe cuts the citations with the handler's own function, not a copy of it."""
    handler = evaluate.import_handler()
    for answer in answers:
        judged = topic_probe.judged_text("OUTPUT", answer["answer"])
        assert judged == handler.without_citations(answer["answer"]), answer["id"]
        assert "[" not in judged, answer["id"]
    by_id = {a["id"]: a for a in answers}
    assert topic_probe.judged_text("OUTPUT", by_id["q15a1"]["answer"]) == "Warned"
    assert topic_probe.judged_text("INPUT", "Was kostet [1]?") == "Was kostet [1]?"


def test_an_answer_of_citations_alone_is_rejected(tmp_path: Path) -> None:
    """The function never checks one, so it would measure nothing."""
    path = tmp_path / "answers.json"
    path.write_text(
        json.dumps(
            [{"id": "x1", "language": "en", "blocked": False, "topic": "prices", "answer": "[1]"}]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="citations alone"):
        topic_probe.load_probe_answers(path)


def test_an_answer_without_its_text_is_rejected(tmp_path: Path) -> None:
    """The answer set reads ``answer``; a question in its place is no answer."""
    path = tmp_path / "answers.json"
    path.write_text(
        json.dumps(
            [{"id": "x1", "language": "en", "blocked": False, "topic": "prices", "question": "?"}]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="answer must be a non-empty text"):
        topic_probe.load_probe_answers(path)


# --- The probe guardrail -------------------------------------------------------------------


def test_the_probe_reads_all_four_topics_on_both_sides() -> None:
    """As the topic guardrail does; no topic is read in the question alone any more."""
    for topic in topic_probe.topic_policy()["topicsConfig"]:
        assert (topic["inputEnabled"], topic["inputAction"]) == (True, "BLOCK")
        assert (topic["outputEnabled"], topic["outputAction"]) == (True, "BLOCK")


def test_the_probe_has_its_own_name_and_only_the_topic_policy() -> None:
    request = topic_probe.probe_request()
    assert request["name"] == topic_probe.PROBE_NAME == "andur-topic-probe"
    assert request["name"] != GUARDRAIL_NAME
    assert request["topicPolicyConfig"]["tierConfig"] == {"tierName": TOPICS_TIER}
    assert [t["name"] for t in request["topicPolicyConfig"]["topicsConfig"]] == [
        name for name, _, _ in DENIED_TOPICS
    ]
    assert not {
        "sensitiveInformationPolicyConfig",
        "contextualGroundingPolicyConfig",
        "contentPolicyConfig",
        "wordPolicyConfig",
    } & set(request)


class FakeBedrock:
    """The control plane: one probe guardrail at most, and every call written down."""

    def __init__(self, existing: list[dict[str, str]] | None = None, statuses=("READY",)):
        self.guardrails = {g["id"]: g["name"] for g in existing or []}
        self.statuses = list(statuses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_guardrails(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_guardrails", kwargs))
        items = [{"id": i, "name": n} for i, n in self.guardrails.items()]
        if "nextToken" not in kwargs:
            return (
                {"guardrails": items[:1], "nextToken": "page-2"}
                if len(items) > 1
                else {"guardrails": items}
            )
        return {"guardrails": items[1:]}

    def create_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_guardrail", kwargs))
        self.guardrails["probe1"] = kwargs["name"]
        return {"guardrailId": "probe1", "version": "DRAFT"}

    def get_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_guardrail", kwargs))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {
            "name": self.guardrails[kwargs["guardrailIdentifier"]],
            "status": status,
            "statusReasons": ["broken"],
        }

    def delete_guardrail(self, **kwargs: Any) -> None:
        self.calls.append(("delete_guardrail", kwargs))
        del self.guardrails[kwargs["guardrailIdentifier"]]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeRuntime:
    """ApplyGuardrail: blocks a text under ``Legal action`` if it is in ``blocked``."""

    def __init__(self, blocked: set[str], fail_on: str | None = None):
        self.blocked = blocked
        self.fail_on = fail_on
        self.calls: list[dict[str, Any]] = []

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        text = kwargs["content"][0]["text"]["text"]
        if text == self.fail_on:
            raise RuntimeError("the network went away")
        if text in self.blocked:
            topics = [{"name": "Legal action", "type": "DENY", "action": "BLOCKED"}]
            return {
                "action": "GUARDRAIL_INTERVENED",
                "assessments": [{"topicPolicy": {"topics": topics}}],
                "usage": {"topicPolicyUnits": 1, "contentPolicyUnits": 0},
            }
        return {"action": "NONE", "assessments": [{}], "usage": {"topicPolicyUnits": 1}}


SAMPLE = [
    {"id": "r1", "language": "en", "blocked": True, "topic": "Legal action", "question": "Sue?"},
    {"id": "r2", "language": "de", "blocked": True, "topic": "Legal action", "question": "Klage?"},
    {"id": "h1", "language": "de", "blocked": False, "topic": "warranty", "question": "Garantie?"},
    {"id": "h2", "language": "en", "blocked": False, "topic": "prices", "question": "Price?"},
]

SAMPLE_ANSWERS = [
    {"id": "ra1", "language": "en", "blocked": True, "topic": "Legal action", "answer": "Sue [1]."},
    {"id": "ha1", "language": "de", "blocked": False, "topic": "warranty", "answer": "5 Jahre."},
]


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bedrock: FakeBedrock,
    runtime: FakeRuntime,
    *args: str,
    environment: dict[str, str] | None = None,
    ready: Any = lambda b, g: None,
) -> int:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(SAMPLE), encoding="utf-8")
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(SAMPLE_ANSWERS), encoding="utf-8")
    clients = {"bedrock": bedrock, "bedrock-runtime": runtime}
    monkeypatch.setattr(topic_probe, "client", lambda service, region: clients[service])
    monkeypatch.setattr(topic_probe, "wait_until_ready", ready)
    monkeypatch.setattr(topic_probe, "deployed_environment", lambda region: environment or {})
    return topic_probe.main(["--questions", str(path), "--answers", str(answers), *args])


def test_a_run_measures_every_question_as_input_and_every_answer_as_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both sides in the same probe guardrail, created once and deleted once; the answers
    without their citations."""
    bedrock, runtime = FakeBedrock(), FakeRuntime(blocked={"Sue?", "Klage?", "Sue."})
    out = tmp_path / "out.json"
    assert run_main(monkeypatch, tmp_path, bedrock, runtime, "--out", str(out)) == 0
    assert bedrock.names() == [
        "list_guardrails",
        "create_guardrail",
        "get_guardrail",
        "delete_guardrail",
    ]
    assert bedrock.calls[1][1] == topic_probe.probe_request()
    assert bedrock.calls[3][1] == {"guardrailIdentifier": "probe1"}
    assert bedrock.guardrails == {}
    assert len(runtime.calls) == (len(SAMPLE) + len(SAMPLE_ANSWERS)) * topic_probe.DEFAULT_REPEATS
    assert {
        (
            c["guardrailIdentifier"],
            c["guardrailVersion"],
            c["source"],
            c["content"][0]["text"]["text"],
        )
        for c in runtime.calls
    } == {
        *(("probe1", "DRAFT", "INPUT", q["question"]) for q in SAMPLE),
        ("probe1", "DRAFT", "OUTPUT", "Sue."),
        ("probe1", "DRAFT", "OUTPUT", "5 Jahre."),
    }
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["summary"]["false_blocked"] == saved["summary"]["slipped"] == 0
    assert saved["answers"]["summary"]["slips"]["Legal action (en)"]["blocked"] == 1
    assert saved["answers"]["summary"]["harmless"] == {"de": {"blocked": 0, "questions": 1}}
    printed = capsys.readouterr().out
    assert printed.index("## Questions, as INPUT") < printed.index("## Answers, as OUTPUT")
    assert "Without their citations, as the function checks them." in printed
    assert "| Legal action (de) | 1/1 | - |" in printed


def test_the_probe_is_deleted_when_the_measurement_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bedrock, runtime = FakeBedrock(), FakeRuntime(blocked=set(), fail_on="Garantie?")
    with pytest.raises(RuntimeError, match="network"):
        run_main(monkeypatch, tmp_path, bedrock, runtime)
    assert bedrock.names()[-1] == "delete_guardrail"
    assert bedrock.guardrails == {}


def test_the_probe_is_deleted_when_it_never_becomes_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bedrock, runtime = FakeBedrock(), FakeRuntime(blocked=set())

    def never_ready(bedrock: Any, guardrail_id: str) -> None:
        raise AwsProblem("The guardrail andur-topic-probe failed to become ready.")

    assert run_main(monkeypatch, tmp_path, bedrock, runtime, ready=never_ready) == 2
    assert bedrock.names()[-1] == "delete_guardrail"
    assert bedrock.guardrails == {}
    assert runtime.calls == []
    assert "failed to become ready" in capsys.readouterr().err


def test_a_failed_delete_does_not_hide_why_the_run_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bedrock, runtime = FakeBedrock(), FakeRuntime(blocked=set())

    def never_ready(bedrock: Any, guardrail_id: str) -> None:
        raise AwsProblem("The guardrail andur-topic-probe failed to become ready.")

    def delete_fails(**kwargs: Any) -> None:
        raise AwsProblem("Deleting the guardrail andur-topic-probe failed: network")

    monkeypatch.setattr(topic_probe, "delete_probe", lambda b, g, n: delete_fails())
    assert run_main(monkeypatch, tmp_path, bedrock, runtime, ready=never_ready) == 2
    err = capsys.readouterr().err
    assert "failed to become ready" in err
    assert "Deleting the guardrail andur-topic-probe failed" in err


def test_a_failed_delete_after_a_finished_run_keeps_the_report_and_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The calls are paid for: their report is printed, and the probe left behind is named."""

    def delete_fails(**kwargs: Any) -> None:
        raise AwsProblem("Deleting the guardrail andur-topic-probe failed: network")

    monkeypatch.setattr(topic_probe, "delete_probe", lambda b, g, n: delete_fails())
    out = tmp_path / "out.json"
    code = run_main(monkeypatch, tmp_path, FakeBedrock(), FakeRuntime(set()), "--out", str(out))
    assert code == 2
    captured = capsys.readouterr()
    assert "## Questions, as INPUT" in captured.out
    assert "## Answers, as OUTPUT" in captured.out
    assert out.exists()
    assert "Deleting the guardrail andur-topic-probe failed" in captured.err


def test_the_report_is_printed_before_a_bad_out_path_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bedrock, runtime = FakeBedrock(), FakeRuntime(blocked={"Sue?", "Klage?"})
    out = tmp_path / "missing" / "out.json"
    with pytest.raises(OSError):
        run_main(monkeypatch, tmp_path, bedrock, runtime, "--out", str(out))
    assert "| Legal action (de) | 1/1 | - |" in capsys.readouterr().out


def test_a_probe_that_is_still_creating_is_waited_for() -> None:
    bedrock = FakeBedrock(statuses=("CREATING", "CREATING", "READY"))
    pauses: list[float] = []
    topic_probe.wait_until_ready(bedrock, topic_probe.create_probe(bedrock), sleep=pauses.append)
    assert pauses == [topic_probe.READY_POLL_SECONDS] * 2


def test_a_probe_left_by_an_earlier_run_stops_the_run_before_anything_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Found on the second page of the listing, too."""
    bedrock = FakeBedrock(
        existing=[
            {"id": "answer1", "name": GUARDRAIL_NAME},
            {"id": "old1", "name": topic_probe.PROBE_NAME},
        ]
    )
    runtime = FakeRuntime(blocked=set())
    assert run_main(monkeypatch, tmp_path, bedrock, runtime) == 2
    assert "create_guardrail" not in bedrock.names()
    assert "delete_guardrail" not in bedrock.names()
    assert runtime.calls == []
    assert "old1" in capsys.readouterr().err


def test_only_a_guardrail_named_like_the_probe_is_ever_deleted() -> None:
    bedrock = FakeBedrock(existing=[{"id": "answer1", "name": GUARDRAIL_NAME}])
    with pytest.raises(AwsProblem, match="Refusing to delete"):
        topic_probe.delete_probe(bedrock, "answer1")
    assert bedrock.guardrails == {"answer1": GUARDRAIL_NAME}
    assert "delete_guardrail" not in bedrock.names()


def test_the_deployed_guardrail_is_measured_and_never_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The questions on the answer guardrail, the answers on the topic guardrail."""
    bedrock = FakeBedrock(existing=[{"id": "answer1", "name": GUARDRAIL_NAME}])

    def deployed(**kwargs: Any) -> dict[str, Any]:
        on_answer = (
            kwargs["guardrailIdentifier"] == "topics1" or ANSWER_GUARDRAIL_READS_ANSWER_TOPICS
        )
        topics = [
            {
                "name": name,
                "definition": definition,
                "examples": examples,
                "type": "DENY",
                "outputEnabled": on_answer,
            }
            for name, definition, examples in DENIED_TOPICS
        ]
        return {"topicPolicy": {"topics": topics, "tier": {"tierName": TOPICS_TIER}}}

    monkeypatch.setattr(bedrock, "get_guardrail", deployed)
    runtime = FakeRuntime(blocked={"Sue?", "Klage?"})
    environment = {
        "GUARDRAIL_ID": "answer1",
        "GUARDRAIL_VERSION": "4",
        "TOPIC_GUARDRAIL_ID": "topics1",
        "TOPIC_GUARDRAIL_VERSION": "5",
    }
    out = tmp_path / "out.json"
    code = run_main(
        monkeypatch,
        tmp_path,
        bedrock,
        runtime,
        "--deployed",
        "--out",
        str(out),
        environment=environment,
    )
    assert code == 0
    assert not {"create_guardrail", "delete_guardrail", "list_guardrails"} & set(bedrock.names())
    assert {
        (c["source"], c["guardrailIdentifier"], c["guardrailVersion"]) for c in runtime.calls
    } == {("INPUT", "answer1", "4"), ("OUTPUT", "topics1", "5")}
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert (saved["guardrail"], saved["version"], saved["topics_as_in_the_repository"]) == (
        "deployed",
        {"INPUT": "4", "OUTPUT": "5"},
        True,
    )


def test_a_function_from_before_the_topic_guardrail_is_named_and_nothing_is_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bedrock, runtime = FakeBedrock(), FakeRuntime(blocked=set())
    environment = {"GUARDRAIL_ID": "answer1", "GUARDRAIL_VERSION": "3"}
    code = run_main(monkeypatch, tmp_path, bedrock, runtime, "--deployed", environment=environment)
    assert code == 2
    assert runtime.calls == [] and bedrock.calls == []
    error = capsys.readouterr().err
    assert "TOPIC_GUARDRAIL_ID, TOPIC_GUARDRAIL_VERSION" in error
    assert "predates the topic guardrail" in error


def test_a_deployed_guardrail_with_other_topics_is_reported() -> None:
    bedrock = FakeBedrock()
    bedrock.get_guardrail = lambda **kwargs: {  # type: ignore[method-assign]
        "topicPolicy": {
            "topics": [{"name": "Legal advice", "definition": "old", "examples": []}],
            "tier": {"tierName": TOPICS_TIER},
        }
    }
    assert topic_probe.deployed_topics_match(bedrock, "answer1", "3", on_answer=False) is False


@pytest.mark.parametrize("on_answer", [True, False])
def test_a_deployed_guardrail_that_reads_the_wrong_sides_is_reported(
    on_answer: bool,
) -> None:
    """Version 3 of the answer guardrail, deployed on 02.10.2026, reads every topic on both
    sides; since the topic guardrail, the answer guardrail reads the question only."""
    bedrock = FakeBedrock()
    topics = [
        {"name": name, "definition": definition, "examples": examples, "outputEnabled": on_answer}
        for name, definition, examples in DENIED_TOPICS
    ]
    bedrock.get_guardrail = lambda **kwargs: {  # type: ignore[method-assign]
        "topicPolicy": {"topics": topics, "tier": {"tierName": TOPICS_TIER}}
    }
    assert topic_probe.deployed_topics_match(bedrock, "g1", "3", on_answer=on_answer) is True
    assert topic_probe.deployed_topics_match(bedrock, "g1", "3", on_answer=not on_answer) is False


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-2"])
def test_a_region_outside_the_eu_profile_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, region: str
) -> None:
    bedrock, runtime = FakeBedrock(), FakeRuntime(blocked=set())
    assert run_main(monkeypatch, tmp_path, bedrock, runtime, "--region", region) == 2
    assert bedrock.calls == [] and runtime.calls == []


def test_repeat_must_be_at_least_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tmp_path, FakeBedrock(), FakeRuntime(set()), "--repeat", "0")


# --- Counting ------------------------------------------------------------------------------


class FlakyRuntime:
    """Blocks a text in the runs listed for it, counted from 1."""

    def __init__(self, plan: dict[str, set[int]]):
        self.plan = plan
        self.seen: dict[str, int] = {}

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        text = kwargs["content"][0]["text"]["text"]
        self.seen[text] = self.seen.get(text, 0) + 1
        if self.seen[text] in self.plan.get(text, set()):
            topics = [{"name": "Legal action", "action": "BLOCKED"}]
            return {
                "action": "GUARDRAIL_INTERVENED",
                "assessments": [{"topicPolicy": {"topics": topics}}],
                "usage": {"topicPolicyUnits": 1},
            }
        return {"action": "NONE", "usage": {"topicPolicyUnits": 1}}


def test_a_question_is_right_only_if_it_is_right_in_every_run() -> None:
    """A refusal that passes once has slipped through; a harmless question blocked once is a
    false block — the visitor who got it saw a refusal."""
    runtime = FlakyRuntime({"Sue?": {1, 2, 3}, "Klage?": {1, 3}, "Garantie?": {2}})
    rows = topic_probe.measure(
        runtime, "g", "DRAFT", SAMPLE, lambda line: None, repeats=3, workers=1
    )
    summary = topic_probe.summarise(rows)
    assert summary["runs"] == 3
    assert summary["slips"]["Legal action (en)"] == {"blocked": 1, "questions": 1, "slipped": []}
    assert summary["slips"]["Legal action (de)"] == {
        "blocked": 0,
        "questions": 1,
        "slipped": ["r2"],
    }
    assert summary["harmless"] == {
        "en": {"blocked": 0, "questions": 1},
        "de": {"blocked": 1, "questions": 1},
    }
    assert summary["false_blocks"] == {"Legal action (de)": ["h1"]}
    assert (summary["slipped"], summary["false_blocked"]) == (1, 1)
    lines = topic_probe.report(summary, rows)
    assert "| r2 | blocked | 2/3 | Legal action | Klage? |" in lines
    assert "| h1 | answered | 1/3 | Legal action | Garantie? |" in lines


def test_a_refusal_under_another_topic_counts_as_blocked_and_is_named() -> None:
    rows = topic_probe.measure(
        FakeRuntime(blocked={"Klage?"}),
        "g",
        "DRAFT",
        [{**SAMPLE[1], "topic": "Tax returns"}],
        lambda line: None,
    )
    summary = topic_probe.summarise(rows)
    assert summary["slips"]["Tax returns (de)"]["blocked"] == 1
    assert summary["other_topic"] == ["r2"]


def test_the_cost_is_the_topic_units_at_the_stacks_price() -> None:
    rows = topic_probe.measure(
        FakeRuntime(blocked={"Sue?"}), "g", "DRAFT", SAMPLE, lambda line: None, repeats=2
    )
    summary = topic_probe.summarise(rows)
    assert summary["units"] == {"topicPolicyUnits": 8}
    assert Decimal(summary["cost_usd"]) == 8 * Decimal(PRICE_TOPIC_PER_1K_UNITS) / 1000


# --- Several calls at once -----------------------------------------------------------------

MANY = [
    {
        **SAMPLE[i % len(SAMPLE)],
        "id": f"t{i}",
        "question": f"{SAMPLE[i % len(SAMPLE)]['question']} {i}",
    }
    for i in range(20)
]
MANY_BLOCKED = {q["question"] for q in MANY if q["blocked"]}


def throttling() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "ApplyGuardrail"
    )


class SlowRuntime(FakeRuntime):
    """Answers the later texts sooner than the earlier ones, so that the responses come back in
    another order than the calls went out, and notes how many calls were open at once."""

    def __init__(self, blocked: set[str], fail_on: str | None = None):
        super().__init__(blocked, fail_on)
        self.open = 0
        self.peak = 0
        self.lock = threading.Lock()

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        text = kwargs["content"][0]["text"]["text"]
        last = text.rsplit(" ", 1)[-1]
        with self.lock:
            self.open += 1
            self.peak = max(self.peak, self.open)
        try:
            time.sleep(0.002 * (len(MANY) - int(last)) if last.isdigit() else 0.01)
            return super().apply_guardrail(**kwargs)
        finally:
            with self.lock:
                self.open -= 1


class ThrottlingEveryThird(FakeRuntime):
    """AWS throttles every third attempt, the repeated ones included."""

    def __init__(self, blocked: set[str]):
        super().__init__(blocked)
        self.attempts = 0
        self.lock = threading.Lock()

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        with self.lock:
            self.attempts += 1
            throttled = self.attempts % 3 == 0
        if throttled:
            raise throttling()
        return super().apply_guardrail(**kwargs)


def measured(runtime: Any, workers: int, **kwargs: Any) -> tuple[list[dict[str, Any]], list[str]]:
    lines: list[str] = []
    rows = topic_probe.measure(
        runtime, "g", "DRAFT", MANY, lines.append, repeats=3, workers=workers, **kwargs
    )
    return rows, lines


@pytest.mark.parametrize("workers", [2, 5, 16])
def test_several_workers_give_the_same_rows_and_lines_in_the_same_order(workers: int) -> None:
    """Only the time changes: rows, counts and progress lines are those of one call after the
    other, though the responses come back in another order."""
    alone_rows, alone_lines = measured(FakeRuntime(MANY_BLOCKED), 1)
    runtime = SlowRuntime(MANY_BLOCKED)
    rows, lines = measured(runtime, workers)
    assert runtime.peak > 1
    assert (rows, lines) == (alone_rows, alone_lines)
    assert len(runtime.calls) == len(MANY) * 3
    summary, alone_summary = topic_probe.summarise(rows), topic_probe.summarise(alone_rows)
    assert summary == alone_summary
    assert topic_probe.report(summary, rows) == topic_probe.report(alone_summary, alone_rows)


def test_one_worker_asks_one_call_after_the_other() -> None:
    runtime = SlowRuntime(MANY_BLOCKED)
    measured(runtime, 1)
    assert runtime.peak == 1


def test_measure_called_from_code_asks_several_calls_at_once_by_default() -> None:
    """A probe written in Python gets the speed of the command line without asking for it."""
    runtime = SlowRuntime(MANY_BLOCKED)
    topic_probe.measure(runtime, "g", "DRAFT", MANY, lambda line: None, repeats=3)
    assert runtime.peak > 1
    assert runtime.peak <= topic_probe.DEFAULT_WORKERS


@pytest.mark.parametrize("workers", [1, 4])
def test_a_throttled_call_is_repeated_and_counted_neither_as_an_error_nor_as_passed(
    workers: int,
) -> None:
    """The refusals stay refusals, every text has all its runs, and the units of the repeated
    calls are counted once."""
    clean_rows, clean_lines = measured(FakeRuntime(MANY_BLOCKED), 1)
    pauses: list[float] = []
    runtime = ThrottlingEveryThird(MANY_BLOCKED)
    rows, lines = measured(runtime, workers, sleep=pauses.append)
    assert rows == clean_rows
    assert all(row["runs"] == 3 for row in rows)
    assert pauses
    assert all(0 < pause <= topic_probe.THROTTLE_MAX_WAIT_SECONDS for pause in pauses)
    assert lines[:-1] == clean_lines
    assert lines[-1] == f"INPUT: AWS throttled {len(pauses)} calls; each was repeated after a wait"
    assert runtime.attempts == len(MANY) * 3 + len(pauses)


def test_the_wait_after_a_throttled_call_doubles_up_to_a_ceiling() -> None:
    pauses: list[float] = []

    class Throttled:
        def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
            if len(pauses) < 7:
                raise throttling()
            return {"action": "NONE"}

    row = {"id": "x1", "text": "Hello"}
    response, repeats = topic_probe.apply_guardrail(
        Throttled(), "g", "DRAFT", "INPUT", row, sleep=pauses.append
    )
    assert (response, repeats) == ({"action": "NONE"}, 7)
    for attempt, pause in enumerate(pauses):
        ceiling = min(topic_probe.THROTTLE_MAX_WAIT_SECONDS, 2.0**attempt)
        assert ceiling / 2 <= pause <= ceiling


@pytest.mark.parametrize("workers", [1, 4])
def test_a_call_that_stays_throttled_ends_the_run(workers: int) -> None:
    pauses: list[float] = []
    attempts: list[int] = []

    class AlwaysThrottled:
        def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
            attempts.append(1)
            raise throttling()

    with pytest.raises(AwsProblem, match="Applying the guardrail to .* failed"):
        topic_probe.measure(
            AlwaysThrottled(),
            "g",
            "DRAFT",
            SAMPLE[:1],
            lambda line: None,
            workers=workers,
            sleep=pauses.append,
        )
    assert len(attempts) == topic_probe.THROTTLE_ATTEMPTS
    assert len(pauses) == topic_probe.THROTTLE_ATTEMPTS - 1


def test_another_error_ends_a_run_of_one_worker_at_the_call_that_failed() -> None:
    runtime = FakeRuntime(blocked=set(), fail_on="Garantie?")
    lines: list[str] = []
    with pytest.raises(RuntimeError, match="network"):
        topic_probe.measure(runtime, "g", "DRAFT", SAMPLE, lines.append, repeats=3, workers=1)
    assert len(runtime.calls) == 3
    assert len(lines) == 2


def test_another_error_ends_a_run_of_several_workers_after_the_lines_before_it() -> None:
    """The lines are those of the calls listed before the failed one, no more; calls not yet
    started are not started."""
    texts = [{**SAMPLE[0], "id": f"t{i}", "question": f"Sue? {i}"} for i in range(60)]
    runtime = FakeRuntime(blocked=set(), fail_on="Sue? 5")
    lines: list[str] = []
    with pytest.raises(RuntimeError, match="network"):
        topic_probe.measure(runtime, "g", "DRAFT", texts, lines.append, workers=4)
    assert [line.split()[2] for line in lines] == [f"t{i}" for i in range(5)]
    assert len(runtime.calls) < len(texts)


def test_another_error_among_throttled_calls_still_ends_the_run() -> None:
    runtime = ThrottlingEveryThird(MANY_BLOCKED)
    runtime.fail_on = MANY[7]["question"]
    with pytest.raises(RuntimeError, match="network"):
        measured(runtime, 4, sleep=lambda seconds: None)


def test_a_main_run_gives_the_same_report_and_lines_for_one_and_for_several_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs = []
    for workers in ("1", "4"):
        out = tmp_path / f"out{workers}.json"
        runtime = SlowRuntime(blocked={"Sue?", "Klage?", "Sue [1]."})
        args = ("--workers", workers, "--out", str(out))
        assert run_main(monkeypatch, tmp_path, FakeBedrock(), runtime, *args) == 0
        captured = capsys.readouterr()
        lines = [line for line in captured.err.splitlines() if line.startswith("run ")]
        outputs.append((captured.out, lines, out.read_text(encoding="utf-8")))
    assert outputs[0] == outputs[1]
    assert len(outputs[0][1]) == (len(SAMPLE) + len(SAMPLE_ANSWERS)) * 3


def test_the_probe_is_deleted_when_the_measurement_fails_with_several_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bedrock, runtime = FakeBedrock(), FakeRuntime(blocked=set(), fail_on="Garantie?")
    with pytest.raises(RuntimeError, match="network"):
        run_main(monkeypatch, tmp_path, bedrock, runtime, "--workers", "4")
    assert bedrock.names()[-1] == "delete_guardrail"
    assert bedrock.guardrails == {}


def test_a_run_says_how_long_its_calls_took(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, tmp_path, FakeBedrock(), FakeRuntime(set()), "--workers", "3")
    assert f"{(len(SAMPLE) + len(SAMPLE_ANSWERS)) * 3} calls in " in capsys.readouterr().err


def test_workers_must_be_at_least_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tmp_path, FakeBedrock(), FakeRuntime(set()), "--workers", "0")
