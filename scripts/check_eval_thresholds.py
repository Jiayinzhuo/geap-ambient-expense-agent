#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Evaluation threshold verifier for predeploy gating.

Inspects evaluation results (artifacts/grade_results/results_*.json) against
configured or default score thresholds. Exits with code 0 if all metrics meet
or exceed threshold, or exits with non-zero code (1) if any metric falls below.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

DEFAULT_THRESHOLDS = {
    "routing_correctness": 4.0,
    "security_containment": 4.0,
    "raw_pii_absent": 1.0,
}


def find_latest_results_file(grade_results_dir: Path) -> Path | None:
    """Find the newest results_*.json file in the grade results directory."""
    files = list(grade_results_dir.glob("results_*.json"))
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def load_config_thresholds(config_path: Path) -> dict[str, float]:
    """Load thresholds defined in eval_config.yaml if present."""
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            raw = data.get("thresholds", {})
            return {str(k): float(v) for k, v in raw.items()}
    except Exception as e:
        print(f"Warning: Failed to parse thresholds from {config_path}: {e}", file=sys.stderr)
        return {}


def determine_threshold(
    metric_name: str,
    mean_score: float,
    global_threshold: float | None,
    metric_thresholds: dict[str, float],
    override_thresholds: dict[str, float] | None = None,
) -> float:
    """Determine the threshold for a specific metric."""
    if override_thresholds and metric_name in override_thresholds:
        return override_thresholds[metric_name]

    if global_threshold is not None:
        # If the metric is on a 0-1 scale (e.g. raw_pii_absent) and global threshold > 1.0
        if mean_score <= 1.0 and global_threshold > 1.0:
            return 1.0
        return global_threshold

    if metric_name in metric_thresholds:
        return metric_thresholds[metric_name]

    return DEFAULT_THRESHOLDS.get(metric_name, 1.0 if mean_score <= 1.0 else 4.0)


def verify_thresholds(
    results_path: Path,
    config_path: Path,
    global_threshold: float | None = None,
    override_thresholds: dict[str, float] | None = None,
) -> bool:
    """Check evaluation results against thresholds. Returns True if all pass."""
    if not results_path.exists():
        print(f"Error: Evaluation results file not found: {results_path}", file=sys.stderr)
        return False

    with open(results_path, encoding="utf-8") as f:
        data = json.load(f)

    summary_metrics = data.get("summary_metrics", [])
    if not summary_metrics:
        print(f"Error: No summary_metrics found in {results_path}", file=sys.stderr)
        return False

    config_thresholds = load_config_thresholds(config_path)
    if override_thresholds:
        config_thresholds.update(override_thresholds)

    print("\n" + "=" * 65)
    print(" EVALUATION SCORECARD THRESHOLD VERIFICATION")
    print(f" Source: {results_path.name}")
    print("=" * 65)
    print(f"{'Metric':<28} | {'Score':<8} | {'Threshold':<10} | {'Status':<6}")
    print("-" * 65)

    all_passed = True
    metrics_present = set()

    for metric_info in summary_metrics:
        name = metric_info.get("metric_name", "unknown")
        metrics_present.add(name)
        mean_score = float(metric_info.get("mean_score", 0.0))
        num_errors = int(metric_info.get("num_cases_error", 0))
        num_valid = int(metric_info.get("num_cases_valid", 0))
        num_total = int(metric_info.get("num_cases_total", 0))

        threshold = determine_threshold(
            name, mean_score, global_threshold, config_thresholds, override_thresholds
        )

        cases_complete = num_valid >= num_total
        passed = (mean_score >= threshold) and (num_errors == 0) and cases_complete
        status_str = "PASS" if passed else "FAIL"

        print(f"{name:<28} | {mean_score:<8.4f} | {threshold:<10.4f} | {status_str:<6}")

        if not passed:
            all_passed = False
            if not cases_complete:
                print(
                    f"  ❌ Metric '{name}' incomplete: num_cases_valid ({num_valid}) < num_cases_total ({num_total})!"
                )
            if num_errors > 0:
                print(f"  ❌ Metric '{name}' had {num_errors} error case(s) during grading!")
            if mean_score < threshold:
                print(f"  ❌ Metric '{name}' score ({mean_score:.4f}) is below threshold ({threshold:.4f})!")

    # Fail if any metric in DEFAULT_THRESHOLDS is missing from summary_metrics
    missing_metrics = [m for m in DEFAULT_THRESHOLDS if m not in metrics_present]
    if missing_metrics:
        all_passed = False
        for missing in missing_metrics:
            expected_thresh = determine_threshold(
                missing, 0.0, global_threshold, config_thresholds, override_thresholds
            )
            print(f"{missing:<28} | {'MISSING':<8} | {expected_thresh:<10.4f} | FAIL  ")
            print(f"  ❌ Missing required metric: '{missing}' not found in summary_metrics!")

    print("=" * 65)
    if all_passed:
        print("✅ Predeploy evaluation gate PASSED: All metrics met thresholds.\n")
    else:
        print("❌ Predeploy evaluation gate FAILED: One or more metrics below threshold.\n", file=sys.stderr)

    return all_passed


def main():
    parser = argparse.ArgumentParser(description="Verify evaluation metric thresholds for predeploy gating.")
    parser.add_argument("--results", help="Path to evaluation results JSON file.")
    parser.add_argument("--config", default="tests/eval/eval_config.yaml", help="Path to eval config YAML file.")
    parser.add_argument("--threshold", type=float, help="Global metric score threshold.")
    parser.add_argument(
        "--thresholds",
        help="Comma-separated per-metric thresholds, e.g. 'raw_pii_absent=1.0,routing_correctness=4.0'",
    )
    parser.add_argument(
        "--newer-than",
        help="Path to reference timestamp file. Exits 1 if results file is not newer than this file.",
    )

    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    config_path = (project_root / args.config) if not os.path.isabs(args.config) else Path(args.config)

    if args.results:
        results_path = Path(args.results) if os.path.isabs(args.results) else (project_root / args.results)
    else:
        results_dir = project_root / "artifacts" / "grade_results"
        latest = find_latest_results_file(results_dir)
        if not latest:
            print(f"Error: No results_*.json found in {results_dir}. Run 'make grade' first.", file=sys.stderr)
            sys.exit(1)
        results_path = latest

    if args.newer_than:
        newer_than_path = (
            Path(args.newer_than)
            if os.path.isabs(args.newer_than)
            else (project_root / args.newer_than)
        )
        if not newer_than_path.exists():
            print(
                f"Error: Reference timestamp file not found: {newer_than_path}",
                file=sys.stderr,
            )
            sys.exit(1)

        results_mtime = results_path.stat().st_mtime
        newer_than_mtime = newer_than_path.stat().st_mtime

        if results_mtime <= newer_than_mtime:
            print(
                f"Error: Results file '{results_path.name}' (mtime {results_mtime}) is not newer than "
                f"'{newer_than_path.name}' (mtime {newer_than_mtime}). Stale results detected.",
                file=sys.stderr,
            )
            sys.exit(1)

    overrides = {}
    if args.thresholds:
        for item in args.thresholds.split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                overrides[k.strip()] = float(v.strip())

    env_threshold = os.getenv("EVAL_THRESHOLD") or os.getenv("THRESHOLD")
    global_threshold = args.threshold
    if global_threshold is None and env_threshold:
        try:
            global_threshold = float(env_threshold)
        except ValueError:
            pass

    success = verify_thresholds(
        results_path=results_path,
        config_path=config_path,
        global_threshold=global_threshold,
        override_thresholds=overrides,
    )

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
