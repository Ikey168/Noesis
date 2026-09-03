"""Machine-checkable quality gates for ``noesis-answer-v1`` responses."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from src.analytics.honesty import analytic_envelope, interval, is_interval

_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "schemas"
    / "jsonschema"
    / "noesis-answer-v1.json"
)


def _cited(statement: Mapping[str, Any]) -> bool:
    return any(
        locator.get("cited") is True
        for field in ("supporting_evidence", "contradicting_evidence")
        for locator in statement.get(field, [])
        if isinstance(locator, dict)
    )


def evaluate_answer(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate schema, citation, refusal, and quantitative-honesty rules.

    The result is itself an honesty envelope.  ``compliance_rate`` is an exact
    descriptive fraction over the statements in this response, so its bounds
    equal its value rather than pretending to be a population estimate.
    """
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft7Validator(schema)
    violations = [
        f"schema {error.json_path}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda item: item.json_path)
    ]
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    statements = data.get("statements")
    statements = statements if isinstance(statements, list) else []

    ids = [statement.get("id") for statement in statements if isinstance(statement, dict)]
    if len(ids) != len(set(ids)):
        violations.append("statement ids must be unique")
    if data.get("n") != len(statements):
        violations.append("data.n must equal the number of statements")

    compliant = 0
    rendered = str(data.get("rendered") or "")
    for index, statement in enumerate(statements):
        if not isinstance(statement, dict):
            violations.append(f"statement {index} is not an object")
            continue
        cited = _cited(statement)
        verdict = statement.get("verdict")
        if cited or verdict == "unverifiable":
            compliant += 1
        else:
            violations.append(
                f"statement {index} is factual but has no cited evidence or "
                "explicit unverifiable verdict"
            )
        if statement.get("text") not in rendered:
            violations.append(f"statement {index} is absent from rendered output")
        if not cited and "uncited — unverifiable" not in rendered:
            violations.append("rendered output hides an uncited statement")

        quantitative = statement.get("quantitative_check")
        if isinstance(quantitative, dict):
            from src.analytics.honesty import validate_analytic_output

            violations.extend(
                f"statement {index} quantitative check: {message}"
                for message in validate_analytic_output(quantitative)
            )
            observed = quantitative.get("observed")
            if observed is not None and not is_interval(observed):
                violations.append(
                    f"statement {index} quantitative observed value has no valid interval"
                )

    status = data.get("answer_status")
    refusal = data.get("refusal")
    if status == "refused":
        if not isinstance(refusal, dict):
            violations.append("refused answer must include refusal metadata")
        if any(statement.get("verdict") != "unverifiable" for statement in statements):
            violations.append("refused answer may contain only unverifiable statements")
    elif status in {"answered", "partial"} and refusal is not None:
        violations.append("non-refused response must not include refusal metadata")
    partial_reasons = data.get("partial_reasons")
    if status == "partial" and not partial_reasons:
        violations.append("partial response must explain why it is partial")
    if status == "answered" and partial_reasons:
        violations.append("answered response may not carry partial reasons")

    total = len(statements)
    rate = compliant / total if total else 0.0
    return analytic_envelope(
        n=total,
        method="exact per-response Answer v1 contract and citation audit",
        assumptions=[
            "the rate describes this response only and is not a population estimate",
            "citation validity means locator presence and resolution, not factual truth",
        ],
        passed=not violations,
        violations=violations,
        compliant_statements=compliant,
        compliance_rate=interval(rate, rate, rate, 1.0),
    )


