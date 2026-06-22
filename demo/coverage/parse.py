from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from demo.config import GENERATED_PREFIX


def parse_jacoco_xml(xml_path: Path) -> Dict[str, float]:
    from demo.coverage.analyzer import CoverageAnalyzer

    project_root = xml_path.parents[3] if len(xml_path.parents) > 3 else xml_path.parent
    analyzer = CoverageAnalyzer(xml_path=xml_path, project_root=project_root)
    return {
        "line_coverage": analyzer.getLineCoverage(),
        "instruction_coverage": analyzer.getInstructionCoverage(),
        "branch_coverage": analyzer.getBranchCoverage(),
    }


def parse_surefire_summary(log: str) -> Optional[Dict[str, int]]:
    m = re.search(
        r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)",
        log,
    )
    if not m:
        return None
    return {
        "tests_run": int(m.group(1)),
        "failures": int(m.group(2)),
        "errors": int(m.group(3)),
        "skipped": int(m.group(4)),
    }


def parse_surefire_reports(reports_dir: Path) -> Optional[Dict[str, int]]:
    if not reports_dir.exists():
        return None
    import xml.etree.ElementTree as ET

    totals = {"tests_run": 0, "failures": 0, "errors": 0, "skipped": 0}
    any_found = False
    for xml_path in reports_dir.glob("TEST-*.xml"):
        try:
            tree = ET.parse(str(xml_path))
        except ET.ParseError:
            continue
        root = tree.getroot()
        if root.tag != "testsuite":
            continue
        any_found = True
        totals["tests_run"] += int(root.attrib.get("tests", "0"))
        totals["failures"] += int(root.attrib.get("failures", "0"))
        totals["errors"] += int(root.attrib.get("errors", "0"))
        totals["skipped"] += int(root.attrib.get("skipped", "0"))
    return totals if any_found else None


def extract_runtime_failures(reports_dir: Path) -> List[Dict[str, str]]:
    if not reports_dir.exists():
        return []
    import xml.etree.ElementTree as ET

    failures: Dict[tuple[str, str], str] = {}
    for xml_path in reports_dir.glob("TEST-*.xml"):
        try:
            tree = ET.parse(str(xml_path))
        except ET.ParseError:
            continue
        root = tree.getroot()
        for case in root.findall(".//testcase"):
            classname = case.attrib.get("classname", "")
            method_name = case.attrib.get("name", "")
            if GENERATED_PREFIX not in classname or not classname.endswith("Test"):
                continue
            failure = case.find("failure")
            error = case.find("error")
            node = failure if failure is not None else error
            if node is None:
                continue
            text = (node.text or "").strip()
            key = (classname, method_name)
            if key not in failures:
                failures[key] = text
    return [
        {"class_name": class_name, "method_name": method_name, "stack_trace": stack_trace}
        for (class_name, method_name), stack_trace in failures.items()
    ]
