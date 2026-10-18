"""Running the steps of a rehearsal (scripts/rehearsal.py): what a step is, how it runs and
is timed, and the summary of a run."""

import concurrent.futures
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.ask import API_KEY_ENVIRONMENT_VARIABLE
from scripts.aws import AwsProblem, read_stack_outputs

REPOSITORY = Path(__file__).resolve().parents[1]


#: What replaces the twelve-digit account id in every file the run writes.
ACCOUNT_PLACEHOLDER = "<account>"


class StepFailed(Exception):
    """A step ended in a way the run must not go on from."""


@dataclass
class Context:
    """What the steps share: the run's settings and what earlier steps found out."""

    region: str
    stack_name: str
    workspace: str
    other_workspace: str
    out_dir: Path
    expect_role: str | None
    clients: Callable[[str], Any]
    #: What the answer to the first question must contain, such as "68 Euro"; None: anything.
    fact: str | None = None
    #: The new workspace's document names, without their suffix: a section file begins with one.
    documents: frozenset[str] = frozenset()
    #: The file names the other workspace's own documents go up as, from the demo folder.
    other_files: frozenset[str] = frozenset()
    outputs: dict[str, str] = field(default_factory=dict)
    account: str | None = None
    started: datetime = field(default_factory=lambda: datetime.now(UTC))

    def client(self, service: str) -> Any:
        return self.clients(service)

    def output(self, name: str) -> str:
        """A stack output; read from the stack the first time one is needed."""
        if not self.outputs:
            self.outputs = read_stack_outputs(self.client("cloudformation"), self.stack_name)
        if name not in self.outputs:
            raise StepFailed(f"the stack has no output {name}")
        return self.outputs[name]


@dataclass(frozen=True)
class Step:
    id: str
    phase: str
    title: str
    #: A command line, run from the repository root with the shell's environment.
    command: tuple[str, ...] | None = None
    #: A check run in this process; returns one line for the summary, raises `StepFailed`.
    check: Callable[[Context], str] | None = None
    #: What a person does by hand; the step runs nothing.
    manual: str | None = None
    #: Reads the command's output; returns one line for the summary, raises `StepFailed`.
    reads: Callable[[str, Context], str] | None = None
    #: The exit code that means the step did what it should.
    exit_code: int = 0
    #: The command prints a key on stdout: it is thrown away, never written to the log.
    secret_stdout: bool = False
    #: Extra environment for the command, made when it starts and never logged.
    environment: Callable[[Context], Mapping[str, str]] | None = None
    #: Steps next to each other with the same group run at once, lane by lane: each lane's
    #: steps one after the other. Only measurements that share no AWS quota share a group.
    group: str | None = None
    lane: str | None = None
    #: Why the step does not run: it is named as skipped, never as passed.
    skipped: str | None = None

    @property
    def kind(self) -> str:
        return "manual" if self.manual else "check" if self.check else "command"


@dataclass
class Result:
    number: int
    id: str
    phase: str
    title: str
    kind: str
    #: passed, failed or skipped; a step by hand: done or open.
    status: str
    started: str
    seconds: float
    summary: str
    exit_code: int | None = None
    log: str | None = None
    group: str | None = None


