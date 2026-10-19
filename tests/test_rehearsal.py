"""The rehearsal run: its plan, how it runs and times the steps, and its checks, without AWS."""

import http.client
import json
import socket
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.page import SECURITY_HEADERS
from scripts import ingest, layout, rehearsal, rehearsal_checks, rehearsal_runner
from scripts.rehearsal_runner import Context, Result, Runner, Step, StepFailed
from tests.test_ingest import sidecar_for

REGION = "eu-central-1"


def arguments(tmp_path: Path, *extra: str) -> Any:
    return rehearsal.parse_args(
        [
            "--workspace",
            "kalvenstett",
            "--source",
            str(tmp_path / "kunden"),
            "--question",
            "Was kostet eine Stunde?",
            "--out-dir",
            str(tmp_path / "out"),
            *extra,
        ]
    )


def context(tmp_path: Path, clients: dict[str, Any] | None = None, **outputs: str) -> Context:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return Context(
        region=REGION,
        stack_name="AndurStack",
        workspace="kalvenstett",
        other_workspace="quillmoor",
        out_dir=tmp_path,
        expect_role=None,
        clients=lambda service: (clients or {})[service],
        outputs={"WebUrl": "https://abc.lambda-url.eu-central-1.on.aws/", **outputs},
    )


# --- The plan ---------------------------------------------------------------------------


@pytest.fixture
def plan(tmp_path: Path) -> list[Step]:
    return rehearsal.build_plan(arguments(tmp_path, "--plan", "betrieb"))


def by_id(plan: list[Step]) -> dict[str, Step]:
    return {step.id: step for step in plan}


def test_the_plan_runs_the_setup_in_the_order_a_new_business_needs(plan: list[Step]) -> None:
    ids = [step.id for step in plan]
    assert len(ids) == len(set(ids))
    assert ids[:5] == ["identity", "stack", "deploy-time", "address", "https-only"]
    order = ["documents-check", "key", "documents", "search", "first-question"]
    assert [i for i in ids if i in order] == order
    assert ids.index("demo-documents") < ids.index("search-apart")
    assert ids.index("demo-documents") < ids.index("topics")
    assert ids[-2:] == ["residency", "function-log"]


def test_the_plan_deploys_deletes_and_removes_nothing(plan: list[Step]) -> None:
    for step in plan:
        words = set(step.command or ())
        for forbidden in ("cdk", "deploy", "destroy", "bootstrap", "--delete", "remove", "--yes"):
            assert forbidden not in words, (step.id, forbidden)


def test_every_command_runs_in_the_region_and_only_frankfurt_can_be_it(plan: list[Step]) -> None:
    for step in plan:
        if step.command and step.id != "documents-check":
            command = step.command
            assert command[command.index("--region") + 1] == REGION, step.id
            assert command.count("--region") == 1, step.id
    sources = by_id(plan)["sources"].command or ()
    assert sources[sources.index("--retrieve-region") + 1] == REGION
    # Behind CloudFront the address is worldwide: the address checks could never pass.
    assert rehearsal.RUN_REGIONS == (REGION,)
    with pytest.raises(SystemExit):
        rehearsal.parse_args(["--workspace", "k", "--source", "s", "--question", "q",
                              "--out-dir", "o", "--region", "eu-west-1"])  # fmt: skip


def test_the_key_is_never_captured_and_the_plan_reaches_it(plan: list[Step]) -> None:
    steps = by_id(plan)
    assert steps["key"].secret_stdout
    assert steps["key"].command is not None
    assert steps["key"].command[-2:] == ("--plan", "betrieb")
    assert [s.id for s in plan if s.secret_stdout] == ["key"]
    assert steps["key-apart"].environment is rehearsal_checks.workspace_key_environment
    assert steps["key-apart"].exit_code == 1


def test_the_other_workspace_must_differ_from_the_new_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        arguments(tmp_path, "--other-workspace", "kalvenstett")
    assert stopped.value.code == 2
    assert "--other-workspace must differ from --workspace" in capsys.readouterr().err


def test_the_steps_by_hand_are_marked_as_such(plan: list[Step]) -> None:
    manual = [step for step in plan if step.manual]
    assert [step.id for step in manual] == ["hand-over", "page"]
    assert all(step.kind == "manual" and step.command is None for step in manual)
    assert "| clip" in (manual[0].manual or "")


def test_the_key_command_is_named_for_powershell_and_for_git_bash(plan: list[Step]) -> None:
    """Git Bash rewrites /andur/... into a Windows path unless MSYS_NO_PATHCONV=1 is set."""
    text = by_id(plan)["hand-over"].manual or ""
    command = text.split("PowerShell: ")[1].split("  --  ")[0]
    assert command.startswith("aws ssm get-parameter --name /andur/api-keys/")
    assert text.endswith(f"in Git Bash: MSYS_NO_PATHCONV=1 {command}")
    assert text.count("MSYS_NO_PATHCONV=1") == 1


def test_only_measurements_on_different_models_run_at_once(plan: list[Step]) -> None:
    together = [step for step in plan if step.group]
    assert {step.id: step.lane for step in together} == {
        "topics": "topic model",
        "tools": "answer model",
        "grounding": "answer model",
    }
    ids = [step.id for step in plan]
    assert ids.index("grounding") - ids.index("topics") == 2


def test_the_measurements_ask_the_deployed_answer_model(plan: list[Step]) -> None:
    for step_id in ("tools", "answers-en", "answers-de", "answers-de-de", "sources"):
        command = by_id(plan)[step_id].command or ()
        assert command[command.index("--model") + 1] == "Qwen3 235B A22B 2507"
    topics = by_id(plan)["topics"].command or ()
    assert "--deployed" in topics


def test_the_german_measurements_follow_the_workspace(plan: list[Step]) -> None:
    steps = by_id(plan)
    de_de = steps["answers-de-de"].command or ()
    assert de_de[de_de.index("--questions") + 1] == "scripts/evaluation-questions-kalvenstett.json"
    sources = steps["sources"].command or ()
    assert sources[sources.index("--workspace") + 1] == "kalvenstett"
    assert [step.id for step in plan if step.skipped] == []


def new_business(tmp_path: Path, workspace: str, *extra: str) -> list[str]:
    return [
        "--workspace",
        workspace,
        "--source",
        "x",
        "--question",
        "q",
        "--out-dir",
        str(tmp_path / "out"),
        *extra,
    ]


