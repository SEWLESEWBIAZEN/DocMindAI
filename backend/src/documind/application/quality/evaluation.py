"""Answer-quality checks: a fixed set of questions with expected answers (ADR 0038).

Each case says what a good answer must do:

* answerable cases: answer from the documents ("grounded"), cite at least one of
  the expected documents, and contain every expected phrase (a phrase can list
  alternatives, any of which counts);
* unanswerable cases: say the documents don't cover the question
  ("insufficient_context") instead of guessing.

Scoring is deliberately simple and strict so a change that makes answers worse
shows up as failed cases, not as a slightly lower number nobody reads.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from documind.domain.errors import ValidationFailedError

MAX_CASES = 500
BUILT_IN = {"samples": "samples.json"}


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    question: str
    answerable: bool = True
    documents: tuple[str, ...] = ()
    # Each entry is a set of alternatives; one of them must appear.
    must_include: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class EvalSet:
    name: str
    cases: tuple[EvalCase, ...]


@dataclass(frozen=True, slots=True)
class Observed:
    """What the system answered, independent of how it was produced."""

    answer: str
    status: str  # "grounded" or "insufficient_context"
    cited_documents: tuple[str, ...]
    latency_ms: int = 0
    model: str | None = None


@dataclass(frozen=True, slots=True)
class CaseResult:
    id: str
    question: str
    passed: bool
    checks: dict[str, bool]
    status: str
    cited_documents: list[str]
    answer: str
    latency_ms: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EvalReport:
    name: str
    total: int
    passed: int
    pass_rate: float
    checks: dict[str, dict[str, int]]
    model: str | None
    duration_ms: int
    cases: list[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize(text: str) -> str:
    text = text.replace("**", "").replace(chr(0xA0), " ").replace(chr(0x202F), " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


def parse_set(data: Any) -> EvalSet:
    """Validate an evaluation set (JSON). Raises ValidationFailedError with the problem."""
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValidationFailedError('An evaluation set is an object with a "cases" list.')
    raw_cases = data["cases"]
    if not raw_cases or len(raw_cases) > MAX_CASES:
        raise ValidationFailedError(f"An evaluation set needs 1 to {MAX_CASES} cases.")
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for n, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, dict):
            raise ValidationFailedError(f"Case {n} must be an object.")
        case_id = str(raw.get("id") or f"case-{n}")
        if case_id in seen:
            raise ValidationFailedError(f"Case id {case_id!r} is used twice.")
        seen.add(case_id)
        question = raw.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValidationFailedError(f"Case {case_id!r} needs a question.")
        answerable = raw.get("answerable", True)
        if not isinstance(answerable, bool):
            raise ValidationFailedError(f"Case {case_id!r}: answerable must be true or false.")
        documents = raw.get("documents", [])
        phrases = raw.get("must_include", [])
        if not isinstance(documents, list) or not all(isinstance(d, str) for d in documents):
            raise ValidationFailedError(f"Case {case_id!r}: documents must be a list of titles.")
        if not isinstance(phrases, list):
            raise ValidationFailedError(f"Case {case_id!r}: must_include must be a list.")
        groups: list[tuple[str, ...]] = []
        for p in phrases:
            options = [p] if isinstance(p, str) else p
            if (
                not isinstance(options, list)
                or not options
                or not all(isinstance(o, str) and o.strip() for o in options)
            ):
                raise ValidationFailedError(
                    f"Case {case_id!r}: each must_include entry is a phrase or a list of phrases."
                )
            groups.append(tuple(options))
        if not answerable and (documents or groups):
            raise ValidationFailedError(
                f"Case {case_id!r}: an unanswerable case can't expect documents or phrases."
            )
        cases.append(
            EvalCase(case_id, question.strip(), answerable, tuple(documents), tuple(groups))
        )
    return EvalSet(str(data.get("name") or "Evaluation set"), tuple(cases))


def load_set(source: str) -> EvalSet:
    """A built-in set by name (``samples``) or a JSON file path."""
    if source in BUILT_IN:
        text = resources.files("documind.resources.evals").joinpath(BUILT_IN[source]).read_text()
    else:
        path = Path(source)
        if not path.is_file():
            raise ValidationFailedError(
                f"{source!r} is neither a built-in set ({', '.join(BUILT_IN)}) nor a file."
            )
        text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationFailedError(f"Not valid JSON: {exc}") from exc
    return parse_set(data)


def score(case: EvalCase, seen: Observed) -> CaseResult:
    checks: dict[str, bool] = {}
    if case.answerable:
        checks["answered"] = seen.status == "grounded"
        if case.documents:
            wanted = {_normalize(d) for d in case.documents}
            checks["cites_expected_document"] = any(
                _normalize(d) in wanted for d in seen.cited_documents
            )
        if case.must_include:
            text = _normalize(seen.answer)
            checks["includes_expected_facts"] = all(
                any(_normalize(option) in text for option in group) for group in case.must_include
            )
    else:
        checks["declined"] = seen.status == "insufficient_context"
    return CaseResult(
        id=case.id,
        question=case.question,
        passed=all(checks.values()),
        checks=checks,
        status=seen.status,
        cited_documents=list(dict.fromkeys(seen.cited_documents)),
        answer=seen.answer,
        latency_ms=seen.latency_ms,
    )


def summarize(name: str, results: Sequence[CaseResult], model: str | None, ms: int) -> EvalReport:
    passed = sum(1 for r in results if r.passed)
    checks: dict[str, dict[str, int]] = {}
    for r in results:
        for check, ok in r.checks.items():
            bucket = checks.setdefault(check, {"passed": 0, "failed": 0})
            bucket["passed" if ok else "failed"] += 1
    return EvalReport(
        name=name,
        total=len(results),
        passed=passed,
        pass_rate=round(passed / len(results), 4) if results else 0.0,
        checks=checks,
        model=model,
        duration_ms=ms,
        cases=list(results),
    )


async def run_set(
    evals: EvalSet,
    ask: Callable[[str], Awaitable[Observed]],
    *,
    on_case: Callable[[CaseResult], None] | None = None,
) -> EvalReport:
    """Ask every question in order (one at a time, to stay within rate limits)."""
    started = time.monotonic()
    results: list[CaseResult] = []
    model: str | None = None
    for case in evals.cases:
        try:
            seen = await ask(case.question)
            model = seen.model or model
            result = score(case, seen)
        except Exception as exc:  # one failing question must not stop the run
            result = CaseResult(
                id=case.id,
                question=case.question,
                passed=False,
                checks={"no_error": False},
                status="error",
                cited_documents=[],
                answer="",
                latency_ms=0,
                error=f"{type(exc).__name__}: {exc}",
            )
        results.append(result)
        if on_case is not None:
            on_case(result)
    return summarize(evals.name, results, model, int((time.monotonic() - started) * 1000))


def markdown(report: EvalReport) -> str:
    """A short report for people (pull requests, release notes)."""
    lines = [
        f"# {report.name}: {report.passed}/{report.total} passed ({report.pass_rate:.0%})",
        "",
        f"Model: `{report.model or 'none'}` · {report.duration_ms / 1000:.1f} s",
        "",
        "| Check | Passed | Failed |",
        "|---|---|---|",
    ]
    lines += [f"| {name} | {c['passed']} | {c['failed']} |" for name, c in report.checks.items()]
    failed = [r for r in report.cases if not r.passed]
    if failed:
        lines += ["", "## Failed", ""]
        for r in failed:
            bad = ", ".join(k for k, ok in r.checks.items() if not ok)
            lines.append(f"- **{r.id}**: {r.question} — failed {bad} (status `{r.status}`)")
            if r.error:
                lines.append(f"  - error: {r.error}")
            elif r.answer:
                snippet = " ".join(r.answer.split())[:240]
                lines.append(f"  - answer: {snippet}")
    return "\n".join(lines) + "\n"