def evaluate_cases(
    answer_fn: Callable[[str], Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Run deterministic answer/refusal cases and report exact pass coverage."""
    results = []
    total_factual = 0
    total_factual_cited = 0
    total_evidence = 0
    expected_evidence_returned = 0
    abstention_cases = 0
    correct_abstentions = 0
    stable_cases = 0
    for case in cases:
        payload = answer_fn(str(case["question"]))
        repeated = answer_fn(str(case["question"]))
        evaluation = evaluate_answer(payload)
        data = payload.get("data", {})
        statements = data.get("statements", [])
        cited_count = sum(1 for statement in statements if _cited(statement))
        verdicts = [statement.get("verdict") for statement in statements]
        errors = list(evaluation["violations"])
        stable = data == repeated.get("data")
        if not stable:
            errors.append("repeated call changed the Answer v1 data payload")
        else:
            stable_cases += 1
        if data.get("answer_status") != case.get("expected_status"):
            errors.append(
                f"expected status {case.get('expected_status')!r}, "
                f"got {data.get('answer_status')!r}"
            )
        for verdict in case.get("expected_verdicts", []):
            if verdict not in verdicts:
                errors.append(f"expected verdict {verdict!r} was not returned")
        minimum_cited = int(case.get("minimum_cited_statements", 0))
        if cited_count < minimum_cited:
            errors.append(
                f"expected at least {minimum_cited} cited statements, got {cited_count}"
            )
        cited_ids = [
            str(locator["document_id"])
            for statement in statements
            for field in ("supporting_evidence", "contradicting_evidence")
            for locator in statement.get(field, [])
            if locator.get("cited") and locator.get("document_id")
        ]
        expected_ids = {str(item) for item in case.get("expected_document_ids", [])}
        forbidden_ids = {str(item) for item in case.get("forbidden_document_ids", [])}
        missing = expected_ids - set(cited_ids)
        unexpected = set(cited_ids) - expected_ids if expected_ids else set()
        forbidden = set(cited_ids) & forbidden_ids
        if missing:
            errors.append(f"missing expected evidence documents: {sorted(missing)}")
        if unexpected:
            errors.append(f"unexpected evidence documents: {sorted(unexpected)}")
        if forbidden:
            errors.append(f"forbidden evidence documents leaked: {sorted(forbidden)}")
        expected_visibility = case.get("expected_visibility")
        if expected_visibility is not None and any(
            locator.get("visibility") != expected_visibility
            for statement in statements
            for field in ("supporting_evidence", "contradicting_evidence")
            for locator in statement.get(field, [])
            if locator.get("cited")
        ):
            errors.append(f"expected all cited evidence to be {expected_visibility!r}")
        expected_quantitative = case.get("expected_quantitative")
        has_quantitative = any(
            isinstance(statement.get("quantitative_check"), dict)
            for statement in statements
        )
        if expected_quantitative is not None and has_quantitative != bool(
            expected_quantitative
        ):
            errors.append(
                f"expected quantitative={bool(expected_quantitative)}, "
                f"got {has_quantitative}"
            )
        expected_integrity = case.get("expected_integrity_status")
        if expected_integrity is not None and not any(
            statement.get("integrity", {}).get("status") == expected_integrity
            for statement in statements
        ):
            errors.append(f"expected integrity status {expected_integrity!r}")

        factual = [
            statement for statement in statements if statement.get("verdict") != "unverifiable"
        ]
        total_factual += len(factual)
        total_factual_cited += sum(1 for statement in factual if _cited(statement))
        total_evidence += len(cited_ids)
        expected_evidence_returned += sum(
            1 for document_id in cited_ids if document_id in expected_ids
        )
        if case.get("expected_status") == "refused":
            abstention_cases += 1
            if data.get("answer_status") == "refused":
                correct_abstentions += 1
        results.append(
            {
                "id": str(case["id"]),
                "passed": not errors,
                "errors": errors,
                "answer_status": data.get("answer_status"),
                "statement_count": len(statements),
                "cited_statement_count": cited_count,
                "evidence_precision": (
                    len([item for item in cited_ids if item in expected_ids])
                    / len(cited_ids)
                    if cited_ids and expected_ids
                    else (1.0 if not cited_ids else None)
                ),
                "deterministic": stable,
            }
        )

    passed = sum(1 for result in results if result["passed"])
    rate = passed / len(results) if results else 0.0
    citation_rate = total_factual_cited / total_factual if total_factual else 1.0
    precision = (
        expected_evidence_returned / total_evidence if total_evidence else 1.0
    )
    abstention_rate = (
        correct_abstentions / abstention_cases if abstention_cases else 1.0
    )
    stability_rate = stable_cases / len(results) if results else 0.0
    return analytic_envelope(
        n=len(results),
        method="deterministic Answer v1 fixture-case evaluation",
        assumptions=[
            "fixtures measure contract and retrieval regressions, not general answer quality",
            "pass rate is an exact descriptive fraction over the committed cases",
        ],
        passed=passed == len(results) and bool(results),
        passed_cases=passed,
        cases=results,
        pass_rate=interval(rate, rate, rate, 1.0),
        citation_coverage=interval(citation_rate, citation_rate, citation_rate, 1.0),
        evidence_precision=interval(precision, precision, precision, 1.0),
        abstention_correctness=interval(
            abstention_rate, abstention_rate, abstention_rate, 1.0
        ),
        deterministic_stability=interval(
            stability_rate, stability_rate, stability_rate, 1.0
        ),
    )