def test_a_workspace_without_german_questions_skips_them_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Measuring another business's questions instead would pass for the new one."""
    steps = by_id(rehearsal.build_plan(rehearsal.parse_args(new_business(tmp_path, "neufirma"))))
    assert steps["answers-de-de"].skipped == "no scripts/evaluation-questions-neufirma.json"
    assert steps["sources"].skipped == (
        "no German questions of neufirma in evaluation-questions-control.json"
    )
    assert steps["answers-de-de"].command is None and steps["sources"].command is None
    run = FakeRun()
    chosen = numbered(steps["answers-en"], steps["answers-de-de"], steps["sources"])
    results = runner(context(tmp_path), run).run_all(chosen, lambda r: None)
    assert [(r.id, r.status) for r in results] == [
        ("answers-en", "passed"),
        ("answers-de-de", "skipped"),
        ("sources", "skipped"),
    ]
    assert len(run.calls) == 1
    lines = rehearsal_runner.summary_lines(results, 1.0)
    (row,) = [line for line in lines if line.startswith("| 2 | answers-de-de |")]
    assert row.startswith("| 2 | answers-de-de | command | skipped |")
    assert row.endswith("| no scripts/evaluation-questions-neufirma.json |")
    assert lines[-1] == "Skipped, not measured: answers-de-de, sources."
    assert rehearsal.main(new_business(tmp_path, "neufirma", "--list")) == 0
    assert "skipped: no scripts/evaluation-questions-neufirma.json" in capsys.readouterr().out


@pytest.mark.parametrize("workspace", ["de", "control"])
def test_a_workspace_named_like_another_question_set_is_refused(
    tmp_path: Path, workspace: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rehearsal.main(new_business(tmp_path, workspace, "--list")) == 2
    assert f"is not only {workspace}'s German questions" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_a_measurement_fails_on_a_failed_call_and_passes_with_wrong_answers(
    plan: list[Step], tmp_path: Path
) -> None:
    """evaluate and control_eval end with 2 after a failed call: there was no measurement. A
    wrong answer is a result, and they end with 0."""
    for step_id in ("answers-en", "answers-de", "answers-de-de", "sources"):
        step = by_id(plan)[step_id]
        failed = FakeRun(codes={sys.executable: 2}, output="error: 1 failed call(s)")
        (result,) = runner(context(tmp_path), failed).run_all(numbered(step), lambda r: None)
        assert (result.status, result.exit_code) == ("failed", 2), step_id
        wrong = FakeRun(output="| Qwen3 235B A22B 2507 | 0 of 30 right |")
        (result,) = runner(context(tmp_path), wrong).run_all(numbered(step), lambda r: None)
        assert result.status == "passed", step_id


def test_list_prints_the_plan_and_calls_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "--workspace",
        "kalvenstett",
        "--source",
        "x",
        "--question",
        "q",
        "--out-dir",
        str(tmp_path / "out"),
        "--list",
    ]
    assert rehearsal.main(argv) == 0
    out = capsys.readouterr().out
    assert "hand-over" in out and "[at once: topic model]" in out
    assert not (tmp_path / "out").exists()


