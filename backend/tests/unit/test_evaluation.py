"""Answer-quality checks (ADR 0038): parsing, scoring and the report."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from documind.application.quality.evaluation import (
    EvalCase,
    Observed,
    load_set,
    markdown,
    parse_set,
    run_set,
    score,
)
from documind.domain.errors import ValidationFailedError

MSA = "Sample: Master Services Agreement"


def test_built_in_sample_set_is_valid() -> None:
    evals = load_set("samples")
    assert len(evals.cases) >= 10
    assert any(not c.answerable for c in evals.cases)
    assert all(c.documents for c in evals.cases if c.answerable)


def test_files_load_and_bad_sets_are_refused(tmp_path: Path) -> None:
    good = tmp_path / "set.json"
    good.write_text(json.dumps({"name": "Mine", "cases": [{"question": "Why?"}]}))
    loaded = load_set(str(good))
    assert loaded.name == "Mine"
    assert loaded.cases[0].id == "case-1"

    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ValidationFailedError, match="Not valid JSON"):
        load_set(str(bad))
    with pytest.raises(ValidationFailedError, match="neither"):
        load_set(str(tmp_path / "missing.json"))
    for data, message in [
        ({}, "cases"),
        ({"cases": []}, "1 to"),
        ({"cases": [{"question": " "}]}, "needs a question"),
        ({"cases": [{"id": "a", "question": "x"}, {"id": "a", "question": "y"}]}, "twice"),
        ({"cases": [{"question": "x", "must_include": [[]]}]}, "phrase"),
        ({"cases": [{"question": "x", "documents": "MSA"}]}, "titles"),
        ({"cases": [{"question": "x", "answerable": False, "documents": ["A"]}]}, "unanswerable"),
        ({"cases": [{"question": "x", "answerable": "no"}]}, "true or false"),
    ]:
        with pytest.raises(ValidationFailedError, match=message):
            parse_set(data)


def test_answerable_cases_need_grounding_the_right_document_and_every_fact() -> None:
    case = EvalCase(
        "fee", "Monthly fee?", documents=(MSA,), must_include=(("18,500", "18500"), ("USD",))
    )
    good = Observed(f"The fee is **USD{chr(0xA0)}18,500** per month [1].", "grounded", (MSA, MSA))
    result = score(case, good)
    assert result.passed
    assert result.cited_documents == [MSA]
    assert result.checks == {
        "answered": True,
        "cites_expected_document": True,
        "includes_expected_facts": True,
    }

    wrong_doc = score(case, Observed("USD 18500", "grounded", ("Sample: Employee Handbook",)))
    assert not wrong_doc.passed
    assert wrong_doc.checks["cites_expected_document"] is False
    missing_fact = score(case, Observed("USD 18,000", "grounded", (MSA,)))
    assert missing_fact.checks["includes_expected_facts"] is False
    declined = score(case, Observed("Not in the documents.", "insufficient_context", ()))
    assert declined.checks["answered"] is False


def test_unanswerable_cases_must_decline() -> None:
    case = EvalCase("mars", "Boiling point on Mars?", answerable=False)
    assert score(case, Observed("I can't find that.", "insufficient_context", ())).passed
    guessed = score(case, Observed("About 10 °C [1].", "grounded", (MSA,)))
    assert not guessed.passed
    assert guessed.checks == {"declined": False}


def test_run_counts_errors_as_failures_and_reports_them() -> None:
    evals = parse_set(
        {
            "name": "Mini",
            "cases": [
                {"id": "ok", "question": "Fee?", "documents": [MSA], "must_include": ["18,500"]},
                {"id": "boom", "question": "Crash?"},
                {"id": "none", "question": "Mars?", "answerable": False},
            ],
        }
    )

    async def ask(question: str) -> Observed:
        if question == "Crash?":
            raise RuntimeError("model unavailable")
        if question == "Fee?":
            return Observed("USD 18,500 [1]", "grounded", (MSA,), 12, "gpt-test")
        return Observed("Sure: 5 °C.", "grounded", (MSA,), 8, "gpt-test")

    seen: list[str] = []
    report = asyncio.run(run_set(evals, ask, on_case=lambda r: seen.append(r.id)))
    assert seen == ["ok", "boom", "none"]
    assert (report.total, report.passed, report.model) == (3, 1, "gpt-test")
    assert report.pass_rate == pytest.approx(1 / 3, abs=1e-3)
    assert report.checks["declined"] == {"passed": 0, "failed": 1}
    boom = next(c for c in report.cases if c.id == "boom")
    assert boom.error == "RuntimeError: model unavailable"

    text = markdown(report)
    assert text.startswith("# Mini: 1/3 passed (33%)")
    assert "**boom**" in text
    assert "model unavailable" in text
    assert "**none**" in text
    assert "**ok**" not in text
    assert json.loads(json.dumps(report.to_dict(), default=str))["passed"] == 1
