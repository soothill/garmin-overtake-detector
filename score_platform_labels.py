#!/usr/bin/env python3
"""Calculate candidate-universe precision/recall/F1 from reviewed events."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


PLATFORMS = ("gpu", "npu", "hailo")
YES = {"yes", "y", "true", "1", "pass"}
NO = {"no", "n", "false", "0", "not_pass"}


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def maximum_score(row: dict, platform: str) -> float | None:
    values = [
        float(value)
        for value in row[f"{platform}_max_confidences"].split(";")
        if value.strip()
    ]
    return max(values) if values else None


def calculate(labelled: list[tuple[dict, bool]], platform: str, threshold=None) -> dict:
    true_positive = false_positive = false_negative = 0
    for row, truth in labelled:
        score = maximum_score(row, platform)
        predicted = score is not None and (threshold is None or score >= threshold)
        if truth and predicted:
            true_positive += 1
        elif not truth and predicted:
            false_positive += 1
        elif truth and not predicted:
            false_negative += 1
    precision = ratio(true_positive, true_positive + false_positive)
    recall = ratio(true_positive, true_positive + false_negative)
    f1 = (
        round(2 * precision * recall / (precision + recall), 6)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "threshold": threshold,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-recall", type=float, default=0.95)
    args = parser.parse_args()

    with args.labels.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    labelled = []
    for row in rows:
        value = row["overtake_pass"].strip().lower()
        if value in YES:
            labelled.append((row, True))
        elif value in NO:
            labelled.append((row, False))

    scores = {}
    calibration = {}
    for platform in PLATFORMS:
        scores[platform] = calculate(labelled, platform)
        thresholds = sorted(
            {
                maximum_score(row, platform)
                for row, _ in labelled
                if maximum_score(row, platform) is not None
            }
        )
        sweep = [calculate(labelled, platform, threshold) for threshold in thresholds]
        valid_f1 = [item for item in sweep if item["f1"] is not None]
        best_f1 = max(
            valid_f1,
            key=lambda item: (item["f1"], item["recall"], item["precision"], item["threshold"]),
            default=None,
        )
        target = [
            item
            for item in sweep
            if item["recall"] is not None and item["recall"] >= args.target_recall
        ]
        target_choice = max(
            target,
            key=lambda item: (item["precision"], item["f1"], item["threshold"]),
            default=None,
        )
        calibration[platform] = {
            "best_f1": best_f1,
            "best_precision_at_target_recall": target_choice,
            "target_recall": args.target_recall,
            "evaluated_thresholds": len(sweep),
        }

    payload = {
        "schema_version": 1,
        "scope": (
            "all two-second platform disagreements plus a sampled unanimous set; "
            "not random-interval population accuracy"
        ),
        "selection_counts": dict(Counter(row["selection_reason"] for row in rows)),
        "review_rows": len(rows),
        "labelled_rows": len(labelled),
        "completion_fraction": ratio(len(labelled), len(rows)),
        "platforms": scores,
        "confidence_calibration": calibration,
        "threshold_warning": (
            "Threshold choices are exploratory because the same selected review set "
            "was used for scoring and calibration; validate on a different ride before "
            "changing deployment defaults."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
