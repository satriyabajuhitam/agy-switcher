#!/usr/bin/env python3
"""
scripts/internal/verify_spec.py
Core zero-dependency specification engine & brownfield delta reconciler for Antigravity Plugins.
Implements AST parsing, validation, Additive Scenario Merging, idempotent reconciliation,
coverage mapping, and declarative workflow DAG phase verification using Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union


@dataclass
class Scenario:
    """Represents a Given/When/Then behavioral scenario in a specification."""
    name: str
    tag: Optional[str] = None
    steps: List[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def raw_title(self) -> str:
        if self.tag:
            return f"[{self.tag}] {self.name}"
        return self.name


@dataclass
class Requirement:
    """Represents a modular behavioral requirement containing one or more scenarios."""
    name: str
    description: str = ""
    scenarios: List[Scenario] = field(default_factory=list)
    raw_text: str = ""


@dataclass
class SpecAST:
    """Abstract Syntax Tree representing a Living or Delta Specification."""
    spec_type: str  # "delta" or "living"
    title: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    context: str = ""
    added_requirements: List[Requirement] = field(default_factory=list)
    modified_requirements: List[Requirement] = field(default_factory=list)
    removed_requirements: List[Requirement] = field(default_factory=list)
    requirements: List[Requirement] = field(default_factory=list)  # Living spec baseline
    raw_content: str = ""


class SpecASTParser:
    """Parses markdown specifications into structured SpecAST representations."""

    SCENARIO_HEADING_RE = re.compile(r"^####\s+Scenario:\s*(?:\[([A-Za-z0-9_-]+)\])?\s*(.+)$", re.IGNORECASE)
    REQUIREMENT_HEADING_RE = re.compile(r"^###\s+Requirement:\s*(.+)$", re.IGNORECASE)
    SECTION_HEADING_RE = re.compile(r"^##\s+(.+)$")
    HEADER_LINE_RE = re.compile(r"^([A-Za-z0-9 _-]+):\s*(.+)$")

    def parse_file(self, file_path: Path | str) -> SpecAST:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Specification file not found: {path}")
        content = path.read_text(encoding="utf-8")
        return self.parse_string(content)

    def parse_string(self, content: str) -> SpecAST:
        lines = content.splitlines()
        spec_type = "living"
        title = ""
        headers: Dict[str, str] = {}
        context_lines: List[str] = []

        added_requirements: List[Requirement] = []
        modified_requirements: List[Requirement] = []
        removed_requirements: List[Requirement] = []
        living_requirements: List[Requirement] = []

        # First pass: identify title and determine default spec_type
        for line in lines:
            line_s = line.strip()
            if line_s.startswith("# Spec Delta:"):
                spec_type = "delta"
                title = line_s[len("# Spec Delta:"):].strip()
                break
            elif line_s.startswith("# Spec:"):
                spec_type = "living"
                title = line_s[len("# Spec:"):].strip()
                break

        # Check section headers for delta signals
        for line in lines:
            line_s = line.strip()
            if re.match(r"^##\s+(?:ADDED|MODIFIED|REMOVED)\s+Requirements", line_s, re.IGNORECASE):
                spec_type = "delta"
                break

        current_section: Optional[str] = None
        current_req: Optional[Requirement] = None
        current_scenario: Optional[Scenario] = None
        req_desc_lines: List[str] = []
        req_list_ref: Optional[List[Requirement]] = None
        in_header_block = True

        for line in lines:
            stripped = line.strip()

            # Ignore top-level title
            if stripped.startswith("# ") and not stripped.startswith("## "):
                continue

            # Header metadata (Target Domain, Target Spec, Domain, Status, etc.)
            if in_header_block:
                if stripped.startswith("## "):
                    in_header_block = False
                else:
                    m = self.HEADER_LINE_RE.match(stripped)
                    if m:
                        headers[m.group(1).strip()] = m.group(2).strip()
                        continue

            # Major sections (## ...)
            m_sec = self.SECTION_HEADING_RE.match(stripped)
            if m_sec:
                # Finalize previous requirement and scenario
                if current_scenario and current_req:
                    current_req.scenarios.append(current_scenario)
                    current_scenario = None
                if current_req and req_list_ref is not None:
                    if req_desc_lines:
                        current_req.description = "\n".join(req_desc_lines).strip()
                    req_list_ref.append(current_req)
                    current_req = None
                    req_desc_lines = []

                raw_sec_name = m_sec.group(1).strip()
                sec_upper = raw_sec_name.upper()

                if "ADDED REQUIREMENTS" in sec_upper:
                    current_section = "ADDED"
                    req_list_ref = added_requirements
                elif "MODIFIED REQUIREMENTS" in sec_upper:
                    current_section = "MODIFIED"
                    req_list_ref = modified_requirements
                elif "REMOVED REQUIREMENTS" in sec_upper:
                    current_section = "REMOVED"
                    req_list_ref = removed_requirements
                elif "REQUIREMENTS" in sec_upper:
                    current_section = "REQUIREMENTS"
                    req_list_ref = living_requirements
                elif "CONTEXT" in sec_upper or "MOTIVATION" in sec_upper or "OVERVIEW" in sec_upper:
                    current_section = "CONTEXT"
                    req_list_ref = None
                else:
                    current_section = "OTHER"
                    req_list_ref = None
                continue

            # Collect context text
            if current_section == "CONTEXT":
                context_lines.append(line)
                continue

            # Requirement headings (### Requirement: ...)
            m_req = self.REQUIREMENT_HEADING_RE.match(stripped)
            if m_req:
                # Finalize previous requirement & scenario
                if current_scenario and current_req:
                    current_req.scenarios.append(current_scenario)
                    current_scenario = None
                if current_req and req_list_ref is not None:
                    if req_desc_lines:
                        current_req.description = "\n".join(req_desc_lines).strip()
                    req_list_ref.append(current_req)

                req_name = m_req.group(1).strip()
                current_req = Requirement(name=req_name)
                req_desc_lines = []
                continue

            # Scenario headings (#### Scenario: ...)
            m_sc = self.SCENARIO_HEADING_RE.match(stripped)
            if m_sc and current_req is not None:
                if current_scenario:
                    current_req.scenarios.append(current_scenario)
                tag = m_sc.group(1).strip() if m_sc.group(1) else None
                sc_name = m_sc.group(2).strip()
                current_scenario = Scenario(name=sc_name, tag=tag)
                continue

            # Scenario steps
            if current_scenario is not None:
                step_match = re.match(r"^(?:[-*]\s+)?(GIVEN|WHEN|THEN|AND|BUT)\b\s*(.*)$", stripped, re.IGNORECASE)
                if step_match:
                    keyword = step_match.group(1).upper()
                    rest = step_match.group(2).strip()
                    current_scenario.steps.append(f"{keyword} {rest}")
                    continue

            # Requirement description lines (before any scenario)
            if current_req is not None and current_scenario is None:
                req_desc_lines.append(line)

        # Finalize open scenario and requirement at EOF
        if current_scenario and current_req:
            current_req.scenarios.append(current_scenario)
        if current_req and req_list_ref is not None:
            if req_desc_lines:
                current_req.description = "\n".join(req_desc_lines).strip()
            req_list_ref.append(current_req)

        # Normalize context string
        context_str = "\n".join(context_lines).strip()

        return SpecAST(
            spec_type=spec_type,
            title=title,
            headers=headers,
            context=context_str,
            added_requirements=added_requirements,
            modified_requirements=modified_requirements,
            removed_requirements=removed_requirements,
            requirements=living_requirements,
            raw_content=content,
        )


class SpecValidator:
    """Validates specification grammar, headers, and scenario completeness."""

    def __init__(self) -> None:
        self.parser = SpecASTParser()

    def validate_file(
        self,
        file_path: Path | str,
        strict: bool = False,
        workspace_root: Optional[Path | str] = None,
    ) -> Dict[str, Any]:
        path = Path(file_path)
        if path.is_dir() and (path / "spec.md").is_file():
            path = path / "spec.md"
        if not path.exists():
            return {
                "valid": False,
                "errors": [f"File not found: {path}"],
                "warnings": [],
                "stats": {},
            }
        content = path.read_text(encoding="utf-8")
        res = self.validate_string(content, strict=strict)

        # Living specification brownfield gate ([ROUT-01])
        if res.get("valid") and strict:
            try:
                ast = self.parser.parse_string(content)
                domain = ast.headers.get("Target Domain") or ast.headers.get("Domain")
                if ast.spec_type != "delta" and domain:
                    root = Path(workspace_root) if workspace_root else Path.cwd()
                    living_spec = root / "docs" / "specs" / domain / "spec.md"
                    if living_spec.is_file():
                        try:
                            is_same_file = path.resolve() == living_spec.resolve()
                        except Exception:
                            is_same_file = False
                        if not is_same_file:
                            err_msg = (
                                f"Living specification for domain '{domain}' already exists at "
                                f"docs/specs/{domain}/spec.md. Brownfield modifications must use Delta Specification format "
                                f"('# Spec Delta: <title>', 'Target Domain: {domain}', and 'Target Spec: docs/specs/{domain}/spec.md') "
                                f"via '/to-spec --delta {domain}' to preserve baseline scenarios ([ROUT-01])."
                            )
                            res["valid"] = False
                            res["errors"].append(err_msg)
            except Exception:
                pass

        return res

    def validate_string(self, content: str, strict: bool = False) -> Dict[str, Any]:
        errors: List[str] = []
        warnings: List[str] = []

        try:
            ast = self.parser.parse_string(content)
        except Exception as exc:
            return {
                "valid": False,
                "errors": [f"AST parsing exception: {exc}"],
                "warnings": [],
                "stats": {},
            }

        # 1. Title validation
        if not ast.title:
            errors.append("Missing specification title heading ('# Spec Delta: <title>' or '# Spec: <title>')")

        # 2. Domain header validation
        domain = ast.headers.get("Target Domain") or ast.headers.get("Domain")
        if not domain:
            errors.append("Missing domain metadata header ('Target Domain: <domain>' or 'Domain: <domain>')")

        # 3. Section presence validation
        if ast.spec_type == "delta":
            if not ast.added_requirements and not ast.modified_requirements and not ast.removed_requirements:
                errors.append(
                    "Delta specification must define at least one of '## ADDED Requirements', "
                    "'## MODIFIED Requirements', or '## REMOVED Requirements'"
                )
        else:
            if not ast.requirements:
                errors.append("Living specification must contain a '## Requirements' section with at least one requirement")

        # 4. Requirements & Scenario Completeness validation
        req_groups: List[Tuple[str, List[Requirement]]] = []
        if ast.spec_type == "delta":
            req_groups.append(("ADDED", ast.added_requirements))
            req_groups.append(("MODIFIED", ast.modified_requirements))
            req_groups.append(("REMOVED", ast.removed_requirements))
        else:
            req_groups.append(("LIVING", ast.requirements))

        total_scenarios = 0
        seen_req_names: Set[str] = set()

        for group_name, reqs in req_groups:
            for req in reqs:
                norm_name = req.name.strip().lower()
                if not req.name.strip():
                    errors.append(f"[{group_name}] Requirement has an empty name")

                if strict:
                    if norm_name in seen_req_names:
                        errors.append(f"[{group_name}] Duplicate requirement name: '{req.name}'")
                    seen_req_names.add(norm_name)

                    if not req.description.strip() and group_name != "REMOVED":
                        errors.append(f"[{group_name}] Requirement '{req.name}' is missing behavioral description")

                # Scenarios are mandatory for ADDED, MODIFIED, and LIVING
                if group_name in ("ADDED", "MODIFIED", "LIVING"):
                    if not req.scenarios:
                        errors.append(f"[{group_name}] Requirement '{req.name}' must define at least one scenario")

                    seen_scenario_tags: Set[str] = set()
                    for sc in req.scenarios:
                        total_scenarios += 1
                        if not sc.name.strip():
                            errors.append(f"[{group_name}] Requirement '{req.name}' contains a scenario with an empty title")

                        # Check Given/When/Then steps
                        has_given = any(s.startswith("GIVEN") for s in sc.steps)
                        has_when = any(s.startswith("WHEN") for s in sc.steps)
                        has_then = any(s.startswith("THEN") for s in sc.steps)

                        if not has_given:
                            errors.append(f"[{group_name}] Requirement '{req.name}' Scenario '{sc.raw_title}' missing GIVEN step")
                        if not has_when:
                            errors.append(f"[{group_name}] Requirement '{req.name}' Scenario '{sc.raw_title}' missing WHEN step")
                        if not has_then:
                            errors.append(f"[{group_name}] Requirement '{req.name}' Scenario '{sc.raw_title}' missing THEN step")

                        # Strict mode scenario checks
                        if strict:
                            if not sc.tag:
                                errors.append(
                                    f"[{group_name}] Strict mode requires scenario tag ID (e.g. '[AUTH-01]'): '{sc.name}'"
                                )
                            elif not re.match(r"^[A-Za-z0-9_-]+$", sc.tag):
                                errors.append(f"[{group_name}] Invalid scenario tag format '[{sc.tag}]'")
                            else:
                                if sc.tag in seen_scenario_tags:
                                    errors.append(f"[{group_name}] Duplicate scenario tag '[{sc.tag}]' in requirement '{req.name}'")
                                seen_scenario_tags.add(sc.tag)

                            # Verify step ordering: GIVEN before WHEN before THEN
                            given_idx = next((i for i, s in enumerate(sc.steps) if s.startswith("GIVEN")), -1)
                            when_idx = next((i for i, s in enumerate(sc.steps) if s.startswith("WHEN")), -1)
                            then_idx = next((i for i, s in enumerate(sc.steps) if s.startswith("THEN")), -1)

                            if given_idx != -1 and when_idx != -1 and given_idx > when_idx:
                                errors.append(
                                    f"[{group_name}] Scenario '{sc.raw_title}' step ordering violation: GIVEN must precede WHEN"
                                )
                            if when_idx != -1 and then_idx != -1 and when_idx > then_idx:
                                errors.append(
                                    f"[{group_name}] Scenario '{sc.raw_title}' step ordering violation: WHEN must precede THEN"
                                )

        # Build stats payload
        stats: Dict[str, Any] = {
            "domain": domain or "",
            "total_scenarios": total_scenarios,
        }
        if ast.spec_type == "delta":
            stats["added_requirements"] = len(ast.added_requirements)
            stats["modified_requirements"] = len(ast.modified_requirements)
            stats["removed_requirements"] = len(ast.removed_requirements)
        else:
            stats["requirements"] = len(ast.requirements)

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "stats": stats,
        }


class SpecReconciler:
    """
    Implements Additive Scenario Merging and idempotent reconciliation
    between Delta Specifications and Living System Specifications.
    """

    def __init__(self) -> None:
        self.parser = SpecASTParser()

    def reconcile_files(
        self,
        delta_path: Path | str,
        target_path: Optional[Path | str] = None,
        dry_run: bool = False,
    ) -> str:
        d_path = Path(delta_path)
        if d_path.is_dir() and (d_path / "spec.md").is_file():
            d_path = d_path / "spec.md"
        if not d_path.exists():
            raise FileNotFoundError(f"Delta specification file not found: {d_path}")

        delta_content = d_path.read_text(encoding="utf-8")
        delta_ast = self.parser.parse_string(delta_content)

        # Resolve target living spec path
        t_path: Optional[Path] = None
        if target_path:
            t_path = Path(target_path)
            if t_path.is_dir() and (t_path / "spec.md").is_file():
                t_path = t_path / "spec.md"
        else:
            spec_target_header = delta_ast.headers.get("Target Spec")
            if spec_target_header:
                t_path = Path(spec_target_header)
            else:
                domain = delta_ast.headers.get("Target Domain") or delta_ast.headers.get("Domain") or "general"
                t_path = Path(f"docs/specs/{domain}/spec.md")

        living_content = ""
        if t_path and t_path.exists():
            living_content = t_path.read_text(encoding="utf-8")

        reconciled_md = self.reconcile_strings(delta_content, living_content)

        if not dry_run and t_path:
            t_path.parent.mkdir(parents=True, exist_ok=True)
            t_path.write_text(reconciled_md, encoding="utf-8")

        return reconciled_md

    @staticmethod
    def _merge_scenarios(existing_scenarios: List[Scenario], incoming_scenarios: List[Scenario]) -> List[Scenario]:
        merged = list(existing_scenarios)
        for delta_sc in incoming_scenarios:
            match_idx = -1
            for idx, esc in enumerate(merged):
                if (delta_sc.tag and esc.tag and delta_sc.tag.lower() == esc.tag.lower()) or (
                    delta_sc.name.strip().lower() == esc.name.strip().lower()
                ):
                    match_idx = idx
                    break

            if match_idx >= 0:
                merged[match_idx] = delta_sc
            else:
                merged.append(delta_sc)
        return merged

    def reconcile_strings(self, delta_content: str, living_content: str = "") -> str:
        delta_ast = self.parser.parse_string(delta_content)
        domain = delta_ast.headers.get("Target Domain") or delta_ast.headers.get("Domain") or "general"

        # Greenfield target initialization: if no living spec exists, synthesize baseline
        if not living_content.strip():
            living_title = delta_ast.title
            if living_title.startswith("Delta:"):
                living_title = living_title[len("Delta:"):].strip()

            out_lines: List[str] = [
                f"# Spec: {living_title}",
                "",
                f"Domain: {domain}",
                "Status: ready-for-agent",
                "",
            ]
            if delta_ast.context:
                out_lines.extend(["## Context & Motivation", delta_ast.context, ""])

            out_lines.append("## Requirements")

            # Greenfield requirements collection: process requirements, added_requirements, and modified_requirements
            greenfield_reqs: List[Requirement] = []
            seen_req_names: Set[str] = set()

            for req in (delta_ast.requirements + delta_ast.added_requirements + delta_ast.modified_requirements):
                norm = req.name.strip().lower()
                if norm not in seen_req_names:
                    seen_req_names.add(norm)
                    greenfield_reqs.append(req)
                else:
                    existing = next(r for r in greenfield_reqs if r.name.strip().lower() == norm)
                    if req.description.strip():
                        existing.description = req.description.strip()
                    existing.scenarios = self._merge_scenarios(existing.scenarios, req.scenarios)

            for req in greenfield_reqs:
                out_lines.append(f"### Requirement: {req.name}")
                if req.description:
                    out_lines.append(req.description)
                out_lines.append("")
                for sc in req.scenarios:
                    out_lines.append(f"#### Scenario: {sc.raw_title}")
                    for step in sc.steps:
                        out_lines.append(f"- {step}")
                    out_lines.append("")

            return "\n".join(out_lines).strip() + "\n"

        # Brownfield reconciliation
        living_ast = self.parser.parse_string(living_content)

        # 1. Process REMOVED requirements
        removed_names = {r.name.strip().lower() for r in delta_ast.removed_requirements}
        retained_reqs: List[Requirement] = [
            r for r in living_ast.requirements if r.name.strip().lower() not in removed_names
        ]

        # 2. Process MODIFIED requirements with Additive Scenario Merging
        for mod_req in delta_ast.modified_requirements:
            mod_norm = mod_req.name.strip().lower()
            existing_req = next((r for r in retained_reqs if r.name.strip().lower() == mod_norm), None)

            if existing_req is None:
                incoming_tags = {sc.tag.lower() for sc in mod_req.scenarios if sc.tag}
                if incoming_tags:
                    for r in retained_reqs:
                        r_tags = {sc.tag.lower() for sc in r.scenarios if sc.tag}
                        if incoming_tags & r_tags:
                            existing_req = r
                            break

            if existing_req is not None:
                if mod_req.description.strip():
                    existing_req.description = mod_req.description.strip()
                existing_req.scenarios = self._merge_scenarios(existing_req.scenarios, mod_req.scenarios)
            else:
                retained_reqs.append(mod_req)

        # 3. Process ADDED and baseline requirements (idempotent: append if new, merge scenarios if already present)
        for incoming_req in (delta_ast.requirements + delta_ast.added_requirements):
            inc_norm = incoming_req.name.strip().lower()
            if inc_norm in removed_names:
                continue
            existing_req = next((r for r in retained_reqs if r.name.strip().lower() == inc_norm), None)

            if existing_req is None:
                retained_reqs.append(incoming_req)
            else:
                if incoming_req.description.strip():
                    existing_req.description = incoming_req.description.strip()
                existing_req.scenarios = self._merge_scenarios(existing_req.scenarios, incoming_req.scenarios)

        # Serialize living spec to deterministic Markdown
        spec_title = living_ast.title or delta_ast.title
        living_domain = living_ast.headers.get("Domain") or domain
        status = living_ast.headers.get("Status") or "ready-for-agent"

        out_lines = [
            f"# Spec: {spec_title}",
            "",
            f"Domain: {living_domain}",
            f"Status: {status}",
            "",
        ]

        context_body = living_ast.context or delta_ast.context
        if context_body:
            out_lines.extend(["## Context & Motivation", context_body, ""])

        out_lines.append("## Requirements")

        for req in retained_reqs:
            out_lines.append(f"### Requirement: {req.name}")
            if req.description:
                out_lines.append(req.description)
            out_lines.append("")

            for sc in req.scenarios:
                out_lines.append(f"#### Scenario: {sc.raw_title}")
                for step in sc.steps:
                    out_lines.append(f"- {step}")
                out_lines.append("")

        return "\n".join(out_lines).strip() + "\n"


class SpecBaselineDiffChecker:
    """
    Compares a brownfield delta specification against a living system specification.
    Verifies that all baseline scenarios not explicitly declared under '## REMOVED Requirements'
    remain intact, enforcing Additive Scenario Discipline ([DIFF-01], [DIFF-02]).
    Detects silently omitted baseline scenarios and can restore them in-place with advisory notices.
    """

    def __init__(self) -> None:
        self.parser = SpecASTParser()

    def diff_check(self, delta_content: str, living_content: str) -> Dict[str, Any]:
        delta_ast = self.parser.parse_string(delta_content)
        living_ast = self.parser.parse_string(living_content)

        removed_req_names: Set[str] = set()
        removed_sc_tags: Set[str] = set()
        removed_sc_names: Set[str] = set()

        for r in delta_ast.removed_requirements:
            r_norm = r.name.strip().lower()
            if not r.scenarios:
                removed_req_names.add(r_norm)
            else:
                for sc in r.scenarios:
                    if sc.tag:
                        removed_sc_tags.add(sc.tag.strip().lower())
                    if sc.name:
                        removed_sc_names.add(sc.name.strip().lower())

        baseline_scenarios: List[Dict[str, Any]] = []
        for l_req in living_ast.requirements:
            if l_req.name.strip().lower() in removed_req_names:
                continue
            for sc in l_req.scenarios:
                sc_tag_norm = sc.tag.strip().lower() if sc.tag else ""
                sc_name_norm = sc.name.strip().lower()
                if sc_tag_norm and sc_tag_norm in removed_sc_tags:
                    continue
                if not sc_tag_norm and sc_name_norm in removed_sc_names:
                    continue
                baseline_scenarios.append({
                    "requirement": l_req.name.strip(),
                    "tag": sc.tag or "",
                    "name": sc.name.strip(),
                    "raw_title": sc.raw_title,
                    "steps": list(sc.steps),
                    "raw_text": sc.raw_text,
                })

        omitted_scenarios: List[Dict[str, Any]] = []
        intact_scenarios: List[Dict[str, Any]] = []

        candidate_reqs = (
            delta_ast.modified_requirements
            + delta_ast.added_requirements
            + delta_ast.requirements
        )

        for base_sc in baseline_scenarios:
            req_name = base_sc["requirement"]
            base_tag = base_sc["tag"].lower() if base_sc["tag"] else ""
            base_name = base_sc["name"].lower()

            mod_req = next(
                (r for r in candidate_reqs if r.name.strip().lower() == req_name.lower()),
                None,
            )
            if mod_req is None and base_tag:
                mod_req = next(
                    (r for r in candidate_reqs if any(sc.tag and sc.tag.lower() == base_tag for sc in r.scenarios)),
                    None,
                )

            if mod_req is not None:
                matched = False
                for delta_sc in mod_req.scenarios:
                    delta_tag = delta_sc.tag.lower() if delta_sc.tag else ""
                    delta_name = delta_sc.name.lower()
                    if base_tag and delta_tag:
                        if base_tag == delta_tag:
                            matched = True
                            break
                    elif not (base_tag and delta_tag) and base_name == delta_name:
                        matched = True
                        break
                if not matched:
                    omitted_scenarios.append(base_sc)
                else:
                    intact_scenarios.append(base_sc)
            else:
                intact_scenarios.append(base_sc)

        valid = len(omitted_scenarios) == 0
        return {
            "valid": valid,
            "omitted_scenarios": omitted_scenarios,
            "intact_scenarios": intact_scenarios,
            "total_baseline": len(baseline_scenarios),
            "removed_requirements": list(removed_req_names),
        }

    def restore_omitted_scenarios(
        self, delta_content: str, living_content: str
    ) -> Tuple[str, List[str]]:
        diff_res = self.diff_check(delta_content, living_content)
        omitted = diff_res["omitted_scenarios"]
        if not omitted:
            return delta_content, []

        advisory_notices: List[str] = []
        lines = delta_content.splitlines()

        for om in omitted:
            req_name = om["requirement"]
            tag_desc = f"[{om['tag']}] {om['name']}" if om["tag"] else om["name"]

            req_idx = -1
            in_modified = False
            for idx, line in enumerate(lines):
                line_s = line.strip()
                if re.match(r"^##\s+MODIFIED\s+Requirements", line_s, re.IGNORECASE):
                    in_modified = True
                elif in_modified and re.match(r"^##\s+", line_s):
                    in_modified = False
                elif in_modified:
                    m_req = SpecASTParser.REQUIREMENT_HEADING_RE.match(line_s)
                    if m_req and m_req.group(1).strip().lower() == req_name.lower():
                        req_idx = idx
                        break

            if req_idx < 0:
                for idx, line in enumerate(lines):
                    m_req = SpecASTParser.REQUIREMENT_HEADING_RE.match(line.strip())
                    if m_req and m_req.group(1).strip().lower() == req_name.lower():
                        req_idx = idx
                        break

            if req_idx < 0:
                mod_sec_idx = -1
                for idx, line in enumerate(lines):
                    if re.match(r"^##\s+MODIFIED\s+Requirements", line.strip(), re.IGNORECASE):
                        mod_sec_idx = idx
                        break

                if mod_sec_idx >= 0:
                    lines.insert(mod_sec_idx + 1, "")
                    lines.insert(mod_sec_idx + 2, f"### Requirement: {req_name}")
                    req_idx = mod_sec_idx + 2
                else:
                    rem_sec_idx = -1
                    for idx, line in enumerate(lines):
                        if re.match(r"^##\s+REMOVED\s+Requirements", line.strip(), re.IGNORECASE):
                            rem_sec_idx = idx
                            break
                    new_sec = [
                        "",
                        "## MODIFIED Requirements",
                        "",
                        f"### Requirement: {req_name}",
                    ]
                    if rem_sec_idx >= 0:
                        lines[rem_sec_idx:rem_sec_idx] = new_sec
                        req_idx = rem_sec_idx + 3
                    else:
                        req_idx = len(lines) + 3
                        lines.extend(new_sec)

            end_idx = len(lines)
            for idx in range(req_idx + 1, len(lines)):
                l_s = lines[idx].strip()
                if SpecASTParser.REQUIREMENT_HEADING_RE.match(l_s) or re.match(r"^##\s+", l_s):
                    end_idx = idx
                    break

            sc_lines: List[str] = [
                "",
                f"#### Scenario: {tag_desc}",
            ]
            for st in om["steps"]:
                sc_lines.append(f"- {st}")
            sc_lines.append("")

            lines[end_idx:end_idx] = sc_lines

            notice = f"[ADVISORY] Restored omitted baseline scenario '{tag_desc}' to requirement '{req_name}' in-place."
            advisory_notices.append(notice)

        restored_content = "\n".join(lines).rstrip() + "\n"
        return restored_content, advisory_notices

    def diff_check_files(
        self,
        delta_path: Path | str,
        target_path: Optional[Path | str] = None,
        restore: bool = False,
    ) -> Dict[str, Any]:
        d_path = Path(delta_path)
        if d_path.is_dir() and (d_path / "spec.md").is_file():
            d_path = d_path / "spec.md"
        if not d_path.exists():
            raise FileNotFoundError(f"Delta specification file not found: {d_path}")

        delta_content = d_path.read_text(encoding="utf-8")
        delta_ast = self.parser.parse_string(delta_content)

        t_path: Optional[Path] = None
        if target_path:
            t_path = Path(target_path)
            if t_path.is_dir() and (t_path / "spec.md").is_file():
                t_path = t_path / "spec.md"
        else:
            spec_target_header = delta_ast.headers.get("Target Spec")
            if spec_target_header:
                t_path = Path(spec_target_header)
            else:
                domain = delta_ast.headers.get("Target Domain") or delta_ast.headers.get("Domain")
                if domain:
                    t_path = Path(f"docs/specs/{domain}/spec.md")

        if not t_path or not t_path.exists():
            raise FileNotFoundError(
                f"Target living specification not found: {t_path or 'unspecified domain'}"
            )

        living_content = t_path.read_text(encoding="utf-8")

        result = self.diff_check(delta_content, living_content)
        result["delta_file"] = str(d_path)
        result["target_file"] = str(t_path)
        result["restored"] = False

        if restore and not result["valid"] and result["omitted_scenarios"]:
            restored_content, notices = self.restore_omitted_scenarios(delta_content, living_content)
            d_path.write_text(restored_content, encoding="utf-8")
            recheck = self.diff_check(restored_content, living_content)
            result["restored"] = len(notices) > 0 and recheck["valid"]
            result["advisory_notices"] = notices
            result["valid"] = recheck["valid"]
            result["omitted_scenarios"] = recheck["omitted_scenarios"]
            result["intact_scenarios"] = recheck["intact_scenarios"]

        return result


class SpecCoverageChecker:
    """Verifies that all Delta Specification requirements and scenarios are mapped to issues."""

    def __init__(self) -> None:
        self.parser = SpecASTParser()

    def check_coverage_file(
        self,
        delta_path: Path | str,
        issues_dir: Path | str,
        target_spec: Path | str | None = None,
    ) -> Dict[str, Any]:
        d_path = Path(delta_path)
        if d_path.is_dir() and (d_path / "spec.md").is_file():
            d_path = d_path / "spec.md"
        if not d_path.exists():
            raise FileNotFoundError(f"Delta spec file not found: {d_path}")
        content = d_path.read_text(encoding="utf-8")
        i_path = Path(issues_dir)
        if i_path.is_dir() and (i_path / "issues").is_dir():
            i_path = i_path / "issues"
        return self.check_coverage_string(content, i_path, target_spec=target_spec)

    def check_coverage_string(
        self,
        delta_content: str,
        issues_dir: Path | str,
        target_spec: Path | str | None = None,
    ) -> Dict[str, Any]:
        ast = self.parser.parse_string(delta_content)
        issues_path = Path(issues_dir)

        if not issues_path.exists() or not issues_path.is_dir():
            return {
                "covered": False,
                "coverage_percent": 0.0,
                "total_requirements": 0,
                "covered_requirements": [],
                "uncovered_requirements": [],
                "total_scenarios": 0,
                "covered_scenarios": [],
                "uncovered_scenarios": [],
                "mapped": {},
                "errors": [f"Issues directory not found: {issues_path}"],
            }

        issue_files = list(issues_path.glob("*.md"))
        issue_texts: Dict[str, str] = {}
        for f in issue_files:
            issue_texts[f.name] = f.read_text(encoding="utf-8")

        # Discover baseline living specification if delta specification
        baseline_scenarios_by_key: Dict[str, Scenario] = {}
        if ast.spec_type == "delta" or ast.headers.get("Target Spec"):
            t_path: Optional[Path] = None
            if target_spec:
                t_path = Path(target_spec)
                if t_path.is_dir() and (t_path / "spec.md").is_file():
                    t_path = t_path / "spec.md"
            else:
                spec_target_header = ast.headers.get("Target Spec")
                if spec_target_header:
                    t_path = Path(spec_target_header)
                else:
                    domain = ast.headers.get("Target Domain") or ast.headers.get("Domain")
                    if domain:
                        t_path = Path(f"docs/specs/{domain}/spec.md")

            if t_path and t_path.is_file():
                try:
                    living_content = t_path.read_text(encoding="utf-8")
                    living_ast = self.parser.parse_string(living_content)

                    # Check if living_ast was already reconciled with this delta spec
                    # (i.e. all delta scenarios are already present in living_ast)
                    all_delta_sc_tags = {
                        sc.tag.lower() for r in (ast.added_requirements + ast.modified_requirements)
                        for sc in r.scenarios if sc.tag
                    }
                    living_sc_tags = {
                        sc.tag.lower() for r in living_ast.requirements for sc in r.scenarios if sc.tag
                    }
                    if all_delta_sc_tags and all_delta_sc_tags.issubset(living_sc_tags):
                        try:
                            rel_path = t_path.relative_to(Path.cwd()) if t_path.is_absolute() else t_path
                            res = subprocess.run(
                                ["git", "show", f"HEAD~1:{rel_path}"],
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            if res.returncode == 0 and res.stdout.strip():
                                git_living_ast = self.parser.parse_string(res.stdout)
                                git_living_tags = {
                                    sc.tag.lower() for r in git_living_ast.requirements for sc in r.scenarios if sc.tag
                                }
                                if not all_delta_sc_tags.issubset(git_living_tags):
                                    living_ast = git_living_ast
                        except Exception:
                            pass

                    for r in living_ast.requirements:
                        for sc in r.scenarios:
                            sc_key = (sc.tag.lower() if sc.tag else "") + "::" + sc.name.strip().lower()
                            baseline_scenarios_by_key[sc_key] = sc
                            if sc.tag:
                                baseline_scenarios_by_key[sc.tag.strip().lower()] = sc
                except Exception:
                    pass

        if baseline_scenarios_by_key:
            all_reqs_raw: List[Requirement] = list(ast.added_requirements) + list(ast.removed_requirements)
            all_scenarios_raw: List[Scenario] = []
            for r in ast.added_requirements:
                all_scenarios_raw.extend(r.scenarios)

            for r in ast.modified_requirements:
                req_delta_scenarios: List[Scenario] = []
                for sc in r.scenarios:
                    sc_key = (sc.tag.lower() if sc.tag else "") + "::" + sc.name.strip().lower()
                    base_match = baseline_scenarios_by_key.get(sc_key)
                    if not base_match and sc.tag:
                        base_match = baseline_scenarios_by_key.get(sc.tag.strip().lower())

                    is_unchanged_baseline = False
                    if base_match:
                        if sc.steps == base_match.steps or not sc.steps:
                            is_unchanged_baseline = True

                    if not is_unchanged_baseline:
                        req_delta_scenarios.append(sc)

                if req_delta_scenarios:
                    r_copy = Requirement(
                        name=r.name,
                        description=r.description,
                        scenarios=req_delta_scenarios,
                        raw_text=r.raw_text,
                    )
                    all_reqs_raw.append(r_copy)
                    all_scenarios_raw.extend(req_delta_scenarios)
        else:
            all_reqs_raw = (
                ast.requirements + ast.added_requirements + ast.modified_requirements + ast.removed_requirements
            )
            all_scenarios_raw = []
            for r in ast.requirements + ast.added_requirements + ast.modified_requirements:
                all_scenarios_raw.extend(r.scenarios)

        seen_reqs: Set[str] = set()
        all_reqs: List[Requirement] = []
        for r in all_reqs_raw:
            r_norm = r.name.strip().lower()
            if r_norm not in seen_reqs:
                seen_reqs.add(r_norm)
                all_reqs.append(r)

        seen_scenarios: Set[str] = set()
        all_scenarios: List[Scenario] = []
        for sc in all_scenarios_raw:
            sc_key = (sc.tag.lower() if sc.tag else "") + "::" + sc.name.strip().lower()
            if sc_key not in seen_scenarios:
                seen_scenarios.add(sc_key)
                all_scenarios.append(sc)

        req_mapping: Dict[str, List[str]] = {}
        uncovered_reqs: List[str] = []

        for req in all_reqs:
            matched_files = []
            for fname, text in issue_texts.items():
                if req.name.lower() in text.lower():
                    matched_files.append(fname)
            if not matched_files and req.scenarios:
                all_scenarios_mapped = True
                covering_files: Set[str] = set()
                for sc in req.scenarios:
                    sc_terms = [sc.name.lower()]
                    if sc.tag:
                        sc_terms.append(sc.tag.lower())
                        sc_terms.append(f"[{sc.tag.lower()}]")
                    sc_matched = []
                    for fname, text in issue_texts.items():
                        text_lower = text.lower()
                        if any(term in text_lower for term in sc_terms):
                            sc_matched.append(fname)
                            covering_files.add(fname)
                    if not sc_matched:
                        all_scenarios_mapped = False
                        break
                if all_scenarios_mapped:
                    matched_files = sorted(list(covering_files))
            if matched_files:
                req_mapping[req.name] = matched_files
            else:
                uncovered_reqs.append(req.name)


        scenario_mapping: Dict[str, List[str]] = {}
        uncovered_scenarios: List[str] = []

        for sc in all_scenarios:
            matched_files = []
            search_terms = [sc.name.lower()]
            if sc.tag:
                search_terms.append(sc.tag.lower())
                search_terms.append(f"[{sc.tag.lower()}]")

            for fname, text in issue_texts.items():
                text_lower = text.lower()
                if any(term in text_lower for term in search_terms):
                    matched_files.append(fname)

            key = sc.tag or sc.name
            if matched_files:
                scenario_mapping[key] = matched_files
            else:
                uncovered_scenarios.append(key)

        anatomy_errors: List[str] = []
        for fname, text in issue_texts.items():
            if "## acceptance criteria" in text.lower():
                if "## completion protocol" not in text.lower():
                    anatomy_errors.append(f"Ticket '{fname}' is missing mandatory '## Completion Protocol' (Invariant [UP-03]).")

        total_items = len(all_reqs) + len(all_scenarios)
        covered_items = len(req_mapping) + len(scenario_mapping)
        coverage_pct = 100.0 if total_items == 0 else round((covered_items / total_items) * 100.0, 1)
        is_covered = (len(uncovered_reqs) == 0 and len(uncovered_scenarios) == 0 and len(anatomy_errors) == 0)

        return {
            "covered": is_covered,
            "coverage_percent": coverage_pct,
            "total_requirements": len(all_reqs),
            "covered_requirements": list(req_mapping.keys()),
            "uncovered_requirements": uncovered_reqs,
            "total_scenarios": len(all_scenarios),
            "covered_scenarios": list(scenario_mapping.keys()),
            "uncovered_scenarios": uncovered_scenarios,
            "anatomy_errors": anatomy_errors,
            "mapped": {
                "requirements": req_mapping,
                "scenarios": scenario_mapping,
            },
        }
class SpecSeamVerifier:
    """
    Physically verifies testing seams declared in specifications exist on disk.
    Detects referenced source files and tests, verifies physical existence,
    and handles greenfield interface contract declarations.
    """

    @staticmethod
    def _clean_seam_path(raw_path: str) -> str:
        p = raw_path.strip().strip("`'\"")
        if p.startswith("http://") or p.startswith("https://") or p.startswith("#"):
            return ""
        p = p.split("#")[0].strip().strip("`'\"")
        p = re.sub(r"(\.[a-zA-Z0-9]+):.*$", r"\1", p).strip().strip("`'\"")
        return p

    def verify_seams_file(self, spec_path: Path | str, repo_root: Path | str = ".") -> Dict[str, Any]:
        s_path = Path(spec_path)
        if s_path.is_dir() and (s_path / "spec.md").is_file():
            s_path = s_path / "spec.md"
        if not s_path.exists():
            raise FileNotFoundError(f"Specification file not found: {s_path}")
        content = s_path.read_text(encoding="utf-8")
        return self.verify_seams_string(content, repo_root=repo_root, spec_file=str(s_path))

    def verify_seams_string(self, spec_content: str, repo_root: Path | str = ".", spec_file: str = "") -> Dict[str, Any]:
        root = Path(repo_root)
        errors: List[str] = []
        verified_seams: List[str] = []
        greenfield_contracts: List[str] = []

        if not spec_content.strip():
            return {
                "valid": False,
                "spec_file": spec_file,
                "verified_seams": [],
                "greenfield_contracts": [],
                "errors": ["Specification content is empty"],
            }

        found_paths: Set[str] = set()

        seams_section_match = re.search(r"(?is)##\s*(?:User Stories\s*&\s*)?Testing Seams.*?(?=\n##(?!#)|\Z)", spec_content)
        if seams_section_match:
            section_text = seams_section_match.group(0)
            for m in re.finditer(r"[`'\"]([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+(?:[#:].*?)?)[`'\"]", section_text):
                cleaned = self._clean_seam_path(m.group(1))
                if cleaned and "." in cleaned:
                    found_paths.add(cleaned)

        seam_patterns = [
            r"(?i)(?:Primary|Target|Testing)?\s*Seam(?:s)?\s*:\s*[`'\"]([^`'\"]+)[`'\"]",
            r"(?i)(?:Primary|Target|Testing)?\s*Seam(?:s)?\s*:\s*([^\s\n,;]+)",
            r"(?i)\b(?:seam\s+file|testing\s+seam)\s+[`'\"]([^`'\"]+)[`'\"]",
        ]
        for pattern in seam_patterns:
            for match in re.finditer(pattern, spec_content):
                cleaned = self._clean_seam_path(match.group(1))
                if cleaned and "." in cleaned:
                    found_paths.add(cleaned)

        lines = spec_content.splitlines()
        for path_str in sorted(found_paths):
            target_path = root / path_str
            if target_path.exists():
                verified_seams.append(path_str)
                continue

            # Localize greenfield contract inspection to the seam declaration context (+/- 2 lines)
            is_greenfield = False
            for i, line in enumerate(lines):
                if path_str in line:
                    context_window = "\n".join(lines[max(0, i - 2) : min(len(lines), i + 3)])
                    if re.search(
                        r"(?i)\b(?:greenfield|new\s+(?:public\s+)?interface\s+contract|vertical\s+slice\s+scaffolding|to\s+be\s+created|scaffold(?:ed)?\s+in\s+ticket)\b",
                        context_window,
                    ):
                        is_greenfield = True
                        break

            if is_greenfield:
                greenfield_contracts.append(path_str)
            else:
                errors.append(f"Referenced testing seam does not exist on disk: '{path_str}'")

        valid = len(errors) == 0

        return {
            "valid": valid,
            "spec_file": spec_file,
            "verified_seams": verified_seams,
            "greenfield_contracts": greenfield_contracts,
            "errors": errors,
        }


class WorkflowDAGChecker:
    """Verifies declarative workflow schema DAGs and prerequisite phase gates."""

    def check_dag(
        self,
        schema_path: Path | str,
        target_phase: str,
        workspace_root: Optional[Path | str] = None,
        context_dir: Optional[Path | str] = None,
    ) -> Dict[str, Any]:
        s_path = Path(schema_path)
        if not s_path.exists():
            return {
                "valid": False,
                "target_phase": target_phase,
                "prerequisites": [],
                "satisfied": False,
                "missing_artifacts": [],
                "errors": [f"Schema file not found: {s_path}"],
            }

        try:
            data = json.loads(s_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "valid": False,
                "target_phase": target_phase,
                "prerequisites": [],
                "satisfied": False,
                "missing_artifacts": [],
                "errors": [f"Malformed JSON schema: {exc}"],
            }

        phases = data.get("phases", {})
        if not isinstance(phases, dict):
            return {
                "valid": False,
                "target_phase": target_phase,
                "prerequisites": [],
                "satisfied": False,
                "missing_artifacts": [],
                "errors": ["Schema missing 'phases' dictionary"],
            }

        if target_phase not in phases:
            return {
                "valid": False,
                "target_phase": target_phase,
                "prerequisites": [],
                "satisfied": False,
                "missing_artifacts": [],
                "errors": [f"Unknown target phase: '{target_phase}'. Recognized phases: {list(phases.keys())}"],
            }

        # Cycle detection and transitive prerequisite resolution via DFS
        visited: Set[str] = set()
        rec_stack: Set[str] = set()
        errors: List[str] = []
        has_cycle = False

        def detect_cycle(phase_name: str, path: List[str]) -> bool:
            nonlocal has_cycle
            visited.add(phase_name)
            rec_stack.add(phase_name)

            phase_def = phases.get(phase_name, {})
            prereqs = phase_def.get("prerequisites", [])

            for p in prereqs:
                if p not in phases:
                    errors.append(f"Phase '{phase_name}' references undeclared prerequisite phase '{p}'")
                    continue
                if p in rec_stack:
                    errors.append(f"Workflow DAG cycle detected: {' -> '.join(path + [p])}")
                    has_cycle = True
                    return True
                if p not in visited:
                    if detect_cycle(p, path + [p]):
                        return True

            rec_stack.remove(phase_name)
            return False

        detect_cycle(target_phase, [target_phase])

        if has_cycle or errors:
            return {
                "valid": False,
                "target_phase": target_phase,
                "prerequisites": [],
                "satisfied": False,
                "missing_artifacts": [],
                "errors": errors,
            }

        # Compute transitive prerequisites (in topological dependency order)
        all_prereqs: List[str] = []
        visited_prereqs: Set[str] = set()

        def collect_prereqs(p_name: str) -> None:
            phase_def = phases.get(p_name, {})
            for p in phase_def.get("prerequisites", []):
                if p not in visited_prereqs:
                    visited_prereqs.add(p)
                    collect_prereqs(p)
                    all_prereqs.append(p)

        collect_prereqs(target_phase)

        # Check required artifacts for all transitive prerequisites
        root = Path(workspace_root) if workspace_root else Path.cwd()
        missing_artifacts: List[str] = []

        for p_name in all_prereqs:
            phase_def = phases.get(p_name, {})
            req_artifacts = phase_def.get("required_artifacts", [])
            for art_pattern in req_artifacts:
                # Support piped alternative patterns (e.g. '.scratch/*/spec.md | docs/specs/*/spec.md')
                sub_patterns = [p.strip() for p in art_pattern.split("|")]
                matched: List[Path] = []
                for sp in sub_patterns:
                    matched.extend(root.glob(sp))
                    if context_dir:
                        matched.extend(Path(context_dir).glob(sp))
                if not matched:
                    missing_artifacts.append(f"Phase '{p_name}': artifact pattern '{art_pattern}' not found")

        is_satisfied = (len(missing_artifacts) == 0 and len(errors) == 0)

        return {
            "valid": True,
            "target_phase": target_phase,
            "prerequisites": all_prereqs,
            "satisfied": is_satisfied,
            "missing_artifacts": missing_artifacts,
            "errors": errors,
        }


class AntiTautologyVerifier:
    """
    [TAUT-01] [TAUT-02] [ERR-TAUT-01]
    Enforces the Anti-Tautology Invariant during the Double-Verification Gate.
    Verifies that all automated test implementations explicitly reference valid Living Specification
    scenario tags (// Scenario: [TAG-xx] or # Scenario: [TAG-xx]), lack self-referential tautological
    assertions, and reference only valid scenarios declared in the specification AST.
    """

    EXPLICIT_SCENARIO_TAG_RE = re.compile(
        r"(?:(?://|#)\s*Scenario:\s*\[?([A-Za-z0-9_-]+)\]?|Scenario:\s*\[?([A-Za-z0-9_-]+)\]?)",
        re.IGNORECASE,
    )
    BRACKETED_TAG_RE = re.compile(r"\[([A-Za-z0-9_-]+)\]")
    TS_TEST_DEF_RE = re.compile(
        r"""(?:\b(?:it|test)\s*\(\s*['"`](.+?)['"`]|\bdescribe\s*\(\s*['"`](.+?)['"`])"""
    )
    TS_TAUTOLOGY_PATTERNS = [
        (re.compile(r"""\bexpect\s*\(\s*([^)]+?)\s*\)\s*\.(?:toBe|toEqual|toStrictEqual)\s*\(\s*([^)]+?)\s*\)"""), "expect({left}) compares identical expressions"),
        (re.compile(r"""\bexpect\s*\(\s*true\s*\)\s*\.(?:toBe|toEqual)\s*\(\s*true\s*\)""", re.IGNORECASE), "expect(true).toBe(true) is a self-referential tautology"),
        (re.compile(r"""\bexpect\s*\(\s*false\s*\)\s*\.(?:toBe|toEqual)\s*\(\s*false\s*\)""", re.IGNORECASE), "expect(false).toBe(false) is a self-referential tautology"),
        (re.compile(r"""\bexpect\s*\(\s*true\s*\)\s*\.toBeTruthy\s*\(\s*\)""", re.IGNORECASE), "expect(true).toBeTruthy() is a self-referential tautology"),
        (re.compile(r"""\bexpect\s*\(\s*false\s*\)\s*\.toBeFalsy\s*\(\s*\)""", re.IGNORECASE), "expect(false).toBeFalsy() is a self-referential tautology"),
    ]
    PY_TAUTOLOGY_PATTERNS = [
        (re.compile(r"""\bself\.assertEqual\s*\(\s*([^,]+?)\s*,\s*\1\s*(?:,\s*[^)]+)?\)"""), "self.assertEqual compares identical expressions"),
        (re.compile(r"""\bself\.assertIs\s*\(\s*([^,]+?)\s*,\s*\1\s*(?:,\s*[^)]+)?\)"""), "self.assertIs compares identical expressions"),
        (re.compile(r"""\bself\.assertTrue\s*\(\s*True\s*(?:,\s*[^)]+)?\)"""), "self.assertTrue(True) is a self-referential tautology"),
        (re.compile(r"""\bself\.assertFalse\s*\(\s*False\s*(?:,\s*[^)]+)?\)"""), "self.assertFalse(False) is a self-referential tautology"),
        (re.compile(r"""\bself\.assertIsNot\s*\(\s*None\s*,\s*None\s*(?:,\s*[^)]+)?\)"""), "self.assertIsNot compares identical expressions"),
        (re.compile(r"""^\s*assert\s+True(?:\s*,\s*.*)?$""", re.MULTILINE), "assert True is a self-referential tautology"),
        (re.compile(r"""^\s*assert\s+([^=\s]+?)\s*==\s*\1(?:\s*,\s*.*)?$""", re.MULTILINE), "assert compares identical expressions"),
    ]

    def __init__(self) -> None:
        self.parser = SpecASTParser()

    def extract_spec_tags(
        self,
        spec_ast: SpecAST,
        spec_file_path: Optional[Path] = None,
        repo_root: Optional[Path] = None,
    ) -> Dict[str, str]:
        tags: Dict[str, str] = {}
        all_reqs = (
            spec_ast.requirements
            + spec_ast.added_requirements
            + spec_ast.modified_requirements
            + spec_ast.removed_requirements
        )
        for req in all_reqs:
            for sc in req.scenarios:
                if sc.tag:
                    clean_tag = sc.tag.strip().upper()
                    tags[clean_tag] = sc.raw_title

        for match in re.finditer(r"####\s+Scenario:\s*\[([A-Za-z0-9_-]+)\]\s*(.+)", spec_ast.raw_content):
            t = match.group(1).strip().upper()
            if t not in tags:
                tags[t] = match.group(2).strip()

        root = repo_root or Path.cwd()
        target_spec_header = spec_ast.headers.get("Target Spec")
        if target_spec_header:
            target_path = Path(target_spec_header)
            if not target_path.is_absolute():
                target_path = root / target_path
            if target_path.is_file():
                try:
                    target_ast = self.parser.parse_file(target_path)
                    for req in (
                        target_ast.requirements
                        + target_ast.added_requirements
                        + target_ast.modified_requirements
                    ):
                        for sc in req.scenarios:
                            if sc.tag:
                                clean_t = sc.tag.strip().upper()
                                if clean_t not in tags:
                                    tags[clean_t] = sc.raw_title
                except Exception:
                    pass

        if spec_file_path and spec_file_path.is_file():
            issues_dir = spec_file_path.parent / "issues"
            if issues_dir.is_dir():
                for issue_file in issues_dir.glob("*.md"):
                    try:
                        issue_text = issue_file.read_text(encoding="utf-8")
                        for m in re.finditer(r"\*\*\[([A-Za-z0-9_-]+)\]\*\*", issue_text):
                            tag_issue = m.group(1).strip().upper()
                            if tag_issue not in tags:
                                tags[tag_issue] = f"Issue tag from {issue_file.name}"
                    except Exception:
                        pass

        return tags

    def _extract_tags_from_text(self, text: str, valid_spec_tags: Set[str]) -> List[str]:
        found: List[str] = []
        for m in self.EXPLICIT_SCENARIO_TAG_RE.finditer(text):
            tag = (m.group(1) or m.group(2) or "").strip().upper()
            if tag and tag not in found:
                found.append(tag)

        for m in self.BRACKETED_TAG_RE.finditer(text):
            tag = m.group(1).strip().upper()
            if not tag:
                continue
            if tag in valid_spec_tags:
                if tag not in found:
                    found.append(tag)
            elif re.match(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$", tag):
                if tag not in found:
                    found.append(tag)

        return found

    def _scan_file_scenario_tags_and_syntax(
        self,
        filename: str,
        content: str,
        valid_spec_tags: Set[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        lines = content.splitlines()
        found_tags: List[Dict[str, Any]] = []
        syntax_errors: List[Dict[str, Any]] = []

        scenario_keyword_re = re.compile(
            r"""(?:(?://|#|/\*|\*)\s*)?Scenarios?:\s*(.*)""",
            re.IGNORECASE,
        )

        for idx, line in enumerate(lines, start=1):
            line_str = line.strip()
            if not line_str:
                continue

            m_scen = scenario_keyword_re.search(line)
            if m_scen:
                rest = m_scen.group(1).strip()
                if not rest:
                    syntax_errors.append({
                        "file": filename,
                        "line": idx,
                        "raw": line_str,
                        "reason": "Empty scenario tag citation: missing tag identifier",
                    })
                    continue

                if not rest.startswith("["):
                    token = rest.split()[0] if rest.split() else rest
                    syntax_errors.append({
                        "file": filename,
                        "line": idx,
                        "raw": token,
                        "reason": f"Scenario tag citation '{token}' must be enclosed in square brackets e.g. '[{token}]'",
                    })
                    continue

                bracket_matches = list(re.finditer(r"\[([^\]]*)\]", rest))
                if not bracket_matches:
                    syntax_errors.append({
                        "file": filename,
                        "line": idx,
                        "raw": rest,
                        "reason": f"Scenario tag citation is missing closing bracket ']': '{rest}'",
                    })
                    continue

                last_end = bracket_matches[-1].end()
                remainder_after_last = rest[last_end:]
                if "[" in remainder_after_last:
                    unclosed_part = remainder_after_last[remainder_after_last.find("["):]
                    syntax_errors.append({
                        "file": filename,
                        "line": idx,
                        "raw": unclosed_part,
                        "reason": f"Scenario tag citation is missing closing bracket ']': '{unclosed_part}'",
                    })

                for m_b in bracket_matches:
                    inside = m_b.group(1)
                    if not inside.strip():
                        syntax_errors.append({
                            "file": filename,
                            "line": idx,
                            "raw": "[]",
                            "reason": "Empty scenario tag bracket '[]'",
                        })
                        continue

                    if any(c.isspace() for c in inside):
                        syntax_errors.append({
                            "file": filename,
                            "line": idx,
                            "raw": f"[{inside}]",
                            "reason": f"Scenario tag '[{inside}]' must not contain whitespace",
                        })
                        continue

                    if any(c.islower() for c in inside):
                        syntax_errors.append({
                            "file": filename,
                            "line": idx,
                            "raw": f"[{inside}]",
                            "reason": f"Scenario tag '[{inside}]' must be uppercase hyphenated alphanumeric (e.g. '[TAG-01]')",
                        })
                        continue

                    if "_" in inside:
                        syntax_errors.append({
                            "file": filename,
                            "line": idx,
                            "raw": f"[{inside}]",
                            "reason": f"Scenario tag '[{inside}]' uses underscores instead of hyphens (e.g. '[TAG-01]')",
                        })
                        continue

                    if not re.match(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$", inside):
                        syntax_errors.append({
                            "file": filename,
                            "line": idx,
                            "raw": f"[{inside}]",
                            "reason": f"Scenario tag '[{inside}]' must be uppercase hyphenated alphanumeric (e.g. '[TAG-01]')",
                        })
                        continue

                    tag_norm = inside.strip().upper()
                    found_tags.append({
                        "tag": tag_norm,
                        "line": idx,
                        "raw": f"[{inside}]",
                    })
            else:
                for m_b in re.finditer(r"\[([A-Za-z0-9_ -]+)\]", line):
                    inside = m_b.group(1)
                    if not inside.strip():
                        continue
                    upper_inside = inside.strip().upper()
                    is_known = upper_inside in valid_spec_tags
                    is_explicit_test = (
                        upper_inside.startswith("NONEXISTENT")
                        or upper_inside.startswith("HALLUCINATED")
                    )
                    is_tag_format = bool(re.match(r"^[A-Z][A-Z0-9]*-[A-Z0-9]+(?:-[A-Z0-9]+)*$", inside))

                    if is_known or is_explicit_test or is_tag_format:
                        found_tags.append({
                            "tag": upper_inside,
                            "line": idx,
                            "raw": f"[{inside}]",
                        })

        return found_tags, syntax_errors

    def _scan_python_content(
        self,
        filename: str,
        content: str,
        valid_spec_tags: Set[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        test_cases: List[Dict[str, Any]] = []
        tautologies: List[Dict[str, Any]] = []
        lines = content.splitlines()

        try:
            tree = ast.parse(content, filename=filename)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                    test_name = node.name
                    line_no = getattr(node, "lineno", 1)
                    end_line_no = getattr(node, "end_lineno", line_no + 50)
                    doc = ast.get_docstring(node) or ""

                    prec_start = line_no
                    prec_lines = []
                    for i in range(line_no - 2, -1, -1):
                        l_str = lines[i].strip()
                        if not l_str:
                            prec_start = i + 1
                            continue
                        if l_str.startswith(("#", "//", "/*", "*", "@")):
                            prec_lines.append(l_str)
                            prec_start = i + 1
                        else:
                            break
                    prev_text = "\n".join(reversed(prec_lines))

                    tags = self._extract_tags_from_text(doc + "\n" + prev_text + "\n" + test_name, valid_spec_tags)

                    test_cases.append({
                        "file": filename,
                        "test_name": test_name,
                        "line": line_no,
                        "prec_start": prec_start,
                        "end_line": end_line_no,
                        "tags": tags,
                    })

                    for child in ast.walk(node):
                        if isinstance(child, ast.Assert):
                            if isinstance(child.test, ast.Constant) and child.test.value is True:
                                tautologies.append({
                                    "file": filename,
                                    "test_name": test_name,
                                    "line": getattr(child, "lineno", line_no),
                                    "assertion": "assert True",
                                    "reason": "assert True is a self-referential tautology",
                                })
                            elif isinstance(child.test, ast.Compare) and len(child.test.comparators) == 1:
                                if ast.dump(child.test.left) == ast.dump(child.test.comparators[0]):
                                    left_str = ast.unparse(child.test.left) if hasattr(ast, "unparse") else "expr"
                                    tautologies.append({
                                        "file": filename,
                                        "test_name": test_name,
                                        "line": getattr(child, "lineno", line_no),
                                        "assertion": f"assert {left_str} == {left_str}",
                                        "reason": "assertion compares identical expressions",
                                    })
                        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                            attr = child.func.attr
                            if attr in ("assertEqual", "assertIs") and len(child.args) >= 2:
                                if ast.dump(child.args[0]) == ast.dump(child.args[1]):
                                    arg_str = ast.unparse(child.args[0]) if hasattr(ast, "unparse") else "expr"
                                    tautologies.append({
                                        "file": filename,
                                        "test_name": test_name,
                                        "line": getattr(child, "lineno", line_no),
                                        "assertion": f"self.{attr}({arg_str}, {arg_str})",
                                        "reason": f"self.{attr} compares identical expressions ({arg_str})",
                                    })
                            elif attr == "assertIsNot" and len(child.args) >= 2:
                                if ast.dump(child.args[0]) == ast.dump(child.args[1]):
                                    arg_str = ast.unparse(child.args[0]) if hasattr(ast, "unparse") else "expr"
                                    tautologies.append({
                                        "file": filename,
                                        "test_name": test_name,
                                        "line": getattr(child, "lineno", line_no),
                                        "assertion": f"self.assertIsNot({arg_str}, {arg_str})",
                                        "reason": "self.assertIsNot compares identical expressions",
                                    })
                            elif attr == "assertTrue" and len(child.args) >= 1:
                                if isinstance(child.args[0], ast.Constant) and child.args[0].value is True:
                                    tautologies.append({
                                        "file": filename,
                                        "test_name": test_name,
                                        "line": getattr(child, "lineno", line_no),
                                        "assertion": "self.assertTrue(True)",
                                        "reason": "self.assertTrue(True) is a self-referential tautology",
                                    })
                                elif isinstance(child.args[0], ast.Compare) and len(child.args[0].comparators) == 1:
                                    if ast.dump(child.args[0].left) == ast.dump(child.args[0].comparators[0]):
                                        tautologies.append({
                                            "file": filename,
                                            "test_name": test_name,
                                            "line": getattr(child, "lineno", line_no),
                                            "assertion": "self.assertTrue(expr == expr)",
                                            "reason": "self.assertTrue compares identical expressions",
                                        })
                            elif attr == "assertFalse" and len(child.args) >= 1:
                                if isinstance(child.args[0], ast.Constant) and child.args[0].value is False:
                                    tautologies.append({
                                        "file": filename,
                                        "test_name": test_name,
                                        "line": getattr(child, "lineno", line_no),
                                        "assertion": "self.assertFalse(False)",
                                        "reason": "self.assertFalse(False) is a self-referential tautology",
                                    })

        except Exception:
            for idx, line in enumerate(lines, start=1):
                m_fn = re.search(r"^\s*def\s+(test_[A-Za-z0-9_]+)", line)
                if m_fn:
                    test_name = m_fn.group(1)
                    prec_start = idx
                    prec_lines = []
                    for i in range(idx - 2, -1, -1):
                        l_str = lines[i].strip()
                        if not l_str:
                            prec_start = i + 1
                            continue
                        if l_str.startswith(("#", "//", "/*", "*", "@")):
                            prec_lines.append(l_str)
                            prec_start = i + 1
                        else:
                            break
                    prev_text = "\n".join(reversed(prec_lines))
                    next_text = "\n".join(lines[idx:min(len(lines), idx + 3)])
                    tags = self._extract_tags_from_text(prev_text + "\n" + next_text + "\n" + test_name, valid_spec_tags)
                    test_cases.append({
                        "file": filename,
                        "test_name": test_name,
                        "line": idx,
                        "prec_start": prec_start,
                        "tags": tags,
                    })

            for i, tc in enumerate(test_cases):
                if i + 1 < len(test_cases):
                    tc["end_line"] = max(tc["line"], test_cases[i + 1].get("prec_start", test_cases[i + 1]["line"]) - 1)
                else:
                    tc["end_line"] = len(lines)

            for pattern, reason in self.PY_TAUTOLOGY_PATTERNS:
                for idx, line in enumerate(lines, start=1):
                    if pattern.search(line):
                        if not any(t["line"] == idx and t["file"] == filename for t in tautologies):
                            tautologies.append({
                                "file": filename,
                                "test_name": "",
                                "line": idx,
                                "assertion": line.strip(),
                                "reason": reason,
                            })

        return test_cases, tautologies

    def _scan_ts_content(
        self,
        filename: str,
        content: str,
        valid_spec_tags: Set[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        test_cases: List[Dict[str, Any]] = []
        tautologies: List[Dict[str, Any]] = []
        lines = content.splitlines()

        for idx, line in enumerate(lines, start=1):
            m = re.search(r"""\b(?:it|test)\s*\(\s*['"`](.+?)['"`]""", line)
            test_name = m.group(1) if m else ""
            if m:
                prec_start = idx
                prec_lines = []
                for i in range(idx - 2, -1, -1):
                    l_str = lines[i].strip()
                    if not l_str:
                        prec_start = i + 1
                        continue
                    if l_str.startswith(("//", "/*", "*", "#")):
                        prec_lines.append(l_str)
                        prec_start = i + 1
                    else:
                        break
                prev_text = "\n".join(reversed(prec_lines))
                tags = self._extract_tags_from_text(test_name + "\n" + prev_text, valid_spec_tags)
                test_cases.append({
                    "file": filename,
                    "test_name": test_name,
                    "line": idx,
                    "prec_start": prec_start,
                    "tags": tags,
                })

        for i, tc in enumerate(test_cases):
            if i + 1 < len(test_cases):
                tc["end_line"] = max(tc["line"], test_cases[i + 1].get("prec_start", test_cases[i + 1]["line"]) - 1)
            else:
                tc["end_line"] = len(lines)

        for idx, line in enumerate(lines, start=1):
            for pattern, reason in self.TS_TAUTOLOGY_PATTERNS:
                m_t = pattern.search(line)
                if m_t:
                    if m_t.lastindex and m_t.lastindex >= 2:
                        left = m_t.group(1).strip()
                        right = m_t.group(2).strip()
                        if left == right:
                            tautologies.append({
                                "file": filename,
                                "test_name": "",
                                "line": idx,
                                "assertion": line.strip(),
                                "reason": reason.format(left=left),
                            })
                    else:
                        tautologies.append({
                            "file": filename,
                            "test_name": "",
                            "line": idx,
                            "assertion": line.strip(),
                            "reason": reason,
                        })

        return test_cases, tautologies

    def _scan_generic_content(
        self,
        filename: str,
        content: str,
        valid_spec_tags: Set[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        test_cases: List[Dict[str, Any]] = []
        tautologies: List[Dict[str, Any]] = []
        lines = content.splitlines()

        for idx, line in enumerate(lines, start=1):
            m = re.search(r"^\s*(?:function\s+)?(test_[A-Za-z0-9_]+)\s*\(\s*\)", line)
            if m:
                test_name = m.group(1)
                prec_start = idx
                prec_lines = []
                for i in range(idx - 2, -1, -1):
                    l_str = lines[i].strip()
                    if not l_str:
                        prec_start = i + 1
                        continue
                    if l_str.startswith(("#", "//", "/*", "*", "@")):
                        prec_lines.append(l_str)
                        prec_start = i + 1
                    else:
                        break
                prev_text = "\n".join(reversed(prec_lines))
                tags = self._extract_tags_from_text(test_name + "\n" + prev_text, valid_spec_tags)
                test_cases.append({
                    "file": filename,
                    "test_name": test_name,
                    "line": idx,
                    "prec_start": prec_start,
                    "tags": tags,
                })

        for i, tc in enumerate(test_cases):
            if i + 1 < len(test_cases):
                tc["end_line"] = max(tc["line"], test_cases[i + 1].get("prec_start", test_cases[i + 1]["line"]) - 1)
            else:
                tc["end_line"] = len(lines)

        if not test_cases:
            tags = self._extract_tags_from_text(content, valid_spec_tags)
            if tags:
                test_cases.append({
                    "file": filename,
                    "test_name": Path(filename).name,
                    "line": 1,
                    "tags": tags,
                })

        return test_cases, tautologies

    def _discover_modified_test_files(self, root: Path) -> List[Path]:
        modified: List[Path] = []
        try:
            cmd = ["git", "diff", "--name-only", "HEAD"]
            res = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line and any(pattern in line for pattern in ("test", "spec")):
                        f_path = root / line
                        if f_path.is_file() and f_path not in modified:
                            modified.append(f_path)

            status_cmd = ["git", "status", "--porcelain"]
            status_res = subprocess.run(status_cmd, cwd=str(root), capture_output=True, text=True)
            if status_res.returncode == 0:
                for line in status_res.stdout.splitlines():
                    line = line.strip()
                    if len(line) > 3:
                        path_str = line[3:].strip()
                        if any(pattern in path_str for pattern in ("test", "spec")):
                            f_path = root / path_str
                            if f_path.is_file() and f_path not in modified:
                                modified.append(f_path)
        except Exception:
            pass
        return modified

    def reject_hallucinated_tags(
        self,
        test_files: Optional[Union[List[Union[Path, str]], Path, str, Dict[str, str]]] = None,
        spec_file: Optional[Union[Path, str, SpecAST]] = None,
        repo_root: Optional[Union[Path, str]] = None,
        test_contents: Optional[Dict[str, str]] = None,
        spec_content: Optional[str] = None,
        strict: bool = True,
        spec_ast: Optional[SpecAST] = None,
    ) -> Dict[str, Any]:
        """
        [ERR-TAUT-01]
        Cross-references scenario tags found in test files against the authoritative Living Specification AST,
        rejecting tests referencing hallucinated, misspelled, or non-existent scenario tags and enforcing
        tag syntax rules with line-specific rejection diagnostics.
        """
        root = Path(repo_root) if repo_root else Path.cwd()

        if isinstance(test_files, dict) and test_contents is None:
            test_contents = test_files
            test_files = None
        elif isinstance(test_files, SpecAST) and spec_ast is None:
            spec_ast = test_files
            test_files = None

        if isinstance(spec_file, SpecAST) and spec_ast is None:
            spec_ast = spec_file
            spec_file = None
        elif isinstance(spec_file, dict) and test_contents is None:
            test_contents = spec_file
            spec_file = None
        elif isinstance(spec_file, list) and test_files is None:
            test_files = spec_file
            spec_file = None

        spec_path_str: str = ""
        if spec_ast is not None:
            spec_path_str = getattr(spec_ast, "title", "<spec_ast>") or "<spec_ast>"
        elif spec_content:
            spec_ast = self.parser.parse_string(spec_content)
            spec_path_str = "<memory_spec>"
        elif spec_file:
            s_path = Path(spec_file)
            if not s_path.is_absolute():
                s_path = root / s_path
            if not s_path.is_file():
                return {
                    "valid": False,
                    "spec_file": str(s_path),
                    "total_test_files": 0,
                    "total_test_cases": 0,
                    "tagged_cases": [],
                    "untagged_cases": [],
                    "invalid_tags": [],
                    "hallucinated_tags": [],
                    "syntax_errors": [],
                    "tautological_assertions": [],
                    "valid_spec_tags": [],
                    "matched_tags": [],
                    "unmatched_tags": [],
                    "errors": [f"Specification file not found: {s_path}"],
                    "advisory_notices": [],
                }
            spec_ast = self.parser.parse_file(s_path)
            spec_path_str = str(s_path)
        else:
            disc_spec, _ = auto_discover_spec_and_issues(None, None, workspace_root=root)
            if disc_spec and disc_spec.is_file():
                spec_ast = self.parser.parse_file(disc_spec)
                spec_path_str = str(disc_spec)
            else:
                return {
                    "valid": False,
                    "spec_file": "",
                    "total_test_files": 0,
                    "total_test_cases": 0,
                    "tagged_cases": [],
                    "untagged_cases": [],
                    "invalid_tags": [],
                    "hallucinated_tags": [],
                    "syntax_errors": [],
                    "tautological_assertions": [],
                    "valid_spec_tags": [],
                    "matched_tags": [],
                    "unmatched_tags": [],
                    "errors": ["Specification file not provided and none could be auto-discovered."],
                    "advisory_notices": [],
                }

        spec_path_obj = (
            Path(spec_path_str)
            if spec_path_str and not spec_path_str.startswith("<")
            else None
        )
        valid_spec_tags_map = self.extract_spec_tags(
            spec_ast, spec_file_path=spec_path_obj, repo_root=root
        )
        valid_spec_tags_set = set(valid_spec_tags_map.keys())

        file_map: Dict[str, str] = {}
        if test_contents:
            file_map = dict(test_contents)
        else:
            paths_to_read: List[Path] = []
            if test_files:
                if isinstance(test_files, (str, Path)):
                    test_files = [test_files]
                for p in test_files:
                    path_obj = Path(p)
                    if not path_obj.is_absolute():
                        path_obj = root / path_obj
                    if path_obj.is_file():
                        paths_to_read.append(path_obj)
                    elif path_obj.is_dir():
                        for ext in ("*.py", "*.ts", "*.tsx", "*.js", "*.sh"):
                            paths_to_read.extend(path_obj.glob(f"**/{ext}"))
            else:
                discovered = self._discover_modified_test_files(root)
                paths_to_read.extend(discovered)
                if not paths_to_read:
                    tests_dir = root / "tests"
                    if tests_dir.is_dir():
                        for ext in ("test_*.py", "*_test.py", "*.test.ts", "*.test.js"):
                            paths_to_read.extend(tests_dir.glob(ext))

            for p_obj in paths_to_read:
                try:
                    rel = p_obj.relative_to(root)
                    rel_str = str(rel)
                except ValueError:
                    rel_str = str(p_obj)
                if p_obj.is_file():
                    try:
                        file_map[rel_str] = p_obj.read_text(encoding="utf-8")
                    except Exception:
                        pass

        if not file_map:
            return {
                "valid": False,
                "spec_file": spec_path_str,
                "total_test_files": 0,
                "total_test_cases": 0,
                "tagged_cases": [],
                "untagged_cases": [],
                "invalid_tags": [],
                "hallucinated_tags": [],
                "syntax_errors": [],
                "tautological_assertions": [],
                "valid_spec_tags": sorted(list(valid_spec_tags_set)),
                "matched_tags": [],
                "unmatched_tags": [],
                "errors": ["No test files found to validate for scenario tagging."],
                "advisory_notices": [],
            }

        tagged_cases: List[Dict[str, Any]] = []
        untagged_cases: List[Dict[str, Any]] = []
        invalid_tags: List[Dict[str, Any]] = []
        hallucinated_tags: List[Dict[str, Any]] = []
        syntax_errors: List[Dict[str, Any]] = []
        tautological_assertions: List[Dict[str, Any]] = []
        matched_tags: Set[str] = set()
        unmatched_tags: Set[str] = set()
        errors: List[str] = []
        advisory_notices: List[str] = []
        total_test_cases = 0

        for fname, content in file_map.items():
            if fname.endswith(".py"):
                cases, tauts = self._scan_python_content(fname, content, valid_spec_tags_set)
            elif fname.endswith((".ts", ".tsx", ".js", ".mjs", ".cjs")):
                cases, tauts = self._scan_ts_content(fname, content, valid_spec_tags_set)
            else:
                cases, tauts = self._scan_generic_content(fname, content, valid_spec_tags_set)

            total_test_cases += len(cases)
            tautological_assertions.extend(tauts)

            file_tags, file_syntax_errors = self._scan_file_scenario_tags_and_syntax(
                fname, content, valid_spec_tags_set
            )

            for syn in file_syntax_errors:
                test_name = ""
                for c in cases:
                    start_l = c.get("prec_start", c["line"])
                    end_l = c.get("end_line", c["line"])
                    if start_l <= syn["line"] <= end_l:
                        test_name = c["test_name"]
                        break
                else:
                    if len(cases) == 1:
                        test_name = cases[0]["test_name"]
                entry = {
                    "file": fname,
                    "test_name": test_name,
                    "line": syn["line"],
                    "tag_line": syn["line"],
                    "tag": syn["raw"],
                    "reason": syn["reason"],
                }
                invalid_tags.append(entry)
                syntax_errors.append(syn)
                errors.append(
                    f"Malformed scenario tag syntax '{syn['raw']}' in {fname}:{syn['line']}: {syn['reason']}"
                )

            processed_lines_for_tags: Set[Tuple[str, int, str]] = set()
            for tag_info in file_tags:
                tag = tag_info["tag"]
                t_line = tag_info["line"]
                t_key = (fname, t_line, tag)
                if t_key in processed_lines_for_tags:
                    continue
                processed_lines_for_tags.add(t_key)

                test_name = ""
                for c in cases:
                    start_l = c.get("prec_start", c["line"])
                    end_l = c.get("end_line", c["line"])
                    if start_l <= t_line <= end_l:
                        test_name = c["test_name"]
                        if tag not in c.get("tags", []):
                            c.setdefault("tags", []).append(tag)
                        break
                else:
                    if len(cases) == 1:
                        test_name = cases[0]["test_name"]
                        if tag not in cases[0].get("tags", []):
                            cases[0].setdefault("tags", []).append(tag)

                if tag in valid_spec_tags_set:
                    matched_tags.add(tag)
                    tagged_cases.append({
                        "file": fname,
                        "test_name": test_name,
                        "line": t_line,
                        "tag": tag,
                    })
                else:
                    unmatched_tags.add(tag)
                    spec_name = Path(spec_path_str).name
                    reason = f"Scenario tag '[{tag}]' does not exist in target specification '{spec_name}'"
                    entry = {
                        "file": fname,
                        "test_name": test_name,
                        "line": t_line,
                        "tag_line": t_line,
                        "tag": tag,
                        "reason": reason,
                    }
                    invalid_tags.append(entry)
                    hallucinated_tags.append(entry)
                    errors.append(
                        f"Unrecognized scenario tag '[{tag}]' in {fname}:{t_line} does not exist in specification '{spec_path_str}'"
                    )

            for c in cases:
                if not c.get("tags"):
                    untagged_cases.append({
                        "file": fname,
                        "test_name": c["test_name"],
                        "line": c["line"],
                        "reason": "Missing scenario tag (e.g. // Scenario: [TAG-xx])",
                    })

        dedup_invalid: List[Dict[str, Any]] = []
        seen_inv: Set[Tuple[str, int, str]] = set()
        for itm in invalid_tags:
            k = (itm["file"], itm["line"], itm["tag"])
            if k not in seen_inv:
                seen_inv.add(k)
                dedup_invalid.append(itm)
        invalid_tags = dedup_invalid

        dedup_err: List[str] = []
        seen_err: Set[str] = set()
        for err in errors:
            if err not in seen_err:
                seen_err.add(err)
                dedup_err.append(err)
        errors = dedup_err

        is_valid = (
            len(invalid_tags) == 0
            and len(unmatched_tags) == 0
            and len(syntax_errors) == 0
        )

        return {
            "valid": is_valid,
            "spec_file": spec_path_str,
            "total_test_files": len(file_map),
            "total_test_cases": total_test_cases,
            "tagged_cases": tagged_cases,
            "untagged_cases": untagged_cases,
            "invalid_tags": invalid_tags,
            "hallucinated_tags": hallucinated_tags,
            "syntax_errors": syntax_errors,
            "tautological_assertions": tautological_assertions,
            "valid_spec_tags": sorted(list(valid_spec_tags_set)),
            "matched_tags": sorted(list(matched_tags)),
            "unmatched_tags": sorted(list(unmatched_tags)),
            "errors": errors,
            "advisory_notices": advisory_notices,
        }

    def validate(
        self,
        test_files: Optional[Union[List[Union[Path, str]], Path, str, Dict[str, str]]] = None,
        spec_file: Optional[Union[Path, str, SpecAST]] = None,
        repo_root: Optional[Union[Path, str]] = None,
        test_contents: Optional[Dict[str, str]] = None,
        spec_content: Optional[str] = None,
        strict: bool = True,
        spec_ast: Optional[SpecAST] = None,
    ) -> Dict[str, Any]:
        """
        [TAUT-01] [TAUT-02] [ERR-TAUT-01]
        Validates scenario tag citations, absence of tautological assertions,
        and full scenario traceability against the declared Living Specification.
        """
        report = self.reject_hallucinated_tags(
            test_files=test_files,
            spec_file=spec_file,
            repo_root=repo_root,
            test_contents=test_contents,
            spec_content=spec_content,
            strict=strict,
            spec_ast=spec_ast,
        )

        for u in report["untagged_cases"]:
            err_msg = f"Untagged test case in {u['file']}:{u['line']} ('{u['test_name']}'): missing living spec scenario tag"
            if err_msg not in report["errors"]:
                report["errors"].append(err_msg)

        for taut in report["tautological_assertions"]:
            err_msg = f"Tautological assertion in {taut['file']}:{taut['line']} ('{taut['assertion']}'): {taut['reason']}"
            if err_msg not in report["errors"]:
                report["errors"].append(err_msg)

        report["valid"] = (
            len(report["invalid_tags"]) == 0
            and len(report["unmatched_tags"]) == 0
            and len(report["syntax_errors"]) == 0
            and len(report["untagged_cases"]) == 0
            and len(report["tautological_assertions"]) == 0
            and len(report["tagged_cases"]) > 0
        )
        return report


def validate_test_scenario_tags(
    test_files: Optional[Union[List[Union[Path, str]], Path, str, Dict[str, str]]] = None,
    spec_file: Optional[Union[Path, str, SpecAST]] = None,
    repo_root: Optional[Union[Path, str]] = None,
    test_contents: Optional[Dict[str, str]] = None,
    spec_content: Optional[str] = None,
    strict: bool = True,
    spec_ast: Optional[SpecAST] = None,
) -> Dict[str, Any]:
    """
    [TAUT-01] [TAUT-02] [ERR-TAUT-01]
    Top-level entrypoint verifying that test implementations cite declared Living Specification
    scenario tags, lack tautological assertions, and reference valid scenarios.
    """
    verifier = AntiTautologyVerifier()
    return verifier.validate(
        test_files=test_files,
        spec_file=spec_file,
        repo_root=repo_root,
        test_contents=test_contents,
        spec_content=spec_content,
        strict=strict,
        spec_ast=spec_ast,
    )


def reject_hallucinated_tags(
    test_files: Optional[Union[List[Union[Path, str]], Path, str, Dict[str, str]]] = None,
    spec_file: Optional[Union[Path, str, SpecAST]] = None,
    repo_root: Optional[Union[Path, str]] = None,
    test_contents: Optional[Dict[str, str]] = None,
    spec_content: Optional[str] = None,
    strict: bool = True,
    spec_ast: Optional[SpecAST] = None,
) -> Dict[str, Any]:
    """
    [ERR-TAUT-01]
    Cross-references scenario tags found in test files against the authoritative Living Specification AST,
    rejecting tests referencing hallucinated, misspelled, or non-existent scenario tags and enforcing
    tag syntax rules with line-specific rejection diagnostics.
    """
    verifier = AntiTautologyVerifier()
    return verifier.reject_hallucinated_tags(
        test_files=test_files,
        spec_file=spec_file,
        repo_root=repo_root,
        test_contents=test_contents,
        spec_content=spec_content,
        strict=strict,
        spec_ast=spec_ast,
    )


def auto_discover_spec_and_issues(
    delta_file: Optional[Path | str] = None,
    issues_dir: Optional[Path | str] = None,
    workspace_root: Optional[Path | str] = None,
) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Auto-discovers specification file and issues directory if either or both are omitted.
    Searches active .scratch/<feature-slug>/ candidates sorted by recency (mtime).
    """
    root = Path(workspace_root) if workspace_root else Path.cwd()

    if delta_file:
        d_path = Path(delta_file)
        if not d_path.is_absolute():
            d_path = root / d_path
        if d_path.is_dir():
            cand_spec = d_path / "spec.md"
            cand_issues = Path(issues_dir) if issues_dir else (d_path / "issues")
            if not cand_issues.is_absolute():
                cand_issues = root / cand_issues
            if cand_spec.is_file() and cand_issues.is_dir():
                return cand_spec, cand_issues
        if not d_path.is_file():
            return None, None
        if issues_dir:
            i_path = Path(issues_dir)
            if not i_path.is_absolute():
                i_path = root / i_path
            return (d_path, i_path) if i_path.is_dir() else (d_path, None)

        candidate_issues = d_path.parent / "issues"
        if candidate_issues.is_dir():
            return d_path, candidate_issues
        candidate_sibling = root / "issues"
        if candidate_sibling.is_dir():
            return d_path, candidate_sibling
        return d_path, None

    scratch_dir = root / ".scratch"
    if scratch_dir.is_dir():
        candidates: List[Tuple[float, Path, Path]] = []
        for p in scratch_dir.iterdir():
            if p.is_dir():
                s_file = p / "spec.md"
                i_dir = p / "issues"
                if s_file.is_file() and i_dir.is_dir():
                    mtime = max(s_file.stat().st_mtime, i_dir.stat().st_mtime)
                    candidates.append((mtime, s_file, i_dir))
        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            return candidates[0][1], candidates[0][2]

    root_spec = root / "spec.md"
    root_issues = root / "issues"
    if root_spec.is_file() and root_issues.is_dir():
        return root_spec, root_issues

    return None, None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Core zero-dependency specification engine & reconciler for Antigravity Plugins.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Operational mode")

    # Mode 1: --validate <spec_file> [--strict] [--json]
    val_parser = subparsers.add_parser("validate", help="Validate specification grammar, headers, and scenarios")
    val_parser.add_argument("spec_file", help="Path to specification file (Delta or Living)")
    val_parser.add_argument("--strict", action="store_true", help="Enforce strict tagging and step ordering invariants")
    val_parser.add_argument("--json", action="store_true", help="Output structured JSON payload")

    # Mode 2: --reconcile <delta_file> [--target <living_spec>] [--dry-run]
    rec_parser = subparsers.add_parser("reconcile", help="Reconcile delta spec into living spec with Additive Scenario Merging")
    rec_parser.add_argument("delta_file", nargs="?", default=None, help="Path to Delta Specification (.scratch/*/spec.md)")
    rec_parser.add_argument("--target", help="Explicit target Living Specification (default: inferred from Target Spec header)")
    rec_parser.add_argument("--dry-run", action="store_true", help="Output reconciled spec to stdout without modifying disk")

    # Mode 3: --check-coverage [<delta_file>] [<issues_dir>] [--target <living_spec>] [--json]
    cov_parser = subparsers.add_parser("check-coverage", help="Verify delta spec requirements and scenarios map to issue files")
    cov_parser.add_argument("delta_file", nargs="?", default=None, help="Path to Delta Specification file")
    cov_parser.add_argument("issues_dir", nargs="?", default=None, help="Directory containing implementation issue markdown files")
    cov_parser.add_argument("--target", help="Explicit target living specification (default: inferred from Target Spec header)")
    cov_parser.add_argument("--json", action="store_true", help="Output structured JSON coverage report")

    # Mode 4: --check-dag <workflow_schema> <target_phase> [--context <dir>] [--json]
    dag_parser = subparsers.add_parser("check-dag", help="Verify declarative workflow DAG prerequisite phase completion")
    dag_parser.add_argument("workflow_schema", help="Path to workflow DAG schema JSON")
    dag_parser.add_argument("target_phase", help="Target development lifecycle phase to verify")
    dag_parser.add_argument("--context", help="Optional feature scratch context directory")
    dag_parser.add_argument("--json", action="store_true", help="Output structured JSON DAG report")

    # Mode 5: --diff-check <delta_file> [--target <living_spec>] [--restore] [--json]
    diff_parser = subparsers.add_parser("diff-check", help="Mechanical baseline diff check between delta spec and living spec")
    diff_parser.add_argument("delta_file", nargs="?", default=None, help="Path to delta specification file")
    diff_parser.add_argument("--target", help="Explicit target living specification (default: inferred from Target Spec header)")
    diff_parser.add_argument("--restore", action="store_true", help="Restore omitted baseline scenarios in-place into delta specification")
    diff_parser.add_argument("--json", action="store_true", help="Output structured JSON diff report")

    # Mode 6: --verify-seams <spec_file> [--repo-root <dir>] [--json] [--strict]
    seam_parser = subparsers.add_parser("verify-seams", help="Physically verify testing seams declared in specification exist on disk")
    seam_parser.add_argument("spec_file", nargs="?", default=None, help="Path to specification file")
    seam_parser.add_argument("--repo-root", default=".", help="Root directory of repository")
    seam_parser.add_argument("--json", action="store_true", help="Output structured JSON report")
    seam_parser.add_argument("--strict", action="store_true", help="Fail if zero testing seams are declared or verified")

    # Mode 7: --validate-tags / validate-tags [<test_files>...] [--spec <spec_file>] [--repo-root <dir>] [--json]
    tag_parser = subparsers.add_parser("validate-tags", help="Validate anti-tautology scenario tags in test suites against Living Spec")
    tag_parser.add_argument("test_files", nargs="*", default=None, help="Test files to validate (default: auto-detect via git diff or tests/)")
    tag_parser.add_argument("--spec", help="Path to specification file (default: auto-discover .scratch/*/spec.md)")
    tag_parser.add_argument("--repo-root", default=".", help="Root directory of repository")
    tag_parser.add_argument("--json", action="store_true", help="Output structured JSON validation report")

    return parser


def parse_cli_args(argv: List[str]) -> Tuple[str, Dict[str, Any]]:
    """Normalizes both positional subcommands and flag-style commands (--validate, etc.)."""
    normalized: List[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--validate":
            normalized.append("validate")
        elif arg == "--reconcile":
            normalized.append("reconcile")
        elif arg == "--check-coverage":
            normalized.append("check-coverage")
        elif arg == "--check-dag":
            normalized.append("check-dag")
        elif arg == "--diff-check":
            normalized.append("diff-check")
        elif arg == "--verify-seams":
            normalized.append("verify-seams")
        elif arg == "--validate-tags":
            normalized.append("validate-tags")
        else:
            normalized.append(arg)
        i += 1

    parser = build_arg_parser()
    parsed = parser.parse_args(normalized)
    return parsed.command, vars(parsed)


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        build_arg_parser().print_help()
        return 1

    try:
        command, args = parse_cli_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    if command == "validate":
        validator = SpecValidator()
        result = validator.validate_file(args["spec_file"], strict=args.get("strict", False))
        if args.get("json"):
            print(json.dumps(result, indent=2))
        else:
            if result["valid"]:
                stats = result["stats"]
                stats_str = ", ".join(f"{k}: {v}" for k, v in stats.items() if k != "domain")
                print(f"✓ Valid Specification: {args['spec_file']} (Domain: {stats.get('domain', 'n/a')}, {stats_str})")
            else:
                print(f"✗ Invalid Specification: {args['spec_file']}", file=sys.stderr)
                for err in result["errors"]:
                    print(f"  - {err}", file=sys.stderr)
        return 0 if result["valid"] else 1

    elif command == "reconcile":
        reconciler = SpecReconciler()
        dry_run = args.get("dry_run", False)
        delta_arg = args.get("delta_file")
        if not delta_arg:
            discovered_spec, _ = auto_discover_spec_and_issues(None, None)
            if not discovered_spec:
                print("Error: Delta spec not provided and auto-discovery found no .scratch/*/spec.md", file=sys.stderr)
                return 1
            delta_arg = str(discovered_spec)
        try:
            output = reconciler.reconcile_files(
                delta_path=delta_arg,
                target_path=args.get("target"),
                dry_run=dry_run,
            )
            if dry_run:
                sys.stdout.write(output)
            else:
                target_desc = args.get("target") or "inferred living spec target"
                print(f"✓ Successfully reconciled {delta_arg} into {target_desc}")
            return 0
        except Exception as exc:
            print(f"Error during reconciliation: {exc}", file=sys.stderr)
            return 1

    elif command == "check-coverage":
        checker = SpecCoverageChecker()
        delta_arg = args.get("delta_file")
        issues_arg = args.get("issues_dir")

        delta_path, issues_path = auto_discover_spec_and_issues(delta_arg, issues_arg)
        if not delta_path or not issues_path:
            err_msg = (
                f"Specification or issues directory not found. "
                f"(delta_file: {delta_arg or 'auto-discover'}, issues_dir: {issues_arg or 'auto-discover'})"
            )
            if args.get("json"):
                print(json.dumps({"covered": False, "coverage_percent": 0.0, "errors": [err_msg]}))
            else:
                print(f"✗ {err_msg}", file=sys.stderr)
            return 1

        try:
            rel_delta = delta_path.relative_to(Path.cwd())
        except ValueError:
            rel_delta = delta_path
        try:
            rel_issues = issues_path.relative_to(Path.cwd())
        except ValueError:
            rel_issues = issues_path

        report = checker.check_coverage_file(delta_path, issues_path, target_spec=args.get("target"))
        if not delta_arg or not issues_arg:
            report["auto_discovered"] = {
                "delta_file": str(rel_delta),
                "issues_dir": str(rel_issues),
            }

        if args.get("json"):
            print(json.dumps(report, indent=2))
        else:
            if not delta_arg or not issues_arg:
                print(f"Auto-discovered: {rel_delta} (issues: {rel_issues})")
            if report["covered"]:
                print(
                    f"✓ 100% Spec Coverage: All {report['total_requirements']} requirements and "
                    f"{report['total_scenarios']} scenarios mapped across issues."
                )
            else:
                print(f"✗ Incomplete Spec Coverage or Ticket Anatomy Failure ({report['coverage_percent']}%):", file=sys.stderr)
                if report.get("anatomy_errors"):
                    for a_err in report["anatomy_errors"]:
                        print(f"  {a_err}", file=sys.stderr)
                if report["uncovered_requirements"]:
                    print(f"  Uncovered Requirements: {', '.join(report['uncovered_requirements'])}", file=sys.stderr)
                if report["uncovered_scenarios"]:
                    print(f"  Uncovered Scenarios: {', '.join(report['uncovered_scenarios'])}", file=sys.stderr)
        return 0 if report["covered"] else 1

    elif command == "check-dag":
        checker = WorkflowDAGChecker()
        result = checker.check_dag(
            schema_path=args["workflow_schema"],
            target_phase=args["target_phase"],
            context_dir=args.get("context"),
        )
        if args.get("json"):
            print(json.dumps(result, indent=2))
        else:
            if result["valid"] and result["satisfied"]:
                prereqs_str = ", ".join(result["prerequisites"]) if result["prerequisites"] else "none"
                print(f"✓ Workflow DAG check passed for phase: '{args['target_phase']}' (Prerequisites: {prereqs_str})")
            else:
                print(f"✗ Workflow DAG check failed for phase: '{args['target_phase']}'", file=sys.stderr)
                for err in result.get("errors", []):
                    print(f"  - {err}", file=sys.stderr)
                for missing in result.get("missing_artifacts", []):
                    print(f"  - Missing prerequisite artifact: {missing}", file=sys.stderr)
        return 0 if (result["valid"] and result["satisfied"]) else 1

    elif command == "diff-check":
        checker = SpecBaselineDiffChecker()
        delta_arg = args.get("delta_file")
        if not delta_arg:
            discovered_spec, _ = auto_discover_spec_and_issues(None, None)
            if not discovered_spec:
                print("Error: Delta spec not provided and auto-discovery found no .scratch/*/spec.md", file=sys.stderr)
                return 1
            delta_arg = str(discovered_spec)

        target_arg = args.get("target")
        restore = args.get("restore", False)
        try:
            report = checker.diff_check_files(delta_arg, target_path=target_arg, restore=restore)
            if args.get("json"):
                print(json.dumps(report, indent=2))
            else:
                if report.get("restored"):
                    print(f"✓ Baseline Diff Check: Restored {len(report.get('advisory_notices', []))} omitted baseline scenario(s) in-place.")
                    for notice in report.get("advisory_notices", []):
                        print(f"  {notice}")
                elif report["valid"]:
                    print(f"✓ Baseline Diff Check passed: All {report['total_baseline']} baseline scenarios intact.")
                else:
                    print(f"✗ Baseline Diff Check failed: {len(report['omitted_scenarios'])} baseline scenario(s) silently omitted:", file=sys.stderr)
                    for om in report["omitted_scenarios"]:
                        tag_str = f"[{om['tag']}] " if om.get("tag") else ""
                        print(f"  - Requirement '{om['requirement']}': Scenario {tag_str}{om['name']}", file=sys.stderr)
            return 0 if report["valid"] or (restore and report.get("restored")) else 1
        except Exception as exc:
            if args.get("json"):
                print(json.dumps({"valid": False, "errors": [str(exc)]}, indent=2))
            else:
                print(f"Error during baseline diff check: {exc}", file=sys.stderr)
            return 1

    elif command == "verify-seams":
        verifier = SpecSeamVerifier()
        spec_arg = args.get("spec_file")
        if not spec_arg:
            discovered_spec, _ = auto_discover_spec_and_issues(None, None)
            if not discovered_spec:
                err_msg = "Spec file not provided and auto-discovery found no .scratch/*/spec.md"
                if args.get("json"):
                    print(json.dumps({"valid": False, "errors": [err_msg]}, indent=2))
                else:
                    print(f"Error: {err_msg}", file=sys.stderr)
                return 1
            spec_arg = str(discovered_spec)
        repo_root = args.get("repo_root", ".")
        try:
            report = verifier.verify_seams_file(spec_arg, repo_root=repo_root)
            strict_mode = bool(args.get("strict"))
            if strict_mode and not report["verified_seams"] and not report.get("greenfield_contracts"):
                report["valid"] = False
                report["errors"].append("Strict mode: No testing seams or greenfield contracts verified in specification")

            if args.get("json"):
                print(json.dumps(report, indent=2))
            else:
                if report["valid"]:
                    seams_str = ", ".join(report["verified_seams"]) if report["verified_seams"] else "none detected"
                    print(f"✓ Physical Seams Verified: {spec_arg} ({len(report['verified_seams'])} physical seams: {seams_str})")
                    for gf in report.get("greenfield_contracts", []):
                        print(f"  [GREENFIELD CONTRACT] {gf}")
                else:
                    print(f"✗ Physical Seam Verification failed: {spec_arg}", file=sys.stderr)
                    for err in report["errors"]:
                        print(f"  - {err}", file=sys.stderr)
            return 0 if report["valid"] else 1
        except Exception as exc:
            if args.get("json"):
                print(json.dumps({"valid": False, "spec_file": spec_arg, "errors": [str(exc)]}, indent=2))
            else:
                print(f"Error during seam verification: {exc}", file=sys.stderr)
            return 1
    elif command == "validate-tags":
        test_files = args.get("test_files")
        spec_arg = args.get("spec")
        repo_root = args.get("repo_root", ".")
        try:
            report = validate_test_scenario_tags(
                test_files=test_files,
                spec_file=spec_arg,
                repo_root=repo_root,
            )
            if args.get("json"):
                print(json.dumps(report, indent=2))
            else:
                if report["valid"]:
                    matched_str = ", ".join(f"[{t}]" for t in report.get("matched_tags", []))
                    print(
                        f"✓ Anti-Tautology Verification Passed: {report['total_test_cases']} test case(s) across "
                        f"{report['total_test_files']} file(s) verified (Tags: {matched_str})."
                    )
                else:
                    print(f"✗ Anti-Tautology Verification Failed ({len(report['errors'])} error(s)):", file=sys.stderr)
                    for err in report["errors"]:
                        print(f"  - {err}", file=sys.stderr)
            return 0 if report["valid"] else 1
        except Exception as exc:
            if args.get("json"):
                print(json.dumps({"valid": False, "errors": [str(exc)]}, indent=2))
            else:
                print(f"Error during anti-tautology tag verification: {exc}", file=sys.stderr)
            return 1

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
