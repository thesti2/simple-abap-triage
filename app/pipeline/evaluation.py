"""Classification eval: demo alerts + synthetic payloads, expected category per case.

Run locally:            uv run python -m app.pipeline.evaluation
Also track in LangSmith: uv run python -m app.pipeline.evaluation --langsmith
(dataset "abap-triage-classification", one experiment per run, metric "correct_category")
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.pipeline.classifier import Classifier, LLMClassifier
from app.schemas import AlertPayload

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
DATASET = "abap-triage-classification"
TARGET_ACCURACY = 0.9
_DEMO = {"div0": "DEVELOPER", "data": "FUNCTIONAL", "rfc": "BASIS"}
_DEFAULTS = {"source": "SAP", "system": "XYZ", "client": "100", "job_status": "CANCELLED", "job_user": "JOBUSER",
             "scheduled_by": "JOBUSER", "job_count": "10000001", "start_time": "2026-11-01T02:00:00",
             "end_time": "2026-11-01T02:01:00", "timezone": "UTC+8"}


@dataclass
class Case:
    id: str
    expected: str
    alert: AlertPayload


def load_cases() -> list[Case]:
    cases = [Case(f"demo_{n}", cat, AlertPayload.model_validate(
        json.loads((FIXTURES / f"alert_{n}.json").read_text(encoding="utf-8")))) for n, cat in _DEMO.items()]
    for raw in json.loads((FIXTURES / "eval_classification.json").read_text(encoding="utf-8")):
        data = {**_DEFAULTS, "alert_id": f"EVAL-{raw['id']}", **raw["alert"]}
        cases.append(Case(raw["id"], raw["expected"], AlertPayload.model_validate(data)))
    return cases


@dataclass
class Outcome:
    case: Case
    category: str
    confidence: float
    source: str

    @property
    def correct(self) -> bool:
        return self.category == self.case.expected


def run_eval(classifier: Classifier, cases: list[Case] | None = None) -> list[Outcome]:
    out = []
    for c in cases or load_cases():
        result, source = classifier(c.alert)
        out.append(Outcome(c, result.category, result.confidence, source))
    return out


def accuracy(outcomes: list[Outcome]) -> float:
    return sum(o.correct for o in outcomes) / len(outcomes) if outcomes else 0.0


def sync_langsmith_dataset(cases: list[Case]) -> str:
    """Create the dataset once; add cases whose id is not in it yet. Returns the dataset name."""
    from langsmith import Client

    client = Client()
    if client.has_dataset(dataset_name=DATASET):
        ds = client.read_dataset(dataset_name=DATASET)
        known = {(e.metadata or {}).get("case_id") for e in client.list_examples(dataset_id=ds.id)}
    else:
        ds = client.create_dataset(DATASET, description="ABAP triage: cancelled-job payload -> expected team")
        known = set()
    new = [c for c in cases if c.id not in known]
    if new:
        client.create_examples(
            dataset_id=ds.id,
            examples=[{"inputs": {"alert": c.alert.model_dump()}, "outputs": {"category": c.expected},
                       "metadata": {"case_id": c.id}} for c in new],
        )
    return DATASET


def run_langsmith(classifier: Classifier, cases: list[Case]) -> Any:
    from langsmith import evaluate

    def target(inputs: dict) -> dict:
        result, source = classifier(AlertPayload.model_validate(inputs["alert"]))
        return {"category": result.category, "confidence": result.confidence, "source": source}

    def correct_category(outputs: dict, reference_outputs: dict) -> bool:
        return outputs["category"] == reference_outputs["category"]

    return evaluate(target, data=sync_langsmith_dataset(cases), evaluators=[correct_category],
                    experiment_prefix="triage-classify", max_concurrency=4)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--langsmith", action="store_true", help="also run as LangSmith experiment on the dataset")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from app.llm import get_llm

    s = get_settings()
    clf = LLMClassifier(get_llm(s), method=s.structured_output_method)
    cases = load_cases()
    outcomes = run_eval(clf, cases)
    for o in outcomes:
        mark = "ok  " if o.correct else "MISS"
        print(f"{mark} {o.case.id:28} expected {o.case.expected:10} got {o.category:10} "
              f"({o.confidence:.2f}, {o.source})")
    acc = accuracy(outcomes)
    print(f"\naccuracy {acc:.0%} ({sum(o.correct for o in outcomes)}/{len(outcomes)}), target {TARGET_ACCURACY:.0%}")
    if args.langsmith:
        run_langsmith(clf, cases)
        print(f"LangSmith experiment recorded on dataset '{DATASET}'")
    return 0 if acc >= TARGET_ACCURACY else 1


if __name__ == "__main__":
    sys.exit(main())
