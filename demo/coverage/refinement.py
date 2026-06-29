from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import json
import re

from demo.config import (
    DEFAULT_COVERAGE_REFINEMENT_METRICS,
    DEFAULT_COVERAGE_THRESHOLD,
    DEFAULT_MAX_ITERATION_REFINEMENTS,
    DEFAULT_MAX_STAGNATION_ITERATIONS,
)
from demo.coverage.analyzer import CoverageAnalyzer, UncoveredMethod
from demo.coverage.maven import run_maven_report, run_maven_test_compile, run_maven_tests, strip_ansi
from demo.llm.ollama import ollama_generate
from demo.utils import ensure_junit_imports


RelatedSourcesProvider = Callable[[Dict], str]


class CoverageRefinement:
    def __init__(
        self,
        project_root: Path,
        demo_root: Path,
        model: str,
        java_version: str,
        junit_version: str,
        has_mockito: bool,
        ast_analysis: Optional[Dict],
        generated_paths: List[str],
        test_target_map: Dict[str, Dict],
        related_sources_provider: RelatedSourcesProvider,
        project_types_text: str,
        threshold: float = DEFAULT_COVERAGE_THRESHOLD,
        metrics: Tuple[str, ...] = DEFAULT_COVERAGE_REFINEMENT_METRICS,
        max_iterations: int = DEFAULT_MAX_ITERATION_REFINEMENTS,
        max_stagnation: int = DEFAULT_MAX_STAGNATION_ITERATIONS,
    ):
        self.project_root = project_root
        self.demo_root = demo_root
        self.model = model
        self.java_version = java_version
        self.junit_version = junit_version
        self.has_mockito = has_mockito
        self.ast_analysis = ast_analysis
        self.generated_paths = generated_paths
        self.test_target_map = test_target_map
        self.related_sources_provider = related_sources_provider
        self.project_types_text = project_types_text
        self.threshold = threshold
        self.metrics = tuple(metrics)
        self.max_iterations = max_iterations
        self.max_stagnation = max_stagnation
        self.log: List[Dict] = []
        (self.demo_root / "coverage_refinement").mkdir(parents=True, exist_ok=True)

    def run(self, xml_path: Path) -> Dict:
        analyzer = CoverageAnalyzer(xml_path, self.project_root, self.ast_analysis)
        previous = self._coverage_snapshot(analyzer)

        if self._threshold_reached(previous):
            return self._finish("Coverage threshold reached.", previous)

        stagnation = 0
        reason = ""
        current_xml = xml_path

        for iteration in range(1, self.max_iterations + 1):
            analyzer = CoverageAnalyzer(current_xml, self.project_root, self.ast_analysis)
            uncovered = analyzer.getUncoveredMethods()
            print(
                f"\nCoverage refinement iteration {iteration}/{self.max_iterations}: "
                f"{self._format_snapshot(previous)}"
            )

            appended = self._append_refinement_tests(iteration, uncovered, previous)
            if not appended:
                reason = "No uncovered methods with matching generated test classes."
                self.log.append(
                    {
                        "iteration": iteration,
                        "previous_coverage": previous,
                        "new_coverage": previous,
                        "improvement": self._zero_improvement(),
                        "action": "skipped",
                        "reason": reason,
                    }
                )
                break

            compile_log, compile_rc = run_maven_test_compile(self.project_root)
            self._write_iteration_log(iteration, "compile_log.txt", compile_log)
            if compile_rc != 0:
                self._rollback(appended)
                self.log.append(
                    {
                        "iteration": iteration,
                        "previous_coverage": previous,
                        "new_coverage": previous,
                        "improvement": self._zero_improvement(),
                        "action": "rolled_back_compile_failure",
                        "errors_tail": "\n".join(strip_ansi(compile_log).splitlines()[-80:]),
                    }
                )
                stagnation += 1
                self._restore_report_after_rollback(iteration)
                if stagnation >= self.max_stagnation:
                    reason = f"Coverage unchanged for {self.max_stagnation} consecutive iterations."
                    break
                continue

            test_log, test_rc = run_maven_tests(self.project_root)
            self._write_iteration_log(iteration, "test_log.txt", test_log)
            if test_rc != 0:
                self._rollback(appended)
                self.log.append(
                    {
                        "iteration": iteration,
                        "previous_coverage": previous,
                        "new_coverage": previous,
                        "improvement": self._zero_improvement(),
                        "action": "rolled_back_runtime_failure",
                        "errors_tail": "\n".join(strip_ansi(test_log).splitlines()[-80:]),
                    }
                )
                stagnation += 1
                self._restore_report_after_rollback(iteration)
                if stagnation >= self.max_stagnation:
                    reason = f"Coverage unchanged for {self.max_stagnation} consecutive iterations."
                    break
                continue

            report_log, report_rc = run_maven_report(self.project_root)
            self._write_iteration_log(iteration, "report_log.txt", report_log)
            latest_xml = self.project_root / "target" / "site" / "jacoco" / "jacoco.xml"
            if report_rc != 0 or not latest_xml.exists():
                self._rollback(appended)
                self._restore_report_after_rollback(iteration)
                self.log.append(
                    {
                        "iteration": iteration,
                        "previous_coverage": previous,
                        "new_coverage": previous,
                        "improvement": self._zero_improvement(),
                        "action": "rolled_back_report_failure",
                        "errors_tail": "\n".join(strip_ansi(report_log).splitlines()[-80:]),
                    }
                )
                stagnation += 1
                if stagnation >= self.max_stagnation:
                    reason = f"Coverage unchanged for {self.max_stagnation} consecutive iterations."
                    break
                continue

            current_xml = latest_xml
            new_analyzer = CoverageAnalyzer(current_xml, self.project_root, self.ast_analysis)
            new_coverage = self._coverage_snapshot(new_analyzer)
            improvement = self._coverage_improvement(previous, new_coverage)
            print(
                f"Coverage refinement iteration {iteration}: "
                f"previous=({self._format_snapshot(previous)}), "
                f"new=({self._format_snapshot(new_coverage)}), "
                f"improvement=({self._format_snapshot(improvement)})"
            )
            self.log.append(
                {
                    "iteration": iteration,
                    "previous_coverage": previous,
                    "new_coverage": new_coverage,
                    "improvement": improvement,
                    "action": "accepted",
                    "appended_files": [str(path) for path, _ in appended],
                    "uncovered_methods": [asdict(m) for m in uncovered[:12]],
                }
            )

            if self._threshold_reached(new_coverage):
                reason = "Coverage threshold reached."
                previous = new_coverage
                break
            if self._any_improvement(improvement):
                stagnation = 0
            else:
                stagnation += 1
                if stagnation >= self.max_stagnation:
                    reason = f"Coverage unchanged for {self.max_stagnation} consecutive iterations."
                    previous = new_coverage
                    break
            previous = new_coverage
        else:
            reason = f"Maximum refinement iterations reached ({self.max_iterations})."

        return self._finish(reason, previous)

    def _append_refinement_tests(
        self,
        iteration: int,
        uncovered: List[UncoveredMethod],
        coverage: Dict[str, float],
    ) -> List[Tuple[Path, str]]:
        uncovered = uncovered[:12]
        groups: Dict[Path, List[UncoveredMethod]] = {}
        for method in uncovered:
            test_path, target = self._test_file_for_method(method)
            if test_path is None or target is None:
                continue
            groups.setdefault(test_path, []).append(method)

        appended: List[Tuple[Path, str]] = []
        for test_path, methods in list(groups.items())[:3]:
            target = self.test_target_map.get(test_path.stem, {})
            prompt = self._build_prompt(iteration, test_path, target, methods[:6], coverage)

            prompt_path = (
                self.demo_root
                / "coverage_refinement"
                / f"iteration_{iteration}_prompt_{test_path.stem}.txt"
            )
            prompt_path.write_text(prompt, encoding="utf-8")
            
            methods_code = self._sanitize_methods(ollama_generate(self.model, prompt))
            if not methods_code.strip():
                continue
            original = test_path.read_text(encoding="utf-8", errors="ignore")
            marker = f"COVERAGE_REFINEMENT iteration {iteration}"
            block = f"\n    // BEGIN {marker}\n{self._indent_methods(methods_code)}\n    // END {marker}\n"
            updated = self._insert_before_final_brace(original, block)
            updated = ensure_junit_imports(updated, self.junit_version)
            test_path.write_text(updated, encoding="utf-8")
            appended.append((test_path, block))
            artifact = self.demo_root / "coverage_refinement" / f"{test_path.stem}_iteration_{iteration}.java"
            artifact.write_text(updated, encoding="utf-8")
        return appended

    def _build_prompt(
    self,
    iteration: int,
    test_path: Path,
    target: Dict,
    methods: List[UncoveredMethod],
    coverage: Dict[str, float],
    ) -> str:
        junit_rule = (
            "Use org.junit.Test and static org.junit.Assert assertions."
            if self.junit_version == "4"
            else "Use org.junit.jupiter.api.Test and org.junit.jupiter.api.Assertions assertions."
        )
        mockito_rule = (
            "Mockito is available. Prefer real objects. Never mock the class under test."
            if self.has_mockito
            else "Mockito is NOT available. Do not import or use any Mockito APIs."
        )
        uncovered_text = self._format_methods(methods)
        existing_names = ", ".join(self._existing_test_method_names(test_path)[:80])

        coverage_gap = {
            metric: max(0.0, self.threshold - coverage.get(metric, 0.0))
            for metric in self.metrics
        }
        prioritized_methods = uncovered_text  # assumed already sorted by _format_methods

        return f"""You are a Java test engineer. Your only job is to write new JUnit test methods that cover uncovered code.

### Environment
- Java version: {self.java_version}
- JUnit version: {self.junit_version} — {junit_rule}
- Mockito: {mockito_rule}

### Strict output rules (violations will cause a compile error)
1. Output ONLY bare Java test method bodies — no class declaration, no imports, no markdown, no explanation.
2. Every method MUST be annotated with @Test.
3. Every method MUST call real production code from the class under test.
4. Method names MUST start with: coverageRefinement{iteration}_
5. Do NOT reproduce or rename any of these existing methods: {existing_names or "(none)"}

### Coverage status
| Metric      | Current | Target | Gap |
|-------------|---------|--------|-----|
| Line        | {coverage.get("line", 0.0)*100:.1f}%  | {self.threshold*100:.1f}% | {coverage_gap.get("line", 0.0)*100:.1f}% |
| Instruction | {coverage.get("instruction", 0.0)*100:.1f}%  | {self.threshold*100:.1f}% | {coverage_gap.get("instruction", 0.0)*100:.1f}% |
| Branch      | {coverage.get("branch", 0.0)*100:.1f}%  | {self.threshold*100:.1f}% | {coverage_gap.get("branch", 0.0)*100:.1f}% |

Focus on closing the gaps above. Prioritize branch coverage — each uncovered branch requires a dedicated test.

### Uncovered code (highest priority first)
{prioritized_methods}

### Output format — one method per block, no blank lines between annotation and signature
@Test
public void coverageRefinement{iteration}_<descriptiveName>() {{
    // arrange
    // act
    // assert
}}
""".strip()

    def _format_methods(self, methods: List[UncoveredMethod]) -> str:
        blocks: List[str] = []
        for method in methods:
            statements = method.uncovered_statements[:4]
            statement_lines = []
            for index, statement in enumerate(statements, 1):
                context = " | ".join(statement.context[:5]) or statement.code
                statement_lines.extend(
                    [
                        f"Target {index}:",
                        f"- Missing behavior: {statement.behavior}",
                        f"- Behavior type: {statement.behavior_type}",
                        f"- Missed branches: {statement.missed_branches}",
                        f"- Minimal AST context: {context}",
                    ]
                )
            blocks.append(
                "\n".join(
                    [
                        f"Class: {method.display_class_name}",
                        f"Method signature: {method.signature or method.method_name}",
                        *statement_lines,
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _test_file_for_method(self, method: UncoveredMethod) -> Tuple[Optional[Path], Optional[Dict]]:
        for path_text in self.generated_paths:
            path = Path(path_text)
            target = self.test_target_map.get(path.stem, {})
            if target.get("class_name") == method.class_name and path.exists():
                return path, target
        return None, None

    def _sanitize_methods(self, text: str) -> str:
        text = (text or "").strip()
        fence = re.search(r"```(?:java)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        text = re.sub(r"(?m)^\s*(Here'?s|Below is|These tests).*?$", "", text, flags=re.IGNORECASE)
        class_match = re.search(r"\bclass\s+\w+\s*\{", text)
        if class_match:
            start = text.find("{", class_match.end() - 1)
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                text = text[start + 1 : end].strip()
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith(("package ", "import ")))
        return text.strip()

    def _indent_methods(self, code: str) -> str:
        lines = code.strip().splitlines()
        return "\n".join("    " + line if line.strip() else "" for line in lines)

    def _insert_before_final_brace(self, text: str, block: str) -> str:
        idx = text.rfind("}")
        if idx == -1:
            return text + block
        return text[:idx].rstrip() + block + "\n" + text[idx:]

    def _rollback(self, appended: List[Tuple[Path, str]]) -> None:
        for path, block in appended:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            path.write_text(text.replace(block, ""), encoding="utf-8")

    def _restore_report_after_rollback(self, iteration: int) -> None:
        test_log, _ = run_maven_tests(self.project_root)
        report_log, _ = run_maven_report(self.project_root)
        self._write_iteration_log(iteration, "rollback_test_log.txt", test_log)
        self._write_iteration_log(iteration, "rollback_report_log.txt", report_log)

    def _existing_test_method_names(self, test_path: Path) -> List[str]:
        try:
            text = test_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        return re.findall(r"\bvoid\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)

    def _write_iteration_log(self, iteration: int, name: str, content: str) -> None:
        path = self.demo_root / "coverage_refinement" / f"iteration_{iteration}_{name}"
        path.write_text(content or "", encoding="utf-8")

    def _coverage_snapshot(self, analyzer: CoverageAnalyzer) -> Dict[str, float]:
        return {
            "line": analyzer.getLineCoverage(),
            "instruction": analyzer.getInstructionCoverage(),
            "branch": analyzer.getBranchCoverage(),
        }

    def _threshold_reached(self, coverage: Dict[str, float]) -> bool:
        return all(coverage.get(metric, 0.0) >= self.threshold for metric in self.metrics)

    def _coverage_improvement(
        self, previous: Dict[str, float], current: Dict[str, float]
    ) -> Dict[str, float]:
        return {
            metric: current.get(metric, 0.0) - previous.get(metric, 0.0)
            for metric in self.metrics
        }

    def _zero_improvement(self) -> Dict[str, float]:
        return {metric: 0.0 for metric in self.metrics}

    def _any_improvement(self, improvement: Dict[str, float]) -> bool:
        return any(improvement.get(metric, 0.0) > 0.000001 for metric in self.metrics)

    def _format_snapshot(self, coverage: Dict[str, float]) -> str:
        return ", ".join(
            f"{metric}={coverage.get(metric, 0.0)*100:.2f}%"
            for metric in self.metrics
        )

    def _finish(self, reason: str, coverage: Dict[str, float]) -> Dict:
        result = {
            "reason": reason,
            "coverage": coverage,
            "threshold": self.threshold,
            "metrics": list(self.metrics),
            "max_iterations": self.max_iterations,
            "max_stagnation": self.max_stagnation,
            "iterations": self.log,
        }
        (self.demo_root / "coverage_refinement" / "coverage_refinement_log.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        print("\nCoverage refinement finished.")
        print(f"Reason: {reason}")
        print(f"Coverage: {self._format_snapshot(coverage)}")
        return result