def test_ctrl_c_still_leaves_a_summary_and_no_account_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    account = "123456789012"
    arn = f"arn:aws:sts::{account}:assumed-role/OrganizationAccountAccessRole/s"
    sts = Fake(get_caller_identity={"Account": account, "Arn": arn})
    monkeypatch.setattr(rehearsal, "client", lambda service, region: sts)

    def interrupted(ctx: Context) -> str:
        (ctx.out_dir / "02-stack.log").write_text(f"arn:aws:iam::{account}:x", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(rehearsal, "check_stack", interrupted)
    out = tmp_path / "out"
    argv = ["--workspace", "k", "--source", "s", "--question", "q", "--out-dir", str(out)]
    with pytest.raises(KeyboardInterrupt):
        rehearsal.main(argv)
    (summary,) = out.glob("run-*.md")
    text = summary.read_text(encoding="utf-8")
    assert "| 1 | identity | check | passed |" in text
    assert "Stopped during the step after the last one listed." in text
    assert (out / "02-stack.log").read_text(encoding="utf-8") == "arn:aws:iam::<account>:x"
    assert account not in capsys.readouterr().out


def test_an_out_dir_in_the_repository_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--workspace", "k", "--source", "x", "--question", "q"]
    assert rehearsal.main([*argv, "--out-dir", str(rehearsal_runner.REPOSITORY / "out")]) == 2
    assert "outside the repository" in capsys.readouterr().err


# --- Choosing steps ---------------------------------------------------------------------


def test_from_and_only_keep_the_identity_first(plan: list[Step]) -> None:
    started = rehearsal_runner.select(plan, "topics", None)
    assert [s.id for _, s in started[:3]] == ["identity", "topics", "tools"]
    assert started[1][0] == [s.id for s in plan].index("topics") + 1
    only = rehearsal_runner.select(plan, None, ["page", "residency"])
    assert [s.id for _, s in only] == ["identity", "page", "residency"]
    assert [s.id for _, s in rehearsal_runner.select(plan, None, None)] == [s.id for s in plan]
    with pytest.raises(ValueError, match="no step 'nope'"):
        rehearsal_runner.select(plan, "nope", None)


# --- Running ----------------------------------------------------------------------------


class FakeRun:
    def __init__(self, codes: dict[str, int] | None = None, output: str = "ok") -> None:
        self.codes = codes or {}
        self.output = output
        self.calls: list[tuple[tuple[str, ...], bool, dict[str, str]]] = []

    def __call__(self, argv: Any, log: Path, secret: bool, env: Any) -> tuple[int, str]:
        self.calls.append((tuple(argv), secret, dict(env)))
        return self.codes.get(argv[0], 0), self.output


def ticking_clock(step: float = 2.0) -> Any:
    times = iter(x * step for x in range(10_000))
    return lambda: next(times)


def runner(ctx: Context, run: Any, **kwargs: Any) -> Runner:
    return Runner(ctx, run=run, clock=ticking_clock(), say=lambda line: None, **kwargs)


def numbered(*steps: Step) -> list[tuple[int, Step]]:
    return list(enumerate(steps, start=1))


def test_a_command_is_timed_and_its_output_read(tmp_path: Path) -> None:
    run = FakeRun(output="Answer (kalvenstett):\n\nx\n\nLayers cited: human\n")
    step = Step("q", "setup", "Q", command=("ask",), reads=rehearsal.answered)
    (result,) = runner(context(tmp_path), run).run_all(numbered(step), lambda r: None)
    assert (result.status, result.seconds, result.exit_code) == ("passed", 2.0, 0)
    assert result.summary == "answered; layers cited: human"
    assert result.log == "01-q.log"


def test_the_run_stops_at_the_first_failure_and_records_each_step(tmp_path: Path) -> None:
    run = FakeRun(codes={"bad": 2})
    steps = numbered(
        Step("a", "p", "A", command=("good",)),
        Step("b", "p", "B", command=("bad",)),
        Step("c", "p", "C", command=("good",)),
    )
    recorded: list[list[str]] = []
    results = runner(context(tmp_path), run).run_all(
        steps, lambda r: recorded.append([x.id for x in r])
    )
    assert [(r.id, r.status) for r in results] == [("a", "passed"), ("b", "failed")]
    assert "exit code 2, not 0" in results[1].summary
    assert recorded == [["a"], ["a", "b"]]
    assert len(run.calls) == 2


def test_an_expected_refusal_passes_and_its_environment_is_handed_on(tmp_path: Path) -> None:
    run = FakeRun(codes={"ask": 1}, output="error: HTTP 401: wrong key")
    step = Step(
        "k",
        "apart",
        "K",
        command=("ask",),
        exit_code=1,
        reads=rehearsal.refused_with_401,
        environment=lambda ctx: {"ANDUR_API_KEY": "secret"},
    )
    (result,) = runner(context(tmp_path), run).run_all(numbered(step), lambda r: None)
    assert result.status == "passed"
    assert run.calls == [(("ask",), False, {"ANDUR_API_KEY": "secret"})]


def test_a_failing_check_or_reader_fails_the_step(tmp_path: Path) -> None:
    def broken(ctx: Context) -> str:
        raise StepFailed("nope")

    steps = numbered(Step("c", "p", "C", check=broken))
    (result,) = runner(context(tmp_path), FakeRun()).run_all(steps, lambda r: None)
    assert (result.status, result.summary) == ("failed", "nope")


def test_any_error_in_a_step_fails_it_and_the_screen_never_shows_the_account(
    tmp_path: Path,
) -> None:
    def unexpected(ctx: Context) -> str:
        raise ConnectionResetError("arn:aws:iam::123456789012:role/x went away")

    ctx = context(tmp_path)
    ctx.account = "123456789012"
    shown: list[str] = []
    clocked = Runner(ctx, run=FakeRun(), clock=ticking_clock(), say=shown.append)
    steps = numbered(Step("c", "p", "C", check=unexpected), Step("d", "p", "D", command=("x",)))
    results = clocked.run_all(steps, lambda r: None)
    assert [(r.id, r.status) for r in results] == [("c", "failed")]
    assert results[0].summary.startswith("stopped by ConnectionResetError")
    assert clocked.results == results
    assert not any("123456789012" in line for line in shown)
    assert "<account>" in shown[-1]
    assert clocked.clean("a 123456789012 b") == "a <account> b"


def test_a_key_in_the_shell_is_not_handed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANDUR_API_KEY", "from-the-shell")
    code = "import os; print(os.environ.get('ANDUR_API_KEY', 'none'))"
    log = tmp_path / "env.log"
    assert (
        rehearsal_runner.run_command((sys.executable, "-c", code), log, False, {})[1].strip()
        == "none"
    )
    given = {"ANDUR_API_KEY": "the-step's"}
    assert rehearsal_runner.run_command((sys.executable, "-c", code), log, False, given)[
        1
    ].strip() == ("the-step's")


def test_steps_by_hand_are_left_open_or_timed_while_waiting(tmp_path: Path) -> None:
    step = Step("hand", "setup", "H", manual="do it")
    skipped = runner(context(tmp_path), FakeRun()).step(1, step)
    assert (skipped.status, skipped.summary) == ("open", "left open")
    answers = iter(["", "skip"])
    waiting = runner(
        context(tmp_path), FakeRun(), manual_mode="wait", ask_person=lambda p: next(answers)
    )
    assert waiting.step(1, step).status == "done"
    assert waiting.step(1, step).status == "open"


def test_a_group_runs_its_lanes_at_once_and_reports_in_plan_order(tmp_path: Path) -> None:
    # Each lane's first step waits for the other lane's: run one after the other, they never
    # meet and the barrier breaks.
    barrier = threading.Barrier(2, timeout=5)
    firsts = {"topics", "tools"}

    def run(argv: Any, log: Path, secret: bool, env: Any) -> tuple[int, str]:
        if argv[0] in firsts:
            barrier.wait()
        return 0, ""

    steps = numbered(
        Step("topics", "m", "T", command=("topics",), group="g", lane="a"),
        Step("tools", "m", "U", command=("tools",), group="g", lane="b"),
        Step("grounding", "m", "G", command=("grounding",), group="g", lane="b"),
        Step("after", "m", "A", command=("after",)),
    )
    clocked = Runner(context(tmp_path), run=run, clock=lambda: 0.0, say=lambda line: None)
    results = clocked.run_all(steps, lambda r: None)
    assert [r.id for r in results] == ["topics", "tools", "grounding", "after"]
    assert all(r.status == "passed" for r in results)
    assert [r.group for r in results] == ["g", "g", "g", None]


def test_a_failure_in_one_lane_stops_the_run_after_the_group(tmp_path: Path) -> None:
    steps = numbered(
        Step("topics", "m", "T", command=("bad",), group="g", lane="a"),
        Step("tools", "m", "U", command=("good",), group="g", lane="b"),
        Step("after", "m", "A", command=("good",)),
    )
    results = runner(context(tmp_path), FakeRun(codes={"bad": 2})).run_all(steps, lambda r: None)
    assert [(r.id, r.status) for r in results] == [("topics", "failed"), ("tools", "passed")]


def test_a_secret_stdout_never_reaches_the_log(tmp_path: Path) -> None:
    # The key is put together at run time, so the logged command line does not hold it.
    code = "import sys; print('THE' + '-KEY'); print('stored', file=sys.stderr)"
    log = tmp_path / "key.log"
    returned, output = rehearsal_runner.run_command((sys.executable, "-c", code), log, True, {})
    assert returned == 0
    assert "THE-KEY" not in output and "THE-KEY" not in log.read_text(encoding="utf-8")
    assert "stored" in log.read_text(encoding="utf-8")
    _, shown = rehearsal_runner.run_command((sys.executable, "-c", code), log, False, {})
    assert "THE-KEY" in shown


def test_the_summary_adds_up_the_phases(tmp_path: Path) -> None:
    results = [
        Result(1, "a", "check", "A", "check", "passed", "t", 5.0, "fine"),
        Result(2, "b", "setup", "B", "manual", "open", "t", 0.0, "left open"),
        Result(3, "c", "setup", "C", "command", "passed", "t", 75.0, "x | y", group="g"),
    ]
    lines = rehearsal_runner.summary_lines(results, 90.0)
    assert "| 3 | c | command | passed | 1 min 15 s (at once) | x \\| y |" in lines
    assert "| setup | 2 | 1 min 15 s |" in lines
    assert "Wall clock of the run: 1 min 30 s." in lines
    assert lines[-1] == "By hand and still open: b."


def test_the_account_id_is_replaced_in_every_file(tmp_path: Path) -> None:
    (tmp_path / "a.log").write_text("arn:aws:iam::123456789012:role/x", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "c.bin").write_bytes(b"\xff\xfe\x00123456789012")
    assert rehearsal_runner.scrub(tmp_path, "123456789012") == 1
    assert (tmp_path / "a.log").read_text(encoding="utf-8") == "arn:aws:iam::<account>:role/x"
    assert rehearsal_runner.scrub(tmp_path, None) == 0


# --- The checks -------------------------------------------------------------------------


class Fake:
    """A client whose methods return what they are given, by name."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name not in self.answers:
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            answer = self.answers[name]
            return answer(*args, **kwargs) if callable(answer) else answer

        return call


def test_the_identity_names_the_role_and_keeps_the_account(tmp_path: Path) -> None:
    arn = "arn:aws:sts::123456789012:assumed-role/OrganizationAccountAccessRole/botocore-1"
    sts = Fake(get_caller_identity={"Account": "123456789012", "Arn": arn})
    ctx = context(tmp_path, {"sts": sts})
    assert rehearsal_checks.check_identity(ctx) == "signed in as OrganizationAccountAccessRole"
    assert ctx.account == "123456789012"
    ctx.expect_role = "OrganizationAccountAccessRole"
    rehearsal_checks.check_identity(ctx)
    user = "arn:aws:iam::123456789012:user/andur-dev"
    ctx.clients = lambda s: Fake(get_caller_identity={"Account": "1", "Arn": user})
    with pytest.raises(StepFailed, match="signed in as user/andur-dev"):
        rehearsal_checks.check_identity(ctx)


def stack(status: str, *outputs: str) -> dict[str, Any]:
    return {
        "Stacks": [
            {
                "StackStatus": status,
                "Outputs": [{"OutputKey": k, "OutputValue": "v"} for k in outputs],
            }
        ]
    }


def test_the_stack_must_be_finished_and_have_its_outputs(tmp_path: Path) -> None:
    ctx = context(
        tmp_path,
        {
            "cloudformation": Fake(
                describe_stacks=stack("CREATE_COMPLETE", *rehearsal_checks.NEEDED_OUTPUTS)
            )
        },
    )
    assert rehearsal_checks.check_stack(ctx) == f"AndurStack CREATE_COMPLETE in {REGION}"
    ctx.clients = lambda s: Fake(describe_stacks=stack("UPDATE_ROLLBACK_COMPLETE", "WebUrl"))
    with pytest.raises(StepFailed, match="not deployed and finished"):
        rehearsal_checks.check_stack(ctx)
    ctx.clients = lambda s: Fake(describe_stacks=stack("UPDATE_COMPLETE", "WebUrl"))
    with pytest.raises(StepFailed, match="no output ApiKeyParameterPath"):
        rehearsal_checks.check_stack(ctx)


T0 = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)


def event(logical: str, status: str, minute: float) -> dict[str, Any]:
    return {
        "LogicalResourceId": logical,
        "ResourceStatus": status,
        "Timestamp": T0 + timedelta(minutes=minute),
    }


def test_the_deploy_is_timed_from_the_stacks_own_events() -> None:
    newest_first = [
        event("AndurStack", "UPDATE_COMPLETE", 30),
        event("AndurStack", "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", 29),
        event("Bucket", "UPDATE_COMPLETE", 26),
        event("AndurStack", "UPDATE_IN_PROGRESS", 25),
        event("AndurStack", "CREATE_COMPLETE", 10),
        event("AndurStack", "CREATE_IN_PROGRESS", 0),
    ]
    later = T0 + timedelta(minutes=30)
    assert rehearsal_checks.last_deploy(newest_first, "AndurStack") == ("update", 300.0, later)
    created = T0 + timedelta(minutes=10)
    assert rehearsal_checks.last_deploy(newest_first[4:], "AndurStack") == (
        "create",
        600.0,
        created,
    )
    assert (
        rehearsal_checks.last_deploy([event("AndurStack", "CREATE_IN_PROGRESS", 0)], "AndurStack")
        is None
    )


def test_the_deploy_time_reads_both_stacks(tmp_path: Path) -> None:
    class Paginator:
        def paginate(self, StackName: str) -> list[dict[str, Any]]:
            return [
                {
                    "StackEvents": [
                        event(StackName, "CREATE_COMPLETE", 2),
                        event(StackName, "CREATE_IN_PROGRESS", 0),
                    ]
                }
            ]

    cloudformation = Fake(get_paginator=lambda name: Paginator())
    ctx = context(tmp_path, {"cloudformation": cloudformation})
    finished = (T0 + timedelta(minutes=2)).astimezone().strftime("%d.%m.%Y %H:%M")
    assert rehearsal_checks.check_deploy_time(ctx) == (
        f"CDKToolkit create 2 min 00 s, finished {finished}; "
        f"AndurStack create 2 min 00 s, finished {finished}"
    )


RANGES = {
    "prefixes": [
        {"ip_prefix": "18.192.0.0/15", "region": "eu-central-1"},
        {"ip_prefix": "52.95.0.0/16", "region": "eu-west-1"},
    ],
    "ipv6_prefixes": [{"ipv6_prefix": "2a05:d014::/32", "region": "eu-central-1"}],
}


def test_every_address_must_lie_in_the_region_only(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    good = ["18.192.39.89", "2a05:d014:e7d:8601::1"]
    summary = rehearsal_checks.check_address_region(ctx, lambda host: good, lambda: RANGES)
    assert summary == f"2 address(es), each in {REGION} only"
    for addresses in (["18.192.39.89", "52.95.1.1"], ["8.8.8.8"]):
        with pytest.raises(StepFailed, match="not only in"):
            rehearsal_checks.check_address_region(ctx, lambda host, a=addresses: a, lambda: RANGES)
    with pytest.raises(StepFailed, match="resolves to no address"):
        rehearsal_checks.check_address_region(ctx, lambda host: [], lambda: RANGES)


def sender(
    plain: Any, page: tuple[int, dict[str, str], bytes], ask: tuple[int, dict[str, str]]
) -> Any:
    """`plain` is what port 80 gives every address, or a dict of it by address."""
    calls = []
    lock = threading.Lock()

    def send(
        scheme: str,
        host: str,
        method: str,
        path: str,
        body: bytes | None = None,
        address: str | None = None,
    ) -> Any:
        with lock:
            calls.append((scheme, host, method, path, body, address))
        if scheme == "http":
            answer = plain[address] if isinstance(plain, dict) else plain
            if isinstance(answer, Exception):
                raise answer
            return answer
        if method == "GET":
            return page
        return (*ask, b"{}")

    send.calls = calls  # type: ignore[attr-defined]
    return send


PAGE = (200, dict(SECURITY_HEADERS), b"<!doctype html><html></html>")
HOST = "abc.lambda-url.eu-central-1.on.aws"
TWO = ["18.192.39.89", "2a05:d014::1"]


def test_https_only_passes_when_port_80_is_closed_and_https_holds(tmp_path: Path) -> None:
    send = sender(TimeoutError("timed out"), PAGE, (401, dict(SECURITY_HEADERS)))
    summary = rehearsal_checks.check_https_only(context(tmp_path), send, lambda host: TWO)
    assert summary == (
        "http: 2 of 2 address(es) without a connection (2 timed out, which a block on this side "
        "would look like too); https: page 200, no key 401, security headers on both"
    )
    plain = sorted(c for c in send.calls if c[0] == "http")
    assert plain == [("http", HOST, "GET", "/", None, a) for a in TWO]
    question = [c for c in send.calls if c[2] == "POST"]
    assert question == [
        ("https", HOST, "POST", "/ask", b'{"question": "Ping?", "workspace": "kalvenstett"}', None)
    ]
    mixed = {TWO[0]: (308, {"Location": "https://abc/"}, b""), TWO[1]: ConnectionRefusedError()}
    redirect = sender(mixed, PAGE, (401, dict(SECURITY_HEADERS)))
    summary = rehearsal_checks.check_https_only(context(tmp_path), redirect, lambda host: TWO)
    assert summary.startswith("http: 1 of 2 address(es) without a connection, sent on: 308 to")
    refused = sender(ConnectionRefusedError(), PAGE, (401, dict(SECURITY_HEADERS)))
    summary = rehearsal_checks.check_https_only(context(tmp_path), refused, lambda host: TWO)
    assert summary.startswith("http: 2 of 2 address(es) without a connection; https:")


def test_an_address_without_a_route_from_here_is_named_as_not_checked(tmp_path: Path) -> None:
    unreachable = {TWO[0]: TimeoutError(), TWO[1]: OSError(10051, "network unreachable")}
    send = sender(unreachable, PAGE, (401, dict(SECURITY_HEADERS)))
    summary = rehearsal_checks.check_https_only(context(tmp_path), send, lambda host: TWO)
    assert summary.startswith(
        "http: 1 of 2 address(es) without a connection (1 timed out, which a block on this side "
        "would look like too), 1 not checked (no route from here);"
    )
    garbled = {TWO[0]: TimeoutError(), TWO[1]: http.client.BadStatusLine("SSH-2.0")}
    with pytest.raises(StepFailed, match="answers, not as HTTP"):
        rehearsal_checks.check_https_only(
            context(tmp_path), sender(garbled, PAGE, (401, {})), lambda h: TWO
        )


def test_no_address_checked_on_port_80_fails(tmp_path: Path) -> None:
    """Without a route to any address, nothing shows that port 80 is closed."""
    unreachable = {TWO[0]: OSError(10051, "network unreachable"), TWO[1]: OSError()}
    send = sender(unreachable, PAGE, (401, dict(SECURITY_HEADERS)))
    with pytest.raises(StepFailed, match="could not be checked on any of the 2 address"):
        rehearsal_checks.check_https_only(context(tmp_path), send, lambda host: TWO)
    assert [c for c in send.calls if c[0] == "https"] == []


def test_one_open_address_on_port_80_fails(tmp_path: Path) -> None:
    mixed = {TWO[0]: OSError(), TWO[1]: (200, {}, b"<html>")}
    send = sender(mixed, PAGE, (401, dict(SECURITY_HEADERS)))
    with pytest.raises(StepFailed, match="plain http on 2a05:d014::1 answers 200"):
        rehearsal_checks.check_https_only(context(tmp_path), send, lambda host: TWO)
    with pytest.raises(StepFailed, match="resolves to no address"):
        rehearsal_checks.check_https_only(context(tmp_path), send, lambda host: [])


@pytest.mark.parametrize(
    "ended",
    [
        lambda: http.client.RemoteDisconnected("Remote end closed connection without response"),
        lambda: ConnectionResetError(10054, "reset by peer"),
        lambda: rehearsal_checks.AcceptedWithoutAnswer("TimeoutError: timed out"),
    ],
    ids=["closed", "reset", "silent"],
)
def test_port_80_that_takes_the_connection_and_ends_it_unanswered_is_open(
    tmp_path: Path, ended: Any
) -> None:
    """The request had arrived, and a client's key would have with it: only a refusal, or a
    timeout before the connection is taken, counts as closed."""
    mixed = {TWO[0]: ConnectionRefusedError(), TWO[1]: ended()}
    send = sender(mixed, PAGE, (401, dict(SECURITY_HEADERS)))
    with pytest.raises(
        StepFailed, match="plain http on 2a05:d014::1 takes the connection and ends it without"
    ):
        rehearsal_checks.check_https_only(context(tmp_path), send, lambda host: TWO)
    assert [c for c in send.calls if c[0] == "https"] == []


@pytest.fixture
def local_port_80(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Plain HTTP of `http_request` sent to a port on this computer instead of port 80."""

    def to(port: int) -> None:
        class Local(http.client.HTTPConnection):
            def __init__(self, host: str, _port: int | None = None, **options: Any) -> None:
                super().__init__("127.0.0.1", port, **options)

        monkeypatch.setattr(http.client, "HTTPConnection", Local)

    return to


def serve_once(behaviour: str) -> tuple[int, threading.Event]:
    """A port that takes one connection, reads the request and then closes it unanswered
    ("close") or stays silent until the returned event is set ("silent")."""
    listener = socket.create_server(("127.0.0.1", 0))
    done = threading.Event()

    def run() -> None:
        with listener:
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                if behaviour == "silent":
                    done.wait(10)

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1], done


