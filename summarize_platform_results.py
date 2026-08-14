#!/usr/bin/env python3
"""Build a comparable table from GPU, NPU, and Hailo paired-video results."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path


def named_path(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition(":")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError("expected NAME:PATH")
    return name, Path(path)


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def event_records(result_path: Path) -> dict[str, list[dict]]:
    values = {}
    for camera in ("front", "rear"):
        path = result_path.parent / camera / "events.json"
        values[camera] = json.loads(path.read_text(encoding="utf-8"))
    return values


def match_event_pairs(
    reference: list[float], candidate: list[float], tolerance: float
) -> list[tuple[int, int, float]]:
    possible = sorted(
        (abs(first - second), first_index, second_index)
        for first_index, first in enumerate(reference)
        for second_index, second in enumerate(candidate)
        if abs(first - second) <= tolerance
    )
    used_reference: set[int] = set()
    used_candidate: set[int] = set()
    pairs = []
    for delta, first_index, second_index in possible:
        if first_index not in used_reference and second_index not in used_candidate:
            used_reference.add(first_index)
            used_candidate.add(second_index)
            pairs.append((first_index, second_index, delta))
    return pairs


def match_event_times(
    reference: list[float], candidate: list[float], tolerance: float
) -> int:
    """Return a one-to-one match count, retained for callers using schema v1."""
    return len(match_event_pairs(reference, candidate, tolerance))


def compact_event(event: dict) -> dict:
    """Keep enough evidence to review an unmatched event without copying sources."""
    return {
        "peak_time": float(event["peak_time"]),
        "class_name": event.get("class_name"),
        "max_confidence": event.get("max_confidence"),
        "duration": event.get("duration"),
        "side": event.get("side"),
    }


def three_platform_consensus(
    records: dict[str, list[dict]], reference: str, tolerance: float
) -> dict:
    """Build a two-of-three event consensus without treating one model as truth."""
    others = [name for name in records if name != reference]
    if len(others) != 2:
        return {}
    first, second = others
    times = {
        name: [float(item["peak_time"]) for item in items]
        for name, items in records.items()
    }
    reference_first = match_event_pairs(times[reference], times[first], tolerance)
    reference_second = match_event_pairs(times[reference], times[second], tolerance)
    supported_reference = {item[0] for item in reference_first} | {
        item[0] for item in reference_second
    }
    matched_first = {item[1] for item in reference_first}
    matched_second = {item[1] for item in reference_second}
    remaining_first = [
        value for index, value in enumerate(times[first]) if index not in matched_first
    ]
    remaining_second = [
        value
        for index, value in enumerate(times[second])
        if index not in matched_second
    ]
    non_reference_pairs = match_event_pairs(
        remaining_first, remaining_second, tolerance
    )
    consensus_count = len(supported_reference) + len(non_reference_pairs)
    supported = {
        reference: len(supported_reference),
        first: len(reference_first) + len(non_reference_pairs),
        second: len(reference_second) + len(non_reference_pairs),
    }
    return {
        "definition": "an event found by at least two of the three platforms",
        "consensus_events": consensus_count,
        "reference_events_supported_by_another_platform": len(supported_reference),
        "reference_only_events": len(records[reference]) - len(supported_reference),
        "events_found_by_both_non_reference_platforms_but_not_reference": len(
            non_reference_pairs
        ),
        "platform_consensus_coverage": {
            name: round(value / consensus_count, 6) if consensus_count else None
            for name, value in supported.items()
        },
        "platform_supported_events": supported,
        "platform_candidate_confirmation": {
            name: round(supported[name] / len(records[name]), 6)
            if records[name]
            else None
            for name in records
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", action="append", required=True, type=named_path)
    parser.add_argument("--power", action="append", default=[], type=named_path)
    parser.add_argument("--health", action="append", default=[], type=named_path)
    parser.add_argument("--reference", default="gpu")
    parser.add_argument("--event-tolerance", type=float, default=2.0)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-csv", required=True, type=Path)
    args = parser.parse_args()

    power_paths = dict(args.power)
    health_paths = dict(args.health)
    result_paths = dict(args.result)
    if args.reference not in result_paths:
        raise SystemExit(f"reference result is unavailable: {args.reference}")
    reference_records = event_records(result_paths[args.reference])
    reference_events = {
        camera: [float(item["peak_time"]) for item in records]
        for camera, records in reference_records.items()
    }
    rows = []
    event_agreement = {}
    all_event_records = {}
    for name, result_path in args.result:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        power = None
        if name in power_paths and power_paths[name].exists():
            power = json.loads(power_paths[name].read_text(encoding="utf-8"))
        health = None
        if name in health_paths and health_paths[name].exists():
            health = json.loads(health_paths[name].read_text(encoding="utf-8"))
        idle_gate = None
        idle_gate_path = result_path.parent / "idle-gate.json"
        if idle_gate_path.exists():
            idle_gate = json.loads(idle_gate_path.read_text(encoding="utf-8"))
        source_hours = result["timing"]["total_source_seconds"] / 3600.0
        candidate_records = event_records(result_path)
        all_event_records[name] = candidate_records
        candidate_events = {
            camera: [float(item["peak_time"]) for item in records]
            for camera, records in candidate_records.items()
        }
        pairs = {
            camera: match_event_pairs(
                reference_events[camera], candidate_events[camera], args.event_tolerance
            )
            for camera in ("front", "rear")
        }
        matched = {camera: len(items) for camera, items in pairs.items()}
        matched_total = sum(matched.values())
        reference_total = sum(len(items) for items in reference_events.values())
        candidate_total = sum(len(items) for items in candidate_events.values())
        agreement_by_camera = {}
        for camera in ("front", "rear"):
            matched_reference = {item[0] for item in pairs[camera]}
            matched_candidate = {item[1] for item in pairs[camera]}
            deltas = [item[2] for item in pairs[camera]]
            class_matches = sum(
                reference_records[camera][first].get("class_name")
                == candidate_records[camera][second].get("class_name")
                for first, second, _ in pairs[camera]
            )
            agreement_by_camera[camera] = {
                "reference_events": len(reference_records[camera]),
                "candidate_events": len(candidate_records[camera]),
                "matched_events": len(pairs[camera]),
                "reference_coverage": round(
                    len(pairs[camera]) / len(reference_records[camera]), 6
                )
                if reference_records[camera]
                else None,
                "candidate_confirmation": round(
                    len(pairs[camera]) / len(candidate_records[camera]), 6
                )
                if candidate_records[camera]
                else None,
                "mean_absolute_peak_delta_seconds": round(sum(deltas) / len(deltas), 6)
                if deltas
                else None,
                "maximum_absolute_peak_delta_seconds": round(max(deltas), 6)
                if deltas
                else None,
                "class_agreement": round(class_matches / len(pairs[camera]), 6)
                if pairs[camera]
                else None,
                "unmatched_reference": [
                    compact_event(item)
                    for index, item in enumerate(reference_records[camera])
                    if index not in matched_reference
                ],
                "unmatched_candidate": [
                    compact_event(item)
                    for index, item in enumerate(candidate_records[camera])
                    if index not in matched_candidate
                ],
            }
        event_agreement[name] = agreement_by_camera
        row = {
            "platform": name,
            "backend": result["backend"],
            "model": result["detector"]["model"],
            "precision": result["detector"].get("precision"),
            "source_hours": round(source_hours, 6),
            "wall_minutes": round(result["timing"]["total_wall_seconds"] / 60.0, 6),
            "realtime_factor": result["timing"]["realtime_factor"],
            "front_candidate_events": result["quality"]["front_candidate_events"],
            "rear_candidate_events": result["quality"]["rear_candidate_events"],
            "candidate_events": result["quality"]["total_candidate_events"],
            "candidate_events_per_source_hour": round(
                candidate_total / source_hours, 6
            ),
            "events_matching_reference": matched_total,
            "front_events_matching_reference": matched["front"],
            "rear_events_matching_reference": matched["rear"],
            "front_reference_event_coverage": agreement_by_camera["front"][
                "reference_coverage"
            ],
            "rear_reference_event_coverage": agreement_by_camera["rear"][
                "reference_coverage"
            ],
            "front_candidate_confirmation": agreement_by_camera["front"][
                "candidate_confirmation"
            ],
            "rear_candidate_confirmation": agreement_by_camera["rear"][
                "candidate_confirmation"
            ],
            "reference_event_coverage": round(matched_total / reference_total, 6)
            if reference_total
            else None,
            "events_not_in_reference": candidate_total - matched_total,
            "candidate_confirmation": round(matched_total / candidate_total, 6)
            if candidate_total
            else None,
            "vehicle_detections": (
                result["quality"]["front_vehicle_detections"]
                + result["quality"]["rear_vehicle_detections"]
            ),
            "consensus_supported_events": None,
            "consensus_event_coverage": None,
            "wall_seconds_per_consensus_supported_event": None,
            "wh_per_consensus_supported_event": None,
            "valid": result["valid"],
            "hardware_health_valid": health.get("valid") if health else None,
            "idle_gate_valid": idle_gate.get("valid") if idle_gate else None,
            "power_available": bool(power and power.get("available")),
            "power_evidence_valid": bool(
                power
                and power.get("available")
                and (
                    result["backend"] not in {"gpu", "npu"}
                    or (idle_gate and idle_gate.get("valid"))
                )
                and (not health or health.get("valid"))
            ),
            "power_scope": None,
            "mean_watts": None,
            "energy_wh": None,
            "wh_per_source_hour": None,
            "source_hours_per_kwh": None,
            "incremental_mean_watts": None,
            "incremental_energy_wh": None,
            "incremental_wh_per_source_hour": None,
            "incremental_source_hours_per_kwh": None,
            "power_coverage": power.get("time_coverage_fraction") if power else None,
        }
        if power and power.get("metrics"):
            preferred = (
                "whole_system_wall",
                "system_package",
                "npu" if result["backend"] == "npu" else "gpu",
                "hailo_module",
                "pi_pmic_output_rails",
            )
            scope = next((item for item in preferred if item in power["metrics"]), None)
            if scope:
                metric = power["metrics"][scope]
                row.update(
                    {
                        "power_scope": scope,
                        "mean_watts": metric["mean_watts"],
                        "energy_wh": metric["gross_energy_wh"],
                        "wh_per_source_hour": round(
                            metric["gross_energy_wh"] / source_hours, 6
                        ),
                        "source_hours_per_kwh": (
                            round(source_hours * 1000.0 / metric["gross_energy_wh"], 6)
                            if metric["gross_energy_wh"]
                            else None
                        ),
                        "incremental_mean_watts": metric.get("incremental_mean_watts"),
                        "incremental_energy_wh": metric.get("incremental_energy_wh"),
                        "incremental_wh_per_source_hour": (
                            round(metric["incremental_energy_wh"] / source_hours, 6)
                            if metric.get("incremental_energy_wh") is not None
                            else None
                        ),
                        "incremental_source_hours_per_kwh": (
                            round(
                                source_hours * 1000.0 / metric["incremental_energy_wh"],
                                6,
                            )
                            if metric.get("incremental_energy_wh")
                            else None
                        ),
                    }
                )
        rows.append(row)

    scopes = {row["power_scope"] for row in rows if row["power_scope"]}
    complete_power = len(rows) >= 2 and all(row["power_evidence_valid"] for row in rows)
    comparable_power = complete_power and len(scopes) == 1
    ranking = []
    if comparable_power:
        ranking = [
            row["platform"]
            for row in sorted(rows, key=lambda item: item["wh_per_source_hour"])
        ]
    consensus = {}
    if len(all_event_records) == 3:
        consensus = {
            camera: three_platform_consensus(
                {name: records[camera] for name, records in all_event_records.items()},
                args.reference,
                args.event_tolerance,
            )
            for camera in ("front", "rear")
        }
        totals = {
            name: sum(
                consensus[camera]["platform_supported_events"][name]
                for camera in ("front", "rear")
            )
            for name in all_event_records
        }
        consensus_total = sum(
            consensus[camera]["consensus_events"] for camera in ("front", "rear")
        )
        consensus["total"] = {
            "consensus_events": consensus_total,
            "platform_supported_events": totals,
            "platform_consensus_coverage": {
                name: round(value / consensus_total, 6) if consensus_total else None
                for name, value in totals.items()
            },
        }
        rows_by_name = {row["platform"]: row for row in rows}
        for name, supported_count in totals.items():
            row = rows_by_name[name]
            row["consensus_supported_events"] = supported_count
            row["consensus_event_coverage"] = (
                round(supported_count / consensus_total, 6) if consensus_total else None
            )
            row["wall_seconds_per_consensus_supported_event"] = (
                round(row["wall_minutes"] * 60.0 / supported_count, 6)
                if supported_count
                else None
            )
            row["wh_per_consensus_supported_event"] = (
                round(row["energy_wh"] / supported_count, 6)
                if row["energy_wh"] is not None and supported_count
                else None
            )
    payload = {
        "schema_version": 2,
        "comparison_valid": all(
            row["valid"] and row["hardware_health_valid"] is not False for row in rows
        ),
        "quality_reference": args.reference,
        "event_match_tolerance_seconds": args.event_tolerance,
        "power_comparison_valid": comparable_power,
        "power_comparison_note": (
            "All compared runs have the same measurement boundary."
            if comparable_power
            else "A definitive energy winner requires power data with the same boundary for every compared platform."
        ),
        "efficiency_ranking": ranking,
        "quality_interpretation": (
            "Event coverage and confirmation are agreement measures against the selected "
            "reference, not independently labelled precision or recall. Unmatched event "
            "evidence is included for human review."
        ),
        "two_of_three_consensus": consensus,
        "event_agreement": event_agreement,
        "results": rows,
    }
    atomic_text(args.output_json, json.dumps(payload, indent=2) + "\n")
    columns = list(rows[0]) if rows else []
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(args.output_csv, buffer.getvalue())
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
