from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Dict, List, Tuple

from demo.static_analysis import run_ast_analysis


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Benchmark two AST analyzer JAR variants and print timing/output size")
    ap.add_argument("--project-root", required=True, help="Path to the Gradle/Maven project root")
    ap.add_argument("--jar-a", required=True, help="Path to the first analyzer JAR")
    ap.add_argument("--jar-b", required=True, help="Path to the second analyzer JAR")
    ap.add_argument("--name-a", default="serial", help="Label for the first analyzer run")
    ap.add_argument("--name-b", default="parallel", help="Label for the second analyzer run")
    ap.add_argument("--output-root", default="./ast-benchmark", help="Directory where per-run outputs will be stored")
    ap.add_argument(
        "--modern-a",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Whether analyzer A supports --output-dir/--threads/--batch-size/--ast-tree",
    )
    ap.add_argument(
        "--modern-b",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether analyzer B supports --output-dir/--threads/--batch-size/--ast-tree",
    )
    ap.add_argument(
        "--shards-a",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Write package shards for analyzer A when it supports modern flags",
    )
    ap.add_argument(
        "--shards-b",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Write package shards for analyzer B when it supports modern flags",
    )
    ap.add_argument("--threads-a", type=int, default=1, help="Threads used for run A")
    ap.add_argument("--threads-b", type=int, default=4, help="Threads used for run B")
    ap.add_argument("--batch-size", type=int, default=50, help="Batch size for the analyzer")
    ap.add_argument("--ast-tree", choices=["none", "summary", "full"], default="summary", help="AST tree detail level")
    ap.add_argument(
        "--full-output",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write analysis.json for modern analyzers too; use --no-full-output for shard-only modern runs",
    )
    return ap.parse_args()


def run_benchmark_case(
    project_root: Path,
    analyzer_jar: Path,
    label: str,
    output_root: Path,
    threads: int,
    batch_size: int,
    ast_tree: str,
    full_output: bool,
    modern_flags: bool,
    write_shards: bool,
) -> Dict:
    run_dir = output_root / label
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / "analysis.json"
    output_dir = run_dir / "shards"
    metrics_path = run_dir / "metrics.json"

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_path.unlink(missing_ok=True)
    metrics_path.unlink(missing_ok=True)

    if modern_flags:
        run_ast_analysis(
            project_root=project_root,
            output_path=output_path,
            analyzer_jar=analyzer_jar,
            output_dir=output_dir if write_shards else None,
            threads=threads,
            batch_size=batch_size,
            ast_tree=ast_tree,
            full_output=full_output,
            metrics_output_path=metrics_path,
        )
    else:
        run_ast_analysis(
            project_root=project_root,
            output_path=output_path,
            analyzer_jar=analyzer_jar,
            output_dir=None,
            threads=None,
            batch_size=None,
            ast_tree=None,
            full_output=True,
            metrics_output_path=metrics_path,
        )

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    return {
        "label": label,
        "jar": str(analyzer_jar),
        "run_dir": str(run_dir),
        "metrics": metrics,
    }


def print_summary(result: Dict) -> None:
    metrics = result["metrics"]
    timing = metrics.get("timing", {})
    input_metrics = metrics.get("input", {})
    output_metrics = metrics.get("output", {})
    print(f"[{result['label']}]")
    print(f"  jar: {result['jar']}")
    print(f"  run_dir: {result['run_dir']}")
    print(f"  elapsed_seconds: {timing.get('elapsed_seconds', 0)}")
    print(f"  execution_time: {timing.get('elapsed_seconds', 0):.6f} seconds")
    print(f"  java_files: {input_metrics.get('java_file_count', 0)}")
    print(f"  java_input_bytes: {input_metrics.get('java_bytes', 0)}")
    print(f"  output_shards: {output_metrics.get('shard_file_count', 0)}")
    print(f"  total_output_bytes: {output_metrics.get('total_output_bytes', 0)}")
    print(f"  total_output_mb: {output_metrics.get('total_output_bytes', 0) / (1024 * 1024):.3f}")
    print(f"  sharded_output: {output_metrics.get('sharded_output', False)}")
    print(f"  class_count: {(metrics.get('analysis') or {}).get('class_count', 0)}")
    for entry in output_metrics.get("files", []):
        print(f"    file: {entry.get('path')} size_bytes: {entry.get('size_bytes', 0)}")


def print_comparison(results: List[Dict]) -> None:
    if len(results) != 2:
        return
    first, second = results
    first_metrics = first["metrics"]
    second_metrics = second["metrics"]
    first_time = (first_metrics.get("timing") or {}).get("elapsed_seconds", 0) or 0
    second_time = (second_metrics.get("timing") or {}).get("elapsed_seconds", 0) or 0
    first_size = (first_metrics.get("output") or {}).get("total_output_bytes", 0) or 0
    second_size = (second_metrics.get("output") or {}).get("total_output_bytes", 0) or 0

    print("[comparison]")
    if first_time > 0 and second_time > 0:
        speedup = first_time / second_time
        saved = first_time - second_time
        print(f"  {second['label']}_vs_{first['label']}_speedup: {speedup:.3f}x")
        print(f"  seconds_saved: {saved:.6f}")
    print(f"  {first['label']}_output_bytes: {first_size}")
    print(f"  {second['label']}_output_bytes: {second_size}")
    print(f"  output_size_delta_bytes: {second_size - first_size}")


def main() -> None:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    results = [
        run_benchmark_case(
            project_root=project_root,
            analyzer_jar=Path(args.jar_a).resolve(),
            label=args.name_a,
            output_root=output_root,
            threads=args.threads_a,
            batch_size=args.batch_size,
            ast_tree=args.ast_tree,
            full_output=args.full_output,
            modern_flags=args.modern_a,
            write_shards=args.shards_a,
        ),
        run_benchmark_case(
            project_root=project_root,
            analyzer_jar=Path(args.jar_b).resolve(),
            label=args.name_b,
            output_root=output_root,
            threads=args.threads_b,
            batch_size=args.batch_size,
            ast_tree=args.ast_tree,
            full_output=args.full_output,
            modern_flags=args.modern_b,
            write_shards=args.shards_b,
        ),
    ]

    print("AST analyzer benchmark")
    for result in results:
        print_summary(result)
    print_comparison(results)


if __name__ == "__main__":
    main()