@pytest.mark.parametrize("behaviour", ["close", "silent"])
def test_plain_http_names_a_connection_taken_and_left_unanswered(
    behaviour: str, local_port_80: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rehearsal_checks, "HTTP_TIMEOUT_SECONDS", 0.5)
    port, done = serve_once(behaviour)
    local_port_80(port)
    try:
        with pytest.raises(rehearsal_checks.AcceptedWithoutAnswer):
            rehearsal_checks.http_request("http", HOST, "GET", "/", address="127.0.0.1")
    finally:
        done.set()


@pytest.mark.parametrize("broken", [ConnectionAbortedError, BrokenPipeError])
def test_plain_http_names_a_connection_taken_and_broken_off(
    broken: type[OSError], local_port_80: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any break once the connection is taken, not only a reset, means it was taken."""
    port, done = serve_once("silent")
    local_port_80(port)

    def request(*_: Any, **__: Any) -> None:
        raise broken()

    monkeypatch.setattr(http.client.HTTPConnection, "request", request)
    try:
        with pytest.raises(rehearsal_checks.AcceptedWithoutAnswer, match=broken.__name__):
            rehearsal_checks.http_request("http", HOST, "GET", "/", address="127.0.0.1")
    finally:
        done.set()


def test_plain_http_to_a_closed_port_is_refused_as_it_is(local_port_80: Any) -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
    local_port_80(port)
    with pytest.raises(ConnectionRefusedError):
        rehearsal_checks.http_request("http", HOST, "GET", "/", address="127.0.0.1")


@pytest.mark.parametrize(
    ("plain", "page", "ask", "message"),
    [
        ((200, {}, b"<html>"), PAGE, (401, SECURITY_HEADERS), "plain http on .* answers 200"),
        (
            (302, {"Location": "http://x/"}, b""),
            PAGE,
            (401, SECURITY_HEADERS),
            "plain http on .* answers 302",
        ),
        (
            TimeoutError(),
            (403, dict(SECURITY_HEADERS), b"<html>"),
            (401, SECURITY_HEADERS),
            "gave 403",
        ),
        (
            TimeoutError(),
            (200, dict(SECURITY_HEADERS), b"{}"),
            (401, SECURITY_HEADERS),
            "gave 200, not the page",
        ),
        (TimeoutError(), (200, {}, b"<html>"), (401, SECURITY_HEADERS), "the page lacks"),
        (TimeoutError(), PAGE, (200, SECURITY_HEADERS), "gave 200, not 401"),
        (TimeoutError(), PAGE, (401, {}), "the 401 lacks"),
    ],
)
def test_https_only_fails_on_every_gap(
    tmp_path: Path, plain: Any, page: Any, ask: Any, message: str
) -> None:
    with pytest.raises(StepFailed, match=message):
        send = sender(plain, page, (ask[0], dict(ask[1])))
        rehearsal_checks.check_https_only(context(tmp_path), send, lambda host: TWO)


def test_a_header_with_another_value_counts_as_missing() -> None:
    headers = {
        **{k.lower(): v for k, v in SECURITY_HEADERS.items()},
        "x-frame-options": "SAMEORIGIN",
    }
    assert rehearsal_checks.missing_headers(headers) == ["X-Frame-Options"]


def test_the_key_for_another_workspace_comes_from_parameter_store(tmp_path: Path) -> None:
    ssm = Fake(get_parameter={"Parameter": {"Value": "k3y"}})
    ctx = context(tmp_path, {"ssm": ssm}, ApiKeyParameterPath="/andur/api-keys")
    assert rehearsal_checks.workspace_key_environment(ctx) == {"ANDUR_API_KEY": "k3y"}
    assert ssm.calls == [
        ("get_parameter", {"Name": "/andur/api-keys/kalvenstett", "WithDecryption": True})
    ]


HITS = """2 hit(s) for 'q' (workspace=kalvenstett):
1. score 0.815  workspace=kalvenstett  layer=human  preise--01-x.md
   text
2. score 0.700  workspace=kalvenstett  layer=human  preise--02-y.md
   text
"""


def test_the_search_readers(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    assert rehearsal.hits_only_in_workspace(HITS, ctx) == "2 hit(s), each from kalvenstett"
    with pytest.raises(StepFailed, match="hits from \\['quillmoor'\\]"):
        rehearsal.hits_only_in_workspace(
            HITS.replace("=kalvenstett  layer", "=quillmoor  layer", 1), ctx
        )
    with pytest.raises(StepFailed, match="no hit"):
        rehearsal.hits_only_in_workspace("0 hit(s)", ctx)


OTHER = """2 hit(s) for 'q' (workspace=quillmoor):
1. score 0.415  workspace=quillmoor  layer=human  faq--03-projects.md
   text
2. score 0.400  workspace=quillmoor  layer=human  pricing.pdf
   text
"""


def test_a_search_elsewhere_must_find_hits_and_none_of_the_new_documents(
    tmp_path: Path,
) -> None:
    ctx = context(tmp_path)
    ctx.documents = frozenset({"preise", "haeufige-fragen"})
    assert rehearsal.no_hits_of_workspace(OTHER, ctx) == (
        "2 hit(s) in quillmoor, none of kalvenstett's documents"
    )
    # Filed under the wrong workspace: the metadata says quillmoor, the file is Kalvenstett's.
    misfiled = OTHER.replace("faq--03-projects.md", "preise--01-x.md")
    with pytest.raises(StepFailed, match=r"kalvenstett's \['preise--01-x.md'\] show up"):
        rehearsal.no_hits_of_workspace(misfiled, ctx)
    with pytest.raises(StepFailed, match="kalvenstett's"):
        rehearsal.no_hits_of_workspace(HITS, ctx)
    with pytest.raises(StepFailed, match="no hit in quillmoor"):
        rehearsal.no_hits_of_workspace("0 hit(s) for 'q' (workspace=quillmoor):\n", ctx)


def test_a_document_named_alike_in_both_workspaces_is_no_alarm(tmp_path: Path) -> None:
    """Both have a "pricing": the hit is the other workspace's own file, which its folder goes
    up as. A file named like the new one's document that the other folder does not produce
    is still found, an earlier version filed there included."""
    ctx = context(tmp_path)
    ctx.documents = frozenset({"preise", "pricing"})
    ctx.other_files = frozenset({"faq--03-projects.md", "pricing.pdf"})
    assert rehearsal.no_hits_of_workspace(OTHER, ctx) == (
        "2 hit(s) in quillmoor, none of kalvenstett's documents"
    )
    misfiled = OTHER.replace("faq--03-projects.md", "pricing--01-old-heading.md")
    with pytest.raises(StepFailed, match=r"kalvenstett's \['pricing--01-old-heading.md'\]"):
        rehearsal.no_hits_of_workspace(misfiled, ctx)
    ctx.other_files = frozenset()
    with pytest.raises(StepFailed, match=r"kalvenstett's \['pricing.pdf'\] show up"):
        rehearsal.no_hits_of_workspace(OTHER, ctx)


def test_the_other_workspaces_own_files_are_named_as_the_upload_names_them(
    tmp_path: Path,
) -> None:
    human = tmp_path / "quillmoor" / "human"
    human.mkdir(parents=True)
    sidecar = json.dumps(sidecar_for("quillmoor", "human"))
    (human / "faq.md").write_text("# FAQ\n\n## Projects\n\nText.\n", encoding="utf-8")
    (human / "pricing.pdf").write_bytes(b"%PDF-1.7")
    for name in ("faq.md", "pricing.pdf"):
        (human / f"{name}.metadata.json").write_text(sidecar, encoding="utf-8")
    assert rehearsal.uploaded_names(tmp_path, "quillmoor") == {
        "faq--01-projects.md",
        "pricing.pdf",
    }
    assert rehearsal.uploaded_names(tmp_path, "nobody") == frozenset()
    assert rehearsal.uploaded_names(tmp_path / "missing", "quillmoor") == frozenset()


def test_the_demo_folder_is_the_one_ingest_uploads_by_default() -> None:
    assert rehearsal.DEMO_SOURCE == rehearsal.REPOSITORY / ingest.parse_args([]).source
    names = rehearsal.uploaded_names(rehearsal.DEMO_SOURCE, "quillmoor")
    assert any(name.startswith("admin-billing-and-plans--01-") for name in names)


def test_the_new_documents_are_named_from_the_source(tmp_path: Path) -> None:
    human = tmp_path / "kalvenstett" / "human"
    human.mkdir(parents=True)
    for name in ("preise.md", "preise.md.metadata.json", "liste.pdf", "liste.pdf.metadata.json"):
        (human / name).write_text("x", encoding="utf-8")
    (human / "notes-4.1.md").write_text("x", encoding="utf-8")
    assert rehearsal.document_names(tmp_path, "kalvenstett") == {"preise", "liste", "notes-4.1"}
    assert rehearsal.document_names(tmp_path, "nobody") == frozenset()
    # A section names its document before "--"; a file uploaded whole keeps its suffix.
    assert layout.document_of("notes-4.1--02-changes.md") == "notes-4.1"
    assert layout.document_of("liste.pdf") == "liste"


def test_a_document_name_with_spaces_or_its_own_dashes_is_read_whole() -> None:
    # The name before a section's number and slug, not before the first "--".
    assert layout.document_of("preise und zahlung--02-skonto.md") == "preise und zahlung"
    assert layout.document_of("a--b--03-x.md") == "a--b"
    # A file uploaded whole has no section part: its "--" belongs to its name.
    assert layout.document_of("notes--draft.pdf") == "notes--draft"
    # Decomposed umlauts, as a Mac may save them, read like the composed keys ingest writes.
    assert layout.document_of("Prëise--01-x.md") == "Prëise"


@pytest.mark.parametrize(
    "file", ["preise und zahlung--01-skonto.md", "preise%20und%20zahlung--01-skonto.md"]
)
def test_a_new_document_with_spaces_in_its_name_is_found_in_another_workspace(
    tmp_path: Path, file: str
) -> None:
    """Whether Bedrock gives the space as it is or as %20, the misfiled section is found; before,
    the line did not match, fell out, and the step passed on the other hit alone."""
    ctx = context(tmp_path)
    ctx.documents = frozenset({"preise und zahlung"})
    misfiled = OTHER.replace("faq--03-projects.md", file)
    with pytest.raises(StepFailed, match="show up in quillmoor"):
        rehearsal.no_hits_of_workspace(misfiled, ctx)


def test_the_first_answer_must_hold_the_fact(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    ctx.fact = "68 Euro"
    output = "Answer (kalvenstett):\n\nEin Geselle kostet 68 Euro [1].\n\nLayers cited: human\n"
    assert rehearsal.answered(output, ctx) == "answered; holds '68 Euro'; layers cited: human"
    # The fact must stand in the answer, not in the list of sources below it.
    sources = "Answer (kalvenstett):\n\nNicht bekannt.\n\nLayers cited: none\n  68 Euro.md\n"
    with pytest.raises(StepFailed, match="lacks '68 Euro'"):
        rehearsal.answered(sources, ctx)


def test_the_answer_readers(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    with pytest.raises(StepFailed, match="no answer"):
        rehearsal.answered("Answer (quillmoor):", ctx)
    with pytest.raises(StepFailed, match="not refused with 401"):
        rehearsal.refused_with_401("error: HTTP 503: 401 workspaces checked", ctx)


def residency_clients(
    model: str = "qwen.qwen3-235b-a22b-2507-v1:0",
    logging: dict[str, Any] | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    arn = f"arn:aws:bedrock:{REGION}:123456789012:guardrail/abc"
    guardrail: dict[str, Any] = {"guardrailArn": arn}
    if profile:
        guardrail["crossRegionDetails"] = {"guardrailProfileId": profile}
    return {
        "s3": Fake(get_bucket_location={"LocationConstraint": REGION}),
        "bedrock-agent": Fake(
            get_knowledge_base={
                "knowledgeBase": {
                    "knowledgeBaseConfiguration": {
                        "vectorKnowledgeBaseConfiguration": {
                            "embeddingModelArn": f"arn:aws:bedrock:{REGION}::foundation-model/t"
                        }
                    },
                    "storageConfiguration": {
                        "s3VectorsConfiguration": {
                            "indexArn": f"arn:aws:s3vectors:{REGION}:1:bucket/b/index/i"
                        }
                    },
                }
            }
        ),
        "lambda": Fake(
            get_function_configuration={
                "FunctionArn": f"arn:aws:lambda:{REGION}:1:function:andur-answer",
                "Environment": {
                    "Variables": {
                        "MODEL_ID": model,
                        "TOPIC_MODEL_ID": "openai.gpt-oss-120b-1:0",
                        "GUARDRAIL_ID": "a",
                        "GUARDRAIL_VERSION": "3",
                        "EXCERPT_GUARDRAIL_ID": "b",
                        "EXCERPT_GUARDRAIL_VERSION": "5",
                    }
                },
            }
        ),
        "bedrock": Fake(
            get_guardrail=guardrail, get_model_invocation_logging_configuration=logging or {}
        ),
        "dynamodb": Fake(
            describe_table={
                "Table": {"TableArn": f"arn:aws:dynamodb:{REGION}:1:table/t", "Replicas": []}
            }
        ),
        "logs": Fake(
            describe_log_groups={
                "logGroups": [
                    {
                        "logGroupName": "/aws/lambda/andur-answer",
                        "arn": f"arn:aws:logs:{REGION}:1:log-group:x:*",
                    }
                ]
            }
        ),
    }


OUTPUTS = {"DocumentsBucketName": "d", "KnowledgeBaseId": "k", "BudgetTableName": "t"}


def test_residency_names_every_place_and_writes_its_table(tmp_path: Path) -> None:
    ctx = context(tmp_path, residency_clients(), **OUTPUTS)
    assert (
        rehearsal_checks.check_residency(ctx)
        == f"11 places, each in {REGION}; no log of the model calls"
    )
    table = (tmp_path / "residency.md").read_text(encoding="utf-8")
    for line in (
        "| answer model qwen.qwen3-235b-a22b-2507-v1:0 | eu-central-1 |",
        "| vector index | eu-central-1 |",
        "| model invocation log | off |",
    ):
        assert line in table


def test_residency_reads_the_guardrail_versions_the_function_calls(tmp_path: Path) -> None:
    """Without a version Bedrock describes the draft, which the function never calls and
    which may have another profile than the version in use."""
    clients = residency_clients()
    rehearsal_checks.check_residency(context(tmp_path, clients, **OUTPUTS))
    read = [kwargs for name, kwargs in clients["bedrock"].calls if name == "get_guardrail"]
    assert read == [
        {"guardrailIdentifier": "a", "guardrailVersion": "3"},
        {"guardrailIdentifier": "b", "guardrailVersion": "5"},
    ]


@pytest.mark.parametrize(
    ("clients", "message"),
    [
        (residency_clients(model="eu.amazon.nova-pro-v1:0"), "nova-pro-v1:0: profile eu"),
        (residency_clients(model=""), "answer model: no MODEL_ID"),
        (
            residency_clients(profile="eu.guardrail.v1:0"),
            "answer guardrail: profile eu.guardrail.v1:0",
        ),
        (
            residency_clients(logging={"loggingConfig": {"textDataDeliveryEnabled": True}}),
            "model invocation log: on",
        ),
    ],
)
def test_residency_fails_on_anything_that_leaves_the_region(
    tmp_path: Path, clients: dict[str, Any], message: str
) -> None:
    with pytest.raises(StepFailed, match=message):
        rehearsal_checks.check_residency(context(tmp_path, clients, **OUTPUTS))


def test_the_function_log_is_counted_and_a_crash_fails(tmp_path: Path) -> None:
    lines = [
        "START RequestId: 1",
        '[INFO]\t2026-10-07T05:00:00Z\t1\t{"event": "answered", "status": 200}',
        '[INFO]\t2026-10-07T05:00:01Z\t2\t{"event": "refused", "status": 401}',
        '[INFO]\t2026-10-07T05:00:02Z\t3\t{"event": "answered", "status": 200}',
        '[INFO]\t2026-10-07T05:00:03Z\t4\t{"event": broken',
    ]
    assert rehearsal_checks.event_counts(lines) == {"answered": 2, "refused": 1}

    class Paginator:
        def __init__(self, messages: list[str]) -> None:
            self.messages = messages

        def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"events": [{"message": m} for m in self.messages]}]

    ctx = context(tmp_path, {"logs": Fake(get_paginator=lambda name: Paginator(lines))})
    assert rehearsal_checks.check_function_log(ctx) == "answered 2, refused 1"
    assert '"answered": 2' in (tmp_path / "function-log.json").read_text(encoding="utf-8")
    crashed = [*lines, '[ERROR]\t...\t{"event": "crashed", "status": 500}']
    ctx.clients = lambda s: Fake(get_paginator=lambda name: Paginator(crashed))
    with pytest.raises(StepFailed, match="ended badly: {'crashed': 1}"):
        rehearsal_checks.check_function_log(ctx)
    # A timeout is Lambda's own line; the handler never gets to write one.
    timed_out = [*lines, "2026-10-07T05:03:15Z 5 Task timed out after 195.00 seconds"]
    ctx.clients = lambda s: Fake(get_paginator=lambda name: Paginator(timed_out))
    with pytest.raises(StepFailed, match="'timed-out': 1"):
        rehearsal_checks.check_function_log(ctx)