def run_command(
    argv: Sequence[str], log: Path, secret_stdout: bool, environment: Mapping[str, str]
) -> tuple[int, str]:
    """Run a command from the repository root and write its output to `log`; stdout is thrown
    away when it carries a secret. Returns the exit code and the output. A key set in the
    shell is not handed on: `ask` would take it over the new workspace's own."""
    inherited = {k: v for k, v in os.environ.items() if k != API_KEY_ENVIRONMENT_VARIABLE}
    completed = subprocess.run(
        list(argv),
        cwd=REPOSITORY,
        env={**inherited, **environment, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.DEVNULL if secret_stdout else subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    log.write_text(" ".join(argv[1:]) + "\n\n" + output, encoding="utf-8")
    return completed.returncode, output


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


@dataclass
class Runner:
    ctx: Context
    manual_mode: str = "skip"
    run: Callable[..., tuple[int, str]] = run_command
    clock: Callable[[], float] = time.monotonic
    ask_person: Callable[[str], str] = input
    say: Callable[[str], None] = say
    #: Every step finished so far, in the plan's order.
    results: list[Result] = field(default_factory=list)

    def clean(self, text: str) -> str:
        """The text without the account id, for the screen."""
        account = self.ctx.account
        return text.replace(account, ACCOUNT_PLACEHOLDER) if account else text

    def tell(self, line: str) -> None:
        self.say(self.clean(line))

    def step(self, number: int, step: Step) -> Result:
        log = self.ctx.out_dir / f"{number:02d}-{step.id}.log"
        started, begin = now_text(), self.clock()
        result = Result(
            number, step.id, step.phase, step.title, step.kind, "passed", started, 0.0, ""
        )
        result.group = step.group
        self.tell(f"[{started}] {number:02d} {step.id}: {step.title}")
        try:
            if step.skipped:
                result.status, result.summary = "skipped", step.skipped
            elif step.manual:
                self.tell(f"  by hand: {step.manual}")
                result.status = "open"
                if self.manual_mode == "wait":
                    answer = self.ask_person("  Enter when done, or 'skip': ")
                    result.status = "open" if answer.strip().lower() == "skip" else "done"
                result.summary = "done by hand" if result.status == "done" else "left open"
            elif step.check:
                result.summary = step.check(self.ctx)
            else:
                assert step.command is not None
                environment = step.environment(self.ctx) if step.environment else {}
                code, output = self.run(step.command, log, step.secret_stdout, environment)
                result.exit_code, result.log = code, log.name
                if code != step.exit_code:
                    raise StepFailed(f"exit code {code}, not {step.exit_code}; see {log.name}")
                result.summary = step.reads(output, self.ctx) if step.reads else f"exit code {code}"
        except (StepFailed, AwsProblem) as problem:
            result.status, result.summary = "failed", str(problem)
        except Exception as problem:  # a network break, a missing field: the step failed
            result.status = "failed"
            result.summary = f"stopped by {type(problem).__name__}: {problem}"
        result.seconds = round(self.clock() - begin, 1)
        self.tell(f"  {result.status} after {format_seconds(result.seconds)}: {result.summary}")
        return result

    def lane(self, entries: Sequence[tuple[int, Step]]) -> list[Result]:
        done: list[Result] = []
        for number, step in entries:
            done.append(self.step(number, step))
            if done[-1].status == "failed":
                break
        return done

    def group(self, members: Sequence[tuple[int, Step]]) -> list[Result]:
        """Run a group's lanes at once; the results come back in the plan's order."""
        lanes: dict[str | None, list[tuple[int, Step]]] = {}
        for number, step in members:
            lanes.setdefault(step.lane, []).append((number, step))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            futures = [pool.submit(self.lane, entries) for entries in lanes.values()]
            finished = [result for future in futures for result in future.result()]
        order = {step.id: number for number, step in members}
        return sorted(finished, key=lambda result: order[result.id])

    def run_all(
        self, steps: Sequence[tuple[int, Step]], record: Callable[[list[Result]], None]
    ) -> list[Result]:
        """Run the steps in order, a group's lanes at once; stop after the first failure."""
        results = self.results
        index = 0
        while index < len(steps):
            group = steps[index][1].group
            end = index + 1
            while group and end < len(steps) and steps[end][1].group == group:
                end += 1
            members = steps[index:end]
            batch = self.group(members) if group else [self.step(*members[0])]
            results.extend(batch)
            record(results)
            if any(result.status == "failed" for result in batch):
                break
            index = end
        return results


def select(
    plan: Sequence[Step], start: str | None, only: Sequence[str] | None
) -> list[tuple[int, Step]]:
    """The steps to run, numbered by their place in the plan. The identity always comes first:
    without it the logs could not be cleared of the account id."""
    ids = [step.id for step in plan]
    for wanted in [start, *(only or [])]:
        if wanted is not None and wanted not in ids:
            raise ValueError(f"no step {wanted!r}; the steps: {', '.join(ids)}")
    numbered = list(enumerate(plan, start=1))
    if only:
        chosen = [(n, s) for n, s in numbered if s.id in only]
    elif start:
        chosen = numbered[ids.index(start) :]
    else:
        chosen = numbered
    if chosen[0][1].id != ids[0]:
        chosen = [numbered[0], *chosen]
    return chosen


def format_seconds(seconds: float) -> str:
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest:02d} s" if minutes else f"{rest} s"


def summary_lines(results: Sequence[Result], wall_seconds: float) -> list[str]:
    """The run as a Markdown table, then the time per phase and of the whole run."""
    lines = ["| # | Step | Kind | Status | Took | Result |", "|---|---|---|---|---|---|"]
    for r in results:
        took = format_seconds(r.seconds) + (" (at once)" if r.group else "")
        summary = r.summary.replace("|", "\\|")
        lines.append(f"| {r.number} | {r.id} | {r.kind} | {r.status} | {took} | {summary} |")
    lines += ["", "| Phase | Steps | Their seconds added up |", "|---|---|---|"]
    phases: dict[str, list[Result]] = {}
    for r in results:
        phases.setdefault(r.phase, []).append(r)
    for phase, members in phases.items():
        added = format_seconds(sum(m.seconds for m in members))
        lines.append(f"| {phase} | {len(members)} | {added} |")
    lines += ["", f"Wall clock of the run: {format_seconds(wall_seconds)}."]
    still_open = [r.id for r in results if r.status == "open"]
    if still_open:
        lines.append(f"By hand and still open: {', '.join(still_open)}.")
    skipped = [r.id for r in results if r.status == "skipped"]
    if skipped:
        lines.append(f"Skipped, not measured: {', '.join(skipped)}.")
    return lines


def scrub(folder: Path, account: str | None) -> int:
    """Replace the account id in every text file of the folder; returns how many it changed."""
    if not account:
        return 0
    changed = 0
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if account in text:
            path.write_text(text.replace(account, ACCOUNT_PLACEHOLDER), encoding="utf-8")
            changed += 1
    return changed


def inside_repository(folder: Path) -> bool:
    resolved = folder.resolve()
    return resolved == REPOSITORY or REPOSITORY in resolved.parents
