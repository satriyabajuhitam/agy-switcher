#!/usr/bin/env python3
"""
scripts/internal/verify_integrity.py
Core zero-dependency verification engine for Antigravity Plugins.
Validates YAML frontmatter, strict naming invariants, schema conformance,
and internal integrity using Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import json
import logging
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.internal.resolve_ticket import TicketBriefValidationResult
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)


try:
    from scripts.internal import verify_routing
except ImportError:
    try:
        import verify_routing
    except ImportError:
        _parent = Path(__file__).resolve().parent
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        try:
            import verify_routing
        except ImportError:
            verify_routing = None  # type: ignore[assignment]

MAX_EXPANDED_RULE_BYTES: int = 20_000


class FrontmatterParseError(Exception):
    """Raised when YAML frontmatter is missing, unclosed, or invalid."""


class TicketRejectionError(Exception):
    """Raised when an implementation ticket fails the Pre-Flight Ticket Rejection Gate ([REJ-01])."""


@dataclass
class ValidationError:
    """Represents a single validation violation."""
    file_path: Path
    category: str
    message: str
    line: int | None = None

    def format(self, repo_root: Path | None = None) -> str:
        display_path = self.file_path
        if repo_root:
            try:
                display_path = self.file_path.relative_to(repo_root)
            except ValueError:
                pass
        line_str = f":{self.line}" if self.line is not None else ""
        return f"[{self.category}] {display_path}{line_str} -> {self.message}"


def parse_frontmatter(content: str) -> dict[str, Any]:
    """
    Extracts and parses YAML frontmatter delimited by initial `---` lines.
    Zero-dependency stdlib parser supporting:
      - key: scalar (unquoted, single/double quoted, boolean, integer)
      - key: >- or key: | (folded / multiline text blocks)
      - key: list items (  - item)
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontmatterParseError("Missing opening '---' delimiter at start of file.")

    closing_idx = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            closing_idx = idx
            break

    if closing_idx == -1:
        raise FrontmatterParseError("Unclosed frontmatter: missing closing '---' delimiter.")

    yaml_lines = lines[1:closing_idx]
    data: dict[str, Any] = {}

    current_key: str | None = None
    multiline_mode: str | None = None  # ">-", "|", ">"
    multiline_buffer: list[str] = []
    current_list: list[str] | None = None

    def flush_multiline():
        nonlocal current_key, multiline_mode, multiline_buffer
        if current_key and multiline_mode:
            if multiline_mode in (">-", ">"):
                # Folded block: join words with spaces, preserve paragraphs if any
                combined = " ".join(line.strip() for line in multiline_buffer if line.strip())
                data[current_key] = combined
            elif multiline_mode in ("|", "|-"):
                data[current_key] = "\n".join(multiline_buffer).rstrip()
            multiline_mode = None
            multiline_buffer = []

    def flush_list():
        nonlocal current_key, current_list
        if current_key and current_list is not None:
            data[current_key] = current_list
            current_list = None

    for raw_line in yaml_lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        # In multiline string mode, indented lines belong to the current key
        if multiline_mode:
            if line.startswith("  ") or (not stripped and multiline_buffer):
                # Continuation of multiline string
                multiline_buffer.append(line.lstrip() if multiline_mode.startswith(">") else line[2:])
                continue
            else:
                flush_multiline()

        # In list mode, indented list items belong to the current key
        if current_list is not None:
            if stripped.startswith("- "):
                item = stripped[2:].strip().strip("\"'")
                current_list.append(item)
                continue
            elif not stripped:
                continue
            else:
                flush_list()

        # Ignore empty lines or comment-only lines outside multiline mode
        if not stripped or stripped.startswith("#"):
            continue

        # Look for top-level key: value
        if ":" in line:
            parts = line.split(":", 1)
            key = parts[0].strip()
            if not key:
                raise FrontmatterParseError(f"Invalid YAML syntax: missing key before ':' on line: '{line}'")
            val = parts[1].strip()

            if (val.startswith('"') and not val.endswith('"')) or (val.startswith("'") and not val.endswith("'")):
                raise FrontmatterParseError(f"Invalid YAML syntax: unclosed quote in value on line: '{line}'")
            if (val.startswith("[") and not val.endswith("]")) or (val.startswith("{") and not val.endswith("}")):
                raise FrontmatterParseError(f"Invalid YAML syntax: unclosed bracket in value on line: '{line}'")

            if val in (">-", ">", "|", "|-"):
                current_key = key
                multiline_mode = val
                multiline_buffer = []
            elif val == "":
                # Could be start of a list or empty dictionary
                current_key = key
                current_list = []
            else:
                # Direct scalar value
                val_lower = val.lower()
                if val_lower == "true":
                    data[key] = True
                elif val_lower == "false":
                    data[key] = False
                elif val.isdigit():
                    data[key] = int(val)
                elif (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    data[key] = val[1:-1]
                else:
                    data[key] = val
                current_key = None
        else:
            raise FrontmatterParseError(f"Invalid YAML syntax: missing ':' mapping delimiter on line: '{line}'")

    flush_multiline()
    flush_list()

    return data


class FrontmatterValidator:
    """Validates frontmatter and strict naming invariants for skills and agents."""

    TRIGGER_PATTERN: re.Pattern[str] = re.compile(
        r"\buse (this )?when\b|\buse (before|after|during)\b", re.IGNORECASE
    )
    CORE_OPERATIONAL_SKILLS: set[str] = {"tdd", "implement", "code-review", "diagnosing-bugs"}
    PRIMARY_SKILLS: set[str] = {"implement", "diagnosing-bugs"}
    MAX_PRIMARY_SKILL_LINES: int = 250
    REQUIRED_CORE_SECTIONS: list[str] = ["## Common Rationalizations", "## Red Flags"]

    def validate_skill(self, skill_path: Path, max_lines: int | None = None) -> list[ValidationError]:
        errors: list[ValidationError] = []
        try:
            content = skill_path.read_text(encoding="utf-8")
        except Exception as exc:
            return [ValidationError(skill_path, "FILE_READ_ERROR", f"Could not read skill file: {exc}")]

        lines = content.splitlines()
        line_count = len(lines)

        try:
            data = parse_frontmatter(content)
        except FrontmatterParseError as fpe:
            return [ValidationError(skill_path, "SKILL_FRONTMATTER_INVALID", f"FRONTMATTER_MALFORMED: {fpe}")]

        # Validate name
        name = data.get("name")
        expected_name = skill_path.parent.name
        if not name or not isinstance(name, str):
            errors.append(ValidationError(skill_path, "SKILL_FRONTMATTER_INVALID", "Missing or empty required field 'name' (MISSING_FIELD)"))
        elif name != expected_name:
            errors.append(
                ValidationError(
                    skill_path,
                    "STRICT_NAMING_INVARIANT",
                    f"Strict Naming Invariant violation: declared name '{name}' does not match parent directory slug '{expected_name}'",
                )
            )

        # Validate description
        description = data.get("description")
        if not description or not isinstance(description, str) or not description.strip():
            errors.append(ValidationError(skill_path, "SKILL_FRONTMATTER_INVALID", "Missing or empty required field 'description' (MISSING_FIELD)"))
        elif not self.TRIGGER_PATTERN.search(description):
            errors.append(
                ValidationError(
                    skill_path,
                    "TRIGGER_PHRASE_MISSING",
                    f"Skill description lacks explicit trigger phrasing matching '\\buse (this )?when\\b|\\buse (before|after|during)\\b': '{description}'",
                )
            )

        # Mechanical line count ceiling check (<= 250 physical lines for primary skills)
        skill_name = name if isinstance(name, str) and name else expected_name
        effective_max = max_lines if max_lines is not None else (self.MAX_PRIMARY_SKILL_LINES if skill_name in self.PRIMARY_SKILLS else None)
        if effective_max is not None and line_count > effective_max:
            errors.append(
                ValidationError(
                    skill_path,
                    "SKILL_LINE_COUNT_EXCEEDED",
                    f"Primary skill '{skill_name}' exceeds physical line count ceiling ({line_count} > {effective_max} lines)",
                )
            )

        # Validate required anatomy sections for core operational skills
        if skill_name in self.CORE_OPERATIONAL_SKILLS:
            for section in self.REQUIRED_CORE_SECTIONS:
                section_title = section.lstrip("#").strip()
                pattern = rf"^##\s+{re.escape(section_title)}\b"
                if not re.search(pattern, content, re.MULTILINE):
                    errors.append(
                        ValidationError(
                            skill_path,
                            "MISSING_SECTION",
                            f"Core operational skill '{skill_name}' is missing required section '{section}'",
                        )
                    )

        # Validate bidirectional reference navigation & anchor integrity for primary skills
        if skill_name in self.PRIMARY_SKILLS:
            errors.extend(self.validate_skill_reference_navigation(skill_path))

        return errors

    def validate_skill_reference_navigation(self, skill_path: Path) -> list[ValidationError]:
        """
        [EDGE-PROG-DISC-02] Validates bidirectional reference navigation and collision-free anchor integrity
        between primary skill runbooks and their extracted reference documents.
        """
        errors: list[ValidationError] = []
        if skill_path.is_dir():
            skill_dir = skill_path
            skill_file = skill_dir / "SKILL.md"
        else:
            skill_file = skill_path
            skill_dir = skill_path.parent

        if not skill_file.exists():
            return [ValidationError(skill_file, "FILE_READ_ERROR", f"Skill file not found at '{skill_file}'")]

        refs_dir = skill_dir / "references"
        if not refs_dir.exists() or not refs_dir.is_dir():
            return []

        ref_files = sorted(p for p in refs_dir.glob("*.md") if p.is_file())
        if not ref_files:
            return []

        # 1. Extract parent SKILL.md headings and slugs
        parent_headings = self._extract_markdown_headings(skill_file)
        parent_slugs: dict[str, tuple[int, str]] = {
            slug: (line_no, raw_title) for line_no, raw_title, slug in parent_headings
        }

        # 2. Track reference document heading slugs across all reference files
        seen_ref_slugs: dict[str, tuple[str, int, str]] = {}

        link_pattern = re.compile(
            r"[@!]?\[([^\]]*)\]\((<[^>]+>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)"
        )

        for ref_file in ref_files:
            try:
                ref_content = ref_file.read_text(encoding="utf-8")
            except Exception as exc:
                errors.append(ValidationError(ref_file, "FILE_READ_ERROR", f"Could not read reference file: {exc}"))
                continue

            # Check 1: Verified backlink to canonical parent SKILL.md
            has_backlink = False
            in_fence = False
            for line in ref_content.splitlines():
                stripped = line.strip()
                if stripped.startswith("```") or stripped.startswith("~~~"):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    continue

                for m in link_pattern.finditer(line):
                    start_pos, end_pos = m.start(), m.end()
                    is_code = False
                    for span in re.finditer(r"(`+)(.+?)\1", line):
                        if span.start() <= start_pos and end_pos <= span.end():
                            is_code = True
                            break
                    if is_code:
                        continue

                    raw_target = m.group(2).strip()
                    if raw_target.startswith("<") and raw_target.endswith(">"):
                        raw_target = raw_target[1:-1]
                    url_part = raw_target.split("#", 1)[0].strip()
                    clean_url = unquote(url_part)

                    if clean_url in ("../SKILL.md", "SKILL.md"):
                        has_backlink = True
                        break
                    elif clean_url.endswith("SKILL.md"):
                        try:
                            resolved_p = (ref_file.parent / clean_url).resolve()
                            if resolved_p == skill_file.resolve():
                                has_backlink = True
                                break
                        except Exception:
                            pass
                if has_backlink:
                    break

            if not has_backlink:
                errors.append(
                    ValidationError(
                        ref_file,
                        "MISSING_BACKLINK",
                        f"Extracted reference document '{ref_file.name}' lacks a verified backlink navigating back to canonical parent SKILL.md",
                    )
                )

            # Check 2: Heading anchor collisions with parent SKILL.md and other reference files
            ref_headings = self._extract_markdown_headings(ref_file)
            for line_no, raw_title, slug in ref_headings:
                if slug in parent_slugs:
                    parent_line, parent_title = parent_slugs[slug]
                    errors.append(
                        ValidationError(
                            ref_file,
                            "ANCHOR_COLLISION",
                            f"Heading anchor '#{slug}' in '{ref_file.name}' (line {line_no}: '{raw_title}') collides with parent SKILL.md heading '{parent_title}' (line {parent_line})",
                            line=line_no,
                        )
                    )
                elif slug in seen_ref_slugs:
                    prev_file, prev_line, prev_title = seen_ref_slugs[slug]
                    if prev_file != ref_file.name:
                        errors.append(
                            ValidationError(
                                ref_file,
                                "ANCHOR_COLLISION",
                                f"Heading anchor '#{slug}' in '{ref_file.name}' (line {line_no}: '{raw_title}') collides with reference document '{prev_file}' heading '{prev_title}' (line {prev_line})",
                                line=line_no,
                            )
                        )
                else:
                    seen_ref_slugs[slug] = (ref_file.name, line_no, raw_title)

        return errors

    @staticmethod
    def _extract_markdown_headings(file_path: Path) -> list[tuple[int, str, str]]:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        headings: list[tuple[int, str, str]] = []
        in_fence = False
        for line_no, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            m = re.match(r"^#{1,6}\s+(.*)", stripped)
            if m:
                raw_title = m.group(1).strip()
                custom_id_match = re.search(r"\{#([^}]+)\}\s*$", raw_title)
                if custom_id_match:
                    slug = custom_id_match.group(1).strip()
                else:
                    slug = FrontmatterValidator._slugify_heading(raw_title)
                if slug:
                    headings.append((line_no, raw_title, slug))
        return headings

    @staticmethod
    def _slugify_heading(text: str) -> str:
        cleaned = re.sub(r"`([^`]+)`", r"\1", text)
        cleaned = re.sub(r"[*_~]", "", cleaned)
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = cleaned.lower().strip()
        cleaned = re.sub(r"[^\w\s-]", "", cleaned, flags=re.UNICODE)
        slug = re.sub(r"[\s_]+", "-", cleaned).strip("-")
        return slug

    def validate_agent(self, agent_path: Path) -> list[ValidationError]:
        errors: list[ValidationError] = []
        try:
            content = agent_path.read_text(encoding="utf-8")
        except Exception as exc:
            return [ValidationError(agent_path, "FILE_READ_ERROR", f"Could not read agent file: {exc}")]

        try:
            data = parse_frontmatter(content)
        except FrontmatterParseError as fpe:
            return [ValidationError(agent_path, "FRONTMATTER_MALFORMED", str(fpe))]

        # Validate name
        name = data.get("name")
        expected_name = agent_path.stem
        if not name or not isinstance(name, str):
            errors.append(ValidationError(agent_path, "MISSING_FIELD", "Missing or empty required field 'name'"))
        elif name != expected_name:
            errors.append(
                ValidationError(
                    agent_path,
                    "STRICT_NAMING_INVARIANT",
                    f"Strict Naming Invariant violation: declared name '{name}' does not match filename slug '{expected_name}'",
                )
            )

        # Validate description
        description = data.get("description")
        if not description or not isinstance(description, str) or not description.strip():
            errors.append(ValidationError(agent_path, "MISSING_FIELD", "Missing or empty required field 'description'"))

        # Validate tools
        tools = data.get("tools")
        if not tools or not isinstance(tools, list) or len(tools) == 0:
            errors.append(ValidationError(agent_path, "MISSING_FIELD", "Missing or empty required field 'tools' (must be a non-empty list)"))

        return errors

    def validate_all(self, root_dir: Path) -> tuple[int, int, list[ValidationError]]:
        all_errors: list[ValidationError] = []

        # Discover all skills/*/SKILL.md
        skills_dir = root_dir / "skills"
        skill_files: list[Path] = []
        if skills_dir.exists():
            for p in sorted(skills_dir.glob("*/SKILL.md")):
                skill_files.append(p)

        for skill_file in skill_files:
            all_errors.extend(self.validate_skill(skill_file))

        # Discover all agents/*.md
        agents_dir = root_dir / "agents"
        agent_files: list[Path] = []
        if agents_dir.exists():
            for p in sorted(agents_dir.glob("*.md")):
                agent_files.append(p)

        for agent_file in agent_files:
            all_errors.extend(self.validate_agent(agent_file))

        return len(skill_files), len(agent_files), all_errors


PERMITTED_MANIFEST_KEYS: set[str] = {
    "name",
    "displayName",
    "description",
    "version",
    "skills",
    "agents",
    "suggestedPrompts",
    "disabled",
    "logo",
    "icon",
    "hooks",
}


class ManifestValidator:
    """Validates plugin.json manifest conformance and hooks.json lifecycle definitions."""

    MAX_EXPANDED_RULE_BYTES: int = 20_000

    CORE_TEMPLATE_ASSETS: tuple[str, ...] = (
        "report-template.html",
        "report-tokens.css",
        "prototype-switcher.html",
        "CONSTRAINTS.md",
        "definition-of-done.md",
    )

    DEPRECATED_TEMPLATE_SUBDIRS: tuple[str, ...] = (
        "codemod",
        "scenario",
    )

    TRICAMERAL_RULES: tuple[str, ...] = (
        "gemini-behavior-guardrails.md",
        "operational-invariants.md",
        "code-quality-invariants.md",
    )

    POLYGLOT_RULE: str = "polyglot-standards.md"
    POLYGLOT_INCLUDE_LABEL: str = "polyglot-standards"

    LEGACY_MONOLITHIC_RULES: tuple[str, ...] = (
        "subagent-orchestration.md",
        "prompt-engineering-discipline.md",
        "typescript-standards.md",
        "architectural-principles.md",
        "tdd-and-verification.md",
    )

    CANONICAL_RUNTIME_ASSETS: tuple[str, ...] = (
        "plugin.json",
        "hooks.json",
        "icon.png",
        "README.md",
        "skills",
        "agents",
        "rules",
        "scripts",
        "hooks",
        "templates",
        "schemas",
    )

    INSTALLER_CHANNELS: tuple[str, ...] = (
        "install.sh",
        "scripts/install.sh",
        "scripts/enable-in-project.sh",
        "scripts/package-bundle.sh",
    )

    def __init__(self) -> None:
        self._preceding_ticket_cache: dict[str, dict[str, Any]] = {}

    def validate_skill(self, skill_path: Path, max_lines: int | None = None) -> list[ValidationError]:
        """Validates a single skill runbook (frontmatter, naming, sections, line ceiling, reference navigation)."""
        return FrontmatterValidator().validate_skill(skill_path, max_lines=max_lines)

    def validate_skill_reference_navigation(self, skill_path: Path) -> list[ValidationError]:
        """
        [EDGE-PROG-DISC-02] Validates bidirectional reference navigation and collision-free anchor integrity
        between primary skill runbooks and their extracted reference documents.
        """
        return FrontmatterValidator().validate_skill_reference_navigation(skill_path)

    def validate_manifest(self, manifest_path: Path, skills_dir: Path) -> list[ValidationError]:
        errors: list[ValidationError] = []
        try:
            manifest_stat = manifest_path.stat()
            if not stat.S_ISREG(manifest_stat.st_mode):
                return [ValidationError(manifest_path, "MANIFEST_NOT_FOUND", "Missing required manifest 'plugin.json' at workspace root")]
        except FileNotFoundError:
            return [ValidationError(manifest_path, "MANIFEST_NOT_FOUND", "Missing required manifest 'plugin.json' at workspace root")]
        except OSError as exc:
            return [ValidationError(manifest_path, "FILE_READ_ERROR", f"Could not inspect manifest file '{manifest_path.name}': {exc}")]

        try:
            content = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            return [ValidationError(manifest_path, "FILE_READ_ERROR", f"Could not read manifest file: {exc}")]
        except UnicodeDecodeError as exc:
            return [ValidationError(manifest_path, "MANIFEST_MALFORMED", f"Invalid UTF-8 in manifest file: {exc}")]
        except Exception as exc:
            return [ValidationError(manifest_path, "FILE_READ_ERROR", f"Could not read manifest file: {exc}")]

        try:
            data = json.loads(content)
        except Exception as exc:
            return [ValidationError(manifest_path, "MANIFEST_MALFORMED", f"Invalid JSON in plugin.json: {exc}")]

        if not isinstance(data, dict):
            return [ValidationError(manifest_path, "MANIFEST_MALFORMED", "Root of plugin.json must be a JSON object")]

        # Validate top-level keys
        for key in data.keys():
            if key not in PERMITTED_MANIFEST_KEYS:
                errors.append(
                    ValidationError(
                        manifest_path,
                        "ILLEGAL_MANIFEST_FIELD",
                        f"Unrecognized top-level field '{key}' in plugin.json (permitted: {sorted(PERMITTED_MANIFEST_KEYS)})",
                    )
                )

        # Validate name
        name = data.get("name")
        if not name or not isinstance(name, str):
            errors.append(ValidationError(manifest_path, "MISSING_FIELD", "Missing or empty required field 'name' in plugin.json"))
        elif name != "agy-matt":
            errors.append(
                ValidationError(
                    manifest_path,
                    "MANIFEST_NAME_MISMATCH",
                    f"Declared plugin name '{name}' does not match expected name 'agy-matt'",
                )
            )

        # Validate skills manifest array
        skills = data.get("skills")
        if skills is not None:
            if not isinstance(skills, list):
                errors.append(ValidationError(manifest_path, "MALFORMED_FIELD", "Field 'skills' must be an array of strings"))
            else:
                declared_skills = set()
                for idx, item in enumerate(skills):
                    if not isinstance(item, str):
                        errors.append(ValidationError(manifest_path, "MALFORMED_FIELD", f"Entry {idx} in 'skills' must be a string"))
                        continue
                    clean_item = item.strip()
                    if clean_item.startswith("./"):
                        clean_item = clean_item[2:]
                    skill_target = manifest_path.parent / clean_item / "SKILL.md"
                    if not skill_target.exists():
                        errors.append(
                            ValidationError(
                                manifest_path,
                                "ORPHANED_MANIFEST_SKILL",
                                f"Skills manifest entry '{item}' does not exist (expected at {clean_item}/SKILL.md)",
                            )
                        )
                    if clean_item.startswith("skills/"):
                        declared_skills.add(clean_item[len("skills/"):])
                    else:
                        declared_skills.add(Path(clean_item).name)

                # Completeness check: all skills on disk must be declared
                if skills_dir.exists():
                    for skill_folder in sorted(skills_dir.iterdir()):
                        if skill_folder.is_dir() and (skill_folder / "SKILL.md").exists():
                            if skill_folder.name not in declared_skills:
                                errors.append(
                                    ValidationError(
                                        manifest_path,
                                        "MANIFEST_MISSING_SKILL",
                                        f"Skill '{skill_folder.name}' exists on disk but is not declared in plugin.json 'skills' array",
                                    )
                                )

        # Validate agents manifest array
        agents = data.get("agents")
        if agents is not None:
            if not isinstance(agents, list):
                errors.append(ValidationError(manifest_path, "MALFORMED_FIELD", "Field 'agents' must be an array of strings"))
            else:
                declared_agents = set()
                for idx, item in enumerate(agents):
                    if not isinstance(item, str):
                        errors.append(ValidationError(manifest_path, "MALFORMED_FIELD", f"Entry {idx} in 'agents' must be a string"))
                        continue
                    clean_item = item.strip()
                    if clean_item.startswith("./"):
                        clean_item = clean_item[2:]
                    agent_target = manifest_path.parent / clean_item
                    if not agent_target.is_file():
                        if not clean_item.endswith(".md"):
                            agent_target = manifest_path.parent / f"{clean_item}.md"
                    if not agent_target.is_file():
                        errors.append(
                            ValidationError(
                                manifest_path,
                                "ORPHANED_MANIFEST_AGENT",
                                f"Agents manifest entry '{item}' does not exist (expected at {clean_item} or {clean_item}.md)",
                            )
                        )
                    clean_agent = clean_item
                    if clean_agent.startswith("agents/"):
                        clean_agent = clean_agent[len("agents/"):]
                    if clean_agent.endswith(".md"):
                        clean_agent = clean_agent[:-3]
                    declared_agents.add(clean_agent)

                # Completeness check: all agents on disk must be declared
                agents_dir = manifest_path.parent / "agents"
                if agents_dir.exists():
                    for agent_file in sorted(agents_dir.glob("*.md")):
                        if agent_file.is_file():
                            if agent_file.stem not in declared_agents:
                                errors.append(
                                    ValidationError(
                                        manifest_path,
                                        "MANIFEST_MISSING_AGENT",
                                        f"Agent '{agent_file.name}' exists on disk but is not declared in plugin.json 'agents' array",
                                    )
                                )

        # Validate suggestedPrompts cross-references
        suggested_prompts = data.get("suggestedPrompts")
        if suggested_prompts is not None:
            if not isinstance(suggested_prompts, list):
                errors.append(ValidationError(manifest_path, "MALFORMED_FIELD", "Field 'suggestedPrompts' must be an array of strings"))
            else:
                for idx, item in enumerate(suggested_prompts):
                    if not isinstance(item, str):
                        errors.append(ValidationError(manifest_path, "MALFORMED_FIELD", f"Entry {idx} in 'suggestedPrompts' must be a string"))
                        continue
                    commands = re.findall(r"\(/([a-zA-Z0-9_-]+)\)", item)
                    for cmd in commands:
                        target_skill = skills_dir / cmd / "SKILL.md"
                        if not target_skill.exists():
                            errors.append(
                                ValidationError(
                                    manifest_path,
                                    "ORPHANED_SUGGESTED_PROMPT",
                                    f"Suggested prompt references non-existent skill '/{cmd}' (expected at skills/{cmd}/SKILL.md)",
                                )
                            )

        # Validate Dual-Compatibility Manifest requirements ([MANIFEST-01], [ERR-MANIFEST-01])
        dual_manifest_targets: dict[str, Path] = {}
        for req_field in ("logo", "icon", "hooks"):
            if req_field not in data or data[req_field] is None:
                errors.append(
                    ValidationError(
                        manifest_path,
                        "MISSING_FIELD",
                        f"Missing or empty required field '{req_field}' in plugin.json",
                    )
                )
                continue

            field_val = data[req_field]
            if not isinstance(field_val, str) or not field_val.strip():
                errors.append(
                    ValidationError(
                        manifest_path,
                        "MALFORMED_FIELD",
                        f"Field '{req_field}' must be a non-empty string in plugin.json",
                    )
                )
                continue

            clean_val = field_val.strip()
            normalized_val = clean_val.replace("\\", "/")
            path_parts = Path(normalized_val).parts
            is_traversal = ".." in path_parts or ".." in normalized_val.split("/")
            is_absolute = (
                Path(clean_val).is_absolute()
                or clean_val.startswith("/")
                or clean_val.startswith("\\")
                or os.path.isabs(clean_val)
                or bool(re.match(r"^[a-zA-Z]:", clean_val))
            )

            if is_traversal or is_absolute:
                errors.append(
                    ValidationError(
                        manifest_path,
                        "PATH_CONFINEMENT_VIOLATION",
                        f"Field '{req_field}' must be a relative workspace path without parent traversal (got '{field_val}')",
                    )
                )
                continue

            rel_token = normalized_val
            while rel_token.startswith("./"):
                rel_token = rel_token[2:]
            target_path = manifest_path.parent / rel_token

            try:
                target_stat = target_path.stat()
                file_is_regular = stat.S_ISREG(target_stat.st_mode)
                file_exists = True
            except FileNotFoundError:
                file_exists = False
                file_is_regular = False
            except OSError as exc:
                errors.append(
                    ValidationError(
                        manifest_path,
                        "FILE_READ_ERROR",
                        f"Could not inspect target file for '{req_field}' at {rel_token}: {exc}",
                    )
                )
                continue

            if not file_exists or not file_is_regular:
                errors.append(
                    ValidationError(
                        manifest_path,
                        "FILE_NOT_FOUND",
                        f"Target file for '{req_field}' does not exist (expected at {rel_token})",
                    )
                )
                continue

            try:
                resolved_target = target_path.resolve()
                resolved_root = manifest_path.parent.resolve()
                resolved_target.relative_to(resolved_root)
            except (ValueError, RuntimeError):
                errors.append(
                    ValidationError(
                        manifest_path,
                        "PATH_CONFINEMENT_VIOLATION",
                        f"Field '{req_field}' resolves outside plugin root (got '{field_val}')",
                    )
                )
                continue
            except OSError as exc:
                errors.append(
                    ValidationError(
                        manifest_path,
                        "FILE_READ_ERROR",
                        f"Could not resolve target path for '{req_field}' at {rel_token}: {exc}",
                    )
                )
                continue

            dual_manifest_targets[req_field] = target_path

        if "logo" in dual_manifest_targets and "icon" in dual_manifest_targets:
            logo_path = dual_manifest_targets["logo"]
            icon_path = dual_manifest_targets["icon"]
            if logo_path != icon_path:
                try:
                    if logo_path.resolve() != icon_path.resolve():
                        errors.append(
                            ValidationError(
                                manifest_path,
                                "MANIFEST_PARITY_VIOLATION",
                                f"Manifest fields 'logo' ('{data.get('logo')}') and 'icon' ('{data.get('icon')}') must resolve to the identical file within plugin root",
                            )
                        )
                except (OSError, RuntimeError, ValueError):
                    errors.append(
                        ValidationError(
                            manifest_path,
                            "MANIFEST_PARITY_VIOLATION",
                            f"Manifest fields 'logo' ('{data.get('logo')}') and 'icon' ('{data.get('icon')}') must resolve to the identical file within plugin root",
                        )
                    )

        image_fields = [f for f in ("logo", "icon") if f in dual_manifest_targets]
        validated_image_paths: set[Path] = set()
        for img_field in image_fields:
            img_path = dual_manifest_targets[img_field]
            try:
                resolved_img = img_path.resolve()
            except (OSError, RuntimeError, ValueError):
                resolved_img = img_path

            if resolved_img in validated_image_paths:
                continue
            validated_image_paths.add(resolved_img)

            icon_bytes: bytes | None = None
            try:
                icon_bytes = img_path.read_bytes()
            except OSError as exc:
                errors.append(
                    ValidationError(
                        manifest_path,
                        "FILE_READ_ERROR",
                        f"Could not read icon file '{img_path.name}': {exc}",
                    )
                )

            if icon_bytes is not None:
                if len(icon_bytes) >= 15360:
                    errors.append(
                        ValidationError(
                            manifest_path,
                            "INVALID_ICON_SIZE",
                            f"Icon file size {len(icon_bytes)} bytes exceeds maximum permitted 15360 bytes (15 KB)",
                        )
                    )

                if len(icon_bytes) < 26:
                    errors.append(
                        ValidationError(
                            manifest_path,
                            "INVALID_ICON_FORMAT",
                            f"Icon file truncated ({len(icon_bytes)} bytes); minimum 26 header bytes required",
                        )
                    )
                elif not icon_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    errors.append(
                        ValidationError(
                            manifest_path,
                            "INVALID_ICON_FORMAT",
                            "Icon file does not declare valid 8-byte PNG signature",
                        )
                    )
                else:
                    first_chunk_type = icon_bytes[12:16]
                    if first_chunk_type != b"IHDR":
                        chunk_repr = first_chunk_type.decode("latin1", errors="replace")
                        errors.append(
                            ValidationError(
                                manifest_path,
                                "INVALID_ICON_FORMAT",
                                f"First PNG chunk must be 'IHDR', got '{chunk_repr}'",
                            )
                        )
                    else:
                        try:
                            width, height, bit_depth, color_type = struct.unpack(">IIBB", icon_bytes[16:26])
                        except (struct.error, IndexError) as exc:
                            errors.append(
                                ValidationError(
                                    manifest_path,
                                    "INVALID_ICON_FORMAT",
                                    f"Failed to unpack PNG IHDR header: {exc}",
                                )
                            )
                        else:
                            if width != 256 or height != 256:
                                errors.append(
                                    ValidationError(
                                        manifest_path,
                                        "INVALID_ICON_GEOMETRY",
                                        f"Icon dimensions must be 256x256, got {width}x{height}",
                                    )
                                )
                            if bit_depth != 8:
                                errors.append(
                                    ValidationError(
                                        manifest_path,
                                        "INVALID_ICON_BIT_DEPTH",
                                        f"Icon bit depth must be 8, got {bit_depth}",
                                    )
                                )
                            if color_type != 6:
                                errors.append(
                                    ValidationError(
                                        manifest_path,
                                        "INVALID_ICON_COLOR_TYPE",
                                        f"Icon color type must be 6 (RGBA), got {color_type}",
                                    )
                                )

        if "hooks" in dual_manifest_targets:
            hooks_file_target = dual_manifest_targets["hooks"]
            hooks_content: str | None = None
            try:
                hooks_content = hooks_file_target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                errors.append(
                    ValidationError(
                        manifest_path,
                        "HOOKS_MALFORMED",
                        f"Invalid UTF-8 encoding in hooks file '{hooks_file_target.name}': {exc}",
                    )
                )
            except OSError as exc:
                errors.append(
                    ValidationError(
                        manifest_path,
                        "FILE_READ_ERROR",
                        f"Could not read hooks file '{hooks_file_target.name}': {exc}",
                    )
                )

            if hooks_content is not None:
                try:
                    hooks_json_data = json.loads(hooks_content)
                except Exception as exc:
                    errors.append(
                        ValidationError(
                            manifest_path,
                            "HOOKS_MALFORMED",
                            f"Invalid JSON in hooks file '{hooks_file_target.name}': {exc}",
                        )
                    )
                else:
                    if not isinstance(hooks_json_data, dict):
                        errors.append(
                            ValidationError(
                                manifest_path,
                                "HOOKS_MALFORMED",
                                f"Root of hooks file '{hooks_file_target.name}' must be a JSON object",
                            )
                        )

        return errors

    def validate_hooks(self, hooks_path: Path) -> list[ValidationError]:
        errors: list[ValidationError] = []
        if not hooks_path.exists():
            return [ValidationError(hooks_path, "HOOKS_NOT_FOUND", "hooks.json file not found")]

        try:
            content = hooks_path.read_text(encoding="utf-8")
        except Exception as exc:
            return [ValidationError(hooks_path, "FILE_READ_ERROR", f"Could not read hooks file: {exc}")]

        try:
            data = json.loads(content)
        except Exception as exc:
            return [ValidationError(hooks_path, "HOOKS_MALFORMED", f"Invalid JSON in hooks.json: {exc}")]

        if not isinstance(data, dict):
            return [ValidationError(hooks_path, "HOOKS_MALFORMED", "Root of hooks.json must be a JSON object")]

        def check_hook_item(item: Any, scope: str):
            if not isinstance(item, dict):
                errors.append(ValidationError(hooks_path, "HOOKS_MALFORMED_COMMAND", f"Hook entry in '{scope}' must be an object"))
                return
            command = item.get("command")
            if not command or not isinstance(command, str) or not command.strip():
                errors.append(
                    ValidationError(
                        hooks_path,
                        "HOOKS_MALFORMED_COMMAND",
                        f"Hook declaration in '{scope}' has missing, empty, or non-string 'command'",
                    )
                )

            # Check hook type if specified
            hook_type = item.get("type")
            if hook_type is not None and hook_type != "command":
                errors.append(
                    ValidationError(
                        hooks_path,
                        "HOOKS_MALFORMED_TYPE",
                        f"Hook entry in '{scope}' has unsupported type '{hook_type}' (permitted: 'command')",
                    )
                )

            # Check timeout if specified
            timeout = item.get("timeout")
            if timeout is not None:
                if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                    errors.append(
                        ValidationError(
                            hooks_path,
                            "HOOKS_MALFORMED_TIMEOUT",
                            f"Hook entry in '{scope}' has invalid timeout '{timeout}' (must be a positive integer)",
                        )
                    )

            # Check referenced local scripts if command points to a local script (e.g. ./scripts/...)
            if isinstance(command, str):
                cmd_stripped = command.strip()
                if cmd_stripped.startswith("./"):
                    script_token = cmd_stripped.split()[0]
                    script_rel = script_token[2:]
                    target_script = hooks_path.parent / script_rel
                    # Fallback for templates placed in subdirectories (e.g. templates/hooks/ -> root_dir)
                    if not target_script.exists():
                        alt_target = hooks_path.resolve().parents[1] / script_rel if len(hooks_path.resolve().parents) > 1 else None
                        if alt_target and alt_target.exists():
                            target_script = alt_target
                        else:
                            alt_root = hooks_path.resolve().parents[2] / script_rel if len(hooks_path.resolve().parents) > 2 else None
                            if alt_root and alt_root.exists():
                                target_script = alt_root
                    if not target_script.exists():
                        errors.append(
                            ValidationError(
                                hooks_path,
                                "HOOKS_MISSING_SCRIPT",
                                f"Hook script '{script_token}' in '{scope}' does not exist relative to hooks root",
                            )
                        )
                    elif not os.access(target_script, os.X_OK):
                        errors.append(
                            ValidationError(
                                hooks_path,
                                "HOOKS_SCRIPT_NOT_EXECUTABLE",
                                f"Hook script '{script_token}' in '{scope}' is not executable (+x)",
                            )
                        )

        def check_event_item(item: Any, scope: str):
            if not isinstance(item, dict):
                errors.append(ValidationError(hooks_path, "HOOKS_MALFORMED_COMMAND", f"Hook entry in '{scope}' must be an object"))
                return
            if "matcher" in item or "hooks" in item:
                matcher = item.get("matcher")
                if matcher is None or not isinstance(matcher, str):
                    errors.append(
                        ValidationError(
                            hooks_path,
                            "HOOKS_MALFORMED_MATCHER",
                            f"Grouped hook entry in '{scope}' has missing, empty, or non-string 'matcher'",
                        )
                    )
                grouped_hooks = item.get("hooks")
                if not isinstance(grouped_hooks, list) or len(grouped_hooks) == 0:
                    errors.append(
                        ValidationError(
                            hooks_path,
                            "HOOKS_MALFORMED_GROUP",
                            f"Grouped hook entry in '{scope}' must contain a non-empty 'hooks' list",
                        )
                    )
                else:
                    for idx, sub_hook in enumerate(grouped_hooks):
                        check_hook_item(sub_hook, f"{scope}[matcher='{matcher}'].hooks[{idx}]")
            else:
                check_hook_item(item, scope)

        for bundle_key, bundle_val in data.items():
            if isinstance(bundle_val, dict):
                for event_key, event_val in bundle_val.items():
                    if event_key == "enabled":
                        continue
                    if isinstance(event_val, list):
                        for hook in event_val:
                            check_event_item(hook, f"{bundle_key}.{event_key}")
                    elif isinstance(event_val, dict):
                        check_event_item(event_val, f"{bundle_key}.{event_key}")
            elif isinstance(bundle_val, list):
                for hook in bundle_val:
                    check_event_item(hook, bundle_key)

        return errors

    def validate_gate_config(self, config_path: Path) -> list[ValidationError]:
        """Validates declarative quality gate configuration (.agents/matt-gate.json or templates/matt-gate.json)."""
        errors: list[ValidationError] = []
        if not config_path.exists():
            return [ValidationError(config_path, "CONFIG_NOT_FOUND", f"Configuration file not found: {config_path.name}")]

        try:
            content = config_path.read_text(encoding="utf-8")
        except Exception as exc:
            return [ValidationError(config_path, "FILE_READ_ERROR", f"Could not read config file: {exc}")]

        try:
            data = json.loads(content)
        except Exception as exc:
            return [ValidationError(config_path, "CONFIG_MALFORMED", f"Invalid JSON in {config_path.name}: {exc}")]

        if not isinstance(data, dict):
            return [ValidationError(config_path, "CONFIG_MALFORMED", f"Root of {config_path.name} must be a JSON object")]

        permitted_keys = {"$schema", "title", "description", "enabled", "command", "timeout"}
        for k in data.keys():
            if k not in permitted_keys:
                errors.append(
                    ValidationError(
                        config_path,
                        "CONFIG_ILLEGAL_FIELD",
                        f"Unrecognized field '{k}' in {config_path.name} (permitted: {sorted(permitted_keys)})",
                    )
                )

        if "enabled" in data and not isinstance(data["enabled"], bool):
            errors.append(
                ValidationError(
                    config_path,
                    "CONFIG_MALFORMED_FIELD",
                    f"Field 'enabled' in {config_path.name} must be a boolean",
                )
            )

        if "command" in data:
            if not isinstance(data["command"], str) or not data["command"].strip():
                errors.append(
                    ValidationError(
                        config_path,
                        "CONFIG_MALFORMED_FIELD",
                        f"Field 'command' in {config_path.name} must be a non-empty string",
                    )
                )

        if "timeout" in data:
            timeout = data["timeout"]
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                errors.append(
                    ValidationError(
                        config_path,
                        "CONFIG_MALFORMED_FIELD",
                        f"Field 'timeout' in {config_path.name} must be a positive integer",
                    )
                )

        return errors

    REQUIRED_TICKET_TEMPLATES: tuple[str, ...] = (
        "ticket-template-local.md",
        "ticket-template-github.md",
    )
    REQUIRED_TICKET_METADATA_KEYS: tuple[str, ...] = (
        "**Context:**",
        "**Seam:**",
        "**Verification:**",
        "**Guardrails:**",
    )

    def validate_ticket_templates(self, resources_dir: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates ticket templates (ticket-template-local.md and ticket-template-github.md).
        Ensures each file exists, is non-empty, contains first-class execution contract
        metadata keys (**Context:**, **Seam:**, **Verification:**, **Guardrails:**),
        and confirms legacy '## Ready-to-Run Starter Prompt' sections and embedded '/implement'
        code blocks are strictly absent.
        """
        errors: list[ValidationError] = []
        if not resources_dir.exists():
            return 0, [ValidationError(resources_dir, "TEMPLATE_NOT_FOUND", f"Directory does not exist: {resources_dir}")]

        scanned = 0
        for template_name in self.REQUIRED_TICKET_TEMPLATES:
            target_file = resources_dir / template_name
            if not target_file.is_file():
                errors.append(
                    ValidationError(
                        target_file,
                        "TEMPLATE_NOT_FOUND",
                        f"Required ticket template '{template_name}' not found in {resources_dir.name}/",
                    )
                )
                continue

            scanned += 1
            try:
                content = target_file.read_text(encoding="utf-8")
                if not content.strip():
                    errors.append(
                        ValidationError(
                            target_file,
                            "TEMPLATE_EMPTY",
                            f"Ticket template '{template_name}' is empty",
                        )
                    )
                    continue

                for key in self.REQUIRED_TICKET_METADATA_KEYS:
                    clean_key = key.replace("*", "").rstrip(":")
                    key_pattern = re.compile(rf"^\s*\*\*{re.escape(clean_key)}:\*\*", re.MULTILINE)
                    if not key_pattern.search(content):
                        unbolded_pattern = re.compile(rf"^\s*{re.escape(clean_key)}:", re.MULTILINE)
                        misplaced_colon_pattern = re.compile(rf"^\s*\*\*{re.escape(clean_key)}\*\*:", re.MULTILINE)
                        if unbolded_pattern.search(content):
                            errors.append(
                                ValidationError(
                                    target_file,
                                    "TEMPLATE_MISSING_METADATA",
                                    f"Ticket template '{template_name}' has unbolded metadata key '{clean_key}:' (must be formatted as '**{clean_key}:**')",
                                )
                            )
                        elif misplaced_colon_pattern.search(content):
                            errors.append(
                                ValidationError(
                                    target_file,
                                    "TEMPLATE_MISSING_METADATA",
                                    f"Ticket template '{template_name}' has misplaced colon in '**{clean_key}**:' (must be formatted as '**{clean_key}:**')",
                                )
                            )
                        else:
                            errors.append(
                                ValidationError(
                                    target_file,
                                    "TEMPLATE_MISSING_METADATA",
                                    f"Ticket template '{template_name}' is missing required metadata key '{key}'",
                                )
                            )

                if re.search(r"^#+\s*Ready-to-Run Starter Prompt", content, re.MULTILINE | re.IGNORECASE):
                    errors.append(
                        ValidationError(
                            target_file,
                            "TEMPLATE_LEGACY_STARTER_PROMPT",
                            f"Ticket template '{template_name}' contains deprecated '## Ready-to-Run Starter Prompt' section",
                        )
                    )

                code_blocks = re.findall(r"(?:`{3,}|~{3,})[a-zA-Z0-9_-]*\s*\n(.*?)[\s\n]*(?:`{3,}|~{3,})", content, re.DOTALL)
                if any("/implement" in block for block in code_blocks):
                    errors.append(
                        ValidationError(
                            target_file,
                            "TEMPLATE_LEGACY_STARTER_PROMPT",
                            f"Ticket template '{template_name}' contains deprecated fenced code block with '/implement'",
                        )
                    )
            except Exception as exc:
                errors.append(ValidationError(target_file, "FILE_READ_ERROR", f"Could not read {template_name}: {exc}"))

        return scanned, errors

    def read_rule_content(self, rule_path: Path) -> str:
        """
        Reads a rule file and recursively expands Antigravity include directives (@[label](target))
        if present, enabling invariant validators to evaluate modular trigger rules seamlessly.
        """
        if not rule_path.is_file():
            return ""
        try:
            content = rule_path.read_text(encoding="utf-8")
        except Exception:
            return ""

        rules_dir = rule_path.parent if rule_path.parent.name == "rules" else rule_path.parent

        def _get_inactive_spans(text: str) -> list[tuple[int, int]]:
            spans: list[tuple[int, int]] = []
            for m in re.finditer(r"<!--.*?-->", text, flags=re.DOTALL):
                spans.append(m.span())
            for m in re.finditer(r"(?:`{3,}|~{3,}).*?(?:`{3,}|~{3,})", text, flags=re.DOTALL):
                spans.append(m.span())
            return spans

        def _is_inside_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
            for s_start, s_end in spans:
                if not (end <= s_start or start >= s_end):
                    return True
            return False

        def _resolve_target(current_file: Path, raw_target: str) -> Path | None:
            clean = raw_target.strip().strip("<> ")
            if "#" in clean:
                clean = clean.split("#", 1)[0]
            if "?" in clean:
                clean = clean.split("?", 1)[0]
            clean = unquote(clean).strip().replace("\\", "/").lstrip("/")
            if clean.startswith("./"):
                clean = clean[2:]
            clean = clean.lstrip("/")
            candidates = [
                current_file.parent / clean,
                rules_dir / clean,
                rules_dir.parent / clean,
            ]
            if clean.startswith("rules/"):
                candidates.append(rules_dir / clean[6:])
            elif clean.startswith(f"{rules_dir.name}/"):
                candidates.append(rules_dir / clean[len(rules_dir.name) + 1:])
            for cand in candidates:
                if cand.is_file():
                    return cand
            return None

        def _expand(fpath: Path, visited: set[Path]) -> str:
            resolved = fpath.resolve()
            if resolved in visited:
                return ""
            try:
                txt = fpath.read_text(encoding="utf-8")
            except Exception:
                return ""
            spans = _get_inactive_spans(txt)
            matches: list[re.Match[str]] = []
            for m in re.finditer(r"(?m)^[ \t]*@\[([^\]]*)\]\(([^)]+)\)[ \t]*\r?$", txt):
                if not _is_inside_spans(m.start(), m.end(), spans):
                    matches.append(m)
            new_visited = visited | {resolved}
            res = txt
            for m in reversed(matches):
                target = _resolve_target(fpath, m.group(2).strip())
                repl = _expand(target, new_visited) if target else ""
                res = res[: m.start()] + repl + res[m.end() :]
            return res

        return _expand(rule_path, set())

    def validate_rules(
        self, rules_dir: Path, require_polyglot: bool | None = None
    ) -> tuple[int, list[ValidationError]]:
        """
        Validates tricameral and polyglot rules architecture and Master Composer inclusion:
        - Ensures rules/AGENTS.md exists and is non-empty.
        - Ensures all tricameral rules exist, are non-empty, and are included in rules/AGENTS.md.
        - Ensures rules/polyglot-standards.md exists, is non-empty, is properly included in rules/AGENTS.md
          via @[polyglot-standards](polyglot-standards.md), and complies with progressive disclosure ceiling (<= 150 lines, < 8 KB).
        - Ensures legacy monolithic rule files are NOT referenced in rules/AGENTS.md.
        - Ensures all Antigravity include directives (@[...](path)) in rules/AGENTS.md reference valid, existing files.
        """
        errors: list[ValidationError] = []
        if not rules_dir.exists():
            return 0, [ValidationError(rules_dir, "RULES_NOT_FOUND", f"Directory does not exist: {rules_dir}")]

        master_composer = rules_dir / "AGENTS.md"
        if not master_composer.is_file():
            errors.append(ValidationError(master_composer, "RULE_NOT_FOUND", "Missing Master Composer entrypoint 'rules/AGENTS.md'"))
            return 0, errors

        try:
            composer_content = master_composer.read_text(encoding="utf-8")
        except Exception as exc:
            return 0, [ValidationError(master_composer, "FILE_READ_ERROR", f"Could not read rules/AGENTS.md: {exc}")]

        if not composer_content.strip():
            return 0, [ValidationError(master_composer, "RULE_EMPTY", "Master Composer entrypoint 'rules/AGENTS.md' is empty")]

        for rule_name in self.TRICAMERAL_RULES:
            rule_path = rules_dir / rule_name
            if not rule_path.is_file():
                errors.append(
                    ValidationError(
                        rule_path,
                        "RULE_NOT_FOUND",
                        f"Required tricameral rule 'rules/{rule_name}' does not exist",
                    )
                )
            else:
                try:
                    if not rule_path.read_text(encoding="utf-8").strip():
                        errors.append(
                            ValidationError(
                                rule_path,
                                "RULE_EMPTY",
                                f"Required tricameral rule 'rules/{rule_name}' is empty",
                            )
                        )
                except Exception as exc:
                    errors.append(ValidationError(rule_path, "FILE_READ_ERROR", f"Could not read {rule_name}: {exc}"))

        # Strip HTML comments and fenced code blocks to prevent commented-out directives from being treated as active includes.
        active_content = re.sub(r"<!--.*?-->", "", composer_content, flags=re.DOTALL)
        active_content = re.sub(r"(?:`{3,}|~{3,}).*?(?:`{3,}|~{3,})", "", active_content, flags=re.DOTALL)

        active_included_rules: set[str] = set()
        has_active_polyglot_include = False

        for line in active_content.splitlines():
            line_stripped = line.strip()
            m = re.match(r"^@\[([^\]]*)\]\(([^)]+)\)", line_stripped)
            if not m:
                continue

            label = m.group(1).strip()
            raw_target = m.group(2).strip()

            clean_target = raw_target.lstrip("/")
            if clean_target.startswith("./"):
                clean_target = clean_target[2:]
            clean_target = clean_target.lstrip("/")
            if clean_target.startswith("rules/"):
                clean_target = clean_target[6:]
            elif clean_target.startswith(f"{rules_dir.name}/"):
                clean_target = clean_target[len(rules_dir.name) + 1:]

            target_path = rules_dir / clean_target
            if not target_path.is_file():
                errors.append(
                    ValidationError(
                        master_composer,
                        "RULE_INCLUDE_TARGET_NOT_FOUND",
                        f"Master Composer include target '{raw_target}' (@[{label}]) does not exist in {rules_dir.name}/",
                    )
                )
            else:
                active_included_rules.add(target_path.name)

            if label == self.POLYGLOT_INCLUDE_LABEL and clean_target == self.POLYGLOT_RULE:
                has_active_polyglot_include = True

        for rule_name in self.TRICAMERAL_RULES:
            if rule_name not in active_included_rules:
                errors.append(
                    ValidationError(
                        master_composer,
                        "RULE_UNREFERENCED_IN_MASTER_COMPOSER",
                        f"Tricameral rule 'rules/{rule_name}' is not included in 'rules/AGENTS.md' via '@[...](...{rule_name})'",
                    )
                )

        # Catch any include reference to legacy monolithic rules across directives or paths
        for legacy_rule in self.LEGACY_MONOLITHIC_RULES:
            pattern = rf"@\[.*?\]\([^)]*{re.escape(legacy_rule)}[^)]*\)"
            if re.search(pattern, composer_content):
                errors.append(
                    ValidationError(
                        master_composer,
                        "RULE_LEGACY_INCLUDED",
                        f"Legacy monolithic rule 'rules/{legacy_rule}' must not be referenced in 'rules/AGENTS.md'",
                    )
                )

        # Polyglot standards rule validation ([INTEG-POLY-02], [ERR-INTEG-POLY-02], [ERR-INTEG-POLY-05])
        if require_polyglot is None:
            try:
                require_polyglot = (rules_dir.parent / "plugin.json").is_file() or rules_dir.resolve() == (REPO_ROOT / "rules").resolve()
            except Exception:
                require_polyglot = (rules_dir.parent / "plugin.json").is_file()

        polyglot_rule = rules_dir / self.POLYGLOT_RULE
        polyglot_exists = polyglot_rule.is_file()

        if require_polyglot and not polyglot_exists:
            errors.append(
                ValidationError(
                    polyglot_rule,
                    "RULE_NOT_FOUND",
                    f"Required polyglot rule 'rules/{self.POLYGLOT_RULE}' does not exist",
                )
            )

        if polyglot_exists:
            try:
                poly_content = polyglot_rule.read_text(encoding="utf-8")
                if not poly_content.strip():
                    errors.append(
                        ValidationError(
                            polyglot_rule,
                            "RULE_EMPTY",
                            f"Required polyglot rule 'rules/{self.POLYGLOT_RULE}' is empty",
                        )
                    )
                else:
                    poly_lines = len(poly_content.splitlines())
                    poly_bytes = len(poly_content.encode("utf-8"))
                    if poly_lines > 150 or poly_bytes >= 8192:
                        errors.append(
                            ValidationError(
                                polyglot_rule,
                                "RULE_SIZE_EXCEEDED",
                                f"Rule 'rules/{self.POLYGLOT_RULE}' exceeds progressive disclosure ceiling "
                                f"({poly_lines} lines > 150, or {poly_bytes} bytes >= 8192 bytes)",
                            )
                        )
            except Exception as exc:
                errors.append(ValidationError(polyglot_rule, "FILE_READ_ERROR", f"Could not read {self.POLYGLOT_RULE}: {exc}"))

        # Master Composer active include validation for polyglot standards ([INTEG-POLY-02], [ERR-INTEG-POLY-02])
        if (polyglot_exists or require_polyglot) and not has_active_polyglot_include:
            errors.append(
                ValidationError(
                    master_composer,
                    "RULE_NOT_INCLUDED",
                    f"Polyglot standards rule 'rules/{self.POLYGLOT_RULE}' is not included in 'rules/AGENTS.md' via '@[{self.POLYGLOT_INCLUDE_LABEL}]({self.POLYGLOT_RULE})'",
                )
            )

        # Validate referential integrity for Antigravity include directives across all rule files
        for rule_file in sorted(rules_dir.glob("**/*.md")):
            if rule_file == master_composer:
                continue
            try:
                r_content = rule_file.read_text(encoding="utf-8")
            except Exception as exc:
                errors.append(ValidationError(rule_file, "FILE_READ_ERROR", f"Could not read {rule_file.name}: {exc}"))
                continue

            r_active = re.sub(r"<!--.*?-->", "", r_content, flags=re.DOTALL)
            r_active = re.sub(r"(?:`{3,}|~{3,}).*?(?:`{3,}|~{3,})", "", r_active, flags=re.DOTALL)

            for line in r_active.splitlines():
                line_stripped = line.strip()
                m = re.match(r"^@\[([^\]]*)\]\(([^)]+)\)", line_stripped)
                if not m:
                    continue

                label = m.group(1).strip()
                raw_target = m.group(2).strip()

                clean_target = raw_target.lstrip("/")
                if clean_target.startswith("./"):
                    clean_target = clean_target[2:]
                clean_target = clean_target.lstrip("/")
                if clean_target.startswith("rules/"):
                    clean_target = clean_target[6:]
                elif clean_target.startswith(f"{rules_dir.name}/"):
                    clean_target = clean_target[len(rules_dir.name) + 1:]

                candidates = [
                    rule_file.parent / clean_target,
                    rules_dir / clean_target,
                ]
                target_path = None
                for cand in candidates:
                    if cand.is_file():
                        target_path = cand
                        break

                if not target_path:
                    errors.append(
                        ValidationError(
                            rule_file,
                            "RULE_INCLUDE_TARGET_NOT_FOUND",
                            f"Rule include target '{raw_target}' (@[{label}]) does not exist in {rules_dir.name}/",
                        )
                    )

        rule_files = sorted(rules_dir.glob("*.md"))
        return len(rule_files), errors

    def validate_rule_size_limit(self, rules_dir_or_root: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates that rules/AGENTS.md and all recursively expanded include directives
        comply with the mechanical size ceiling (MAX_EXPANDED_RULE_BYTES = 20,000 bytes).
        Prevents silent truncation regressions below Antigravity's 24 KB context threshold.
        """
        errors: list[ValidationError] = []

        rules_dir_or_root = Path(rules_dir_or_root)
        if rules_dir_or_root.is_file():
            composer_path = rules_dir_or_root
            curr = rules_dir_or_root.parent
            rules_dir = curr
            while curr != curr.parent:
                if curr.name == "rules":
                    rules_dir = curr
                    break
                curr = curr.parent
            if rules_dir.name != "rules" and (rules_dir / "rules").is_dir():
                rules_dir = rules_dir / "rules"
        elif (rules_dir_or_root / "rules" / "AGENTS.md").is_file():
            composer_path = rules_dir_or_root / "rules" / "AGENTS.md"
            rules_dir = rules_dir_or_root / "rules"
        elif (rules_dir_or_root / "AGENTS.md").is_file():
            composer_path = rules_dir_or_root / "AGENTS.md"
            rules_dir = rules_dir_or_root
        elif rules_dir_or_root.name == "rules":
            composer_path = rules_dir_or_root / "AGENTS.md"
            rules_dir = rules_dir_or_root
        else:
            composer_path = rules_dir_or_root / "rules" / "AGENTS.md"
            rules_dir = rules_dir_or_root / "rules"

        if not composer_path.is_file():
            return 0, [
                ValidationError(
                    composer_path,
                    "RULE_NOT_FOUND",
                    "Missing Master Composer entrypoint 'rules/AGENTS.md'",
                )
            ]

        def _get_inactive_spans(text: str) -> list[tuple[int, int]]:
            spans: list[tuple[int, int]] = []
            for m in re.finditer(r"<!--.*?-->", text, flags=re.DOTALL):
                spans.append(m.span())
            for m in re.finditer(r"(?:`{3,}|~{3,}).*?(?:`{3,}|~{3,})", text, flags=re.DOTALL):
                spans.append(m.span())
            return spans

        def _is_inside_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
            for s_start, s_end in spans:
                if not (end <= s_start or start >= s_end):
                    return True
            return False

        def _resolve_target_path(current_file: Path, raw_target: str) -> Path | None:
            clean = raw_target.strip().strip("<> ")
            if "#" in clean:
                clean = clean.split("#", 1)[0]
            if "?" in clean:
                clean = clean.split("?", 1)[0]
            clean = unquote(clean).strip()
            clean = clean.replace("\\", "/")
            clean = clean.lstrip("/")
            if clean.startswith("./"):
                clean = clean[2:]
            clean = clean.lstrip("/")

            candidates = [
                current_file.parent / clean,
                rules_dir / clean,
                rules_dir.parent / clean,
            ]
            if clean.startswith("rules/"):
                candidates.append(rules_dir / clean[6:])
            elif clean.startswith(f"{rules_dir.name}/"):
                candidates.append(rules_dir / clean[len(rules_dir.name) + 1:])

            allowed_boundary = (rules_dir.parent if rules_dir.name == "rules" else rules_dir).resolve()
            for cand in candidates:
                try:
                    resolved = cand.resolve()
                    if resolved.is_file() and (resolved == allowed_boundary or resolved.is_relative_to(allowed_boundary)):
                        return resolved
                except Exception:
                    continue
            return None

        def _add_error(err: ValidationError) -> None:
            if not any(e.file_path == err.file_path and e.category == err.category and e.message == err.message for e in errors):
                errors.append(err)

        def _expand_file(file_path: Path, call_stack: list[Path]) -> str:
            resolved_file = file_path.resolve()
            if resolved_file in call_stack:
                cycle_idx = call_stack.index(resolved_file)
                cycle_chain = [p.name for p in call_stack[cycle_idx:]] + [resolved_file.name]
                cycle_str = " -> ".join(cycle_chain)
                _add_error(
                    ValidationError(
                        file_path,
                        "RULE_CIRCULAR_INCLUDE",
                        f"Circular rule include detected in '{file_path.name}': {cycle_str}",
                    )
                )
                return ""

            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception as exc:
                _add_error(ValidationError(file_path, "FILE_READ_ERROR", f"Could not read rule file '{file_path.name}': {exc}"))
                return ""

            spans = _get_inactive_spans(content)
            matches: list[re.Match[str]] = []
            for m in re.finditer(r"(?m)^[ \t]*@\[([^\]]*)\]\(([^)]+)\)[ \t]*\r?$", content):
                if not _is_inside_spans(m.start(), m.end(), spans):
                    matches.append(m)

            new_stack = call_stack + [resolved_file]
            expanded = content
            for m in reversed(matches):
                label = m.group(1).strip()
                raw_target = m.group(2).strip()
                target_path = _resolve_target_path(file_path, raw_target)
                if target_path is None:
                    _add_error(
                        ValidationError(
                            file_path,
                            "RULE_INCLUDE_TARGET_NOT_FOUND",
                            f"Rule include target '{raw_target}' (@[{label}]) does not exist",
                        )
                    )
                    replacement = ""
                elif target_path.resolve() in new_stack:
                    cycle_idx = new_stack.index(target_path.resolve())
                    cycle_chain = [p.name for p in new_stack[cycle_idx:]] + [target_path.name]
                    cycle_str = " -> ".join(cycle_chain)
                    _add_error(
                        ValidationError(
                            file_path,
                            "RULE_CIRCULAR_INCLUDE",
                            f"Circular rule include detected in '{file_path.name}': {cycle_str}",
                        )
                    )
                    replacement = ""
                else:
                    replacement = _expand_file(target_path, new_stack)
                expanded = expanded[: m.start()] + replacement + expanded[m.end() :]

            return expanded

        expanded_content = _expand_file(composer_path, [])
        expanded_bytes = len(expanded_content.encode("utf-8"))

        if expanded_bytes > self.MAX_EXPANDED_RULE_BYTES:
            overflow = expanded_bytes - self.MAX_EXPANDED_RULE_BYTES
            _add_error(
                ValidationError(
                    composer_path,
                    "RULE_SIZE_LIMIT_EXCEEDED",
                    f"Expanded rules footprint ({expanded_bytes} bytes) exceeds limit of "
                    f"{self.MAX_EXPANDED_RULE_BYTES} bytes (overflow: {overflow} bytes)",
                )
            )

        if not rules_dir_or_root.is_file():
            orch_master = rules_dir / "subagent-orchestration.md"
            if orch_master.is_file() and orch_master != composer_path:
                _expand_file(orch_master, [])
            orch_dir = rules_dir / "orchestration"
            if orch_dir.is_dir():
                for tf in sorted(orch_dir.glob("*.md")):
                    if tf != composer_path:
                        _expand_file(tf, [])

        return expanded_bytes, errors

    def validate_template_assets(self, templates_dir: Path) -> list[ValidationError]:
        """
        Validates existence and non-empty content of core template assets:
        - templates/report-template.html
        - templates/report-tokens.css
        - templates/prototype-switcher.html
        """
        errors: list[ValidationError] = []
        if not templates_dir.exists():
            return [ValidationError(templates_dir, "TEMPLATE_NOT_FOUND", "templates/ directory does not exist")]

        for asset in self.CORE_TEMPLATE_ASSETS:
            target = templates_dir / asset
            if not target.exists():
                errors.append(
                    ValidationError(
                        target,
                        "TEMPLATE_NOT_FOUND",
                        f"Required template asset '{asset}' not found in templates/",
                    )
                )
            else:
                try:
                    content = target.read_text(encoding="utf-8")
                    if not content.strip():
                        errors.append(
                            ValidationError(
                                target,
                                "TEMPLATE_EMPTY",
                                f"Required template asset '{asset}' is empty",
                            )
                        )
                except Exception as exc:
                    errors.append(ValidationError(target, "FILE_READ_ERROR", f"Could not read template asset '{asset}': {exc}"))

        return errors

    def validate_constraints_template(self, constraints_path: Path) -> list[ValidationError]:
        """Validates templates/CONSTRAINTS.md syntax, floor rules, numerical thresholds, and expiring exceptions."""
        errors: list[ValidationError] = []
        try:
            content = constraints_path.read_text(encoding="utf-8")
        except Exception as exc:
            return [ValidationError(constraints_path, "FILE_READ_ERROR", f"Could not read {constraints_path.name}: {exc}")]

        if not content.strip():
            return [ValidationError(constraints_path, "TEMPLATE_EMPTY", f"Template '{constraints_path.name}' is empty")]

        # Floor Rules section and required invariants
        if "## Floor Rules" not in content:
            errors.append(
                ValidationError(
                    constraints_path,
                    "CONSTRAINTS_MISSING_FLOOR_RULES",
                    "Missing '## Floor Rules' section in CONSTRAINTS.md",
                )
            )
        else:
            floor_content = content.split("## Floor Rules", 1)[1]
            if "## " in floor_content:
                floor_content = floor_content.split("## ", 1)[0]
            required_floor_tokens = ["@ts-ignore", "eslint-disable", "stub", "deleted"]
            for token in required_floor_tokens:
                if token.lower() not in floor_content.lower():
                    errors.append(
                        ValidationError(
                            constraints_path,
                            "CONSTRAINTS_FLOOR_RULE_MISSING",
                            f"Floor rules in CONSTRAINTS.md missing mandatory invariant regarding '{token}'",
                        )
                    )

        # Numerical Thresholds table
        if "## Numerical Thresholds" not in content:
            errors.append(
                ValidationError(
                    constraints_path,
                    "CONSTRAINTS_MISSING_THRESHOLDS",
                    "Missing '## Numerical Thresholds' section in CONSTRAINTS.md",
                )
            )
        else:
            thresh_content = content.split("## Numerical Thresholds", 1)[1]
            if "## " in thresh_content:
                thresh_content = thresh_content.split("## ", 1)[0]
            if not re.search(r"\|\s*Dimension\s*\|\s*Target\s*(?:Value)?\s*\|\s*Verification Command\s*\|", thresh_content, re.IGNORECASE):
                errors.append(
                    ValidationError(
                        constraints_path,
                        "CONSTRAINTS_MALFORMED_THRESHOLDS_TABLE",
                        "Numerical Thresholds table missing required columns (Dimension, Target Value, Verification Command)",
                    )
                )

        # Expiring Exceptions table
        if "## Expiring Exceptions" not in content:
            errors.append(
                ValidationError(
                    constraints_path,
                    "CONSTRAINTS_MISSING_EXCEPTIONS",
                    "Missing '## Expiring Exceptions' section in CONSTRAINTS.md",
                )
            )
        else:
            exc_content = content.split("## Expiring Exceptions", 1)[1]
            if "## " in exc_content:
                exc_content = exc_content.split("## ", 1)[0]
            if not re.search(r"\|\s*ID\s*\|\s*Dimension\s*\|\s*Rationale\s*\|\s*Expiration Date\s*\|", exc_content, re.IGNORECASE):
                errors.append(
                    ValidationError(
                        constraints_path,
                        "CONSTRAINTS_MALFORMED_EXCEPTIONS_TABLE",
                        "Expiring Exceptions table missing required columns (ID, Dimension, Rationale, Expiration Date)",
                    )
                )

        return errors

    def validate_definition_of_done_template(self, dod_path: Path) -> list[ValidationError]:
        """Validates templates/definition-of-done.md defining release criteria across all SDLC phases."""
        errors: list[ValidationError] = []
        try:
            content = dod_path.read_text(encoding="utf-8")
        except Exception as exc:
            return [ValidationError(dod_path, "FILE_READ_ERROR", f"Could not read {dod_path.name}: {exc}")]

        if not content.strip():
            return [ValidationError(dod_path, "TEMPLATE_EMPTY", f"Template '{dod_path.name}' is empty")]

        sdlc_phases = ["Correctness", "Quality", "Integration", "Documentation", "Ship-readiness"]
        for phase in sdlc_phases:
            if phase.lower() not in content.lower():
                errors.append(
                    ValidationError(
                        dod_path,
                        "DOD_MISSING_SDLC_PHASE",
                        f"Missing required SDLC phase '{phase}' in definition-of-done.md",
                    )
                )

        if "- [ ]" not in content:
            errors.append(
                ValidationError(
                    dod_path,
                    "DOD_MISSING_CHECKLIST",
                    "definition-of-done.md is missing standing checklist items ('- [ ]')",
                )
            )

        return errors

    def validate_templates(
        self,
        templates_dir: Path,
        check_core_assets: bool = False,
        root_dir: Path | None = None,
    ) -> tuple[int, list[ValidationError]]:
        """
        Validates template assets discovered across root templates/ and skills/*/resources/ with full parity.
        Ensures valid JSON syntax, readable non-empty assets, and executes specific schema validators where applicable.
        Audits deprecated template subdirectories and detects duplicate template assets ([ERR-SELF-CONT-02]).
        Detects stale template paths in skill runbooks and malformed template syntax ([ERR-SELF-CONT-01]).
        """
        errors: list[ValidationError] = []
        workspace_root = root_dir if root_dir is not None else templates_dir.parent
        skills_dir = workspace_root / "skills"

        if not templates_dir.exists() and not skills_dir.exists():
            return 0, errors

        # Check for deprecated template subdirectories and duplicate asset collisions ([ERR-SELF-CONT-02])
        if templates_dir.exists():
            for dep_name in self.DEPRECATED_TEMPLATE_SUBDIRS:
                dep_path = templates_dir / dep_name
                if dep_path.exists():
                    local_resource_dir = skills_dir / dep_name / "resources"
                    if dep_path.is_file():
                        errors.append(
                            ValidationError(
                                dep_path,
                                "DEPRECATED_TEMPLATE_PATH",
                                f"Deprecated template file 'templates/{dep_name}' must be pruned; relocate to skills/{dep_name}/resources/",
                            )
                        )
                    else:
                        dep_files = [p for p in dep_path.rglob("*") if p.is_file()]
                        if not dep_files:
                            errors.append(
                                ValidationError(
                                    dep_path,
                                    "DEPRECATED_TEMPLATE_PATH",
                                    f"Deprecated template directory 'templates/{dep_name}' must be pruned; relocate assets to skills/{dep_name}/resources/",
                                )
                            )
                        for f in dep_files:
                            rel_file = f.relative_to(dep_path)
                            local_f = local_resource_dir / rel_file
                            if local_f.exists():
                                errors.append(
                                    ValidationError(
                                        f,
                                        "DUPLICATE_TEMPLATE_ASSET",
                                        f"Duplicate template asset detected at deprecated root path '{f}'. Single source of truth must reside in '{local_f}'",
                                    )
                                )
                            else:
                                errors.append(
                                    ValidationError(
                                        f,
                                        "DEPRECATED_TEMPLATE_PATH",
                                        f"Template asset residing in deprecated root directory '{f}'; must be relocated to '{local_resource_dir / rel_file}'",
                                    )
                                )

        # Detect stale template path references in skill runbooks and skill files ([ERR-SELF-CONT-01])
        if skills_dir.exists():
            for skill_file in sorted(skills_dir.rglob("*")):
                if not skill_file.is_file():
                    continue
                if skill_file.name.startswith(".") or skill_file.suffix in (".pyc", ".png", ".ico", ".svg"):
                    continue
                try:
                    file_content = skill_file.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                for dep_name in self.DEPRECATED_TEMPLATE_SUBDIRS:
                    stale_patterns = (f"../../templates/{dep_name}", f"templates/{dep_name}/", f"templates/{dep_name}")
                    for line_idx, line in enumerate(file_content.splitlines(), start=1):
                        for pattern in stale_patterns:
                            if pattern in line:
                                errors.append(
                                    ValidationError(
                                        skill_file,
                                        "DEAD_LINK_ERROR",
                                        f"Stale template path reference '{pattern}' found in {skill_file.name}:{line_idx}; template has been relocated to skills/{dep_name}/resources/",
                                        line=line_idx,
                                    )
                                )
                                break

        if check_core_assets and templates_dir.exists():
            errors.extend(self.validate_template_assets(templates_dir))

        # Discover template directories across root templates/ and all skills/*/resources/ ([EDGE-SELF-CONT-01])
        template_dirs: list[Path] = []
        if templates_dir.exists() and templates_dir.is_dir():
            template_dirs.append(templates_dir)
        if skills_dir.exists() and skills_dir.is_dir():
            template_dirs.extend(sorted([d for d in skills_dir.glob("*/resources") if d.is_dir()]))

        json_files: list[Path] = []
        html_files: list[Path] = []
        css_files: list[Path] = []
        md_files: list[Path] = []
        ts_files: list[Path] = []
        py_files: list[Path] = []

        for t_dir in template_dirs:
            json_files.extend(t_dir.rglob("*.json"))
            html_files.extend(t_dir.rglob("*.html"))
            css_files.extend(t_dir.rglob("*.css"))
            md_files.extend(t_dir.rglob("*.md"))
            ts_files.extend(list(t_dir.rglob("*.ts")) + list(t_dir.rglob("*.tsx")))
            py_files.extend(t_dir.rglob("*.py"))

        json_files = sorted(set(json_files))
        html_files = sorted(set(html_files))
        css_files = sorted(set(css_files))
        md_files = sorted(set(md_files))
        ts_files = sorted(set(ts_files))
        py_files = sorted(set(py_files))

        for t_file in json_files:
            try:
                content = t_file.read_text(encoding="utf-8")
                if not content.strip():
                    errors.append(
                        ValidationError(
                            t_file,
                            "TEMPLATE_EMPTY",
                            f"Template asset '{t_file.name}' is empty",
                        )
                    )
                    continue
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                errors.append(ValidationError(t_file, "TEMPLATE_MALFORMED_JSON", f"Invalid JSON in {t_file.name}: {exc}"))
                continue
            except Exception as exc:
                errors.append(ValidationError(t_file, "FILE_READ_ERROR", f"Could not read {t_file.name}: {exc}"))
                continue

            if not isinstance(data, dict):
                errors.append(ValidationError(t_file, "TEMPLATE_MALFORMED_JSON", f"Root of {t_file.name} must be a JSON object"))
                continue

            if t_file.name == "matt-gate.json":
                errors.extend(self.validate_gate_config(t_file))
            elif t_file.parent.name == "hooks" or t_file.name.endswith("-hooks.json"):
                errors.extend(self.validate_hooks(t_file))

        core_asset_paths = {
            templates_dir / asset for asset in self.CORE_TEMPLATE_ASSETS
        } if (check_core_assets and templates_dir.exists()) else set()

        for asset_file in html_files + css_files:
            if check_core_assets and asset_file in core_asset_paths:
                continue
            try:
                content = asset_file.read_text(encoding="utf-8")
                if not content.strip():
                    errors.append(
                        ValidationError(
                            asset_file,
                            "TEMPLATE_EMPTY",
                            f"Template asset '{asset_file.name}' is empty",
                        )
                    )
            except Exception as exc:
                errors.append(ValidationError(asset_file, "FILE_READ_ERROR", f"Could not read {asset_file.name}: {exc}"))

        for md_file in md_files:
            if md_file.name == "CONSTRAINTS.md":
                errors.extend(self.validate_constraints_template(md_file))
            elif md_file.name == "definition-of-done.md":
                errors.extend(self.validate_definition_of_done_template(md_file))
            else:
                try:
                    content = md_file.read_text(encoding="utf-8")
                    if not content.strip():
                        errors.append(
                            ValidationError(
                                md_file,
                                "TEMPLATE_EMPTY",
                                f"Template file '{md_file.name}' is empty",
                            )
                        )
                except Exception as exc:
                    errors.append(ValidationError(md_file, "FILE_READ_ERROR", f"Could not read {md_file.name}: {exc}"))

        for ts_file in ts_files:
            errors.extend(self.validate_typescript_template(ts_file, check_contracts=check_core_assets))

        for py_file in py_files:
            errors.extend(self.validate_python_template(py_file))

        total_scanned = len(json_files) + len(html_files) + len(css_files) + len(ts_files) + len(md_files) + len(py_files)
        return total_scanned, errors

    def _check_ts_delimiters_and_syntax(self, content: str) -> str | None:
        """
        Scans TypeScript content to verify balanced delimiters ({}, (), [])
        and properly closed string literals and block comments.
        """
        stack: list[tuple[str, int]] = []
        pairs = {"}": "{", ")": "(", "]": "["}
        i = 0
        n = len(content)
        line = 1

        while i < n:
            c = content[i]

            if c == "\n":
                line += 1
                i += 1
                continue

            if c == "/" and i + 1 < n and content[i + 1] == "/":
                i += 2
                while i < n and content[i] != "\n":
                    i += 1
                continue

            if c == "/" and i + 1 < n and content[i + 1] == "*":
                comment_start_line = line
                i += 2
                closed = False
                while i + 1 < n:
                    if content[i] == "\n":
                        line += 1
                    if content[i] == "*" and content[i + 1] == "/":
                        i += 2
                        closed = True
                        break
                    i += 1
                if not closed:
                    return f"Unclosed block comment '/*' starting at line {comment_start_line}"
                continue

            if c in ("'", '"'):
                quote = c
                quote_start_line = line
                i += 1
                closed = False
                while i < n:
                    if content[i] == "\n":
                        return f"Unclosed string literal starting at line {quote_start_line}"
                    if content[i] == "\\":
                        i += 2
                        continue
                    if content[i] == quote:
                        closed = True
                        i += 1
                        break
                    i += 1
                if not closed:
                    return f"Unclosed string literal starting at line {quote_start_line}"
                continue

            if c == "`":
                template_start_line = line
                i += 1
                closed = False
                while i < n:
                    if content[i] == "\n":
                        line += 1
                    elif content[i] == "\\":
                        i += 2
                        continue
                    elif content[i] == "$" and i + 1 < n and content[i + 1] == "{":
                        stack.append(("${", line))
                        i += 2
                        break
                    elif content[i] == "`":
                        closed = True
                        i += 1
                        break
                    i += 1
                if not closed and (not stack or stack[-1][0] != "${"):
                    return f"Unclosed template literal '`' starting at line {template_start_line}"
                continue

            if c in ("{", "(", "["):
                stack.append((c, line))
                i += 1
                continue

            if c in ("}", ")", "]"):
                if not stack:
                    return f"Unexpected closing delimiter '{c}' at line {line}"
                top, top_line = stack.pop()
                if top == "${" and c == "}":
                    template_start_line = line
                    closed = False
                    i += 1
                    while i < n:
                        if content[i] == "\n":
                            line += 1
                        elif content[i] == "\\":
                            i += 2
                            continue
                        elif content[i] == "$" and i + 1 < n and content[i + 1] == "{":
                            stack.append(("${", line))
                            i += 2
                            break
                        elif content[i] == "`":
                            closed = True
                            i += 1
                            break
                        i += 1
                    if not closed and (not stack or stack[-1][0] != "${"):
                        return f"Unclosed template literal '`' starting at line {template_start_line}"
                    continue
                expected = pairs[c]
                if top != expected:
                    return f"Mismatched delimiter: expected closing for '{top}' (from line {top_line}), but found '{c}' at line {line}"
                i += 1
                continue

            i += 1

        if stack:
            unclosed, u_line = stack[-1]
            return f"Unclosed delimiter '{unclosed}' opened at line {u_line}"

        return None

    def validate_typescript_template(self, ts_file: Path, check_contracts: bool = True) -> list[ValidationError]:
        """
        [EDGE-SELF-CONT-02] Validates TypeScript and TSX template assets in local skill resources and templates.
        Enforces Total TypeScript rigor: zero double-casting assertions, zero unconstrained any annotations,
        zero blind compiler suppressions, valid syntax (balanced delimiters, closed comments),
        and verified execution contract seams.
        """
        errors: list[ValidationError] = []
        try:
            content = ts_file.read_text(encoding="utf-8")
        except Exception as exc:
            return [ValidationError(ts_file, "FILE_READ_ERROR", f"Could not read {ts_file.name}: {exc}")]

        if not content.strip():
            return [
                ValidationError(
                    ts_file,
                    "TEMPLATE_EMPTY",
                    f"Template asset '{ts_file.name}' is empty",
                )
            ]

        for line_idx, line in enumerate(content.splitlines(), start=1):
            if "as unknown as" in line:
                errors.append(
                    ValidationError(
                        ts_file,
                        "TEMPLATE_BANNED_TYPESCRIPT_PATTERN",
                        f"Banned TypeScript pattern 'as unknown as' found in {ts_file.name}:{line_idx}",
                        line=line_idx,
                    )
                )
            if "as any as" in line:
                errors.append(
                    ValidationError(
                        ts_file,
                        "TEMPLATE_BANNED_TYPESCRIPT_PATTERN",
                        f"Banned TypeScript pattern 'as any as' found in {ts_file.name}:{line_idx}",
                        line=line_idx,
                    )
                )

            if re.search(r"(:\s*any\b|as\s+any\b|<any>|any\[\]|Array<any>)", line):
                errors.append(
                    ValidationError(
                        ts_file,
                        "TEMPLATE_BANNED_TYPESCRIPT_PATTERN",
                        f"Banned TypeScript pattern 'any' found in {ts_file.name}:{line_idx}",
                        line=line_idx,
                    )
                )

            if "@ts-ignore" in line or "@ts-nocheck" in line:
                errors.append(
                    ValidationError(
                        ts_file,
                        "TEMPLATE_BANNED_TYPESCRIPT_PATTERN",
                        f"Banned TypeScript compiler suppression found in {ts_file.name}:{line_idx}",
                        line=line_idx,
                    )
                )

        syntax_error = self._check_ts_delimiters_and_syntax(content)
        if syntax_error:
            errors.append(
                ValidationError(
                    ts_file,
                    "TEMPLATE_MALFORMED_SYNTAX",
                    f"TypeScript syntax error in {ts_file.name}: {syntax_error}",
                )
            )

        if check_contracts:
            if ts_file.name == "native-ts-transformer.ts":
                required_symbols = [
                    "runNativeTransform",
                    "NativeTransformOptions",
                    "NativeTransformResult",
                ]
                for sym in required_symbols:
                    if sym not in content:
                        errors.append(
                            ValidationError(
                                ts_file,
                                "TEMPLATE_MISSING_CONTRACT",
                                f"Native TypeScript transformer template missing required export contract '{sym}' in {ts_file.name}",
                            )
                        )
            elif ts_file.name == "ts-morph-transformer.ts":
                required_symbols = [
                    "runTransform",
                    "TransformOptions",
                    "TransformResult",
                ]
                for sym in required_symbols:
                    if sym not in content:
                        errors.append(
                            ValidationError(
                                ts_file,
                                "TEMPLATE_MISSING_CONTRACT",
                                f"ts-morph transformer template missing required export contract '{sym}' in {ts_file.name}",
                            )
                        )
            elif ts_file.name == "factory-template.ts":
                required_symbols = [
                    "defineFactory",
                    "createScenario",
                    "sequence",
                    "resetSequences",
                    "Factory",
                    "FactoryConfig",
                ]
                for sym in required_symbols:
                    if sym not in content:
                        errors.append(
                            ValidationError(
                                ts_file,
                                "TEMPLATE_MISSING_CONTRACT",
                                f"Scenario factory template missing required export contract '{sym}' in {ts_file.name}",
                            )
                        )

        return errors

    def validate_python_template(self, py_file: Path) -> list[ValidationError]:
        """
        [INTEG-POLY-01] [ERR-INTEG-POLY-01] Validates Python template assets in local skill resources and templates.
        Parses source into AST via stdlib ast.parse(), rejects empty files with TEMPLATE_EMPTY,
        and catches SyntaxError reporting TEMPLATE_PYTHON_SYNTAX_ERROR with exact line number.
        Also enforces [POLY-RULE-02] and [ERR-POLY-RULE-03]:
        - Bans bare '# type: ignore' without bracketed codes (TEMPLATE_BARE_TYPE_IGNORE).
        - Bans unconstrained 'Any' on public function/method parameters and returns (TEMPLATE_PYTHON_UNCHECKED_ANY).
        """
        errors: list[ValidationError] = []
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception as exc:
            return [ValidationError(py_file, "FILE_READ_ERROR", f"Could not read {py_file.name}: {exc}")]

        if not content.strip():
            return [
                ValidationError(
                    py_file,
                    "TEMPLATE_EMPTY",
                    f"Template asset '{py_file.name}' is empty",
                )
            ]

        # Ban bare '# type: ignore' without bracketed error codes
        for line_idx, line in enumerate(content.splitlines(), start=1):
            if re.search(r"#\s*type:\s*ignore\s*(?:$|[^\[\w])", line):
                errors.append(
                    ValidationError(
                        py_file,
                        "TEMPLATE_BARE_TYPE_IGNORE",
                        f"Bare '# type: ignore' without bracketed code prohibited on line {line_idx} in {py_file.name}",
                        line=line_idx,
                    )
                )

        try:
            tree = ast.parse(content, filename=str(py_file))
        except SyntaxError as exc:
            line_suffix = f":{exc.lineno}" if exc.lineno is not None else ""
            errors.append(
                ValidationError(
                    py_file,
                    "TEMPLATE_PYTHON_SYNTAX_ERROR",
                    f"Python syntax error in {py_file.name}{line_suffix}: {exc.msg}",
                    line=exc.lineno,
                )
            )
            return errors

        # Public seam typing inspection
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                func_name = node.name
                is_public = not func_name.startswith("_") or func_name in ("__init__", "__call__")
                if is_public:
                    for arg in node.args.args:
                        if arg.arg in ("self", "cls"):
                            continue
                        if arg.annotation and isinstance(arg.annotation, ast.Name) and arg.annotation.id == "Any":
                            errors.append(
                                ValidationError(
                                    py_file,
                                    "TEMPLATE_PYTHON_UNCHECKED_ANY",
                                    f"Unchecked 'Any' annotation on public parameter '{arg.arg}' of function '{func_name}' in {py_file.name}:{arg.lineno}",
                                    line=arg.lineno,
                                )
                            )
                    if node.returns and isinstance(node.returns, ast.Name) and node.returns.id == "Any":
                        errors.append(
                            ValidationError(
                                py_file,
                                "TEMPLATE_PYTHON_UNCHECKED_ANY",
                                f"Unchecked 'Any' return annotation on public function '{func_name}' in {py_file.name}:{node.returns.lineno}",
                                line=node.returns.lineno,
                            )
                        )

        return errors

    @staticmethod
    def _read_skill_with_references(skill_path: Path) -> str:
        """
        Reads a skill runbook (SKILL.md) along with any markdown reference files in its local references/ directory.
        Supports progressive disclosure architecture by combining extracted reference catalogs into semantic audits.
        """
        content = skill_path.read_text(encoding="utf-8")
        ref_dir = skill_path.parent / "references"
        if ref_dir.is_dir():
            for ref_file in sorted(ref_dir.glob("*.md")):
                try:
                    content += "\n\n" + ref_file.read_text(encoding="utf-8")
                except Exception:
                    pass
        return content


    def relocate_skill_templates(self, root_dir: Path) -> list[str]:
        """
        [SELF-CONT-01][ERR-SELF-CONT-02] Relocates domain-specific templates from templates/ to skills/*/resources/.
        Strictly idempotent: safely migrates files, prunes deprecated root subdirectories, and avoids corrupting existing local resources.
        Returns list of relocated relative file paths.
        """
        templates_dir = root_dir / "templates"
        skills_dir = root_dir / "skills"
        relocated_files: list[str] = []

        if not templates_dir.exists():
            return relocated_files

        for dep_name in self.DEPRECATED_TEMPLATE_SUBDIRS:
            dep_path = templates_dir / dep_name
            if not dep_path.exists():
                continue

            target_res_dir = skills_dir / dep_name / "resources"
            target_res_dir.mkdir(parents=True, exist_ok=True)

            if dep_path.is_file():
                dest = target_res_dir / dep_path.name
                shutil.copy2(dep_path, dest)
                dep_path.unlink()
                relocated_files.append(str(dest.relative_to(root_dir)))
            elif dep_path.is_dir():
                for f in sorted(dep_path.rglob("*")):
                    if f.is_file():
                        rel = f.relative_to(dep_path)
                        dest = target_res_dir / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dest)
                        relocated_files.append(str(dest.relative_to(root_dir)))
                # Clean up deprecated directory tree
                shutil.rmtree(dep_path, ignore_errors=True)

        return relocated_files

    def validate_shell_network_safety(self, script_path: Path, content: str) -> list[ValidationError]:
        """
        Validates that any network invocations (curl, wget) in shell scripts declare explicit timeouts.
        - 'curl' must specify '--connect-timeout <seconds>' and ('--max-time <seconds>' or '-m <seconds>').
        - 'wget' must specify ('--timeout <seconds>' or '-T <seconds>').
        Avoids false positives on comments, echo/printf output statements, command lookups (command -v, which, type),
        and string literals inside logging/error messages.
        """
        errors: list[ValidationError] = []
        cleaned_content = re.sub(r"\\\s*\n", " ", content)
        in_heredoc = False
        heredoc_delimiter = ""

        for line_idx, line in enumerate(cleaned_content.splitlines(), start=1):
            line_trimmed = line.strip()
            if in_heredoc:
                if line_trimmed == heredoc_delimiter:
                    in_heredoc = False
                    heredoc_delimiter = ""
                continue

            if line_trimmed.startswith("#"):
                continue

            # Check if this line initiates a heredoc
            heredoc_match = re.search(r"<<-?\s*['\"]?([A-Za-z0-9_]+)['\"]?", line)
            if heredoc_match:
                in_heredoc = True
                heredoc_delimiter = heredoc_match.group(1)
                continue

            if re.match(r"^\s*(echo|printf)\b", line_trimmed):
                continue

            # Strip command checks: command -v <cmd>, which <cmd>, type <cmd>
            curl_stripped = re.sub(r"\b(command\s+-v|which|type)\s+curl\b", "", line)
            # Strip string literals so error/log messages mentioning curl don't trigger false positives
            curl_effective = re.sub(r'("[^"]*"|\'[^\']*\')', '""', curl_stripped)
            if re.search(r"\bcurl\b", curl_effective):
                has_connect_timeout = bool(re.search(r"--connect-timeout\s+\d+", line))
                has_max_time = bool(re.search(r"(--max-time|-m)\s+\d+", line))
                if not (has_connect_timeout and has_max_time):
                    errors.append(
                        ValidationError(
                            script_path,
                            "SCRIPT_UNBOUNDED_NETWORK_CALL",
                            f"Shell script {script_path.name} line {line_idx} invokes 'curl' without both '--connect-timeout <seconds>' and '--max-time <seconds>' bounds",
                            line=line_idx,
                        )
                    )

            wget_stripped = re.sub(r"\b(command\s+-v|which|type)\s+wget\b", "", line)
            wget_effective = re.sub(r'("[^"]*"|\'[^\']*\')', '""', wget_stripped)
            if re.search(r"\bwget\b", wget_effective):
                has_timeout = bool(re.search(r"(--timeout|-T)\s+\d+", line))
                if not has_timeout:
                    errors.append(
                        ValidationError(
                            script_path,
                            "SCRIPT_UNBOUNDED_NETWORK_CALL",
                            f"Shell script {script_path.name} line {line_idx} invokes 'wget' without explicit timeout bounds ('--timeout <seconds>' or '-T <seconds>')",
                            line=line_idx,
                        )
                    )
        return errors

    BANNED_SHELL_COMMENT_PATTERNS = [
        (re.compile(r"^\s*#\s*Parse (?:options|args|arguments|flags)\b", re.IGNORECASE), "Parse options/args"),
        (re.compile(r"^\s*#\s*Check for required (?:HTTP download client|tar|curl|wget)\b", re.IGNORECASE), "Check for required HTTP client/tar"),
        (re.compile(r"^\s*#\s*Check for (?:tar|curl|wget)\b", re.IGNORECASE), "Check for tool existence"),
        (re.compile(r"^\s*#\s*(?:Compute|Print) summary metrics\b", re.IGNORECASE), "Compute/Print summary metrics"),
        (re.compile(r"^\s*#\s*Package runtime assets into (?:gzip )?tarball\b", re.IGNORECASE), "Package runtime assets"),
        (re.compile(r"^\s*#\s*Compute SHA256 checksum\b", re.IGNORECASE), "Compute SHA256 checksum"),
    ]

    def validate_shell_clean_comments(self, script_path: Path, content: str) -> list[ValidationError]:
        """
        Enforces Clean Comment Discipline (Fowler Smell #13: Obvious Comments) on shell scripts.
        Flags redundant comments that merely echo syntax rather than providing non-obvious 'Why' rationale.
        """
        errors: list[ValidationError] = []
        for line_idx, line in enumerate(content.splitlines(), start=1):
            line_trimmed = line.strip()
            if not line_trimmed.startswith("#"):
                continue
            for pattern, pattern_name in self.BANNED_SHELL_COMMENT_PATTERNS:
                if pattern.search(line_trimmed):
                    errors.append(
                        ValidationError(
                            script_path,
                            "SCRIPT_OBVIOUS_COMMENT",
                            f"Shell script {script_path.name} line {line_idx} contains obvious syntax-repeating comment matching '{pattern_name}'. Adhere to Clean Comment Discipline (Fowler Smell #13).",
                            line=line_idx,
                        )
                    )
                    break
        return errors

    def validate_hook_scripts(self, hooks_dir: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates permissions, shebang, and bash syntax for hook scripts in hooks/.
        Enforces 0755 permissions and '#!/usr/bin/env bash' on hooks/research-cache.sh.
        Also validates that network requests declare explicit timeouts.
        """
        errors: list[ValidationError] = []
        if not hooks_dir.exists():
            return 0, errors

        scanned = 0
        for hook_sh in sorted(hooks_dir.glob("*.sh")):
            scanned += 1
            if not os.access(hook_sh, os.X_OK):
                errors.append(
                    ValidationError(
                        hook_sh,
                        "SCRIPT_NOT_EXECUTABLE",
                        f"Hook script {hook_sh.name} is missing executable bit (+x)",
                    )
                )

            mode = hook_sh.stat().st_mode & 0o777
            if mode != 0o755:
                errors.append(
                    ValidationError(
                        hook_sh,
                        "SCRIPT_INVALID_PERMISSIONS",
                        f"Hook script {hook_sh.name} must have 0755 permissions, got {oct(mode)}",
                    )
                )

            try:
                content = hook_sh.read_text(encoding="utf-8")
                lines = content.splitlines() if hook_sh.stat().st_size > 0 else []
                first_line = lines[0].strip() if lines else ""
                if not first_line.startswith("#!"):
                    errors.append(
                        ValidationError(
                            hook_sh,
                            "SCRIPT_MISSING_SHEBANG",
                            f"Hook script {hook_sh.name} is missing shebang line",
                        )
                    )
                elif first_line != "#!/usr/bin/env bash":
                    errors.append(
                        ValidationError(
                            hook_sh,
                            "SCRIPT_INVALID_SHEBANG",
                            f"Hook script {hook_sh.name} must declare shebang '#!/usr/bin/env bash', got '{first_line}'",
                        )
                    )
                # Validate network timeouts
                errors.extend(self.validate_shell_network_safety(hook_sh, content))
                # Validate clean comment discipline (Smell #13)
                errors.extend(self.validate_shell_clean_comments(hook_sh, content))
            except Exception as exc:
                errors.append(ValidationError(hook_sh, "FILE_READ_ERROR", f"Could not read {hook_sh.name}: {exc}"))

            # Validate bash syntax via bash -n
            try:
                syntax_check = subprocess.run(
                    ["bash", "-n", str(hook_sh)],
                    capture_output=True,
                    text=True,
                )
                if syntax_check.returncode != 0:
                    errors.append(
                        ValidationError(
                            hook_sh,
                            "SCRIPT_SYNTAX_ERROR",
                            f"Hook script {hook_sh.name} has syntax error: {syntax_check.stderr.strip()}",
                        )
                    )
            except Exception as exc:
                errors.append(
                    ValidationError(
                        hook_sh,
                        "SCRIPT_SYNTAX_ERROR",
                        f"Could not verify syntax for {hook_sh.name}: {exc}",
                    )
                )

        return scanned, errors

    def validate_scripts(self, scripts_dir: Path, hooks_dir: Path | None = None) -> tuple[int, list[ValidationError]]:
        """
        Validates executable bits and shebang declarations across scripts/, scripts/internal/, and hooks/.
        Enforces 0755 permissions and '#!/usr/bin/env bash' on scripts/serve-report.sh,
        scripts/setup-worktree-env.sh, scripts/codemod.sh, and hooks/research-cache.sh.
        """
        errors: list[ValidationError] = []
        if not scripts_dir.exists():
            return 0, errors

        scanned = 0
        # Check all .sh files in scripts/
        for sh_path in sorted(scripts_dir.glob("*.sh")):
            scanned += 1
            if not os.access(sh_path, os.X_OK):
                errors.append(
                    ValidationError(
                        sh_path,
                        "SCRIPT_NOT_EXECUTABLE",
                        f"Shell script {sh_path.name} is missing executable bit (+x)",
                    )
                )

            # Specific rules for serve-report.sh, setup-worktree-env.sh, codemod.sh, verify-spec.sh, and verify-research.sh
            if sh_path.name in ("serve-report.sh", "setup-worktree-env.sh", "codemod.sh", "verify-spec.sh", "verify-research.sh"):
                mode = sh_path.stat().st_mode & 0o777
                if mode != 0o755:
                    errors.append(
                        ValidationError(
                            sh_path,
                            "SCRIPT_INVALID_PERMISSIONS",
                            f"Script {sh_path.name} must have 0755 permissions, got {oct(mode)}",
                        )
                    )

            try:
                first_line = sh_path.read_text(encoding="utf-8").splitlines()[0] if sh_path.stat().st_size > 0 else ""
                if not first_line.startswith("#!"):
                    errors.append(
                        ValidationError(
                            sh_path,
                            "SCRIPT_MISSING_SHEBANG",
                            f"Shell script {sh_path.name} is missing shebang line",
                        )
                    )
                elif sh_path.name in ("serve-report.sh", "setup-worktree-env.sh", "codemod.sh", "verify-spec.sh", "verify-research.sh") and first_line.strip() != "#!/usr/bin/env bash":
                    errors.append(
                        ValidationError(
                            sh_path,
                            "SCRIPT_INVALID_SHEBANG",
                            f"Script {sh_path.name} must declare shebang '#!/usr/bin/env bash', got '{first_line.strip()}'",
                        )
                    )
                # Validate network timeouts
                content = sh_path.read_text(encoding="utf-8")
                errors.extend(self.validate_shell_network_safety(sh_path, content))
                # Validate clean comment discipline (Smell #13)
                errors.extend(self.validate_shell_clean_comments(sh_path, content))
            except Exception as exc:
                errors.append(ValidationError(sh_path, "FILE_READ_ERROR", f"Could not read {sh_path.name}: {exc}"))

        # Check internal python scripts in scripts/internal/
        internal_dir = scripts_dir / "internal"
        if internal_dir.exists():
            for py_path in sorted(internal_dir.glob("*.py")):
                scanned += 1
                try:
                    content = py_path.read_text(encoding="utf-8")
                    compile(content, str(py_path), "exec")
                except SyntaxError as syn_err:
                    errors.append(
                        ValidationError(
                            py_path,
                            "SCRIPT_SYNTAX_ERROR",
                            f"Python script {py_path.name} has syntax error: {syn_err}",
                            line=syn_err.lineno,
                        )
                    )
                except Exception as exc:
                    errors.append(ValidationError(py_path, "FILE_READ_ERROR", f"Could not read {py_path.name}: {exc}"))

        # Check hook scripts in hooks/
        resolved_hooks_dir = hooks_dir if hooks_dir is not None else (scripts_dir.parent / "hooks" if scripts_dir.parent else None)
        if resolved_hooks_dir and resolved_hooks_dir.exists():
            h_scanned, h_errors = self.validate_hook_scripts(resolved_hooks_dir)
            scanned += h_scanned
            errors.extend(h_errors)

        return scanned, errors

    def validate_track_10_invariants(self, root_dir: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 10 behavioral decision gates, anti-slop discipline, and adversarial interrogation invariants:
        1. Empirical Question Classification Invariant (rules/gemini-behavior-guardrails.md, rules/AGENTS.md, skills/grilling, skills/implement)
        2. Attack the Premise Circuit Breaker (skills/diagnosing-bugs, agents/bug-diagnoser.md, rules/subagent-orchestration.md Trigger C)
        3. Clean Comment Discipline & Fowler Smell #13 (rules/typescript-standards.md, skills/code-review/resources/fowler-smells.md, agents/code-reviewer.md)
        4. Subtract-Before-Add Step in Task Graph Slicing (skills/to-tickets, skills/implement, skills/implement-spec)
        5. Adversarial Interrogation Protocol in Design Swarms (rules/subagent-orchestration.md Trigger E, rules/AGENTS.md, skills/codebase-design, agents/design-explorer.md)
        """
        errors: list[ValidationError] = []
        invariants_scanned = 5

        # --- Invariant 1: Empirical Question Classification Invariant ---
        guardrails_path = root_dir / "rules" / "gemini-behavior-guardrails.md"
        if not guardrails_path.is_file():
            errors.append(ValidationError(guardrails_path, "TRACK10_EMPIRICAL_GATE_VIOLATION", "Missing required rule 'rules/gemini-behavior-guardrails.md'"))
        else:
            try:
                g_content = guardrails_path.read_text(encoding="utf-8")
                if not re.search(r"(?m)^##\s+7\.\s+Empirical Question Classification Invariant", g_content):
                    errors.append(ValidationError(guardrails_path, "TRACK10_EMPIRICAL_GATE_VIOLATION", "rules/gemini-behavior-guardrails.md missing Section 7 'Empirical Question Classification Invariant'"))
                if "ask_question" not in g_content:
                    errors.append(ValidationError(guardrails_path, "TRACK10_EMPIRICAL_GATE_VIOLATION", "rules/gemini-behavior-guardrails.md must reference 'ask_question'"))
                if "empirical fact" not in g_content.lower() or "subjective" not in g_content.lower():
                    errors.append(ValidationError(guardrails_path, "TRACK10_EMPIRICAL_GATE_VIOLATION", "rules/gemini-behavior-guardrails.md must classify Observable Empirical Facts vs Subjective Preferences"))
                if "/prototype" not in g_content and "prototype-builder" not in g_content:
                    errors.append(ValidationError(guardrails_path, "TRACK10_EMPIRICAL_GATE_VIOLATION", "rules/gemini-behavior-guardrails.md must reference autonomous prototype escalation"))
                if not re.search(r"(?m)^##\s+9\.\s+Nested Code Block Fencing Invariant", g_content):
                    errors.append(ValidationError(guardrails_path, "TRACK10_EMPIRICAL_GATE_VIOLATION", "rules/gemini-behavior-guardrails.md missing Section 9 'Nested Code Block Fencing Invariant'"))
                if "4-backtick" not in g_content.lower() and "4 backtick" not in g_content.lower():
                    errors.append(ValidationError(guardrails_path, "TRACK10_EMPIRICAL_GATE_VIOLATION", "rules/gemini-behavior-guardrails.md must mandate 4-backtick enclosing for nested code blocks"))
            except Exception as exc:
                errors.append(ValidationError(guardrails_path, "FILE_READ_ERROR", f"Could not read {guardrails_path.name}: {exc}"))

        agents_rule = root_dir / "rules" / "AGENTS.md"
        if agents_rule.is_file():
            try:
                a_content = agents_rule.read_text(encoding="utf-8")
                if "Empirical Question Classification Invariant" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK10_EMPIRICAL_GATE_VIOLATION", "rules/AGENTS.md must summarize Empirical Question Classification Invariant"))
                if "Nested Code Block Fencing Invariant" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK10_EMPIRICAL_GATE_VIOLATION", "rules/AGENTS.md must summarize Nested Code Block Fencing Invariant"))
            except Exception as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        grilling_skill = root_dir / "skills" / "grilling" / "SKILL.md"
        if grilling_skill.is_file():
            try:
                gr_content = grilling_skill.read_text(encoding="utf-8")
                if "ask_question" not in gr_content or ("empirical" not in gr_content.lower() and "classification" not in gr_content.lower()):
                    errors.append(ValidationError(grilling_skill, "TRACK10_EMPIRICAL_GATE_VIOLATION", "skills/grilling/SKILL.md missing empirical classification gate"))
            except Exception as exc:
                errors.append(ValidationError(grilling_skill, "FILE_READ_ERROR", f"Could not read {grilling_skill.name}: {exc}"))

        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if implement_skill.is_file():
            try:
                imp_content = self._read_skill_with_references(implement_skill)
                if "runnable proof" not in imp_content.lower() and "empirical" not in imp_content.lower():
                    errors.append(ValidationError(implement_skill, "TRACK10_EMPIRICAL_GATE_VIOLATION", "skills/implement/SKILL.md missing pre-flight check for empirical verification / runnable proof"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        # --- Invariant 2: Attack the Premise Circuit Breaker ---
        bugs_skill = root_dir / "skills" / "diagnosing-bugs" / "SKILL.md"
        if not bugs_skill.is_file():
            errors.append(ValidationError(bugs_skill, "TRACK10_CIRCUIT_BREAKER_VIOLATION", "Missing required skill 'skills/diagnosing-bugs/SKILL.md'"))
        else:
            try:
                b_content = bugs_skill.read_text(encoding="utf-8")
                if "Attack the Premise" not in b_content:
                    errors.append(ValidationError(bugs_skill, "TRACK10_CIRCUIT_BREAKER_VIOLATION", "skills/diagnosing-bugs/SKILL.md missing 'Attack the Premise'"))
                if not ("2 consecutive" in b_content or "two consecutive" in b_content):
                    errors.append(ValidationError(bugs_skill, "TRACK10_CIRCUIT_BREAKER_VIOLATION", "skills/diagnosing-bugs/SKILL.md missing 2-consecutive failure circuit breaker"))
                if "<shared_premise>" not in b_content or "<falsification_test>" not in b_content:
                    errors.append(ValidationError(bugs_skill, "TRACK10_CIRCUIT_BREAKER_VIOLATION", "skills/diagnosing-bugs/SKILL.md missing XML tags <shared_premise> or <falsification_test>"))
                if not ("third patch" in b_content.lower() or "3rd patch" in b_content.lower() or "third attempt" in b_content.lower()):
                    errors.append(ValidationError(bugs_skill, "TRACK10_CIRCUIT_BREAKER_VIOLATION", "skills/diagnosing-bugs/SKILL.md must prohibit third patch attempt before falsification"))
            except Exception as exc:
                errors.append(ValidationError(bugs_skill, "FILE_READ_ERROR", f"Could not read {bugs_skill.name}: {exc}"))

        bug_agent = root_dir / "agents" / "bug-diagnoser.md"
        if bug_agent.is_file():
            try:
                ba_content = bug_agent.read_text(encoding="utf-8")
                if "Attack the Premise" not in ba_content or "<shared_premise>" not in ba_content:
                    errors.append(ValidationError(bug_agent, "TRACK10_CIRCUIT_BREAKER_VIOLATION", "agents/bug-diagnoser.md missing Attack the Premise circuit breaker or <shared_premise> schema"))
            except Exception as exc:
                errors.append(ValidationError(bug_agent, "FILE_READ_ERROR", f"Could not read {bug_agent.name}: {exc}"))

        orch_rule = root_dir / "rules" / "subagent-orchestration.md"
        if orch_rule.is_file():
            try:
                orch_content = self.read_rule_content(orch_rule)
                if "Attack the Premise" not in orch_content:
                    errors.append(ValidationError(orch_rule, "TRACK10_CIRCUIT_BREAKER_VIOLATION", "rules/subagent-orchestration.md Trigger C must reference Attack the Premise"))
            except Exception as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        # --- Invariant 3: Clean Comment Discipline & Fowler Smell #13 ---
        ts_standards = root_dir / "rules" / "typescript-standards.md"
        if not ts_standards.is_file():
            errors.append(ValidationError(ts_standards, "TRACK10_CLEAN_COMMENT_VIOLATION", "Missing required rule 'rules/typescript-standards.md'"))
        else:
            try:
                ts_content = ts_standards.read_text(encoding="utf-8")
                if not re.search(r"##\s*6\.\s*Clean Comment Discipline", ts_content):
                    errors.append(ValidationError(ts_standards, "TRACK10_CLEAN_COMMENT_VIOLATION", "rules/typescript-standards.md missing Section 6 'Clean Comment Discipline'"))
                if "What-comment" not in ts_content or "Why-comment" not in ts_content:
                    errors.append(ValidationError(ts_standards, "TRACK10_CLEAN_COMMENT_VIOLATION", "rules/typescript-standards.md must contrast What-comments and Why-comments"))
                if not any(token in ts_content.lower() for token in ["syntax repetition", "repeat syntax", "syntax-repeating", "repeats syntax"]):
                    errors.append(ValidationError(ts_standards, "TRACK10_CLEAN_COMMENT_VIOLATION", "rules/typescript-standards.md must ban syntax-repeating comments"))
                if "// ❌" not in ts_content or "// ✅" not in ts_content:
                    errors.append(ValidationError(ts_standards, "TRACK10_CLEAN_COMMENT_VIOLATION", "rules/typescript-standards.md must provide bad vs good code examples with // ❌ and // ✅"))
            except Exception as exc:
                errors.append(ValidationError(ts_standards, "FILE_READ_ERROR", f"Could not read {ts_standards.name}: {exc}"))

        fowler_smells = root_dir / "skills" / "code-review" / "resources" / "fowler-smells.md"
        if not fowler_smells.is_file():
            errors.append(ValidationError(fowler_smells, "TRACK10_CLEAN_COMMENT_VIOLATION", "Missing required resource 'skills/code-review/resources/fowler-smells.md'"))
        else:
            try:
                fow_content = fowler_smells.read_text(encoding="utf-8")
                if not re.search(r"###\s*13\.\s*(Redundant\s*/\s*)?Obvious Comments", fow_content):
                    errors.append(ValidationError(fowler_smells, "TRACK10_CLEAN_COMMENT_VIOLATION", "fowler-smells.md must include Smell #13 'Obvious Comments'"))
            except Exception as exc:
                errors.append(ValidationError(fowler_smells, "FILE_READ_ERROR", f"Could not read {fowler_smells.name}: {exc}"))

        reviewer_agent = root_dir / "agents" / "code-reviewer.md"
        if reviewer_agent.is_file():
            try:
                rev_content = reviewer_agent.read_text(encoding="utf-8")
                if "Obvious Comments" not in rev_content and "Smell #13" not in rev_content:
                    errors.append(ValidationError(reviewer_agent, "TRACK10_CLEAN_COMMENT_VIOLATION", "agents/code-reviewer.md Axis A rubric must inspect diffs for Smell #13 (Obvious Comments)"))
            except Exception as exc:
                errors.append(ValidationError(reviewer_agent, "FILE_READ_ERROR", f"Could not read {reviewer_agent.name}: {exc}"))

        # --- Invariant 4: Subtract-Before-Add Step in Task Graph Slicing ---
        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "TRACK10_SUBTRACT_BEFORE_ADD_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            try:
                tt_content = to_tickets_skill.read_text(encoding="utf-8")
                if "Subtract-Before-Add" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "TRACK10_SUBTRACT_BEFORE_ADD_VIOLATION", "skills/to-tickets/SKILL.md missing 'Subtract-Before-Add' invariant"))
                if "[PRUNE]" not in tt_content and "[SUBTRACT]" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "TRACK10_SUBTRACT_BEFORE_ADD_VIOLATION", "skills/to-tickets/SKILL.md must specify [PRUNE] or [SUBTRACT] prefix"))
                if "dead code" not in tt_content.lower() and "unreferenced" not in tt_content.lower():
                    errors.append(ValidationError(to_tickets_skill, "TRACK10_SUBTRACT_BEFORE_ADD_VIOLATION", "skills/to-tickets/SKILL.md must mandate pruning dead code or unreferenced exports"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        if implement_skill.is_file():
            try:
                imp_content = self._read_skill_with_references(implement_skill)
                if "[PRUNE]" not in imp_content and "[SUBTRACT]" not in imp_content and "PRUNE" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK10_SUBTRACT_BEFORE_ADD_VIOLATION", "skills/implement/SKILL.md Step 1 must explicitly document [PRUNE] ticket verification"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        impl_spec_skill = root_dir / "skills" / "implement-spec" / "SKILL.md"
        if impl_spec_skill.is_file():
            try:
                is_content = impl_spec_skill.read_text(encoding="utf-8")
                if "[PRUNE]" not in is_content and "Subtract-Before-Add" not in is_content and "PRUNE" not in is_content:
                    errors.append(ValidationError(impl_spec_skill, "TRACK10_SUBTRACT_BEFORE_ADD_VIOLATION", "skills/implement-spec/SKILL.md Unified Slicing Protocol must reference [PRUNE] or Subtract-Before-Add"))
            except Exception as exc:
                errors.append(ValidationError(impl_spec_skill, "FILE_READ_ERROR", f"Could not read {impl_spec_skill.name}: {exc}"))

        # --- Invariant 5: Adversarial Interrogation Protocol in Design Swarms ---
        if orch_rule.is_file():
            try:
                orch_content = self.read_rule_content(orch_rule)
                if not ("Pass 1" in orch_content and "Pass 2" in orch_content):
                    errors.append(ValidationError(orch_rule, "TRACK10_ADVERSARIAL_INTERROGATION_VIOLATION", "rules/subagent-orchestration.md Trigger E must define Pass 1 and Pass 2"))
                if "<role_and_objective>" not in orch_content or "<context_pointers>" not in orch_content:
                    errors.append(ValidationError(orch_rule, "TRACK10_ADVERSARIAL_INTERROGATION_VIOLATION", "rules/subagent-orchestration.md Trigger E missing structured brief XML schema tags"))
                if "debate matrix" not in orch_content.lower():
                    errors.append(ValidationError(orch_rule, "TRACK10_ADVERSARIAL_INTERROGATION_VIOLATION", "rules/subagent-orchestration.md Trigger E must mandate compiling debate matrix before operator presentation"))
            except Exception as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        if agents_rule.is_file():
            try:
                a_content = agents_rule.read_text(encoding="utf-8")
                if "Adversarial Interrogation Protocol" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK10_ADVERSARIAL_INTERROGATION_VIOLATION", "rules/AGENTS.md must summarize Adversarial Interrogation Protocol in Trigger E"))
            except Exception as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        cb_design_skill = root_dir / "skills" / "codebase-design" / "SKILL.md"
        if cb_design_skill.is_file():
            try:
                cbd_content = cb_design_skill.read_text(encoding="utf-8")
                if "Adversarial Interrogation" not in cbd_content and "debate matrix" not in cbd_content.lower():
                    errors.append(ValidationError(cb_design_skill, "TRACK10_ADVERSARIAL_INTERROGATION_VIOLATION", "skills/codebase-design/SKILL.md must document Adversarial Interrogation Protocol"))
            except Exception as exc:
                errors.append(ValidationError(cb_design_skill, "FILE_READ_ERROR", f"Could not read {cb_design_skill.name}: {exc}"))

        design_agent = root_dir / "agents" / "design-explorer.md"
        if design_agent.is_file():
            try:
                da_content = design_agent.read_text(encoding="utf-8")
                if "Adversarial Interrogation" not in da_content and "adversarial cross-critique" not in da_content.lower():
                    errors.append(ValidationError(design_agent, "TRACK10_ADVERSARIAL_INTERROGATION_VIOLATION", "agents/design-explorer.md must document Pass 2 adversarial cross-critique"))
            except Exception as exc:
                errors.append(ValidationError(design_agent, "FILE_READ_ERROR", f"Could not read {design_agent.name}: {exc}"))

        return invariants_scanned, errors

    def validate_spec_engine_and_schemas(self, root_dir: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates the living specification engine, workflow schemas, and specification templates:
        1. schemas/workflow-schema.json schema structure, required fields, and phase requirements.
        2. skills/to-spec/resources/ and templates/ (templates/specs/) living and delta spec templates.
        3. scripts/verify-spec.sh executable permissions (0755), bash shebang, and strict mode.
        4. scripts/internal/verify_spec.py syntax integrity.
        """
        errors: list[ValidationError] = []
        components_scanned = 0

        # --- 1. Workflow Schema Validation ---
        schema_path = root_dir / "schemas" / "workflow-schema.json"
        components_scanned += 1
        if not schema_path.is_file():
            errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", "Missing required workflow schema 'schemas/workflow-schema.json'"))
        else:
            try:
                s_content = schema_path.read_text(encoding="utf-8")
                schema_data = json.loads(s_content)
                if not isinstance(schema_data, dict):
                    errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", "schemas/workflow-schema.json must be a JSON object"))
                else:
                    for req_key in ("$schema", "title", "phases"):
                        if req_key not in schema_data:
                            errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", f"schemas/workflow-schema.json missing required top-level key '{req_key}'"))
                    phases = schema_data.get("phases")
                    if not isinstance(phases, dict):
                        errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", "schemas/workflow-schema.json 'phases' must be a JSON object"))
                    else:
                        required_phases = ("inception", "shaping", "spec", "tickets", "implementation", "review", "pr")
                        for p in required_phases:
                            if p not in phases:
                                errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", f"schemas/workflow-schema.json missing required phase '{p}'"))
                            else:
                                p_data = phases[p]
                                if not isinstance(p_data, dict):
                                    errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", f"Phase '{p}' must be a JSON object"))
                                else:
                                    if "description" not in p_data:
                                        errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", f"Phase '{p}' missing 'description'"))
                                    if "prerequisites" not in p_data or not isinstance(p_data["prerequisites"], list):
                                        errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", f"Phase '{p}' missing 'prerequisites' list"))
                                    if "required_artifacts" not in p_data or not isinstance(p_data["required_artifacts"], list):
                                        errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", f"Phase '{p}' missing 'required_artifacts' list"))
                        spec_phase = phases.get("spec")
                        if isinstance(spec_phase, dict):
                            val_cmd = spec_phase.get("validation_command", "")
                            if "verify-spec.sh" not in val_cmd:
                                errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", "Phase 'spec' must include 'validation_command' referencing 'verify-spec.sh'"))
            except Exception as exc:
                errors.append(ValidationError(schema_path, "SPEC_SCHEMA_VIOLATION", f"Invalid JSON in schemas/workflow-schema.json: {exc}"))

        # --- 2. Specification Templates Validation ---
        living_template = root_dir / "skills" / "to-spec" / "resources" / "living-spec-template.md"
        delta_template = root_dir / "skills" / "to-spec" / "resources" / "delta-spec-template.md"
        spec_template = root_dir / "skills" / "to-spec" / "resources" / "spec-template.md"

        for tmpl_path, tmpl_type in [
            (living_template, "living"),
            (delta_template, "delta"),
            (spec_template, "legacy"),
        ]:
            components_scanned += 1
            if not tmpl_path.is_file():
                errors.append(ValidationError(tmpl_path, "SPEC_TEMPLATE_VIOLATION", f"Missing specification template '{tmpl_path.name}'"))
            else:
                try:
                    t_content = tmpl_path.read_text(encoding="utf-8")
                    if not t_content.strip():
                        errors.append(ValidationError(tmpl_path, "SPEC_TEMPLATE_VIOLATION", f"Specification template '{tmpl_path.name}' is empty"))
                    elif tmpl_type == "living":
                        if "Domain:" not in t_content or "## Requirements" not in t_content or "Scenario:" not in t_content:
                            errors.append(ValidationError(tmpl_path, "SPEC_TEMPLATE_VIOLATION", f"'{tmpl_path.name}' missing living spec headers or scenario definitions"))
                    elif tmpl_type == "delta":
                        if "Target Domain:" not in t_content or "## ADDED Requirements" not in t_content or "## MODIFIED Requirements" not in t_content or "## REMOVED Requirements" not in t_content:
                            errors.append(ValidationError(tmpl_path, "SPEC_TEMPLATE_VIOLATION", f"'{tmpl_path.name}' missing delta spec headers (ADDED, MODIFIED, REMOVED)"))
                except Exception as exc:
                    errors.append(ValidationError(tmpl_path, "FILE_READ_ERROR", f"Could not read {tmpl_path.name}: {exc}"))

        # Also validate templates/specs/ if present
        templates_specs_dir = root_dir / "templates" / "specs"
        if templates_specs_dir.is_dir():
            for t_file in sorted(templates_specs_dir.glob("*.md")):
                components_scanned += 1
                try:
                    content = t_file.read_text(encoding="utf-8")
                    if not content.strip():
                        errors.append(ValidationError(t_file, "SPEC_TEMPLATE_VIOLATION", f"Template '{t_file.name}' in templates/specs/ is empty"))
                except Exception as exc:
                    errors.append(ValidationError(t_file, "FILE_READ_ERROR", f"Could not read {t_file.name}: {exc}"))

        # --- 3. Executable Script Validation: scripts/verify-spec.sh ---
        verify_script = root_dir / "scripts" / "verify-spec.sh"
        components_scanned += 1
        if not verify_script.is_file():
            errors.append(ValidationError(verify_script, "SPEC_SCRIPT_VIOLATION", "Missing runtime script 'scripts/verify-spec.sh'"))
        else:
            st_mode = verify_script.stat().st_mode
            if not (st_mode & 0o111):
                errors.append(ValidationError(verify_script, "SPEC_SCRIPT_VIOLATION", "scripts/verify-spec.sh is not marked executable (0755 expected)"))
            try:
                s_lines = verify_script.read_text(encoding="utf-8").splitlines()
                if not s_lines or not s_lines[0].startswith(("#!/usr/bin/env bash", "#!/bin/bash")):
                    errors.append(ValidationError(verify_script, "SPEC_SCRIPT_VIOLATION", "scripts/verify-spec.sh missing '#!/usr/bin/env bash' or '#!/bin/bash' shebang"))
                full_script = "\n".join(s_lines)
                if not re.search(r"set\s+-[^\n]*e", full_script):
                    errors.append(ValidationError(verify_script, "SPEC_SCRIPT_VIOLATION", "scripts/verify-spec.sh missing strict error handling (set -euo pipefail)"))
                if "verify_spec.py" not in full_script:
                    errors.append(ValidationError(verify_script, "SPEC_SCRIPT_VIOLATION", "scripts/verify-spec.sh must invoke 'verify_spec.py'"))
            except Exception as exc:
                errors.append(ValidationError(verify_script, "FILE_READ_ERROR", f"Could not read verify-spec.sh: {exc}"))

        # --- 4. Python Engine Validation: scripts/internal/verify_spec.py ---
        engine_script = root_dir / "scripts" / "internal" / "verify_spec.py"
        components_scanned += 1
        if not engine_script.is_file():
            errors.append(ValidationError(engine_script, "SPEC_SCRIPT_VIOLATION", "Missing engine script 'scripts/internal/verify_spec.py'"))
        else:
            try:
                e_content = engine_script.read_text(encoding="utf-8")
                compile(e_content, str(engine_script), "exec")
            except Exception as exc:
                errors.append(ValidationError(engine_script, "SPEC_SCRIPT_VIOLATION", f"Syntax error in scripts/internal/verify_spec.py: {exc}"))

        return components_scanned, errors

    def validate_research_engine(self, root_dir: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates the mechanical research note validator CLI and internal engine ([CLI-02]):
        1. scripts/verify-research.sh executable permissions (0755), bash shebang, strict mode, and invocation of verify_research.py.
        2. scripts/internal/verify_research.py syntax integrity and self-test execution.
        """
        errors: list[ValidationError] = []
        components_scanned = 0

        # --- 1. Executable CLI Script Validation: scripts/verify-research.sh ---
        verify_script = root_dir / "scripts" / "verify-research.sh"
        components_scanned += 1
        if not verify_script.is_file():
            errors.append(ValidationError(verify_script, "RESEARCH_SCRIPT_VIOLATION", "Missing CLI wrapper 'scripts/verify-research.sh'"))
        else:
            st_mode = verify_script.stat().st_mode
            if not (st_mode & 0o111):
                errors.append(ValidationError(verify_script, "RESEARCH_SCRIPT_VIOLATION", "scripts/verify-research.sh is not marked executable (+x)"))
            mode = st_mode & 0o777
            if mode != 0o755:
                errors.append(ValidationError(verify_script, "RESEARCH_SCRIPT_VIOLATION", f"scripts/verify-research.sh must have 0755 permissions, got {oct(mode)}"))
            try:
                s_lines = verify_script.read_text(encoding="utf-8").splitlines()
                if not s_lines or not s_lines[0].startswith(("#!/usr/bin/env bash", "#!/bin/bash")):
                    errors.append(ValidationError(verify_script, "RESEARCH_SCRIPT_VIOLATION", "scripts/verify-research.sh missing '#!/usr/bin/env bash' shebang"))
                full_script = "\n".join(s_lines)
                if not re.search(r"set\s+-[^\n]*e", full_script):
                    errors.append(ValidationError(verify_script, "RESEARCH_SCRIPT_VIOLATION", "scripts/verify-research.sh missing strict error handling (set -euo pipefail)"))
                if "verify_research.py" not in full_script:
                    errors.append(ValidationError(verify_script, "RESEARCH_SCRIPT_VIOLATION", "scripts/verify-research.sh must invoke 'verify_research.py'"))
            except Exception as exc:
                errors.append(ValidationError(verify_script, "FILE_READ_ERROR", f"Could not read verify-research.sh: {exc}"))

        # --- 2. Python Engine Validation: scripts/internal/verify_research.py ---
        engine_script = root_dir / "scripts" / "internal" / "verify_research.py"
        components_scanned += 1
        if not engine_script.is_file():
            errors.append(ValidationError(engine_script, "RESEARCH_SCRIPT_VIOLATION", "Missing engine script 'scripts/internal/verify_research.py'"))
        else:
            try:
                e_content = engine_script.read_text(encoding="utf-8")
                compile(e_content, str(engine_script), "exec")
            except Exception as exc:
                errors.append(ValidationError(engine_script, "RESEARCH_SCRIPT_VIOLATION", f"Syntax error in scripts/internal/verify_research.py: {exc}"))

        # --- 3. Self-Test Execution ---
        components_scanned += 1
        try:
            if str(root_dir) not in sys.path:
                sys.path.insert(0, str(root_dir))
            internal_dir = root_dir / "scripts" / "internal"
            if str(internal_dir) not in sys.path:
                sys.path.insert(0, str(internal_dir))
            try:
                from scripts.internal.verify_research import run_self_tests
            except ImportError:
                from verify_research import run_self_tests  # type: ignore[import-not-found,no-redef]
            if not run_self_tests():
                errors.append(ValidationError(engine_script, "RESEARCH_SELFTEST_FAILURE", "scripts/internal/verify_research.py self-test returned False"))
        except Exception as exc:
            errors.append(ValidationError(engine_script, "RESEARCH_SELFTEST_FAILURE", f"Exception during research validator self-test: {exc}"))

        return components_scanned, errors

    def validate_track_12_invariants(self, root_dir: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 12 Living Specifications & Anti-Scenario-Loss invariants:
        1. Section 8 Anti-Scenario-Loss Invariant in rules/gemini-behavior-guardrails.md
        2. Tier 3 Living System Specifications in rules/architectural-principles.md
        3. Master Composer integration in rules/AGENTS.md & docs/agents/domain.md
        4. Upstream /to-spec Dual-Mode & /to-tickets Delta Slicing Rule
        5. Downstream /implement Scenario Binding, /code-review Tripartite Spec Audit & /pr Pre-PR Reconciliation
        """
        errors: list[ValidationError] = []
        invariants_scanned = 5

        # --- Invariant 1: Anti-Scenario-Loss Invariant in rules/gemini-behavior-guardrails.md ---
        guardrails_path = root_dir / "rules" / "gemini-behavior-guardrails.md"
        if not guardrails_path.is_file():
            errors.append(ValidationError(guardrails_path, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "Missing required rule 'rules/gemini-behavior-guardrails.md'"))
        else:
            try:
                g_content = guardrails_path.read_text(encoding="utf-8")
                if not re.search(r"(?m)^##\s+8\.\s+Anti-Scenario-Loss Invariant", g_content):
                    errors.append(ValidationError(guardrails_path, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "rules/gemini-behavior-guardrails.md missing Section 8 'Anti-Scenario-Loss Invariant'"))
                if "Scenario Omission Trap" not in g_content:
                    errors.append(ValidationError(guardrails_path, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "rules/gemini-behavior-guardrails.md must document 'Scenario Omission Trap'"))
                if "Additive Scenario Discipline" not in g_content and "additive merging" not in g_content.lower():
                    errors.append(ValidationError(guardrails_path, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "rules/gemini-behavior-guardrails.md must enforce Additive Scenario Discipline"))
                if "## MODIFIED Requirements" not in g_content:
                    errors.append(ValidationError(guardrails_path, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "rules/gemini-behavior-guardrails.md must reference '## MODIFIED Requirements'"))
                if "## REMOVED Requirements" not in g_content:
                    errors.append(ValidationError(guardrails_path, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "rules/gemini-behavior-guardrails.md must reference '## REMOVED Requirements'"))
                if "verify-spec.sh --reconcile" not in g_content and "SpecReconciler" not in g_content:
                    errors.append(ValidationError(guardrails_path, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "rules/gemini-behavior-guardrails.md must reference reconciliation command"))
            except Exception as exc:
                errors.append(ValidationError(guardrails_path, "FILE_READ_ERROR", f"Could not read {guardrails_path.name}: {exc}"))

        # --- Invariant 2: Tier 3 Living Specs in rules/architectural-principles.md ---
        principles_path = root_dir / "rules" / "architectural-principles.md"
        if not principles_path.is_file():
            errors.append(ValidationError(principles_path, "TRACK12_TIER3_RULES_VIOLATION", "Missing required rule 'rules/architectural-principles.md'"))
        else:
            try:
                p_content = principles_path.read_text(encoding="utf-8")
                if not re.search(r"Tier 3:\s*Living (System )?Specifications", p_content):
                    errors.append(ValidationError(principles_path, "TRACK12_TIER3_RULES_VIOLATION", "rules/architectural-principles.md missing Section 3 'Tier 3: Living System Specifications'"))
                if "docs/specs/<domain>/spec.md" not in p_content and "docs/specs/" not in p_content:
                    errors.append(ValidationError(principles_path, "TRACK12_TIER3_RULES_VIOLATION", "rules/architectural-principles.md must reference 'docs/specs/<domain>/spec.md'"))
                if "permanent behavioral contracts" not in p_content.lower():
                    errors.append(ValidationError(principles_path, "TRACK12_TIER3_RULES_VIOLATION", "rules/architectural-principles.md must define living specs as permanent behavioral contracts"))
                if "Delta Specification" not in p_content and "# Spec Delta:" not in p_content:
                    errors.append(ValidationError(principles_path, "TRACK12_TIER3_RULES_VIOLATION", "rules/architectural-principles.md must mandate Delta Specifications"))
            except Exception as exc:
                errors.append(ValidationError(principles_path, "FILE_READ_ERROR", f"Could not read {principles_path.name}: {exc}"))

        # --- Invariant 3: Master Composer & Domain Docs Integration ---
        agents_rule = root_dir / "rules" / "AGENTS.md"
        if agents_rule.is_file():
            try:
                a_content = agents_rule.read_text(encoding="utf-8")
                if "Anti-Scenario-Loss" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "rules/AGENTS.md must summarize Anti-Scenario-Loss Invariant"))
                if "3-tier knowledge hierarchy" not in a_content.lower() and "Living Specs" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK12_TIER3_RULES_VIOLATION", "rules/AGENTS.md must summarize 3-tier knowledge hierarchy"))
            except Exception as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        domain_doc = root_dir / "docs" / "agents" / "domain.md"
        if domain_doc.is_file():
            try:
                d_content = domain_doc.read_text(encoding="utf-8")
                if "Tier 3" not in d_content or "docs/specs/" not in d_content:
                    errors.append(ValidationError(domain_doc, "TRACK12_TIER3_RULES_VIOLATION", "docs/agents/domain.md must document Tier 3 Living Specs"))
            except Exception as exc:
                errors.append(ValidationError(domain_doc, "FILE_READ_ERROR", f"Could not read {domain_doc.name}: {exc}"))

        # --- Invariant 4: Upstream /to-spec & /to-tickets Slicing ---
        to_spec_skill = root_dir / "skills" / "to-spec" / "SKILL.md"
        if to_spec_skill.is_file():
            try:
                ts_content = to_spec_skill.read_text(encoding="utf-8")
                if "Greenfield" not in ts_content or "Brownfield Delta" not in ts_content:
                    errors.append(ValidationError(to_spec_skill, "TRACK12_TIER3_RULES_VIOLATION", "skills/to-spec/SKILL.md missing Dual-Mode detection"))
                if "verify-spec.sh" not in ts_content:
                    errors.append(ValidationError(to_spec_skill, "TRACK12_TIER3_RULES_VIOLATION", "skills/to-spec/SKILL.md missing verify-spec.sh command reference"))
            except Exception as exc:
                errors.append(ValidationError(to_spec_skill, "FILE_READ_ERROR", f"Could not read {to_spec_skill.name}: {exc}"))

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if to_tickets_skill.is_file():
            try:
                tt_content = to_tickets_skill.read_text(encoding="utf-8")
                if "Delta Slicing Rule" not in tt_content and "Ticket 01 `[PRUNE]`" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "TRACK12_TIER3_RULES_VIOLATION", "skills/to-tickets/SKILL.md missing Delta Slicing Rule or Ticket 01 [PRUNE]"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        # --- Invariant 5: Downstream /implement, /code-review & /pr ---
        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if implement_skill.is_file():
            try:
                imp_content = self._read_skill_with_references(implement_skill)
                if "Given/When/Then Scenario Binding" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "skills/implement/SKILL.md missing Given/When/Then Scenario Binding"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        reviewer_agent = root_dir / "agents" / "code-reviewer.md"
        if reviewer_agent.is_file():
            try:
                ra_content = reviewer_agent.read_text(encoding="utf-8")
                if "Tripartite" not in ra_content and "MODIFIED Behaviors Audit" not in ra_content:
                    errors.append(ValidationError(reviewer_agent, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "agents/code-reviewer.md missing Tripartite Spec Conformance audit"))
            except Exception as exc:
                errors.append(ValidationError(reviewer_agent, "FILE_READ_ERROR", f"Could not read {reviewer_agent.name}: {exc}"))

        pr_skill = root_dir / "skills" / "pr" / "SKILL.md"
        if pr_skill.is_file():
            try:
                pr_content = pr_skill.read_text(encoding="utf-8")
                if "verify-spec.sh --reconcile" not in pr_content:
                    errors.append(ValidationError(pr_skill, "TRACK12_ANTI_SCENARIO_LOSS_VIOLATION", "skills/pr/SKILL.md missing verify-spec.sh --reconcile command"))
            except Exception as exc:
                errors.append(ValidationError(pr_skill, "FILE_READ_ERROR", f"Could not read {pr_skill.name}: {exc}"))

        return invariants_scanned, errors

    def validate_track_13_invariants(self, root_dir: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 13 Single-Ticket Micro-Swarm invariants:
        1. Sub-Trigger A2 & Orchestrator Rules in rules/subagent-orchestration.md
        2. Two-Stage Micro-Swarm Runbook & Pipeline in skills/implement/SKILL.md
        3. Skeptical Reporting Protocol & Circuit Breaker in skills/implement/SKILL.md
        4. Double-Verification Gate, Milestone Stream & Red Flags in skills/implement/SKILL.md
        5. Master Composer Integration in rules/AGENTS.md
        """
        errors: list[ValidationError] = []
        invariants_scanned = 5

        # --- Invariant 1: Sub-Trigger A2 in rules/subagent-orchestration.md ---
        orch_rule = root_dir / "rules" / "subagent-orchestration.md"
        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            try:
                o_content = self.read_rule_content(orch_rule)
                if not re.search(r"(?m)^#{2,4}\s+Sub-Trigger A2:\s+Single-Ticket Two-Stage Micro-Swarm", o_content):
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing Sub-Trigger A2: Single-Ticket Two-Stage Micro-Swarm"))
                if "Pipeline Coordinator" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing Pipeline Coordinator persona"))
                if 'Workspace: "inherit"' not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md Sub-Trigger A2 must use Workspace: 'inherit'"))
                if "spec-implementer" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md Sub-Trigger A2 must dispatch spec-implementer"))
                if "> [!WARNING] **Skepticism Disclaimer**" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing Skepticism Disclaimer callout"))
                if "3-Tier Verification Record" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing 3-Tier Verification Record"))
                if "2 consecutive" not in o_content and "two consecutive" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing circuit breaker on 2 consecutive failing test cycles"))
                if "Active Improvement Worker" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing Active Improvement Worker in Stage 2"))
                if "break the prior attempt" not in o_content.lower():
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing mandate to break the prior attempt"))
                if "Double-Verification Gate" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing Double-Verification Gate"))
                if "Milestone Notification Stream" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing Milestone Notification Stream"))
                if "Single-Notification Stream Invariant" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing Single-Notification Stream Invariant"))
                if "--direct" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK13_TRIGGER_A2_VIOLATION", "rules/subagent-orchestration.md missing --direct escape hatch"))
            except Exception as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        # --- Invariants 2, 3, 4: Two-Stage Micro-Swarm in skills/implement/SKILL.md ---
        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "TRACK13_RUNBOOK_PIPELINE_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
            errors.append(ValidationError(implement_skill, "TRACK13_SKEPTICAL_REPORTING_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
            errors.append(ValidationError(implement_skill, "TRACK13_DOUBLE_VERIFICATION_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
        else:
            try:
                imp_content = self._read_skill_with_references(implement_skill)

                # Invariant 2: Runbook & Pipeline
                if "Pipeline Coordinator" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_RUNBOOK_PIPELINE_VIOLATION", "skills/implement/SKILL.md missing Pipeline Coordinator persona transition"))
                if "Two-Stage Micro-Swarm" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_RUNBOOK_PIPELINE_VIOLATION", "skills/implement/SKILL.md missing Two-Stage Micro-Swarm pipeline"))
                if 'Workspace: "inherit"' not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_RUNBOOK_PIPELINE_VIOLATION", "skills/implement/SKILL.md must specify Workspace: 'inherit'"))
                if "spec-implementer" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_RUNBOOK_PIPELINE_VIOLATION", "skills/implement/SKILL.md must dispatch spec-implementer builder"))
                if "Active Improvement Worker" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_RUNBOOK_PIPELINE_VIOLATION", "skills/implement/SKILL.md missing Active Improvement Worker dispatch"))
                if "<prior_attempt>" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_RUNBOOK_PIPELINE_VIOLATION", "skills/implement/SKILL.md prompt schema missing <prior_attempt> tag"))
                if "--direct" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_RUNBOOK_PIPELINE_VIOLATION", "skills/implement/SKILL.md missing --direct escape hatch"))

                # Invariant 3: Skeptical Reporting Protocol & Circuit Breaker
                if "> [!WARNING] **Skepticism Disclaimer**" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_SKEPTICAL_REPORTING_VIOLATION", "skills/implement/SKILL.md missing Skepticism Disclaimer template"))
                if "3-Tier Verification Record" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_SKEPTICAL_REPORTING_VIOLATION", "skills/implement/SKILL.md missing 3-Tier Verification Record in builder report"))
                if "Deep Verification" not in imp_content or "Shallow Verification" not in imp_content or "Unverified" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_SKEPTICAL_REPORTING_VIOLATION", "skills/implement/SKILL.md missing 3-tier categories (Deep, Shallow, Unverified)"))
                if "2 consecutive" not in imp_content and "two consecutive" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_SKEPTICAL_REPORTING_VIOLATION", "skills/implement/SKILL.md missing 2-consecutive failure circuit breaker"))
                if "Attack the Premise" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_SKEPTICAL_REPORTING_VIOLATION", "skills/implement/SKILL.md missing Attack the Premise circuit breaker reference"))

                # Invariant 4: Double-Verification Gate, Milestone Stream & Red Flags
                if "Double-Verification Gate" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_DOUBLE_VERIFICATION_VIOLATION", "skills/implement/SKILL.md missing Double-Verification Gate"))
                if "./scripts/verify-build.sh" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_DOUBLE_VERIFICATION_VIOLATION", "skills/implement/SKILL.md must reference physical command ./scripts/verify-build.sh"))
                if "Anti-False-Green Invariant" not in imp_content and "Prohibition of Premature Victory" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_DOUBLE_VERIFICATION_VIOLATION", "skills/implement/SKILL.md missing Anti-False-Green Invariant"))
                if "Milestone 1" not in imp_content or "Milestone 2" not in imp_content or "Milestone 3" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_DOUBLE_VERIFICATION_VIOLATION", "skills/implement/SKILL.md missing Milestone 1, 2, or 3 notifications"))
                if "Single-Notification Stream Invariant" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_DOUBLE_VERIFICATION_VIOLATION", "skills/implement/SKILL.md missing Single-Notification Stream Invariant"))
                if "Common Rationalizations" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_DOUBLE_VERIFICATION_VIOLATION", "skills/implement/SKILL.md missing Common Rationalizations section"))
                if "Red Flags" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK13_DOUBLE_VERIFICATION_VIOLATION", "skills/implement/SKILL.md missing Red Flags section"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        # --- Invariant 5: Master Composer Integration in rules/AGENTS.md ---
        agents_rule = root_dir / "rules" / "AGENTS.md"
        if not agents_rule.is_file():
            errors.append(ValidationError(agents_rule, "TRACK13_MASTER_COMPOSER_VIOLATION", "Missing required rule 'rules/AGENTS.md'"))
        else:
            try:
                a_content = agents_rule.read_text(encoding="utf-8")
                if "Sub-Trigger A2" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK13_MASTER_COMPOSER_VIOLATION", "rules/AGENTS.md must summarize Sub-Trigger A2"))
                if "Pipeline Coordinator" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK13_MASTER_COMPOSER_VIOLATION", "rules/AGENTS.md must summarize Pipeline Coordinator Invariant"))
                if "Skeptical Reporting Protocol" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK13_MASTER_COMPOSER_VIOLATION", "rules/AGENTS.md must summarize Skeptical Reporting Protocol"))
                if "Active Improvement Worker" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK13_MASTER_COMPOSER_VIOLATION", "rules/AGENTS.md must summarize Active Improvement Worker mandate"))
                if "Double-Verification Gate" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK13_MASTER_COMPOSER_VIOLATION", "rules/AGENTS.md must summarize Double-Verification Gate"))
                if "Single-Notification Stream Invariant" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK13_MASTER_COMPOSER_VIOLATION", "rules/AGENTS.md must summarize Single-Notification Stream Invariant"))
                if "--direct" not in a_content:
                    errors.append(ValidationError(agents_rule, "TRACK13_MASTER_COMPOSER_VIOLATION", "rules/AGENTS.md must summarize --direct escape hatch"))
            except Exception as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        return invariants_scanned, errors

    def inspect_spec_context(
        self, context_ref: str, spec_root: Path | None = None, base_dir: Path | None = None
    ) -> tuple[Path | None, str | None]:
        """
        [ING-02] Inspects referenced spec section lines via read_file semantics.
        Resolves spec file location against spec_root, ticket directory, or repository root.
        Extracts line ranges (e.g. #L10-L25 or #L42) if specified.
        Falls back gracefully returning (None, None) if the spec file cannot be located.
        """
        if not context_ref or not isinstance(context_ref, str):
            return None, None

        spec_file_str, _, line_range = context_ref.partition("#")
        spec_file_str = spec_file_str.strip()
        if not spec_file_str:
            return None, None

        candidates: list[Path] = []
        allowed_roots: list[Path] = []
        if spec_root:
            allowed_roots.append(spec_root.resolve())
            candidates.append(spec_root / spec_file_str)
        if base_dir:
            allowed_roots.append(base_dir.resolve())
            allowed_roots.append(base_dir.parent.resolve())
            allowed_roots.append(base_dir.parent.parent.resolve())
            candidates.append(base_dir / spec_file_str)
            candidates.append(base_dir.parent / spec_file_str)
            candidates.append(base_dir.parent.parent / spec_file_str)
        allowed_roots.append(Path.cwd().resolve())

        if Path(spec_file_str).is_absolute():
            cand_abs = Path(spec_file_str).resolve()
            if not any(cand_abs == root or cand_abs.is_relative_to(root) for root in allowed_roots):
                return None, None
            candidates.append(cand_abs)
        else:
            candidates.append(Path.cwd() / spec_file_str)

        spec_path: Path | None = None
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if resolved.is_file() and any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
                    spec_path = resolved
                    break
            except Exception:
                continue

        if not spec_path:
            return None, None

        try:
            content_str = spec_path.read_text(encoding="utf-8")
        except Exception:
            return spec_path, None

        if not line_range:
            return spec_path, content_str

        m = re.match(r"^L?(\d+)(?:-L?(\d+))?$", line_range.strip())
        if not m:
            return spec_path, None

        start_line = int(m.group(1))
        end_line = int(m.group(2)) if m.group(2) else start_line
        lines = content_str.splitlines()
        selected = lines[max(0, start_line - 1):end_line]
        return spec_path, "\n".join(selected)

    def _detect_active_branch(self, target_dir: Path) -> str:
        """Inspects .git/HEAD or worktree .git pointer to detect current branch name without subprocess overhead."""
        try:
            curr = target_dir if target_dir.is_dir() else target_dir.parent
            for _ in range(5):
                git_entry = curr / ".git"
                if git_entry.is_file():
                    content = git_entry.read_text(encoding="utf-8").strip()
                    if content.startswith("gitdir:"):
                        raw_gitdir = content[7:].strip()
                        gitdir_path = Path(raw_gitdir)
                        if not gitdir_path.is_absolute():
                            gitdir_path = (curr / gitdir_path).resolve()
                        head_file = gitdir_path / "HEAD"
                        if head_file.is_file():
                            head_content = head_file.read_text(encoding="utf-8").strip()
                            if head_content.startswith("ref: refs/heads/"):
                                return head_content[16:].strip()
                            return head_content
                elif git_entry.is_dir():
                    head_file = git_entry / "HEAD"
                    if head_file.is_file():
                        head_content = head_file.read_text(encoding="utf-8").strip()
                        if head_content.startswith("ref: refs/heads/"):
                            return head_content[16:].strip()
                        return head_content
                if curr.parent == curr:
                    break
                curr = curr.parent
        except Exception:
            pass
        return ""

    def resolve_ticket_shorthand(self, issues_dir: Path, ticket_ref: str | int) -> Path:
        """
        [ING-01] Resolves a ticket shorthand (1-, 2-, or 3-digit number, '#NN', 'ticket-NN', or slug)
        against an issues directory, a feature directory, .scratch/, or project root.
        Autonomously locates .scratch/<feature-slug>/issues/<NN>-*.md without operator prompting.
        Disambiguates between multiple feature trackers via active git branch.
        """
        if not issues_dir.exists():
            raise FileNotFoundError(f"Issues directory does not exist: {issues_dir}")

        if issues_dir.is_file():
            return issues_dir

        raw_str = str(ticket_ref).strip()
        allowed_roots = [
            issues_dir.resolve(),
            issues_dir.parent.resolve(),
            Path.cwd().resolve(),
        ]

        def _is_safe(p: Path) -> bool:
            try:
                resolved = p.resolve()
                return any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots)
            except Exception:
                return False

        direct_path = issues_dir / raw_str
        if direct_path.is_file() and _is_safe(direct_path):
            return direct_path.resolve()

        clean_ref = raw_str
        if clean_ref.startswith("#"):
            clean_ref = clean_ref[1:].strip()
        elif clean_ref.lower().startswith("ticket-"):
            clean_ref = clean_ref[7:].strip()
        elif clean_ref.lower().startswith("issue-"):
            clean_ref = clean_ref[6:].strip()

        if (issues_dir / clean_ref).is_file() and _is_safe(issues_dir / clean_ref):
            return (issues_dir / clean_ref).resolve()

        search_dirs: list[Path] = []
        if issues_dir.name == "issues":
            search_dirs.append(issues_dir)
        else:
            if (issues_dir / "issues").is_dir():
                search_dirs.append(issues_dir / "issues")
            if (issues_dir / ".scratch").is_dir():
                search_dirs.extend([p for p in sorted((issues_dir / ".scratch").glob("*/issues")) if p.is_dir()])
            search_dirs.extend([p for p in sorted(issues_dir.glob("*/issues")) if p.is_dir()])
            if not search_dirs:
                search_dirs.append(issues_dir)

        all_matches: list[Path] = []
        seen_matches: set[Path] = set()

        for sdir in search_dirs:
            if clean_ref.isdigit():
                num_val = int(clean_ref)
                pats = [
                    f"{num_val:02d}-*.md",
                    f"{num_val:03d}-*.md",
                    f"{num_val}-*.md",
                    f"{num_val:02d}.md",
                    f"{num_val:03d}.md",
                    f"{num_val}.md",
                    f"ticket-{num_val:02d}*.md",
                    f"ticket-{num_val:03d}*.md",
                    f"ticket-{num_val}*.md",
                    f"issue-{num_val:02d}*.md",
                    f"issue-{num_val:03d}*.md",
                    f"issue-{num_val}*.md",
                ]
                for pat in pats:
                    for match in sorted(sdir.glob(pat)):
                        if match.is_file() and _is_safe(match) and match not in seen_matches:
                            seen_matches.add(match)
                            all_matches.append(match.resolve())
            else:
                base = clean_ref[:-3] if clean_ref.endswith(".md") else clean_ref
                pats = [f"{clean_ref}", f"{base}*.md"]
                for pat in pats:
                    for match in sorted(sdir.glob(pat)):
                        if match.is_file() and _is_safe(match) and match not in seen_matches:
                            seen_matches.add(match)
                            all_matches.append(match.resolve())

        if not all_matches:
            raise FileNotFoundError(f"No ticket file found matching shorthand '{ticket_ref}' in {issues_dir}")

        first_parent = all_matches[0].parent
        if all(m.parent == first_parent for m in all_matches):
            return all_matches[0]

        active_branch = self._detect_active_branch(issues_dir)
        if active_branch:
            branch_filtered = [m for m in all_matches if m.parent.parent.name in active_branch]
            if len(branch_filtered) == 1:
                return branch_filtered[0]
            if branch_filtered and all(m.parent == branch_filtered[0].parent for m in branch_filtered):
                return branch_filtered[0]

        paths_str = [str(m) for m in all_matches]
        raise ValueError(
            f"Ambiguous ticket shorthand '{ticket_ref}': found matching tickets across multiple feature directories: {paths_str}. "
            "Please specify the full path or feature slug."
        )

    def _parse_ticket_markdown(self, raw_content: str, default_title: str = "ticket") -> dict[str, str]:
        content = raw_content.replace("\r\n", "\n")
        meta: dict[str, str] = {}

        title_match = re.search(r"^#+\s+(.+)$", content, re.MULTILINE)
        if title_match:
            meta["title"] = title_match.group(1).strip()
        else:
            meta["title"] = default_title

        canonical_map = {
            "context": "Context",
            "seam": "Seam",
            "verification": "Verification",
            "guardrails": "Guardrails",
            "status": "Status",
            "blocked by": "Blocked by",
            "blocked_by": "Blocked by",
            "verified commit": "Verified Commit",
            "verified commit sha": "Verified Commit",
            "commit sha": "Verified Commit",
            "commit": "Verified Commit",
        }

        if content.strip().startswith("---"):
            try:
                fm_data = parse_frontmatter(content.strip())
                for k, v in fm_data.items():
                    k_str = str(k).strip()
                    canon_key = canonical_map.get(k_str.lower(), k_str)
                    v_str = str(v).strip() if not isinstance(v, list) else ", ".join(str(x) for x in v)
                    meta[canon_key] = v_str
                    meta[k_str] = v_str
                title_val = fm_data.get("title") or fm_data.get("Title")
                if title_val:
                    meta["title"] = str(title_val).strip()
            except Exception:
                pass

        lines = content.splitlines()
        idx = 0
        while idx < len(lines):
            line = lines[idx]
            match = re.match(r"^[ \t]*(?:[-*][ \t]+)?\*\*([A-Za-z0-9_\s-]+?)\s*(?::\*\*|\*\*\s*:)[ \t]*(.*?)$", line)
            if match:
                raw_key = match.group(1).strip()
                val = match.group(2).strip()
                val = re.sub(r"<!--.*?-->", "", val).strip()

                if not val:
                    continuation: list[str] = []
                    j = idx + 1
                    while j < len(lines):
                        next_line = lines[j]
                        if re.match(r"^[ \t]*(?:[-*][ \t]+)?\*\*", next_line) or re.match(r"^[ \t]*#", next_line):
                            break
                        stripped = next_line.strip()
                        if stripped.startswith("- ") or stripped.startswith("* ") or (continuation and stripped):
                            continuation.append(stripped)
                            j += 1
                        elif not stripped and not continuation:
                            j += 1
                        else:
                            break
                    if continuation:
                        val = "\n".join(continuation)
                        idx = j - 1

                canon_key = canonical_map.get(raw_key.lower(), raw_key)
                meta[canon_key] = val
                meta[raw_key] = val
            idx += 1

        ac_match = re.search(r"^##\s+Acceptance Criteria\s*\n(.*?)(?=\n##|\Z)", content, re.DOTALL | re.MULTILINE)
        if ac_match:
            ac_text = ac_match.group(1).strip()
            if ac_text:
                meta["Acceptance Criteria"] = ac_text

        return meta

    def extract_ticket_metadata(self, ticket_path: Path) -> dict[str, str]:
        """
        [ING-02] Extracts first-class execution metadata key-values directly from a ticket file:
        Context, Seam, Verification, Guardrails, Status, Blocked by, and Title.
        Normalizes CRLF, strips HTML comments, supports continuation bullet lines,
        and canonicalizes key casing. Raises ValueError if required execution keys are missing.
        """
        if not ticket_path.is_file():
            raise FileNotFoundError(f"Ticket file does not exist: {ticket_path}")

        meta = self._parse_ticket_markdown(ticket_path.read_text(encoding="utf-8"), ticket_path.stem)

        for req_key in ("Context", "Seam", "Verification", "Guardrails"):
            if req_key not in meta or not meta[req_key].strip():
                raise ValueError(
                    f"Ticket '{ticket_path.name}' is missing required metadata key: '**{req_key}:**'"
                )

        return meta

    def update_ticket_status(self, ticket_input: Path | str, new_status: str) -> str:
        """
        [LIFE-01] [LIFE-02] Updates the on-disk ticket status (e.g. 'ready-for-agent' -> 'in-progress' -> 'resolved').
        Supports a Path, file path string, or raw ticket markdown content string.
        Replaces '**Status:** <old>' or 'Status: <old>' while preserving all surrounding metadata,
        formatting, bullets, inline comments, and line endings (CRLF/LF).
        If a file path or Path is provided, writes the updated content back to disk.
        Returns the updated ticket markdown content string.
        """
        is_file_target = False
        target_path: Path | None = None
        raw_text = ""

        if isinstance(ticket_input, Path):
            is_file_target = True
            target_path = ticket_input
            if target_path.is_dir():
                raise IsADirectoryError(f"Ticket path is a directory, expected a file: {target_path}")
            if not target_path.is_file():
                raise FileNotFoundError(f"Ticket file does not exist: {target_path}")
            with target_path.open("r", encoding="utf-8", newline="") as f:
                raw_text = f.read()
        elif isinstance(ticket_input, str):
            if "\n" not in ticket_input and not ticket_input.strip().startswith("#"):
                p = Path(ticket_input)
                if p.is_dir():
                    raise IsADirectoryError(f"Ticket path is a directory, expected a file: {p}")
                if p.is_file():
                    is_file_target = True
                    target_path = p
                    with target_path.open("r", encoding="utf-8", newline="") as f:
                        raw_text = f.read()
                elif ticket_input.endswith((".md", ".markdown")) or "/" in ticket_input or "\\" in ticket_input:
                    raise FileNotFoundError(f"Ticket file does not exist: {p}")
                else:
                    raw_text = ticket_input
            else:
                raw_text = ticket_input

        # Regex replace Status field while preserving existing markup style, bullets, and line ending
        status_pattern = re.compile(
            r"^(?P<prefix>[ \t]*(?:[-*][ \t]+)?)(?P<key>\*{0,2}Status(?::\*{0,2}|\*{0,2}:))[ \t]*(?P<val_marker>[\*`\"]*)[ \t]*(?P<val>.*?)(?P<val_marker_post>[\*`\"]*)(?P<comment>[ \t]*<!--.*?-->)?[ \t]*(?P<cr>\r?)$",
            re.MULTILINE | re.IGNORECASE,
        )

        def _replace_status(m: re.Match) -> str:
            vm = m.group("val_marker") or ""
            vmp = m.group("val_marker_post") or ""
            if vm or vmp:
                clean_status = new_status.strip().strip("`*\"'")
                marker_close = vmp if vmp else vm
                formatted_status = f"{vm}{clean_status}{marker_close}"
            else:
                formatted_status = new_status.strip()
            comm = f" {m.group('comment').strip()}" if m.group("comment") else ""
            cr = m.group("cr") or ""
            return f"{m.group('prefix')}{m.group('key')} {formatted_status}{comm}{cr}"

        if status_pattern.search(raw_text):
            # Limit count=1 to avoid mutating subsequent mentions of status in ticket descriptions/examples
            updated_text = status_pattern.sub(_replace_status, raw_text, count=1)
        else:
            newline = "\r\n" if "\r\n" in raw_text else "\n"
            title_pattern = re.compile(r"^(#+\s+[^\r\n]+(?:\r?\n)+)", re.MULTILINE)
            if title_pattern.search(raw_text):
                updated_text = title_pattern.sub(rf"\g<1>**Status:** {new_status}{newline}{newline}", raw_text, count=1)
            else:
                updated_text = f"**Status:** {new_status}{newline}{newline}" + raw_text

        if is_file_target and target_path is not None:
            with target_path.open("w", encoding="utf-8", newline="") as f:
                f.write(updated_text)

        return updated_text

    def persist_session_anchor(
        self,
        tracker_dir_or_feature_dir: Path | str,
        conversation_id: str | None = None,
        feature_slug: str | None = None,
        active_phase: str | None = None,
        fix_loop_count: int | None = None,
        reviewed_commits: list[str] | None = None,
    ) -> Path:
        """
        [ANCH-01][STATE-01] Persists Command Center Session Anchor metadata (.scratch/<feature-slug>/session.json).
        Captures commandCenterConversationId, feature, createdAt (ISO-8601 UTC timestamp),
        activePhase, fixLoopCount, and reviewedCommits.
        Resolves from $ANTIGRAVITY_CONVERSATION_ID environment variable if conversation_id is omitted.
        """
        if tracker_dir_or_feature_dir is None or not str(tracker_dir_or_feature_dir).strip():
            raise ValueError("A non-empty tracker_dir_or_feature_dir is required to persist session anchor")

        raw_path = Path(tracker_dir_or_feature_dir)
        explicit_slug = (feature_slug or "").strip()

        if raw_path.name == "session.json":
            target_file = raw_path
            feature_dir = raw_path.parent
        else:
            curr = raw_path.parent if (raw_path.is_file() or raw_path.suffix in (".json", ".md")) else raw_path

            feature_dir = None
            if curr.name == "issues":
                feature_dir = curr.parent
            else:
                for ancestor in curr.parents:
                    if ancestor.name == "issues":
                        feature_dir = ancestor.parent
                        break

            if feature_dir is None:
                if curr.name == ".scratch":
                    if not explicit_slug:
                        raise ValueError(
                            "A non-empty feature_slug is required when tracker_dir_or_feature_dir points to root .scratch"
                        )
                    feature_dir = curr / explicit_slug
                elif curr.parent.name == ".scratch":
                    feature_dir = curr
                elif curr.name in ("", ".", "/"):
                    if not explicit_slug:
                        raise ValueError(
                            "A non-empty feature_slug is required when tracker_dir_or_feature_dir is empty or root"
                        )
                    scratch_sub = curr / ".scratch"
                    feature_dir = (scratch_sub / explicit_slug) if scratch_sub.is_dir() else (curr / explicit_slug)
                else:
                    feature_dir = curr

            target_file = feature_dir / "session.json"

        if target_file.is_dir():
            raise IsADirectoryError(f"Target session anchor path '{target_file}' is an existing directory")

        resolved_cid: str | None = None
        if conversation_id is not None:
            resolved_cid = conversation_id.strip() or os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
            if not resolved_cid:
                raise ValueError(
                    "A non-empty conversation_id or ANTIGRAVITY_CONVERSATION_ID environment variable is required to persist session anchor"
                )

        resolved_slug = explicit_slug or (feature_dir.name if feature_dir else "")
        if not resolved_slug or resolved_slug in (".scratch", ".", "/"):
            raise ValueError(
                "A non-empty feature_slug or valid feature directory is required to persist session anchor"
            )

        existing_data: dict[str, Any] = {}
        if target_file.is_file():
            try:
                existing_data = json.loads(target_file.read_text(encoding="utf-8"))
            except Exception:
                existing_data = {}

        created_at = existing_data.get("createdAt") or datetime.now(timezone.utc).isoformat()
        if active_phase is not None:
            if not isinstance(active_phase, str) or not active_phase.strip():
                raise ValueError("active_phase must be a non-empty string")
            resolved_phase = active_phase.strip()
        else:
            resolved_phase = existing_data.get("activePhase", "implementation")
            if not isinstance(resolved_phase, str) or not resolved_phase.strip():
                resolved_phase = "implementation"

        if fix_loop_count is not None:
            if not isinstance(fix_loop_count, int) or fix_loop_count < 0:
                raise ValueError("fix_loop_count must be a non-negative integer")
            resolved_fix_count = fix_loop_count
        else:
            raw_fc = existing_data.get("fixLoopCount", 0)
            resolved_fix_count = raw_fc if isinstance(raw_fc, int) and raw_fc >= 0 else 0

        if reviewed_commits is not None:
            if not isinstance(reviewed_commits, list):
                raise ValueError("reviewed_commits must be a list of commit SHAs")
            resolved_commits = [str(c).strip() for c in reviewed_commits if str(c).strip()]
        else:
            raw_rc = existing_data.get("reviewedCommits", [])
            resolved_commits = [str(c).strip() for c in raw_rc if str(c).strip()] if isinstance(raw_rc, list) else []

        data: dict[str, Any] = {
            "feature": resolved_slug,
            "createdAt": created_at,
            "activePhase": resolved_phase,
            "fixLoopCount": resolved_fix_count,
            "reviewedCommits": resolved_commits,
        }
        if resolved_cid is not None:
            data["commandCenterConversationId"] = resolved_cid

        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return target_file

    write_session_anchor = persist_session_anchor

    def _resolve_session_anchor_path(
        self,
        tracker_dir_or_feature_dir: Path | str | None,
        feature_slug: str | None = None,
    ) -> Path | None:
        """
        Resolves target session.json file path from directory, ticket file, issues dir, or feature slug.
        Returns the resolved Path (which may or may not exist on disk), or None if input is empty.
        """
        if tracker_dir_or_feature_dir is None or not str(tracker_dir_or_feature_dir).strip():
            return None

        raw_path = Path(tracker_dir_or_feature_dir)
        explicit_slug = (feature_slug or "").strip()

        if raw_path.name == "session.json" and raw_path.is_file():
            return raw_path

        candidates: list[Path] = []
        if explicit_slug:
            candidates.extend([
                raw_path / explicit_slug / "session.json",
                raw_path / ".scratch" / explicit_slug / "session.json",
            ])

        curr = raw_path.parent if (raw_path.is_file() or raw_path.suffix in (".json", ".md")) else raw_path

        if curr.name == "issues":
            candidates.append(curr.parent / "session.json")
        for ancestor in curr.parents:
            if ancestor.name == "issues":
                candidates.append(ancestor.parent / "session.json")
                break

        candidates.append(curr / "session.json")
        if curr.parent.name == ".scratch":
            candidates.append(curr / "session.json")

        if curr.name == ".scratch" and curr.is_dir():
            sub_anchors = sorted(curr.glob("*/session.json"))
            if len(sub_anchors) == 1:
                candidates.append(sub_anchors[0])

        for cand in candidates:
            if cand.is_file():
                return cand

        return (curr.parent / "session.json") if curr.name == "issues" else (curr / "session.json")

    @staticmethod
    def _resolve_feature_dir(target: Path | str, check_exists: bool = False) -> Path:
        if not target or not str(target).strip():
            raise ValueError("Target path must be a non-empty string or Path.")
        target_raw = str(target).strip()
        if ".." in Path(target_raw).parts:
            raise ValueError(f"Path traversal detected in target path: {target}")
        raw = Path(target_raw)
        if check_exists and not raw.exists():
            raise FileNotFoundError(f"Feature directory or ticket path not found: {target}")
        curr = raw.parent if (raw.is_file() or raw.suffix in (".json", ".md")) else raw
        if curr.name == "issues":
            return curr.parent
        for ancestor in curr.parents:
            if ancestor.name == "issues":
                return ancestor.parent
        return curr

    @staticmethod
    def _resolve_artifact_file(artifact_path: Path | str | None) -> Path | None:
        if not artifact_path or not str(artifact_path).strip():
            return None
        art_raw = str(artifact_path).strip()
        if ".." in Path(art_raw).parts:
            raise ValueError(f"Path traversal detected in artifact_path: {artifact_path}")
        art = Path(art_raw)
        if art.name.startswith(".") and not art.name.lower().endswith((".md", ".markdown")):
            raise ValueError(f"Artifact path cannot target dotfiles or hidden files: {art.name}")
        if art.is_dir() or not art.suffix:
            return art / "task-graph.md"
        if art.suffix.lower() not in (".md", ".markdown"):
            raise ValueError(f"Artifact file target must be a Markdown file (.md), got: {art.name}")
        return art

    def update_session_anchor(
        self,
        tracker_dir_or_feature_dir: Path | str,
        active_phase: str | None = None,
        fix_loop_count: int | None = None,
        reviewed_commits: list[str] | None = None,
        feature_slug: str | None = None,
        remediated: bool | None = None,
        commit_sha: str | None = None,
        failing_phase: str | None = None,
        remediation_scope: str | None = None,
        audit_bypassed: bool | None = None,
        phase_overrides: dict[str, str] | None = None,
    ) -> Path:
        """
        [STATE-01] [REMD-02] [LOOP-01] [LOOP-02] Updates Command Center Session Anchor metadata (.scratch/<feature-slug>/session.json).
        Updates activePhase, fixLoopCount, reviewedCommits, and remediation metadata, persisting to disk immediately.
        """
        if tracker_dir_or_feature_dir is None or not str(tracker_dir_or_feature_dir).strip():
            raise ValueError("A non-empty tracker_dir_or_feature_dir is required to update session anchor")

        target_file = self._resolve_session_anchor_path(tracker_dir_or_feature_dir, feature_slug=feature_slug)
        if not target_file or not target_file.is_file():
            raise FileNotFoundError(f"Session anchor not found at '{target_file}'")

        try:
            data = json.loads(target_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Malformed session anchor JSON in '{target_file}': {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"Session anchor in '{target_file}' must be a JSON object")

        if active_phase is not None:
            if not isinstance(active_phase, str) or not active_phase.strip():
                raise ValueError("active_phase must be a non-empty string")
            data["activePhase"] = active_phase.strip()

        if fix_loop_count is not None:
            if not isinstance(fix_loop_count, int) or fix_loop_count < 0:
                raise ValueError("fix_loop_count must be a non-negative integer")
            data["fixLoopCount"] = fix_loop_count

        if reviewed_commits is not None:
            if not isinstance(reviewed_commits, list):
                raise ValueError("reviewed_commits must be a list of commit SHAs")
            cleaned_commits: list[str] = []
            for c in reviewed_commits:
                cs = str(c).strip()
                if cs and cs not in cleaned_commits:
                    cleaned_commits.append(cs)
            data["reviewedCommits"] = cleaned_commits

        if remediated is not None:
            data["remediated"] = bool(remediated)

        if commit_sha is not None:
            clean_sha = str(commit_sha).strip()
            if clean_sha:
                data["commitSha"] = clean_sha
                data["remediationCommit"] = clean_sha
                if "reviewedCommits" in data and isinstance(data["reviewedCommits"], list):
                    if clean_sha not in data["reviewedCommits"]:
                        data["reviewedCommits"].append(clean_sha)

        if failing_phase is not None:
            data["failingPhase"] = str(failing_phase).strip()

        if remediation_scope is not None:
            data["remediationScope"] = str(remediation_scope).strip()

        if audit_bypassed is not None:
            data["auditBypassed"] = bool(audit_bypassed)

        if phase_overrides is not None:
            data["phaseOverrides"] = dict(phase_overrides)

        target_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return target_file

    def read_session_anchor(
        self,
        tracker_dir_or_feature_dir: Path | str | None,
        feature_slug: str | None = None,
    ) -> dict[str, Any] | None:
        """
        [ANCH-01] Reads and validates the Command Center Session Anchor (.scratch/<feature-slug>/session.json).
        Returns parsed dict if valid, None if file does not exist, or raises ValueError if malformed.
        """
        if tracker_dir_or_feature_dir is None or not str(tracker_dir_or_feature_dir).strip():
            return None

        target_file = self._resolve_session_anchor_path(tracker_dir_or_feature_dir, feature_slug=feature_slug)
        if not target_file or not target_file.is_file():
            return None

        try:
            content = target_file.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as exc:
            raise ValueError(f"Malformed session anchor JSON in '{target_file}': {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"Session anchor in '{target_file}' must be a JSON object, got {type(data).__name__}")

        for req in ("commandCenterConversationId", "feature", "createdAt"):
            if req not in data:
                raise ValueError(f"Session anchor in '{target_file}' is missing required field: '{req}'")
            val = data[req]
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"Session anchor in '{target_file}' field '{req}' must be a non-empty string, got {val!r}")

        try:
            parsed_dt = datetime.fromisoformat(data["createdAt"])
            if parsed_dt.tzinfo is None:
                raise ValueError("Timestamp must include explicit timezone offset (e.g. UTC)")
        except Exception as exc:
            raise ValueError(
                f"Session anchor in '{target_file}' field 'createdAt' must be a valid ISO-8601 UTC timestamp: {exc}"
            ) from exc

        if "activePhase" not in data:
            data["activePhase"] = "implementation"
        else:
            phase_val = data["activePhase"]
            if not isinstance(phase_val, str) or not phase_val.strip():
                raise ValueError(
                    f"Session anchor in '{target_file}' field 'activePhase' must be a non-empty string, got {phase_val!r}"
                )

        if "fixLoopCount" not in data:
            data["fixLoopCount"] = 0
        else:
            fix_val = data["fixLoopCount"]
            if not isinstance(fix_val, int) or fix_val < 0:
                raise ValueError(
                    f"Session anchor in '{target_file}' field 'fixLoopCount' must be a non-negative integer, got {fix_val!r}"
                )

        if "reviewedCommits" not in data:
            data["reviewedCommits"] = []
        else:
            commits_val = data["reviewedCommits"]
            if not isinstance(commits_val, list):
                raise ValueError(
                    f"Session anchor in '{target_file}' field 'reviewedCommits' must be a list, got {commits_val!r}"
                )

        return data

    def validate_session_anchor_schema(
        self,
        session_data_or_path: dict[str, Any] | Path | str,
    ) -> tuple[bool, list[str]]:
        """
        [ZERO-02] Validates the schema of .scratch/<feature-slug>/session.json:
        - Must contain required fields: feature, createdAt, activePhase, fixLoopCount, reviewedCommits.
        - Must contain ZERO occurrences of commandCenterConversationId.
        - Returns (is_valid, errors).
        """
        errors: list[str] = []
        if isinstance(session_data_or_path, (str, Path)):
            p = Path(session_data_or_path)
            if not p.is_file():
                return False, [f"Session anchor file '{p}' not found"]
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                return False, [f"Could not parse session anchor JSON in '{p}': {exc}"]
        elif isinstance(session_data_or_path, dict):
            data = session_data_or_path
        else:
            return False, ["Session anchor data must be a dictionary or path"]

        if not isinstance(data, dict):
            return False, ["Session anchor data must be a dictionary"]

        required_keys = ("feature", "createdAt", "activePhase", "fixLoopCount", "reviewedCommits")
        for req in required_keys:
            if req not in data:
                errors.append(f"Session anchor schema missing required key '{req}'")

        if "commandCenterConversationId" in data:
            errors.append("Session anchor schema must not contain forbidden legacy key 'commandCenterConversationId'")

        return len(errors) == 0, errors

    def build_completion_signal(
        self,
        ticket_ref: str,
        commit_sha: str,
        summary: str = "",
        status: str = "resolved",
        as_json: bool = False,
    ) -> str:
        """
        [SIG-01] Builds the structured completion payload dispatched to the Command Center conversation.
        Supports both plain-text instruction and JSON-serialized signal structures.
        """
        if ticket_ref is None:
            raise ValueError("A non-empty ticket_ref is required to construct completion signal")
        ref_raw = str(ticket_ref).strip()
        if not ref_raw or ref_raw.lower() == "none":
            raise ValueError("A non-empty ticket_ref is required to construct completion signal")

        if commit_sha is None:
            raise ValueError("A non-empty commit_sha is required to construct completion signal")
        commit_clean = str(commit_sha).strip()
        if not commit_clean or commit_clean.lower() == "none":
            raise ValueError("A non-empty commit_sha is required to construct completion signal")

        # Strip user-facing shorthand markers (# or ticket-) to guarantee uniform DAG ticket correlation
        if ref_raw.startswith("#"):
            ref_clean = ref_raw[1:].strip()
        elif ref_raw.lower().startswith("ticket-"):
            ref_clean = ref_raw[7:].strip()
        elif ref_raw.lower().startswith("issue-"):
            ref_clean = ref_raw[6:].strip()
        else:
            ref_clean = ref_raw

        status_clean = (str(status).strip() if status is not None else "") or "resolved"
        summary_clean = " ".join(str(summary).strip().split()) if summary is not None else ""

        msg = f"Ticket {ref_clean} has been resolved and committed (commit: {commit_clean}). Status: {status_clean}."
        if summary_clean:
            msg += f" Summary: {summary_clean}."
        msg += " Please re-scan disk, update the Task Dashboard, and emit the next frontier handoff command."

        if as_json:
            return json.dumps(
                {
                    "ticket": ref_clean,
                    "commitSha": commit_clean,
                    "status": status_clean,
                    "summary": summary_clean,
                    "message": msg,
                },
                sort_keys=True,
            )
        return msg

    @classmethod
    def find_agentapi_client(
        cls,
        client_path_arg: str | Path | None = None,
    ) -> Path | None:
        """
        [DISC-01] Resolves the location of agentapi_client.py using a 3-tier cascade:
          Tier 1: Explicit CLI option / parameter (--client-path) if provided and exists
          Tier 2: AGENTAPI_CLIENT_PATH environment variable if set and exists
          Tier 3: Path.home() / ".gemini/config/skills/agentapi-orchestration/scripts/agentapi_client.py" if exists
        Returns resolved Path if found; None if all tiers fail to locate an existing binary.
        """
        if client_path_arg and str(client_path_arg).strip():
            p1 = Path(str(client_path_arg).strip()).expanduser()
            if p1.exists():
                return p1.resolve()

        env_path = os.environ.get("AGENTAPI_CLIENT_PATH")
        if env_path and env_path.strip():
            p2 = Path(env_path.strip()).expanduser()
            if p2.exists():
                return p2.resolve()

        try:
            p3 = Path.home() / ".gemini" / "config" / "skills" / "agentapi-orchestration" / "scripts" / "agentapi_client.py"
            if p3.exists():
                return p3.resolve()
        except (RuntimeError, KeyError):
            pass

        return None

    @classmethod
    def discover_command_center(
        cls,
        feature_dir: Path | str,
        workspace_root: Path | str | None = None,
        app_data_dir: Path | str | None = None,
    ) -> str | None:
        """
        [DISC-01] [DISC-02] 3-Tier Structural Discovery Cascade for target Command Center sessions.
        Tier 1 (Brain Artifact Fingerprint): Inspects <appDataDir>/brain/<id>/task-graph.md artifacts matching
        'Feature: <feature_slug>', returning the conversation ID with the latest modification timestamp.
        Tier 2 (SQLite FTS Command History): Queries SQLite index in <appDataDir>/conversation_summaries.db
        table 'conversations_fts' for conversations executing /to-tickets or /implement-spec in the workspace path.
        Returns the discovered conversation ID string, or None if structural discovery fails.
        """
        if not feature_dir or not str(feature_dir).strip():
            return None

        feature_slug = ""
        feature_path: Path | None = None
        raw_p = Path(str(feature_dir).strip())
        if raw_p.name in ("session.json", "issues"):
            feature_path = raw_p.parent
            feature_slug = raw_p.parent.name
        else:
            feature_path = raw_p
            feature_slug = raw_p.name

        if feature_path and (feature_path / "session.json").is_file():
            try:
                s_data = json.loads((feature_path / "session.json").read_text(encoding="utf-8"))
                if isinstance(s_data, dict) and s_data.get("feature"):
                    feature_slug = str(s_data["feature"]).strip()
            except Exception:
                pass

        if feature_slug in ("", ".", ".scratch", "issues"):
            feature_slug = ""

        # Resolve workspace_root to project/repo root
        resolved_workspace = None
        candidates = []
        if workspace_root and str(workspace_root).strip():
            candidates.append(Path(str(workspace_root).strip()))
        if feature_path:
            candidates.append(feature_path)

        for candidate in candidates:
            try:
                p = candidate.resolve()
                for ancestor in [p] + list(p.parents):
                    if (ancestor / ".scratch").is_dir() or (ancestor / ".git").is_dir() or (ancestor / "plugin.json").is_file():
                        resolved_workspace = ancestor
                        break
                if resolved_workspace:
                    break
            except Exception:
                pass

        if not resolved_workspace:
            if candidates:
                resolved_workspace = candidates[0].resolve()
            else:
                resolved_workspace = Path.cwd().resolve()

        if app_data_dir and str(app_data_dir).strip():
            app_dir = Path(str(app_data_dir).strip()).expanduser().resolve()
        elif os.environ.get("ANTIGRAVITY_APP_DATA_DIR") and os.environ["ANTIGRAVITY_APP_DATA_DIR"].strip():
            app_dir = Path(os.environ["ANTIGRAVITY_APP_DATA_DIR"].strip()).expanduser().resolve()
        else:
            try:
                app_dir = (Path.home() / ".gemini" / "antigravity").resolve()
            except (RuntimeError, KeyError):
                app_dir = Path("/home/agyuser/.gemini/antigravity")

        if not app_dir.is_dir():
            return None

        # Tier 1 (Brain Artifact Fingerprint)
        brain_dir = app_dir / "brain"
        if feature_slug and brain_dir.is_dir():
            candidates_brain: list[tuple[float, str]] = []
            try:
                pattern = re.compile(
                    rf"(?i)(?:\*\*)?feature(?:\*\*)?\s*:\s*[`*_'\"]*{re.escape(feature_slug)}[`*_'\"]*",
                )
                for entry in brain_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    task_graph = entry / "task-graph.md"
                    if task_graph.is_file():
                        try:
                            content = task_graph.read_text(encoding="utf-8", errors="replace")
                            if pattern.search(content):
                                mtime = task_graph.stat().st_mtime
                                candidates_brain.append((mtime, entry.name))
                        except OSError:
                            continue
            except OSError:
                pass

            if candidates_brain:
                candidates_brain.sort(key=lambda x: x[0], reverse=True)
                return candidates_brain[0][1]

        # Tier 2 (SQLite FTS Command History)
        db_path = app_dir / "conversation_summaries.db"
        if db_path.is_file():
            try:
                import sqlite3
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1 FROM sqlite_master WHERE type in ('table', 'view') AND name = 'conversations_fts'")
                    if cursor.fetchone():
                        cursor.execute("SELECT 1 FROM sqlite_master WHERE type in ('table', 'view') AND name = 'conversation_summaries'")
                        has_summaries = bool(cursor.fetchone())

                        workspace_str = str(resolved_workspace)
                        workspace_basename = resolved_workspace.name

                        rows: list[tuple[str, Any]] = []
                        if has_summaries:
                            query = """
                                SELECT f.conversation_id,
                                       COALESCE(s.last_modified_time, f.last_modified_time, '') as mod_time
                                FROM conversations_fts f
                                LEFT JOIN conversation_summaries s ON f.conversation_id = s.conversation_id
                                WHERE (f.user_body LIKE '%/to-tickets%' OR f.user_body LIKE '%/implement-spec%')
                                  AND (f.workspace LIKE ? OR f.workspace LIKE ? OR (f.workspace != '' AND ? LIKE '%' || f.workspace || '%')
                                       OR s.workspace_uris LIKE ? OR s.workspace_uris LIKE ?)
                                ORDER BY mod_time DESC
                            """
                            cursor.execute(
                                query,
                                (
                                    f"%{workspace_str}%",
                                    f"%{workspace_basename}%",
                                    workspace_str,
                                    f"%{workspace_str}%",
                                    f"%{workspace_basename}%",
                                ),
                            )
                            rows = cursor.fetchall()
                        else:
                            query = """
                                SELECT conversation_id, last_modified_time
                                FROM conversations_fts
                                WHERE (user_body LIKE '%/to-tickets%' OR user_body LIKE '%/implement-spec%')
                                  AND (workspace LIKE ? OR workspace LIKE ? OR (workspace != '' AND ? LIKE '%' || workspace || '%'))
                                ORDER BY last_modified_time DESC
                            """
                            cursor.execute(
                                query,
                                (
                                    f"%{workspace_str}%",
                                    f"%{workspace_basename}%",
                                    workspace_str,
                                ),
                            )
                            rows = cursor.fetchall()

                        if rows:
                            for cid, _ in rows:
                                clean_cid = str(cid).strip()
                                if clean_cid:
                                    return clean_cid
                finally:
                    conn.close()
            except (sqlite3.Error, OSError):
                pass

        return None

    def dispatch_completion_signal(
        self,
        tracker_dir_or_feature_dir: Path | str | None,
        ticket_ref: str,
        commit_sha: str,
        summary: str = "",
        status: str = "resolved",
        feature_slug: str | None = None,
        client_path: Path | str | None = None,
        timeout_seconds: float = 15.0,
        as_json: bool = False,
        payload: str | None = None,
    ) -> tuple[bool, str]:
        """
        [SIG-01][SIG-02] Dispatches asynchronous completion notification via ConnectRPC to the Command Center.
        Gracefully degrades if session.json is missing or the language server returns a non-zero exit code.
        """
        try:
            anchor_data = self.read_session_anchor(tracker_dir_or_feature_dir, feature_slug=feature_slug)
        except Exception as exc:
            return False, f"Failed to read session anchor: {exc}"

        if not anchor_data:
            return False, "Session anchor (.scratch/<feature-slug>/session.json) not found; skipped signal dispatch."

        target_cid = str(anchor_data.get("commandCenterConversationId") or "").strip()
        if not target_cid:
            return False, "Session anchor missing valid commandCenterConversationId; skipped signal dispatch."

        # Prevent malformed conversation identifiers with newlines or spaces from corrupting CLI argument bounds
        if any(c.isspace() for c in target_cid):
            return False, f"Session anchor contains invalid commandCenterConversationId with whitespace: {target_cid!r}"

        if payload is not None:
            resolved_payload = payload
        else:
            try:
                resolved_payload = self.build_completion_signal(
                    ticket_ref=ticket_ref,
                    commit_sha=commit_sha,
                    summary=summary,
                    status=status,
                    as_json=as_json,
                )
            except Exception as exc:
                return False, f"Failed to build completion signal payload: {exc}"

        # [DISC-01] 3-tier cascade client discovery: CLI option -> AGENTAPI_CLIENT_PATH -> standard user home
        resolved_client = self.find_agentapi_client(client_path)
        if not resolved_client:
            # Fallback to declared path if client does not exist on disk (mock test compatibility)
            if client_path and str(client_path).strip():
                resolved_client = Path(str(client_path).strip()).expanduser()
            elif os.environ.get("AGENTAPI_CLIENT_PATH") and os.environ["AGENTAPI_CLIENT_PATH"].strip():
                resolved_client = Path(os.environ["AGENTAPI_CLIENT_PATH"].strip()).expanduser()
            else:
                try:
                    resolved_client = Path.home() / ".gemini" / "config" / "skills" / "agentapi-orchestration" / "scripts" / "agentapi_client.py"
                except (RuntimeError, KeyError):
                    resolved_client = Path("/home/agyuser/.gemini/config/skills/agentapi-orchestration/scripts/agentapi_client.py")

        cmd = [sys.executable, str(resolved_client), "send", target_cid, resolved_payload]

        # Guard against invalid or negative timeout values that cause subprocess to raise an immediate TimeoutExpired
        effective_timeout = timeout_seconds if (timeout_seconds is not None and timeout_seconds > 0) else 15.0

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
            )
            if proc.returncode == 0:
                return True, f"Completion signal successfully dispatched to Command Center ({target_cid})."
            err_msg = (proc.stderr or proc.stdout or "(no output recorded)").strip()
            return False, f"ConnectRPC dispatch failed with exit code {proc.returncode}: {err_msg}"
        except subprocess.TimeoutExpired:
            return False, f"ConnectRPC dispatch timed out after {effective_timeout} seconds."
        except (FileNotFoundError, PermissionError) as exc:
            return False, f"ConnectRPC client execution error: {exc}"
        except OSError as exc:
            return False, f"ConnectRPC OS error: {exc}"
        except Exception as exc:
            return False, f"ConnectRPC dispatch error: {exc}"

    def build_session_brief(
        self,
        phase: str = "code-review",
        spec_path: str = "",
        delta_spec_path: str = "",
        commits: list[str] | str = "",
        branch: str = "",
        seam: str = "",
        conversation_id: str = "",
        as_fenced_block: bool = False,
    ) -> str:
        """
        [BRIEF-01] Builds the complete Structured Session Brief for downstream phases (/code-review, /security-audit, /pr).
        Contains spec line references, delta spec path, commit SHAs, architectural seams, and local terminal
        reporting directives with Recommend-and-Stop turn conclusion.
        """
        phase_cmd = phase if phase.startswith("/") else f"/{phase}"
        clean_phase = phase_cmd.lstrip("/").lower()

        if not commits:
            commits_str = ""
        elif isinstance(commits, (list, tuple, set)):
            commits_str = " ".join(str(c).strip() for c in commits if str(c).strip())
        else:
            commits_str = str(commits).strip()

        commits_line = commits_str
        if branch:
            commits_line = f"{commits_str} (branch: {branch})" if commits_str else f"(branch: {branch})"

        if "security-audit" in clean_phase:
            reporting_line = "Reporting:  ./scripts/report-phase.sh security-audit <verdict> --summary \"<summary>\" (conclude with Recommend-and-Stop)"
        elif clean_phase == "pr":
            reporting_line = "Reporting:  Local completion / PR submission (conclude with Recommend-and-Stop)"
        else:
            reporting_line = "Reporting:  ./scripts/report-phase.sh code-review <verdict> --summary \"<summary>\" (conclude with Recommend-and-Stop)"

        lines = [
            phase_cmd,
            f"Spec:       {spec_path or 'docs/specs/<domain>/spec.md'}",
            f"Delta:      {delta_spec_path or '.scratch/<feature-slug>/spec.md'}",
            f"Commits:    {commits_line or '<sha>'}",
            f"Seam:       {seam or '<target/seam#Section>'}",
            reporting_line,
            "",
            "```",
        ]

        if "security-audit" in clean_phase:
            lines.extend([
                "SECURITY_AUDIT_RESULT",
                "verdict: PASS | FAIL",
                "critical_findings: <count>",
                "high_findings: <count>",
                "findings: <bulleted summary or \"none\">",
                f"commits_reviewed: {commits_str or '<sha>'}",
            ])
        elif clean_phase == "pr":
            lines.extend([
                "PR_RESULT",
                "status: MERGED | CLOSED | OPEN",
                "pr_url: <github-pr-url or \"none\">",
            ])
        else:
            lines.extend([
                "CODE_REVIEW_RESULT",
                "verdict: PASS | FAIL",
                "axis_standards: PASS | FAIL",
                "axis_spec: PASS | FAIL",
                "findings: <bulleted summary or \"none\">",
                f"commits_reviewed: {commits_str or '<sha>'}",
            ])

        lines.append("```")
        body = "\n".join(lines)
        if as_fenced_block:
            return f"````markdown\n{body}\n````"
        return body

    def validate_session_brief_content(
        self,
        content: str,
        enforce_zero_ipc: bool = False,
    ) -> tuple[bool, list[str]]:
        """
        [BRIEF-01][ZERO-01] Validates Structured Session Brief content for downstream phase handoffs:
        - Must contain downstream command (e.g., /code-review, /security-audit, /pr).
        - Must contain Spec context pointer (e.g., 'Spec:').
        - Must contain Delta spec pointer (e.g., 'Delta:').
        - Must contain reviewed Commits pointer (e.g., 'Commits:').
        - Must contain Seam pointer (e.g., 'Seam:').
        - Strictly rejects bare commands without context pointers.
        - When enforce_zero_ipc=True, asserts terminal Recommend-and-Stop directives instead of
          ConnectRPC report-back instructions, and fails on any presence of cross-conversation IPC.
        """
        errors: list[str] = []

        if not re.search(r"(?:/code-review|/security-audit|/pr|/implement(?:\s+fix)?)", content):
            errors.append("Session brief missing downstream command (e.g. '/code-review')")

        if not re.search(r"Spec:\s*\S+", content, re.IGNORECASE):
            errors.append("Session brief missing 'Spec:' context pointer")

        if not re.search(r"Delta:\s*\S+", content, re.IGNORECASE):
            errors.append("Session brief missing 'Delta:' spec path pointer")

        if not re.search(r"Commits:\s*\S+", content, re.IGNORECASE):
            errors.append("Session brief missing 'Commits:' pointer")

        if not re.search(r"Seam:\s*\S+", content, re.IGNORECASE):
            errors.append("Session brief missing 'Seam:' architectural seam pointer")

        if enforce_zero_ipc:
            if (
                re.search(r"agentapi_client(?:\.py)?(?:\s+send\b|\b)", content, re.IGNORECASE)
                or re.search(r"\bconnectrpc\b", content, re.IGNORECASE)
                or re.search(r"\bcommandCenterConversationId\b", content, re.IGNORECASE)
            ):
                errors.append("Session brief must not contain cross-conversation IPC or ConnectRPC instructions")

            if "recommend-and-stop" not in content.lower():
                errors.append("Session brief must mandate local terminal Recommend-and-Stop directives instead of ConnectRPC report-back instructions")

        return len(errors) == 0, errors

    TRIGGER_H_DOMAIN_A_PATTERNS: tuple[str, ...] = (
        r"(?:^|/)api(?:/|\.|$)",
        r"(?:^|/)routes?(?:/|\.|$)",
        r"(?:^|/)server-actions?(?:/|\.|$)",
        r"\.action\.[a-zA-Z0-9]+$",
        r"(?:^|/)middleware\.[a-zA-Z0-9]+$",
        r"(?:^|/)auth(?:/|\.|$)",
    )

    TRIGGER_H_DOMAIN_B_PATTERNS: tuple[str, ...] = (
        r"\.sh$",
        r"(?:^|/)hooks\.json$",
        r"(?:^|/)agentapi_client\.py$",
    )

    TRIGGER_H_DOMAIN_B_TOKEN_PATTERN: str = (
        r"\b(subprocess|sqlite3|shlex|tempfile|mktemp|os\.(?:system|popen|exec[a-z]*|spawn[a-z]*)|"
        r"(?:cursor|conn|connection|db)\.(?:execute|executemany))\b|"
        r"(?:\.execute(?:many)?\s*\(\s*(?:f[\"']|[\"'].*?\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b))"
    )

    TRIGGER_H_PATTERNS: tuple[str, ...] = TRIGGER_H_DOMAIN_A_PATTERNS + TRIGGER_H_DOMAIN_B_PATTERNS

    @staticmethod
    def is_pure_test_file(path_str: str) -> bool:
        """Determines whether a file is an isolated unit test or test suite (non-attack surface)."""
        norm = path_str.replace("\\", "/")
        if re.search(r"(?:^|/)(?:tests?|__tests__|evals)/", norm, re.IGNORECASE):
            return True
        filename = Path(norm).name
        if re.search(r"(?:\.test\.|\.spec\.|^test_|_test\.)[a-zA-Z0-9]+$", filename, re.IGNORECASE):
            return True
        return False

    def evaluate_trigger_h(
        self,
        files_or_diff: list[str] | str | None,
        repo_path: Path | str | None = None,
        force_audit: bool = False,
        audit: bool = False,
        manual_override: bool = False,
    ) -> tuple[bool, list[str]]:
        """
        [BRIEF-02][ROUTE-01][ROUTE-04] Evaluates files or raw diff against Dual-Domain Trigger H attack surface patterns:
        - Domain A (Web & Application Endpoints):
          - **/api/**, **/api.*
          - **/routes/**, **/route.*
          - **/server-actions/**, **/server-action.*
          - **/*.action.ts
          - **/middleware.ts
          - **/auth/**, **/auth.*
          - Any file declaring 'use server'
        - Domain B (CLI, Tooling & Platform Infrastructure):
          - scripts/**/*.sh
          - hooks/**/*.sh
          - hooks.json
          - agentapi_client.py
          - scripts/internal/**/*.py using subprocess or sqlite3
        Explicit manual overrides (force_audit, audit, manual_override) unconditionally force matched = True ([ROUTE-04]).
        Returns (is_matched, matched_files).
        """
        is_override = bool(force_audit or audit or manual_override)
        if not files_or_diff:
            return (True, []) if is_override else (False, [])

        matched_files: list[str] = []

        # Resolve candidate directory roots to handle .scratch subdirectories transparently
        repo_candidates: list[Path] = []
        if repo_path:
            p = Path(repo_path).resolve()
            repo_candidates.append(p)
            if ".scratch" in p.parts:
                idx = p.parts.index(".scratch")
                repo_candidates.append(Path(*p.parts[:idx]))
            if p.parent not in repo_candidates:
                repo_candidates.append(p.parent)
        else:
            repo_candidates.append(Path.cwd().resolve())

        if isinstance(files_or_diff, str):
            diff_text = files_or_diff.strip()
            if "\n" in diff_text and ("diff --git" in diff_text or "--- " in diff_text or "+++ " in diff_text):
                # Parse diff into per-file sections to prevent cross-file contamination
                sections: list[tuple[list[str], str]] = []
                current_files: list[str] = []
                current_lines: list[str] = []

                for line in diff_text.splitlines():
                    m_git_quoted = re.match(r"^diff --git \"(?:a/)?(.*?)\" \"(?:b/)?(.*?)\"$", line)
                    m_git_unquoted = re.match(r"^diff --git (?:a/)?(\S+)\s+(?:b/)?(\S+)$", line)
                    m_git = m_git_quoted or m_git_unquoted
                    if m_git:
                        if current_files or current_lines:
                            sections.append((current_files, "\n".join(current_lines)))
                            current_files = []
                            current_lines = []
                        f1 = m_git.group(1).strip("\"' \t")
                        f2 = m_git.group(2).strip("\"' \t")
                        if f2 and f2 not in ("/dev/null", "dev/null"):
                            current_files.append(f2)
                        if f1 and f1 not in ("/dev/null", "dev/null") and f1 not in current_files:
                            current_files.append(f1)
                        current_lines.append(line)
                        continue

                    m_minus = re.match(r"^--- (?:\"?a/)?(.*?)(?:\"|\t|$)", line)
                    if m_minus:
                        fn = m_minus.group(1).strip("\"' \t")
                        # In multi-file unified diffs without diff --git, seeing a new --- after a +++ indicates the next file
                        if current_files and any(l.startswith("+++ ") for l in current_lines):
                            sections.append((current_files, "\n".join(current_lines)))
                            current_files = []
                            current_lines = []
                        if fn and fn not in ("/dev/null", "dev/null") and fn not in current_files:
                            current_files.append(fn)
                        current_lines.append(line)
                        continue

                    m_plus = re.match(r"^\+\+\+ (?:\"?b/)?(.*?)(?:\"|\t|$)", line)
                    if m_plus:
                        fn = m_plus.group(1).strip("\"' \t")
                        if fn and fn not in ("/dev/null", "dev/null") and fn not in current_files:
                            current_files.append(fn)
                        current_lines.append(line)
                        continue

                    current_lines.append(line)

                if current_files or current_lines:
                    sections.append((current_files, "\n".join(current_lines)))

                for sec_files, sec_diff in sections:
                    has_domain_b_tokens = bool(re.search(self.TRIGGER_H_DOMAIN_B_TOKEN_PATTERN, sec_diff, re.IGNORECASE))
                    for f in sec_files:
                        norm = f.replace("\\", "/")
                        is_test = self.is_pure_test_file(norm)

                        is_domain_b_pattern = not is_test and any(re.search(pat, norm, re.IGNORECASE) for pat in self.TRIGGER_H_DOMAIN_B_PATTERNS)
                        is_domain_a_pattern = not is_test and any(re.search(pat, norm, re.IGNORECASE) for pat in self.TRIGGER_H_DOMAIN_A_PATTERNS)

                        is_server_candidate = not is_test and norm.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))
                        has_use_server = is_server_candidate and bool(re.search(r"['\"]use server['\"]", sec_diff))

                        is_script_py = not is_test and norm.endswith(".py")
                        has_py_surface = False
                        if is_script_py:
                            if has_domain_b_tokens:
                                has_py_surface = True
                            elif repo_candidates:
                                for base in repo_candidates:
                                    cand_path = base / f if not Path(f).is_absolute() else Path(f)
                                    if cand_path.is_file():
                                        try:
                                            content = cand_path.read_text(encoding="utf-8", errors="replace")
                                            if re.search(self.TRIGGER_H_DOMAIN_B_TOKEN_PATTERN, content, re.IGNORECASE):
                                                has_py_surface = True
                                                break
                                        except Exception:
                                            pass

                        if not (is_domain_a_pattern or is_domain_b_pattern or has_use_server or has_py_surface) and is_server_candidate and repo_candidates:
                            for base in repo_candidates:
                                cand_path = base / f if not Path(f).is_absolute() else Path(f)
                                if cand_path.is_file():
                                    try:
                                        content = cand_path.read_text(encoding="utf-8", errors="replace")
                                        if re.search(r"['\"]use server['\"]", content):
                                            has_use_server = True
                                            break
                                    except Exception:
                                        pass

                        if (is_domain_a_pattern or is_domain_b_pattern or has_use_server or has_py_surface) and f not in matched_files:
                            matched_files.append(f)

                # Fallback for headless inline diff snippets containing 'use server'
                if not matched_files and re.search(r"['\"]use server['\"]", diff_text):
                    matched_files.append("inline-diff")

                return (len(matched_files) > 0 or is_override), matched_files
            else:
                raw_entries = re.split(r"[;\n,]+", diff_text)
                file_list = [e.strip() for e in raw_entries if e.strip()]
        else:
            file_list = [str(e).strip() for e in files_or_diff if str(e).strip()]

        for f_clean in file_list:
            norm = f_clean.replace("\\", "/")
            is_test = self.is_pure_test_file(norm)

            is_domain_b_pattern = not is_test and any(re.search(pat, norm, re.IGNORECASE) for pat in self.TRIGGER_H_DOMAIN_B_PATTERNS)
            is_domain_a_pattern = not is_test and any(re.search(pat, norm, re.IGNORECASE) for pat in self.TRIGGER_H_DOMAIN_A_PATTERNS)

            is_server_candidate = not is_test and norm.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))
            is_script_py = not is_test and norm.endswith(".py")

            is_match = is_domain_a_pattern or is_domain_b_pattern
            if not is_match and repo_candidates:
                for base in repo_candidates:
                    cand_path = base / f_clean if not Path(f_clean).is_absolute() else Path(f_clean)
                    if cand_path.is_file():
                        try:
                            content = cand_path.read_text(encoding="utf-8", errors="replace")
                            if is_server_candidate and re.search(r"['\"]use server['\"]", content):
                                is_match = True
                                break
                            if is_script_py and re.search(self.TRIGGER_H_DOMAIN_B_TOKEN_PATTERN, content, re.IGNORECASE):
                                is_match = True
                                break
                        except Exception:
                            pass

            if is_match and f_clean not in matched_files:
                matched_files.append(f_clean)

        return (len(matched_files) > 0 or is_override), matched_files

    def format_security_audit_decision_log(
        self,
        trigger_matched: bool,
        attack_surfaces: list[str] | None = None,
        manual_override: bool = False,
    ) -> str:
        """
        [ROUTE-03][ROUTE-04] Emits explicit single-line blockquote log for security gate decisions.
        - If manual override requested: notes explicit manual override.
        - If activated via patterns: lists detected attack surface files.
        - If bypassed: explicitly declares 0 attack surface files detected.
        """
        if manual_override:
            if attack_surfaces:
                files_str = ", ".join(attack_surfaces)
                return f"> 🛡️ Security Gate: Trigger H Activated (Explicit manual override requested; Attack surfaces detected: {files_str}). Advancing to /security-audit."
            return "> 🛡️ Security Gate: Trigger H Activated (Explicit manual override requested). Advancing to /security-audit."

        if trigger_matched:
            files_str = ", ".join(attack_surfaces or [])
            return f"> 🛡️ Security Gate: Trigger H Activated (Attack surfaces detected: {files_str}). Advancing to /security-audit."

        return "> 🛡️ Security Gate: Bypassed (0 attack surface files detected in diff). Advancing directly to /pr."

    def build_security_audit_brief(
        self,
        spec_path: str = "",
        delta_spec_path: str = "",
        commits: list[str] | str | None = "",
        branch: str = "",
        attack_surfaces: list[str] | str | None = "",
        conversation_id: str = "",
        as_fenced_block: bool = False,
    ) -> str:
        """
        [BRIEF-02] Builds the complete Structured Session Brief for /security-audit phase.
        Includes audited commit range, attack surface file paths as the seam, and terminal
        reporting directives via ./scripts/report-phase.sh security-audit without ConnectRPC instructions.
        """
        if not attack_surfaces:
            seam_str = ""
        elif isinstance(attack_surfaces, (list, tuple, set)):
            seam_str = "; ".join(str(s).strip() for s in attack_surfaces if str(s).strip())
        else:
            seam_str = str(attack_surfaces).strip()

        return self.build_session_brief(
            phase="security-audit",
            spec_path=spec_path,
            delta_spec_path=delta_spec_path,
            commits=commits,
            branch=branch,
            seam=seam_str or "public attack surface",
            conversation_id=conversation_id,
            as_fenced_block=as_fenced_block,
        )

    def synthesize_security_audit_session_brief(
        self,
        spec_path: str = "",
        delta_spec_path: str = "",
        commits: list[str] | str | None = "",
        branch: str = "",
        attack_surfaces: list[str] | str | None = "",
        conversation_id: str = "",
        as_fenced_block: bool = False,
    ) -> str:
        """
        [BRIEF-02] Synthesizes complete Structured Session Brief for /security-audit phase with terminal
        reporting directives via ./scripts/report-phase.sh security-audit and Recommend-and-Stop turn conclusion.
        """
        return self.build_security_audit_brief(
            spec_path=spec_path,
            delta_spec_path=delta_spec_path,
            commits=commits,
            branch=branch,
            attack_surfaces=attack_surfaces,
            conversation_id=conversation_id,
            as_fenced_block=as_fenced_block,
        )

    def fallback_scan_verdict(self, payload: str | dict[str, Any]) -> str:
        """
        [ROUTE-02] Regex fallback scanner for inbound review/audit verdicts deviating from standard formatting.
        Scans for PASS or FAIL tokens across text and dictionary structures.
        Returns 'PASS', 'FAIL', or 'UNRESOLVED'.
        """
        if not payload:
            return "UNRESOLVED"

        text = ""
        if isinstance(payload, dict):
            v_direct = str(payload.get("verdict", "") or payload.get("status", "") or payload.get("result", "")).strip().upper()
            if v_direct in ("PASS", "PASSED", "SUCCEEDED", "APPROVED"):
                return "PASS"
            if v_direct in ("FAIL", "FAILED", "REJECTED"):
                return "FAIL"
            parts = [f"{k}: {v}" for k, v in payload.items()]
            text = "\n".join(parts)
        else:
            text = str(payload)

        m_confirm = re.search(r"(?i)\[Confirm\s+(PASS|FAIL)\]|\bconfirm(?:ed)?\s*[:=]?\s*[*`\"']*(PASS|FAIL)\b", text)
        if m_confirm:
            token = (m_confirm.group(1) or m_confirm.group(2)).upper()
            return token

        stripped = text.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or ("```" in text):
            cand_json = stripped if (stripped.startswith("{") and stripped.endswith("}")) else ""
            if not cand_json:
                m_fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
                if m_fence:
                    cand_json = m_fence.group(1).strip()
            if cand_json:
                try:
                    data = json.loads(cand_json)
                    if isinstance(data, dict):
                        j_verdict = str(data.get("verdict", "") or data.get("status", "") or data.get("result", "")).strip().upper()
                        if j_verdict in ("PASS", "PASSED", "SUCCEEDED", "APPROVED"):
                            return "PASS"
                        if j_verdict in ("FAIL", "FAILED", "REJECTED"):
                            return "FAIL"
                except Exception:
                    pass

        m_alert = re.search(
            r"(?im)^\s*>\s*\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION|ALERT|CRITICAL)\]\s*[*`\"'\s]*(PASS(?:ED)?|FAIL(?:ED)?|SUCCEEDED|FAILED|APPROVED|REJECTED)\b",
            text,
        )
        if m_alert:
            tok = m_alert.group(1).upper()
            return "PASS" if tok in ("PASS", "PASSED", "SUCCEEDED", "APPROVED") else "FAIL"

        m_tbl = re.search(
            r"(?im)^\|\s*[*`\"'\s]*(?:verdict|status|result|outcome|evaluation)[*`\"'\s]*\|\s*[*`\"'\s]*(PASS(?:ED)?|FAIL(?:ED)?|SUCCEEDED|FAILED|APPROVED|REJECTED)[*`\"'\s]*\|",
            text,
        )
        if m_tbl:
            tok = m_tbl.group(1).upper()
            return "PASS" if tok in ("PASS", "PASSED", "SUCCEEDED", "APPROVED") else "FAIL"

        m_expl = re.search(
            r"(?im)(?:^|\n|\r)\s*(?:[-*#>]\s*)*[*`\"'\s]*(?:verdict|status|result|outcome|evaluation|code_review_result|security_audit_result)[*`\"'\s]*[:=]\s*[*`\"'\s]*(PASS(?:ED)?|FAIL(?:ED)?|SUCCEEDED|FAILED|APPROVED|REJECTED)\b",
            text,
        )
        if m_expl:
            tok = m_expl.group(1).upper()
            return "PASS" if tok in ("PASS", "PASSED", "SUCCEEDED", "APPROVED") else "FAIL"

        clean_text = text
        if not (stripped.startswith("```") and stripped.endswith("```") and stripped.count("```") == 2):
            clean_text = re.sub(r"```[\s\S]*?```", " ", text)
            clean_text = re.sub(r"`[^`\n]+`", " ", clean_text)

        neg_fail_matches = re.findall(
            r"(?i)\b(?:0|zero|no|without|clean\s+of)\s+(?:failures?|fails?|failed|regressions?|defects?|errors?)\b",
            clean_text,
        )
        neg_pass_matches = re.findall(
            r"(?i)\b(?:0|zero|no|without)\s+(?:passes?|passed)\b",
            clean_text,
        )

        raw_fail_matches = re.findall(r"(?i)\b(FAIL(?:ED)?|REJECTED|failures?|fails?)\b", clean_text)
        raw_pass_matches = re.findall(r"(?i)\b(PASS(?:ED)?|SUCCEEDED|APPROVED|passes?)\b", clean_text)

        eff_fail = max(0, len(raw_fail_matches) - len(neg_fail_matches))
        eff_pass = max(0, len(raw_pass_matches) - len(neg_pass_matches))

        if eff_fail > 0 and eff_pass == 0:
            return "FAIL"
        if eff_pass > 0 and eff_fail == 0:
            return "PASS"

        return "UNRESOLVED"

    def parse_verdict_signal(
        self,
        payload: str | dict[str, Any],
        fallback: bool = True,
    ) -> dict[str, Any]:
        """
        [ROUTE-02] Parses review/audit verdict signal with non-blocking fallback scanning.
        Returns a dict containing:
        - verdict: 'PASS' | 'FAIL' | 'UNRESOLVED'
        - axis_standards: 'PASS' | 'FAIL' | None
        - axis_spec: 'PASS' | 'FAIL' | None
        - findings: str or list
        - commits_reviewed: list[str]
        - is_fallback: bool
        - requires_operator_confirmation: bool
        - operator_prompt: str (markdown block for 1-click operator confirmation if UNRESOLVED)
        - raw_payload: str
        """
        raw_str = ""
        verdict = ""
        axis_standards = None
        axis_spec = None
        findings = ""
        commits_reviewed: list[str] = []
        is_fallback = False
        audit_required = False

        if not payload:
            verdict = "UNRESOLVED"
            raw_str = ""
        else:
            dict_payload: dict[str, Any] | None = None
            if isinstance(payload, dict):
                dict_payload = payload
                raw_str = json.dumps(payload, indent=2)
            elif isinstance(payload, str):
                raw_str = payload
                stripped = payload.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    try:
                        parsed_json = json.loads(stripped)
                        if isinstance(parsed_json, dict):
                            dict_payload = parsed_json
                    except Exception:
                        pass
                if dict_payload is None and "```" in payload:
                    m_fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", payload)
                    if m_fence:
                        try:
                            parsed_fence = json.loads(m_fence.group(1))
                            if isinstance(parsed_fence, dict):
                                dict_payload = parsed_fence
                        except Exception:
                            pass

            if dict_payload is not None:
                v_val = str(dict_payload.get("verdict", "")).strip().upper()
                if v_val in ("PASS", "FAIL"):
                    verdict = v_val

                if "axis_standards" in dict_payload:
                    axis_standards = str(dict_payload["axis_standards"]).strip().upper() or None
                if "axis_spec" in dict_payload:
                    axis_spec = str(dict_payload["axis_spec"]).strip().upper() or None

                findings = dict_payload.get("findings", "")

                audit_val = dict_payload.get("audit_required")
                if audit_val is None:
                    audit_val = dict_payload.get("force_audit")
                if audit_val is None:
                    audit_val = dict_payload.get("audit")
                if isinstance(audit_val, bool):
                    audit_required = audit_val
                elif isinstance(audit_val, str):
                    audit_required = audit_val.strip().lower() in ("true", "yes", "1")
                elif audit_val is not None:
                    audit_required = bool(audit_val)

                c_val = dict_payload.get("commits_reviewed", [])
                if isinstance(c_val, list):
                    commits_reviewed = [str(x).strip() for x in c_val if str(x).strip()]
                elif isinstance(c_val, str):
                    commits_reviewed = [x.strip() for x in c_val.split() if x.strip() and x.strip() != "<sha>"]

                if verdict == "PASS" and (axis_standards == "FAIL" or axis_spec == "FAIL"):
                    verdict = "FAIL"

                if not verdict and fallback:
                    fb = self.fallback_scan_verdict(dict_payload)
                    if fb in ("PASS", "FAIL"):
                        verdict = fb
                        is_fallback = True
                    else:
                        verdict = "UNRESOLVED"
                elif not verdict:
                    verdict = "UNRESOLVED"
            else:
                m_verdict = re.search(
                    r"(?im)(?:^|\n|\r)\s*(?:[-*#>]\s*)*[*`\"'\s]*(?:verdict|CODE_REVIEW_RESULT|SECURITY_AUDIT_RESULT)[*`\"'\s]*[:=]?\s*[*`\"'\s]*(PASS|FAIL)\b",
                    raw_str,
                )
                if m_verdict:
                    verdict = m_verdict.group(1).upper()
                else:
                    m_token = re.search(
                        r"(?im)(?:^|\n|\r)\s*(?:[-*#>]\s*)*[*`\"'\s]*(?:status|result|outcome|evaluation)[*`\"'\s]*[:=]\s*[*`\"'\s]*(PASS|FAIL)\b",
                        raw_str,
                    )
                    if m_token:
                        verdict = m_token.group(1).upper()

                m_axis_std = re.search(
                    r"(?im)(?:^|\n|\r)\s*(?:[-*#>]\s*)*[*`\"'\s]*(?:axis_standards|axis\s*[aA]\s*(?:\(standards\))?)[*`\"'\s]*[:=]\s*[*`\"'\s]*(PASS|FAIL)\b",
                    raw_str,
                )
                if m_axis_std:
                    axis_standards = m_axis_std.group(1).upper()

                m_axis_spec = re.search(
                    r"(?im)(?:^|\n|\r)\s*(?:[-*#>]\s*)*[*`\"'\s]*(?:axis_spec|axis\s*[bB]\s*(?:\(spec\))?)[*`\"'\s]*[:=]\s*[*`\"'\s]*(PASS|FAIL)\b",
                    raw_str,
                )
                if m_axis_spec:
                    axis_spec = m_axis_spec.group(1).upper()

                if verdict == "PASS" and (axis_standards == "FAIL" or axis_spec == "FAIL"):
                    verdict = "FAIL"

                m_findings = re.search(
                    r"(?im)(?:^|\n|\r)\s*(?:[-*#>]\s*)*[*`\"'\s]*findings[*`\"'\s]*[:=]\s*([^\n\r]+(?:\n\s*[-*#0-9.)].+)*)",
                    raw_str,
                )
                if m_findings:
                    findings = m_findings.group(1).strip()

                m_commits = re.search(
                    r"(?im)(?:^|\n|\r)\s*(?:[-*#>]\s*)*[*`\"'\s]*commits_reviewed[*`\"'\s]*[:=]\s*([^\n\r]+)",
                    raw_str,
                )
                if m_commits:
                    commits_reviewed = [x.strip() for x in m_commits.group(1).split() if x.strip() and x.strip() != "<sha>"]

                m_audit = re.search(
                    r"(?im)(?:^|\n|\r)\s*(?:[-*#>]\s*)*[*`\"'\s]*(?:audit_required|force_audit|audit)[*`\"'\s]*[:=]\s*[*`\"'\s]*(true|yes|1)\b",
                    raw_str,
                )
                if m_audit:
                    audit_required = True

                if not verdict and fallback:
                    fb = self.fallback_scan_verdict(raw_str)
                    if fb in ("PASS", "FAIL"):
                        verdict = fb
                        is_fallback = True
                    else:
                        verdict = "UNRESOLVED"
                elif not verdict:
                    verdict = "UNRESOLVED"

        requires_operator_confirmation = (verdict == "UNRESOLVED")
        operator_prompt = ""
        if requires_operator_confirmation:
            operator_prompt = (
                "> ⚠️ **Unresolved Review Verdict**: The incoming review/audit payload could not be unambiguously "
                "resolved to `PASS` or `FAIL` via standard parsing or regex fallback scanning.\n>\n"
                "> **Raw Report**:\n> ```\n"
                f"{raw_str}\n"
                "> ```\n>\n"
                "> Please confirm the verdict to proceed:\n"
                "> - Confirm PASS: `[Confirm PASS]`\n"
                "> - Confirm FAIL: `[Confirm FAIL]`\n"
            )

        return {
            "verdict": verdict,
            "axis_standards": axis_standards,
            "axis_spec": axis_spec,
            "findings": findings,
            "commits_reviewed": commits_reviewed,
            "is_fallback": is_fallback,
            "audit_required": audit_required,
            "requires_operator_confirmation": requires_operator_confirmation,
            "operator_prompt": operator_prompt,
            "raw_payload": raw_str,
        }

    def build_fix_brief(
        self,
        findings: str | list[Any] | dict[str, Any] | None,
        failing_commits: list[str] | str = "",
        seam: str = "",
        spec_path: str = "",
        delta_spec_path: str = "",
        ticket_id: str | int = "fix",
        conversation_id: str = "",
        branch: str = "",
        as_fenced_block: bool = False,
    ) -> str:
        """
        [FIX-01] Synthesizes a structured Fix Brief for /implement <NN> worker sessions:
        - Downstream 1-line handoff command (/implement fix or /implement <ticket_id>).
        - Spec and Delta context pointers.
        - Failing commit SHAs as Context anchors with optional branch.
        - Target architectural seam requiring remediation.
        - Terminal completion directives enforcing Recommend-and-Stop.
        - Review findings formatted as Given/When/Then acceptance criteria.
        """
        if isinstance(failing_commits, list):
            commits_str = " ".join(str(c).strip() for c in failing_commits if str(c).strip())
        else:
            commits_str = str(failing_commits).strip()

        commits_line = commits_str
        if branch:
            commits_line = f"{commits_str} (branch: {branch})" if commits_str else f"(branch: {branch})"

        cid = (conversation_id or "").strip() or os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "<COMMAND_CENTER_CONVERSATION_ID>").strip()

        findings_list: list[str] = []
        if isinstance(findings, list):
            for f in findings:
                if isinstance(f, dict):
                    for v in f.values():
                        vs = str(v).strip()
                        if vs:
                            findings_list.append(vs)
                elif f is not None:
                    fs = str(f).strip()
                    if fs:
                        findings_list.append(fs)
        elif isinstance(findings, dict):
            for k, v in findings.items():
                vs = str(v).strip()
                if vs:
                    findings_list.append(f"{k}: {vs}" if not vs.startswith(str(k)) else vs)
        elif isinstance(findings, str):
            for line in findings.strip().splitlines():
                clean_l = re.sub(r"^\s*[-*#0-9.)]+\s*", "", line).strip()
                if clean_l and clean_l.lower() not in ("none", '<bulleted summary or "none">', "none.", '""', "''"):
                    findings_list.append(clean_l)
            if not findings_list and findings.strip():
                clean_f = findings.strip()
                if clean_f.lower() not in ("none", '<bulleted summary or "none">', "none.", '""', "''"):
                    findings_list.append(clean_f)

        clean_findings: list[str] = []
        for item in findings_list:
            flat = " ".join(item.split())
            if flat and flat not in clean_findings:
                clean_findings.append(flat)

        if not clean_findings:
            clean_findings = ["Address review failure findings and ensure verification suite passes."]

        commit_ref = commits_str or "<sha>"
        target_seam = seam or "<target/seam>"

        criteria_lines: list[str] = []
        for idx, item in enumerate(clean_findings, start=1):
            if re.search(r"\bGIVEN\b", item, re.IGNORECASE) and re.search(r"\bWHEN\b", item, re.IGNORECASE) and re.search(r"\bTHEN\b", item, re.IGNORECASE):
                criteria_lines.append(f"- [ ] **[FIX-{idx:02d}]**: {item}")
            else:
                criteria_lines.append(
                    f"- [ ] **[FIX-{idx:02d}]**: GIVEN failing review finding '{item}' at commit {commit_ref}, "
                    f"WHEN remediation is implemented at {target_seam}, "
                    f"THEN the issue is resolved and all verification tests pass."
                )

        tid_str = str(ticket_id).strip() if ticket_id is not None else ""
        normalized_tid = f"{int(tid_str):02d}" if tid_str.isdigit() else (tid_str or "fix")

        cmd = f"/implement {normalized_tid}"
        lines = [
            cmd,
            f"Spec:       {spec_path or 'docs/specs/<domain>/spec.md'}",
            f"Delta:      {delta_spec_path or '.scratch/<feature-slug>/spec.md'}",
            f"Commits:    {commits_line or '<sha>'}",
            f"Seam:       {seam or '<target/seam#Section>'}",
            "Reporting:  Execute resolve-ticket.sh or local completion (conclude with Recommend-and-Stop)",
            "",
            "### Acceptance Criteria (Fix Scope)",
            *criteria_lines,
            "",
            "```",
            "CODE_REVIEW_RESULT",
            "verdict: PASS | FAIL",
            "axis_standards: PASS | FAIL",
            "axis_spec: PASS | FAIL",
            "findings: <bulleted summary or \"none\">",
            f"commits_reviewed: {commits_str or '<sha>'}",
            "```",
        ]
        body = "\n".join(lines)
        if as_fenced_block:
            return f"````markdown\n{body}\n````"
        return body

    def synthesize_fix_session_brief(
        self,
        findings: str | list[Any] | dict[str, Any] | None,
        failing_commits: list[str] | str = "",
        seam: str = "",
        spec_path: str = "",
        delta_spec_path: str = "",
        ticket_id: str | int = "fix",
        conversation_id: str = "",
        branch: str = "",
        as_fenced_block: bool = False,
    ) -> str:
        """
        [FIX-01] Synthesizes structured Fix Brief for /implement <NN> worker sessions:
        - Downstream 1-line handoff command (/implement fix or /implement <ticket_id>).
        - Spec and Delta context pointers.
        - Failing commit SHAs as Context anchors with optional branch.
        - Target architectural seam requiring remediation.
        - Terminal Recommend-and-Stop reporting directives without ConnectRPC instructions.
        - Review findings formatted as Given/When/Then acceptance criteria.
        """
        return self.build_fix_brief(
            findings=findings,
            failing_commits=failing_commits,
            seam=seam,
            spec_path=spec_path,
            delta_spec_path=delta_spec_path,
            ticket_id=ticket_id,
            conversation_id=conversation_id,
            branch=branch,
            as_fenced_block=as_fenced_block,
        )

    def evaluate_circuit_breaker(
        self,
        session_anchor: dict[str, Any] | Path | str | None,
        verdict_payload: dict[str, Any] | str,
        max_loops: int = 2,
        failing_phase: str | None = None,
    ) -> tuple[bool, str]:
        """
        [FIX-02] Evaluates fix loop circuit breaker escalation:
        - If fixLoopCount > max_loops for the same evaluation phase and inbound signal is verdict: FAIL,
          the Command Center trips the circuit breaker, halts automated fix dispatch, and emits a high-priority
          diagnostic alert in chat requesting operator intervention.
        - Returns (is_tripped, diagnostic_alert).
        """
        anchor_data: dict[str, Any] = {}
        if isinstance(session_anchor, (str, Path)):
            try:
                read_data = self.read_session_anchor(session_anchor)
                if isinstance(read_data, dict):
                    anchor_data = read_data
            except Exception:
                anchor_data = {}
        elif isinstance(session_anchor, dict):
            anchor_data = session_anchor

        raw_flc = anchor_data.get("fixLoopCount", 0)
        try:
            curr_loops = max(0, int(raw_flc))
        except (ValueError, TypeError):
            curr_loops = 0

        try:
            eff_max_loops = max(0, int(max_loops))
        except (ValueError, TypeError):
            eff_max_loops = 2

        parsed = self.parse_verdict_signal(verdict_payload, fallback=True)
        verdict = parsed.get("verdict", "")
        if verdict != "FAIL":
            return False, ""

        if curr_loops <= eff_max_loops:
            return False, ""

        # Architectural constraint: 'fix' is remediation, not an evaluation phase
        raw_phase = str((failing_phase or anchor_data.get("activePhase") or "").strip())
        active_phase = raw_phase if raw_phase and raw_phase != "fix" else "code-review"

        findings = parsed.get("findings", "")
        commits = parsed.get("commits_reviewed", [])
        if isinstance(commits, list):
            commits_str = " ".join(str(c).strip() for c in commits if str(c).strip())
        else:
            commits_str = str(commits or "").strip()

        if not commits_str:
            raw_rc = anchor_data.get("reviewedCommits")
            if isinstance(raw_rc, list):
                commits_str = " ".join(str(c).strip() for c in raw_rc if str(c).strip())
            else:
                commits_str = str(raw_rc or "").strip()
        if not commits_str:
            commits_str = "<failing-commits>"

        findings_str = ""
        if isinstance(findings, list):
            findings_str = "; ".join(str(f).strip() for f in findings if str(f).strip())
        elif isinstance(findings, dict):
            findings_str = "; ".join(f"{k}: {v}".strip() for k, v in findings.items() if str(v).strip())
        else:
            findings_str = str(findings or "").strip()

        if not findings_str:
            findings_str = "Continuous evaluation failures detected."

        # RFC 7763 / GitHub Alert invariant: preserve blockquote prefix across multiline findings
        if "\n" in findings_str:
            formatted_findings = "\n>   " + "\n>   ".join(line.strip() for line in findings_str.splitlines() if line.strip())
        else:
            formatted_findings = findings_str

        alert_message = (
            f"> [!CAUTION] Circuit Breaker Tripped: Maximum Fix Loops Exceeded\n"
            f">\n"
            f"> Automated fix loop dispatch has been halted after reaching {curr_loops} fix attempts (threshold: {eff_max_loops}).\n"
            f"> Continuous failure detected during evaluation phase: `{active_phase}`.\n"
            f">\n"
            f"> **Diagnostic Summary**:\n"
            f"> - Phase: `{active_phase}`\n"
            f"> - Fix Loop Count: {curr_loops} (max allowed: {eff_max_loops})\n"
            f"> - Failing Commits: `{commits_str}`\n"
            f"> - Findings: {formatted_findings}\n"
            f">\n"
            f"> Operator intervention is required. Please inspect the workspace, resolve the underlying regression or test flakiness, "
            f"and manually re-trigger `/code-review` or `/implement`."
        )
        return True, alert_message

    def build_pr_brief(
        self,
        session_anchor: dict[str, Any] | Path | str | None = None,
        branch: str | None = None,
        commits: list[str] | str | None = None,
        spec_path: str = "",
        delta_spec_path: str = "",
        seam: str = "",
        conversation_id: str = "",
        as_fenced_block: bool = False,
    ) -> str:
        """
        [ROUTE-01] Synthesizes the Structured Session Brief for /pr phase with target spec,
        reviewed commits, and verification summary.
        """
        anchor_data: dict[str, Any] = {}
        if isinstance(session_anchor, (str, Path)):
            try:
                read_data = self.read_session_anchor(session_anchor)
                if isinstance(read_data, dict):
                    anchor_data = read_data
            except Exception:
                anchor_data = {}
        elif isinstance(session_anchor, dict):
            anchor_data = session_anchor

        effective_commits = commits if commits is not None and commits != "" and commits != [] else anchor_data.get("reviewedCommits", "")
        if isinstance(effective_commits, list):
            commits_str = " ".join(str(c).strip() for c in effective_commits if str(c).strip())
        else:
            commits_str = str(effective_commits or "").strip()

        effective_branch = (branch or "").strip() or str(anchor_data.get("branch", "")).strip()
        commits_line = commits_str
        if effective_branch:
            commits_line = f"{commits_str} (branch: {effective_branch})" if commits_str else f"(branch: {effective_branch})"

        cid = (conversation_id or "").strip() or os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip() or "<COMMAND_CENTER_CONVERSATION_ID>"

        feature_slug = str(anchor_data.get("feature", "") or anchor_data.get("featureSlug", "")).strip()
        eff_spec = spec_path or str(anchor_data.get("specPath", "")).strip() or "docs/specs/<domain>/spec.md"
        eff_delta = delta_spec_path or str(anchor_data.get("deltaSpecPath", "")).strip()
        if not eff_delta:
            eff_delta = f".scratch/{feature_slug}/spec.md" if feature_slug else ".scratch/<feature-slug>/spec.md"

        eff_seam = seam or str(anchor_data.get("seam", "")).strip() or "skills/pr/SKILL.md"

        lines = [
            "/pr",
            f"Spec:       {eff_spec}",
            f"Delta:      {eff_delta}",
            f"Commits:    {commits_line or '<sha>'}",
            f"Seam:       {eff_seam}",
            "Reporting:  Local completion / PR submission (conclude with Recommend-and-Stop)",
            "",
            "### Verification Summary",
            "- All post-implementation quality gates passed (Code Review & Verification Suite: PASSED)",
            f"- Commits verified: {commits_str or '<sha>'}",
            "",
            "```",
            "PR_RESULT",
            "status: MERGED | CLOSED | OPEN",
            "pr_url: <github-pr-url or \"none\">",
            "```",
        ]
        body = "\n".join(lines)
        if as_fenced_block:
            return f"````markdown\n{body}\n````"
        return body

    def validate_fix_brief_invariants(self, brief_content: str) -> tuple[bool, list[str]]:
        """
        [FIX-01] Validates Fix Brief invariants:
        - Must contain 1-line handoff command (/implement fix or /implement <ticket_id>).
        - Must contain Spec, Delta, Commits, Seam context pointers.
        - Must contain Report-Back instructions enforcing Recommend-and-Stop.
        - Must contain review findings formatted as Given/When/Then acceptance criteria.
        """
        errors: list[str] = []
        if not re.search(r"^/implement\s+\S+", brief_content, re.MULTILINE):
            errors.append("Fix brief missing 1-line '/implement <ticket>' handoff command")

        if not re.search(r"Spec:\s*\S+", brief_content, re.IGNORECASE):
            errors.append("Fix brief missing Spec context pointer")

        if not re.search(r"Delta:\s*\S+", brief_content, re.IGNORECASE):
            errors.append("Fix brief missing Delta spec pointer")

        if not re.search(r"Commits:\s*\S+", brief_content, re.IGNORECASE):
            errors.append("Fix brief missing Commits pointer")

        if not re.search(r"Seam:\s*\S+", brief_content, re.IGNORECASE):
            errors.append("Fix brief missing Seam pointer")

        has_given = bool(re.search(r"\bGIVEN\b", brief_content))
        has_when = bool(re.search(r"\bWHEN\b", brief_content))
        has_then = bool(re.search(r"\bTHEN\b", brief_content))
        if not (has_given and has_when and has_then):
            errors.append("Fix brief must format review findings as Given/When/Then acceptance criteria (missing GIVEN, WHEN, or THEN)")

        return len(errors) == 0, errors

    def route_review_failure(
        self,
        payload: str | dict[str, Any],
        tracker_dir_or_feature_dir: Path | str | None = None,
        failing_phase: str = "code-review",
        seam: str = "",
        spec_path: str = "",
        delta_spec_path: str = "",
        ticket_id: str | int = "fix",
        conversation_id: str = "",
        branch: str = "",
        as_fenced_block: bool = False,
    ) -> dict[str, Any]:
        """
        [FIX-01] Processes review or audit failure signal:
        - Reads .scratch/<feature-slug>/session.json and increments fixLoopCount.
        - Persists activePhase: 'fix' and incremented fixLoopCount to session.json.
        - Extracts findings from payload and formats them as Given/When/Then acceptance criteria.
        - Synthesizes structured Fix Brief and outputs 1-line handoff command (/implement fix).
        """
        parsed = self.parse_verdict_signal(payload, fallback=True)
        findings = parsed.get("findings", "")
        effective_commits = parsed.get("commits_reviewed", [])

        anchor = None
        cid = (conversation_id or "").strip()
        resolved_spec = spec_path
        resolved_delta = delta_spec_path
        curr_count = 0
        resolved_tid = ticket_id

        target_file = None
        if tracker_dir_or_feature_dir:
            try:
                target_file = self._resolve_session_anchor_path(tracker_dir_or_feature_dir)
                anchor = self.read_session_anchor(tracker_dir_or_feature_dir)
                if anchor:
                    raw_flc = anchor.get("fixLoopCount", 0)
                    try:
                        curr_count = max(0, int(raw_flc))
                    except (ValueError, TypeError):
                        curr_count = 0
                    if not cid:
                        cid = str(anchor.get("commandCenterConversationId", "")).strip()
                    if not effective_commits and anchor.get("reviewedCommits"):
                        effective_commits = list(anchor.get("reviewedCommits", []))
                    if not resolved_tid or str(resolved_tid) == "fix":
                        resolved_tid = anchor.get("activeTicket") or anchor.get("ticketId") or resolved_tid
            except Exception:
                pass

            if target_file and target_file.is_file() and not anchor:
                try:
                    raw_data = json.loads(target_file.read_text(encoding="utf-8"))
                    if isinstance(raw_data, dict):
                        raw_flc = raw_data.get("fixLoopCount", 0)
                        try:
                            curr_count = max(0, int(raw_flc))
                        except (ValueError, TypeError):
                            curr_count = 0
                        if not cid:
                            cid = str(raw_data.get("commandCenterConversationId", "")).strip()
                        if not effective_commits and raw_data.get("reviewedCommits"):
                            effective_commits = list(raw_data.get("reviewedCommits", []))
                        if not resolved_tid or str(resolved_tid) == "fix":
                            resolved_tid = raw_data.get("activeTicket") or raw_data.get("ticketId") or resolved_tid
                except Exception:
                    pass

            base_path = Path(tracker_dir_or_feature_dir)
            feature_dir = base_path.parent if base_path.name == "issues" else (base_path if base_path.is_dir() else base_path.parent)
            feature_slug = feature_dir.name if feature_dir else ""
            if not resolved_delta:
                if feature_dir and (feature_dir / "spec.md").is_file():
                    resolved_delta = str(feature_dir / "spec.md")
                elif feature_slug:
                    resolved_delta = f".scratch/{feature_slug}/spec.md"

            if not resolved_spec:
                tickets = self.scan_tickets_from_tracker(tracker_dir_or_feature_dir)
                spec_anchors: list[str] = []
                for t in tickets:
                    c = t.get("context", "").strip()
                    if c and c not in spec_anchors:
                        spec_anchors.append(c)
                if spec_anchors:
                    resolved_spec = "; ".join(spec_anchors)

        # [FIX-02] Circuit breaker trips when fixLoopCount > max_loops to halt runaway loops and alert operator.
        # Ensure evaluate_circuit_breaker evaluates against failing evaluation phase rather than remediation phase ("fix").
        breaker_anchor = dict(anchor) if isinstance(anchor, dict) else {}
        breaker_anchor.setdefault("fixLoopCount", curr_count)
        breaker_anchor["activePhase"] = failing_phase
        if effective_commits and not breaker_anchor.get("reviewedCommits"):
            breaker_anchor["reviewedCommits"] = effective_commits

        tripped, alert = self.evaluate_circuit_breaker(
            session_anchor=breaker_anchor,
            verdict_payload=payload,
            max_loops=2,
            failing_phase=failing_phase,
        )

        if tripped:
            if tracker_dir_or_feature_dir:
                try:
                    self.update_session_anchor(
                        tracker_dir_or_feature_dir,
                        active_phase="escalation",
                        failing_phase=failing_phase,
                        reviewed_commits=effective_commits if effective_commits else None,
                    )
                    self.rescan_and_refresh_dashboard(tracker_dir_or_feature_dir)
                except Exception:
                    if target_file and target_file.is_file():
                        try:
                            raw_data = json.loads(target_file.read_text(encoding="utf-8"))
                            if isinstance(raw_data, dict):
                                raw_data["activePhase"] = "escalation"
                                raw_data["failingPhase"] = failing_phase
                                target_file.write_text(json.dumps(raw_data, indent=2) + "\n", encoding="utf-8")
                        except Exception:
                            pass
            return {
                "next_phase": "escalation",
                "active_phase": "escalation",
                "circuit_breaker_tripped": True,
                "fix_loop_count": curr_count,
                "diagnostic_alert": alert,
                "brief": alert,
                "handoff_command": None,
                "failing_phase": failing_phase,
                "failing_commits": effective_commits,
                "findings": findings,
            }

        new_count = curr_count + 1
        if tracker_dir_or_feature_dir:
            try:
                self.update_session_anchor(
                    tracker_dir_or_feature_dir,
                    active_phase="fix",
                    failing_phase=failing_phase,
                    remediation_scope=findings,
                    fix_loop_count=new_count,
                    reviewed_commits=effective_commits if effective_commits else None,
                )
                self.rescan_and_refresh_dashboard(tracker_dir_or_feature_dir)
            except Exception:
                if target_file and target_file.is_file():
                    try:
                        raw_data = json.loads(target_file.read_text(encoding="utf-8"))
                        if isinstance(raw_data, dict):
                            raw_data["activePhase"] = "fix"
                            raw_data["failingPhase"] = failing_phase
                            raw_data["remediationScope"] = findings
                            raw_data["fixLoopCount"] = new_count
                            if effective_commits:
                                raw_data["reviewedCommits"] = effective_commits
                            target_file.write_text(json.dumps(raw_data, indent=2) + "\n", encoding="utf-8")
                    except Exception:
                        pass

        if not cid:
            cid = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "<COMMAND_CENTER_CONVERSATION_ID>").strip()

        brief = self.build_fix_brief(
            findings=findings,
            failing_commits=effective_commits,
            seam=seam or "skills/to-tickets/SKILL.md#Step 5",
            spec_path=resolved_spec,
            delta_spec_path=resolved_delta,
            ticket_id=resolved_tid,
            conversation_id=cid,
            branch=branch,
            as_fenced_block=as_fenced_block,
        )

        tid_str = str(resolved_tid).strip() if resolved_tid is not None else ""
        normalized_cmd_tid = f"{int(tid_str):02d}" if tid_str.isdigit() else (tid_str or "fix")

        return {
            "next_phase": "fix",
            "active_phase": "fix",
            "circuit_breaker_tripped": False,
            "fix_loop_count": new_count,
            "brief": brief,
            "handoff_command": f"/implement {normalized_cmd_tid}",
            "failing_phase": failing_phase,
            "failing_commits": effective_commits,
            "findings": findings,
        }

    def route_code_review_verdict(
        self,
        payload: str | dict[str, Any],
        tracker_dir_or_feature_dir: Path | str | None = None,
        modified_files: list[str] | str | None = None,
        commits: list[str] | str | None = None,
        branch: str = "",
        spec_path: str = "",
        delta_spec_path: str = "",
        conversation_id: str = "",
        repo_path: Path | str | None = None,
        ticket_id: str | int = "fix",
        as_fenced_block: bool = False,
        force_audit: bool = False,
        audit: bool = False,
        manual_override: bool = False,
        cli_args: list[str] | str | None = None,
        cli_flags: list[str] | str | None = None,
        flags: list[str] | str | None = None,
    ) -> dict[str, Any]:
        """
        [ROUTE-01][ROUTE-03][ROUTE-04] Phase Routing State Machine evaluation on code-review verdict:
        - Parses verdict (PASS | FAIL) with strict two-axis validation.
        - Checks explicit manual overrides (audit_required: true, --audit, --force-audit) ([ROUTE-04]).
        - If PASS: checks Trigger H attack surface predicate on modified files/diff.
          - Emits single-line blockquote audit trail log to chat ([ROUTE-03]).
          - If attack surface files modified or override requested: transitions activePhase to 'security-audit'
            and outputs Structured Session Brief for /security-audit ([BRIEF-02]).
          - If no attack surface files modified: transitions activePhase to 'pr'
            and outputs Structured Session Brief for /pr ([ROUTE-01]).
        - If FAIL (or if either review axis failed): transitions activePhase to 'fix'.
        Updates session.json on disk if tracker_dir_or_feature_dir is provided.
        """
        if not payload:
            raise ValueError("route_code_review_verdict requires a non-empty payload")

        parsed = self.parse_verdict_signal(payload, fallback=True)
        verdict = parsed["verdict"]
        commits_reviewed = parsed["commits_reviewed"]

        if verdict not in ("PASS", "FAIL"):
            raise ValueError(f"Payload missing valid verdict (PASS | FAIL): {payload}")

        flag_sources: list[str] = []
        if isinstance(cli_args, list):
            flag_sources.extend(str(x) for x in cli_args)
        elif isinstance(cli_args, str):
            flag_sources.extend(cli_args.split())

        if isinstance(cli_flags, list):
            flag_sources.extend(str(x) for x in cli_flags)
        elif isinstance(cli_flags, str):
            flag_sources.extend(cli_flags.split())

        if isinstance(flags, list):
            flag_sources.extend(str(x) for x in flags)
        elif isinstance(flags, str):
            flag_sources.extend(flags.split())

        has_cli_override = any(arg in ("--audit", "--force-audit") for arg in flag_sources)
        if not has_cli_override and any(arg in ("--audit", "--force-audit") for arg in sys.argv):
            has_cli_override = True

        effective_override = bool(
            force_audit
            or audit
            or manual_override
            or parsed.get("audit_required", False)
            or has_cli_override
        )

        effective_commits: list[str] = []
        if commits:
            if isinstance(commits, list):
                effective_commits = [str(c).strip() for c in commits if str(c).strip()]
            else:
                effective_commits = [x.strip() for x in str(commits).split() if x.strip()]
        elif commits_reviewed:
            effective_commits = commits_reviewed

        anchor = None
        cid = (conversation_id or "").strip()
        resolved_spec = spec_path
        resolved_delta = delta_spec_path

        if tracker_dir_or_feature_dir:
            base_path = Path(tracker_dir_or_feature_dir)
            feature_dir = base_path.parent if base_path.name == "issues" else (base_path if base_path.is_dir() else base_path.parent)
            feature_slug = feature_dir.name if feature_dir else ""
            if not resolved_delta:
                if feature_dir and (feature_dir / "spec.md").is_file():
                    resolved_delta = str(feature_dir / "spec.md")
                elif feature_slug:
                    resolved_delta = f".scratch/{feature_slug}/spec.md"

            try:
                anchor = self.read_session_anchor(tracker_dir_or_feature_dir)
                if anchor:
                    if not cid:
                        cid = str(anchor.get("commandCenterConversationId", "")).strip()
                    if not effective_commits and anchor.get("reviewedCommits"):
                        effective_commits = list(anchor.get("reviewedCommits", []))
            except Exception:
                pass

            if not resolved_spec:
                tickets = self.scan_tickets_from_tracker(tracker_dir_or_feature_dir)
                spec_anchors: list[str] = []
                for t in tickets:
                    c = t.get("context", "").strip()
                    if c and c not in spec_anchors:
                        spec_anchors.append(c)
                if spec_anchors:
                    resolved_spec = "; ".join(spec_anchors)

        if not cid:
            cid = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "<COMMAND_CENTER_CONVERSATION_ID>").strip()

        trigger_matched, attack_surfaces = False, []
        effective_repo = repo_path
        if not effective_repo and tracker_dir_or_feature_dir:
            effective_repo = tracker_dir_or_feature_dir

        if modified_files is not None:
            trigger_matched, attack_surfaces = self.evaluate_trigger_h(
                modified_files,
                repo_path=effective_repo,
                manual_override=effective_override,
            )
        elif effective_override:
            trigger_matched, attack_surfaces = self.evaluate_trigger_h(
                None,
                repo_path=effective_repo,
                manual_override=True,
            )

        if verdict == "PASS":
            if trigger_matched:
                next_phase = "security-audit"
                active_phase = "security-audit"
                brief = self.build_security_audit_brief(
                    spec_path=resolved_spec,
                    delta_spec_path=resolved_delta,
                    commits=effective_commits,
                    branch=branch,
                    attack_surfaces=attack_surfaces,
                    conversation_id=cid,
                    as_fenced_block=as_fenced_block,
                )
            else:
                next_phase = "pr"
                active_phase = "pr"
                brief = self.build_pr_brief(
                    session_anchor=anchor,
                    branch=branch,
                    commits=effective_commits,
                    spec_path=resolved_spec,
                    delta_spec_path=resolved_delta,
                    seam="skills/pr/SKILL.md",
                    conversation_id=cid,
                    as_fenced_block=as_fenced_block,
                )
            audit_trail_log = self.format_security_audit_decision_log(
                trigger_matched=trigger_matched,
                attack_surfaces=attack_surfaces,
                manual_override=effective_override,
            )
            chat_response = f"{audit_trail_log}\n\n{brief}"
            circuit_breaker_tripped = False
            diagnostic_alert = ""
            handoff_command = None
        else:
            fix_res = self.route_review_failure(
                payload=payload,
                tracker_dir_or_feature_dir=tracker_dir_or_feature_dir,
                failing_phase="code-review",
                seam="skills/to-tickets/SKILL.md#Step 5",
                spec_path=resolved_spec,
                delta_spec_path=resolved_delta,
                ticket_id=ticket_id,
                conversation_id=cid,
                branch=branch,
                as_fenced_block=as_fenced_block,
            )
            next_phase = fix_res["next_phase"]
            active_phase = fix_res["active_phase"]
            brief = fix_res["brief"]
            audit_trail_log = ""
            chat_response = brief
            circuit_breaker_tripped = fix_res.get("circuit_breaker_tripped", False)
            diagnostic_alert = fix_res.get("diagnostic_alert", "")
            handoff_command = fix_res.get("handoff_command")

        if verdict == "PASS" and tracker_dir_or_feature_dir and anchor:
            try:
                self.update_session_anchor(
                    tracker_dir_or_feature_dir,
                    active_phase=active_phase,
                    reviewed_commits=effective_commits,
                    audit_bypassed=not trigger_matched,
                )
                self.rescan_and_refresh_dashboard(tracker_dir_or_feature_dir)
            except Exception:
                pass

        ret_val = {
            "verdict": verdict,
            "next_phase": next_phase,
            "active_phase": active_phase,
            "brief": brief,
            "trigger_h_matched": trigger_matched,
            "attack_surfaces": attack_surfaces,
            "circuit_breaker_tripped": circuit_breaker_tripped,
            "diagnostic_alert": diagnostic_alert,
            "handoff_command": handoff_command,
            "audit_trail_log": audit_trail_log,
            "chat_response": chat_response,
            "manual_override": effective_override,
        }
        if verdict != "PASS":
            ret_val["fix_loop_count"] = fix_res.get("fix_loop_count", 1)
            ret_val["failing_phase"] = fix_res.get("failing_phase", "code-review")
            ret_val["findings"] = fix_res.get("findings", "")
        return ret_val
    @staticmethod
    def _is_targeted_verification(cmd: str) -> bool:
        cmd_clean = cmd.strip()
        if not cmd_clean:
            return False

        # Whole-repository build and plugin integrity scripts are never targeted test commands
        if re.search(r"\b(?:verify-build\.sh|verify-plugin-integrity\.sh)\b", cmd_clean):
            return False

        # Strip python interpreter invocation so interpreter -m switch is not confused with test runner filter flags
        cmd_no_py = re.sub(
            r"^\s*python[0-9.]*\s+(?:-[a-zA-Z0-9]+\s+)*-m\s+(?:unittest|pytest)\b",
            "",
            cmd_clean,
        )

        has_filter_flag = bool(
            re.search(r"(?:^|\s)(?:-t\b|--testNamePattern\b|-k\b|--grep\b|--filter\b|-run\b)", cmd_no_py)
            or re.search(r"(?:^|\s)-m\s+['\"]?[a-zA-Z0-9_]+", cmd_no_py)
        )

        has_test_target = bool(
            re.search(
                r"(\btests?/[\w./-]+\.(py|ts|js|tsx|jsx|sh)\b|\b[\w.-]+\.(test|spec)\.(ts|js|tsx|jsx)\b|\btest_\w+\.py\b|\b[\w_]+_test\.go\b|\btests\.[a-zA-Z0-9_.]+\b)",
                cmd_clean,
            )
        )

        # Explicitly ban bare broad runner invocations without target files or selectors
        bare_broad_patterns = [
            r"^(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|check)(?:\s+--)?$",
            r"^(?:npx\s+)?(?:vitest|jest)(?:\s+run)?$",
            r"^(?:pytest|py\.test)(?:\s+-[a-zA-Z0-9]+)*$",
            r"^(?:python[0-9.]*\s+)?-m\s+pytest(?:\s+-[a-zA-Z0-9]+)*$",
            r"^(?:python[0-9.]*\s+)?-m\s+unittest(?:\s+(?:discover|-[a-zA-Z0-9]+))*$",
            r"^cargo\s+test(?:\s+--(?:all|workspace))?$",
            r"^go\s+test(?:\s+\./\.\.\.)?$",
        ]
        for pattern in bare_broad_patterns:
            if re.search(pattern, cmd_clean):
                return False

        return has_filter_flag or has_test_target

    @staticmethod
    def _format_ticket_rejection_diagnostic(errors: list[str]) -> str:
        lines = [
            "🛑 PRE-FLIGHT TICKET REJECTION: Ticket execution contracts are missing or incomplete.",
            "Violations:",
        ]
        for err in errors:
            lines.append(f"  - {err}")
        lines.extend([
            "",
            "Action Required:",
            "  Immediately HALT execution. Do NOT explore the codebase or execute test runners in the primary session.",
            "  Regenerate this ticket via '/to-tickets' to ensure complete, concrete execution contracts.",
        ])
        return "\n".join(lines)

    def validate_ticket_preflight(
        self,
        ticket_input: Path | str | dict[str, str],
        raise_on_error: bool = False,
    ) -> tuple[int, list[str], str]:
        """
        [REJ-01] [REJ-02] Pre-Flight Ticket Rejection Gate:
        Deterministically validates ticket execution contracts upon ingestion:
        1. Context: Must include exact line range anchors ('#Lxx-Lyy' or '#Lxx').
        2. Seam: Must point to an exact file and symbol ('#symbol'), strictly prohibiting broad directories.
        3. Verification: Must specify a targeted test command (with targeted test file or '-t'/'-k' filter flag),
           strictly prohibiting broad repository test suites ('npm test', 'pytest', './scripts/verify-build.sh' alone).
        4. Acceptance Criteria: Must declare at least one Given/When/Then scenario or tagged BDD scenario ('[TAG-01]').

        If any contract is missing or ambiguous, halts with status 1 and returns a structured rejection
        diagnostic directing the operator to run /to-tickets.
        If all contracts are present and concrete, clears with status 0, empty errors, and proceeds.
        """
        meta: dict[str, str] = {}
        ticket_name = "ticket"

        if isinstance(ticket_input, dict):
            meta = dict(ticket_input)
            ticket_name = meta.get("title", "ticket")
        elif isinstance(ticket_input, Path):
            ticket_name = ticket_input.name
            if not ticket_input.is_file():
                errors = [f"Ticket file does not exist: {ticket_input}"]
                diagnostic = self._format_ticket_rejection_diagnostic(errors)
                if raise_on_error:
                    raise TicketRejectionError(diagnostic)
                return 1, errors, diagnostic
            meta = self._parse_ticket_markdown(ticket_input.read_text(encoding="utf-8"), ticket_name)
        elif isinstance(ticket_input, str):
            if "\n" in ticket_input or ticket_input.strip().startswith("#"):
                meta = self._parse_ticket_markdown(ticket_input, "ticket")
            else:
                p = Path(ticket_input)
                if p.is_file():
                    ticket_name = p.name
                    meta = self._parse_ticket_markdown(p.read_text(encoding="utf-8"), ticket_name)
                else:
                    meta = self._parse_ticket_markdown(ticket_input, "ticket")
        else:
            errors = [f"Invalid ticket input type: {type(ticket_input)}"]
            diagnostic = self._format_ticket_rejection_diagnostic(errors)
            if raise_on_error:
                raise TicketRejectionError(diagnostic)
            return 1, errors, diagnostic

        errors: list[str] = []

        # [REJ-01] Require line anchors to prevent amnesia during context compaction
        context_val = meta.get("Context", "").strip()
        if not context_val:
            errors.append("Context: missing required execution key '**Context:**'")
        elif "#L" not in context_val or not re.search(r"#L\d+(?:-L?\d+)?", context_val):
            errors.append(
                f"Context: missing exact line range anchor ('#Lxx-Lyy') in '{context_val}'. "
                "Context must anchor to specific specification lines."
            )
        else:
            context_file = context_val.split("#", 1)[0].strip()
            context_file = re.sub(r"^\[.*?\]\(", "", context_file).strip()
            context_file = context_file.strip("`'\"")
            if not context_file or context_file.endswith("/") or ("\\" in context_file and context_file.endswith("\\")):
                errors.append(
                    f"Context: '{context_val}' lacks a concrete specification file path before line anchor. "
                    "Context must specify a concrete specification file ('spec.md#Lxx-Lyy')."
                )

        # [REJ-01] Prohibit broad directories to enforce tracer-bullet execution at concrete seams
        seam_val = meta.get("Seam", "").strip()
        if not seam_val:
            errors.append("Seam: missing required execution key '**Seam:**'")
        elif "#" not in seam_val:
            errors.append(
                f"Seam: '{seam_val}' lacks concrete symbol anchor ('path/to/file.ext#symbol'). "
                "Broad directories or ambiguous module paths are strictly prohibited."
            )
        else:
            file_part, symbol_part = seam_val.split("#", 1)
            file_part = file_part.strip().strip("`'\"")
            symbol_part = symbol_part.strip().strip("`'\"")
            is_broad_dir = (
                not file_part
                or file_part.endswith("/")
                or ("\\" in file_part and file_part.endswith("\\"))
            )
            if is_broad_dir:
                errors.append(
                    f"Seam: '{seam_val}' specifies a broad directory or lacks concrete file path ('path/to/file.ext#symbol'). "
                    "Broad directories or ambiguous module paths are strictly prohibited."
                )
            elif not symbol_part:
                errors.append(
                    f"Seam: '{seam_val}' lacks concrete symbol anchor after '#' ('path/to/file.ext#symbol'). "
                    "Every seam must identify a concrete export or structural entrypoint."
                )

        # [REJ-01] Bar whole-repo runners to prevent context window log flooding
        verification_val = meta.get("Verification", "").strip()
        if not verification_val:
            errors.append("Verification: missing required execution key '**Verification:**'")
        else:
            if not self._is_targeted_verification(verification_val):
                errors.append(
                    f"Verification: '{verification_val}' is a broad repository test command. "
                    "Verification must specify a targeted test file or filter flag ('-t' or '-k')."
                )

        # [REJ-01] Strip list checkboxes so [X] cannot masquerade as BDD scenario tags
        ac_val = meta.get("Acceptance Criteria", "").strip()
        if not ac_val:
            errors.append("Acceptance Criteria: missing required '## Acceptance Criteria' section or content")
        else:
            ac_without_checkboxes = re.sub(r"^[ \t]*[-*]\s*\[[ xX+-]\]\s*", "", ac_val, flags=re.MULTILINE)
            has_bdd_tag = bool(re.search(r"\[[A-Z0-9_]+-[A-Z0-9_]+\]", ac_without_checkboxes))
            has_gwt = bool(re.search(r"(?i)\bgiven\b.*\bwhen\b.*\bthen\b", ac_without_checkboxes, re.DOTALL))
            if not (has_bdd_tag or has_gwt):
                errors.append(
                    "Acceptance Criteria: missing Given/When/Then scenario or tagged BDD scenario ([TAG-01]). "
                    "Every ticket must specify at least one verifiable behavioral scenario."
                )

        if errors:
            diagnostic = self._format_ticket_rejection_diagnostic(errors)
            if raise_on_error:
                raise TicketRejectionError(diagnostic)
            return 1, errors, diagnostic

        return 0, [], ""

    def synthesize_subagent_brief(self, ticket_path: Path, spec_root: Path | None = None) -> str:
        """
        [ING-02] Autonomous execution metadata extraction from ticket:
        Extracts Context, Seam, Verification, and Guardrails directly from ticket key-values,
        inspects referenced spec lines via read_file semantics, and constructs the 6-field
        Structured Subagent Brief for spec-implementer internally without asking the operator
        to provide context strings.
        """
        meta = self.extract_ticket_metadata(ticket_path)
        title = meta.get("title", ticket_path.stem)
        context_ref = meta["Context"]
        seam = meta["Seam"]
        verification = meta["Verification"]
        guardrails = meta["Guardrails"]

        spec_path, spec_excerpt = self.inspect_spec_context(
            context_ref, spec_root=spec_root, base_dir=ticket_path.parent
        )

        spec_pointers = [f"- Ticket: {ticket_path.name}", f"- Spec Section: {context_ref}"]
        if spec_path and spec_excerpt:
            spec_pointers.append(f"- Spec Context ({spec_path.name}):\n" + "\n".join(f"  {l}" for l in spec_excerpt.splitlines()))
        spec_pointers.append(f"- Seam: {seam}")

        blocked_by = meta.get("Blocked by")
        if blocked_by and blocked_by.lower() not in ("none", "none (frontier - can start immediately)"):
            spec_pointers.append(f"- Blocked by: {blocked_by}")

        pointers_block = "\n".join(spec_pointers)

        brief = f"""<role_and_objective>
Implement ticket {title} adhering strictly to agents/spec-implementer.md and the vertical slice TDD Red-Green-Refactor loop.
</role_and_objective>

<context_pointers>
{pointers_block}
</context_pointers>

<architectural_seam>
{seam}
</architectural_seam>

<verification_gate>
{verification}
</verification_gate>

<strict_guardrails>
{guardrails}
- Zero tolerance on any and blind type assertions.
- Anti-lazy-truncation: Full file replacements only.
- Host environment invariant: Execute terminal commands with BypassSandbox: true.
</strict_guardrails>

<completion_protocol>
Upon completing TDD and compiler verification, report back via send_message adhering strictly to the Skeptical Reporting Protocol. Include the mandatory Skepticism Disclaimer and 3-Tier Verification Record (Deep, Shallow, Unverified).
</completion_protocol>"""
        return brief

    def render_task_dashboard(
        self,
        tickets: list[dict | Path | str],
        active_frontier: list[str | int],
        feature_slug: str | None = None,
        phase_overrides: dict[int | str, str] | None = None,
        session_anchor: dict[str, Any] | Path | str | None = None,
        remediation_subrow: dict[str, Any] | None = None,
        deliverable_overrides: dict[int | str, str] | None = None,
        *,
        feature_title: str | None = None,
        mission: str | None = None,
        include_mermaid: bool = False,
    ) -> str:
        """
        [DASH-01] [DASH-03] [COCKPIT-01] [COCKPIT-02] [LOOP-01] [LOOP-02] [MOD-COCKPIT-01] [LINK-COCKPIT-02] Renders the Modular Four-Stage Task Graph Cockpit:
        - Top-Level Mission Brief header documenting feature objective and current active stage.
        - Stage I: Inception & Architecture (Command Center) - Phases 1-3.
        - Stage II: Implementation Swarm - Progress counter, Active Frontier with relative links, and 5-column ticket table.
        - Stage III: Verification & Delivery (Evaluation Pipeline) - Phases 5-7 with optional Security Audit note.
        - Stage IV: Retrospective - Phase 8.
        - Prunes fragile Mermaid `graph TD` block by default (unless include_mermaid=True).
        """
        resolved_title = feature_title
        resolved_mission = mission

        spec_path = None
        if feature_slug:
            candidate_specs = [
                Path(f".scratch/{feature_slug}/spec.md"),
                Path(f"docs/specs/{feature_slug}/spec.md"),
            ]
            for cs in candidate_specs:
                if cs.is_file():
                    spec_path = cs
                    break

        if not spec_path and tickets:
            for t in tickets:
                t_p = None
                if isinstance(t, (str, Path)):
                    t_p = Path(t)
                elif isinstance(t, dict) and t.get("path"):
                    t_p = Path(str(t["path"]))
                if t_p and t_p.is_file():
                    parent_spec = t_p.parent.parent / "spec.md"
                    if parent_spec.is_file():
                        spec_path = parent_spec
                        break

        if spec_path and spec_path.is_file():
            try:
                spec_text = spec_path.read_text(encoding="utf-8")
                if not resolved_title:
                    m_title = re.search(r"^#\s+(?:🧭\s*)?(?:Specification:\s*|Task Graph:\s*)?(.*)", spec_text, re.MULTILINE)
                    if m_title:
                        resolved_title = m_title.group(1).strip()
                if not resolved_mission:
                    m_mission = re.search(r"^>\s*\*\*Mission\*\*:\s*(.*)", spec_text, re.MULTILINE | re.IGNORECASE)
                    if not m_mission:
                        m_mission = re.search(r"\*\*Mission\*\*:\s*(.*)", spec_text, re.IGNORECASE)
                    if not m_mission:
                        m_mission = re.search(r"##\s*(?:1\.\s*)?(?:Mission|Objective)\s*\n\s*([^\n#]+)", spec_text, re.IGNORECASE)
                    if m_mission:
                        resolved_mission = m_mission.group(1).strip()
            except Exception:
                pass

        if not resolved_title:
            if feature_slug:
                clean_slug = feature_slug.replace("-", " ").replace("_", " ").title()
                resolved_title = clean_slug
            else:
                resolved_title = "Feature Dashboard"

        if not resolved_mission:
            resolved_mission = f"Deliver {resolved_title} end-to-end adhering to technical specification standards."

        frontier_set: set[str] = set()
        if active_frontier is not None:
            for f in active_frontier:
                f_s = str(f).strip().lstrip("#").lower()
                if f_s.startswith("ticket-"):
                    f_s = f_s[7:].strip()
                if f_s.isdigit():
                    frontier_set.add(f"{int(f_s):02d}")
                frontier_set.add(f_s)
        else:
            calc_f = self.calculate_unblocked_frontier(tickets)
            for f in calc_f:
                f_s = str(f).strip().lstrip("#").lower()
                if f_s.isdigit():
                    frontier_set.add(f"{int(f_s):02d}")
                frontier_set.add(f_s)

        mermaid_lines = ["```mermaid", "graph TD"]
        table_rows = [
            "| Ticket | Title | Blockers | Status | Verified Commit |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]

        if not tickets:
            mermaid_lines.append("  %% Empty task graph - no tickets scheduled")
            table_rows.append("| None | No tasks scheduled | None | complete | - |")

        normalized_tickets: list[dict] = []
        for t in tickets:
            if isinstance(t, Path) or (isinstance(t, str) and (Path(t).is_file() or "/" in t or t.endswith(".md"))):
                t_path = Path(t)
                if t_path.is_file():
                    try:
                        file_content = t_path.read_text(encoding="utf-8")
                        meta = self._parse_ticket_markdown(file_content, t_path.stem)
                        stem = t_path.stem
                        m_id = re.match(r"^([0-9]{1,3})", stem)
                        ticket_id = m_id.group(1) if m_id else stem
                        blockers_raw = meta.get("Blocked by", "")
                        blockers = [b.strip() for b in blockers_raw.split(",") if b.strip()]
                        raw_title = meta.get("title", meta.get("Title", stem))
                        if ":" in raw_title:
                            clean_title = raw_title.split(":", 1)[1].strip()
                        else:
                            clean_title = raw_title
                        commit_val = meta.get("Verified Commit", meta.get("Commit", meta.get("commit", "")))
                        normalized_tickets.append({
                            "id": ticket_id,
                            "title": clean_title,
                            "status": meta.get("Status", meta.get("status", "ready-for-agent")),
                            "blockers": blockers,
                            "commit": commit_val,
                            "path": t_path.as_posix(),
                        })
                        continue
                    except Exception:
                        pass
            if isinstance(t, dict):
                t_dict = dict(t)
                if ("status" not in t_dict or not t_dict["status"]) and "path" in t_dict and t_dict["path"]:
                    t_path = Path(str(t_dict["path"]))
                    if t_path.is_file():
                        try:
                            file_content = t_path.read_text(encoding="utf-8")
                            meta = self._parse_ticket_markdown(file_content, t_path.stem)
                            t_dict["status"] = meta.get("Status", meta.get("status", "ready-for-agent"))
                            if "title" not in t_dict or not t_dict["title"]:
                                raw_title = meta.get("title", meta.get("Title", t_path.stem))
                                if ":" in raw_title:
                                    clean_title = raw_title.split(":", 1)[1].strip()
                                else:
                                    clean_title = raw_title
                                t_dict["title"] = clean_title
                            if "blockers" not in t_dict and "Blocked by" in meta:
                                t_dict["blockers"] = [b.strip() for b in meta["Blocked by"].split(",") if b.strip()]
                            if "commit" not in t_dict and "Verified Commit" in meta:
                                t_dict["commit"] = meta["Verified Commit"]
                        except Exception:
                            pass
                normalized_tickets.append(t_dict)
            else:
                normalized_tickets.append({"id": str(t), "title": str(t), "status": "ready-for-agent", "blockers": [], "commit": ""})

        total_tickets = len(normalized_tickets)
        resolved_count = sum(
            1 for t in normalized_tickets if self._is_status_resolved(str(t.get("status", "")))
        )
        all_resolved = total_tickets > 0 and resolved_count == total_tickets

        # Feature slug resolution enables canonical relative deliverables for ADR 0033
        detected_slug = feature_slug
        if not detected_slug and spec_path:
            p_parts = Path(str(spec_path).replace("\\", "/")).parts
            if ".scratch" in p_parts:
                idx = p_parts.index(".scratch")
                if idx + 1 < len(p_parts):
                    detected_slug = p_parts[idx + 1]
            elif spec_path.parent.name and spec_path.parent.name not in (".", ""):
                detected_slug = spec_path.parent.name

        if not detected_slug:
            for t in normalized_tickets:
                t_p = t.get("path")
                if t_p:
                    p = Path(str(t_p).replace("\\", "/"))
                    parts = p.parts
                    if ".scratch" in parts:
                        idx = parts.index(".scratch")
                        if idx + 1 < len(parts):
                            detected_slug = parts[idx + 1]
                            break
                    elif len(parts) >= 3 and parts[-2] == "issues":
                        detected_slug = parts[-3]
                        break
                    elif len(parts) >= 2 and parts[-2] != "issues":
                        detected_slug = parts[-2]
                        break
        slug_str = detected_slug if detected_slug and detected_slug != "issues" else "<slug>"

        # Resolve session anchor metadata [LOOP-01] [LOOP-02]
        anchor_data: dict[str, Any] = {}
        if isinstance(session_anchor, dict):
            anchor_data = dict(session_anchor)
        elif isinstance(session_anchor, (str, Path)):
            try:
                loaded = self.read_session_anchor(session_anchor)
                if isinstance(loaded, dict):
                    anchor_data = loaded
            except Exception:
                pass
        elif detected_slug:
            try:
                candidate = Path(".scratch") / detected_slug / "session.json"
                if candidate.is_file():
                    loaded = self.read_session_anchor(candidate.parent)
                    if isinstance(loaded, dict):
                        anchor_data = loaded
            except Exception:
                pass

        anchor_active_phase = str(anchor_data.get("activePhase") or anchor_data.get("active_phase") or "").strip().lower()
        anchor_failing_phase = str(anchor_data.get("failingPhase") or anchor_data.get("failing_phase") or "").strip().lower()
        raw_fix_loops = anchor_data.get("fixLoopCount") if anchor_data.get("fixLoopCount") is not None else anchor_data.get("fix_loop_count", 0)
        try:
            anchor_fix_loops = max(0, int(raw_fix_loops))
        except (ValueError, TypeError):
            anchor_fix_loops = 0
        anchor_scope = str(
            anchor_data.get("remediationScope")
            or anchor_data.get("remediation_scope")
            or anchor_data.get("targetFixScope")
            or anchor_data.get("findings")
            or ""
        ).strip()
        anchor_audit_bypassed = bool(anchor_data.get("auditBypassed") or anchor_data.get("audit_bypassed") or False)
        anchor_overrides = anchor_data.get("phaseOverrides") or anchor_data.get("phase_overrides")

        if total_tickets == 0:
            phase_4_status = "🟡 in-progress (0/0 resolved)"
        elif all_resolved:
            phase_4_status = f"✅ completed ({resolved_count}/{total_tickets} resolved)"
        else:
            phase_4_status = f"🟡 in-progress ({resolved_count}/{total_tickets} resolved)"

        phase_5_status = "🟡 in-progress" if all_resolved else "⬜ pending"

        phase_status_map: dict[int, str] = {
            1: "✅ completed",
            2: "✅ completed",
            3: "✅ completed",
            4: phase_4_status,
            5: phase_5_status,
            6: "⬜ pending",
            7: "⬜ pending",
            8: "⬜ pending",
        }

        failing_num = 5
        if any(tok in anchor_failing_phase for tok in ("6", "security", "audit")):
            failing_num = 6
        elif any(tok in anchor_failing_phase for tok in ("5", "code", "review")):
            failing_num = 5
        elif any(tok in anchor_active_phase for tok in ("security", "audit")) or anchor_active_phase in ("6", 6):
            failing_num = 6
        elif anchor_active_phase in ("fix", "escalation"):
            failing_num = 5

        if remediation_subrow and isinstance(remediation_subrow, dict):
            r_ph = remediation_subrow.get("phase") or remediation_subrow.get("failing_phase")
            if r_ph is not None and str(r_ph).lower() in ("6", "security-audit", "security", "audit", "phase-6", "phase 6"):
                failing_num = 6

        is_escalated = anchor_fix_loops > 2 or anchor_active_phase == "escalation"
        n_att = max(1, anchor_fix_loops)

        if is_escalated:
            if failing_num == 6:
                phase_status_map[5] = "✅ completed"
                phase_status_map[6] = "⚠️ escalated-to-human"
            else:
                phase_status_map[5] = "⚠️ escalated-to-human"
                phase_status_map[6] = "⬜ pending"
            phase_status_map[7] = "⬜ pending"
        elif anchor_active_phase == "fix":
            if failing_num == 6:
                phase_status_map[5] = "✅ completed"
                phase_status_map[6] = f"🔄 fix-loop (attempt {n_att}/2)"
                phase_status_map[7] = "⬜ pending"
            else:
                phase_status_map[5] = f"🔄 fix-loop (attempt {n_att}/2)"
                phase_status_map[6] = "⬜ pending"
                phase_status_map[7] = "⬜ pending"
        elif anchor_active_phase in ("security-audit", "security", "audit"):
            phase_status_map[5] = "✅ completed"
            phase_status_map[6] = "🟡 in-progress"
            phase_status_map[7] = "⬜ pending"
        elif anchor_active_phase == "pr":
            phase_status_map[5] = "✅ completed"
            if anchor_audit_bypassed:
                phase_status_map[6] = "⏭️ bypassed (0 attack surface files detected)"
            else:
                phase_status_map[6] = "✅ completed"
            phase_status_map[7] = "🟡 in-progress"

        if remediation_subrow and isinstance(remediation_subrow, dict) and not is_escalated:
            r_ph = remediation_subrow.get("phase") or remediation_subrow.get("failing_phase")
            sub_target = 6 if str(r_ph).lower() in ("6", "security-audit", "security", "audit", "phase-6", "phase 6") else 5
            if sub_target == 6:
                phase_status_map[5] = "✅ completed"
                if "fix-loop" not in phase_status_map[6]:
                    phase_status_map[6] = f"🔄 fix-loop (attempt {n_att}/2)"
            else:
                if "fix-loop" not in phase_status_map[5]:
                    phase_status_map[5] = f"🔄 fix-loop (attempt {n_att}/2)"

        if anchor_overrides and isinstance(anchor_overrides, dict):
            for p_k, p_v in anchor_overrides.items():
                p_n = None
                if isinstance(p_k, int):
                    p_n = p_k
                elif isinstance(p_k, str):
                    m_n = re.search(r"\d+", p_k)
                    if m_n:
                        p_n = int(m_n.group(0))
                if p_n is not None and p_n in phase_status_map:
                    phase_status_map[p_n] = str(p_v).strip()

        if phase_overrides:
            for p_key, p_stat in phase_overrides.items():
                p_num = None
                if isinstance(p_key, int):
                    p_num = p_key
                elif isinstance(p_key, str):
                    m_num = re.search(r"\d+", p_key)
                    if m_num:
                        p_num = int(m_num.group(0))
                if p_num is not None and p_num in phase_status_map:
                    phase_status_map[p_num] = str(p_stat).strip()

        if "bypassed" in phase_status_map[6].lower():
            if phase_status_map[5] in ("🟡 in-progress", "⬜ pending"):
                phase_status_map[5] = "✅ completed"
            if all_resolved and phase_status_map[7] == "⬜ pending":
                phase_status_map[7] = "🟡 in-progress"

        if anchor_fix_loops > 2:
            target_esc_phase = 6 if failing_num == 6 else 5
            phase_status_map[target_esc_phase] = "⚠️ escalated-to-human"

        render_subrow = False
        subrow_phase = failing_num
        subrow_status = "🟡 in-progress"
        subrow_command = "`/implement fix`"
        subrow_scope = anchor_scope

        if remediation_subrow and isinstance(remediation_subrow, dict):
            render_subrow = True
            r_ph = remediation_subrow.get("phase") or remediation_subrow.get("failing_phase")
            if r_ph is not None:
                if str(r_ph).lower() in ("6", "security-audit", "security", "audit", "phase-6", "phase 6"):
                    subrow_phase = 6
                else:
                    subrow_phase = 5
            subrow_status = str(remediation_subrow.get("status", "🟡 in-progress")).strip()
            subrow_command = str(remediation_subrow.get("command", "`/implement fix`")).strip()
            if not subrow_command.startswith("`"):
                subrow_command = f"`{subrow_command}`"
            subrow_scope = str(remediation_subrow.get("scope") or remediation_subrow.get("target_fix_scope") or subrow_scope).strip()
        elif anchor_active_phase == "fix" and not is_escalated:
            render_subrow = True
        elif ("fix-loop" in phase_status_map[5] or "fix-loop" in phase_status_map[6]) and not is_escalated:
            if "fix-loop" in phase_status_map[6] and phase_status_map[6] != "⚠️ escalated-to-human":
                render_subrow = True
                subrow_phase = 6
            elif "fix-loop" in phase_status_map[5] and phase_status_map[5] != "⚠️ escalated-to-human":
                render_subrow = True
                subrow_phase = 5

        if phase_status_map.get(subrow_phase) == "⚠️ escalated-to-human" or is_escalated:
            render_subrow = False

        if subrow_scope:
            clean_scope = re.sub(r"[\r\n]+", " ", subrow_scope).strip().replace("|", "\\|")
            if "target fix scope" in clean_scope.lower():
                deliverable_text = clean_scope
            else:
                deliverable_text = f"Target fix scope: {clean_scope}"
        else:
            deliverable_text = "Target fix scope"

        escaped_subrow_status = subrow_status.replace("|", "\\|")
        escaped_subrow_command = subrow_command.replace("|", "\\|")
        remediation_row = f"| | ↳ Remediation Fix | {escaped_subrow_status} | {escaped_subrow_command} | {deliverable_text} |"

        deliverable_map: dict[int, str] = {
            1: "`GLOSSARY.md`, `docs/adr/`",
            2: f"`.scratch/{slug_str}/spec.md`",
            3: f"`.scratch/{slug_str}/issues/*.md`",
            4: "Verified Commits on HEAD",
            5: f"`.scratch/{slug_str}/code-review.md`",
            6: f"`.scratch/{slug_str}/security-audit.md`",
            7: "Draft PR & Spec Reconciliation",
            8: "Automated Checks & Hooks",
        }
        if deliverable_overrides:
            for d_k, d_v in deliverable_overrides.items():
                d_num = None
                if isinstance(d_k, int):
                    d_num = d_k
                elif isinstance(d_k, str):
                    m_d = re.search(r"\d+", d_k)
                    if m_d:
                        d_num = int(m_d.group(0))
                if d_num is not None and d_num in deliverable_map:
                    deliverable_map[d_num] = str(d_v).strip()

        stage_1_rows = [
            "## Stage I: Inception & Architecture (Command Center)",
            "| # | Phase | Status | Command | Deliverable |",
            "| :-: | :--- | :---: | :--- | :--- |",
            f"| 1 | Inception & Grilling | {phase_status_map[1]} | `/grill-with-docs` | {deliverable_map[1]} |",
            f"| 2 | Living / Delta Spec | {phase_status_map[2]} | `/to-spec` | {deliverable_map[2]} |",
            f"| 3 | Ticket Decomposition | {phase_status_map[3]} | `/to-tickets` | {deliverable_map[3]} |",
        ]
        stage_1_block = "\n".join(stage_1_rows)

        resolved_nodes: list[str] = []
        in_progress_nodes: list[str] = []
        frontier_nodes: list[str] = []
        blocked_nodes: list[str] = []
        link_lines: list[str] = []

        all_ticket_ids: set[str] = set()
        for t in normalized_tickets:
            raw_id = (
                t.get("id")
                if t.get("id") is not None
                else (t.get("number") if t.get("number") is not None else t.get("ticket"))
            )
            if raw_id is not None:
                c_id = str(raw_id).strip().lstrip("#")
                if c_id.lower().startswith("ticket-"):
                    c_id = c_id[7:].strip()
                if c_id.isdigit():
                    c_id = f"{int(c_id):02d}"
                if c_id:
                    all_ticket_ids.add(c_id)

        for t in normalized_tickets:
            raw_id = (
                t.get("id")
                if t.get("id") is not None
                else (t.get("number") if t.get("number") is not None else t.get("ticket"))
            )
            if raw_id is None:
                raw_id = ""
            clean_id = str(raw_id).strip().lstrip("#")
            if clean_id.lower().startswith("ticket-"):
                clean_id = clean_id[7:].strip()
            if clean_id.isdigit():
                clean_id = f"{int(clean_id):02d}"

            title = str(t.get("title", "")).strip()
            status = str(t.get("status", "ready-for-agent")).strip()

            raw_blockers = t.get("blockers")
            if raw_blockers is None:
                raw_blockers = t.get("blocked_by")

            blockers = self._parse_blocker_ids(raw_blockers)

            is_frontier = clean_id.lower() in frontier_set or str(raw_id).strip().lower() in frontier_set

            safe_node_id = re.sub(r"[^a-zA-Z0-9_]", "_", clean_id) if clean_id else "unknown"
            node_var = f"T{safe_node_id}"

            clean_title = title.strip()
            if clean_title.startswith(f"{clean_id}:"):
                clean_title = clean_title.split(":", 1)[1].strip()
            clean_title = re.sub(r"\s*\((?:Resolved|In Progress|Frontier)\)$", "", clean_title, flags=re.IGNORECASE).strip()
            escaped_title = clean_title.replace('"', "#quot;")

            clean_status = status.lower().strip("`* \t")
            if clean_status.startswith("🟡"):
                clean_status = clean_status[1:].strip("`* \t")
            if clean_status.startswith("✅"):
                clean_status = clean_status[1:].strip("`* \t")

            is_resolved = self._is_status_resolved(clean_status)
            is_in_progress = self._is_status_in_progress(clean_status)

            if is_resolved:
                label = f"{clean_id}: {escaped_title} (Resolved)"
                resolved_nodes.append(f'    {node_var}["{label}"]')
            elif is_in_progress:
                label = f"{clean_id}: {escaped_title} (In Progress)"
                in_progress_nodes.append(f'    {node_var}["{label}"]')
            elif is_frontier:
                label = f"{clean_id}: {escaped_title} (Frontier)"
                frontier_nodes.append(f'    {node_var}["{label}"]')
            else:
                label = f"{clean_id}: {escaped_title}"
                blocked_nodes.append(f'    {node_var}["{label}"]')

            formatted_blockers: list[str] = []
            for b in blockers:
                if b in all_ticket_ids and b != clean_id:
                    safe_b_id = re.sub(r"[^a-zA-Z0-9_]", "_", b) if b else "unknown"
                    node_b = f"T{safe_b_id}"
                    link_lines.append(f"  {node_b} --> {node_var}")
                    formatted_blockers.append(b)

            if not formatted_blockers:
                blockers_display = "None (Frontier)" if is_frontier else "None"
            else:
                blockers_display = ", ".join(formatted_blockers)
            escaped_table_title = title.replace("|", "\\|")
            display_status = status

            if is_resolved:
                if not display_status.startswith("✅"):
                    display_status = f"✅ {clean_status}"
            elif is_in_progress and not display_status.startswith("🟡"):
                display_status = f"🟡 {status}"

            commit_val = str(t.get("commit") or t.get("commit_sha") or t.get("verified_commit") or t.get("Verified Commit") or t.get("sha") or "").strip()
            commit_display = commit_val if commit_val else "-"

            escaped_clean_id = clean_id.replace("|", "\\|")
            escaped_display_status = display_status.replace("|", "\\|")
            escaped_commit_display = commit_display.replace("|", "\\|")
            escaped_blockers_display = blockers_display.replace("|", "\\|")

            if t.get("path"):
                clean_p_name = Path(str(t["path"]).replace("\\", "/")).name
                rel_link = f"issues/{clean_p_name}"
                ticket_display = f"[{escaped_clean_id}]({rel_link})"
            else:
                ticket_display = escaped_clean_id

            table_rows.append(f"| {ticket_display} | {escaped_table_title} | {escaped_blockers_display} | {escaped_display_status} | {escaped_commit_display} |")

        subgraphs = [
            ("S_RESOLVED", 'subgraph S_RESOLVED["✅ Completed & Verified"]', resolved_nodes, "empty_resolved"),
            ("S_IN_PROGRESS", 'subgraph S_IN_PROGRESS["🟡 In Progress"]', in_progress_nodes, "empty_in_progress"),
            ("S_FRONTIER", 'subgraph S_FRONTIER["🎯 Active Frontier (Ready to Start)"]', frontier_nodes, "empty_frontier"),
            ("S_BLOCKED", 'subgraph S_BLOCKED["⏳ Blocked (Waiting Dependencies)"]', blocked_nodes, "empty_blocked"),
        ]

        cluster_representatives: list[tuple[str, bool]] = []
        for sg_id, header, nodes, empty_var in subgraphs:
            mermaid_lines.append(f"  {header}")
            if nodes:
                mermaid_lines.extend(nodes)
                first_node_line = nodes[0].strip()
                m_var = re.match(r"^([A-Za-z0-9_-]+)", first_node_line)
                rep_var = m_var.group(1) if m_var else first_node_line.split("[", 1)[0].strip()
                cluster_representatives.append((rep_var, False))
            else:
                mermaid_lines.append("    %% None")
                mermaid_lines.append(f'    {empty_var}["(None)"]')
                cluster_representatives.append((empty_var, True))
            mermaid_lines.append("  end")
            mermaid_lines.append("")

        hierarchy_links: list[str] = []
        for i in range(len(cluster_representatives) - 1):
            curr_rep, curr_is_empty = cluster_representatives[i]
            next_rep, next_is_empty = cluster_representatives[i + 1]
            if curr_is_empty or next_is_empty:
                hierarchy_links.append(f"  {curr_rep} ~~~ {next_rep}")

        if link_lines:
            mermaid_lines.extend(link_lines)
        if hierarchy_links:
            mermaid_lines.extend(hierarchy_links)

        while mermaid_lines and mermaid_lines[-1] == "":
            mermaid_lines.pop()

        mermaid_lines.append("```")

        mermaid_block = "\n".join(mermaid_lines)
        table_block = "\n".join(table_rows)

        progress_emoji = "✅" if all_resolved else "🟡"
        progress_line = f"*Progress: {progress_emoji} {resolved_count} dari {total_tickets} tiket selesai*"

        frontier_lines: list[str] = []
        if frontier_set:
            seen_frontier_ids: set[str] = set()
            for t in normalized_tickets:
                raw_id = (
                    t.get("id")
                    if t.get("id") is not None
                    else (t.get("number") if t.get("number") is not None else t.get("ticket"))
                )
                clean_id = str(raw_id).strip().lstrip("#") if raw_id is not None else ""
                if clean_id.lower().startswith("ticket-"):
                    clean_id = clean_id[7:].strip()
                if clean_id.isdigit():
                    clean_id = f"{int(clean_id):02d}"

                if clean_id.lower() in frontier_set or str(raw_id).strip().lower() in frontier_set:
                    seen_frontier_ids.add(clean_id.lower())
                    if raw_id is not None:
                        seen_frontier_ids.add(str(raw_id).strip().lower())
                    title = str(t.get("title", "")).strip()
                    clean_title = title
                    if clean_title.startswith(f"{clean_id}:"):
                        clean_title = clean_title.split(":", 1)[1].strip()
                    clean_title = re.sub(r"\s*\((?:Resolved|In Progress|Frontier)\)$", "", clean_title, flags=re.IGNORECASE).strip()
                    if t.get("path"):
                        p_file = Path(str(t["path"]).replace("\\", "/")).name
                        rel_link = f"issues/{p_file}"
                    else:
                        rel_link = f"issues/{clean_id}.md"
                    frontier_lines.append(f"- [{clean_id}: {clean_title}]({rel_link})")

            for f_cand in active_frontier or []:
                f_str = str(f_cand).strip().lstrip("#")
                if f_str.lower().startswith("ticket-"):
                    f_str = f_str[7:].strip()
                f_norm = f"{int(f_str):02d}" if f_str.isdigit() else f_str
                if f_norm.lower() not in seen_frontier_ids and str(f_cand).strip().lower() not in seen_frontier_ids:
                    frontier_lines.append(f"- [{f_norm}](issues/{f_norm}.md)")
                    seen_frontier_ids.add(f_norm.lower())

        if not frontier_lines:
            if all_resolved:
                frontier_lines.append("*(None — all tickets resolved)*")
            else:
                frontier_lines.append("*(None)*")

        frontier_block = "\n".join(frontier_lines)

        stage_2_block = (
            f"## Stage II: Implementation Swarm\n"
            f"{progress_line}\n\n"
            f"### 🎯 Active Frontier (Ready to Start)\n"
            f"{frontier_block}\n\n"
            f"### Status Progress Table\n"
            f"{table_block}"
        )

        stage_3_rows = [
            "## Stage III: Verification & Delivery (Evaluation Pipeline)",
            "| # | Phase | Status | Command | Deliverable |",
            "| :-: | :--- | :---: | :--- | :--- |",
            f"| 5 | Two-Axis Code Review | {phase_status_map[5]} | `/code-review` | {deliverable_map[5]} |",
        ]
        if render_subrow and subrow_phase == 5:
            stage_3_rows.append(remediation_row)

        stage_3_rows.append(
            f"| 6 | Semantic Security Audit (optional / attack-surface dependent) | {phase_status_map[6]} | `/security-audit` | {deliverable_map[6]} |"
        )
        if render_subrow and subrow_phase == 6:
            stage_3_rows.append(remediation_row)

        stage_3_rows.append(
            f"| 7 | Pull Request & Sync | {phase_status_map[7]} | `/pr` | {deliverable_map[7]} |"
        )
        stage_3_block = "\n".join(stage_3_rows)

        stage_4_rows = [
            "## Stage IV: Retrospective",
            "| # | Phase | Status | Command | Deliverable |",
            "| :-: | :--- | :---: | :--- | :--- |",
            f"| 8 | Retrospective | {phase_status_map[8]} | `/retro` | {deliverable_map[8]} |",
        ]
        stage_4_block = "\n".join(stage_4_rows)

        if total_tickets == 0:
            current_stage_text = "Stage I — Inception & Architecture"
        elif not all_resolved:
            current_stage_text = f"Stage II — Implementation Swarm ({resolved_count}/{total_tickets} resolved)"
        elif anchor_active_phase == "retro" or phase_status_map.get(8) == "🟡 in-progress":
            current_stage_text = f"Stage IV — Retrospective ({resolved_count}/{total_tickets} resolved)"
        else:
            current_stage_text = f"Stage III — Verification & Delivery ({resolved_count}/{total_tickets} resolved)"

        artifact = (
            f"# 🧭 Task Graph: {resolved_title}\n\n"
            f"> **Mission**: {resolved_mission}\n"
            f"> **Current Stage**: {current_stage_text}\n\n"
            f"{stage_1_block}\n\n"
            f"{stage_2_block}\n\n"
            f"{stage_3_block}\n\n"
            f"{stage_4_block}\n"
        )
        if include_mermaid:
            artifact += f"\n## Dependency DAG\n{mermaid_block}\n"
        return artifact


    @staticmethod
    def _is_status_in_progress(status: str) -> bool:
        s = status.lower().strip("`* \t")
        if s.startswith("🟡"):
            s = s[1:].strip("`* \t")
        return "in-progress" in s or "in_progress" in s

    @staticmethod
    def _is_status_resolved(status: str) -> bool:
        s = status.lower().strip("`* \t")
        if s.startswith("✅"):
            s = s[1:].strip("`* \t")
        if any(neg in s for neg in ("unresolved", "not resolved", "incomplete", "not complete", "wontfix", "won't fix", "in-progress", "in_progress")):
            return False
        tokens = set(re.findall(r"[a-z0-9_-]+", s))
        resolved_tokens = {"resolved", "closed", "complete", "completed", "done"}
        return bool(tokens & resolved_tokens)

    @staticmethod
    def _is_status_ready_for_agent(status: str) -> bool:
        s = status.lower().strip("`* \t")
        if s.startswith("⚪"):
            s = s[1:].strip("`* \t")
        if any(neg in s for neg in ("human", "wontfix", "won't fix", "triage", "info", "blocked", "in-progress", "in_progress", "resolved", "closed", "done")):
            return False
        tokens = set(re.findall(r"[a-z0-9_-]+", s))
        ready_tokens = {"ready-for-agent", "ready_for_agent", "ready"}
        return bool((tokens & ready_tokens) or not s)

    @staticmethod
    def _parse_blocker_ids(raw_blockers: str | list | tuple | set | None) -> list[str]:
        if raw_blockers is None:
            return []

        sentinels = {
            "none",
            "null",
            "n/a",
            "na",
            "unblocked",
            "frontier",
            "[]",
        }

        if isinstance(raw_blockers, (int, float)):
            return [f"{int(raw_blockers):02d}"]

        items: list[str] = []
        if isinstance(raw_blockers, str):
            raw_s = raw_blockers.strip()
            raw_lower = raw_s.lower().strip("`* \t")
            if (
                not raw_s
                or raw_lower in sentinels
                or raw_lower.startswith("none")
                or raw_lower.startswith("unblocked")
                or raw_lower.startswith("frontier")
            ):
                return []
            if raw_s.startswith("[") and raw_s.endswith("]"):
                raw_s = raw_s[1:-1]
            items = re.split(r"[\n,]+", raw_s)
        elif isinstance(raw_blockers, (list, tuple, set)):
            for b in raw_blockers:
                if b is not None:
                    if isinstance(b, (int, float)):
                        items.append(str(int(b)))
                    elif isinstance(b, str):
                        b_s = b.strip()
                        if b_s.startswith("[") and b_s.endswith("]"):
                            b_s = b_s[1:-1]
                        items.extend(re.split(r"[\n,]+", b_s))

        clean_blockers: list[str] = []
        for item in items:
            s = item.strip().lstrip("-* \t").strip("`'\"* \t")
            if not s:
                continue
            s_clean = s.lstrip("#")
            s_lower = s_clean.lower()
            if (
                s_lower in sentinels
                or s_lower.startswith("none")
                or s_lower.startswith("unblocked")
                or s_lower.startswith("frontier")
                or s_clean.strip("[] \t") == ""
                or s_clean == "-"
            ):
                continue
            m = re.match(r"^(?:ticket[-_]?)?#?([0-9]{1,3})\b", s_clean, re.IGNORECASE)
            if m:
                clean_id = f"{int(m.group(1)):02d}"
                if clean_id not in clean_blockers:
                    clean_blockers.append(clean_id)
            else:
                candidate = re.sub(r"[:(\[].*$", "", s_clean).strip().strip("`'\"* \t")
                cand_lower = candidate.lower()
                if (
                    candidate
                    and cand_lower not in sentinels
                    and not cand_lower.startswith("none")
                    and not cand_lower.startswith("unblocked")
                    and not cand_lower.startswith("frontier")
                    and candidate not in clean_blockers
                ):
                    clean_blockers.append(candidate)

        return clean_blockers

    def _extract_ticket_status_from_text(self, text: str) -> str:
        m = re.search(r"^[ \t]*(?:[-*][ \t]+)?\*\*Status:\*\*[ \t]*(.*?)$", text, re.MULTILINE | re.IGNORECASE)
        if not m:
            m = re.search(r"^[ \t]*(?:[-*][ \t]+)?Status:\s*(.*?)$", text, re.MULTILINE | re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            val = re.sub(r"<!--.*?-->", "", val).strip()
            val = re.sub(r"\s*\(.*?\)", "", val).strip()
            return val.strip("`* \t")
        return ""

    def _format_ticket_relative_path(self, path: Path | str) -> str:
        p = Path(path)
        parts = p.parts
        if ".scratch" in parts:
            idx = parts.index(".scratch")
            return Path(*parts[idx:]).as_posix()
        return p.as_posix()

    def _lookup_ticket_file_in_tracker(self, tracker_dir: Path | str | None, ticket_id: str) -> Path | None:
        if not tracker_dir:
            return None
        td = Path(tracker_dir)
        if not td.is_dir():
            return None
        clean_id = str(ticket_id).strip().lstrip("#")
        if clean_id.lower().startswith("ticket-"):
            clean_id = clean_id[7:].strip()
        if clean_id.isdigit():
            clean_id = f"{int(clean_id):02d}"
        for pattern in (f"{clean_id}-*.md", f"*{clean_id}*.md"):
            matches = list(td.glob(pattern))
            if matches:
                return matches[0]
        return None

    def _lookup_ticket_status_in_tracker(self, tracker_dir: Path | str | None, ticket_id: str) -> str:
        if not tracker_dir:
            return ""
        td = Path(tracker_dir)
        if not td.is_dir():
            return ""
        for pattern in (f"{ticket_id}-*.md", f"*{ticket_id}*.md"):
            matches = list(td.glob(pattern))
            if matches:
                try:
                    return self._extract_ticket_status_from_text(matches[0].read_text(encoding="utf-8"))
                except Exception:
                    pass
        return ""

    def _resolve_ticket_target_and_status(
        self,
        item: str | int | dict | Path,
        tracker_dir: Path | str | None = None,
        prefer_paths: bool = False,
    ) -> tuple[str, str]:
        if item is None:
            raise ValueError("Frontier ticket identifier cannot be None")

        if isinstance(item, dict):
            if not item:
                raise ValueError("Frontier ticket dictionary cannot be empty")
            status = str(item.get("status", "")).strip()
            if "path" in item and item["path"]:
                target = str(item["path"]).strip()
                if not status:
                    p = Path(target)
                    if p.is_file():
                        try:
                            status = self._extract_ticket_status_from_text(p.read_text(encoding="utf-8"))
                        except Exception:
                            pass
                return self._format_ticket_relative_path(target), status

            raw_id = (
                item.get("id")
                if item.get("id") is not None
                else (
                    item.get("number")
                    if item.get("number") is not None
                    else item.get("ticket")
                )
            )
            if raw_id is None or str(raw_id).strip() == "":
                raise ValueError(f"Ticket dictionary missing 'id', 'number', or 'ticket' key: {item}")
            clean_id = str(raw_id).strip().lstrip("#")
            if clean_id.lower().startswith("ticket-"):
                clean_id = clean_id[7:].strip()
            if clean_id.isdigit():
                clean_id = f"{int(clean_id):02d}"

            if not status and tracker_dir:
                status = self._lookup_ticket_status_in_tracker(tracker_dir, clean_id)

            if prefer_paths and tracker_dir:
                matching_file = self._lookup_ticket_file_in_tracker(tracker_dir, clean_id)
                if matching_file:
                    return self._format_ticket_relative_path(matching_file), status

            return clean_id, status

        if isinstance(item, int):
            clean_id = f"{item:02d}"
            status = self._lookup_ticket_status_in_tracker(tracker_dir, clean_id)
            if prefer_paths and tracker_dir:
                matching_file = self._lookup_ticket_file_in_tracker(tracker_dir, clean_id)
                if matching_file:
                    return self._format_ticket_relative_path(matching_file), status
            return clean_id, status

        if isinstance(item, Path):
            status = ""
            if item.is_file():
                try:
                    status = self._extract_ticket_status_from_text(item.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return self._format_ticket_relative_path(item), status

        raw_str = str(item).strip()
        if not raw_str:
            raise ValueError("Frontier ticket identifier cannot be empty string")

        if "\n" in raw_str or (raw_str.startswith("#") and " " in raw_str):
            status = self._extract_ticket_status_from_text(raw_str)
            meta = self._parse_ticket_markdown(raw_str, "ticket")
            raw_id = meta.get("id", meta.get("Title", "01"))
            m = re.match(r"^([0-9]{1,3})", str(raw_id).strip())
            clean_id = f"{int(m.group(1)):02d}" if m else str(raw_id).strip()
            if prefer_paths and tracker_dir:
                matching_file = self._lookup_ticket_file_in_tracker(tracker_dir, clean_id)
                if matching_file:
                    return self._format_ticket_relative_path(matching_file), status
            return clean_id, status

        p = Path(raw_str)
        if p.is_file():
            status = ""
            try:
                status = self._extract_ticket_status_from_text(p.read_text(encoding="utf-8"))
            except Exception:
                pass
            return self._format_ticket_relative_path(p), status

        target = raw_str
        if raw_str.startswith("#"):
            num_part = raw_str[1:].strip()
            if num_part.isdigit():
                target = f"{int(num_part):02d}"
        elif raw_str.lower().startswith("ticket-"):
            num_part = raw_str[7:].strip()
            if num_part.isdigit():
                target = f"{int(num_part):02d}"
        elif raw_str.isdigit():
            target = f"{int(raw_str):02d}"

        status = ""
        matching_file = None
        if tracker_dir and (raw_str.isdigit() or raw_str.startswith("#") or raw_str.lower().startswith("ticket-")):
            status = self._lookup_ticket_status_in_tracker(tracker_dir, target)
            if prefer_paths:
                matching_file = self._lookup_ticket_file_in_tracker(tracker_dir, target)

        if prefer_paths and matching_file:
            return self._format_ticket_relative_path(matching_file), status

        return target, status

    def strip_diagrams_and_tables(self, content: str) -> str:
        """
        [ERR-SUMM-01] Strips accidental raw Mermaid graphs and Markdown tables from chat responses,
        bounding the output strictly to the standardized 3-line summary when present.
        """
        if not content:
            return ""

        def _strip_block(match: re.Match) -> str:
            body = match.group(0)
            if any(term in body.lower() for term in ("graph td", "graph lr", "flowchart", "mermaid")):
                return ""
            return body

        stripped = re.sub(r"(?s)(?:`{3,}|~{3,}).*?(?:`{3,}|~{3,})", _strip_block, content)
        stripped = re.sub(
            r"(?m)^\s*(?:graph|flowchart)\s+(?:TD|LR|TB|RL)\b.*?(?=\n\s*\n|\n(?=[#📊🎯📁])|\Z)",
            "",
            stripped,
            flags=re.DOTALL,
        )

        cleaned_lines: list[str] = []
        for line in stripped.splitlines():
            if re.match(r"^\s*\|.*\|\s*$", line):
                continue
            cleaned_lines.append(line)
        stripped = "\n".join(cleaned_lines).strip()
        stripped = re.sub(r"\n{3,}", "\n\n", stripped)

        if "📊 **Progress**:" in stripped and "📁 **Task Graph**:" in stripped:
            raw_lines = [l.strip() for l in stripped.splitlines() if l.strip()]
            line1_idx = next((i for i, l in enumerate(raw_lines) if l.startswith("📊 **Progress**:")), -1)
            line3_idx = next((i for i, l in enumerate(raw_lines) if l.startswith("📁 **Task Graph**:")), -1)
            if line1_idx != -1 and line3_idx != -1 and line1_idx < line3_idx:
                selected_lines = [raw_lines[line1_idx]]
                for l in raw_lines[line1_idx + 1:line3_idx]:
                    if l.startswith("🎯 ") or l.startswith("- /implement") or l.startswith("/implement"):
                        selected_lines.append(l)
                selected_lines.append(raw_lines[line3_idx])
                return "\n".join(selected_lines)

        return stripped

    def clean_chat_handoff_content(self, content: str) -> str:
        """[ERR-SUMM-01] Convenience alias for strip_diagrams_and_tables."""
        return self.strip_diagrams_and_tables(content)

    def format_chat_handoff(
        self,
        frontier_ticket: str | int | dict | Path | list | tuple | None,
        tracker_dir: Path | str | None = None,
        prefer_paths: bool = False,
        *,
        lean_summary: bool = False,
        total_tickets: int | None = None,
        resolved_tickets: int | None = None,
        phase: str | None = None,
        task_graph_path: Path | str | None = None,
        feature_slug: str | None = None,
        multi_frontier_style: str = "bullet",
    ) -> str:
        """
        [DASH-02] [DASH-03] [SUMM-01] [SUMM-02] [REFRESH-02] Formats minimal, copy-pasteable execution command
        or standardized 3-line chat notification under the Lean 3-Line Summary Protocol.
        When lean_summary is False, emits 1-line execution command(s) (/implement 01 or /implement .scratch/.../issues/01-...md).
        When lean_summary is True, strictly emits the standardized 3-line notification:
        Line 1: 📊 **Progress**: `X/N resolved` *(Phase: <Phase Name>)*
        Line 2: 🎯 **Active Frontier**: /implement .scratch/<slug>/issues/<NN>-<name>.md
        Line 3: 📁 **Task Graph**: [`.scratch/<slug>/task-graph.md`](file:///path/to/.scratch/<slug>/task-graph.md)
        Strictly forbids dumping Markdown tables or Mermaid graphs into chat transcripts.
        """
        standby_msg = (
            "> ⏳ Standby: All active frontier tickets are currently in-progress. "
            "Awaiting worker completion before calculating downstream unblocked tickets."
        )

        resolved_tracker: Path | None = None
        detected_slug = feature_slug
        if tracker_dir:
            resolved_tracker = Path(tracker_dir)
        else:
            candidates: list[Path] = []
            if isinstance(frontier_ticket, (str, Path)):
                candidates.append(Path(frontier_ticket))
            elif isinstance(frontier_ticket, (list, tuple)):
                for item in frontier_ticket:
                    if isinstance(item, (str, Path)):
                        candidates.append(Path(item))
                    elif isinstance(item, dict) and item.get("path"):
                        candidates.append(Path(item["path"]))

            for cand_p in candidates:
                if cand_p.parent.name == "issues" and cand_p.parent.is_dir():
                    resolved_tracker = cand_p.parent
                if ".scratch" in cand_p.parts:
                    idx = cand_p.parts.index(".scratch")
                    if len(cand_p.parts) > idx + 1:
                        slug_candidate = cand_p.parts[idx + 1]
                        if not detected_slug:
                            detected_slug = slug_candidate
                        issues_cand = Path(*cand_p.parts[:idx + 2]) / "issues"
                        if issues_cand.is_dir():
                            resolved_tracker = issues_cand
                if resolved_tracker:
                    break

            if not resolved_tracker and detected_slug:
                slug_issues = Path(".scratch") / detected_slug / "issues"
                if slug_issues.is_dir():
                    resolved_tracker = slug_issues

        scanned = None
        is_empty_tracker_state = False
        if resolved_tracker and resolved_tracker.is_dir():
            scanned = self.scan_tickets_from_tracker(resolved_tracker)
            if len(scanned) == 0:
                is_empty_tracker_state = True
        elif resolved_tracker and not resolved_tracker.exists():
            is_empty_tracker_state = True
        elif total_tickets == 0:
            is_empty_tracker_state = True
        elif (
            lean_summary
            and (frontier_ticket is None or (isinstance(frontier_ticket, (list, tuple)) and len(frontier_ticket) == 0))
            and total_tickets is None
            and resolved_tickets is None
        ):
            is_empty_tracker_state = True

        total_count = total_tickets
        resolved_count = resolved_tickets

        if is_empty_tracker_state:
            total_count = 0
            resolved_count = 0
        elif resolved_tracker and (total_count is None or resolved_count is None):
            if scanned is None:
                scanned = self.scan_tickets_from_tracker(resolved_tracker)
            if scanned:
                if total_count is None:
                    total_count = len(scanned)
                if resolved_count is None:
                    resolved_count = sum(1 for t in scanned if self._is_status_resolved(t.get("status", "")))

        if total_count is None:
            if isinstance(frontier_ticket, (list, tuple)):
                total_count = len(frontier_ticket)
            else:
                total_count = 1
        if resolved_count is None:
            if isinstance(frontier_ticket, (list, tuple)):
                resolved_count = sum(
                    1 for item in frontier_ticket
                    if isinstance(item, dict) and self._is_status_resolved(str(item.get("status", "")))
                )
            else:
                resolved_count = 0

        is_resolved_milestone = bool(
            lean_summary
            and total_count is not None
            and resolved_count is not None
            and total_count > 0
            and resolved_count >= total_count
        )

        executable_targets: list[str] = []
        if is_empty_tracker_state or is_resolved_milestone:
            cmd = ""
        elif frontier_ticket is None:
            if lean_summary and resolved_tracker:
                if scanned and any(self._is_status_in_progress(t.get("status", "")) for t in scanned):
                    cmd = standby_msg
                else:
                    cmd = ""
            elif lean_summary:
                cmd = ""
            else:
                raise ValueError("Frontier ticket identifier cannot be None")
        elif isinstance(frontier_ticket, (list, tuple)):
            if not frontier_ticket:
                if lean_summary and resolved_tracker:
                    if scanned and any(self._is_status_in_progress(t.get("status", "")) for t in scanned):
                        cmd = standby_msg
                    else:
                        cmd = ""
                elif lean_summary:
                    cmd = ""
                else:
                    raise ValueError("Frontier ticket collection cannot be empty")

            has_in_progress = False
            for item in frontier_ticket:
                target, status = self._resolve_ticket_target_and_status(item, resolved_tracker or tracker_dir, prefer_paths=prefer_paths)
                if self._is_status_in_progress(status):
                    has_in_progress = True
                    continue
                if self._is_status_resolved(status):
                    continue
                if not self._is_status_ready_for_agent(status):
                    continue
                executable_targets.append(target)

            if not executable_targets:
                if has_in_progress:
                    cmd = standby_msg
                else:
                    cmd = ""
            else:
                commands = [f"/implement {target}" for target in executable_targets]
                cmd = "\n\n".join(commands)
        else:
            target, status = self._resolve_ticket_target_and_status(frontier_ticket, resolved_tracker or tracker_dir, prefer_paths=prefer_paths)
            if self._is_status_in_progress(status):
                cmd = standby_msg
            elif self._is_status_resolved(status):
                cmd = ""
            elif not self._is_status_ready_for_agent(status):
                cmd = ""
            else:
                executable_targets.append(target)
                cmd = f"/implement {target}"

        if not lean_summary:
            return cmd

        # --- Lean 3-Line Summary Engine ([SUMM-01], [SUMM-02]) ---

        if phase and str(phase).strip():
            phase_name = str(phase).strip()
        else:
            anchor_phase: str | None = None
            if resolved_tracker:
                try:
                    anchor = self.read_session_anchor(resolved_tracker)
                    if anchor and anchor.get("activePhase"):
                        anchor_phase = str(anchor["activePhase"]).strip()
                except Exception:
                    try:
                        feat_d = resolved_tracker.parent if resolved_tracker.name == "issues" else resolved_tracker
                        sf = feat_d / "session.json"
                        if sf.is_file():
                            raw_a = json.loads(sf.read_text(encoding="utf-8"))
                            if isinstance(raw_a, dict) and raw_a.get("activePhase"):
                                anchor_phase = str(raw_a["activePhase"]).strip()
                    except Exception:
                        pass
            if anchor_phase:
                ap_lower = anchor_phase.lower()
                if total_count > 0 and resolved_count < total_count and ap_lower in ("code-review", "code_review", "security-audit", "security_audit", "pr"):
                    phase_name = "Implementation"
                elif total_count > 0 and resolved_count >= total_count and ap_lower in ("implementation", "implement", "inception", ""):
                    phase_name = "Code Review"
                elif total_count == 0 and ap_lower not in ("inception", "grilling", "inception & grilling"):
                    phase_name = "Inception & Grilling"
                else:
                    phase_name = anchor_phase.replace("-", " ").title()
            elif total_count == 0 and resolved_count == 0:
                phase_name = "Inception & Grilling"
            elif total_count > 0 and resolved_count >= total_count:
                phase_name = "Code Review"
            else:
                phase_name = "Implementation"

        line1 = f"📊 **Progress**: `{resolved_count}/{total_count} resolved` *(Phase: {phase_name})*"

        if total_count == 0:
            line2 = "🎯 **Active Frontier**: ⚠️ No executable tickets found in task graph"
        elif total_count > 0 and resolved_count >= total_count:
            if phase_name.lower() in ("security audit", "security-audit", "security", "audit"):
                line2 = "🎯 **Next Phase**: /security-audit"
            elif phase_name.lower() in ("pull request", "pr"):
                line2 = "🎯 **Next Phase**: /pr"
            elif phase_name.lower() == "fix":
                line2 = "🎯 **Active Frontier**: /implement fix"
            elif phase_name.lower() == "escalation":
                line2 = "🎯 **Active Frontier**: ⚠️ Escalation: Circuit breaker tripped"
            else:
                line2 = "🎯 **Next Phase**: /code-review"
        elif cmd == standby_msg:
            line2 = "🎯 **Active Frontier**: ⏳ Standby (active tickets in-progress)"
        elif executable_targets:
            if len(executable_targets) > 1:
                if multi_frontier_style == "pipe":
                    line2 = "🎯 **Active Frontier**: " + " | ".join(f"/implement {t}" for t in executable_targets)
                else:
                    bullets = "\n".join(f"- /implement {t}" for t in executable_targets)
                    line2 = f"🎯 **Active Frontier**:\n{bullets}"
            else:
                line2 = f"🎯 **Active Frontier**: /implement {executable_targets[0]}"
        elif not cmd:
            if total_count > 0 and resolved_count < total_count:
                line2 = "🎯 **Active Frontier**: ⚠️ No unblocked frontier tickets available in task graph"
            else:
                if phase_name.lower() in ("security audit", "security-audit", "security", "audit"):
                    line2 = "🎯 **Next Phase**: /security-audit"
                elif phase_name.lower() in ("pull request", "pr"):
                    line2 = "🎯 **Next Phase**: /pr"
                else:
                    line2 = "🎯 **Next Phase**: /code-review"
        else:
            line2 = f"🎯 **Active Frontier**: {cmd}"

        if task_graph_path:
            tg_path = Path(task_graph_path)
        elif resolved_tracker:
            feat_dir = resolved_tracker.parent if resolved_tracker.name == "issues" else resolved_tracker
            tg_path = feat_dir / "task-graph.md"
        elif detected_slug:
            tg_path = Path(".scratch") / detected_slug / "task-graph.md"
        else:
            tg_path = Path(".scratch/task-graph.md")

        abs_tg = tg_path.resolve()
        abs_uri = abs_tg.as_uri()

        tg_posix = abs_tg.as_posix()
        if "/.scratch/" in tg_posix:
            rel_label = ".scratch/" + tg_posix.split("/.scratch/", 1)[1]
        elif tg_posix.startswith(".scratch/"):
            rel_label = tg_posix
        else:
            try:
                rel_label = abs_tg.relative_to(REPO_ROOT).as_posix()
            except Exception:
                try:
                    rel_label = abs_tg.relative_to(Path.cwd().resolve()).as_posix()
                except Exception:
                    rel_label = abs_tg.name

        line3 = f"📁 **Task Graph**: [`{rel_label}`]({abs_uri})"
        return self.strip_diagrams_and_tables(f"{line1}\n{line2}\n{line3}")

    def format_lean_chat_summary(
        self,
        frontier_ticket: str | int | dict | Path | list | tuple | None = None,
        tracker_dir: Path | str | None = None,
        prefer_paths: bool = True,
        *,
        total_tickets: int | None = None,
        resolved_tickets: int | None = None,
        phase: str | None = None,
        task_graph_path: Path | str | None = None,
        feature_slug: str | None = None,
        multi_frontier_style: str = "bullet",
    ) -> str:
        """
        [SUMM-01] Convenience helper formatting standardized 3-line chat notification under the Lean 3-Line Summary Protocol.
        Emits:
        Line 1: 📊 **Progress**: `X/N resolved` *(Phase: <Phase Name>)*
        Line 2: 🎯 **Active Frontier**: /implement .scratch/<slug>/issues/<NN>-<name>.md
        Line 3: 📁 **Task Graph**: [`.scratch/<slug>/task-graph.md`](file:///path/to/.scratch/<slug>/task-graph.md)
        """
        return self.format_chat_handoff(
            frontier_ticket=frontier_ticket,
            tracker_dir=tracker_dir,
            prefer_paths=prefer_paths,
            lean_summary=True,
            total_tickets=total_tickets,
            resolved_tickets=resolved_tickets,
            phase=phase,
            task_graph_path=task_graph_path,
            feature_slug=feature_slug,
            multi_frontier_style=multi_frontier_style,
        )

    def format_multi_chat_handoff(
        self,
        frontier_tickets: Sequence[dict[str, str] | Path | str | int] | list | tuple,
        tracker_dir: Path | str | None = None,
        prefer_paths: bool = False,
    ) -> str:
        """
        [DASH-02] Convenience helper formatting 1-line execution commands for a list or tuple
        of unblocked active frontier tickets, separated by double newlines.
        """
        if frontier_tickets is None:
            raise ValueError("Frontier tickets list cannot be None")
        if not isinstance(frontier_tickets, (list, tuple)):
            raise ValueError("Frontier tickets must be a list or tuple")
        return self.format_chat_handoff(frontier_tickets, tracker_dir=tracker_dir, prefer_paths=prefer_paths)

    def _validate_ticket_completeness(self, content: str) -> bool:
        """
        [EDGE-REFRESH-01] Validates ticket frontmatter completeness:
        Verifies non-empty content, valid YAML frontmatter closure (if starting with ---),
        presence of ticket title header or frontmatter title, and presence of status field.
        """
        stripped = content.strip()
        if not stripped:
            return False

        if stripped.startswith("---"):
            try:
                fm = parse_frontmatter(stripped)
            except (FrontmatterParseError, Exception):
                return False

            closing_match = re.search(r"^---\s*$", stripped[3:], re.MULTILINE)
            if not closing_match:
                return False
            body = stripped[3 + closing_match.end():]

            has_fm_status = bool(fm.get("status") or fm.get("Status"))
            has_body_status = bool(re.search(r"(?:^|[ \t]+)(?:\*\*)?status(?:\*\*)?\s*:\s*\S+", body, re.IGNORECASE | re.MULTILINE))
            has_status = has_fm_status or has_body_status

            has_fm_title = bool(fm.get("title") or fm.get("Title"))
            has_body_title = bool(re.search(r"^#+\s+(.+)$", body, re.MULTILINE))
            has_title = has_fm_title or has_body_title

            return has_title and has_status

        has_title = bool(re.search(r"^#+\s+(.+)$", stripped, re.MULTILINE))
        has_status = bool(re.search(r"(?:^|[ \t]+)(?:\*\*)?status(?:\*\*)?\s*:\s*\S+", stripped, re.IGNORECASE | re.MULTILINE))
        return has_title and has_status

    def _read_ticket_with_retry_and_fallback(
        self,
        ticket_file: Path,
        feature_dir: Path | None = None,
        max_retries: int = 5,
        base_delay: float = 0.01,
    ) -> dict[str, Any] | None:
        """
        [EDGE-REFRESH-01] Reads a ticket file with frontmatter completeness validation,
        retrying with exponential backoff on partial/in-progress writes, and falling
        back to preceding valid state if partial content persists.
        """
        cache_key = str(ticket_file.resolve())
        if not hasattr(self, "_preceding_ticket_cache"):
            self._preceding_ticket_cache = {}

        raw_content = ""
        is_complete = False

        for attempt in range(max_retries + 1):
            try:
                if ticket_file.is_file():
                    raw_content = ticket_file.read_text(encoding="utf-8")
                    if self._validate_ticket_completeness(raw_content):
                        is_complete = True
                        break
            except (OSError, UnicodeDecodeError, ValueError):
                pass

            if attempt < max_retries:
                time.sleep(base_delay * (2 ** attempt))

        if is_complete:
            try:
                meta = self._parse_ticket_markdown(raw_content, ticket_file.stem)
                m = re.match(r"^([0-9]{1,3})", ticket_file.stem)
                ticket_id = f"{int(m.group(1)):02d}" if m else meta.get("id", ticket_file.stem)
                raw_title = meta.get("title", meta.get("Title", ticket_file.stem))
                if ":" in raw_title:
                    clean_title = raw_title.split(":", 1)[1].strip()
                else:
                    clean_title = raw_title
                status = meta.get("Status", meta.get("status", "ready-for-agent")).strip()
                blockers_raw = meta.get("Blocked by", meta.get("blocked_by", ""))
                clean_blockers = self._parse_blocker_ids(blockers_raw)
                commit_val = meta.get("Verified Commit", meta.get("Commit", meta.get("commit", "")))

                ticket_dict = {
                    "id": ticket_id,
                    "title": clean_title,
                    "status": status,
                    "blockers": clean_blockers,
                    "commit": commit_val,
                    "context": meta.get("Context", meta.get("context", "")),
                    "seam": meta.get("Seam", meta.get("seam", "")),
                    "verification": meta.get("Verification", meta.get("verification", "")),
                    "path": ticket_file.as_posix(),
                }
                self._preceding_ticket_cache[cache_key] = ticket_dict
                return ticket_dict
            except Exception:
                pass

        if not is_complete:
            if raw_content and raw_content.strip().startswith("---"):
                parse_frontmatter(raw_content.strip())

        if cache_key in self._preceding_ticket_cache:
            return self._preceding_ticket_cache[cache_key]

        resolved_feat_dir = feature_dir or (
            ticket_file.parent.parent if ticket_file.parent.name == "issues" else ticket_file.parent
        )
        if resolved_feat_dir:
            tg_path = resolved_feat_dir / "task-graph.md"
            if tg_path.is_file():
                try:
                    tg_content = tg_path.read_text(encoding="utf-8")
                    m_id = re.match(r"^([0-9]{1,3})", ticket_file.stem)
                    t_id_str = f"{int(m_id.group(1)):02d}" if m_id else ticket_file.stem
                    id_pattern = (
                        rf"(?:0*{int(t_id_str)}|{re.escape(t_id_str)})"
                        if t_id_str.isdigit()
                        else re.escape(t_id_str)
                    )
                    row_match = re.search(
                        rf"^\s*\|\s*\*{{0,2}}(?:\[\*{{0,2}}(?:ticket-|issue-|#)?{id_pattern}\*{{0,2}}\]\([^)]+\)|(?:ticket-|issue-|#)?{id_pattern})\*{{0,2}}\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|",
                        tg_content,
                        re.MULTILINE,
                    )
                    if row_match:
                        clean_title = row_match.group(1).replace(r"\|", "|").strip()
                        blockers_raw = row_match.group(2).strip()
                        status_raw = row_match.group(3).strip()
                        commit_raw = row_match.group(4).strip("` \t")
                        clean_blockers = self._parse_blocker_ids(blockers_raw)
                        if self._is_status_resolved(status_raw):
                            clean_status = "resolved"
                        elif self._is_status_in_progress(status_raw):
                            clean_status = "in-progress"
                        elif "blocked" in status_raw.lower():
                            clean_status = "blocked"
                        else:
                            clean_status = "ready-for-agent"
                        fallback_dict = {
                            "id": t_id_str,
                            "title": clean_title,
                            "status": clean_status,
                            "blockers": clean_blockers,
                            "commit": commit_raw if commit_raw != "-" else "",
                            "context": "",
                            "seam": "",
                            "verification": "",
                            "path": ticket_file.as_posix(),
                        }
                        self._preceding_ticket_cache[cache_key] = fallback_dict
                        return fallback_dict
                except Exception:
                    pass

        return None

    def scan_tickets_from_tracker(self, tracker_dir_or_feature_dir: Path | str) -> list[dict]:
        """
        [REACT-01] [ERR-REFRESH-01] [EDGE-REFRESH-01] [ERR-API-01] Scans ticket markdown files from tracker directory or feature directory on disk.
        Returns parsed ticket metadata dictionaries sorted by ticket identifier.
        """
        if not tracker_dir_or_feature_dir or not str(tracker_dir_or_feature_dir).strip():
            return []
        base_path = Path(tracker_dir_or_feature_dir)
        feature_dir = self._resolve_feature_dir(base_path)
        if base_path.name.lower() in ("task-graph.md", "task-graph.markdown"):
            if not feature_dir.is_dir():
                return []
        elif not base_path.exists():
            return []

        if base_path.is_file():
            if base_path.parent.name == "issues":
                issues_dir = base_path.parent
            elif (base_path.parent / "issues").is_dir():
                issues_dir = base_path.parent / "issues"
            else:
                issues_dir = base_path.parent
        elif (feature_dir / "issues").is_dir():
            issues_dir = feature_dir / "issues"
        elif (base_path / "issues").is_dir():
            issues_dir = base_path / "issues"
        elif base_path.is_dir():
            issues_dir = base_path
        elif feature_dir.is_dir():
            issues_dir = feature_dir
        else:
            return []

        ticket_files: list[Path] = []
        try:
            if issues_dir.is_dir():
                for p in sorted(issues_dir.glob("*.md")):
                    if p.name.lower() in ("task-graph.md", "spec.md", "session.json", "index.md", "readme.md"):
                        continue
                    ticket_files.append(p)
        except OSError as exc:
            logger.warning("Failed to access issues directory '%s': %s", issues_dir, exc)

        resolved_feat_dir = feature_dir or (
            issues_dir.parent if issues_dir.name == "issues" else issues_dir
        )
        if resolved_feat_dir:
            tg_path = resolved_feat_dir / "task-graph.md"
            if tg_path.is_file():
                try:
                    tg_content = tg_path.read_text(encoding="utf-8")
                    for row_match in re.finditer(
                        r"\[(?P<link_id>[^\]]+)\]\((?P<link_path>[^)]*issues/[^)]+)\)",
                        tg_content,
                    ):
                        link_path = row_match.group("link_path")
                        if link_path:
                            clean_lp = link_path.strip()
                            cand_p = issues_dir / Path(clean_lp).name
                            existing_names = {p.name for p in ticket_files}
                            existing_ids = set()
                            for p in ticket_files:
                                m = re.match(r"^([0-9]{1,3})", p.stem)
                                if m:
                                    existing_ids.add(f"{int(m.group(1)):02d}")
                            m_cand = re.match(r"^([0-9]{1,3})", cand_p.stem)
                            cand_id = f"{int(m_cand.group(1)):02d}" if m_cand else None
                            if cand_p.name not in existing_names and (not cand_id or cand_id not in existing_ids):
                                ticket_files.append(cand_p)
                except Exception:
                    pass

        def _sort_key(p: Path) -> tuple[int, str]:
            m = re.match(r"^([0-9]{1,3})", p.stem)
            return (int(m.group(1)), p.name) if m else (9999, p.name)

        ticket_files.sort(key=_sort_key)

        tickets: list[dict] = []
        for p in ticket_files:
            try:
                t_dict = self._read_ticket_with_retry_and_fallback(p, feature_dir=feature_dir)
                if t_dict is not None:
                    tickets.append(t_dict)
            except FrontmatterParseError as fpe:
                logger.warning(
                    "Skipping corrupt ticket '%s' due to frontmatter parse error: %s",
                    p.name,
                    fpe,
                )
            except Exception as exc:
                logger.warning(
                    "Skipping unreadable ticket '%s': %s",
                    p.name,
                    exc,
                )

        return tickets

    def calculate_unblocked_frontier(self, tickets: list[dict | Path | str]) -> list[str]:
        """
        [REACT-01] Recalculates unblocked active frontier tickets based on resolved blockers.
        Returns ticket identifiers whose blockers are all resolved and whose status is not resolved.
        """
        norm_tickets: list[dict] = []
        for t in tickets:
            if isinstance(t, dict):
                norm_tickets.append(dict(t))
            elif isinstance(t, (Path, str)):
                p = Path(t)
                try:
                    t_dict = self._read_ticket_with_retry_and_fallback(p)
                    if t_dict:
                        norm_tickets.append(t_dict)
                except FrontmatterParseError as fpe:
                    logger.warning("Skipping corrupt ticket '%s' in frontier calculation: %s", p.name, fpe)
                except Exception:
                    pass

        resolved_ids: set[str] = set()
        all_known_ids: set[str] = set()
        for t in norm_tickets:
            raw_id = (
                t.get("id")
                if t.get("id") is not None
                else (t.get("number") if t.get("number") is not None else t.get("ticket"))
            )
            tid = str(raw_id or "").strip().lstrip("#")
            if tid.lower().startswith("ticket-"):
                tid = tid[7:].strip()
            if tid.isdigit():
                tid = f"{int(tid):02d}"
            if tid:
                all_known_ids.add(tid)
            status = str(t.get("status", "")).strip()
            if self._is_status_resolved(status):
                resolved_ids.add(tid)

        frontier: list[str] = []
        for t in norm_tickets:
            raw_id = (
                t.get("id")
                if t.get("id") is not None
                else (t.get("number") if t.get("number") is not None else t.get("ticket"))
            )
            tid = str(raw_id or "").strip().lstrip("#")
            if tid.lower().startswith("ticket-"):
                tid = tid[7:].strip()
            if tid.isdigit():
                tid = f"{int(tid):02d}"
            status = str(t.get("status", "")).strip()
            if self._is_status_resolved(status):
                continue

            raw_b = t.get("blockers") or t.get("blocked_by") or t.get("Blocked by")
            clean_blockers = self._parse_blocker_ids(raw_b)
            existing_blockers = [b for b in clean_blockers if b in all_known_ids and b != tid]
            if all(b in resolved_ids for b in existing_blockers):
                frontier.append(tid)

        return frontier

    def validate_ticket_blockers(
        self,
        tickets: list[dict | Path | str] | Path | str,
        root_dir: Path | str | None = None,
        raise_on_cycle: bool = True,
        emit_warnings: bool = True,
    ) -> Any:
        """
        [EDGE-DAG-SELF-REF] [ERR-DAG-ORPHAN] Validates ticket blocker dependencies for self-loops and orphans.
        """
        from scripts.internal.resolve_ticket import validate_ticket_blockers
        return validate_ticket_blockers(
            tickets,
            validator=self,
            root_dir=root_dir,
            raise_on_cycle=raise_on_cycle,
            emit_warnings=emit_warnings,
        )

    def topological_sort_tickets(self, tickets: list[dict | Path | str] | Path | str) -> list[str]:
        """
        [DAG-01] [ERR-DAG-CYCLE] Computes deterministic topological sort order of tickets using Kahn's algorithm
        and validates that the task graph is a directed acyclic graph (DAG) with zero cycles.
        """
        from scripts.internal.resolve_ticket import topological_sort_tickets
        return topological_sort_tickets(tickets, validator=self)

    def validate_ticket_brief_schema(
        self,
        ticket_input: Path | str | dict[str, Any],
        root_dir: Path | str | None = None,
        spec_path: Path | str | None = None,
    ) -> TicketBriefValidationResult:
        """
        [ERR-BRIEF-01] [ERR-BRIEF-02] Boundary schema validator for Command Center worker starter brief synthesis.
        """
        from scripts.internal.resolve_ticket import validate_ticket_brief_schema
        return validate_ticket_brief_schema(
            ticket_input=ticket_input,
            root_dir=root_dir,
            spec_path=spec_path,
        )

    validate_ticket_for_brief = validate_ticket_brief_schema

    def write_canonical_task_dashboard(
        self,
        tracker_dir_or_feature_dir: Path | str,
        artifact_path: Path | str | None = None,
    ) -> tuple[Path, str]:
        """
        [DISK-01] [BAN-01] [DISK-02] [EDGE-DISK-02] [ERR-DISK-01] [EDGE-DISK-01] Persists the canonical Dual-Layer Task Graph Cockpit directly and
        exclusively to .scratch/<feature-slug>/task-graph.md on the workspace filesystem, permanently
        barring all write operations to conversation brain artifact directories. Automatically creates parent directories
        recursively (parents=True, exist_ok=True) and raises actionable PermissionError or IOError on read-only filesystems.
        Returns (canonical_path, dashboard_content).
        """
        feature_dir = self._resolve_feature_dir(tracker_dir_or_feature_dir)
        if "brain" in feature_dir.parts:
            raise ValueError(
                f"Task graph persistence is prohibited in brain artifact directories ({feature_dir}); "
                "task graph state must reside exclusively on disk under .scratch/<feature-slug>/task-graph.md."
            )
        feature_slug = feature_dir.name
        anchor = None
        try:
            anchor = self.read_session_anchor(feature_dir)
        except Exception:
            pass
        tickets = self.scan_tickets_from_tracker(tracker_dir_or_feature_dir)
        if tickets:
            self.topological_sort_tickets(tickets)
        frontier = self.calculate_unblocked_frontier(tickets)
        dashboard = self.render_task_dashboard(tickets, frontier, feature_slug=feature_slug, session_anchor=anchor)

        canonical_path = feature_dir / "task-graph.md"
        temp_path = feature_dir / f".task-graph.md.tmp.{os.getpid()}_{uuid.uuid4().hex}"
        try:
            feature_dir.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(dashboard, encoding="utf-8")
            temp_path.replace(canonical_path)
        except PermissionError as exc:
            raise PermissionError(
                f"Failed to persist canonical task graph: destination path '{canonical_path}' is not writeable. "
                f"Ensure filesystem write permissions are granted: {exc}"
            ) from exc
        except OSError as exc:
            raise IOError(
                f"Failed to persist canonical task graph: destination path '{canonical_path}' could not be written. "
                f"Filesystem error: {exc}"
            ) from exc
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        return canonical_path, dashboard

    def rescan_and_refresh_dashboard(
        self,
        tracker_dir_or_feature_dir: Path | str,
        artifact_path: Path | str | None = None,
        check_exists: bool = False,
    ) -> tuple[str, list[str], bool]:
        """
        [REACT-01] [REFRESH-01] [BAN-01] [DISK-01] [REFRESH-02] [ERR-REFRESH-01] [EDGE-REFRESH-01] [EDGE-API-01] Re-scans on-disk ticket frontmatter
        in tracker/feature directory, updates canonical on-disk task graph (.scratch/<feature-slug>/task-graph.md)
        directly and exclusively on the workspace filesystem without creating or updating files in
        conversation brain artifact directories, and recalculates active unblocked frontier tickets.
        Returns (dashboard_markdown, active_frontier, all_resolved).
        """
        if not tracker_dir_or_feature_dir or not str(tracker_dir_or_feature_dir).strip():
            if check_exists:
                raise FileNotFoundError("Feature directory or ticket path cannot be empty.")
            return "", [], False

        raw_path = Path(str(tracker_dir_or_feature_dir).strip())
        if ".." in raw_path.parts:
            raise ValueError(f"Path traversal detected in target path: {tracker_dir_or_feature_dir}")

        feature_dir = self._resolve_feature_dir(tracker_dir_or_feature_dir, check_exists=False)
        if "brain" in feature_dir.parts:
            raise ValueError(
                f"Task graph refresh is prohibited in brain artifact directories ({feature_dir}); "
                "task graph state must reside exclusively on disk under .scratch/<feature-slug>/task-graph.md."
            )
        has_scratch = ".scratch" in feature_dir.parts
        has_issues = (feature_dir / "issues").is_dir() or (raw_path.is_file() and raw_path.parent.name == "issues")
        if not has_scratch and not has_issues:
            if check_exists:
                raise FileNotFoundError(
                    f"Feature directory must reside within workspace .scratch/ boundary: {feature_dir}"
                )
            return "", [], False

        is_tg_target = raw_path.name.lower() in ("task-graph.md", "task-graph.markdown")
        if is_tg_target:
            if not feature_dir.is_dir():
                if check_exists:
                    raise FileNotFoundError(
                        f"Feature directory not found for task graph: {feature_dir}"
                    )
                return "", [], False
        else:
            if not raw_path.exists():
                if check_exists:
                    raise FileNotFoundError(
                        f"Feature directory or ticket path not found: {tracker_dir_or_feature_dir}"
                    )
                return "", [], False
        feature_slug = feature_dir.name
        tickets = self.scan_tickets_from_tracker(tracker_dir_or_feature_dir)
        if tickets:
            self.topological_sort_tickets(tickets)
        frontier = self.calculate_unblocked_frontier(tickets)
        total_tickets = len(tickets)
        resolved_count = sum(
            1 for t in tickets if self._is_status_resolved(t.get("status", ""))
        )
        all_resolved = total_tickets > 0 and resolved_count >= total_tickets

        if total_tickets == 0:
            derived_phase = "inception & grilling"
        elif all_resolved:
            derived_phase = "code-review"
        else:
            derived_phase = "implementation"

        session_file = feature_dir / "session.json"
        is_missing = not session_file.is_file()
        is_unparseable = False
        is_stale_phase = False
        anchor = None

        if not is_missing:
            try:
                anchor = self.read_session_anchor(feature_dir)
            except Exception:
                try:
                    raw_data = json.loads(session_file.read_text(encoding="utf-8"))
                    if isinstance(raw_data, dict) and "feature" in raw_data and "createdAt" in raw_data and "activePhase" in raw_data:
                        anchor = raw_data
                    else:
                        is_unparseable = True
                except Exception:
                    is_unparseable = True

        if anchor is not None:
            raw_active_phase = str(anchor.get("activePhase") or "").strip().lower()
            if total_tickets == 0:
                if raw_active_phase not in ("inception", "grilling", "inception & grilling"):
                    is_stale_phase = True
            elif all_resolved:
                if raw_active_phase in ("implementation", "implement", "inception", ""):
                    is_stale_phase = True
            else:
                if raw_active_phase in ("code-review", "code_review", "security-audit", "security_audit", "pr"):
                    is_stale_phase = True

        if is_missing or is_unparseable or is_stale_phase:
            if is_missing or is_unparseable or not anchor:
                target_anchor: dict[str, Any] = {
                    "feature": feature_slug,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "activePhase": derived_phase,
                    "fixLoopCount": 0,
                    "reviewedCommits": [],
                }
            else:
                target_anchor = dict(anchor)
                target_anchor["feature"] = feature_slug
                if "createdAt" not in target_anchor or not target_anchor["createdAt"]:
                    target_anchor["createdAt"] = datetime.now(timezone.utc).isoformat()
                target_anchor["activePhase"] = derived_phase
                if "fixLoopCount" not in target_anchor or not isinstance(target_anchor["fixLoopCount"], int):
                    target_anchor["fixLoopCount"] = 0
                if "reviewedCommits" not in target_anchor or not isinstance(target_anchor["reviewedCommits"], list):
                    target_anchor["reviewedCommits"] = []

            try:
                feature_dir.mkdir(parents=True, exist_ok=True)
                session_file.write_text(json.dumps(target_anchor, indent=2) + "\n", encoding="utf-8")
                anchor = target_anchor
            except Exception:
                pass

        canonical_path = feature_dir / "task-graph.md"
        has_existing_mermaid = False
        if canonical_path.is_file():
            try:
                tg_existing = canonical_path.read_text(encoding="utf-8")
                if re.search(r"```mermaid|subgraph\s+S_", tg_existing):
                    has_existing_mermaid = True
            except Exception:
                pass

        dashboard = self.render_task_dashboard(
            tickets,
            frontier,
            feature_slug=feature_slug,
            session_anchor=anchor,
            include_mermaid=has_existing_mermaid,
        )

        feature_dir.mkdir(parents=True, exist_ok=True)
        temp_path = feature_dir / f".task-graph.md.tmp.{os.getpid()}_{uuid.uuid4().hex}"
        try:
            temp_path.write_text(dashboard, encoding="utf-8")
            temp_path.replace(canonical_path)
        except Exception:
            canonical_path.write_text(dashboard, encoding="utf-8")
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        return dashboard, frontier, all_resolved

    def format_command_center_reaction(
        self,
        tracker_dir_or_feature_dir: Path | str,
        artifact_path: Path | str | None = None,
        branch: str = "",
        commits: list[str] | None = None,
        prefer_paths: bool = False,
        lean_summary: bool = False,
    ) -> tuple[str, str]:
        """
        [REACT-02] [LOOP-01] [LOOP-02] [REFRESH-01] [REFRESH-02] Formats Command Center autonomous reaction chat response:
        - If all tickets in the DAG are resolved, evaluates active phase (review, fix loop, security audit, or PR).
        - If fixLoopCount > 2 or activePhase is escalation, emits circuit breaker escalation alert awaiting human intervention ([LOOP-02]).
        - If activePhase is fix, emits targeted /implement fix command ([LOOP-01]).
        - If unblocked active frontier tickets exist, emits copy-pasteable 1-line execution commands (/implement <NN> or /implement <ticket-path>) simultaneously ([DASH-02], [REFRESH-02]).
        - If all active frontier tickets are in-progress, emits standby notification ([DASH-03], [REFRESH-02]).
        Returns (chat_response, dashboard_markdown).
        """
        dashboard, frontier, all_resolved = self.rescan_and_refresh_dashboard(
            tracker_dir_or_feature_dir=tracker_dir_or_feature_dir,
            artifact_path=artifact_path,
        )

        if all_resolved:
            tickets = self.scan_tickets_from_tracker(tracker_dir_or_feature_dir)
            anchor = None
            try:
                anchor = self.read_session_anchor(tracker_dir_or_feature_dir)
            except Exception:
                try:
                    feat_p = self._resolve_feature_dir(tracker_dir_or_feature_dir)
                    sf = feat_p / "session.json"
                    if sf.is_file():
                        anchor = json.loads(sf.read_text(encoding="utf-8"))
                except Exception:
                    pass
            cid = ""
            reviewed_commits: list[str] = []
            active_phase = "code-review"
            fix_loop_count = 0
            if anchor:
                cid = str(anchor.get("commandCenterConversationId", "")).strip()
                reviewed_commits = list(anchor.get("reviewedCommits", []))
                active_phase = str(anchor.get("activePhase", "")).strip() or "code-review"
                try:
                    fix_loop_count = max(0, int(anchor.get("fixLoopCount", 0)))
                except (ValueError, TypeError):
                    fix_loop_count = 0

            if commits:
                for c in commits:
                    cs = str(c).strip()
                    if cs and cs not in reviewed_commits:
                        reviewed_commits.append(cs)

            if active_phase in ("implementation", "") or (commits and active_phase == "code-review"):
                try:
                    self.update_session_anchor(
                        tracker_dir_or_feature_dir,
                        active_phase="code-review",
                        reviewed_commits=reviewed_commits,
                    )
                    active_phase = "code-review"
                    dashboard, frontier, all_resolved = self.rescan_and_refresh_dashboard(
                        tracker_dir_or_feature_dir=tracker_dir_or_feature_dir,
                        artifact_path=artifact_path,
                    )
                except Exception:
                    try:
                        self.write_session_anchor(
                            tracker_dir_or_feature_dir,
                            conversation_id=cid or "command-center",
                            active_phase="code-review",
                            reviewed_commits=reviewed_commits,
                        )
                        active_phase = "code-review"
                        dashboard, frontier, all_resolved = self.rescan_and_refresh_dashboard(
                            tracker_dir_or_feature_dir=tracker_dir_or_feature_dir,
                            artifact_path=artifact_path,
                        )
                    except Exception:
                        pass

            if active_phase == "escalation" or fix_loop_count > 2:
                failing_phase = str(anchor.get("failingPhase", "code-review") if anchor else "code-review").strip()
                if anchor and active_phase != "escalation":
                    try:
                        self.update_session_anchor(
                            tracker_dir_or_feature_dir,
                            active_phase="escalation",
                            failing_phase=failing_phase,
                        )
                        dashboard, frontier, all_resolved = self.rescan_and_refresh_dashboard(
                            tracker_dir_or_feature_dir=tracker_dir_or_feature_dir,
                            artifact_path=artifact_path,
                        )
                    except Exception:
                        pass
                active_phase = "escalation"

            if lean_summary:
                base_p = Path(tracker_dir_or_feature_dir)
                tr_dir = base_p if base_p.name == "issues" else ((base_p / "issues") if (base_p / "issues").is_dir() else base_p)
                feat_dir = base_p.parent if base_p.name == "issues" else (base_p if base_p.is_dir() else base_p.parent)
                canonical_path = feat_dir / "task-graph.md"
                phase_label = "Code Review"
                if active_phase == "escalation":
                    phase_label = "Escalation"
                elif active_phase == "fix":
                    phase_label = "Fix"
                elif active_phase == "pr":
                    phase_label = "Pull Request"
                elif active_phase in ("security-audit", "security", "audit"):
                    phase_label = "Security Audit"
                chat_response = self.format_chat_handoff(
                    [],
                    tracker_dir=tr_dir,
                    prefer_paths=prefer_paths or lean_summary,
                    lean_summary=True,
                    total_tickets=len(tickets),
                    resolved_tickets=len(tickets),
                    phase=phase_label,
                    task_graph_path=canonical_path,
                )
                return chat_response, dashboard

            if active_phase == "escalation":
                chat_response = (
                    f"> ⚠️ Escalation: Circuit breaker tripped after {fix_loop_count} failed remediation cycles on {failing_phase}. "
                    "Automated retries halted awaiting operator intervention."
                )
                return chat_response, dashboard

            if active_phase == "fix":
                failing_phase = str(anchor.get("failingPhase", "code-review") if anchor else "code-review").strip()
                attempt_num = max(1, fix_loop_count)
                chat_response = (
                    f"> 🔄 Fix loop in-progress (attempt {attempt_num}/2) following {failing_phase} findings.\n\n"
                    "/implement fix"
                )
                return chat_response, dashboard

            if active_phase == "pr":
                chat_response = (
                    "🎉 All tickets in the task graph are resolved and security evaluation complete!\n\n"
                    "### Downstream Phase Handoff: Pull Request & Sync\n\n"
                    "/pr"
                )
                return chat_response, dashboard

            base_path = Path(tracker_dir_or_feature_dir)
            feature_dir = base_path.parent if base_path.name == "issues" else (base_path if base_path.is_dir() else base_path.parent)
            feature_slug = feature_dir.name if feature_dir else ""
            delta_spec = f".scratch/{feature_slug}/spec.md" if feature_slug else ".scratch/<feature-slug>/spec.md"
            if feature_dir and (feature_dir / "spec.md").is_file():
                delta_spec = str(feature_dir / "spec.md")

            spec_anchors: list[str] = []
            seams: list[str] = []
            for t in tickets:
                c = t.get("context", "").strip()
                if c and c not in spec_anchors:
                    spec_anchors.append(c)
                s = t.get("seam", "").strip()
                if s and s not in seams:
                    seams.append(s)

            spec_line = "; ".join(spec_anchors) if spec_anchors else "docs/specs/implement/spec.md"
            seam_line = "; ".join(seams) if seams else "skills/to-tickets/SKILL.md#Step 5"

            detected_branch = (branch or "").strip()
            if not detected_branch:
                try:
                    res = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=5.0,
                    )
                    if res.returncode == 0 and res.stdout.strip() and res.stdout.strip() != "HEAD":
                        detected_branch = res.stdout.strip()
                except Exception:
                    pass

            if active_phase in ("security-audit", "security", "audit"):
                brief = self.build_security_audit_brief(
                    spec_path=spec_line,
                    delta_spec_path=delta_spec,
                    commits=reviewed_commits,
                    branch=detected_branch,
                    conversation_id=cid,
                )
                chat_response = (
                    "🎉 All tickets in the task graph are resolved and code review passed!\n\n"
                    "### Downstream Phase Handoff: Semantic Security Audit\n\n"
                    f"{brief}"
                )
                return chat_response, dashboard

            brief = self.build_session_brief(
                phase="code-review",
                spec_path=spec_line,
                delta_spec_path=delta_spec,
                commits=reviewed_commits,
                branch=detected_branch,
                seam=seam_line,
                conversation_id=cid,
            )

            chat_response = (
                "🎉 All tickets in the task graph are resolved (100% complete)!\n\n"
                "### Downstream Phase Handoff: Code Review\n\n"
                f"{brief}"
            )
            return chat_response, dashboard

        if not frontier:
            tickets = self.scan_tickets_from_tracker(tracker_dir_or_feature_dir)
            if not tickets:
                if lean_summary:
                    base_p = Path(tracker_dir_or_feature_dir)
                    tr_dir = base_p if base_p.name == "issues" else ((base_p / "issues") if (base_p / "issues").is_dir() else base_p)
                    chat_response = self.format_chat_handoff(
                        [],
                        tracker_dir=tr_dir,
                        prefer_paths=prefer_paths or lean_summary,
                        lean_summary=True,
                    )
                else:
                    chat_response = "> ℹ️ No tickets found in the task graph."
            elif any(self._is_status_in_progress(t.get("status", "")) for t in tickets):
                if lean_summary:
                    base_p = Path(tracker_dir_or_feature_dir)
                    tr_dir = base_p.parent if base_p.is_file() else ((base_p / "issues") if (base_p / "issues").is_dir() else base_p)
                    chat_response = self.format_chat_handoff(
                        [],
                        tracker_dir=tr_dir,
                        prefer_paths=prefer_paths or lean_summary,
                        lean_summary=True,
                    )
                else:
                    chat_response = (
                        "> ⏳ Standby: All active frontier tickets are currently in-progress. "
                        "Awaiting worker completion before calculating downstream unblocked tickets."
                    )
            else:
                if lean_summary:
                    base_p = Path(tracker_dir_or_feature_dir)
                    tr_dir = base_p.parent if base_p.is_file() else ((base_p / "issues") if (base_p / "issues").is_dir() else base_p)
                    chat_response = self.format_chat_handoff(
                        [],
                        tracker_dir=tr_dir,
                        prefer_paths=prefer_paths or lean_summary,
                        lean_summary=True,
                    )
                else:
                    chat_response = "> ⚠️ No unblocked frontier tickets available in the task graph. Awaiting dependency resolution."
            return chat_response, dashboard

        base_path = Path(tracker_dir_or_feature_dir)
        if base_path.is_file():
            tracker_dir = base_path.parent
        elif (base_path / "issues").is_dir():
            tracker_dir = base_path / "issues"
        else:
            tracker_dir = base_path
        chat_response = self.format_chat_handoff(
            frontier,
            tracker_dir=tracker_dir,
            prefer_paths=prefer_paths or lean_summary,
            lean_summary=lean_summary,
        )
        return chat_response, dashboard


    def validate_task_dashboard_content(self, content: str) -> tuple[bool, list[str]]:
        """
        [DASH-01] [MOD-COCKPIT-01] Validates Antigravity task graph artifact content:
        - For modular four-stage cockpit: requires 4 stage headers and status progress table. Prunes Mermaid requirement.
        - For legacy dual-layer cockpit: must contain interactive Mermaid DAG ('graph TD').
        - Must contain status progress table with columns: Ticket, Title, Blockers, Status.
        - Strictly forbids raw starter prompt code blocks or fenced /implement commands.
        """
        errors: list[str] = []

        has_stages = bool(
            re.search(r"^##\s*Stage\s+I\s*:", content, re.MULTILINE | re.IGNORECASE)
            and re.search(r"^##\s*Stage\s+II\s*:", content, re.MULTILINE | re.IGNORECASE)
            and re.search(r"^##\s*Stage\s+III\s*:", content, re.MULTILINE | re.IGNORECASE)
            and re.search(r"^##\s*Stage\s+IV\s*:", content, re.MULTILINE | re.IGNORECASE)
        )

        if not has_stages:
            if not re.search(r"(?:`{3,}|~{3,})mermaid\s*\n\s*graph\s+TD", content, re.IGNORECASE):
                errors.append("Task graph artifact missing interactive Mermaid DAG ('graph TD')")

        if not re.search(r"\|\s*Ticket\s*\|\s*Title\s*\|\s*Blockers\s*\|\s*Status\s*\|", content, re.IGNORECASE):
            errors.append("Task graph artifact missing status progress table with columns: Ticket, Title, Blockers, Status")

        if re.search(r"^#+\s*(?:Ready-to-Run\s+)?Starter\s+Prompt", content, re.MULTILINE | re.IGNORECASE):
            errors.append("Task graph artifact contains prohibited 'Ready-to-Run Starter Prompt' section")

        if re.search(r"^#+\s*Layer\s+3", content, re.MULTILINE | re.IGNORECASE):
            errors.append("Task graph artifact contains prohibited 'Layer 3' handoff section")

        code_blocks = re.findall(r"(?:`{3,}|~{3,})[a-zA-Z0-9_-]*\s*\n(.*?)[\s\n]*(?:`{3,}|~{3,})", content, re.DOTALL)
        if any("/implement" in block for block in code_blocks):
            errors.append("Task graph artifact contains prohibited starter prompt code block with '/implement'")

        return len(errors) == 0, errors

    def load_task_graph_template(
        self,
        template_path: Path | str | None = None,
        *,
        root_dir: Path | str | None = None,
    ) -> tuple[str, str | None]:
        """
        [ERR-TMPL-IO] Reads canonical task graph template from disk, trapping missing or unreadable file conditions.
        When skills/to-tickets/resources/task-graph-template.md is missing or unreadable, returns diagnostic:
          [ERR-TMPL-NOT-FOUND] Canonical task graph template missing at skills/to-tickets/resources/task-graph-template.md
        and gracefully falls back to built-in programmatic template strings without throwing unhandled exceptions.

        Returns:
            tuple[content: str, diagnostic: str | None]
        """
        base_root = Path(root_dir) if root_dir else REPO_ROOT
        if template_path is None:
            target = base_root / "skills" / "to-tickets" / "resources" / "task-graph-template.md"
            display_str = "skills/to-tickets/resources/task-graph-template.md"
        else:
            target = Path(template_path)
            if not target.is_absolute():
                target = base_root / target
            try:
                display_str = target.relative_to(base_root).as_posix()
            except ValueError:
                display_str = target.as_posix() if hasattr(target, "as_posix") else str(target)

        if not target.is_file():
            diagnostic = f"[ERR-TMPL-NOT-FOUND] Canonical task graph template missing at {display_str}"
            return DEFAULT_TASK_GRAPH_TEMPLATE, diagnostic

        try:
            content = target.read_text(encoding="utf-8")
            return content, None
        except Exception as exc:
            diagnostic = f"[ERR-TMPL-NOT-FOUND] Canonical task graph template missing at {display_str}: {exc}"
            return DEFAULT_TASK_GRAPH_TEMPLATE, diagnostic

    def validate_task_graph_template_contract(
        self,
        template_or_content: Path | str | None = None,
        *,
        root_dir: Path | str | None = None,
    ) -> tuple[bool, list[str]]:
        """
        [TMPL-01] [EDGE-TMPL-PROMPT-FENCE] Validates canonical task graph template contract:
        - Must exist on disk at skills/to-tickets/resources/task-graph-template.md when checked by path.
        - Must define the Dual-Layer Task Graph Cockpit structure:
          * Layer 1: Macro 8-phase Lifecycle Tracker table
          * Layer 2: Clustered Subgraphs in Mermaid graph TD (S_RESOLVED, S_IN_PROGRESS, S_FRONTIER, S_BLOCKED)
          * 5-column status table (| Ticket | Title | Blockers | Status | Verified Commit |)
        - Must contain zero raw starter prompt code blocks or Layer 3 code fences.
        """
        errors: list[str] = []
        base_root = Path(root_dir) if root_dir else REPO_ROOT

        content = ""
        display_path = "skills/to-tickets/resources/task-graph-template.md"
        if template_or_content is None:
            tmpl_file = base_root / "skills" / "to-tickets" / "resources" / "task-graph-template.md"
            if not tmpl_file.is_file():
                return False, [f"[ERR-TMPL-NOT-FOUND] Canonical task graph template missing at {display_path}"]
            try:
                content = tmpl_file.read_text(encoding="utf-8")
            except Exception as exc:
                return False, [f"[ERR-TMPL-NOT-FOUND] Canonical task graph template missing at {display_path}: {exc}"]
        elif isinstance(template_or_content, Path):
            try:
                target_disp = template_or_content.relative_to(base_root).as_posix()
            except ValueError:
                target_disp = template_or_content.as_posix()
            if not template_or_content.is_file():
                return False, [f"[ERR-TMPL-NOT-FOUND] Canonical task graph template missing at {target_disp}"]
            try:
                content = template_or_content.read_text(encoding="utf-8")
            except Exception as exc:
                return False, [f"[ERR-TMPL-NOT-FOUND] Canonical task graph template missing at {target_disp}: {exc}"]
        else:
            s = str(template_or_content)
            is_path = False
            if "\n" not in s and len(s) < 1024:
                cand = Path(s) if Path(s).is_absolute() else base_root / s
                if cand.is_file() or s.endswith((".md", ".markdown")):
                    is_path = True

            if is_path:
                p = Path(s) if Path(s).is_absolute() else base_root / s
                try:
                    target_disp = p.relative_to(base_root).as_posix()
                except ValueError:
                    target_disp = p.as_posix()
                if not p.is_file():
                    return False, [f"[ERR-TMPL-NOT-FOUND] Canonical task graph template missing at {target_disp}"]
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception as exc:
                    return False, [f"[ERR-TMPL-NOT-FOUND] Canonical task graph template missing at {target_disp}: {exc}"]
            else:
                content = s

        if not content or not content.strip():
            return False, [
                "[ERR-TMPL-CORRUPT] task-graph-template.md violates canonical Dual-Layer Cockpit schema contract (template content is empty)"
            ]

        code_blocks = re.findall(r"(?:`{3,}|~{3,})[a-zA-Z0-9_-]*\s*\n(.*?)[\s\n]*(?:`{3,}|~{3,})", content, re.DOTALL)
        has_prompt_fence = False
        if any("/implement" in b for b in code_blocks):
            has_prompt_fence = True
        if re.search(r"^#+\s*Layer\s+3", content, re.MULTILINE | re.IGNORECASE):
            has_prompt_fence = True
        if re.search(r"^#+\s*(?:Ready-to-Run\s+)?Starter\s+Prompt", content, re.MULTILINE | re.IGNORECASE):
            has_prompt_fence = True

        if has_prompt_fence:
            errors.append(
                "[ERR-TMPL-PROMPT-FENCE] task-graph-template.md must remain purely visual and contain zero raw starter prompt code blocks"
            )

        has_stage_1 = bool(re.search(r"^##\s*Stage\s+I\s*:", content, re.MULTILINE | re.IGNORECASE))
        has_stage_2 = bool(re.search(r"^##\s*Stage\s+II\s*:", content, re.MULTILINE | re.IGNORECASE))
        has_stage_3 = bool(re.search(r"^##\s*Stage\s+III\s*:", content, re.MULTILINE | re.IGNORECASE))
        has_stage_4 = bool(re.search(r"^##\s*Stage\s+IV\s*:", content, re.MULTILINE | re.IGNORECASE))
        is_modular = has_stage_1 and has_stage_2 and has_stage_3 and has_stage_4

        has_l1_table = bool(re.search(r"\|\s*#\s*\|\s*Phase\s*\|\s*Status\s*\|\s*Command\s*\|\s*Deliverable\s*\|", content, re.IGNORECASE))

        mermaid_match = re.search(r"(?:`{3,}|~{3,})mermaid\s*\n(.*?)(?:`{3,}|~~~)", content, re.DOTALL | re.IGNORECASE)
        if not is_modular:
            if not has_l1_table:
                errors.append("[ERR-TMPL-CORRUPT] task-graph-template.md violates canonical Dual-Layer Cockpit schema contract (missing Layer 1 Lifecycle table)")

            if not mermaid_match:
                errors.append("[ERR-TMPL-CORRUPT] task-graph-template.md violates canonical Dual-Layer Cockpit schema contract (missing or unclosed Layer 2 interactive Mermaid DAG block)")
            else:
                mermaid_body = mermaid_match.group(1)
                has_graph_td = bool(re.search(r"^\s*graph\s+TD\b", mermaid_body, re.MULTILINE | re.IGNORECASE))
                required_subgraphs = ("subgraph S_RESOLVED", "subgraph S_IN_PROGRESS", "subgraph S_FRONTIER", "subgraph S_BLOCKED")
                missing_subgraphs = [sg for sg in required_subgraphs if sg not in mermaid_body]
                if not has_graph_td or missing_subgraphs:
                    errors.append("[ERR-TMPL-CORRUPT] task-graph-template.md violates canonical Dual-Layer Cockpit schema contract (missing Layer 2 Clustered Subgraphs in Mermaid graph TD)")
                else:
                    sg_count = len(re.findall(r"\bsubgraph\b", mermaid_body))
                    end_count = len(re.findall(r"\bend\b", mermaid_body))
                    if sg_count > end_count:
                        errors.append("[ERR-TMPL-CORRUPT] task-graph-template.md violates canonical Dual-Layer Cockpit schema contract (unclosed subgraphs in Mermaid graph TD)")
        else:
            if not re.search(r">\s*\*\*Mission\*\*:", content, re.IGNORECASE):
                errors.append("[ERR-TMPL-CORRUPT] task-graph-template.md violates canonical Modular Cockpit schema contract (missing Mission block)")
            if mermaid_match:
                mermaid_body = mermaid_match.group(1)
                has_graph_td = bool(re.search(r"^\s*graph\s+TD\b", mermaid_body, re.MULTILINE | re.IGNORECASE))
                if not has_graph_td:
                    errors.append("[ERR-TMPL-CORRUPT] task-graph-template.md Mermaid block missing graph TD")

        has_status_table = bool(re.search(r"\|\s*Ticket\s*\|\s*Title\s*\|\s*Blockers\s*\|\s*Status\s*\|\s*Verified Commit\s*\|", content, re.IGNORECASE))
        if not has_status_table:
            errors.append("[ERR-TMPL-CORRUPT] task-graph-template.md violates canonical Dual-Layer Cockpit schema contract (missing 5-column status progress table)")

        lines = content.splitlines()
        in_status_section = False
        in_lifecycle_section = False
        for line in lines:
            line_str = line.strip()
            if "Main Flow Lifecycle" in line_str or "Stage I:" in line_str or "Stage III:" in line_str or "Stage IV:" in line_str:
                in_lifecycle_section = True
                in_status_section = False
                continue
            elif "Status Progress Table" in line_str:
                in_status_section = True
                in_lifecycle_section = False
                continue
            elif line_str.startswith("#") or "Dependency DAG" in line_str or "Active Frontier" in line_str:
                in_lifecycle_section = False
                in_status_section = False
                continue

            if in_lifecycle_section:
                if "|" in line_str:
                    clean_pipes = line_str.replace(r"\|", "")
                    pipe_count = clean_pipes.count("|")
                    if not line_str.startswith("|") or not line_str.endswith("|") or pipe_count != 6:
                        errors.append("[ERR-TMPL-CORRUPT] task-graph-template.md violates canonical Dual-Layer Cockpit schema contract (mangled Markdown table pipes in Lifecycle table)")
                        break

            if in_status_section:
                if "|" in line_str:
                    clean_pipes = line_str.replace(r"\|", "")
                    pipe_count = clean_pipes.count("|")
                    if not line_str.startswith("|") or not line_str.endswith("|") or pipe_count != 6:
                        errors.append("[ERR-TMPL-CORRUPT] task-graph-template.md violates canonical Dual-Layer Cockpit schema contract (mangled Markdown table pipes in status progress table)")
                        break

        return len(errors) == 0, errors

    def validate_chat_handoff_content(self, content: str) -> tuple[bool, list[str]]:
        """
        [DASH-02] [DASH-03] Validates Command Center chat handoff response content:
        - Must contain minimal 1-line /implement execution command, OR standby notification when all frontier tickets are in-progress.
        - Strictly forbids multi-line prompt blocks with redundant Context, Seam, or Verification lines.
        """
        errors: list[str] = []

        is_standby = bool(
            re.search(r">\s*⏳?\s*Standby\s*:", content, re.IGNORECASE)
            or ("standby" in content.lower() and "in-progress" in content.lower())
        )
        is_completion = bool(
            re.search(r"(?:/code-review|/security-audit|/pr)", content)
            and any(term in content.lower() for term in ("100%", "complete", "completed", "resolved", "next phase"))
        )

        is_empty_or_blocked = bool(
            re.search(r">\s*(?:⚠️|ℹ️)?\s*(?:No unblocked|No tickets)", content, re.IGNORECASE)
            or re.search(r"🎯\s*\*\*Active Frontier\*\*\s*:\s*⚠️?\s*No (?:unblocked|executable|tickets)", content, re.IGNORECASE)
            or ("0/0 resolved" in content and "no executable tickets found" in content.lower())
            or ("no unblocked frontier tickets" in content.lower())
            or ("escalation" in content.lower())
        )

        has_implement_cmd = bool(
            re.search(r"/implement\s+(?:#?[0-9]{1,3}|\S+\.md|fix\b)", content)
        )

        if not is_standby and not is_completion and not is_empty_or_blocked and not has_implement_cmd:
            errors.append("Chat handoff response: Missing /implement execution command (e.g., '/implement 01')")

        code_blocks = re.findall(r"(?:`{3,}|~{3,})[a-zA-Z0-9_-]*\s*\n(.*?)[\s\n]*(?:`{3,}|~{3,})", content, re.DOTALL)
        has_multiline_block = False
        for block in code_blocks:
            if "/implement" in block:
                non_empty_lines = [l.strip() for l in block.splitlines() if l.strip()]
                if len(non_empty_lines) > 1:
                    has_multiline_block = True
                    break
                block_lower = block.lower()
                if any(k in block_lower for k in ("context:", "seam:", "verification:", "guardrails:")):
                    has_multiline_block = True
                    break

        if not has_multiline_block:
            if re.search(r"/implement[^\n]*\n\s*(?:\*\*|\*)?(?:Context|Seam|Verification|Guardrails)(?:\*\*|\*)?:", content, re.IGNORECASE):
                has_multiline_block = True

        if has_multiline_block:
            errors.append("Chat handoff response contains prohibited multi-line prompt block with redundant Context, Seam, or Verification lines")

        # [SUMM-02] Prohibit raw Mermaid graph code blocks in chat transcripts
        if re.search(r"(?:`{3,}|~{3,})mermaid", content, re.IGNORECASE) or re.search(r"\bgraph\s+TD\b", content):
            errors.append(
                "Chat handoff response contains prohibited Mermaid graph block ('graph TD'); "
                "transcript diagram dumps are prohibited under [SUMM-02] (Mermaid diagrams must reside exclusively in task-graph.md)"
            )

        # [SUMM-02] Prohibit Markdown status progress tables or lifecycle tables in chat transcripts
        if (
            re.search(r"\|\s*Ticket\s*\|\s*Title\s*\|", content, re.IGNORECASE)
            or re.search(r"\|\s*Phase\s*\|\s*Status\s*\|", content, re.IGNORECASE)
            or re.search(r"\|\s*[-:]{3,}\s*\|\s*[-:]{3,}\s*\|", content)
        ):
            errors.append(
                "Chat handoff response contains prohibited Markdown table; "
                "transcript table dumps are prohibited under [SUMM-02] (tabular progress tracking must reside exclusively in task-graph.md)"
            )

        # [SUMM-01] Validate that Task Graph link in lean summary uses absolute file:// URI scheme
        if re.search(r"📁\s*\*\*Task Graph\*\*\s*:", content):
            if not re.search(r"📁\s*\*\*Task Graph\*\*\s*:\s*\[[^\]]+\]\(file://[^\)]+\)", content):
                errors.append(
                    "Chat handoff Task Graph line must format a valid markdown link using the absolute file:// URI scheme [SUMM-01]"
                )

        return len(errors) == 0, errors

    def validate_track_14_invariants(self, root_dir: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 14 Ticket-Centric Metadata & Lean Flow Ergonomics invariants:
        1. Shorthand resolution and autonomous ticket metadata ingestion in skills/implement/SKILL.md ([ING-01], [ING-02])
        2. Sub-Trigger A2 autonomous ticket ingestion & metadata extraction protocol in rules/subagent-orchestration.md
        3. Clean visual task dashboard and single-line chat stream in skills/to-tickets/SKILL.md ([DASH-01], [DASH-02])
        4. Command Center session clean visual dashboard & single-line chat discipline in rules/prompt-engineering-discipline.md
        5. Zero-argument code review with autonomous merge-base, spec detection, and 1-line handoffs in skills/code-review/SKILL.md ([AUTO-01])
        6. Zero-argument security audit scoping against merge base in skills/security-audit/SKILL.md ([AUTO-02])
        7. Autonomous evidence-driven pull request body synthesis in skills/pr/SKILL.md ([AUTO-03])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 7

        # --- Invariant 1: skills/implement/SKILL.md ---
        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "TRACK14_IMPLEMENT_INGESTION_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
        else:
            try:
                imp_content = self._read_skill_with_references(implement_skill)
                if "/implement <NN>" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK14_IMPLEMENT_INGESTION_VIOLATION", "skills/implement/SKILL.md must document /implement <NN> shorthand syntax"))
                if ".scratch/<feature-slug>/issues/<NN>-*.md" not in imp_content and ".scratch/<feature-slug>/issues/<NN>" not in imp_content:
                    errors.append(ValidationError(implement_skill, "TRACK14_IMPLEMENT_INGESTION_VIOLATION", "skills/implement/SKILL.md must document ticket filename pattern matching in issues/"))
                for key in ("**Context:**", "**Seam:**", "**Verification:**", "**Guardrails:**"):
                    if key not in imp_content:
                        errors.append(ValidationError(implement_skill, "TRACK14_IMPLEMENT_INGESTION_VIOLATION", f"skills/implement/SKILL.md must reference ticket metadata key '{key}'"))
                if "without prompting" not in imp_content.lower() and "without asking" not in imp_content.lower():
                    errors.append(ValidationError(implement_skill, "TRACK14_IMPLEMENT_INGESTION_VIOLATION", "skills/implement/SKILL.md must prohibit asking operator for full path or metadata strings"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        # --- Invariant 2: rules/subagent-orchestration.md ---
        orch_rule = root_dir / "rules" / "subagent-orchestration.md"
        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "TRACK14_TRIGGER_A2_INGESTION_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            try:
                o_content = self.read_rule_content(orch_rule)
                if "/implement <NN>" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK14_TRIGGER_A2_INGESTION_VIOLATION", "rules/subagent-orchestration.md Sub-Trigger A2 must document shorthand syntax /implement <NN>"))
                if "Ticket Ingestion & Metadata Extraction Protocol" not in o_content:
                    errors.append(ValidationError(orch_rule, "TRACK14_TRIGGER_A2_INGESTION_VIOLATION", "rules/subagent-orchestration.md Sub-Trigger A2 must define 'Ticket Ingestion & Metadata Extraction Protocol'"))
                for key in ("Context", "Seam", "Verification", "Guardrails"):
                    if key not in o_content:
                        errors.append(ValidationError(orch_rule, "TRACK14_TRIGGER_A2_INGESTION_VIOLATION", f"rules/subagent-orchestration.md must reference metadata field '{key}'"))
            except Exception as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        # --- Invariant 3: skills/to-tickets/SKILL.md ---
        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "TRACK14_TO_TICKETS_DASHBOARD_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            try:
                tt_content = to_tickets_skill.read_text(encoding="utf-8")
                # [DASH-01]: Restrict artifacts to visual Mermaid DAG and status progress table, banning prompt code blocks
                if "Mermaid" not in tt_content or "graph TD" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "TRACK14_TO_TICKETS_DASHBOARD_VIOLATION", "skills/to-tickets/SKILL.md must mandate interactive Mermaid DAG ('graph TD') in task graph artifact"))
                if "progress table" not in tt_content.lower() and "status table" not in tt_content.lower():
                    errors.append(ValidationError(to_tickets_skill, "TRACK14_TO_TICKETS_DASHBOARD_VIOLATION", "skills/to-tickets/SKILL.md must mandate status progress table in task graph artifact"))
                if "prohibit" not in tt_content.lower() and "ban" not in tt_content.lower() and "eliminate" not in tt_content.lower():
                    errors.append(ValidationError(to_tickets_skill, "TRACK14_TO_TICKETS_DASHBOARD_VIOLATION", "skills/to-tickets/SKILL.md must prohibit raw starter prompt code blocks in task graph artifacts"))
                if "[DASH-01]" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "TRACK14_TO_TICKETS_DASHBOARD_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [DASH-01]"))

                # [DASH-02]: Format Command Center chat handoff recommendations as minimal 1-line execution commands
                if "/implement 01" not in tt_content and "/implement <NN>" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "TRACK14_TO_TICKETS_CHAT_HANDOFF_VIOLATION", "skills/to-tickets/SKILL.md must document minimal 1-line command in chat handoff (e.g., '/implement 01')"))
                if "1-line" not in tt_content and "one-line" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "TRACK14_TO_TICKETS_CHAT_HANDOFF_VIOLATION", "skills/to-tickets/SKILL.md must mandate 1-line execution command in chat handoff"))
                if "multi-frontier" not in tt_content.lower():
                    errors.append(ValidationError(to_tickets_skill, "TRACK14_TO_TICKETS_CHAT_HANDOFF_VIOLATION", "skills/to-tickets/SKILL.md must mandate multi-frontier handoff for unblocked tickets"))
                if "[DASH-02]" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "TRACK14_TO_TICKETS_CHAT_HANDOFF_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [DASH-02]"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        # --- Invariant 4: rules/prompt-engineering-discipline.md & rules/operational-invariants.md ---
        prompt_rule = root_dir / "rules" / "prompt-engineering-discipline.md"
        if not prompt_rule.is_file():
            errors.append(ValidationError(prompt_rule, "TRACK14_PROMPT_DISCIPLINE_VIOLATION", "Missing required rule 'rules/prompt-engineering-discipline.md'"))
        else:
            try:
                pr_content = prompt_rule.read_text(encoding="utf-8")
                if "Command Center Session" not in pr_content:
                    errors.append(ValidationError(prompt_rule, "TRACK14_PROMPT_DISCIPLINE_VIOLATION", "rules/prompt-engineering-discipline.md missing Section 5 Command Center Session specification"))
                if "[DASH-01]" not in pr_content:
                    errors.append(ValidationError(prompt_rule, "TRACK14_PROMPT_DISCIPLINE_VIOLATION", "rules/prompt-engineering-discipline.md Section 5 must reference [DASH-01]"))
                if "[DASH-02]" not in pr_content:
                    errors.append(ValidationError(prompt_rule, "TRACK14_PROMPT_DISCIPLINE_VIOLATION", "rules/prompt-engineering-discipline.md Section 5 must reference [DASH-02]"))
                if "/implement 01" not in pr_content:
                    errors.append(ValidationError(prompt_rule, "TRACK14_PROMPT_DISCIPLINE_VIOLATION", "rules/prompt-engineering-discipline.md Section 5 must document minimal 1-line command (e.g. '/implement 01')"))
                if "1-line" not in pr_content and "one-line" not in pr_content:
                    errors.append(ValidationError(prompt_rule, "TRACK14_PROMPT_DISCIPLINE_VIOLATION", "rules/prompt-engineering-discipline.md Section 5 must mandate 1-line execution recommendations"))
            except Exception as exc:
                errors.append(ValidationError(prompt_rule, "FILE_READ_ERROR", f"Could not read {prompt_rule.name}: {exc}"))

        op_rule = root_dir / "rules" / "operational-invariants.md"
        if op_rule.is_file():
            try:
                op_content = op_rule.read_text(encoding="utf-8")
                if "multi-frontier" not in op_content.lower():
                    errors.append(ValidationError(op_rule, "TRACK14_OPERATIONAL_INVARIANTS_MULTI_FRONTIER_VIOLATION", "rules/operational-invariants.md Section 4 must codify multi-frontier handoff"))
            except Exception as exc:
                errors.append(ValidationError(op_rule, "FILE_READ_ERROR", f"Could not read {op_rule.name}: {exc}"))

        # --- Invariant 5: skills/code-review/SKILL.md ([AUTO-01]) ---
        code_review_skill = root_dir / "skills" / "code-review" / "SKILL.md"
        if not code_review_skill.is_file():
            errors.append(ValidationError(code_review_skill, "TRACK14_CODE_REVIEW_AUTO_VIOLATION", "Missing required skill 'skills/code-review/SKILL.md'"))
        else:
            try:
                cr_content = code_review_skill.read_text(encoding="utf-8")
                if "[AUTO-01]" not in cr_content:
                    errors.append(ValidationError(code_review_skill, "TRACK14_CODE_REVIEW_AUTO_VIOLATION", "skills/code-review/SKILL.md must reference scenario [AUTO-01]"))
                if "git merge-base main HEAD" not in cr_content:
                    errors.append(ValidationError(code_review_skill, "TRACK14_CODE_REVIEW_AUTO_VIOLATION", "skills/code-review/SKILL.md must mandate autonomous base fixed point determination via 'git merge-base main HEAD'"))
                if ".scratch/<feature-slug>/spec.md" not in cr_content and "docs/specs/" not in cr_content:
                    errors.append(ValidationError(code_review_skill, "TRACK14_CODE_REVIEW_AUTO_VIOLATION", "skills/code-review/SKILL.md must document autonomous spec resolution from .scratch/<feature-slug>/spec.md or docs/specs/"))
                if "without prompting" not in cr_content.lower() and "without asking" not in cr_content.lower():
                    errors.append(ValidationError(code_review_skill, "TRACK14_CODE_REVIEW_AUTO_VIOLATION", "skills/code-review/SKILL.md must mandate zero-argument resolution without prompting the user"))
                if "1-line" not in cr_content and "one-line" not in cr_content and "single-line" not in cr_content:
                    errors.append(ValidationError(code_review_skill, "TRACK14_CODE_REVIEW_AUTO_VIOLATION", "skills/code-review/SKILL.md Step 6 must mandate 1-line execution handoff commands"))
                if "/security-audit" not in cr_content or "/pr" not in cr_content:
                    errors.append(ValidationError(code_review_skill, "TRACK14_CODE_REVIEW_AUTO_VIOLATION", "skills/code-review/SKILL.md Step 6 must provide 1-line execution commands for /security-audit and /pr"))
                if "Branch: <current-branch>" in cr_content or "Target: Staged changes" in cr_content or "Base: <base-ref>" in cr_content:
                    errors.append(ValidationError(code_review_skill, "TRACK14_CODE_REVIEW_AUTO_VIOLATION", "skills/code-review/SKILL.md Step 6 must eliminate multi-line starter prompt blocks in chat handoff"))
            except Exception as exc:
                errors.append(ValidationError(code_review_skill, "FILE_READ_ERROR", f"Could not read {code_review_skill.name}: {exc}"))

        # --- Invariant 6: skills/security-audit/SKILL.md ([AUTO-02]) ---
        security_audit_skill = root_dir / "skills" / "security-audit" / "SKILL.md"
        if not security_audit_skill.is_file():
            errors.append(ValidationError(security_audit_skill, "TRACK14_SECURITY_AUDIT_AUTO_VIOLATION", "Missing required skill 'skills/security-audit/SKILL.md'"))
        else:
            try:
                sa_content = security_audit_skill.read_text(encoding="utf-8")
                if "[AUTO-02]" not in sa_content:
                    errors.append(ValidationError(security_audit_skill, "TRACK14_SECURITY_AUDIT_AUTO_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [AUTO-02]"))
                if "git merge-base main HEAD" not in sa_content:
                    errors.append(ValidationError(security_audit_skill, "TRACK14_SECURITY_AUDIT_AUTO_VIOLATION", "skills/security-audit/SKILL.md must document autonomous diff computation against base branch via 'git merge-base main HEAD'"))
                if "without prompting" not in sa_content.lower() and "without requiring flags" not in sa_content.lower():
                    errors.append(ValidationError(security_audit_skill, "TRACK14_SECURITY_AUDIT_AUTO_VIOLATION", "skills/security-audit/SKILL.md must mandate zero-argument scoping without prompting for scoping flags"))
                if "/security-audit" not in sa_content:
                    errors.append(ValidationError(security_audit_skill, "TRACK14_SECURITY_AUDIT_AUTO_VIOLATION", "skills/security-audit/SKILL.md must document /security-audit invocation"))
                if "5 full-stack threat vectors" not in sa_content:
                    errors.append(ValidationError(security_audit_skill, "TRACK14_SECURITY_AUDIT_AUTO_VIOLATION", "skills/security-audit/SKILL.md must mandate evaluation against the 5 full-stack threat vectors"))
            except Exception as exc:
                errors.append(ValidationError(security_audit_skill, "FILE_READ_ERROR", f"Could not read {security_audit_skill.name}: {exc}"))

        # --- Invariant 7: skills/pr/SKILL.md ([AUTO-03]) ---
        pr_skill = root_dir / "skills" / "pr" / "SKILL.md"
        if not pr_skill.is_file():
            errors.append(ValidationError(pr_skill, "TRACK14_PR_AUTO_VIOLATION", "Missing required skill 'skills/pr/SKILL.md'"))
        else:
            try:
                pr_content = pr_skill.read_text(encoding="utf-8")
                if "[AUTO-03]" not in pr_content:
                    errors.append(ValidationError(pr_skill, "TRACK14_PR_AUTO_VIOLATION", "skills/pr/SKILL.md must reference scenario [AUTO-03]"))
                if ".scratch/<feature-slug>/issues/" not in pr_content and "issues/" not in pr_content:
                    errors.append(ValidationError(pr_skill, "TRACK14_PR_AUTO_VIOLATION", "skills/pr/SKILL.md must document gathering completed tickets from .scratch/<feature-slug>/issues/"))
                if "specification delta" not in pr_content.lower() and "spec delta" not in pr_content.lower():
                    errors.append(ValidationError(pr_skill, "TRACK14_PR_AUTO_VIOLATION", "skills/pr/SKILL.md must document extracting Specification Delta from active spec file"))
                if "without requesting manual prose" not in pr_content.lower() and "without asking" not in pr_content.lower() and "zero-prose" not in pr_content.lower():
                    errors.append(ValidationError(pr_skill, "TRACK14_PR_AUTO_VIOLATION", "skills/pr/SKILL.md must compile PR body without requesting manual prose input from operator"))
                if "verdict" not in pr_content.lower() and "audit" not in pr_content.lower():
                    errors.append(ValidationError(pr_skill, "TRACK14_PR_AUTO_VIOLATION", "skills/pr/SKILL.md must document gathering review and security verdicts"))
            except Exception as exc:
                errors.append(ValidationError(pr_skill, "FILE_READ_ERROR", f"Could not read {pr_skill.name}: {exc}"))

        return invariants_scanned, errors

    def validate_hierarchical_worker_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Hierarchical Subagent Orchestration & Context Hygiene invariants in agents/spec-implementer.md:
        1. Tool whitelisting: invoke_subagent present in tools: list ([HIER-01])
        2. Child delegation to bug-diagnoser (Workspace: "inherit") and distilled send_message ingestion ([HIER-01])
        3. Strict Level-2 depth limit enforcement prohibiting spec-implementer recursions and leaf subagent recursion ([DEPTH-01])
        4. Targeted test runner execution scoping with -t and --bail 1 ([HYG-01])
        5. Line-bounded file inspection (StartLine/EndLine) for files exceeding 100 lines ([HYG-02])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 5

        root = Path(root_dir)
        implementer_agent = root / "agents" / "spec-implementer.md"
        if not implementer_agent.is_file():
            errors.append(ValidationError(implementer_agent, "HIERARCHICAL_WORKER_VIOLATION", "Missing required agent 'agents/spec-implementer.md'"))
            return invariants_scanned, errors

        try:
            content = implementer_agent.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(implementer_agent, "FILE_READ_ERROR", f"Could not read {implementer_agent.name}: {exc}"))
            return invariants_scanned, errors

        try:
            fm = parse_frontmatter(content)
        except Exception as exc:
            errors.append(ValidationError(implementer_agent, "FRONTMATTER_MALFORMED", f"Could not parse frontmatter: {exc}"))
            return invariants_scanned, errors

        # 1. invoke_subagent in tools: ([HIER-01])
        tools = fm.get("tools")
        if not isinstance(tools, list) or "invoke_subagent" not in tools:
            errors.append(ValidationError(implementer_agent, "HIERARCHICAL_TOOL_VIOLATION", "agents/spec-implementer.md tools must include 'invoke_subagent' ([HIER-01])"))

        # 2. bug-diagnoser delegation rules ([HIER-01])
        if "[HIER-01]" not in content:
            errors.append(ValidationError(implementer_agent, "HIERARCHICAL_DELEGATION_VIOLATION", "agents/spec-implementer.md must reference scenario [HIER-01]"))
        if "bug-diagnoser" not in content:
            errors.append(ValidationError(implementer_agent, "HIERARCHICAL_DELEGATION_VIOLATION", "agents/spec-implementer.md must document delegating test failure diagnosis to 'bug-diagnoser'"))
        if 'Workspace: "inherit"' not in content and "Workspace: 'inherit'" not in content:
            errors.append(ValidationError(implementer_agent, "HIERARCHICAL_DELEGATION_VIOLATION", "agents/spec-implementer.md must specify Workspace: 'inherit' for child subagent diagnostics"))
        if "send_message" not in content:
            errors.append(ValidationError(implementer_agent, "HIERARCHICAL_DELEGATION_VIOLATION", "agents/spec-implementer.md must specify ingesting distilled diagnostic findings via send_message"))

        # 3. Strict Level-2 depth limit ([DEPTH-01])
        if "[DEPTH-01]" not in content:
            errors.append(ValidationError(implementer_agent, "DEPTH_LIMIT_VIOLATION", "agents/spec-implementer.md must reference scenario [DEPTH-01]"))
        if "Level-2" not in content and "level-2" not in content:
            errors.append(ValidationError(implementer_agent, "DEPTH_LIMIT_VIOLATION", "agents/spec-implementer.md must document Level-2 depth limit"))
        body = content.split("---", 2)[2] if content.count("---") >= 2 else content
        prohibit_spec_implementer = bool(
            re.search(r"(prohibit|bar|forbid)[^.\n]*spec-implementer", body, re.IGNORECASE)
            or re.search(r"spec-implementer[^.\n]*(prohibit|bar|forbid)", body, re.IGNORECASE)
        )
        if not prohibit_spec_implementer:
            errors.append(ValidationError(implementer_agent, "DEPTH_LIMIT_VIOLATION", "agents/spec-implementer.md must explicitly prohibit spawning another spec-implementer"))
        leaf_subagent_barred = (
            "further subagents" in content.lower()
            or "spawn further" in content.lower()
            or "subagent recursion" in content.lower()
        )
        if not leaf_subagent_barred:
            errors.append(ValidationError(implementer_agent, "DEPTH_LIMIT_VIOLATION", "agents/spec-implementer.md must bar leaf child subagents from spawning further subagents"))

        # 4. Targeted test runner execution ([HYG-01])
        if "[HYG-01]" not in content:
            errors.append(ValidationError(implementer_agent, "TERMINAL_HYGIENE_VIOLATION", "agents/spec-implementer.md must reference scenario [HYG-01]"))
        if "--bail" not in content or ("-t" not in content and "--testNamePattern" not in content):
            errors.append(ValidationError(implementer_agent, "TERMINAL_HYGIENE_VIOLATION", "agents/spec-implementer.md must mandate targeted test runner flags (-t and --bail 1)"))

        # 5. Bounded file inspection ([HYG-02])
        if "[HYG-02]" not in content:
            errors.append(ValidationError(implementer_agent, "TERMINAL_HYGIENE_VIOLATION", "agents/spec-implementer.md must reference scenario [HYG-02]"))
        if "StartLine" not in content or "EndLine" not in content:
            errors.append(ValidationError(implementer_agent, "TERMINAL_HYGIENE_VIOLATION", "agents/spec-implementer.md must mandate StartLine and EndLine bounded file inspections"))
        if "100" not in content:
            errors.append(ValidationError(implementer_agent, "TERMINAL_HYGIENE_VIOLATION", "agents/spec-implementer.md must specify 100-line threshold for bounded inspection"))

        return invariants_scanned, errors

    def validate_adaptive_pipeline_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Single-Hop Builder & Micro-Worker Pipeline invariants in skills/implement/SKILL.md:
        1. Single-Hop Subagent Builder delegation on ticket invocations ([ORCH-01])
        2. Single-turn direct execution escape hatch via --direct ([ORCH-02])
        3. Milestone notification formatting for Stage 1 Builder
        4. Prohibition of obsolete Stage 1A/1B decomposition ([PRUNE-01])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 5

        root = Path(root_dir)
        skill_file = root / "skills" / "implement" / "SKILL.md"
        if not skill_file.is_file():
            errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
            return invariants_scanned, errors

        try:
            content = skill_file.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(skill_file, "FILE_READ_ERROR", f"Could not read {skill_file.name}: {exc}"))
            return invariants_scanned, errors

        # 1. Scenario tags [ORCH-01], [ORCH-02]
        for tag in ["[ORCH-01]", "[ORCH-02]"]:
            if tag not in content:
                errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", f"skills/implement/SKILL.md must reference scenario {tag}"))

        # 2. [ORCH-01] Micro-swarm delegation & Pipeline Coordinator
        if "Pipeline Coordinator" not in content:
            errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", "skills/implement/SKILL.md must specify Pipeline Coordinator persona transition"))
        if 'Workspace: "inherit"' not in content and "Workspace: 'inherit'" not in content:
            errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", "skills/implement/SKILL.md must specify Workspace: 'inherit' for builder subagents"))

        # 3. [ORCH-02] --direct escape hatch
        if "--direct" not in content:
            errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", "skills/implement/SKILL.md must document --direct execution flag"))

        # 4. Milestone notification and single-hop builder
        if "> 🚀 **Stage 1 (Builder)**: Subagent `spec-implementer` active in background" not in content:
            errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", "skills/implement/SKILL.md must specify milestone notification format for Stage 1 (Builder)"))
        if "flowchart TD" not in content:
            errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", "skills/implement/SKILL.md must contain a mermaid flowchart TD"))

        # 5. [PRUNE-01] Obsolete Stage 1A/1B decomposition, --split flag, and mandatory review must be excised
        if "Stage 1A" in content or "Stage 1B" in content:
            errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", "skills/implement/SKILL.md must not contain obsolete Stage 1A or Stage 1B decomposition"))
        if "--split" in content:
            errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", "skills/implement/SKILL.md must not contain obsolete --split flag"))
        if "Two-Stage Micro-Swarm is mandatory" in content or "Every commit requires review" in content:
            errors.append(ValidationError(skill_file, "ADAPTIVE_PIPELINE_VIOLATION", "skills/implement/SKILL.md must not claim Stage 2 review is mandatory on every ticket"))

        return invariants_scanned, errors

    def validate_orchestration_routing_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Phase Boundary Specification Routing ([ROUT-01]) and Sub-Trigger A3 Hierarchical Worker Delegation:
        1. skills/grill-with-docs/SKILL.md enforces mandatory delta specification routing for living specs ([ROUT-01])
        2. skills/grill-with-docs/SKILL.md explicitly prohibits shortcuts directly to /implement or /to-tickets
        3. rules/subagent-orchestration.md documents Sub-Trigger A2 single-hop builder & direct execution ([ORCH-01], [ORCH-02])
        4. rules/subagent-orchestration.md documents Sub-Trigger A3 hierarchical delegation & Level-2 depth limit ([DEPTH-01], [HIER-01], [HYG-01], [HYG-02])
        5. rules/operational-invariants.md and rules/AGENTS.md reflect Sub-Trigger A3 summary integrity
        6. Expanded rule size in rules/AGENTS.md remains <= 20,000 bytes
        """
        errors: list[ValidationError] = []
        invariants_scanned = 6

        root = Path(root_dir)
        grill_skill = root / "skills" / "grill-with-docs" / "SKILL.md"
        if not grill_skill.is_file():
            errors.append(ValidationError(grill_skill, "ROUTING_VIOLATION", "Missing required skill 'skills/grill-with-docs/SKILL.md'"))
            return invariants_scanned, errors

        try:
            grill_content = grill_skill.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(grill_skill, "FILE_READ_ERROR", f"Could not read {grill_skill.name}: {exc}"))
            return invariants_scanned, errors

        # 1. [ROUT-01] Mandatory delta specification routing for living specs
        if "[ROUT-01]" not in grill_content:
            errors.append(ValidationError(grill_skill, "ROUTING_VIOLATION", "skills/grill-with-docs/SKILL.md must reference scenario [ROUT-01]"))
        if "/to-spec --delta" not in grill_content:
            errors.append(ValidationError(grill_skill, "ROUTING_VIOLATION", "skills/grill-with-docs/SKILL.md must mandate '/to-spec --delta <domain>' for living specifications"))
        if "docs/specs/" not in grill_content and "living spec" not in grill_content.lower():
            errors.append(ValidationError(grill_skill, "ROUTING_VIOLATION", "skills/grill-with-docs/SKILL.md must reference living specifications in docs/specs/<domain>/spec.md"))

        # 2. Prohibition of shortcuts directly to /implement or /to-tickets
        prohibits_shortcut = (
            ("prohibit" in grill_content.lower() or "bar" in grill_content.lower() or "never" in grill_content.lower())
            and ("/implement" in grill_content or "/to-tickets" in grill_content)
        )
        if not prohibits_shortcut:
            errors.append(ValidationError(grill_skill, "ROUTING_VIOLATION", "skills/grill-with-docs/SKILL.md must explicitly prohibit direct shortcuts to /implement or /to-tickets without delta reconciliation"))

        # 3. rules/subagent-orchestration.md Sub-Trigger A2 and Sub-Trigger A3
        orch_rules = root / "rules" / "subagent-orchestration.md"
        if not orch_rules.is_file():
            errors.append(ValidationError(orch_rules, "ORCHESTRATION_RULE_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
            return invariants_scanned, errors

        try:
            orch_content = self.read_rule_content(orch_rules)
        except Exception as exc:
            errors.append(ValidationError(orch_rules, "FILE_READ_ERROR", f"Could not read {orch_rules.name}: {exc}"))
            return invariants_scanned, errors

        # Sub-Trigger A2 single-hop builder & direct execution ([ORCH-01], [ORCH-02])
        for tag in ["[ORCH-01]", "[ORCH-02]"]:
            if tag not in orch_content:
                errors.append(ValidationError(orch_rules, "ORCHESTRATION_RULE_VIOLATION", f"rules/subagent-orchestration.md must reference scenario {tag} in Sub-Trigger A2"))
        if "Stage 1A" in orch_content or "Stage 1B" in orch_content:
            errors.append(ValidationError(orch_rules, "ORCHESTRATION_RULE_VIOLATION", "rules/subagent-orchestration.md must not contain obsolete Stage 1A or Stage 1B decomposition"))
        if "--split" in orch_content:
            errors.append(ValidationError(orch_rules, "ORCHESTRATION_RULE_VIOLATION", "rules/subagent-orchestration.md must not contain obsolete --split flag"))

        # Sub-Trigger A3 hierarchical delegation & Level-2 depth limit
        if "Sub-Trigger A3" not in orch_content:
            errors.append(ValidationError(orch_rules, "ORCHESTRATION_RULE_VIOLATION", "rules/subagent-orchestration.md must author Sub-Trigger A3"))
        if "[HIER-01]" not in orch_content or "bug-diagnoser" not in orch_content:
            errors.append(ValidationError(orch_rules, "ORCHESTRATION_RULE_VIOLATION", "rules/subagent-orchestration.md must document hierarchical delegation to bug-diagnoser ([HIER-01])"))
        if "[DEPTH-01]" not in orch_content or "Level-2" not in orch_content:
            errors.append(ValidationError(orch_rules, "DEPTH_LIMIT_VIOLATION", "rules/subagent-orchestration.md must document strict Level-2 depth limit ([DEPTH-01])"))
        if not bool(re.search(r"(prohibit|bar|forbid)[^.\n]*spec-implementer", orch_content, re.IGNORECASE) or re.search(r"spec-implementer[^.\n]*(prohibit|bar|forbid)", orch_content, re.IGNORECASE)):
            errors.append(ValidationError(orch_rules, "DEPTH_LIMIT_VIOLATION", "rules/subagent-orchestration.md must prohibit spawning another spec-implementer"))
        if "[HYG-01]" not in orch_content or "--bail" not in orch_content:
            errors.append(ValidationError(orch_rules, "TERMINAL_HYGIENE_VIOLATION", "rules/subagent-orchestration.md must document targeted test runner flags ([HYG-01])"))
        if "[HYG-02]" not in orch_content or "StartLine" not in orch_content or "EndLine" not in orch_content:
            errors.append(ValidationError(orch_rules, "TERMINAL_HYGIENE_VIOLATION", "rules/subagent-orchestration.md must document bounded file inspection ([HYG-02])"))

        # 4. rules/operational-invariants.md and rules/AGENTS.md
        op_inv = root / "rules" / "operational-invariants.md"
        if op_inv.is_file():
            try:
                op_content = op_inv.read_text(encoding="utf-8")
                if "Sub-Trigger A3" not in op_content:
                    errors.append(ValidationError(op_inv, "ORCHESTRATION_RULE_VIOLATION", "rules/operational-invariants.md must reference Sub-Trigger A3"))
            except Exception as exc:
                errors.append(ValidationError(op_inv, "FILE_READ_ERROR", f"Could not read {op_inv.name}: {exc}"))

        agents_rule = root / "rules" / "AGENTS.md"
        if agents_rule.is_file():
            try:
                agents_content = agents_rule.read_text(encoding="utf-8")
                if "Sub-Trigger A3" not in agents_content:
                    errors.append(ValidationError(agents_rule, "ORCHESTRATION_RULE_VIOLATION", "rules/AGENTS.md must reference Sub-Trigger A3 in composed architecture summary"))
                size, size_errors = self.validate_rule_size_limit(agents_rule)
                errors.extend(size_errors)
            except Exception as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        return invariants_scanned, errors

    def validate_upstream_ticket_standards(self, root_dir: Path) -> tuple[int, list[ValidationError]]:
        """
        Validates Upstream Ticket Generation Standards & Tracer-Bullet Slicing Ceiling ([UP-01], [UP-02]):
        1. skills/to-tickets/SKILL.md:
           - Declares scenario tags [UP-01] and [UP-02]
           - Mandates concrete seam anchors ('path/to/file.ts#symbol_or_handler') and prohibits broad directories
           - Mandates targeted test commands in Verification and prohibits broad repository test suites
           - Mandates tracer-bullet slicing ceiling of maximum 2 Given/When/Then scenarios per ticket
        2. skills/to-tickets/resources/ (ticket-template-local.md and ticket-template-github.md):
           - Declares concrete seam anchor placeholder with '#symbol' and prohibits broad directories
           - Declares targeted test command placeholder with runner flag ('-t ' or '-k ') and prohibits broad repository test suites
           - Restricts Acceptance Criteria to <= 2 scenarios
        """
        errors: list[ValidationError] = []
        invariants_scanned = 0

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                tt_content = to_tickets_skill.read_text(encoding="utf-8")
                if "[UP-01]" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [UP-01]"))
                if "path/to/file.ts#symbol_or_handler" not in tt_content and "#symbol_or_handler" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must mandate concrete seam anchor format 'path/to/file.ts#symbol_or_handler'"))
                if not ("broad director" in tt_content.lower() or "broad module" in tt_content.lower()):
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must strictly prohibit broad directories or ambiguous module paths in seams"))
                if not ("targeted test" in tt_content.lower() or "targeted verification" in tt_content.lower()):
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must require targeted test commands in Verification"))
                if not ("broad repository test" in tt_content.lower() or "broad test suite" in tt_content.lower() or "global test" in tt_content.lower()):
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must strictly prohibit broad repository test suites in ticket contracts"))

                if "[UP-02]" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [UP-02]"))
                if not ("maximum of 2" in tt_content.lower() or "<= 2" in tt_content or "1–2" in tt_content or "1-2" in tt_content):
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must strictly bound ticket slicing to a maximum of 2 scenarios"))
                if not ("partition" in tt_content.lower() or "split" in tt_content.lower()):
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must mandate splitting or partitioning slices with 3 or more scenarios"))
                if "[UP-03]" not in tt_content:
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [UP-03]"))
                if "completion protocol" not in tt_content.lower():
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must mandate '## Completion Protocol' in ticket generation"))
                if "[clone-01]" not in tt_content.lower():
                    errors.append(ValidationError(to_tickets_skill, "UPSTREAM_TICKET_STANDARDS_VIOLATION", "skills/to-tickets/SKILL.md must prohibit legacy ticket cloning ([CLONE-01])"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        # Both local markdown and GitHub issue templates must enforce identical execution contracts
        templates_dir = root_dir / "skills" / "to-tickets" / "resources"
        template_names = ("ticket-template-local.md", "ticket-template-github.md")
        for template_name in template_names:
            template_path = templates_dir / template_name
            if not template_path.is_file():
                errors.append(ValidationError(template_path, "UPSTREAM_TICKET_STANDARDS_VIOLATION", f"Missing required template '{template_name}'"))
                continue
            invariants_scanned += 1
            try:
                tmpl_content = template_path.read_text(encoding="utf-8")
                if "#symbol" not in tmpl_content:
                    errors.append(ValidationError(template_path, "UPSTREAM_TICKET_STANDARDS_VIOLATION", f"{template_name} Seam key must specify concrete file#symbol anchor"))
                if not ("broad director" in tmpl_content.lower() or "broad module" in tmpl_content.lower()):
                    errors.append(ValidationError(template_path, "UPSTREAM_TICKET_STANDARDS_VIOLATION", f"{template_name} Seam key must prohibit broad directories"))
                if not ("-t " in tmpl_content or "-k " in tmpl_content):
                    errors.append(ValidationError(template_path, "UPSTREAM_TICKET_STANDARDS_VIOLATION", f"{template_name} Verification key must demonstrate targeted test runner flags (-t or -k)"))
                if not ("broad repository test" in tmpl_content.lower() or "broad test suite" in tmpl_content.lower()):
                    errors.append(ValidationError(template_path, "UPSTREAM_TICKET_STANDARDS_VIOLATION", f"{template_name} Verification key must prohibit broad test suites"))
                if not ("2" in tmpl_content and ("max" in tmpl_content.lower() or "scenario" in tmpl_content.lower())):
                    errors.append(ValidationError(template_path, "UPSTREAM_TICKET_STANDARDS_VIOLATION", f"{template_name} must specify <= 2 scenarios ceiling in Acceptance Criteria or Sizing"))
                if "completion protocol" not in tmpl_content.lower():
                    errors.append(ValidationError(template_path, "UPSTREAM_TICKET_STANDARDS_VIOLATION", f"{template_name} must declare '## Completion Protocol'"))
            except Exception as exc:
                errors.append(ValidationError(template_path, "FILE_READ_ERROR", f"Could not read {template_name}: {exc}"))

        return invariants_scanned, errors

    def validate_preflight_ticket_rejection_gate(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Pre-Flight Ticket Rejection Gate Invariants ([REJ-01], [REJ-02]):
        1. skills/implement/SKILL.md:
           - Declares scenario tags [REJ-01] and [REJ-02]
           - Documents the Pre-Flight Ticket Rejection Gate in Step 1
           - Mandates line range anchors in Context ('#Lxx-Lyy')
           - Mandates concrete file and symbol anchor in Seam ('#symbol') and prohibits broad directories
           - Mandates targeted test commands in Verification and prohibits broad repository test suites
           - Mandates Given/When/Then or tagged BDD scenarios in Acceptance Criteria
           - Mandates immediate HALT with structured rejection diagnostic directing to /to-tickets upon rejection
           - Mandates gate clearance with status 0 and proceeding to subagent dispatch upon valid execution contracts
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
            return 0, errors

        invariants_scanned += 1
        try:
            content = self._read_skill_with_references(implement_skill)
            if "[REJ-01]" not in content:
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must reference scenario [REJ-01]"))
            if "[REJ-02]" not in content:
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must reference scenario [REJ-02]"))
            if "Pre-Flight Ticket Rejection Gate" not in content and "Pre-Flight Ticket Rejection" not in content:
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must document 'Pre-Flight Ticket Rejection Gate' in Step 1"))
            if not ("line range anchor" in content.lower() or "line anchor" in content.lower()):
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must mandate exact line range anchors (#Lxx-Lyy) in Context"))
            if not ("#symbol" in content or "symbol anchor" in content.lower() or "file and symbol" in content.lower()):
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must mandate concrete seam anchor with symbol ('#symbol')"))
            if not ("broad director" in content.lower() or "broad module" in content.lower()):
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must strictly prohibit broad directories in Seam"))
            if not ("targeted test" in content.lower() or "targeted verification" in content.lower()):
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must mandate targeted test commands in Verification"))
            if not ("broad repository test" in content.lower() or "broad test suite" in content.lower()):
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must strictly prohibit broad repository test suites in Verification"))
            if not ("given/when/then" in content.lower() or "bdd scenario" in content.lower()):
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must require Given/When/Then or tagged BDD scenarios in Acceptance Criteria"))
            if "[COMP-01]" not in content:
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must reference scenario [COMP-01]"))
            if "completion protocol" not in content.lower():
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must require '## Completion Protocol' check in Pre-Flight Ticket Rejection Gate"))
            if not ("halt" in content.lower() and "/to-tickets" in content):
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must mandate immediate HALT and direction to regenerate via /to-tickets upon contract failure"))
            if not ("status 0" in content.lower() or "exit status 0" in content.lower() or "clears" in content.lower()):
                errors.append(ValidationError(implement_skill, "PREFLIGHT_GATE_VIOLATION", "skills/implement/SKILL.md must specify clearing gate with status 0 upon valid contracts"))
        except Exception as exc:
            errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        return invariants_scanned, errors

    def validate_zero_pre_execution_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Zero Primary Session Pre-Execution Invariants ([PRE-01], [PRE-02]):
        1. skills/implement/SKILL.md:
           - Declares scenario tags [PRE-01] and [PRE-02]
           - Documents 'Zero Primary Session Pre-Execution Invariant' in Step 1
           - Does not contain ambiguous 'Empirical Gate Pre-Flight' in Step 1
           - Prohibits primary session test runners, compilers, linters, or build scripts prior to subagent dispatch ([PRE-01])
           - Restricts pre-flight file reading to at most two files ([PRE-02])
           - Mandates invoke_subagent dispatch on Turn 1 or Turn 2 ([PRE-02])
        2. rules/subagent-orchestration.md:
           - Sub-Trigger A2 documents Zero Primary Session Pre-Execution Invariant ([PRE-01], [PRE-02])
           - Prohibits primary session test runners before subagent dispatch ([PRE-01])
           - Restricts file reading to at most two files ([PRE-02])
           - Mandates dispatch on Turn 1 or Turn 2 ([PRE-02])
        3. rules/operational-invariants.md:
           - Documents Zero Primary Session Pre-Execution Invariant ([PRE-01], [PRE-02])
           - Prohibits primary session test runners or compilers before dispatch ([PRE-01])
           - Restricts file reading to at most two files ([PRE-02])
           - Mandates dispatch on Turn 1 or Turn 2 ([PRE-02])
        4. rules/AGENTS.md:
           - Summarizes Zero Primary Session Pre-Execution Invariant
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "ZERO_PRE_EXECUTION_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = self._read_skill_with_references(implement_skill)
                if "[PRE-01]" not in content:
                    errors.append(ValidationError(implement_skill, "ZERO_PRE_EXECUTION_VIOLATION", "skills/implement/SKILL.md must reference scenario [PRE-01]"))
                if "[PRE-02]" not in content:
                    errors.append(ValidationError(implement_skill, "ZERO_PRE_EXECUTION_VIOLATION", "skills/implement/SKILL.md must reference scenario [PRE-02]"))
                if "Zero Primary Session Pre-Execution Invariant" not in content and "Zero Primary Session Pre-Execution" not in content:
                    errors.append(ValidationError(implement_skill, "ZERO_PRE_EXECUTION_VIOLATION", "skills/implement/SKILL.md must document 'Zero Primary Session Pre-Execution Invariant' in Step 1"))
                if "Empirical Gate Pre-Flight" in content:
                    errors.append(ValidationError(implement_skill, "ZERO_PRE_EXECUTION_VIOLATION", "skills/implement/SKILL.md must eliminate ambiguous 'Empirical Gate Pre-Flight' heading"))
                if not re.search(r"(?i)strictly (?:barred|prohibited) from (?:calling|invoking|executing) `?run_command`? with (?:terminal )?test runners", content):
                    errors.append(ValidationError(implement_skill, "ZERO_PRE_EXECUTION_VIOLATION", "skills/implement/SKILL.md must declare strict prohibition on primary session test runners"))
                if not ("at most two files" in content.lower() or "maximum of two files" in content.lower()):
                    errors.append(ValidationError(implement_skill, "ZERO_PRE_EXECUTION_VIOLATION", "skills/implement/SKILL.md must restrict pre-flight file reads to at most two files"))
                if not ("turn 1" in content.lower() and "turn 2" in content.lower()):
                    errors.append(ValidationError(implement_skill, "ZERO_PRE_EXECUTION_VIOLATION", "skills/implement/SKILL.md must mandate subagent dispatch on Turn 1 or Turn 2"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        orch_file = root_dir / "rules" / "subagent-orchestration.md"
        if not orch_file.is_file():
            errors.append(ValidationError(orch_file, "ZERO_PRE_EXECUTION_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            invariants_scanned += 1
            try:
                orch_content = self.read_rule_content(orch_file)
                if "[PRE-01]" not in orch_content or "[PRE-02]" not in orch_content:
                    errors.append(ValidationError(orch_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/subagent-orchestration.md must reference [PRE-01] and [PRE-02] under Sub-Trigger A2"))
                if "Zero Primary Session Pre-Execution" not in orch_content:
                    errors.append(ValidationError(orch_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/subagent-orchestration.md must document Zero Primary Session Pre-Execution under Sub-Trigger A2"))
                if not re.search(r"(?i)strictly (?:barred|prohibited) from (?:calling|invoking|executing) `?run_command`? with (?:terminal )?test runners", orch_content):
                    errors.append(ValidationError(orch_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/subagent-orchestration.md must declare prohibition on primary session test runners"))
                if "at most two files" not in orch_content.lower():
                    errors.append(ValidationError(orch_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/subagent-orchestration.md must restrict primary file reading to at most two files"))
                if not ("turn 1" in orch_content.lower() and "turn 2" in orch_content.lower()):
                    errors.append(ValidationError(orch_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/subagent-orchestration.md must mandate dispatch on Turn 1 or Turn 2"))
            except Exception as exc:
                errors.append(ValidationError(orch_file, "FILE_READ_ERROR", f"Could not read {orch_file.name}: {exc}"))

        op_file = root_dir / "rules" / "operational-invariants.md"
        if not op_file.is_file():
            errors.append(ValidationError(op_file, "ZERO_PRE_EXECUTION_VIOLATION", "Missing required rule 'rules/operational-invariants.md'"))
        else:
            invariants_scanned += 1
            try:
                op_content = op_file.read_text(encoding="utf-8")
                if "[PRE-01]" not in op_content or "[PRE-02]" not in op_content:
                    errors.append(ValidationError(op_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/operational-invariants.md must reference [PRE-01] and [PRE-02]"))
                if "Zero Primary Session Pre-Execution" not in op_content:
                    errors.append(ValidationError(op_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/operational-invariants.md must document Zero Primary Session Pre-Execution Invariant"))
                if not re.search(r"(?i)strictly (?:barred|prohibited) from calling test runners", op_content):
                    errors.append(ValidationError(op_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/operational-invariants.md must declare prohibition on primary session test runners or compilers"))
                if not ("at most two files" in op_content.lower() or "<= 2 files" in op_content.lower()):
                    errors.append(ValidationError(op_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/operational-invariants.md must reference at most two files restriction"))
                if not ("turn 1" in op_content.lower() and "turn 2" in op_content.lower()):
                    errors.append(ValidationError(op_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/operational-invariants.md must reference Turn 1 or Turn 2 dispatch"))
            except Exception as exc:
                errors.append(ValidationError(op_file, "FILE_READ_ERROR", f"Could not read {op_file.name}: {exc}"))

        agents_file = root_dir / "rules" / "AGENTS.md"
        if not agents_file.is_file():
            errors.append(ValidationError(agents_file, "ZERO_PRE_EXECUTION_VIOLATION", "Missing required rule 'rules/AGENTS.md'"))
        else:
            invariants_scanned += 1
            try:
                agents_content = agents_file.read_text(encoding="utf-8")
                if "Zero Primary Session Pre-Execution" not in agents_content:
                    errors.append(ValidationError(agents_file, "ZERO_PRE_EXECUTION_VIOLATION", "rules/AGENTS.md must summarize Zero Primary Session Pre-Execution Invariant"))
            except Exception as exc:
                errors.append(ValidationError(agents_file, "FILE_READ_ERROR", f"Could not read {agents_file.name}: {exc}"))

        return invariants_scanned, errors

    def validate_ticket_lifecycle_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Decentralized Ticket Lifecycle Invariants (Track 18 [LIFE-01], [LIFE-02]):
        1. skills/implement/SKILL.md:
           - Declares scenario tags [LIFE-01] and [LIFE-02]
           - Step 1: Documents Autonomous Ticket Claiming Protocol ([LIFE-01]), mutating on-disk status
             to 'in-progress' before dispatching background subagents.
           - Step 5: Documents Autonomous Ticket Resolution Protocol ([LIFE-02]), mutating on-disk status
             to 'resolved' after double-verification exits with status 0 and git commit is created,
             before rendering the completion artifact.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
            return 0, errors

        invariants_scanned += 1
        try:
            content = self._read_skill_with_references(implement_skill)
            if "[LIFE-01]" not in content:
                errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md must reference scenario [LIFE-01]"))
            if "[LIFE-02]" not in content:
                errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md must reference scenario [LIFE-02]"))

            step1_match = re.search(r"### Step 1:.*?(?=### Step 2:)", content, re.DOTALL)
            if not step1_match:
                errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md must contain '### Step 1:' section"))
            else:
                s1 = step1_match.group(0)
                if "[LIFE-01]" not in s1:
                    errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md Step 1 must reference scenario [LIFE-01]"))
                if not ("in-progress" in s1.lower() and ("claim" in s1.lower() or "claiming" in s1.lower())):
                    errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md must document claiming ticket to 'in-progress' in Step 1"))
                if not re.search(r"(?i)before\s+dispatching\s+background\s+subagents", s1):
                    errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md must specify claiming ticket on disk before dispatching background subagents"))

            step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
            if not step5_match:
                errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md must contain '### Step 5:' section"))
            else:
                s5 = step5_match.group(0)
                if "[LIFE-02]" not in s5:
                    errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md Step 5 must reference scenario [LIFE-02]"))
                if not ("resolved" in s5.lower() and ("resolve" in s5.lower() or "resolution" in s5.lower())):
                    errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md must document resolving ticket to 'resolved' in Step 5"))
                if not re.search(r"(?i)before\s+rendering\s+(?:the\s+)?completion\s+artifact", s5):
                    errors.append(ValidationError(implement_skill, "TICKET_LIFECYCLE_VIOLATION", "skills/implement/SKILL.md must specify resolving ticket on disk before rendering completion artifact"))
        except Exception as exc:
            errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        return invariants_scanned, errors

    def validate_single_ticket_termination_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Single-Ticket Termination Invariant and Anti-Frontier-Draining (Track 17 [TERM-01], [TERM-02]):
        1. skills/implement/SKILL.md:
           - Declares scenario tags [TERM-01] and [TERM-02]
           - Step 5: Documents Single-Ticket Termination Invariant ([TERM-01]), concluding turn immediately
             with Recommend-and-Stop upon ticket resolution and completion artifact rendering.
           - Step 5: Strictly bars DAG frontier recalculation or claiming downstream tickets in the same session ([TERM-01]).
           - Mentions Anti-Frontier-Draining and exclusivity of frontier draining for /implement-spec ([TERM-02]).
           - Common Rationalizations & Red Flags reference Single-Ticket Termination / Anti-Frontier-Draining.
        2. rules/subagent-orchestration.md:
           - Declares scenario tags [TERM-01] and [TERM-02]
           - Documents Single-Ticket Termination Invariant (Anti-Frontier-Draining) under Sub-Trigger A2.
           - Documents in Section 4 that autonomous frontier recalculation is exclusively reserved for /implement-spec (Sub-Trigger A1).
        3. rules/gemini-behavior-guardrails.md:
           - Declares scenario tags [TERM-01] and [TERM-02]
           - Section 4 (Phase Boundary & "Recommend-and-Stop" Enforcement) documents Single-Ticket Termination Invariant.
        4. rules/AGENTS.md:
           - Summarizes Single-Ticket Termination Invariant in Composed Standards Architecture.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = self._read_skill_with_references(implement_skill)
                if "[TERM-01]" not in content:
                    errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md must reference scenario [TERM-01]"))
                if "[TERM-02]" not in content:
                    errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md must reference scenario [TERM-02]"))
                if "Single-Ticket Termination Invariant" not in content:
                    errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md must document Single-Ticket Termination Invariant"))
                if "Anti-Frontier-Draining" not in content:
                    errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md must document Anti-Frontier-Draining"))

                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if "[TERM-01]" not in s5:
                        errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md Step 5 must reference scenario [TERM-01]"))
                    if "recommend-and-stop" not in s5.lower():
                        errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md Step 5 must document Recommend-and-Stop"))
                    if not re.search(r"(?i)(?:conclude[s]?\s+(?:its\s+)?turn\s+immediately|immediately\s+conclude[s]?\s+(?:its\s+)?turn)", s5):
                        errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md Step 5 must specify concluding turn immediately"))
                    if not re.search(r"(?i)(?:barred|prohibited|forbidden)\s+from\s+recalculating\s+(?:the\s+)?(?:DAG\s+)?frontier", s5):
                        errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md Step 5 must bar recalculating DAG frontier in the same session"))

                red_flags_match = re.search(r"## Red Flags.*?(?=\n## |\Z)", content, re.DOTALL)
                if not red_flags_match or not re.search(r"(?i)(?:recalculating\s+the\s+DAG\s+frontier|draining\s+(?:the\s+)?frontier|Single-Ticket Termination|Anti-Frontier-Draining)", red_flags_match.group(0)):
                    errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md Red Flags must reference Single-Ticket Termination / Anti-Frontier-Draining"))

                rat_match = re.search(r"## Common Rationalizations.*?(?=\n## |\Z)", content, re.DOTALL)
                if not rat_match or not re.search(r"(?i)(?:Single-Ticket Termination Invariant|Anti-Frontier-Draining)", rat_match.group(0)):
                    errors.append(ValidationError(implement_skill, "TERMINATION_INVARIANT_VIOLATION", "skills/implement/SKILL.md Common Rationalizations must reference Single-Ticket Termination / Anti-Frontier-Draining"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        subagent_rules = root_dir / "rules" / "subagent-orchestration.md"
        if not subagent_rules.is_file():
            errors.append(ValidationError(subagent_rules, "TERMINATION_INVARIANT_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            invariants_scanned += 1
            try:
                orch_content = subagent_rules.read_text(encoding="utf-8")
                if "[TERM-01]" not in orch_content:
                    errors.append(ValidationError(subagent_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/subagent-orchestration.md must reference scenario [TERM-01]"))
                if "[TERM-02]" not in orch_content:
                    errors.append(ValidationError(subagent_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/subagent-orchestration.md must reference scenario [TERM-02]"))
                if "Single-Ticket Termination Invariant" not in orch_content:
                    errors.append(ValidationError(subagent_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/subagent-orchestration.md must document Single-Ticket Termination Invariant"))
                if not re.search(r"(?i)(?:exclusively\s+reserved\s+for|exclusively\s+permitted\s+under)\s+`?/implement-spec`?", orch_content):
                    errors.append(ValidationError(subagent_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/subagent-orchestration.md must specify frontier draining is exclusively reserved for /implement-spec"))

                s4_match = re.search(r"## 4\.\s+Swarm Orchestration & Concurrency Lifecycle.*?(?=\n## |\Z)", orch_content, re.DOTALL)
                if not s4_match:
                    errors.append(ValidationError(subagent_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/subagent-orchestration.md must contain Section 4 'Swarm Orchestration & Concurrency Lifecycle'"))
                else:
                    s4 = s4_match.group(0)
                    if "[TERM-02]" not in s4:
                        errors.append(ValidationError(subagent_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/subagent-orchestration.md Section 4 must reference scenario [TERM-02]"))
                    if not re.search(r"(?i)(?:exclusively\s+reserved\s+for|exclusively\s+permitted\s+under)\s+`?/implement-spec`?", s4):
                        errors.append(ValidationError(subagent_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/subagent-orchestration.md Section 4 must specify frontier draining exclusivity for /implement-spec"))
            except Exception as exc:
                errors.append(ValidationError(subagent_rules, "FILE_READ_ERROR", f"Could not read {subagent_rules.name}: {exc}"))

        guard_rules = root_dir / "rules" / "gemini-behavior-guardrails.md"
        if not guard_rules.is_file():
            errors.append(ValidationError(guard_rules, "TERMINATION_INVARIANT_VIOLATION", "Missing required rule 'rules/gemini-behavior-guardrails.md'"))
        else:
            invariants_scanned += 1
            try:
                guard_content = guard_rules.read_text(encoding="utf-8")
                if "[TERM-01]" not in guard_content:
                    errors.append(ValidationError(guard_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/gemini-behavior-guardrails.md must reference scenario [TERM-01]"))
                if "[TERM-02]" not in guard_content:
                    errors.append(ValidationError(guard_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/gemini-behavior-guardrails.md must reference scenario [TERM-02]"))
                if "Single-Ticket Termination Invariant" not in guard_content:
                    errors.append(ValidationError(guard_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/gemini-behavior-guardrails.md must document Single-Ticket Termination Invariant"))

                s4_guard = re.search(r"## 4\.\s+Phase Boundary.*?Enforcement.*?(?=\n---|\n## |\Z)", guard_content, re.DOTALL)
                if not s4_guard:
                    errors.append(ValidationError(guard_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/gemini-behavior-guardrails.md must contain Section 4 'Phase Boundary & Recommend-and-Stop Enforcement'"))
                else:
                    s4g = s4_guard.group(0)
                    if "[TERM-01]" not in s4g:
                        errors.append(ValidationError(guard_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/gemini-behavior-guardrails.md Section 4 must reference scenario [TERM-01]"))
                    if "Single-Ticket Termination Invariant" not in s4g:
                        errors.append(ValidationError(guard_rules, "TERMINATION_INVARIANT_VIOLATION", "rules/gemini-behavior-guardrails.md Section 4 must document Single-Ticket Termination Invariant"))
            except Exception as exc:
                errors.append(ValidationError(guard_rules, "FILE_READ_ERROR", f"Could not read {guard_rules.name}: {exc}"))

        agents_file = root_dir / "rules" / "AGENTS.md"
        if not agents_file.is_file():
            errors.append(ValidationError(agents_file, "TERMINATION_INVARIANT_VIOLATION", "Missing required rule 'rules/AGENTS.md'"))
        else:
            invariants_scanned += 1
            try:
                agents_content = agents_file.read_text(encoding="utf-8")
                if "Single-Ticket Termination Invariant" not in agents_content:
                    errors.append(ValidationError(agents_file, "TERMINATION_INVARIANT_VIOLATION", "rules/AGENTS.md must summarize Single-Ticket Termination Invariant"))
            except Exception as exc:
                errors.append(ValidationError(agents_file, "FILE_READ_ERROR", f"Could not read {agents_file.name}: {exc}"))

        return invariants_scanned, errors

    def validate_command_center_frontier_filtering_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Command Center Dynamic Frontier Filtering (Track 18 [DASH-03]):
        1. skills/to-tickets/SKILL.md:
           - Declares scenario tag [DASH-03]
           - Step 5: Documents inspecting on-disk ticket files in .scratch/<feature-slug>/issues/ for Status:
           - Step 5: Documents displaying active tickets with '🟡 in-progress' (or 'in-progress') in progress table
           - Step 5: Documents strictly suppressing/excluding tickets with status 'in-progress' or 'resolved' from chat recommendations
           - Step 5: Documents outputting standby notification awaiting worker completion when all active frontier tickets are in-progress
        2. rules/operational-invariants.md:
           - Declares scenario tag [DASH-03] under Section 4
           - Codifies dynamic on-disk frontier filtering, status progress table, chat command suppression, and standby notification.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "FRONTIER_FILTERING_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = to_tickets_skill.read_text(encoding="utf-8")
                if "[DASH-03]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "FRONTIER_FILTERING_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [DASH-03]"))

                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(to_tickets_skill, "FRONTIER_FILTERING_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if "[DASH-03]" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "FRONTIER_FILTERING_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must reference scenario [DASH-03]"))
                    if not ("in-progress" in s5.lower() and ("table" in s5.lower() or "progress" in s5.lower())):
                        errors.append(ValidationError(to_tickets_skill, "FRONTIER_FILTERING_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must specify displaying in-progress in progress table"))
                    if not (("exclude" in s5.lower() or "suppress" in s5.lower() or "filter" in s5.lower()) and "resolved" in s5.lower()):
                        errors.append(ValidationError(to_tickets_skill, "FRONTIER_FILTERING_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must specify excluding in-progress and resolved tickets from chat execution commands"))
                    if "standby" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "FRONTIER_FILTERING_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must specify standby notification when all frontier tickets are in-progress"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        op_rules = root_dir / "rules" / "operational-invariants.md"
        if not op_rules.is_file():
            errors.append(ValidationError(op_rules, "FRONTIER_FILTERING_VIOLATION", "Missing required rule 'rules/operational-invariants.md'"))
        else:
            invariants_scanned += 1
            try:
                op_content = op_rules.read_text(encoding="utf-8")
                if "[DASH-03]" not in op_content:
                    errors.append(ValidationError(op_rules, "FRONTIER_FILTERING_VIOLATION", "rules/operational-invariants.md must reference scenario [DASH-03]"))

                s4_match = re.search(r"## 4\.\s+Ticket Ingestion.*?Ergonomics.*?(?=\n---|\n## |\Z)", op_content, re.DOTALL)
                if not s4_match:
                    errors.append(ValidationError(op_rules, "FRONTIER_FILTERING_VIOLATION", "rules/operational-invariants.md must contain Section 4 'Ticket Ingestion & Command Center Ergonomics'"))
                else:
                    s4 = s4_match.group(0)
                    if "[DASH-03]" not in s4:
                        errors.append(ValidationError(op_rules, "FRONTIER_FILTERING_VIOLATION", "rules/operational-invariants.md Section 4 must reference scenario [DASH-03]"))
                    if not ("in-progress" in s4.lower() and "standby" in s4.lower()):
                        errors.append(ValidationError(op_rules, "FRONTIER_FILTERING_VIOLATION", "rules/operational-invariants.md Section 4 must document in-progress filtering and standby notification"))
            except Exception as exc:
                errors.append(ValidationError(op_rules, "FILE_READ_ERROR", f"Could not read {op_rules.name}: {exc}"))

        return invariants_scanned, errors

    def validate_session_anchor_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        [ANCH-01] Validates Command Center Session Anchor Persistence invariants:
        1. skills/to-tickets/SKILL.md:
           - Declares scenario tag [ANCH-01]
           - Step 4 (Publish to Configured Tracker): Documents persisting .scratch/<feature-slug>/session.json
             containing commandCenterConversationId, feature, and createdAt (ISO-8601 UTC timestamp).
           - Mandates that individual ticket files remain clean domain specifications without session IDs.
           - Step 5: Documents session anchor resolution by worker sessions.
        2. docs/agents/issue-tracker.md:
           - Documents .scratch/<feature-slug>/session.json in directory structure conventions.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = to_tickets_skill.read_text(encoding="utf-8")
                if "[ANCH-01]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [ANCH-01]"))

                if "session.json" not in content:
                    errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_VIOLATION", "skills/to-tickets/SKILL.md must document 'session.json' persistence"))

                step4_match = re.search(r"### Step 4:.*?(?=\n### Step 5:|\Z)", content, re.DOTALL)
                if not step4_match:
                    errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 4:' section"))
                else:
                    s4 = step4_match.group(0)
                    if "session.json" not in s4:
                        errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_VIOLATION", "skills/to-tickets/SKILL.md Step 4 must mandate session.json persistence"))
                    if "createdat" not in s4.lower():
                        errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_VIOLATION", "skills/to-tickets/SKILL.md Step 4 must mandate createdAt in session.json"))

                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if "session.json" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document session.json resolution"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        issue_tracker_doc = root_dir / "docs" / "agents" / "issue-tracker.md"
        if issue_tracker_doc.is_file():
            invariants_scanned += 1
            try:
                it_content = issue_tracker_doc.read_text(encoding="utf-8")
                if "session.json" not in it_content:
                    errors.append(
                        ValidationError(
                            issue_tracker_doc,
                            "SESSION_ANCHOR_VIOLATION",
                            "docs/agents/issue-tracker.md must document session.json in directory conventions",
                        )
                    )
            except Exception as exc:
                errors.append(ValidationError(issue_tracker_doc, "FILE_READ_ERROR", f"Could not read {issue_tracker_doc.name}: {exc}"))

        return invariants_scanned, errors

    def validate_signal_dispatch_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        [PRUNE-01] Validates signal dispatch invariants:
        1. skills/implement/SKILL.md:
           - Step 5 documents session anchor resolution from session.json
           - Step 5 enforces Single-Ticket Termination Invariant (Recommend-and-Stop)
           - No longer mandates presence of agentapi_client.py, [SIG-01], [SIG-02], or commandCenterConversationId.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "SIGNAL_DISPATCH_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = self._read_skill_with_references(implement_skill)
                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(implement_skill, "SIGNAL_DISPATCH_VIOLATION", "skills/implement/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if "session.json" not in s5:
                        errors.append(ValidationError(implement_skill, "SIGNAL_DISPATCH_VIOLATION", "skills/implement/SKILL.md Step 5 must document session.json resolution"))
                    if "recommend-and-stop" not in s5.lower():
                        errors.append(ValidationError(implement_skill, "SIGNAL_DISPATCH_VIOLATION", "skills/implement/SKILL.md Step 5 must mandate Recommend-and-Stop termination"))

                    # [ZERO-01] Zero-IPC: Active verification of absence of ConnectRPC signal dispatch directives
                    if re.search(r"agentapi_client(?:\.py)?\s+send\b", s5, re.IGNORECASE) or re.search(r"agentapi_client(?:\.py)?\s+send\b", content, re.IGNORECASE):
                        errors.append(ValidationError(implement_skill, "SIGNAL_DISPATCH_VIOLATION", "skills/implement/SKILL.md must not contain 'agentapi_client.py send' dispatch directives"))
                    if "[sig-01]" in s5.lower() or "[sig-02]" in s5.lower():
                        errors.append(ValidationError(implement_skill, "SIGNAL_DISPATCH_VIOLATION", "skills/implement/SKILL.md must not reference legacy signal dispatch tags ([SIG-01] / [SIG-02])"))
                    if "commandcenterconversationid" in s5.lower():
                        errors.append(ValidationError(implement_skill, "SIGNAL_DISPATCH_VIOLATION", "skills/implement/SKILL.md Step 5 must not reference commandCenterConversationId"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        adr_doc = root_dir / "docs" / "adr" / "0021-autonomous-cross-conversation-signal-and-session-anchor.md"
        if adr_doc.is_file():
            invariants_scanned += 1

        return invariants_scanned, errors

    def validate_command_center_reaction_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        [REACT-01][REACT-02] Validates Command Center Autonomous Reaction and Task Dashboard Refresh invariants:
        1. skills/to-tickets/SKILL.md:
           - Declares scenario tags [REACT-01] and [REACT-02]
           - Step 5 documents receiving worker completion signals via injected USER_INPUT turns
           - Step 5 documents re-scanning .scratch/<feature-slug>/issues/ on disk
           - Step 5 documents updating Task Dashboard artifact (task-graph.md) in place
           - Step 5 documents marking completed tickets as resolved in progress table and updating Mermaid DAG
           - Step 5 documents recalculating active unblocked frontier tickets
           - Step 5 documents emitting 1-line execution commands (/implement <NN>) for all active frontier tickets simultaneously ([DASH-02])
           - Step 5 documents updating Task Dashboard to 100% complete and directing operator to /code-review or /pr when all tickets resolved
        2. rules/operational-invariants.md:
           - Section 4 documents [REACT-01] and [REACT-02]
           - Section 4 documents disk re-scanning, in-place dashboard refresh, frontier handoffs, and /code-review or /pr routing.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "REACTION_INVARIANT_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = to_tickets_skill.read_text(encoding="utf-8")
                if "[REACT-01]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "REACTION_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [REACT-01]"))
                if "[REACT-02]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "REACTION_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [REACT-02]"))

                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(to_tickets_skill, "REACTION_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if not re.search(r"(?:re-scan|rescan).*?disk", s5, re.IGNORECASE):
                        errors.append(ValidationError(to_tickets_skill, "REACTION_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document re-scanning disk on worker completion"))
                    if "task-graph.md" not in s5 and "task dashboard" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "REACTION_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document updating Task Dashboard artifact"))
                    if "100%" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "REACTION_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document 100% complete milestone on DAG resolution"))
                    if "/code-review" not in s5 and "/pr" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "REACTION_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must direct operator to /code-review or /pr upon all tickets resolved"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        op_rules = root_dir / "rules" / "operational-invariants.md"
        if op_rules.is_file():
            invariants_scanned += 1
            try:
                op_content = op_rules.read_text(encoding="utf-8")
                s4_match = re.search(r"## 4\..*?(?=\n## 5\.|\Z)", op_content, re.DOTALL)
                if s4_match:
                    s4 = s4_match.group(0)
                    if "[REACT-01]" not in s4 and "[REACT-01]" not in op_content:
                        errors.append(ValidationError(op_rules, "REACTION_INVARIANT_VIOLATION", "rules/operational-invariants.md Section 4 must reference scenario [REACT-01]"))
                    if "[REACT-02]" not in s4 and "[REACT-02]" not in op_content:
                        errors.append(ValidationError(op_rules, "REACTION_INVARIANT_VIOLATION", "rules/operational-invariants.md Section 4 must reference scenario [REACT-02]"))
                    if "/code-review" not in s4 and "/pr" not in s4:
                        errors.append(ValidationError(op_rules, "REACTION_INVARIANT_VIOLATION", "rules/operational-invariants.md Section 4 must document /code-review or /pr routing"))
            except Exception as exc:
                errors.append(ValidationError(op_rules, "FILE_READ_ERROR", f"Could not read {op_rules.name}: {exc}"))

        return invariants_scanned, errors

    def validate_session_anchor_state_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        [STATE-01][BRIEF-01] Validates Session Anchor State & Structured Downstream Review Brief invariants:
        1. templates/session-anchor.json:
           - Exists and parses as a valid JSON object.
           - Contains feature, commandCenterConversationId, createdAt, activePhase, fixLoopCount, and reviewedCommits.
        2. skills/to-tickets/SKILL.md:
           - Declares scenario tags [STATE-01] and [BRIEF-01].
           - Step 4 documents activePhase, fixLoopCount, and reviewedCommits in session.json.
           - Step 5 documents persisting activePhase: "code-review" upon all tickets resolved ([STATE-01]).
           - Step 5 documents generating Structured Session Brief for /code-review with spec references, delta spec,
             commit SHAs, seams, and ConnectRPC report-back instructions targeting $ANTIGRAVITY_CONVERSATION_ID ([BRIEF-01]).
           - Step 5 strictly prohibits bare 1-line commands without context pointers.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        template_file = root_dir / "templates" / "session-anchor.json"
        if not template_file.is_file():
            errors.append(ValidationError(template_file, "SESSION_ANCHOR_STATE_VIOLATION", "Missing required template 'templates/session-anchor.json'"))
        else:
            invariants_scanned += 1
            try:
                data = json.loads(template_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    errors.append(ValidationError(template_file, "SESSION_ANCHOR_STATE_VIOLATION", "templates/session-anchor.json must be a JSON object"))
                else:
                    for req_field in ("feature", "createdAt", "activePhase", "fixLoopCount", "reviewedCommits"):
                        if req_field not in data:
                            errors.append(ValidationError(template_file, "SESSION_ANCHOR_STATE_VIOLATION", f"templates/session-anchor.json missing required field '{req_field}'"))
                    if not isinstance(data.get("activePhase"), str) or not str(data.get("activePhase")).strip():
                        errors.append(ValidationError(template_file, "SESSION_ANCHOR_STATE_VIOLATION", "templates/session-anchor.json field 'activePhase' must be a non-empty string"))
                    if not isinstance(data.get("fixLoopCount"), int) or data.get("fixLoopCount") < 0:
                        errors.append(ValidationError(template_file, "SESSION_ANCHOR_STATE_VIOLATION", "templates/session-anchor.json field 'fixLoopCount' must be a non-negative integer"))
                    if not isinstance(data.get("reviewedCommits"), list):
                        errors.append(ValidationError(template_file, "SESSION_ANCHOR_STATE_VIOLATION", "templates/session-anchor.json field 'reviewedCommits' must be a list"))
            except Exception as exc:
                errors.append(ValidationError(template_file, "SESSION_ANCHOR_STATE_VIOLATION", f"Could not parse templates/session-anchor.json: {exc}"))

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_STATE_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = to_tickets_skill.read_text(encoding="utf-8")
                if "[STATE-01]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_STATE_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [STATE-01]"))
                if "[BRIEF-01]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_STATE_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [BRIEF-01]"))

                step4_match = re.search(r"### Step 4:.*?(?=\n### Step 5:|\Z)", content, re.DOTALL)
                if not step4_match:
                    errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_STATE_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 4:' section"))
                else:
                    s4 = step4_match.group(0)
                    for f in ("activePhase", "fixLoopCount", "reviewedCommits"):
                        if f.lower() not in s4.lower():
                            errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_STATE_VIOLATION", f"skills/to-tickets/SKILL.md Step 4 must document '{f}' in session.json"))

                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_STATE_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if "code-review" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_STATE_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document transition to code-review phase"))
                    if "session brief" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_STATE_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document Structured Session Brief generation"))
                    if "recommend-and-stop" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "SESSION_ANCHOR_STATE_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document Recommend-and-Stop directives in brief"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        return invariants_scanned, errors

    def validate_verdict_routing_state_machine_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        [ROUTE-01][BRIEF-02] Validates Phase Routing State Machine & Trigger H Security Audit Gate invariants:
        1. skills/to-tickets/SKILL.md:
           - Declares scenario tags [ROUTE-01] and [BRIEF-02].
           - Step 5 documents Phase Routing State Machine and Trigger H Attack Surface Evaluation.
           - Documents transition to 'security-audit' on Trigger H match ([ROUTE-01]).
           - Documents transition to 'pr' on non-attack-surface diffs ([ROUTE-01]).
           - Documents generating Structured Session Brief for /security-audit with audited commit range,
             attack surface file paths as seam, and terminal reporting directives enforcing local execution via ./scripts/report-phase.sh ([BRIEF-02]).
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "VERDICT_ROUTING_STATE_MACHINE_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = to_tickets_skill.read_text(encoding="utf-8")
                if "[BRIEF-02]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "VERDICT_ROUTING_STATE_MACHINE_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [BRIEF-02]"))

                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(to_tickets_skill, "VERDICT_ROUTING_STATE_MACHINE_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if "trigger h" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "VERDICT_ROUTING_STATE_MACHINE_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document Trigger H attack surface evaluation"))
                    if "security-audit" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "VERDICT_ROUTING_STATE_MACHINE_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document transition to security-audit"))
                    if "/pr" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "VERDICT_ROUTING_STATE_MACHINE_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document transition to /pr"))
                    if "recommend-and-stop" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "VERDICT_ROUTING_STATE_MACHINE_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document Recommend-and-Stop directives in brief"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        return invariants_scanned, errors

    def validate_fallback_and_fix_brief_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        [ROUTE-02][FIX-01] Validates Fallback Verdict Parser and Fix Brief Synthesis invariants:
        1. skills/to-tickets/SKILL.md:
           - Declares scenario tags [ROUTE-02] and [FIX-01].
           - Step 5 documents non-blocking regex fallback scanner for PASS/FAIL on malformed payloads.
           - Documents presenting raw report to operator for 1-click confirmation when unresolved without crashing ([ROUTE-02]).
           - Documents incrementing fixLoopCount in .scratch/<feature-slug>/session.json on failure ([FIX-01]).
           - Documents synthesizing Fix Brief with findings as Given/When/Then acceptance criteria, failing commit SHA, and target seam ([FIX-01]).
           - Documents 1-line execution command for a fresh worker session ([FIX-01]).
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "FALLBACK_FIX_BRIEF_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = to_tickets_skill.read_text(encoding="utf-8")
                if "[FIX-01]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "FALLBACK_FIX_BRIEF_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [FIX-01]"))

                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(to_tickets_skill, "FALLBACK_FIX_BRIEF_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if "fallback" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "FALLBACK_FIX_BRIEF_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document fallback verdict parsing"))
                    if "fix brief" not in s5.lower() and "fixloopcount" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "FALLBACK_FIX_BRIEF_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document Fix Brief synthesis or fixLoopCount"))
                    if "given" not in s5.lower() or "when" not in s5.lower() or "then" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "FALLBACK_FIX_BRIEF_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document formatting findings as Given/When/Then acceptance criteria"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        return invariants_scanned, errors

    def validate_circuit_breaker_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        [FIX-02] Validates Fix Loop Circuit Breaker Escalation and PR Phase Handoff invariants:
        1. skills/to-tickets/SKILL.md:
           - Declares scenario tag [FIX-02].
           - Step 5 documents Circuit Breaker Escalation protocol (trips when fixLoopCount > 2 / >= 3).
           - Step 5 documents halting automated fix loop dispatch and emitting operator alert:
             '> [!CAUTION] Circuit Breaker Tripped: Maximum Fix Loops Exceeded'.
           - Step 5 documents PR Phase Handoff & Structured Session Brief for /pr ([ROUTE-01]).
        2. Programmatic validation of evaluate_circuit_breaker and build_pr_brief.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 0

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            invariants_scanned += 1
            try:
                content = to_tickets_skill.read_text(encoding="utf-8")
                if "[FIX-02]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [FIX-02]"))

                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if "circuit breaker" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document Circuit Breaker Escalation"))
                    if "[!caution]" not in s5.lower() or "circuit breaker tripped" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document diagnostic alert '[!CAUTION] Circuit Breaker Tripped: Maximum Fix Loops Exceeded'"))
                    if "/pr" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document PR phase handoff"))
                    if "build_pr_brief" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must reference build_pr_brief"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        # [FIX-02] Verify evaluate_circuit_breaker threshold logic, boundary conditions, and build_pr_brief schema.
        invariants_scanned += 1
        try:
            tripped_low, _ = self.evaluate_circuit_breaker({"fixLoopCount": 1}, "verdict: FAIL", max_loops=2)
            if tripped_low:
                errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "evaluate_circuit_breaker tripped unexpectedly on count 1 <= 2"))

            tripped_boundary, _ = self.evaluate_circuit_breaker({"fixLoopCount": 2}, "verdict: FAIL", max_loops=2)
            if tripped_boundary:
                errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "evaluate_circuit_breaker tripped unexpectedly on boundary count 2 <= 2"))

            tripped_high, alert = self.evaluate_circuit_breaker({"fixLoopCount": 3}, "verdict: FAIL", max_loops=2)
            if not tripped_high:
                errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "evaluate_circuit_breaker failed to trip on count 3 > 2"))
            if "[!CAUTION]" not in alert or "Circuit Breaker Tripped" not in alert:
                errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", "evaluate_circuit_breaker alert missing '[!CAUTION] Circuit Breaker Tripped: Maximum Fix Loops Exceeded'"))

            pr_brief = self.build_pr_brief({"feature": "test", "reviewedCommits": ["c1"]})
            is_valid, brief_errors = self.validate_session_brief_content(pr_brief)
            if not is_valid:
                errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", f"build_pr_brief failed session brief validation: {brief_errors}"))
        except Exception as exc:
            errors.append(ValidationError(to_tickets_skill, "CIRCUIT_BREAKER_VIOLATION", f"Error evaluating circuit breaker helpers: {exc}"))

        return invariants_scanned, errors

    def validate_single_hop_builder_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 22/23 Single-Hop Builder & Adaptive Review Gate invariants:
        1. Single-Hop Subagent Builder delegation and context shielding ([ORCH-01])
        2. Double-Verification Terminal Gate targeted seam verification with exit code 0 ([GATE-01])
        3. Adaptive Review Gate Fast Path bypass for clean leaf tickets ([ADAPT-01])
        4. Adaptive Review Gate Escalation Path dispatch on attack surfaces and risk signals ([ADAPT-02])
        5. Dual-Mode Reviewer Invariant for Active Improvement Worker ([ADAPT-02], ADR 0025)
        6. Fail-Safe Escalation Degradation on timeout or quota exhaustion ([ESC-01], ADR 0025)
        7. Living Specification Reconciliation & Additive Scenario integrity ([RECON-01])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 7

        root = Path(root_dir)
        implement_skill = root / "skills" / "implement" / "SKILL.md"
        orch_rule = root / "rules" / "subagent-orchestration.md"
        living_spec = root / "docs" / "specs" / "implement" / "spec.md"

        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "SINGLE_HOP_BUILDER_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
            return invariants_scanned, errors
        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "ADAPTIVE_REVIEW_ESCALATION_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
            return invariants_scanned, errors
        if not living_spec.is_file():
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "Missing required living spec 'docs/specs/implement/spec.md'"))
            return invariants_scanned, errors

        try:
            imp_content = self._read_skill_with_references(implement_skill)
        except Exception as exc:
            errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))
            return invariants_scanned, errors

        try:
            orch_content = self.read_rule_content(orch_rule)
        except Exception as exc:
            errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))
            return invariants_scanned, errors

        try:
            spec_content = living_spec.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(living_spec, "FILE_READ_ERROR", f"Could not read {living_spec.name}: {exc}"))
            return invariants_scanned, errors

        # Invariant 1: [ORCH-01] Single-Hop Subagent Builder delegation and context shielding
        if "[ORCH-01]" not in imp_content:
            errors.append(ValidationError(implement_skill, "SINGLE_HOP_BUILDER_VIOLATION", "skills/implement/SKILL.md must reference scenario [ORCH-01]"))
        if "Pipeline Coordinator" not in imp_content:
            errors.append(ValidationError(implement_skill, "SINGLE_HOP_BUILDER_VIOLATION", "skills/implement/SKILL.md must specify Pipeline Coordinator persona"))
        if "spec-implementer" not in imp_content:
            errors.append(ValidationError(implement_skill, "SINGLE_HOP_BUILDER_VIOLATION", "skills/implement/SKILL.md must dispatch single spec-implementer subagent"))
        if 'Workspace: "inherit"' not in imp_content and "Workspace: 'inherit'" not in imp_content:
            errors.append(ValidationError(implement_skill, "SINGLE_HOP_BUILDER_VIOLATION", "skills/implement/SKILL.md must specify Workspace: 'inherit' for builder"))
        if "auto-compaction" not in imp_content.lower():
            errors.append(ValidationError(implement_skill, "SINGLE_HOP_BUILDER_VIOLATION", "skills/implement/SKILL.md must reference preventing Antigravity auto-compaction"))

        # Invariant 2: [GATE-01] Targeted Seam Double-Verification Gate with exit code 0
        if "[GATE-01]" not in imp_content:
            errors.append(ValidationError(implement_skill, "TARGETED_SEAM_GATE_VIOLATION", "skills/implement/SKILL.md must reference scenario [GATE-01]"))
        if "tsc --noEmit && npm test -- <targeted-test-file>" not in imp_content and "tsc --noEmit" not in imp_content:
            errors.append(ValidationError(implement_skill, "TARGETED_SEAM_GATE_VIOLATION", "skills/implement/SKILL.md must document targeted seam verification command"))
        if not re.search(r"(?i)exit(?: code| status) 0 before (?:updating ticket status to resolved|resolving the ticket)", imp_content):
            errors.append(ValidationError(implement_skill, "TARGETED_SEAM_GATE_VIOLATION", "skills/implement/SKILL.md must mandate exit code 0 before resolving ticket"))
        if "pull request creation" not in imp_content:
            errors.append(ValidationError(implement_skill, "TARGETED_SEAM_GATE_VIOLATION", "skills/implement/SKILL.md must defer full repository regression suites to pull request creation"))

        # Invariant 3: [ADAPT-01] Adaptive Review Gate Fast Path bypass
        if "[ADAPT-01]" not in imp_content:
            errors.append(ValidationError(implement_skill, "ADAPTIVE_REVIEW_FAST_PATH_VIOLATION", "skills/implement/SKILL.md must reference scenario [ADAPT-01]"))
        if "[ADAPT-01]" not in orch_content:
            errors.append(ValidationError(orch_rule, "ADAPTIVE_REVIEW_FAST_PATH_VIOLATION", "rules/subagent-orchestration.md must reference scenario [ADAPT-01]"))
        if "Fast Path" not in imp_content or "Fast Path" not in orch_content:
            errors.append(ValidationError(implement_skill, "ADAPTIVE_REVIEW_FAST_PATH_VIOLATION", "skills/implement/SKILL.md and rules/subagent-orchestration.md must document Fast Path"))
        if not re.search(r"(?i)100%\s+Deep\s+Verification", imp_content):
            errors.append(ValidationError(implement_skill, "ADAPTIVE_REVIEW_FAST_PATH_VIOLATION", "skills/implement/SKILL.md must require 100% Deep Verification for Fast Path"))
        if not re.search(r"(?i)(?:at most 2|<= 2|\b2\b)\s+(?:test iterations|retry cycles|iterations)", imp_content):
            errors.append(ValidationError(implement_skill, "ADAPTIVE_REVIEW_FAST_PATH_VIOLATION", "skills/implement/SKILL.md must specify at most 2 retry cycles for Fast Path"))

        # Invariant 4: [ADAPT-02] Adaptive Review Gate Escalation Path dispatch
        if "[ADAPT-02]" not in imp_content:
            errors.append(ValidationError(implement_skill, "ADAPTIVE_REVIEW_ESCALATION_VIOLATION", "skills/implement/SKILL.md must reference scenario [ADAPT-02]"))
        if "[ADAPT-02]" not in orch_content:
            errors.append(ValidationError(orch_rule, "ADAPTIVE_REVIEW_ESCALATION_VIOLATION", "rules/subagent-orchestration.md must reference scenario [ADAPT-02]"))
        if "Escalation Path" not in imp_content or "Escalation Path" not in orch_content:
            errors.append(ValidationError(implement_skill, "ADAPTIVE_REVIEW_ESCALATION_VIOLATION", "skills/implement/SKILL.md and rules/subagent-orchestration.md must document Escalation Path"))
        for surface in ["**/api/**", "**/routes/**", "'use server'", "auth.ts"]:
            if surface not in imp_content or surface not in orch_content:
                errors.append(ValidationError(implement_skill, "ADAPTIVE_REVIEW_ESCALATION_VIOLATION", f"Must document attack surface '{surface}' in implement/SKILL.md and subagent-orchestration.md"))
        if not re.search(r"(?i)(?:3 or more|>= 3)\s+retry cycles", imp_content):
            errors.append(ValidationError(implement_skill, "ADAPTIVE_REVIEW_ESCALATION_VIOLATION", "skills/implement/SKILL.md must specify 3 or more retry cycles as escalation trigger"))
        if not re.search(r"(?i)dispatches\s+Stage\s+2\s+`?code-reviewer`?", imp_content):
            errors.append(ValidationError(implement_skill, "ADAPTIVE_REVIEW_ESCALATION_VIOLATION", "skills/implement/SKILL.md must specify dispatching Stage 2 code-reviewer upon escalation"))

        # Invariant 5: [ADAPT-02] Dual-Mode Reviewer Invariant (ADR 0025)
        if 'TypeName: "self"' not in imp_content and "TypeName: 'self'" not in imp_content:
            errors.append(ValidationError(implement_skill, "DUAL_MODE_REVIEWER_VIOLATION", "skills/implement/SKILL.md must specify Stage 2 dispatch with TypeName: 'self'"))
        if "Active Improvement Worker" not in imp_content:
            errors.append(ValidationError(implement_skill, "DUAL_MODE_REVIEWER_VIOLATION", "skills/implement/SKILL.md must designate Stage 2 as Active Improvement Worker"))
        if 'TypeName: "self"' not in orch_content and "TypeName: 'self'" not in orch_content:
            errors.append(ValidationError(orch_rule, "DUAL_MODE_REVIEWER_VIOLATION", "rules/subagent-orchestration.md must specify Stage 2 dispatch with TypeName: 'self'"))
        if "Active Improvement Worker" not in orch_content:
            errors.append(ValidationError(orch_rule, "DUAL_MODE_REVIEWER_VIOLATION", "rules/subagent-orchestration.md must designate Stage 2 as Active Improvement Worker"))

        # Invariant 6: [ESC-01] Fail-Safe Escalation Degradation (ADR 0025)
        if "[ESC-01]" not in imp_content:
            errors.append(ValidationError(implement_skill, "FAIL_SAFE_DEGRADATION_VIOLATION", "skills/implement/SKILL.md must reference scenario [ESC-01]"))
        if "ESCALATION_BYPASSED_TIMEOUT" not in imp_content:
            errors.append(ValidationError(implement_skill, "FAIL_SAFE_DEGRADATION_VIOLATION", "skills/implement/SKILL.md must document ESCALATION_BYPASSED_TIMEOUT status marker"))
        if "[ESC-01]" not in orch_content:
            errors.append(ValidationError(orch_rule, "FAIL_SAFE_DEGRADATION_VIOLATION", "rules/subagent-orchestration.md must reference scenario [ESC-01]"))
        if "ESCALATION_BYPASSED_TIMEOUT" not in orch_content:
            errors.append(ValidationError(orch_rule, "FAIL_SAFE_DEGRADATION_VIOLATION", "rules/subagent-orchestration.md must document ESCALATION_BYPASSED_TIMEOUT status marker"))

        # Invariant 7: [RECON-01] Living Spec Reconciliation & Additive Scenario integrity
        if "### Requirement: Adaptive Review Gate" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing reconciled requirement '### Requirement: Adaptive Review Gate'"))
        if "[ADAPT-01]" not in spec_content or "[ADAPT-02]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing reconciled scenarios [ADAPT-01] or [ADAPT-02]"))
        if 'TypeName: "self"' not in spec_content and "TypeName: 'self'" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md [ADAPT-02] missing TypeName: 'self'"))
        if "### Requirement: Fail-Safe Escalation Degradation" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing reconciled requirement '### Requirement: Fail-Safe Escalation Degradation'"))
        if "[ESC-01]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing reconciled scenario [ESC-01]"))
        if "ESCALATION_BYPASSED_TIMEOUT" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing ESCALATION_BYPASSED_TIMEOUT in [ESC-01]"))
        if "[ORCH-01]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing modified scenario [ORCH-01]"))
        if "[ORCH-02]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing preserved baseline scenario [ORCH-02]"))
        if "[GATE-01]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing modified scenario [GATE-01]"))

        return invariants_scanned, errors

    def validate_worker_report_resilience_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 26 Worker Report Resilience & Resilient Signaling invariants (ADR 0027):
        1. Commit-based ticket resolution idempotency decoupled from frontmatter status ([IDEMP-01])
        2. Stage-Gated Single-Dispatch Invariant & Subagent Brief Guardrails in implement/SKILL.md ([GATE-01])
        3. Pipeline Coordinator Exclusivity & continuation mandate in rules/subagent-orchestration.md ([GATE-01])
        4. 3-tier Structural Discovery Cascade with brain artifact & SQLite FTS fallbacks ([DISC-01], [DISC-02])
        5. Living Specification Reconciliation & Additive Scenario integrity ([RECON-01])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 5

        root = Path(root_dir)
        resolve_script = root / "scripts" / "internal" / "resolve_ticket.py"
        verify_script = root / "scripts" / "internal" / "verify_integrity.py"
        implement_skill = root / "skills" / "implement" / "SKILL.md"
        orch_rule = root / "rules" / "subagent-orchestration.md"
        living_spec = root / "docs" / "specs" / "implement" / "spec.md"

        if not resolve_script.is_file():
            errors.append(ValidationError(resolve_script, "COMMIT_IDEMPOTENCY_VIOLATION", "Missing required script 'scripts/internal/resolve_ticket.py'"))
            return invariants_scanned, errors
        if not verify_script.is_file():
            errors.append(ValidationError(verify_script, "STRUCTURAL_DISCOVERY_VIOLATION", "Missing required script 'scripts/internal/verify_integrity.py'"))
            return invariants_scanned, errors
        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "COORDINATOR_EXCLUSIVITY_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
            return invariants_scanned, errors
        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "COORDINATOR_EXCLUSIVITY_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
            return invariants_scanned, errors
        if not living_spec.is_file():
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "Missing required living spec 'docs/specs/implement/spec.md'"))
            return invariants_scanned, errors

        try:
            resolve_content = resolve_script.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(resolve_script, "FILE_READ_ERROR", f"Could not read {resolve_script.name}: {exc}"))
            return invariants_scanned, errors

        try:
            verify_content = verify_script.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(verify_script, "FILE_READ_ERROR", f"Could not read {verify_script.name}: {exc}"))
            return invariants_scanned, errors

        try:
            imp_content = self._read_skill_with_references(implement_skill)
        except Exception as exc:
            errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))
            return invariants_scanned, errors

        try:
            orch_content = self.read_rule_content(orch_rule)
        except Exception as exc:
            errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))
            return invariants_scanned, errors

        try:
            spec_content = living_spec.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(living_spec, "FILE_READ_ERROR", f"Could not read {living_spec.name}: {exc}"))
            return invariants_scanned, errors

        # Invariant 1: [IDEMP-01] Commit-based ticket resolution idempotency (ADR 0027)
        if "reviewedCommits" not in resolve_content:
            errors.append(ValidationError(resolve_script, "COMMIT_IDEMPOTENCY_VIOLATION", "scripts/internal/resolve_ticket.py must track reviewedCommits in session.json for commit-based idempotency"))
        if "[IDEMP-01]" not in resolve_content:
            errors.append(ValidationError(resolve_script, "COMMIT_IDEMPOTENCY_VIOLATION", "scripts/internal/resolve_ticket.py must reference scenario [IDEMP-01]"))
        if not re.search(r"(?i)(?:idempotent.*?(?:skip|already recorded|bypass)|(?:skip|already recorded).*?idempotent)", resolve_content):
            errors.append(ValidationError(resolve_script, "COMMIT_IDEMPOTENCY_VIOLATION", "scripts/internal/resolve_ticket.py must implement idempotent skip when commit SHA is already recorded"))

        # Invariant 2: [GATE-01] Pipeline Coordinator Exclusivity & Subagent Brief Guardrails in implement/SKILL.md
        if "[GATE-01]" not in imp_content:
            errors.append(ValidationError(implement_skill, "COORDINATOR_EXCLUSIVITY_VIOLATION", "skills/implement/SKILL.md must reference scenario [GATE-01]"))
        if "Pipeline Coordinator Exclusivity" not in imp_content:
            errors.append(ValidationError(implement_skill, "COORDINATOR_EXCLUSIVITY_VIOLATION", "skills/implement/SKILL.md must specify Pipeline Coordinator Exclusivity"))
        if not re.search(r"(?is)strictly (?:barred|prohibited) from concluding its turn upon receiving [`]?send_message", imp_content):
            errors.append(ValidationError(implement_skill, "COORDINATOR_EXCLUSIVITY_VIOLATION", "skills/implement/SKILL.md must mandate coordinator turn continuation upon receiving send_message"))
        if not re.search(r"(?is)(?:strictly (?:barred|prohibited)|NEVER).*?(?:edit|mutat|updat).*?ticket.*?\.scratch/", imp_content):
            errors.append(ValidationError(implement_skill, "COORDINATOR_EXCLUSIVITY_VIOLATION", "skills/implement/SKILL.md must forbid subagents from mutating .scratch/ ticket files"))

        # Invariant 3: [GATE-01] Pipeline Coordinator Exclusivity in rules/subagent-orchestration.md
        if "Pipeline Coordinator Exclusivity" not in orch_content:
            errors.append(ValidationError(orch_rule, "COORDINATOR_EXCLUSIVITY_VIOLATION", "rules/subagent-orchestration.md must specify Pipeline Coordinator Exclusivity"))
        if not re.search(r"(?is)strictly (?:barred|prohibited) from concluding its turn upon receiving [`]?send_message", orch_content):
            errors.append(ValidationError(orch_rule, "COORDINATOR_EXCLUSIVITY_VIOLATION", "rules/subagent-orchestration.md must bar coordinator from concluding turn upon receiving send_message"))
        if not re.search(r"(?is)(?:strictly barred from mutating ticket files|report stage completion without ticket-level resolution status)", orch_content):
            errors.append(ValidationError(orch_rule, "COORDINATOR_EXCLUSIVITY_VIOLATION", "rules/subagent-orchestration.md must require subagents to report stage completion without ticket resolution status"))

        # Invariant 4: [DISC-01], [DISC-02] 3-tier Structural Discovery Cascade (ADR 0027)
        if "def discover_command_center" not in verify_content:
            errors.append(ValidationError(verify_script, "STRUCTURAL_DISCOVERY_VIOLATION", "scripts/internal/verify_integrity.py must implement discover_command_center on ManifestValidator"))
        if "task-graph.md" not in verify_content or "brain" not in verify_content:
            errors.append(ValidationError(verify_script, "STRUCTURAL_DISCOVERY_VIOLATION", "scripts/internal/verify_integrity.py must implement Tier 1 brain artifact fingerprint discovery"))
        if "conversations_fts" not in verify_content:
            errors.append(ValidationError(verify_script, "STRUCTURAL_DISCOVERY_VIOLATION", "scripts/internal/verify_integrity.py must implement Tier 2 SQLite FTS command history discovery"))
        if "discover_command_center" not in resolve_content:
            errors.append(ValidationError(resolve_script, "STRUCTURAL_DISCOVERY_VIOLATION", "scripts/internal/resolve_ticket.py must invoke discover_command_center on HTTP 404"))
        if not re.search(r"(?i)manual (?:report|banner)", resolve_content):
            errors.append(ValidationError(resolve_script, "STRUCTURAL_DISCOVERY_VIOLATION", "scripts/internal/resolve_ticket.py must print conspicuous manual banner fallback"))

        # Invariant 5: [RECON-01] Living Spec Reconciliation & Additive Scenario integrity
        if "### Requirement: Stage-Gated Single-Dispatch Invariant" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing reconciled requirement '### Requirement: Stage-Gated Single-Dispatch Invariant'"))
        if "[GATE-01]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing modified scenario [GATE-01]"))
        if "[IDEMP-01]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing modified scenario [IDEMP-01]"))
        if "reviewedCommits" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md scenario [IDEMP-01] must reference reviewedCommits"))
        if "[ORCH-01]" not in spec_content or "[ORCH-02]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing preserved baseline scenarios [ORCH-01] or [ORCH-02]"))
        if "[ADAPT-01]" not in spec_content or "[ADAPT-02]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing preserved baseline scenarios [ADAPT-01] or [ADAPT-02]"))
        if "[ESC-01]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing preserved baseline scenario [ESC-01]"))

        return invariants_scanned, errors

    def validate_dual_domain_security_gate_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 27 Dual-Domain Security Gate & Routing Transparency invariants (ADR 0028):
        1. Dual-Domain Attack Surface Matrix in skills/to-tickets/SKILL.md ([ROUTE-01], [BRIEF-02])
        2. Security Decision Audit Trail single-line log in skills/to-tickets/SKILL.md ([ROUTE-03])
        3. Explicit Manual Audit Override in skills/to-tickets/SKILL.md and rules/subagent-orchestration.md ([ROUTE-04])
        4. Dual-Domain Attack Surface Matrix & Audit Trail in rules/subagent-orchestration.md Trigger H ([ROUTE-01], [ROUTE-03], [ROUTE-04])
        5. Living Specification Reconciliation & Additive Scenario integrity ([RECON-01])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 5

        root = Path(root_dir)
        to_tickets_skill = root / "skills" / "to-tickets" / "SKILL.md"
        orch_rule = root / "rules" / "subagent-orchestration.md"
        verify_script = root / "scripts" / "internal" / "verify_integrity.py"
        living_spec = root / "docs" / "specs" / "implement" / "spec.md"

        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "DUAL_DOMAIN_ATTACK_SURFACE_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
            return invariants_scanned, errors
        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "ORCHESTRATION_TRIGGER_H_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
            return invariants_scanned, errors
        if not verify_script.is_file():
            errors.append(ValidationError(verify_script, "DUAL_DOMAIN_VERIFIER_VIOLATION", "Missing required script 'scripts/internal/verify_integrity.py'"))
            return invariants_scanned, errors
        if not living_spec.is_file():
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "Missing required living spec 'docs/specs/implement/spec.md'"))
            return invariants_scanned, errors

        try:
            to_tickets_content = to_tickets_skill.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))
            return invariants_scanned, errors

        try:
            orch_content = self.read_rule_content(orch_rule)
        except Exception as exc:
            errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))
            return invariants_scanned, errors

        try:
            verify_content = verify_script.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(verify_script, "FILE_READ_ERROR", f"Could not read {verify_script.name}: {exc}"))
            return invariants_scanned, errors

        try:
            spec_content = living_spec.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(ValidationError(living_spec, "FILE_READ_ERROR", f"Could not read {living_spec.name}: {exc}"))
            return invariants_scanned, errors

        # Invariant 1: [ROUTE-01], [BRIEF-02] Dual-Domain Attack Surface Matrix in skills/to-tickets/SKILL.md
        if "Dual-Domain" not in to_tickets_content:
            errors.append(ValidationError(to_tickets_skill, "DUAL_DOMAIN_ATTACK_SURFACE_VIOLATION", "skills/to-tickets/SKILL.md must document Dual-Domain Attack Surface Matrix"))
        if "Domain A" not in to_tickets_content or "Domain B" not in to_tickets_content:
            errors.append(ValidationError(to_tickets_skill, "DUAL_DOMAIN_ATTACK_SURFACE_VIOLATION", "skills/to-tickets/SKILL.md must specify Domain A and Domain B in attack surface evaluation"))
        if "[BRIEF-02]" not in to_tickets_content:
            errors.append(ValidationError(to_tickets_skill, "DUAL_DOMAIN_ATTACK_SURFACE_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [BRIEF-02]"))

        # Invariant 2: [ROUTE-03] Security Decision Audit Trail in skills/to-tickets/SKILL.md
        if "[ROUTE-03]" not in to_tickets_content:
            errors.append(ValidationError(to_tickets_skill, "SECURITY_AUDIT_TRAIL_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [ROUTE-03]"))
        if "Security Decision Audit Trail" not in to_tickets_content:
            errors.append(ValidationError(to_tickets_skill, "SECURITY_AUDIT_TRAIL_VIOLATION", "skills/to-tickets/SKILL.md must document Security Decision Audit Trail"))
        if "🛡️ Security Gate:" not in to_tickets_content:
            errors.append(ValidationError(to_tickets_skill, "SECURITY_AUDIT_TRAIL_VIOLATION", "skills/to-tickets/SKILL.md must specify single-line blockquote log with '🛡️ Security Gate:'"))

        # Invariant 3: [ROUTE-04] Explicit Manual Override in skills/to-tickets/SKILL.md
        if "[ROUTE-04]" not in to_tickets_content:
            errors.append(ValidationError(to_tickets_skill, "MANUAL_OVERRIDE_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [ROUTE-04]"))
        if "audit_required" not in to_tickets_content:
            errors.append(ValidationError(to_tickets_skill, "MANUAL_OVERRIDE_VIOLATION", "skills/to-tickets/SKILL.md must document audit_required payload override"))
        if "--force-audit" not in to_tickets_content and "--audit" not in to_tickets_content:
            errors.append(ValidationError(to_tickets_skill, "MANUAL_OVERRIDE_VIOLATION", "skills/to-tickets/SKILL.md must document --audit / --force-audit CLI flag override"))

        # Invariant 4: Trigger H Dual-Domain Matrix, Audit Trail & Manual Override in rules/subagent-orchestration.md
        if "Trigger H" not in orch_content:
            errors.append(ValidationError(orch_rule, "ORCHESTRATION_TRIGGER_H_VIOLATION", "rules/subagent-orchestration.md must declare Trigger H"))
        if "Dual-Domain Attack Surface Matrix" not in orch_content:
            errors.append(ValidationError(orch_rule, "ORCHESTRATION_TRIGGER_H_VIOLATION", "rules/subagent-orchestration.md must document Dual-Domain Attack Surface Matrix in Trigger H"))
        if "Domain A" not in orch_content or "Domain B" not in orch_content:
            errors.append(ValidationError(orch_rule, "ORCHESTRATION_TRIGGER_H_VIOLATION", "rules/subagent-orchestration.md must document Domain A and Domain B in Trigger H"))
        if "[ROUTE-03]" not in orch_content or "Security Decision Audit Trail" not in orch_content:
            errors.append(ValidationError(orch_rule, "ORCHESTRATION_TRIGGER_H_VIOLATION", "rules/subagent-orchestration.md must document Security Decision Audit Trail [ROUTE-03]"))
        if "[ROUTE-04]" not in orch_content or "audit_required" not in orch_content:
            errors.append(ValidationError(orch_rule, "ORCHESTRATION_TRIGGER_H_VIOLATION", "rules/subagent-orchestration.md must document Explicit Manual Override [ROUTE-04]"))

        # Invariant 5: [RECON-01] Living Spec Reconciliation & Additive Scenario integrity
        if "[BRIEF-02]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing modified scenario [BRIEF-02]"))
        if "### Requirement: Security Decision Audit Trail and Transparency" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing requirement '### Requirement: Security Decision Audit Trail and Transparency'"))
        if "[ROUTE-03]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing added scenario [ROUTE-03]"))
        if "Dual-Domain Attack Surface Matrix" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md scenario [ROUTE-03] must reference Dual-Domain Attack Surface Matrix"))
        if "### Requirement: Explicit Manual Audit Override" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing requirement '### Requirement: Explicit Manual Audit Override'"))
        if "[ROUTE-04]" not in spec_content:
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "docs/specs/implement/spec.md missing added scenario [ROUTE-04]"))

        for baseline_tag in ["[BRIEF-01]", "[FIX-01]", "[FIX-02]", "[STATE-01]", "[RES-01]", "[RES-02]", "[RUN-01]", "[ORCH-01]", "[ORCH-02]", "[GATE-01]", "[ADAPT-01]", "[ADAPT-02]", "[ESC-01]"]:
            if baseline_tag not in spec_content:
                errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", f"docs/specs/implement/spec.md missing preserved baseline scenario {baseline_tag}"))

        # Invariant 6: [DUDO-01] Dual-Domain Threat Scoping in skills/security-audit/SKILL.md
        sec_audit_skill = root / "skills" / "security-audit" / "SKILL.md"
        if sec_audit_skill.is_file():
            invariants_scanned += 1
            try:
                sa_content = sec_audit_skill.read_text(encoding="utf-8")
                if "[DUDO-01]" not in sa_content:
                    errors.append(ValidationError(sec_audit_skill, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [DUDO-01]"))
                if "Domain B" not in sa_content or "Domain A" not in sa_content:
                    errors.append(ValidationError(sec_audit_skill, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "skills/security-audit/SKILL.md must document Domain A and Domain B attack surfaces"))
                if "subprocess" not in sa_content.lower():
                    errors.append(ValidationError(sec_audit_skill, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "skills/security-audit/SKILL.md must document subprocess invocations in Domain B"))
                if "sqlite" not in sa_content.lower():
                    errors.append(ValidationError(sec_audit_skill, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "skills/security-audit/SKILL.md must document SQLite query logic in Domain B"))
                if "temp" not in sa_content.lower():
                    errors.append(ValidationError(sec_audit_skill, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "skills/security-audit/SKILL.md must document temporary file operations in Domain B"))
                if "not skipped" not in sa_content.lower() and "not silently bypassed" not in sa_content.lower() and "not be skipped" not in sa_content.lower():
                    errors.append(ValidationError(sec_audit_skill, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "skills/security-audit/SKILL.md must mandate that repositories without web endpoints are not skipped when Domain B surfaces are modified"))
            except Exception as exc:
                errors.append(ValidationError(sec_audit_skill, "FILE_READ_ERROR", f"Could not read {sec_audit_skill.name}: {exc}"))
        elif (root / "skills" / "security-audit").is_dir():
            errors.append(ValidationError(sec_audit_skill, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "Missing required skill 'skills/security-audit/SKILL.md'"))

        # Invariant 7: [DUDO-01] Domain B Threat Vectors in agents/security-auditor.md
        sec_auditor_agent = root / "agents" / "security-auditor.md"
        if sec_auditor_agent.is_file():
            invariants_scanned += 1
            try:
                agent_content = sec_auditor_agent.read_text(encoding="utf-8")
                if "Domain B" not in agent_content:
                    errors.append(ValidationError(sec_auditor_agent, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "agents/security-auditor.md must document Domain B Threat Vectors"))
                if "subprocess" not in agent_content.lower():
                    errors.append(ValidationError(sec_auditor_agent, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "agents/security-auditor.md must document subprocess injection threat vector"))
                if "sqlite" not in agent_content.lower():
                    errors.append(ValidationError(sec_auditor_agent, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "agents/security-auditor.md must document SQLite query injection threat vector"))
                if "temp" not in agent_content.lower():
                    errors.append(ValidationError(sec_auditor_agent, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "agents/security-auditor.md must document insecure temporary file threat vector"))
            except Exception as exc:
                errors.append(ValidationError(sec_auditor_agent, "FILE_READ_ERROR", f"Could not read {sec_auditor_agent.name}: {exc}"))
        elif (root / "agents").is_dir():
            errors.append(ValidationError(sec_auditor_agent, "DUAL_DOMAIN_THREAT_SCOPING_VIOLATION", "Missing required agent 'agents/security-auditor.md'"))

        return invariants_scanned, errors

    def validate_execution_state_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 28 Execution State Machine, Resolution Stage-Gating & Phase Reporting Airgap invariants ([EVAL-01]):
        1. scripts/report-phase.sh presence, executable permissions (0755), and shebang ([REPT-01])
        2. scripts/internal/execution_state.py presence and schema definitions ([STATE-01], [STATE-02], [RUN-02])
        3. scripts/internal/adaptive_gate.py state-aware Stop gate disarm invariants ([DISARM-01])
        4. scripts/internal/resolve_ticket.py resolution stage gating & state completion invariants ([RUN-02])
        5. Strict PR Airgap in skills/security-audit/SKILL.md and rules/gemini-behavior-guardrails.md ([AIRGAP-01])
        6. Living specification reconciliation & 7 delta scenarios in docs/specs/implement/spec.md ([EVAL-01])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 6

        root = Path(root_dir)
        report_phase_sh = root / "scripts" / "report-phase.sh"
        report_phase_py = root / "scripts" / "internal" / "report_phase.py"
        execution_state_py = root / "scripts" / "internal" / "execution_state.py"
        adaptive_gate_py = root / "scripts" / "internal" / "adaptive_gate.py"
        resolve_ticket_py = root / "scripts" / "internal" / "resolve_ticket.py"
        sec_audit_skill = root / "skills" / "security-audit" / "SKILL.md"
        guardrails_rule = root / "rules" / "gemini-behavior-guardrails.md"
        living_spec = root / "docs" / "specs" / "implement" / "spec.md"

        # Invariant 1: [REPT-01] scripts/report-phase.sh presence, permissions (0755), and shebang
        if not report_phase_sh.is_file():
            errors.append(ValidationError(report_phase_sh, "EXECUTION_STATE_SCRIPT_VIOLATION", "Missing required script 'scripts/report-phase.sh'"))
        else:
            mode = report_phase_sh.stat().st_mode & 0o777
            if mode != 0o755:
                errors.append(ValidationError(report_phase_sh, "EXECUTION_STATE_SCRIPT_VIOLATION", f"scripts/report-phase.sh must have 0755 permissions, got {oct(mode)}"))
            try:
                sh_content = report_phase_sh.read_text(encoding="utf-8")
                if not sh_content.startswith("#!/usr/bin/env bash"):
                    errors.append(ValidationError(report_phase_sh, "EXECUTION_STATE_SCRIPT_VIOLATION", "scripts/report-phase.sh must start with '#!/usr/bin/env bash'"))
                if "set -euo pipefail" not in sh_content:
                    errors.append(ValidationError(report_phase_sh, "EXECUTION_STATE_SCRIPT_VIOLATION", "scripts/report-phase.sh must include 'set -euo pipefail'"))
            except Exception as exc:
                errors.append(ValidationError(report_phase_sh, "FILE_READ_ERROR", f"Could not read {report_phase_sh.name}: {exc}"))

        # Invariant 2: [STATE-01], [STATE-02], [RUN-02] scripts/internal/execution_state.py presence and schema
        if not execution_state_py.is_file():
            errors.append(ValidationError(execution_state_py, "EXECUTION_STATE_SCHEMA_VIOLATION", "Missing required engine 'scripts/internal/execution_state.py'"))
        else:
            try:
                es_content = execution_state_py.read_text(encoding="utf-8")
                for stage in ("STAGE_1_PREFLIGHT", "STAGE_2_BUILDER", "STAGE_3_ADAPTIVE_REVIEW", "STAGE_4_DOUBLE_VERIFY", "STAGE_5_COMMIT_AND_RESOLVE"):
                    if stage not in es_content:
                        errors.append(ValidationError(execution_state_py, "EXECUTION_STATE_SCHEMA_VIOLATION", f"scripts/internal/execution_state.py must define {stage}"))
                if "VALID_STAGES" not in es_content:
                    errors.append(ValidationError(execution_state_py, "EXECUTION_STATE_SCHEMA_VIOLATION", "scripts/internal/execution_state.py must define VALID_STAGES"))
                if "init_execution_state" not in es_content:
                    errors.append(ValidationError(execution_state_py, "EXECUTION_STATE_SCHEMA_VIOLATION", "scripts/internal/execution_state.py must define init_execution_state"))
                if "transition_execution_state" not in es_content:
                    errors.append(ValidationError(execution_state_py, "EXECUTION_STATE_SCHEMA_VIOLATION", "scripts/internal/execution_state.py must define transition_execution_state"))
                if "complete_execution_state" not in es_content:
                    errors.append(ValidationError(execution_state_py, "EXECUTION_STATE_SCHEMA_VIOLATION", "scripts/internal/execution_state.py must define complete_execution_state"))
            except Exception as exc:
                errors.append(ValidationError(execution_state_py, "FILE_READ_ERROR", f"Could not read {execution_state_py.name}: {exc}"))

        # Invariant 3: [DISARM-01] scripts/internal/adaptive_gate.py state-aware Stop gate disarm
        if not adaptive_gate_py.is_file():
            errors.append(ValidationError(adaptive_gate_py, "STAGE_GATING_VIOLATION", "Missing required script 'scripts/internal/adaptive_gate.py'"))
        else:
            try:
                ag_content = adaptive_gate_py.read_text(encoding="utf-8")
                if "[DISARM-01]" not in ag_content:
                    errors.append(ValidationError(adaptive_gate_py, "STAGE_GATING_VIOLATION", "scripts/internal/adaptive_gate.py must implement [DISARM-01]"))
                if "STAGE_5_COMMIT_AND_RESOLVE" not in ag_content:
                    errors.append(ValidationError(adaptive_gate_py, "STAGE_GATING_VIOLATION", "scripts/internal/adaptive_gate.py must check STAGE_5_COMMIT_AND_RESOLVE"))
                if "disarmed" not in ag_content:
                    errors.append(ValidationError(adaptive_gate_py, "STAGE_GATING_VIOLATION", "scripts/internal/adaptive_gate.py must return disarmed status"))
            except Exception as exc:
                errors.append(ValidationError(adaptive_gate_py, "FILE_READ_ERROR", f"Could not read {adaptive_gate_py.name}: {exc}"))

        # Invariant 4: [RUN-02] scripts/internal/resolve_ticket.py stage gating & completion
        if not resolve_ticket_py.is_file():
            errors.append(ValidationError(resolve_ticket_py, "STAGE_GATING_VIOLATION", "Missing required script 'scripts/internal/resolve_ticket.py'"))
        else:
            try:
                rt_content = resolve_ticket_py.read_text(encoding="utf-8")
                if "[RUN-02]" not in rt_content:
                    errors.append(ValidationError(resolve_ticket_py, "STAGE_GATING_VIOLATION", "scripts/internal/resolve_ticket.py must implement [RUN-02]"))
                if "STAGE_5_COMMIT_AND_RESOLVE" not in rt_content:
                    errors.append(ValidationError(resolve_ticket_py, "STAGE_GATING_VIOLATION", "scripts/internal/resolve_ticket.py must verify STAGE_5_COMMIT_AND_RESOLVE"))
                if "execution-state.json" not in rt_content:
                    errors.append(ValidationError(resolve_ticket_py, "STAGE_GATING_VIOLATION", "scripts/internal/resolve_ticket.py must verify execution-state.json presence"))
            except Exception as exc:
                errors.append(ValidationError(resolve_ticket_py, "FILE_READ_ERROR", f"Could not read {resolve_ticket_py.name}: {exc}"))

        # Invariant 5: [AIRGAP-01] Strict PR Airgap in skills/security-audit/SKILL.md & rules/gemini-behavior-guardrails.md
        if not sec_audit_skill.is_file():
            errors.append(ValidationError(sec_audit_skill, "STRICT_PR_AIRGAP_VIOLATION", "Missing required skill 'skills/security-audit/SKILL.md'"))
        else:
            try:
                sa_content = sec_audit_skill.read_text(encoding="utf-8")
                if "[AIRGAP-01]" not in sa_content:
                    errors.append(ValidationError(sec_audit_skill, "STRICT_PR_AIRGAP_VIOLATION", "skills/security-audit/SKILL.md must document [AIRGAP-01]"))
                if "report-phase.sh" not in sa_content:
                    errors.append(ValidationError(sec_audit_skill, "STRICT_PR_AIRGAP_VIOLATION", "skills/security-audit/SKILL.md must direct to use report-phase.sh"))
            except Exception as exc:
                errors.append(ValidationError(sec_audit_skill, "FILE_READ_ERROR", f"Could not read {sec_audit_skill.name}: {exc}"))

        if not guardrails_rule.is_file():
            errors.append(ValidationError(guardrails_rule, "STRICT_PR_AIRGAP_VIOLATION", "Missing required rule 'rules/gemini-behavior-guardrails.md'"))
        else:
            try:
                gr_content = guardrails_rule.read_text(encoding="utf-8")
                if "[AIRGAP-01]" not in gr_content:
                    errors.append(ValidationError(guardrails_rule, "STRICT_PR_AIRGAP_VIOLATION", "rules/gemini-behavior-guardrails.md must document [AIRGAP-01]"))
                if "Strict PR Airgap" not in gr_content:
                    errors.append(ValidationError(guardrails_rule, "STRICT_PR_AIRGAP_VIOLATION", "rules/gemini-behavior-guardrails.md must define Strict PR Airgap Invariant"))
            except Exception as exc:
                errors.append(ValidationError(guardrails_rule, "FILE_READ_ERROR", f"Could not read {guardrails_rule.name}: {exc}"))

        # Invariant 6: [EVAL-01] Living Spec Reconciliation & All 7 Delta Scenarios present
        if not living_spec.is_file():
            errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", "Missing required living spec 'docs/specs/implement/spec.md'"))
        else:
            try:
                ls_content = living_spec.read_text(encoding="utf-8")
                delta_scenarios = [
                    "[STATE-01]",
                    "[STATE-02]",
                    "[DISARM-01]",
                    "[REPT-01]",
                    "[AIRGAP-01]",
                    "[ORCH-03]",
                    "[RUN-02]",
                ]
                for sc in delta_scenarios:
                    if sc not in ls_content:
                        errors.append(ValidationError(living_spec, "LIVING_SPEC_RECONCILIATION_VIOLATION", f"docs/specs/implement/spec.md missing reconciled delta scenario {sc}"))
            except Exception as exc:
                errors.append(ValidationError(living_spec, "FILE_READ_ERROR", f"Could not read {living_spec.name}: {exc}"))

        return invariants_scanned, errors

    def validate_ephemeral_worker_context_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 31 Ephemeral Worker Context & Session Isolation invariants ([EPHM-01], [EPHM-02], ADR 0031):
        1. rules/subagent-orchestration.md mandates distinct subagent spawn via invoke_subagent with clean context ([EPHM-01])
        2. rules/subagent-orchestration.md strictly prohibits cross-stage session reuse via send_message ([EPHM-02])
        3. skills/implement/SKILL.md Step 3 mandates ephemeral worker context & prohibits session reuse ([EPHM-01], [EPHM-02])
        4. rules/operational-invariants.md summarizes ephemeral worker context & clean conversation isolation ([EPHM-01], [EPHM-02])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 4

        root = Path(root_dir)
        orch_rule = root / "rules" / "subagent-orchestration.md"
        implement_skill = root / "skills" / "implement" / "SKILL.md"
        op_rule = root / "rules" / "operational-invariants.md"

        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "EPHEMERAL_WORKER_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            try:
                orch_content = self.read_rule_content(orch_rule)
                if "[EPHM-01]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "EPHEMERAL_WORKER_VIOLATION", "rules/subagent-orchestration.md must reference scenario [EPHM-01]"))
                if "[EPHM-02]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "EPHEMERAL_WORKER_VIOLATION", "rules/subagent-orchestration.md must reference scenario [EPHM-02]"))
                if not re.search(r"(?i)brand-new\s+subagent\s+instance\s+via\s+`invoke_subagent`", orch_content):
                    errors.append(ValidationError(orch_rule, "EPHEMERAL_WORKER_VIOLATION", "rules/subagent-orchestration.md must mandate spawning a brand-new subagent instance via invoke_subagent"))
                if not re.search(r"(?i)strictly\s+prohibited\s+from\s+sending\s+review\s+instructions\s+via\s+`send_message`", orch_content):
                    errors.append(ValidationError(orch_rule, "EPHEMERAL_WORKER_VIOLATION", "rules/subagent-orchestration.md must prohibit sending review instructions via send_message to Stage 1 subagent ID"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "EPHEMERAL_WORKER_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
        else:
            try:
                imp_content = self._read_skill_with_references(implement_skill)
                if "[EPHM-01]" not in imp_content:
                    errors.append(ValidationError(implement_skill, "EPHEMERAL_WORKER_VIOLATION", "skills/implement/SKILL.md must reference scenario [EPHM-01]"))
                if "[EPHM-02]" not in imp_content:
                    errors.append(ValidationError(implement_skill, "EPHEMERAL_WORKER_VIOLATION", "skills/implement/SKILL.md must reference scenario [EPHM-02]"))
                if not re.search(r"(?i)brand-new\s+subagent\s+instance\s+via\s+`invoke_subagent`", imp_content):
                    errors.append(ValidationError(implement_skill, "EPHEMERAL_WORKER_VIOLATION", "skills/implement/SKILL.md must mandate spawning a brand-new subagent instance via invoke_subagent"))
                if not re.search(r"(?i)strictly\s+prohibited\s+from\s+(?:sending\s+review\s+instructions\s+via\s+`send_message`|reusing\s+(?:the\s+Stage\s+1\s+subagent|subagent\s+conversation\s+IDs))", imp_content):
                    errors.append(ValidationError(implement_skill, "EPHEMERAL_WORKER_VIOLATION", "skills/implement/SKILL.md must prohibit cross-stage session reuse via send_message"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        if not op_rule.is_file():
            errors.append(ValidationError(op_rule, "EPHEMERAL_WORKER_VIOLATION", "Missing required rule 'rules/operational-invariants.md'"))
        else:
            try:
                op_content = op_rule.read_text(encoding="utf-8")
                if "[EPHM-01]" not in op_content or "[EPHM-02]" not in op_content:
                    errors.append(ValidationError(op_rule, "EPHEMERAL_WORKER_VIOLATION", "rules/operational-invariants.md must reference scenarios [EPHM-01] and [EPHM-02]"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(op_rule, "FILE_READ_ERROR", f"Could not read {op_rule.name}: {exc}"))

        return invariants_scanned, errors

    def validate_three_tag_worker_handoff_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 31 Three-Tag Worker Handoff Contract invariants ([TAG-01], [TAG-02], ADR 0031):
        1. templates/worker-handoff.md defines standardized three-tag enclosure (<original_task>, <prior_attempt>, <instructions>) ([TAG-01])
        2. templates/worker-handoff.md prohibits dumping raw terminal test runner outputs or verbose compiler logs ([TAG-02])
        3. skills/implement/SKILL.md Step 3 references [TAG-01] and [TAG-02] and prohibits raw log dumping
        4. rules/subagent-orchestration.md and rules/operational-invariants.md reference [TAG-01] and [TAG-02]
        """
        errors: list[ValidationError] = []
        invariants_scanned = 4

        root = Path(root_dir)
        template_file = root / "templates" / "worker-handoff.md"
        implement_skill = root / "skills" / "implement" / "SKILL.md"
        orch_rule = root / "rules" / "subagent-orchestration.md"
        op_rule = root / "rules" / "operational-invariants.md"

        if not template_file.is_file():
            errors.append(ValidationError(template_file, "WORKER_HANDOFF_VIOLATION", "Missing required template 'templates/worker-handoff.md'"))
        else:
            try:
                t_content = template_file.read_text(encoding="utf-8")
                for tag in ("<original_task>", "</original_task>", "<prior_attempt>", "</prior_attempt>", "<instructions>", "</instructions>"):
                    if tag not in t_content:
                        errors.append(ValidationError(template_file, "WORKER_HANDOFF_VIOLATION", f"templates/worker-handoff.md must define tag '{tag}'"))
                if not re.search(r"(?i)strictly\s+prohibited\s+from\s+dumping\s+raw\s+terminal\s+test\s+runner\s+outputs\s+or\s+verbose\s+compiler\s+logs", t_content):
                    errors.append(ValidationError(template_file, "WORKER_HANDOFF_VIOLATION", "templates/worker-handoff.md must prohibit dumping raw terminal test runner outputs or verbose compiler logs"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(template_file, "FILE_READ_ERROR", f"Could not read {template_file.name}: {exc}"))

        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "WORKER_HANDOFF_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
        else:
            try:
                imp_content = self._read_skill_with_references(implement_skill)
                if "[TAG-01]" not in imp_content:
                    errors.append(ValidationError(implement_skill, "WORKER_HANDOFF_VIOLATION", "skills/implement/SKILL.md must reference scenario [TAG-01]"))
                if "[TAG-02]" not in imp_content:
                    errors.append(ValidationError(implement_skill, "WORKER_HANDOFF_VIOLATION", "skills/implement/SKILL.md must reference scenario [TAG-02]"))
                if not re.search(r"(?i)passes\s+only\s+structured\s+markdown\s+summaries\s+and\s+file\s+pointers", imp_content):
                    errors.append(ValidationError(implement_skill, "WORKER_HANDOFF_VIOLATION", "skills/implement/SKILL.md must require passing only structured markdown summaries and file pointers"))
                if not re.search(r"(?i)strictly\s+prohibited\s+from\s+dumping\s+raw\s+terminal\s+test\s+runner\s+outputs\s+or\s+verbose\s+compiler\s+logs", imp_content):
                    errors.append(ValidationError(implement_skill, "WORKER_HANDOFF_VIOLATION", "skills/implement/SKILL.md must prohibit dumping raw terminal test runner outputs or verbose compiler logs"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "WORKER_HANDOFF_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            try:
                orch_content = self.read_rule_content(orch_rule)
                if "[TAG-01]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "WORKER_HANDOFF_VIOLATION", "rules/subagent-orchestration.md must reference scenario [TAG-01]"))
                if "[TAG-02]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "WORKER_HANDOFF_VIOLATION", "rules/subagent-orchestration.md must reference scenario [TAG-02]"))
                if not re.search(r"(?i)strictly\s+prohibited\s+from\s+dumping\s+raw\s+terminal\s+test\s+runner\s+outputs\s+or\s+verbose\s+compiler\s+logs", orch_content):
                    errors.append(ValidationError(orch_rule, "WORKER_HANDOFF_VIOLATION", "rules/subagent-orchestration.md must prohibit dumping raw terminal test runner outputs or verbose compiler logs"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        if not op_rule.is_file():
            errors.append(ValidationError(op_rule, "WORKER_HANDOFF_VIOLATION", "Missing required rule 'rules/operational-invariants.md'"))
        else:
            try:
                op_content = op_rule.read_text(encoding="utf-8")
                if "[TAG-01]" not in op_content or "[TAG-02]" not in op_content:
                    errors.append(ValidationError(op_rule, "WORKER_HANDOFF_VIOLATION", "rules/operational-invariants.md must reference scenarios [TAG-01] and [TAG-02]"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(op_rule, "FILE_READ_ERROR", f"Could not read {op_rule.name}: {exc}"))

        return invariants_scanned, errors

    def validate_active_improvement_worker_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 31 Active Break-and-Fix Protocol & Failure Lineage invariants ([IMPR-01], [IMPR-02], ADR 0025, ADR 0031):
        1. agents/code-reviewer.md defines '## Active Improvement Worker Mode' and references [IMPR-01], [IMPR-02]
        2. skills/implement/SKILL.md Step 3 specifies break-and-fix protocol and failure lineage ([IMPR-01], [IMPR-02])
        3. rules/subagent-orchestration.md Sub-Trigger A2 specifies break-and-fix and failure lineage ([IMPR-01], [IMPR-02])
        4. rules/operational-invariants.md summarizes Active Improvement Worker break-and-fix and failure lineage ([IMPR-01], [IMPR-02])
        5. rules/AGENTS.md summarizes Active Improvement Worker mandate ([IMPR-01], [IMPR-02])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 5

        root = Path(root_dir)
        reviewer_agent = root / "agents" / "code-reviewer.md"
        implement_skill = root / "skills" / "implement" / "SKILL.md"
        orch_rule = root / "rules" / "subagent-orchestration.md"
        op_rule = root / "rules" / "operational-invariants.md"
        agents_rule = root / "rules" / "AGENTS.md"

        if not reviewer_agent.is_file():
            errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "Missing required agent 'agents/code-reviewer.md'"))
        else:
            try:
                rev_content = reviewer_agent.read_text(encoding="utf-8")
                if not re.search(r"(?m)^#{2,3}\s+Active\s+Improvement\s+Worker\s+Mode", rev_content):
                    errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "agents/code-reviewer.md must define '## Active Improvement Worker Mode'"))
                if "[IMPR-01]" not in rev_content:
                    errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "agents/code-reviewer.md must reference scenario [IMPR-01]"))
                if "[IMPR-02]" not in rev_content:
                    errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "agents/code-reviewer.md must reference scenario [IMPR-02]"))
                if "break the prior attempt" not in rev_content.lower():
                    errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "agents/code-reviewer.md must mandate break the prior attempt"))
                if "in-place" not in rev_content.lower() and "in place" not in rev_content.lower():
                    errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "agents/code-reviewer.md must mandate in-place defect remediation"))
                if not re.search(r"(?i)exit\s+code\s+0", rev_content):
                    errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "agents/code-reviewer.md must require confirming exit code 0"))
                if "What the prior attempt got wrong" not in rev_content:
                    errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "agents/code-reviewer.md must define 'What the prior attempt got wrong' section"))
                for field in ("Input", "Expected", "Actual", "Root Cause"):
                    if field not in rev_content:
                        errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", f"agents/code-reviewer.md must require failure lineage field '{field}'"))
                if "> [!WARNING] **Skepticism Disclaimer**" not in rev_content:
                    errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "agents/code-reviewer.md must include Skepticism Disclaimer"))
                if "3-Tier Verification Record" not in rev_content:
                    errors.append(ValidationError(reviewer_agent, "ACTIVE_IMPROVER_VIOLATION", "agents/code-reviewer.md must include 3-Tier Verification Record"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(reviewer_agent, "FILE_READ_ERROR", f"Could not read {reviewer_agent.name}: {exc}"))

        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
        else:
            try:
                imp_content = self._read_skill_with_references(implement_skill)
                if "[IMPR-01]" not in imp_content:
                    errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", "skills/implement/SKILL.md must reference scenario [IMPR-01]"))
                if "[IMPR-02]" not in imp_content:
                    errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", "skills/implement/SKILL.md must reference scenario [IMPR-02]"))
                if "break the prior attempt" not in imp_content.lower():
                    errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", "skills/implement/SKILL.md must instruct worker to break the prior attempt"))
                if "in-place" not in imp_content.lower() and "in place" not in imp_content.lower():
                    errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", "skills/implement/SKILL.md must instruct in-place defect remediation"))
                if not re.search(r"(?i)exit\s+code\s+0", imp_content):
                    errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", "skills/implement/SKILL.md must specify exit code 0"))
                if "What the prior attempt got wrong" not in imp_content:
                    errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", "skills/implement/SKILL.md must define 'What the prior attempt got wrong' section"))
                for field in ("Input", "Expected", "Actual", "Root Cause"):
                    if field not in imp_content:
                        errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", f"skills/implement/SKILL.md must specify failure lineage field '{field}'"))
                if "> [!WARNING] **Skepticism Disclaimer**" not in imp_content:
                    errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", "skills/implement/SKILL.md must require Skepticism Disclaimer"))
                if "3-Tier Verification Record" not in imp_content:
                    errors.append(ValidationError(implement_skill, "ACTIVE_IMPROVER_VIOLATION", "skills/implement/SKILL.md must require 3-Tier Verification Record"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            try:
                orch_content = self.read_rule_content(orch_rule)
                if "[IMPR-01]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/subagent-orchestration.md must reference scenario [IMPR-01]"))
                if "[IMPR-02]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/subagent-orchestration.md must reference scenario [IMPR-02]"))
                if "break the prior attempt" not in orch_content.lower():
                    errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/subagent-orchestration.md must mandate break the prior attempt"))
                if "in-place" not in orch_content.lower() and "in place" not in orch_content.lower():
                    errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/subagent-orchestration.md must mandate in-place defect remediation"))
                if not re.search(r"(?i)exit\s+code\s+0", orch_content):
                    errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/subagent-orchestration.md must mandate confirming exit code 0"))
                if "What the prior attempt got wrong" not in orch_content:
                    errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/subagent-orchestration.md must define 'What the prior attempt got wrong' section"))
                for field in ("Input", "Expected", "Actual", "Root Cause"):
                    if field not in orch_content:
                        errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", f"rules/subagent-orchestration.md must require failure lineage field '{field}'"))
                if "> [!WARNING] **Skepticism Disclaimer**" not in orch_content:
                    errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/subagent-orchestration.md must require Skepticism Disclaimer"))
                if "3-Tier Verification Record" not in orch_content:
                    errors.append(ValidationError(orch_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/subagent-orchestration.md must require 3-Tier Verification Record"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        if not op_rule.is_file():
            errors.append(ValidationError(op_rule, "ACTIVE_IMPROVER_VIOLATION", "Missing required rule 'rules/operational-invariants.md'"))
        else:
            try:
                op_content = op_rule.read_text(encoding="utf-8")
                if "[IMPR-01]" not in op_content or "[IMPR-02]" not in op_content:
                    errors.append(ValidationError(op_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/operational-invariants.md must reference scenarios [IMPR-01] and [IMPR-02]"))
                if "in-place" not in op_content.lower() and "in place" not in op_content.lower():
                    errors.append(ValidationError(op_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/operational-invariants.md must reference in-place patching"))
                if "What the prior attempt got wrong" not in op_content:
                    errors.append(ValidationError(op_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/operational-invariants.md must reference 'What the prior attempt got wrong' section"))
                if "Skepticism Disclaimer" not in op_content:
                    errors.append(ValidationError(op_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/operational-invariants.md must reference Skepticism Disclaimer"))
                if not re.search(r"(?i)3-tier\s+verification\s+record", op_content):
                    errors.append(ValidationError(op_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/operational-invariants.md must reference 3-tier Verification Record"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(op_rule, "FILE_READ_ERROR", f"Could not read {op_rule.name}: {exc}"))

        if not agents_rule.is_file():
            errors.append(ValidationError(agents_rule, "ACTIVE_IMPROVER_VIOLATION", "Missing required rule 'rules/AGENTS.md'"))
        else:
            try:
                agents_content = agents_rule.read_text(encoding="utf-8")
                if "[IMPR-01]" not in agents_content:
                    errors.append(ValidationError(agents_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/AGENTS.md must reference scenario [IMPR-01]"))
                if "[IMPR-02]" not in agents_content:
                    errors.append(ValidationError(agents_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/AGENTS.md must reference scenario [IMPR-02]"))
                if "Active Improvement Worker" not in agents_content:
                    errors.append(ValidationError(agents_rule, "ACTIVE_IMPROVER_VIOLATION", "rules/AGENTS.md must summarize Active Improvement Worker mandate"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        return invariants_scanned, errors

    def validate_bounded_verification_output_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 31 Bounded Verification Output Invariants ([BND-01], [BND-02], ADR 0031):
        1. rules/subagent-orchestration.md specifies bounded verification output ([BND-01], [BND-02])
        2. skills/implement/SKILL.md Step 4 specifies bounded verification output ([BND-01], [BND-02])
        3. rules/operational-invariants.md summarizes Bounded Verification Output Invariant ([BND-01], [BND-02])
        4. rules/AGENTS.md summarizes Bounded Verification Output Invariant ([BND-01], [BND-02])
        5. docs/specs/implement/spec.md defines Bounded Verification Output Invariant ([BND-01], [BND-02])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 5

        root = Path(root_dir)
        orch_rule = root / "rules" / "subagent-orchestration.md"
        implement_skill = root / "skills" / "implement" / "SKILL.md"
        op_rule = root / "rules" / "operational-invariants.md"
        agents_rule = root / "rules" / "AGENTS.md"
        spec_file = root / "docs" / "specs" / "implement" / "spec.md"

        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "BOUNDED_VERIFICATION_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            try:
                orch_content = self.read_rule_content(orch_rule)
                if "[BND-01]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "BOUNDED_VERIFICATION_VIOLATION", "rules/subagent-orchestration.md must reference scenario [BND-01]"))
                if "[BND-02]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "BOUNDED_VERIFICATION_VIOLATION", "rules/subagent-orchestration.md must reference scenario [BND-02]"))
                if not re.search(r"(?i)concise\s+summary\s+notification", orch_content):
                    errors.append(ValidationError(orch_rule, "BOUNDED_VERIFICATION_VIOLATION", "rules/subagent-orchestration.md must require concise summary notification"))
                if not re.search(r"(?i)not\s+dump\s+hundreds\s+of\s+lines\s+of\s+passing\s+test\s+traces", orch_content):
                    errors.append(ValidationError(orch_rule, "BOUNDED_VERIFICATION_VIOLATION", "rules/subagent-orchestration.md must prohibit dumping passing test traces"))
                if not re.search(r"(?i)error\s+diagnostics\s+and\s+failing\s+assertions", orch_content):
                    errors.append(ValidationError(orch_rule, "BOUNDED_VERIFICATION_VIOLATION", "rules/subagent-orchestration.md must mandate error diagnostics and failing assertions on failure"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        if not implement_skill.is_file():
            errors.append(ValidationError(implement_skill, "BOUNDED_VERIFICATION_VIOLATION", "Missing required skill 'skills/implement/SKILL.md'"))
        else:
            try:
                skill_content = self._read_skill_with_references(implement_skill)
                if "[BND-01]" not in skill_content:
                    errors.append(ValidationError(implement_skill, "BOUNDED_VERIFICATION_VIOLATION", "skills/implement/SKILL.md must reference scenario [BND-01]"))
                if "[BND-02]" not in skill_content:
                    errors.append(ValidationError(implement_skill, "BOUNDED_VERIFICATION_VIOLATION", "skills/implement/SKILL.md must reference scenario [BND-02]"))
                if not re.search(r"(?i)concise\s+summary\s+notification", skill_content):
                    errors.append(ValidationError(implement_skill, "BOUNDED_VERIFICATION_VIOLATION", "skills/implement/SKILL.md must require concise summary notification"))
                if not re.search(r"(?i)dump(?:ing)?\s+hundreds\s+of\s+lines\s+of\s+passing\s+test\s+traces", skill_content):
                    errors.append(ValidationError(implement_skill, "BOUNDED_VERIFICATION_VIOLATION", "skills/implement/SKILL.md must prohibit dumping passing test traces"))
                if not re.search(r"(?i)error\s+diagnostics(?:,\s+stack\s+traces,)?\s+and\s+failing\s+assertions", skill_content):
                    errors.append(ValidationError(implement_skill, "BOUNDED_VERIFICATION_VIOLATION", "skills/implement/SKILL.md must mandate error diagnostics on failure"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        if not op_rule.is_file():
            errors.append(ValidationError(op_rule, "BOUNDED_VERIFICATION_VIOLATION", "Missing required rule 'rules/operational-invariants.md'"))
        else:
            try:
                op_content = op_rule.read_text(encoding="utf-8")
                if "[BND-01]" not in op_content or "[BND-02]" not in op_content:
                    errors.append(ValidationError(op_rule, "BOUNDED_VERIFICATION_VIOLATION", "rules/operational-invariants.md must reference scenarios [BND-01] and [BND-02]"))
                if "Bounded Verification Output" not in op_content:
                    errors.append(ValidationError(op_rule, "BOUNDED_VERIFICATION_VIOLATION", "rules/operational-invariants.md must summarize Bounded Verification Output Invariant"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(op_rule, "FILE_READ_ERROR", f"Could not read {op_rule.name}: {exc}"))

        if not agents_rule.is_file():
            errors.append(ValidationError(agents_rule, "BOUNDED_VERIFICATION_VIOLATION", "Missing required rule 'rules/AGENTS.md'"))
        else:
            try:
                agents_content = agents_rule.read_text(encoding="utf-8")
                if "[BND-01]" not in agents_content or "[BND-02]" not in agents_content:
                    errors.append(ValidationError(agents_rule, "BOUNDED_VERIFICATION_VIOLATION", "rules/AGENTS.md must reference scenarios [BND-01] and [BND-02]"))
                if "Bounded Verification Output" not in agents_content:
                    errors.append(ValidationError(agents_rule, "BOUNDED_VERIFICATION_VIOLATION", "rules/AGENTS.md must summarize Bounded Verification Output Invariant"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        if not spec_file.is_file():
            errors.append(ValidationError(spec_file, "BOUNDED_VERIFICATION_VIOLATION", "Missing required living spec 'docs/specs/implement/spec.md'"))
        else:
            try:
                spec_content = spec_file.read_text(encoding="utf-8")
                if "[BND-01]" not in spec_content or "[BND-02]" not in spec_content:
                    errors.append(ValidationError(spec_file, "BOUNDED_VERIFICATION_VIOLATION", "docs/specs/implement/spec.md must reference scenario [BND-01] and [BND-02]"))
                if "Bounded Verification Output" not in spec_content:
                    errors.append(ValidationError(spec_file, "BOUNDED_VERIFICATION_VIOLATION", "docs/specs/implement/spec.md must define Bounded Verification Output Invariant"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(spec_file, "FILE_READ_ERROR", f"Could not read {spec_file.name}: {exc}"))

        return invariants_scanned, errors

    def validate_evaluation_phase_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 32/35/36 Ephemeral Reviewer Prompting, Bounded Output, and On-Disk Persistence invariants ([EVLP-01], [EVLP-02], [ERR-REV-01], [ERR-SEC-01], ADR 0032, ADR 0035):
        1. skills/code-review/SKILL.md Step 4 encloses reviewer prompts with Three-Tag Contract ([EVLP-01])
        2. skills/code-review/SKILL.md Step 5 mandates bounded chat output (<= 15 lines), fallback persistence (.scratch/reports/code-review-<timestamp>.md), and zero references to Brain UI Artifacts or ArtifactMetadata ([EVLP-02], [ERR-REV-01])
        3. skills/security-audit/SKILL.md Step 2 encloses security-auditor prompt with Three-Tag Contract ([EVLP-01])
        4. skills/security-audit/SKILL.md Step 4 mandates bounded chat output (<= 15 lines), fallback persistence (.scratch/security/security-audit-<sha>.md), and zero references to Brain UI Artifacts or ArtifactMetadata ([EVLP-02], [ERR-SEC-01])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 4

        root = Path(root_dir)
        code_review_skill = root / "skills" / "code-review" / "SKILL.md"
        security_audit_skill = root / "skills" / "security-audit" / "SKILL.md"

        if not code_review_skill.is_file():
            errors.append(ValidationError(code_review_skill, "EVALUATION_PHASE_VIOLATION", "Missing required skill 'skills/code-review/SKILL.md'"))
        else:
            try:
                cr_content = code_review_skill.read_text(encoding="utf-8")
                if "[EVLP-01]" not in cr_content:
                    errors.append(ValidationError(code_review_skill, "EVALUATION_PHASE_VIOLATION", "skills/code-review/SKILL.md must reference scenario [EVLP-01]"))
                for tag in ("<original_task>", "</original_task>", "<context_pointers>", "</context_pointers>", "<instructions>", "</instructions>"):
                    if tag not in cr_content:
                        errors.append(ValidationError(code_review_skill, "EVALUATION_PHASE_VIOLATION", f"skills/code-review/SKILL.md must define tag '{tag}'"))
                if "[EVLP-02]" not in cr_content:
                    errors.append(ValidationError(code_review_skill, "EVALUATION_PHASE_VIOLATION", "skills/code-review/SKILL.md must reference scenario [EVLP-02]"))
                if not re.search(r"(?i)code-review-<id>\.md", cr_content):
                    errors.append(ValidationError(code_review_skill, "EVALUATION_PHASE_VIOLATION", "skills/code-review/SKILL.md must persist authoritative report to code-review-<id>.md"))
                if not re.search(r"(?i)15\s+lines\s+or\s+fewer", cr_content):
                    errors.append(ValidationError(code_review_skill, "EVALUATION_PHASE_VIOLATION", "skills/code-review/SKILL.md must bound chat transcript summary to 15 lines or fewer"))
                if "[ERR-REV-01]" not in cr_content:
                    errors.append(ValidationError(code_review_skill, "EVALUATION_PHASE_VIOLATION", "skills/code-review/SKILL.md must reference scenario [ERR-REV-01]"))
                if ".scratch/reports/code-review-<timestamp>.md" not in cr_content:
                    errors.append(ValidationError(code_review_skill, "EVALUATION_PHASE_VIOLATION", "skills/code-review/SKILL.md must persist fallback report to .scratch/reports/code-review-<timestamp>.md"))
                if self._has_active_evaluation_brain_artifacts(cr_content):
                    errors.append(ValidationError(code_review_skill, "EVALUATION_PHASE_VIOLATION", "skills/code-review/SKILL.md must contain zero references directing Brain UI Artifacts or ArtifactMetadata (deprecated per ADR 0035)"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(code_review_skill, "FILE_READ_ERROR", f"Could not read {code_review_skill.name}: {exc}"))

        if not security_audit_skill.is_file():
            errors.append(ValidationError(security_audit_skill, "EVALUATION_PHASE_VIOLATION", "Missing required skill 'skills/security-audit/SKILL.md'"))
        else:
            try:
                sec_content = security_audit_skill.read_text(encoding="utf-8")
                if "[EVLP-01]" not in sec_content:
                    errors.append(ValidationError(security_audit_skill, "EVALUATION_PHASE_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [EVLP-01]"))
                for tag in ("<original_task>", "</original_task>", "<context_pointers>", "</context_pointers>", "<instructions>", "</instructions>"):
                    if tag not in sec_content:
                        errors.append(ValidationError(security_audit_skill, "EVALUATION_PHASE_VIOLATION", f"skills/security-audit/SKILL.md must define tag '{tag}'"))
                if "[EVLP-02]" not in sec_content:
                    errors.append(ValidationError(security_audit_skill, "EVALUATION_PHASE_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [EVLP-02]"))
                if not re.search(r"\.scratch/security/(?:audit|security-audit)-<sha>\.md", sec_content):
                    errors.append(ValidationError(security_audit_skill, "EVALUATION_PHASE_VIOLATION", "skills/security-audit/SKILL.md must persist authoritative report to .scratch/security/audit-<sha>.md"))
                if not re.search(r"(?i)15\s+lines\s+or\s+fewer", sec_content):
                    errors.append(ValidationError(security_audit_skill, "EVALUATION_PHASE_VIOLATION", "skills/security-audit/SKILL.md must bound chat transcript summary to 15 lines or fewer"))
                if "[ERR-SEC-01]" not in sec_content:
                    errors.append(ValidationError(security_audit_skill, "EVALUATION_PHASE_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [ERR-SEC-01]"))
                if ".scratch/security/security-audit-<sha>.md" not in sec_content:
                    errors.append(ValidationError(security_audit_skill, "EVALUATION_PHASE_VIOLATION", "skills/security-audit/SKILL.md must persist fallback report to .scratch/security/security-audit-<sha>.md"))
                if self._has_active_evaluation_brain_artifacts(sec_content):
                    errors.append(ValidationError(security_audit_skill, "EVALUATION_PHASE_VIOLATION", "skills/security-audit/SKILL.md must contain zero references directing Brain UI Artifacts or ArtifactMetadata (deprecated per ADR 0035)"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(security_audit_skill, "FILE_READ_ERROR", f"Could not read {security_audit_skill.name}: {exc}"))

        cr_template = root / "skills" / "code-review" / "resources" / "review-artifact-template.md"
        if cr_template.is_file():
            try:
                cr_t_content = cr_template.read_text(encoding="utf-8")
                if self._has_active_evaluation_brain_artifacts(cr_t_content):
                    errors.append(ValidationError(cr_template, "EVALUATION_PHASE_VIOLATION", "skills/code-review/resources/review-artifact-template.md must contain zero references directing Brain UI Artifacts or ArtifactMetadata (deprecated per ADR 0035)"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(cr_template, "FILE_READ_ERROR", f"Could not read {cr_template.name}: {exc}"))

        sec_template = root / "skills" / "security-audit" / "resources" / "audit-report-template.md"
        if sec_template.is_file():
            try:
                sec_t_content = sec_template.read_text(encoding="utf-8")
                if self._has_active_evaluation_brain_artifacts(sec_t_content):
                    errors.append(ValidationError(sec_template, "EVALUATION_PHASE_VIOLATION", "skills/security-audit/resources/audit-report-template.md must contain zero references directing Brain UI Artifacts or ArtifactMetadata (deprecated per ADR 0035)"))
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(sec_template, "FILE_READ_ERROR", f"Could not read {sec_template.name}: {exc}"))

        return invariants_scanned, errors

    def validate_track_32_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 32 Evaluation Phase Resilience, Remediation Airgap & Integrity Invariants (ADR 0032):
        1. Resilient Phase Reporting Cascade ([CASC-01], [CASC-02])
        2. Three-Tag Reviewer Subagent Prompting ([EVLP-01])
        3. Bounded Evaluation Output & Disk Persistence ([EVLP-02], [BND-01], [BND-02])
        4. Dual-Domain Attack Surface Detection ([DUDO-01])
        5. Opt-In Remediation Worker Mode & Atomic Fix Commits ([REMD-01], [REMD-02])
        6. Strict PR Airgap Preservation during Active Remediation ([AIRGAP-02])
        """
        errors: list[ValidationError] = []
        invariants_scanned = 6

        root = Path(root_dir)
        code_review_skill = root / "skills" / "code-review" / "SKILL.md"
        security_audit_skill = root / "skills" / "security-audit" / "SKILL.md"
        guardrails_rule = root / "rules" / "gemini-behavior-guardrails.md"

        cr_content: str | None = None
        if not code_review_skill.is_file():
            errors.append(ValidationError(code_review_skill, "TRACK32_CASCADE_VIOLATION", "Missing required skill 'skills/code-review/SKILL.md'"))
        else:
            try:
                cr_content = code_review_skill.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(code_review_skill, "FILE_READ_ERROR", f"Could not read {code_review_skill.name}: {exc}"))

        sec_content: str | None = None
        if not security_audit_skill.is_file():
            errors.append(ValidationError(security_audit_skill, "TRACK32_CASCADE_VIOLATION", "Missing required skill 'skills/security-audit/SKILL.md'"))
        else:
            try:
                sec_content = security_audit_skill.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(security_audit_skill, "FILE_READ_ERROR", f"Could not read {security_audit_skill.name}: {exc}"))

        guard_content: str | None = None
        if not guardrails_rule.is_file():
            errors.append(ValidationError(guardrails_rule, "TRACK32_AIRGAP_VIOLATION", "Missing required rule 'rules/gemini-behavior-guardrails.md'"))
        else:
            try:
                guard_content = guardrails_rule.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(guardrails_rule, "FILE_READ_ERROR", f"Could not read {guardrails_rule.name}: {exc}"))

        # --- Invariant 1: Resilient Phase Reporting Cascade ([CASC-01], [CASC-02]) ---
        cascade_pattern = r"\[\s+-f\s+[\"']\./scripts/report-phase\.sh[\"']\s+\]\s+&&\s+\./scripts/report-phase\.sh\s+\S+\s+\S+.*?\|\|\s+\.agents/plugins/agy-matt/scripts/report-phase\.sh"
        if cr_content is not None:
            if not re.search(cascade_pattern, cr_content):
                errors.append(ValidationError(code_review_skill, "TRACK32_CASCADE_VIOLATION", "skills/code-review/SKILL.md must mandate resilient phase reporting cascade"))
            if "[CASC-01]" not in cr_content:
                errors.append(ValidationError(code_review_skill, "TRACK32_CASCADE_VIOLATION", "skills/code-review/SKILL.md must reference scenario [CASC-01]"))
            if "[CASC-02]" not in cr_content:
                errors.append(ValidationError(code_review_skill, "TRACK32_CASCADE_VIOLATION", "skills/code-review/SKILL.md must reference scenario [CASC-02]"))

        if sec_content is not None:
            if not re.search(cascade_pattern, sec_content):
                errors.append(ValidationError(security_audit_skill, "TRACK32_CASCADE_VIOLATION", "skills/security-audit/SKILL.md must mandate resilient phase reporting cascade"))
            if "[CASC-01]" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_CASCADE_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [CASC-01]"))

        # --- Invariant 2: Three-Tag Reviewer Subagent Prompt Enclosure ([EVLP-01]) ---
        required_tags = ("<original_task>", "</original_task>", "<context_pointers>", "</context_pointers>", "<instructions>", "</instructions>")
        if cr_content is not None:
            if "[EVLP-01]" not in cr_content:
                errors.append(ValidationError(code_review_skill, "TRACK32_THREE_TAG_VIOLATION", "skills/code-review/SKILL.md must reference scenario [EVLP-01]"))
            for tag in required_tags:
                if tag not in cr_content:
                    errors.append(ValidationError(code_review_skill, "TRACK32_THREE_TAG_VIOLATION", f"skills/code-review/SKILL.md must define tag '{tag}'"))

        if sec_content is not None:
            if "[EVLP-01]" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_THREE_TAG_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [EVLP-01]"))
            for tag in required_tags:
                if tag not in sec_content:
                    errors.append(ValidationError(security_audit_skill, "TRACK32_THREE_TAG_VIOLATION", f"skills/security-audit/SKILL.md must define tag '{tag}'"))

        # --- Invariant 3: Bounded Evaluation Output & Disk Persistence ([EVLP-02], [BND-01], [BND-02]) ---
        if cr_content is not None:
            if "[EVLP-02]" not in cr_content:
                errors.append(ValidationError(code_review_skill, "TRACK32_BOUNDED_OUTPUT_VIOLATION", "skills/code-review/SKILL.md must reference scenario [EVLP-02]"))
            if not re.search(r"(?i)code-review-<id>\.md", cr_content):
                errors.append(ValidationError(code_review_skill, "TRACK32_BOUNDED_OUTPUT_VIOLATION", "skills/code-review/SKILL.md must persist authoritative report to code-review-<id>.md"))
            if not re.search(r"(?i)15\s+lines\s+or\s+fewer", cr_content):
                errors.append(ValidationError(code_review_skill, "TRACK32_BOUNDED_OUTPUT_VIOLATION", "skills/code-review/SKILL.md must bound chat transcript summary to 15 lines or fewer"))

        if sec_content is not None:
            if "[EVLP-02]" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_BOUNDED_OUTPUT_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [EVLP-02]"))
            if ".scratch/security/audit-<sha>.md" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_BOUNDED_OUTPUT_VIOLATION", "skills/security-audit/SKILL.md must persist authoritative report to .scratch/security/audit-<sha>.md"))
            if not re.search(r"(?i)15\s+lines\s+or\s+fewer", sec_content):
                errors.append(ValidationError(security_audit_skill, "TRACK32_BOUNDED_OUTPUT_VIOLATION", "skills/security-audit/SKILL.md must bound chat transcript summary to 15 lines or fewer"))

        # --- Invariant 4: Dual-Domain Threat Scoping & Detection ([DUDO-01]) ---
        if sec_content is not None:
            if "[DUDO-01]" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_DUAL_DOMAIN_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [DUDO-01]"))
            if not re.search(r"(?i)Domain B", sec_content):
                errors.append(ValidationError(security_audit_skill, "TRACK32_DUAL_DOMAIN_VIOLATION", "skills/security-audit/SKILL.md must evaluate Domain B attack surfaces"))
            if not re.search(r"\*\.sh|\*\.py", sec_content):
                errors.append(ValidationError(security_audit_skill, "TRACK32_DUAL_DOMAIN_VIOLATION", "skills/security-audit/SKILL.md must inspect CLI scripts (*.sh, *.py)"))

        # --- Invariant 5: Opt-In Remediation Worker Mode & Atomic Fix Commits ([REMD-01], [REMD-02]) ---
        if cr_content is not None:
            if "[REMD-01]" not in cr_content:
                errors.append(ValidationError(code_review_skill, "TRACK32_REMEDIATION_VIOLATION", "skills/code-review/SKILL.md must reference scenario [REMD-01]"))
            if "[REMD-02]" not in cr_content:
                errors.append(ValidationError(code_review_skill, "TRACK32_REMEDIATION_VIOLATION", "skills/code-review/SKILL.md must reference scenario [REMD-02]"))
            if "--fix" not in cr_content:
                errors.append(ValidationError(code_review_skill, "TRACK32_REMEDIATION_VIOLATION", "skills/code-review/SKILL.md must support --fix flag"))
            if "fix(review):" not in cr_content:
                errors.append(ValidationError(code_review_skill, "TRACK32_REMEDIATION_VIOLATION", "skills/code-review/SKILL.md must specify commit prefix fix(review):"))

        if sec_content is not None:
            if "[REMD-01]" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_REMEDIATION_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [REMD-01]"))
            if "[REMD-02]" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_REMEDIATION_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [REMD-02]"))
            if "--fix" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_REMEDIATION_VIOLATION", "skills/security-audit/SKILL.md must support --fix flag"))
            if "fix(security):" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_REMEDIATION_VIOLATION", "skills/security-audit/SKILL.md must specify commit prefix fix(security):"))

        # --- Invariant 6: Strict PR Airgap Preservation during Active Remediation ([AIRGAP-02], [AIRGAP-03]) ---
        if guard_content is not None:
            if "[AIRGAP-02]" not in guard_content:
                errors.append(ValidationError(guardrails_rule, "TRACK32_AIRGAP_VIOLATION", "rules/gemini-behavior-guardrails.md must reference scenario [AIRGAP-02]"))
            if "[AIRGAP-03]" not in guard_content:
                errors.append(ValidationError(guardrails_rule, "TRACK32_AIRGAP_VIOLATION", "rules/gemini-behavior-guardrails.md must reference scenario [AIRGAP-03]"))
            if ".scratch/pr-body.md" not in guard_content or "gh pr create" not in guard_content:
                errors.append(ValidationError(guardrails_rule, "TRACK32_AIRGAP_VIOLATION", "rules/gemini-behavior-guardrails.md must forbid .scratch/pr-body.md and gh pr create"))
            if not re.search(r"(?i)Recommend-and-Stop", guard_content):
                errors.append(ValidationError(guardrails_rule, "TRACK32_AIRGAP_VIOLATION", "rules/gemini-behavior-guardrails.md must mandate Recommend-and-Stop"))

        if cr_content is not None:
            if "[AIRGAP-02]" not in cr_content:
                errors.append(ValidationError(code_review_skill, "TRACK32_AIRGAP_VIOLATION", "skills/code-review/SKILL.md must reference scenario [AIRGAP-02]"))
            if not re.search(r"(?is)--fix.*?\.scratch/pr-body\.md.*?gh pr create.*?Recommend-and-Stop", cr_content):
                errors.append(ValidationError(code_review_skill, "TRACK32_AIRGAP_VIOLATION", "skills/code-review/SKILL.md must preserve PR airgap under --fix"))

        if sec_content is not None:
            if "[AIRGAP-02]" not in sec_content:
                errors.append(ValidationError(security_audit_skill, "TRACK32_AIRGAP_VIOLATION", "skills/security-audit/SKILL.md must reference scenario [AIRGAP-02]"))
            if not re.search(r"(?is)--fix.*?\.scratch/pr-body\.md.*?gh pr create.*?Recommend-and-Stop", sec_content):
                errors.append(ValidationError(security_audit_skill, "TRACK32_AIRGAP_VIOLATION", "skills/security-audit/SKILL.md must preserve PR airgap under --fix"))

        return invariants_scanned, errors

    @staticmethod
    def _has_active_evaluation_brain_artifacts(text: str) -> bool:
        """
        [ERR-REV-01] [ERR-SEC-01] Helper asserting zero references directing Brain UI Artifacts
        or ArtifactMetadata across evaluation phase runbooks, while permitting explicit
        prohibition/deprecation statements.
        """
        cleaned = re.sub(r"\[[A-Z0-9_-]+\]", "", text)

        # 1. Check ArtifactMetadata usage:
        for m in re.finditer(r"ArtifactMetadata", cleaned):
            start, end = m.start(), m.end()
            clause_start = max(
                cleaned.rfind("\n\n", 0, start),
                cleaned.rfind("\n#", 0, start),
                cleaned.rfind("\n-", 0, start),
                cleaned.rfind("\n*", 0, start),
                cleaned.rfind(".\n", 0, start),
                cleaned.rfind(". ", 0, start),
                cleaned.rfind(";\n", 0, start),
            )
            clause_start = 0 if clause_start == -1 else clause_start + 1

            clause_end_candidates = [
                cleaned.find("\n\n", end),
                cleaned.find("\n#", end),
                cleaned.find("\n-", end),
                cleaned.find("\n*", end),
                cleaned.find(".\n", end),
                cleaned.find(". ", end),
                cleaned.find(";\n", end),
            ]
            valid_candidates = [c for c in clause_end_candidates if c != -1]
            clause_end = min(valid_candidates) if valid_candidates else len(cleaned)

            clause = cleaned[clause_start:clause_end]
            negated = (
                re.search(r"\bwithout\s+[`\"]?ArtifactMetadata[`\"]?", clause, re.IGNORECASE)
                or re.search(
                    r"\b(?:deprecat(?:ed|ion|e|es|ing)?|prohibit(?:ed|ion|s|ing)?|bar(?:red|ring|s)?|ban(?:ned|ning|s)?|zero)\b.*ArtifactMetadata",
                    clause,
                    re.IGNORECASE | re.DOTALL,
                )
                or re.search(
                    r"ArtifactMetadata.*\b(?:deprecat(?:ed|ion|e|es|ing)?|prohibit(?:ed|ion|s|ing)?|bar(?:red|ring|s)?|ban(?:ned|ning|s)?)\b",
                    clause,
                    re.IGNORECASE | re.DOTALL,
                )
            )
            if not negated:
                return True

        # 2. Check Brain UI Artifact / brain directory active directives
        target_pattern = re.compile(
            r"(?:mirror(?:ing)?\s+(?:.*?\s+)?to\s+(?:the\s+)?(?:brain|ui\s+artifact)|"
            r"(?:create|render|persist|write|save)\s+(?:.*?\s+)?(?:an?\s+)?(?:antigravity\s+)?brain\s+ui\s+artifact|"
            r"(?:create|render|persist|write|save)\s+(?:.*?to\s+)?(?:the\s+)?<appdatadir>/brain|"
            r"with\s+artifactmetadata|"
            r"provide\s+artifactmetadata)",
            re.IGNORECASE,
        )

        for m in target_pattern.finditer(cleaned):
            start, end = m.start(), m.end()
            clause_start = max(
                cleaned.rfind("\n\n", 0, start),
                cleaned.rfind("\n#", 0, start),
                cleaned.rfind("\n-", 0, start),
                cleaned.rfind("\n*", 0, start),
                cleaned.rfind(".\n", 0, start),
                cleaned.rfind(". ", 0, start),
                cleaned.rfind(";\n", 0, start),
            )
            clause_start = 0 if clause_start == -1 else clause_start + 1

            clause_end_candidates = [
                cleaned.find("\n\n", end),
                cleaned.find("\n#", end),
                cleaned.find("\n-", end),
                cleaned.find("\n*", end),
                cleaned.find(".\n", end),
                cleaned.find(". ", end),
                cleaned.find(";\n", end),
            ]
            valid_candidates = [c for c in clause_end_candidates if c != -1]
            clause_end = min(valid_candidates) if valid_candidates else len(cleaned)

            clause = cleaned[clause_start:clause_end]
            negated = re.search(
                r"\b(?:without|zero|prohibit(?:ed|ion|s|ing)?|ban(?:ned|ning|s)?|bar(?:red|ring|s)?|deprecat(?:ed|ion|e|es|ing)?)\b",
                clause,
                re.IGNORECASE,
            )
            if not negated:
                return True

        return False

    @staticmethod
    def _has_active_brain_mirroring(text: str) -> bool:
        """
        [API-03] Helper detecting active instructions or documentation directing
        Brain UI Artifact mirroring, while ignoring explicit deprecation/prohibition statements
        across soft line wraps, multi-line bullet lists, and clause boundaries.
        """
        cleaned = re.sub(r"\[[A-Z0-9_-]+\]", "", text)

        target_pattern = re.compile(
            r"(?:mirror(?:ing)?\s+(?:it\s+)?to\s+(?:the\s+)?(?:brain|ui\s+artifact)|"
            r"(?:brain\s+)?ui\s+artifact(?:\s+mirroring)?|"
            r"updates?\s+(?:the\s+)?(?:local\s+)?(?:brain\s+)?ui\s+artifact|"
            r"project(?:ing|ion)?\s+into\s+(?:the\s+)?(?:brain\s+)?ui\s+artifact|"
            r"<appdatadir>/brain/[^\s\)]*task-graph|"
            r"ui\s+artifact\s+and\s+task-graph|"
            r"brain\s+artifact\s+directories)",
            re.IGNORECASE,
        )

        neg_post = re.compile(
            r"^\s*(?:\.[a-z0-9_`]+\s*)?(?:\([^\)]*\)\s*|\)\s*)?(?:is\s+|are\s+)?(?:strictly\s+|permanently\s+)?(?:prohibit(?:s|ed|ing|ion)?|ban(?:s|ned|ning)?|deprecat(?:e|ed|es|ing|ion)?|bar(?:s|red|ring)?|eliminat(?:e|es|ed|ing))\b",
            re.IGNORECASE,
        )

        neg_header = re.compile(
            r"\b(?:prohibit(?:s|ed|ing|ion)?|ban(?:s|ned|ning)?|bar(?:s|red|ring)?|forbidden)\b",
            re.IGNORECASE,
        )

        for m in target_pattern.finditer(cleaned):
            start, end = m.start(), m.end()

            clause_start = max(
                cleaned.rfind("\n\n", 0, start),
                cleaned.rfind(". ", 0, start),
                cleaned.rfind(";\n", 0, start),
                cleaned.rfind("; ", 0, start),
            )
            clause_start = 0 if clause_start == -1 else (clause_start + 2 if cleaned[clause_start:clause_start+2] in (". ", "; ") else clause_start + 2)

            clause_end_candidates = [
                cleaned.find("\n\n", end),
                cleaned.find(". ", end),
                cleaned.find("; ", end),
                cleaned.find("\n#", end),
            ]
            valid_candidates = [c for c in clause_end_candidates if c != -1]
            clause_end = min(valid_candidates) if valid_candidates else len(cleaned)

            pre_text = cleaned[clause_start:start]
            post_text = cleaned[end:clause_end]

            pre_normalized = " ".join(pre_text.split())
            post_normalized = " ".join(post_text.split())

            if re.search(r"\b(?:zero(?:\s+references?\s+to)?|prohibit(?:s|ed|ing|ion)?(?:\s+(?:of|on|from|any|the|in))?|ban(?:s|ned|ning)?(?:\s+on)?|bar(?:s|red|ring)?(?:\s+from)?|deprecat(?:e|ed|es|ing|ion)?|eliminat(?:e|es|ed|ing)?|without(?:\s+(?:any|active))?|no(?:\s+(?:longer|more|active))?)\b\s*$", pre_normalized, re.IGNORECASE):
                continue
            if re.search(r"\b(?:zero(?:\s+references?\s+to)?|prohibit(?:s|ed|ing|ion)?(?:\s+(?:of|on|from|any|the|in))?|ban(?:s|ned|ning)?(?:\s+on)?|bar(?:s|red|ring)?(?:\s+from)?|deprecat(?:e|ed|es|ing|ion)?|eliminat(?:e|es|ed|ing)?|without|no\s+(?:longer|more|active))\b[^\w]*\w{0,30}[^\w]*$", pre_normalized, re.IGNORECASE):
                continue

            if neg_post.search(post_normalized) or re.search(r"^\s*(?:\)\s*)?\([^\)]*(?:deprecat|prohibit|ban|bar|eliminat)[^\)]*\)", post_normalized, re.IGNORECASE):
                continue

            line_start = cleaned.rfind("\n", 0, start)
            line_start = 0 if line_start == -1 else line_start + 1
            curr_line = cleaned[line_start:start].lstrip()
            if curr_line.startswith(("-", "*", "1.", "2.", "3.", "4.", "5.")):
                prev_nl = cleaned.rfind("\n", 0, line_start - 1)
                prev_line = cleaned[0 if prev_nl == -1 else prev_nl + 1 : line_start - 1]
                if neg_header.search(prev_line):
                    continue

            return True

        return False

    def validate_ticket_deconstruction_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 33/35 Ticket Deconstruction & Canonical On-Disk Persistence Invariants ([DISK-01], [DISK-02], [BAN-01], [API-03], ADR 0033, ADR 0035):
        1. Mandatory creation of on-disk canonical task-graph.md on ticket deconstruction ([DISK-01]).
        2. Absolute ban on Brain UI Artifact mirroring; persistence exclusively to .scratch/<feature-slug>/task-graph.md ([DISK-02], [BAN-01], [API-03]).
        """
        errors: list[ValidationError] = []
        invariants_scanned = 2

        root = Path(root_dir)
        to_tickets_skill = root / "skills" / "to-tickets" / "SKILL.md"
        issue_tracker_doc = root / "docs" / "agents" / "issue-tracker.md"

        tt_content: str | None = None
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "TICKET_DECONSTRUCTION_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            try:
                tt_content = to_tickets_skill.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        it_content: str | None = None
        if not issue_tracker_doc.is_file():
            errors.append(ValidationError(issue_tracker_doc, "TICKET_DECONSTRUCTION_VIOLATION", "Missing required doc 'docs/agents/issue-tracker.md'"))
        else:
            try:
                it_content = issue_tracker_doc.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(ValidationError(issue_tracker_doc, "FILE_READ_ERROR", f"Could not read {issue_tracker_doc.name}: {exc}"))

        if tt_content is not None:
            if "[DISK-01]" not in tt_content:
                errors.append(ValidationError(to_tickets_skill, "TICKET_DECONSTRUCTION_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [DISK-01]"))
            if ".scratch/<feature-slug>/task-graph.md" not in tt_content:
                errors.append(ValidationError(to_tickets_skill, "TICKET_DECONSTRUCTION_VIOLATION", "skills/to-tickets/SKILL.md must document '.scratch/<feature-slug>/task-graph.md' canonical on-disk persistence"))
            if not re.search(r"(?is)Phase 1.*?Phase 2.*?Phase 3.*?✅ completed", tt_content):
                errors.append(ValidationError(to_tickets_skill, "TICKET_DECONSTRUCTION_VIOLATION", "skills/to-tickets/SKILL.md must document Phase 1, 2, 3 marked as completed in initial cockpit"))
            if "0/N resolved" not in tt_content and "0/" not in tt_content:
                errors.append(ValidationError(to_tickets_skill, "TICKET_DECONSTRUCTION_VIOLATION", "skills/to-tickets/SKILL.md must document Phase 4 marked as in-progress (0/N resolved) in initial cockpit"))
            if not re.search(r"(?is)(?:downstream.*?phases|review phases).*?⬜\s*pending", tt_content):
                errors.append(ValidationError(to_tickets_skill, "TICKET_DECONSTRUCTION_VIOLATION", "skills/to-tickets/SKILL.md must document downstream review phases marked as '⬜ pending' in initial cockpit"))

            if "[DISK-02]" not in tt_content and "[BAN-01]" not in tt_content:
                errors.append(ValidationError(to_tickets_skill, "TICKET_DECONSTRUCTION_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario [DISK-02] or [BAN-01]"))

            if self._has_active_brain_mirroring(tt_content):
                errors.append(ValidationError(to_tickets_skill, "TICKET_DECONSTRUCTION_VIOLATION", "skills/to-tickets/SKILL.md must contain zero references to Brain UI Artifact mirroring (deprecated per ADR 0035)"))

            if not re.search(r"(?is)authoritative.*?persistence target", tt_content):
                errors.append(ValidationError(to_tickets_skill, "TICKET_DECONSTRUCTION_VIOLATION", "skills/to-tickets/SKILL.md must designate .scratch/<feature-slug>/task-graph.md as authoritative persistence target for worker sessions"))

        if it_content is not None:
            if "task-graph.md" not in it_content or "single source of truth" not in it_content.lower():
                errors.append(ValidationError(issue_tracker_doc, "TICKET_DECONSTRUCTION_VIOLATION", "docs/agents/issue-tracker.md must document task-graph.md as persistent physical single source of truth in directory conventions"))
            if self._has_active_brain_mirroring(it_content):
                errors.append(ValidationError(issue_tracker_doc, "TICKET_DECONSTRUCTION_VIOLATION", "docs/agents/issue-tracker.md must contain zero references to Brain UI Artifact mirroring (deprecated per ADR 0035)"))

        return invariants_scanned, errors

    def validate_command_center_refresh_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 33/35 In-Place Command Center Dashboard Refresh & Lean Runbook Invariants ([REFRESH-01], [REFRESH-02], [SUMM-01], [SUMM-02], [API-03], ADR 0033, ADR 0035):
        1. skills/to-tickets/SKILL.md:
           - Declares scenario tags [REFRESH-01] and [REFRESH-02].
           - Step 5 documents autonomous on-disk re-scan of .scratch/<feature-slug>/task-graph.md and issues/ via rescan_and_refresh_dashboard upon operator input ([REFRESH-01]).
           - Step 5 documents in-place update of .scratch/<feature-slug>/task-graph.md on workspace filesystem with zero Brain UI Artifact mirroring ([REFRESH-01], [API-03]).
           - Step 5 documents dynamic frontier recalculation emitting 1-line execution commands (/implement <ticket-path>) for all unblocked frontier tickets simultaneously ([REFRESH-02]).
           - Step 5 documents standby notification when all active frontier tickets are in-progress ([REFRESH-02]).
           - Step 5 documents transitioning Phase 4 to completed, Phase 5 to in-progress, and emitting Structured Session Brief for /code-review when all tickets are resolved ([REFRESH-02]).
           - Step 5 documents Lean 3-Line Summary format and prohibition of transcript diagram dumps ([SUMM-01], [SUMM-02], [API-03]).
        2. skills/implement/SKILL.md:
           - Documents on-disk update of .scratch/<feature-slug>/task-graph.md with zero Brain UI Artifact mirroring ([API-03]).
           - Documents Lean 3-Line Summary or concise completion summary format ([API-03]).
           - Documents prohibition of transcript diagram dumps ([API-03]).
        3. rules/operational-invariants.md & rules/AGENTS.md:
           - Section 4 documents [REFRESH-01] and [REFRESH-02].
           - Section 4 documents on-disk task graph refresh with zero UI artifact mirroring and Lean 3-Line Summary format.
        4. Helper scripts (scripts/internal/resolve_ticket.py):
           - Documents zero active Brain UI Artifact mirroring.
           - Implements or documents Lean 3-Line Summary format.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []
        invariants_scanned = 2

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if not to_tickets_skill.is_file():
            errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "Missing required skill 'skills/to-tickets/SKILL.md'"))
        else:
            try:
                content = to_tickets_skill.read_text(encoding="utf-8")
                if "[REFRESH-01]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [REFRESH-01]"))
                if "[REFRESH-02]" not in content:
                    errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md must reference scenario tag [REFRESH-02]"))

                step5_match = re.search(r"### Step 5:.*?(?=\n## |\Z)", content, re.DOTALL)
                if not step5_match:
                    errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md must contain '### Step 5:' section"))
                else:
                    s5 = step5_match.group(0)
                    if not re.search(r"(?:re-scan|rescan).*?disk", s5, re.IGNORECASE):
                        errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document autonomous on-disk re-scanning"))

                    if self._has_active_brain_mirroring(s5):
                        errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must contain zero references to Brain UI Artifact mirroring or brain directory writes"))

                    if not re.search(r"(?:in-place|update).*?task-graph\.md", s5, re.IGNORECASE):
                        errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document in-place on-disk task graph refresh"))

                    if "standby" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document standby notification when active frontier is in-progress"))
                    if "/code-review" not in s5:
                        errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document /code-review handoff when all tickets resolved"))

                    if "lean 3-line summary" not in s5.lower() and "3-line summary" not in s5.lower():
                        errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must document Lean 3-Line Summary protocol"))
                    if not re.search(r"(?:prohibit|ban|eliminate).*?(?:diagram|mermaid|table).*?(?:dump|chat|transcript)", s5, re.IGNORECASE):
                        errors.append(ValidationError(to_tickets_skill, "REFRESH_INVARIANT_VIOLATION", "skills/to-tickets/SKILL.md Step 5 must prohibit transcript diagram dumps in chat"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if implement_skill.is_file():
            try:
                imp_content = self._read_skill_with_references(implement_skill)
                if "task-graph.md" not in imp_content:
                    errors.append(ValidationError(implement_skill, "REFRESH_INVARIANT_VIOLATION", "skills/implement/SKILL.md must document on-disk update of task-graph.md"))
                if self._has_active_brain_mirroring(imp_content):
                    errors.append(ValidationError(implement_skill, "REFRESH_INVARIANT_VIOLATION", "skills/implement/SKILL.md must contain zero references to Brain UI Artifact mirroring"))
                if not re.search(r"(?:lean\s+3-line\s+summary|3-line\s+summary|concise\s+completion\s+summary)", imp_content, re.IGNORECASE):
                    errors.append(ValidationError(implement_skill, "REFRESH_INVARIANT_VIOLATION", "skills/implement/SKILL.md must document Lean 3-Line Summary or concise completion summary format"))
                if not re.search(r"(?:prohibit|ban|eliminate|zero).*?(?:transcript|diagram|mermaid|table).*?(?:dump|chat)", imp_content, re.IGNORECASE):
                    errors.append(ValidationError(implement_skill, "REFRESH_INVARIANT_VIOLATION", "skills/implement/SKILL.md must document prohibition of transcript diagram dumps"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        op_rules = root_dir / "rules" / "operational-invariants.md"
        if op_rules.is_file():
            try:
                op_content = op_rules.read_text(encoding="utf-8")
                s4_match = re.search(r"## 4\..*?(?=\n## 5\.|\Z)", op_content, re.DOTALL)
                if s4_match:
                    s4 = s4_match.group(0)
                    if "[REFRESH-01]" not in s4 and "[REFRESH-01]" not in op_content:
                        errors.append(ValidationError(op_rules, "REFRESH_INVARIANT_VIOLATION", "rules/operational-invariants.md Section 4 must reference scenario [REFRESH-01]"))
                    if "[REFRESH-02]" not in s4 and "[REFRESH-02]" not in op_content:
                        errors.append(ValidationError(op_rules, "REFRESH_INVARIANT_VIOLATION", "rules/operational-invariants.md Section 4 must reference scenario [REFRESH-02]"))
                    if self._has_active_brain_mirroring(s4):
                        errors.append(ValidationError(op_rules, "REFRESH_INVARIANT_VIOLATION", "rules/operational-invariants.md Section 4 must not reference UI artifact mirroring (deprecated per ADR 0035)"))
                    if "lean 3-line summary" not in s4.lower() and "3-line summary" not in s4.lower():
                        errors.append(ValidationError(op_rules, "REFRESH_INVARIANT_VIOLATION", "rules/operational-invariants.md Section 4 must reference Lean 3-Line Summary protocol"))
                    if not re.search(r"(?:prohibit|ban|eliminate).*?(?:diagram|transcript)", s4, re.IGNORECASE):
                        errors.append(ValidationError(op_rules, "REFRESH_INVARIANT_VIOLATION", "rules/operational-invariants.md Section 4 must document prohibition of transcript diagram dumps"))
            except Exception as exc:
                errors.append(ValidationError(op_rules, "FILE_READ_ERROR", f"Could not read {op_rules.name}: {exc}"))

        agents_rules = root_dir / "rules" / "AGENTS.md"
        if agents_rules.is_file():
            try:
                agents_content = agents_rules.read_text(encoding="utf-8")
                if self._has_active_brain_mirroring(agents_content):
                    errors.append(ValidationError(agents_rules, "REFRESH_INVARIANT_VIOLATION", "rules/AGENTS.md must not reference UI artifact mirroring (deprecated per ADR 0035)"))
                if "[BAN-01]" not in agents_content:
                    errors.append(ValidationError(agents_rules, "REFRESH_INVARIANT_VIOLATION", "rules/AGENTS.md must reference scenario [BAN-01]"))
                if "[SUMM-01]" not in agents_content or "[SUMM-02]" not in agents_content:
                    errors.append(ValidationError(agents_rules, "REFRESH_INVARIANT_VIOLATION", "rules/AGENTS.md must reference scenarios [SUMM-01] and [SUMM-02]"))
            except Exception as exc:
                errors.append(ValidationError(agents_rules, "FILE_READ_ERROR", f"Could not read {agents_rules.name}: {exc}"))

        resolve_ticket_helper = root_dir / "scripts" / "internal" / "resolve_ticket.py"
        if resolve_ticket_helper.is_file():
            try:
                helper_content = resolve_ticket_helper.read_text(encoding="utf-8")
                if self._has_active_brain_mirroring(helper_content):
                    errors.append(ValidationError(resolve_ticket_helper, "REFRESH_INVARIANT_VIOLATION", "scripts/internal/resolve_ticket.py must contain zero references directing Brain UI Artifact mirroring"))
                if not re.search(r"(?:lean\s+3-line\s+summary|lean_summary|3-line\s+summary)", helper_content, re.IGNORECASE):
                    errors.append(ValidationError(resolve_ticket_helper, "REFRESH_INVARIANT_VIOLATION", "scripts/internal/resolve_ticket.py must implement or document Lean 3-Line Summary Protocol"))
            except Exception as exc:
                errors.append(ValidationError(resolve_ticket_helper, "FILE_READ_ERROR", f"Could not read {resolve_ticket_helper.name}: {exc}"))

        return invariants_scanned, errors

    def validate_spec_swarm_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 34/36 Two-Stage Ephemeral Spec Swarm Invariants ([SWARM-01], [SWARM-02], [SEAM-01], [SEAM-02], [QUOTA-01], [QUOTA-02], [DIFF-01], [DIFF-02], [GATE-01], [GATE-02], [EVAL-01], [EVAL-02], ADR 0034):
        1. skills/to-spec/SKILL.md:
           - Documents Two-Stage Ephemeral Swarm, Pipeline Coordinator persona, Spec Drafter and Adversarial Spec Auditor.
           - Documents pristine context window (0 prior tokens) and Three-Tag handoff contract (<original_task>, <prior_attempt>, <instructions>).
           - Documents Mandatory Physical Testing Seam Verification Protocol ([SEAM-01], [SEAM-02]) and read_file inspection.
           - Documents Adversarial Scenario Quota ([QUOTA-01], [QUOTA-02]) across 3 failure categories.
           - Documents Mechanical Baseline Diff Check ([DIFF-01], [DIFF-02]) for brownfield delta mode.
           - Documents Double-Verification Gate & Task Graph Cockpit Sync ([GATE-01]).
           - Documents Bounded Chat Transcript Output & Recommend-and-Stop ([GATE-02]) (<= 15 lines).
           - Documents single-turn direct escape hatch (--direct) and failure circuit breaker ([SWARM-02]).
        2. rules/subagent-orchestration.md:
           - Declares Sub-Trigger A4: Two-Stage Ephemeral Spec Swarm (/to-spec).
           - Enforces Pipeline Coordinator persona and anti-monolithic authoring invariant (< 15,000 tokens).
           - Documents Stage 1 Spec Drafter (dispatches TypeName: "self" or spec-implementer / primary-researcher).
           - Documents Stage 2 Adversarial Spec Auditor (dispatches code-reviewer / design-explorer / adversarial worker).
           - Documents pristine context window (0 prior tokens).
           - Documents Standardized Three-Tag Worker Handoff Contract (<original_task>, <prior_attempt>, <instructions>).
           - Prohibits dumping raw terminal test runner outputs or verbose logs.
           - Documents Double-Verification Gate ([GATE-01]) and Bounded Output ([GATE-02]).
        3. rules/operational-invariants.md & rules/AGENTS.md:
           - Reference Sub-Trigger A4 or Two-Stage Ephemeral Spec Swarm.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []

        to_spec_skill = root_dir / "skills" / "to-spec" / "SKILL.md"
        if not to_spec_skill.is_file():
            errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "Missing required skill 'skills/to-spec/SKILL.md'"))
        else:
            try:
                content = to_spec_skill.read_text(encoding="utf-8")
                if "Two-Stage Ephemeral Spec Swarm" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must document Two-Stage Ephemeral Spec Swarm"))
                if "Pipeline Coordinator" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must document Pipeline Coordinator persona"))
                if "Spec Drafter" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must document Stage 1 Spec Drafter"))
                if "Adversarial Spec Auditor" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must document Stage 2 Adversarial Spec Auditor"))
                if not re.search(r"(?i)pristine context window|0 prior tokens", content):
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must document pristine context window for Stage 2 Auditor"))

                for tag in ("<original_task>", "<prior_attempt>", "<instructions>"):
                    if tag not in content:
                        errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", f"skills/to-spec/SKILL.md must define tag '{tag}'"))

                if "[SEAM-01]" not in content or "[SEAM-02]" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must reference scenario tags [SEAM-01] and [SEAM-02]"))
                if "read_file" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must document physical seam inspection via read_file"))

                if "[QUOTA-01]" not in content or "[QUOTA-02]" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must reference scenario tags [QUOTA-01] and [QUOTA-02]"))

                if "[DIFF-01]" not in content or "[DIFF-02]" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must reference scenario tags [DIFF-01] and [DIFF-02]"))

                if "[GATE-01]" not in content or "[GATE-02]" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must reference scenario tags [GATE-01] and [GATE-02]"))
                if "[SWARM-01]" not in content or "[SWARM-02]" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must reference scenario tags [SWARM-01] and [SWARM-02]"))
                if "feat/<feature-slug>" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must specify execution on feat/<feature-slug>"))
                if "--direct" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must document --direct escape hatch"))
                if not re.search(r"(?i)circuit breaker", content):
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must document failure circuit breaker"))
                for field in ("Input", "Expected", "Actual", "Root Cause"):
                    if field not in content:
                        errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", f"skills/to-spec/SKILL.md failure lineage must contain '{field}' field"))
                if not re.search(r"(?i)15 lines or fewer|<=\s*15 lines", content):
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must bound chat summary output to 15 lines or fewer"))
                if not re.search(r"Pre-Flight (?:Feature )?Branch (?:Alignment|Inception)", content):
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must document Pre-Flight Branch Alignment Gate and feature branch inception"))
                for branch_tag in ("[BRANCH-01]", "[ERR-BRANCH-DIRTY]", "[ERR-BRANCH-NESTED]", "[ERR-BRANCH-SLUG]", "[ERR-BRANCH-LOCK]", "[EDGE-BRANCH-EXISTS]"):
                    if branch_tag not in content:
                        errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", f"skills/to-spec/SKILL.md must reference scenario tag {branch_tag}"))
                if "git status --porcelain" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must verify working tree cleanliness via 'git status --porcelain'"))
                if "git branch --show-current" not in content:
                    errors.append(ValidationError(to_spec_skill, "SPEC_SWARM_VIOLATION", "skills/to-spec/SKILL.md must verify base branch alignment via 'git branch --show-current'"))

            except Exception as exc:
                errors.append(ValidationError(to_spec_skill, "FILE_READ_ERROR", f"Could not read {to_spec_skill.name}: {exc}"))

        orch_rule = root_dir / "rules" / "subagent-orchestration.md"
        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            try:
                orch_content = self.read_rule_content(orch_rule)
                if not re.search(r"Sub-Trigger\s+A4:\s+Two-Stage Ephemeral Spec Swarm", orch_content):
                    errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md must declare Sub-Trigger A4: Two-Stage Ephemeral Spec Swarm"))
                if "/to-spec" not in orch_content:
                    errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md must codify the /to-spec delegation trigger"))

                for tag in ("<original_task>", "<prior_attempt>", "<instructions>"):
                    if tag not in orch_content:
                        errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", f"rules/subagent-orchestration.md must define tag '{tag}' in Sub-Trigger A4"))

                if not re.search(r"(?i)pristine context window|0 prior tokens", orch_content):
                    errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md must document pristine context window for Stage 2 Auditor"))

                if "[SEAM-01]" not in orch_content or "[SEAM-02]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md must reference [SEAM-01] and [SEAM-02]"))

                if "[QUOTA-01]" not in orch_content or "[QUOTA-02]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md must reference [QUOTA-01] and [QUOTA-02]"))

                if "[DIFF-01]" not in orch_content or "[DIFF-02]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md must reference [DIFF-01] and [DIFF-02]"))

                if "[GATE-01]" not in orch_content or "[GATE-02]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md must reference [GATE-01] and [GATE-02]"))

                if "[SWARM-01]" not in orch_content or "[SWARM-02]" not in orch_content:
                    errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md must reference [SWARM-01] and [SWARM-02]"))

                sub_a4_match = re.search(r"(?s)Sub-Trigger\s+A4:.*?(?=###\s+Trigger\s+[B-Z]|\Z)", orch_content)
                if not sub_a4_match:
                    errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md must contain Sub-Trigger A4 section"))
                else:
                    sub_a4_text = sub_a4_match.group(0)
                    if "feat/<feature-slug>" not in sub_a4_text:
                        errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md Sub-Trigger A4 must specify execution on feat/<feature-slug>"))
                    if not re.search(r"checked-out\s+`?feat/<feature-slug>`?\s+branch", sub_a4_text):
                        errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md Sub-Trigger A4 must codify feature branch dispatch binding"))
                    if "--direct" not in sub_a4_text:
                        errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md Sub-Trigger A4 must document --direct escape hatch"))
                    if not re.search(r"(?i)circuit breaker", sub_a4_text):
                        errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", "rules/subagent-orchestration.md Sub-Trigger A4 must document failure circuit breaker"))
                    for field in ("Input", "Expected", "Actual", "Root Cause"):
                        if field not in sub_a4_text:
                            errors.append(ValidationError(orch_rule, "SPEC_SWARM_VIOLATION", f"rules/subagent-orchestration.md Sub-Trigger A4 failure lineage must contain '{field}' field"))

            except Exception as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        agents_rule = root_dir / "rules" / "AGENTS.md"
        if not agents_rule.is_file():
            errors.append(ValidationError(agents_rule, "SPEC_SWARM_VIOLATION", "Missing required rule 'rules/AGENTS.md'"))
        else:
            try:
                agents_content = agents_rule.read_text(encoding="utf-8")
                if "Sub-Trigger A4" not in agents_content and "Two-Stage Ephemeral Spec Swarm" not in agents_content:
                    errors.append(ValidationError(agents_rule, "SPEC_SWARM_VIOLATION", "rules/AGENTS.md must reference Sub-Trigger A4 or Two-Stage Ephemeral Spec Swarm"))
                if "[SWARM-01]" not in agents_content or "[SWARM-02]" not in agents_content:
                    errors.append(ValidationError(agents_rule, "SPEC_SWARM_VIOLATION", "rules/AGENTS.md must reference [SWARM-01] and [SWARM-02]"))
            except Exception as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        op_rule = root_dir / "rules" / "operational-invariants.md"
        if not op_rule.is_file():
            errors.append(ValidationError(op_rule, "SPEC_SWARM_VIOLATION", "Missing required rule 'rules/operational-invariants.md'"))
        else:
            try:
                op_content = op_rule.read_text(encoding="utf-8")
                if "Sub-Trigger A4" not in op_content and "Two-Stage Ephemeral Spec Swarm" not in op_content:
                    errors.append(ValidationError(op_rule, "SPEC_SWARM_VIOLATION", "rules/operational-invariants.md must reference Sub-Trigger A4 or Two-Stage Ephemeral Spec Swarm"))
                if "feat/<feature-slug>" not in op_content:
                    errors.append(ValidationError(op_rule, "SPEC_SWARM_VIOLATION", "rules/operational-invariants.md must reference feat/<feature-slug> for Sub-Trigger A4"))
            except Exception as exc:
                errors.append(ValidationError(op_rule, "FILE_READ_ERROR", f"Could not read {op_rule.name}: {exc}"))

        to_tickets_skill = root_dir / "skills" / "to-tickets" / "SKILL.md"
        if to_tickets_skill.is_file():
            try:
                tickets_content = to_tickets_skill.read_text(encoding="utf-8")
                if "feat/<feature-slug>" not in tickets_content:
                    errors.append(ValidationError(to_tickets_skill, "SPEC_SWARM_VIOLATION", "skills/to-tickets/SKILL.md must verify active git branch matches feat/<feature-slug>"))
                if not re.search(r"git branch --show-current|git branch matches `?feat/<feature-slug>`?", tickets_content):
                    errors.append(ValidationError(to_tickets_skill, "SPEC_SWARM_VIOLATION", "skills/to-tickets/SKILL.md must assert active branch via git branch --show-current"))
            except Exception as exc:
                errors.append(ValidationError(to_tickets_skill, "FILE_READ_ERROR", f"Could not read {to_tickets_skill.name}: {exc}"))

        implement_skill = root_dir / "skills" / "implement" / "SKILL.md"
        if implement_skill.is_file():
            try:
                impl_content = self._read_skill_with_references(implement_skill)
                if "feat/<feature-slug>" not in impl_content:
                    errors.append(ValidationError(implement_skill, "SPEC_SWARM_VIOLATION", "skills/implement/SKILL.md must assert active git branch matches feat/<feature-slug>"))
                if "git branch --show-current" not in impl_content:
                    errors.append(ValidationError(implement_skill, "SPEC_SWARM_VIOLATION", "skills/implement/SKILL.md must check branch via git branch --show-current"))
            except Exception as exc:
                errors.append(ValidationError(implement_skill, "FILE_READ_ERROR", f"Could not read {implement_skill.name}: {exc}"))

        invariants_scanned = 12
        return invariants_scanned, errors

    def validate_track_34_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 34/38 Bug Diagnoser Ephemeral Swarm & Orchestration Invariants (ADR 0038, [EVAL-01], [EVAL-02]):
        1. skills/diagnosing-bugs/SKILL.md:
           - Documents Three-Stage Ephemeral Swarm (Pipeline Coordinator, Stage 1 Isolator, Stage 2 Falsifier, Stage 3 Fixer).
           - Documents Diagnostic Workspace (.scratch/diagnostics/<bug-id>/).
           - Documents Attack the Premise circuit breaker (2 consecutive failures).
           - Documents probe cleanup gates (probes.json, git grep '[DEBUG-').
        2. agents/bug-diagnoser.md:
           - Defines subagent manifest (name: bug-diagnoser, tools, model, skills).
           - Defines Attack the Premise circuit breaker schemas (<shared_premise>, <falsification_test>).
        3. rules/subagent-orchestration.md:
           - Trigger C / Sub-Trigger C specifies Three-Stage Ephemeral Swarm.
           - Specifies ephemeral subagent dispatch (invoke_subagent, 0 prior tokens / pristine context window).
           - Specifies Stage 1 Isolator / Stage 1: Ephemeral Isolator Worker.
           - Specifies Three-Tag handoffs (<original_symptom>, <repro_command>, <instructions>).
           - Specifies Attack the Premise circuit breaker (2 consecutive failures).
        4. rules/AGENTS.md & rules/operational-invariants.md:
           - References Sub-Trigger C or Three-Stage Ephemeral Bug Isolator Swarm.
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []

        bugs_skill = root_dir / "skills" / "diagnosing-bugs" / "SKILL.md"
        if not bugs_skill.is_file():
            errors.append(ValidationError(bugs_skill, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "Missing required skill 'skills/diagnosing-bugs/SKILL.md'"))
        else:
            try:
                skill_content = bugs_skill.read_text(encoding="utf-8")
                if "Three-Stage Ephemeral Swarm" not in skill_content and "Three-Stage" not in skill_content:
                    errors.append(ValidationError(bugs_skill, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "skills/diagnosing-bugs/SKILL.md must document Three-Stage Ephemeral Swarm"))
                if "Diagnostic Workspace" not in skill_content and ".scratch/diagnostics/" not in skill_content:
                    errors.append(ValidationError(bugs_skill, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "skills/diagnosing-bugs/SKILL.md must document Diagnostic Workspace"))
                if "Attack the Premise" not in skill_content:
                    errors.append(ValidationError(bugs_skill, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "skills/diagnosing-bugs/SKILL.md must document Attack the Premise circuit breaker"))
                if "probes.json" not in skill_content or "[DEBUG-" not in skill_content:
                    errors.append(ValidationError(bugs_skill, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "skills/diagnosing-bugs/SKILL.md must document probe cleanup gates"))
            except Exception as exc:
                errors.append(ValidationError(bugs_skill, "FILE_READ_ERROR", f"Could not read {bugs_skill.name}: {exc}"))

        bug_agent = root_dir / "agents" / "bug-diagnoser.md"
        if not bug_agent.is_file():
            errors.append(ValidationError(bug_agent, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "Missing required agent 'agents/bug-diagnoser.md'"))
        else:
            try:
                agent_content = bug_agent.read_text(encoding="utf-8")
                if "name: bug-diagnoser" not in agent_content:
                    errors.append(ValidationError(bug_agent, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "agents/bug-diagnoser.md must define name: bug-diagnoser"))
                if "Attack the Premise" not in agent_content:
                    errors.append(ValidationError(bug_agent, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "agents/bug-diagnoser.md must document Attack the Premise circuit breaker"))
                if "<shared_premise>" not in agent_content or "<falsification_test>" not in agent_content:
                    errors.append(ValidationError(bug_agent, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "agents/bug-diagnoser.md must define circuit breaker schemas <shared_premise> and <falsification_test>"))
            except Exception as exc:
                errors.append(ValidationError(bug_agent, "FILE_READ_ERROR", f"Could not read {bug_agent.name}: {exc}"))

        orch_rule = root_dir / "rules" / "subagent-orchestration.md"
        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            try:
                orch_content = self.read_rule_content(orch_rule)
                match = re.search(r"###\s+Trigger\s+C:.*?(?=###\s+Trigger|\Z)", orch_content, re.DOTALL)
                if not match:
                    errors.append(ValidationError(orch_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "rules/subagent-orchestration.md must declare Trigger C"))
                else:
                    trigger_c = match.group(0)
                    if "Three-Stage Ephemeral Swarm" not in trigger_c:
                        errors.append(ValidationError(orch_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "rules/subagent-orchestration.md Trigger C must specify Three-Stage Ephemeral Swarm"))
                    if not re.search(r"Stage 1:?\s*(?:Ephemeral\s+)?Isolator", trigger_c, re.IGNORECASE):
                        errors.append(ValidationError(orch_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "rules/subagent-orchestration.md Trigger C must specify Stage 1 Isolator"))
                    if "Three-Tag" not in trigger_c:
                        errors.append(ValidationError(orch_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "rules/subagent-orchestration.md Trigger C must specify Three-Tag handoffs"))
                    for tag in ("<original_symptom>", "<repro_command>", "<instructions>"):
                        if tag not in trigger_c:
                            errors.append(ValidationError(orch_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", f"rules/subagent-orchestration.md Trigger C must define tag '{tag}'"))
                    if "Attack the Premise" not in trigger_c:
                        errors.append(ValidationError(orch_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "rules/subagent-orchestration.md Trigger C must reference Attack the Premise circuit breaker"))
            except Exception as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        agents_rule = root_dir / "rules" / "AGENTS.md"
        if not agents_rule.is_file():
            errors.append(ValidationError(agents_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "Missing required rule 'rules/AGENTS.md'"))
        else:
            try:
                agents_content = agents_rule.read_text(encoding="utf-8")
                if "Sub-Trigger C" not in agents_content and "Three-Stage Ephemeral Bug Isolator Swarm" not in agents_content:
                    errors.append(ValidationError(agents_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "rules/AGENTS.md must reference Sub-Trigger C or Three-Stage Ephemeral Bug Isolator Swarm"))
            except Exception as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        op_rule = root_dir / "rules" / "operational-invariants.md"
        if not op_rule.is_file():
            errors.append(ValidationError(op_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "Missing required rule 'rules/operational-invariants.md'"))
        else:
            try:
                op_content = op_rule.read_text(encoding="utf-8")
                if "Sub-Trigger C" not in op_content and "Three-Stage Ephemeral Bug Isolator Swarm" not in op_content:
                    errors.append(ValidationError(op_rule, "TRACK34_SWARM_ORCHESTRATION_VIOLATION", "rules/operational-invariants.md must reference Sub-Trigger C or Three-Stage Ephemeral Bug Isolator Swarm"))
            except Exception as exc:
                errors.append(ValidationError(op_rule, "FILE_READ_ERROR", f"Could not read {op_rule.name}: {exc}"))

        invariants_scanned = 6
        return invariants_scanned, errors

    def validate_track_35_invariants(self, root_dir: Path | str) -> tuple[int, list[ValidationError]]:
        """
        Validates Track 35/39 Architecture Swarm, Subagent Brief Schemas, 
        and Downstream Phase Routing & Airgap Invariants (ADR 0039, [ROUT-01], [ROUT-02], [ERR-ROUT-01], [AIRGAP-03]):
        1. skills/improve-codebase-architecture/SKILL.md:
           - Documents Three-Stage Ephemeral Swarm ([SWARM-01], [SWARM-03], [SWARM-04]).
           - Documents 4-Tier Double-Verification Gate ([GATE-01], [GATE-02]).
           - Documents Downstream Phase Boundary Routing ([ROUT-01]) with /to-spec --delta, /to-spec, /to-tickets, and /implement.
           - Documents Recommend-and-Stop & Airgap Preservation ([ROUT-02], [ERR-ROUT-01], [AIRGAP-03]).
           - Documents Domain Glossary Fallback ([EDGE-ROUT-01]) and Gate Clearance Pre-Condition ([ERR-ROUT-02]).
        2. rules/subagent-orchestration.md:
           - Declares Trigger E specifying Three-Stage Ephemeral Architecture Swarm Pipeline.
           - Specifies Subagent Critique Brief schema (<role_and_objective>, <context_pointers>, <verification_gate>, <strict_guardrails>, <completion_protocol>).
           - Specifies three vulnerability axes (Edge-Case Failure Modes, Call-Site Cognitive Friction, Leaky Abstractions).
           - Specifies Stage 4 Double-Verification Gate (scripts/internal/verify_architecture_workspace.py).
           - Specifies Stage 5 Downstream Phase Routing ([ROUT-01]) and Recommend-and-Stop / Airgap Preservation ([ROUT-02], [ERR-ROUT-01], [AIRGAP-03]).
        3. rules/AGENTS.md & rules/operational-invariants.md:
           - References Trigger E, Downstream Phase Routing ([ROUT-01]), and Recommend-and-Stop ([ROUT-02]).
        """
        root_dir = Path(root_dir)
        errors: list[ValidationError] = []

        arch_skill = root_dir / "skills" / "improve-codebase-architecture" / "SKILL.md"
        if not arch_skill.is_file():
            errors.append(ValidationError(arch_skill, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", "Missing required skill 'skills/improve-codebase-architecture/SKILL.md'"))
        else:
            try:
                skill_content = arch_skill.read_text(encoding="utf-8")
                if "Three-Stage" not in skill_content and "3-Stage" not in skill_content:
                    errors.append(ValidationError(arch_skill, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", "skills/improve-codebase-architecture/SKILL.md must document Three-Stage Swarm"))
                if "[GATE-01]" not in skill_content:
                    errors.append(ValidationError(arch_skill, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", "skills/improve-codebase-architecture/SKILL.md must document Double-Verification Gate [GATE-01]"))
                if "[ROUT-01]" not in skill_content:
                    errors.append(ValidationError(arch_skill, "TRACK35_PHASE_ROUTING_VIOLATION", "skills/improve-codebase-architecture/SKILL.md must document Phase Boundary Routing [ROUT-01]"))
                if "/to-spec --delta" not in skill_content:
                    errors.append(ValidationError(arch_skill, "TRACK35_PHASE_ROUTING_VIOLATION", "skills/improve-codebase-architecture/SKILL.md must document /to-spec --delta"))
                if "[ROUT-02]" not in skill_content:
                    errors.append(ValidationError(arch_skill, "TRACK35_AIRGAP_VIOLATION", "skills/improve-codebase-architecture/SKILL.md must document Recommend-and-Stop [ROUT-02]"))
                if "[AIRGAP-03]" not in skill_content and "[ERR-ROUT-01]" not in skill_content:
                    errors.append(ValidationError(arch_skill, "TRACK35_AIRGAP_VIOLATION", "skills/improve-codebase-architecture/SKILL.md must enforce airgap against unauthorized traversal"))
                if "[EDGE-ROUT-01]" not in skill_content:
                    errors.append(ValidationError(arch_skill, "TRACK35_PHASE_ROUTING_VIOLATION", "skills/improve-codebase-architecture/SKILL.md must document unclassified domain fallback [EDGE-ROUT-01]"))
                if "[ERR-ROUT-02]" not in skill_content:
                    errors.append(ValidationError(arch_skill, "TRACK35_PHASE_ROUTING_VIOLATION", "skills/improve-codebase-architecture/SKILL.md must document gate pre-condition check [ERR-ROUT-02]"))
            except Exception as exc:
                errors.append(ValidationError(arch_skill, "FILE_READ_ERROR", f"Could not read {arch_skill.name}: {exc}"))

        orch_rule = root_dir / "rules" / "subagent-orchestration.md"
        if not orch_rule.is_file():
            errors.append(ValidationError(orch_rule, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", "Missing required rule 'rules/subagent-orchestration.md'"))
        else:
            try:
                orch_content = self.read_rule_content(orch_rule)
                match = re.search(r"###\s+Trigger\s+E:.*?(?=###\s+Trigger|\Z)", orch_content, re.DOTALL)
                if not match:
                    errors.append(ValidationError(orch_rule, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", "rules/subagent-orchestration.md must declare Trigger E"))
                else:
                    trigger_e = match.group(0)
                    if "Three-Stage Ephemeral Architecture Swarm" not in trigger_e and "Three-Stage" not in trigger_e:
                        errors.append(ValidationError(orch_rule, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", "rules/subagent-orchestration.md Trigger E must specify Three-Stage Architecture Swarm"))
                    for tag in ("<role_and_objective>", "<context_pointers>", "<verification_gate>", "<strict_guardrails>", "<completion_protocol>"):
                        if tag not in trigger_e:
                            errors.append(ValidationError(orch_rule, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", f"rules/subagent-orchestration.md Trigger E must define brief tag '{tag}'"))
                    for axis in ("Edge-Case", "Call-Site", "Leaky Abstraction"):
                        if axis.lower() not in trigger_e.lower():
                            errors.append(ValidationError(orch_rule, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", f"rules/subagent-orchestration.md Trigger E must define vulnerability axis '{axis}'"))
                    if "[ROUT-01]" not in trigger_e:
                        errors.append(ValidationError(orch_rule, "TRACK35_PHASE_ROUTING_VIOLATION", "rules/subagent-orchestration.md Trigger E must document Phase Boundary Routing [ROUT-01]"))
                    if "[ROUT-02]" not in trigger_e:
                        errors.append(ValidationError(orch_rule, "TRACK35_AIRGAP_VIOLATION", "rules/subagent-orchestration.md Trigger E must document Recommend-and-Stop [ROUT-02]"))
            except Exception as exc:
                errors.append(ValidationError(orch_rule, "FILE_READ_ERROR", f"Could not read {orch_rule.name}: {exc}"))

        agents_rule = root_dir / "rules" / "AGENTS.md"
        if not agents_rule.is_file():
            errors.append(ValidationError(agents_rule, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", "Missing required rule 'rules/AGENTS.md'"))
        else:
            try:
                agents_content = agents_rule.read_text(encoding="utf-8")
                if "Trigger E" not in agents_content:
                    errors.append(ValidationError(agents_rule, "TRACK35_ARCHITECTURE_SWARM_VIOLATION", "rules/AGENTS.md must reference Trigger E"))
            except Exception as exc:
                errors.append(ValidationError(agents_rule, "FILE_READ_ERROR", f"Could not read {agents_rule.name}: {exc}"))

        op_rule = root_dir / "rules" / "operational-invariants.md"
        if not op_rule.is_file():
            errors.append(ValidationError(op_rule, "TRACK35_AIRGAP_VIOLATION", "Missing required rule 'rules/operational-invariants.md'"))
        else:
            try:
                op_content = op_rule.read_text(encoding="utf-8")
                if "[ROUT-01]" not in op_content and "Phase Routing" not in op_content and "Trigger E" not in op_content:
                    errors.append(ValidationError(op_rule, "TRACK35_AIRGAP_VIOLATION", "rules/operational-invariants.md must reference Phase Routing or Trigger E"))
            except Exception as exc:
                errors.append(ValidationError(op_rule, "FILE_READ_ERROR", f"Could not read {op_rule.name}: {exc}"))

        invariants_scanned = 7
        return invariants_scanned, errors

    def check_skill_collisions(
        self,
        skills_dir: Path,
        warn_threshold: float = 0.50,
        error_threshold: float = 0.75,
    ) -> tuple[int, list[ValidationError]]:
        """
        Calculates pairwise cosine similarity across all skill descriptions in skills_dir.
        Checks all N*(N-1)//2 comparisons (300 pairs for 25 skills) and asserts 0 warnings and 0 errors ([EVAL-01]).
        """
        errors: list[ValidationError] = []
        if verify_routing is None:
            return 0, [ValidationError(skills_dir, "ROUTING_UNAVAILABLE", "Routing evaluation engine 'verify_routing' is not available")]

        if not skills_dir.exists():
            return 0, [ValidationError(skills_dir, "SKILLS_DIR_NOT_FOUND", f"Skills directory not found: {skills_dir}")]

        if not skills_dir.is_dir():
            return 0, [ValidationError(skills_dir, "SKILLS_DIR_NOT_A_DIRECTORY", f"Skills path is not a directory: {skills_dir}")]

        if not (0.0 <= warn_threshold <= error_threshold <= 1.0):
            return 0, [
                ValidationError(
                    skills_dir,
                    "INVALID_THRESHOLD",
                    f"Invalid collision thresholds: warn_threshold ({warn_threshold}) must be <= error_threshold ({error_threshold}) within [0.0, 1.0]",
                )
            ]

        try:
            skills = verify_routing.load_skills(skills_dir)
        except Exception as exc:
            return 0, [ValidationError(skills_dir, "SKILL_LOAD_ERROR", f"Failed to load skills for collision check: {exc}")]

        n = len(skills)
        total_pairs = n * (n - 1) // 2
        if total_pairs == 0:
            return 0, []

        matrix = verify_routing.CollisionMatrix(skills, warn_threshold=warn_threshold, error_threshold=error_threshold)
        collisions = matrix.compute_collisions()

        for c in collisions:
            cat = "SKILL_COLLISION_ERROR" if c.severity == "ERROR" else "SKILL_COLLISION_WARNING"
            thresh = error_threshold if c.severity == "ERROR" else warn_threshold
            errors.append(
                ValidationError(
                    file_path=skills_dir / c.skill_a / "SKILL.md",
                    category=cat,
                    message=f"Collision detected between '{c.skill_a}' and '{c.skill_b}' (similarity {c.similarity:.4f} >= {thresh:.2f})",
                )
            )

        return total_pairs, errors

    def validate_polyglot_subagent_verification_invariants(
        self,
        root_dir: Path | str,
    ) -> tuple[int, list[ValidationError]]:
        """
        Validates Polyglot Subagent Verification Invariant ([POLY-01], [ERR-POLY-01]):
        Audits agents/spec-implementer.md, agents/bug-diagnoser.md, and agents/code-reviewer.md
        to guarantee ticket-first Verification: priority, 4-tier polyglot verification cascade
        fallback, cross-language blind type-suppression bans, and absence of unconditional
        TypeScript-only compiler mandates.
        """
        errors: list[ValidationError] = []
        invariants_scanned = 6
        root = Path(root_dir)
        agents_dir = root / "agents"

        target_agents = (
            "spec-implementer.md",
            "bug-diagnoser.md",
            "code-reviewer.md",
        )
        contents: dict[str, str] = {}

        for agent_name in target_agents:
            agent_file = agents_dir / agent_name
            if not agent_file.is_file():
                errors.append(
                    ValidationError(
                        agent_file,
                        "POLYGLOT_VERIFICATION_VIOLATION",
                        f"Polyglot Verification Invariant: missing required subagent 'agents/{agent_name}'",
                    )
                )
                continue
            try:
                contents[agent_name] = agent_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(
                    ValidationError(
                        agent_file,
                        "FILE_READ_ERROR",
                        f"Polyglot Verification Invariant: could not read {agent_name}: {exc}",
                    )
                )

        legacy_ts_only_patterns: tuple[tuple[str, str], ...] = (
            (
                r"(?i)always\s+ensure\s+`?tsc\s+--noEmit`?\s+passes\s+with\s+0\s+errors",
                "unconditional 'Always ensure tsc --noEmit passes with 0 errors' mandate",
            ),
            (
                r"(?i)run\s+`?tsc\s+--noEmit`?\s+repeatedly\s+during\s+editing",
                "unconditional 'Run tsc --noEmit repeatedly during editing' mandate",
            ),
            (
                r"(?i)run\s+`?tsc\s+--noEmit`?\s+to\s+verify\s+zero\s+type\s+regressions",
                "unconditional 'Run tsc --noEmit to verify zero type regressions' mandate",
            ),
            (
                r"(?i)demand\s+non-negotiable\s+verification\s+via\s+`?tsc\s+--noEmit`?\s+and\s+focused\s+tests",
                "unconditional 'Demand non-negotiable verification via tsc --noEmit' mandate",
            ),
            (
                r"(?i)compiler\s+checks\s*\(\s*`?tsc\s+--noEmit`?\s+or\s+`?\./scripts/verify-build\.sh`?\s*\)",
                "incomplete two-command compiler check omitting ticket Verification: and 4-tier polyglot cascade",
            ),
            (
                r"(?m)^\s*npm\s+run\s+check\s*\|\|\s*\(\s*tsc\s+--noEmit\s*&&\s*npm\s+test\s*\)\s*$",
                "hardcoded npm/tsc-only pre-completion command block",
            ),
            (
                r"(?m)^\s*npm\s+test\s+--\s+<test-file>\s+-t\s+\"<scenario-tag>\"\s+--bail\s+1\s*\|\|\s*pytest\s+<test-file>\s+-k\s+\"<scenario-tag>\"\s+--maxfail=1\s*$",
                "Red-phase runner command omitting ticket Verification: and stdlib unittest fallback",
            ),
        )

        cascade_tokens = (
            "Verification:",
            "./scripts/verify-build.sh",
            "./scripts/verify-plugin-integrity.sh",
            "tsc --noEmit",
            "[TYPE-01]",
            "python3 -m mypy",
            "cargo check",
            "go vet",
        )
        polyglot_line_qualifiers = (
            "verification:",
            "tsconfig.json",
            "typescript",
            "verify-build.sh",
            "verify-plugin-integrity.sh",
            "mypy",
            "cargo",
            "polyglot",
        )

        for agent_name, text in contents.items():
            agent_file = agents_dir / agent_name

            for pattern, reason in legacy_ts_only_patterns:
                if re.search(pattern, text):
                    errors.append(
                        ValidationError(
                            agent_file,
                            "POLYGLOT_VERIFICATION_VIOLATION",
                            f"Polyglot Verification Invariant violated in agents/{agent_name}: detected {reason}",
                        )
                    )

            body_start_idx = 0
            lines = text.splitlines()
            if lines and lines[0].strip() == "---":
                for idx in range(1, len(lines)):
                    if lines[idx].strip() == "---":
                        body_start_idx = idx + 1
                        break

            for line_no, raw_line in enumerate(lines[body_start_idx:], start=body_start_idx + 1):
                if "tsc --noEmit" in raw_line:
                    lowered = raw_line.lower()
                    if not any(q in lowered for q in polyglot_line_qualifiers):
                        errors.append(
                            ValidationError(
                                agent_file,
                                "POLYGLOT_VERIFICATION_VIOLATION",
                                f"Polyglot Verification Invariant violated in agents/{agent_name}: unconditional 'tsc --noEmit' without ticket Verification: or polyglot cascade scoping",
                                line=line_no,
                            )
                        )

            for token in cascade_tokens:
                if token not in text:
                    errors.append(
                        ValidationError(
                            agent_file,
                            "POLYGLOT_VERIFICATION_VIOLATION",
                            f"Polyglot Verification Invariant violated in agents/{agent_name}: missing required cascade reference '{token}'",
                        )
                    )

            if "python3 -m unittest" not in text and "pytest" not in text:
                errors.append(
                    ValidationError(
                        agent_file,
                        "POLYGLOT_VERIFICATION_VIOLATION",
                        f"Polyglot Verification Invariant violated in agents/{agent_name}: missing Python test runner reference ('python3 -m unittest' or 'pytest')",
                    )
                )

        suppression_tokens = (
            "any",
            "@ts-ignore",
            "@ts-expect-error",
            "@ts-nocheck",
            "# type: ignore",
            "#[allow(",
            "//nolint",
        )
        for sup_agent in ("spec-implementer.md", "code-reviewer.md"):
            if sup_agent not in contents:
                continue
            sup_text = contents[sup_agent]
            sup_file = agents_dir / sup_agent
            for token in suppression_tokens:
                if token not in sup_text:
                    errors.append(
                        ValidationError(
                            sup_file,
                            "POLYGLOT_VERIFICATION_VIOLATION",
                            f"Polyglot Verification Invariant violated in agents/{sup_agent}: must prohibit cross-language blind type-suppression token '{token}'",
                        )
                    )

        if "spec-implementer.md" in contents:
            impl_text = contents["spec-implementer.md"]
            impl_file = agents_dir / "spec-implementer.md"
            if "## Core Operational Invariants" not in impl_text:
                errors.append(
                    ValidationError(
                        impl_file,
                        "POLYGLOT_VERIFICATION_VIOLATION",
                        "Polyglot Verification Invariant violated in agents/spec-implementer.md: missing '## Core Operational Invariants' seam heading",
                    )
                )
            for heading_pattern, label in (
                (r"## Core Operational Invariants\b(.*?)(?=\n## |\Z)", "Core Operational Invariants"),
                (r"### Step 3: Vertical Slice TDD Loop\b(.*?)(?=\n### |\Z)", "Step 3: Vertical Slice TDD Loop"),
                (r"### Step 4: Pre-Completion Sync & Verification\b(.*?)(?=\n### |\Z)", "Step 4: Pre-Completion Sync & Verification"),
            ):
                sec_match = re.search(heading_pattern, impl_text, re.DOTALL)
                if not sec_match or "Verification:" not in sec_match.group(1) or "./scripts/verify-plugin-integrity.sh" not in sec_match.group(1):
                    errors.append(
                        ValidationError(
                            impl_file,
                            "POLYGLOT_VERIFICATION_VIOLATION",
                            f"Polyglot Verification Invariant violated in agents/spec-implementer.md: section '{label}' must prioritize ticket Verification: and reference the 4-tier polyglot cascade",
                        )
                    )

        if "bug-diagnoser.md" in contents:
            bug_text = contents["bug-diagnoser.md"]
            bug_file = agents_dir / "bug-diagnoser.md"
            phase4_match = re.search(r"## Phase 4: Regression Lock & Handoff\b(.*?)(?=\n## |\Z)", bug_text, re.DOTALL)
            if not phase4_match or "Verification:" not in phase4_match.group(1) or "./scripts/verify-plugin-integrity.sh" not in phase4_match.group(1):
                errors.append(
                    ValidationError(
                        bug_file,
                        "POLYGLOT_VERIFICATION_VIOLATION",
                        "Polyglot Verification Invariant violated in agents/bug-diagnoser.md: Phase 4 must prioritize ticket Verification: and reference the 4-tier polyglot cascade",
                    )
                )

        if "code-reviewer.md" in contents:
            rev_text = contents["code-reviewer.md"]
            rev_file = agents_dir / "code-reviewer.md"
            for heading_pattern, label in (
                (r"## Axis A: Standards Reviewer\b(.*?)(?=\n## |\Z)", "Axis A: Standards Reviewer"),
                (r"## Active Improvement Worker Mode\b(.*?)(?=\n## |\Z)", "Active Improvement Worker Mode"),
            ):
                sec_match = re.search(heading_pattern, rev_text, re.DOTALL)
                if not sec_match or "Verification:" not in sec_match.group(1) or "./scripts/verify-plugin-integrity.sh" not in sec_match.group(1):
                    errors.append(
                        ValidationError(
                            rev_file,
                            "POLYGLOT_VERIFICATION_VIOLATION",
                            f"Polyglot Verification Invariant violated in agents/code-reviewer.md: section '{label}' must prioritize ticket Verification: and reference the 4-tier polyglot cascade",
                        )
                    )

        return invariants_scanned, errors

    def validate_installer_asset_parity(
        self,
        root_dir: Path | str,
    ) -> tuple[int, list[ValidationError]]:
        """
        [PARITY-02] [ERR-PARITY-01] Validates that install.sh, scripts/install.sh,
        scripts/enable-in-project.sh, and scripts/package-bundle.sh declare the canonical
        11-asset bundle, purge bytecode caches, and enforce executable bits.
        """
        errors: list[ValidationError] = []
        root = Path(root_dir)
        scanned = 0

        for channel in self.INSTALLER_CHANNELS:
            scanned += 1
            script_path = root / channel
            if not script_path.exists():
                errors.append(
                    ValidationError(
                        script_path,
                        "INSTALLER_NOT_FOUND",
                        f"Installer distribution script '{channel}' not found",
                    )
                )
                continue

            try:
                content = script_path.read_text(encoding="utf-8")
            except Exception as exc:
                errors.append(
                    ValidationError(
                        script_path,
                        "FILE_READ_ERROR",
                        f"Could not read installer script '{channel}': {exc}",
                    )
                )
                continue

            match = re.search(r"RUNTIME_ASSETS=\s*\((.*?)\)", content, re.DOTALL)
            if not match:
                errors.append(
                    ValidationError(
                        script_path,
                        "INSTALLER_PARITY_VIOLATION",
                        f"Installer distribution script '{channel}' does not declare RUNTIME_ASSETS array",
                    )
                )
            else:
                raw_assets = match.group(1)
                declared_assets = [
                    item.strip("\"'")
                    for item in raw_assets.split()
                    if item.strip("\"'") and not item.strip().startswith("#")
                ]
                missing = [a for a in self.CANONICAL_RUNTIME_ASSETS if a not in declared_assets]
                extraneous = [a for a in declared_assets if a not in self.CANONICAL_RUNTIME_ASSETS]

                if missing:
                    errors.append(
                        ValidationError(
                            script_path,
                            "INSTALLER_PARITY_VIOLATION",
                            f"Installer distribution script '{channel}' has drifted from canonical 11-asset bundle: missing required runtime asset(s): {', '.join(sorted(missing))}",
                        )
                    )
                if extraneous:
                    errors.append(
                        ValidationError(
                            script_path,
                            "INSTALLER_PARITY_VIOLATION",
                            f"Installer distribution script '{channel}' declares extraneous or unapproved authoring asset(s): {', '.join(sorted(extraneous))}",
                        )
                    )

            has_pycache_purge = "__pycache__" in content
            has_pyc_purge = "*.pyc" in content or r"\.pyc" in content
            if not (has_pycache_purge and has_pyc_purge):
                errors.append(
                    ValidationError(
                        script_path,
                        "INSTALLER_PARITY_VIOLATION",
                        f"Installer distribution script '{channel}' is missing bytecode cache purging (__pycache__ or *.pyc)",
                    )
                )

            has_scripts_chmod = bool(re.search(r"chmod\s+(?:-[a-zA-Z]+\s+)*\+x\s+.*scripts", content))
            has_hooks_chmod = bool(re.search(r"chmod\s+(?:-[a-zA-Z]+\s+)*\+x\s+.*hooks", content))
            if not has_scripts_chmod:
                errors.append(
                    ValidationError(
                        script_path,
                        "INSTALLER_PARITY_VIOLATION",
                        f"Installer distribution script '{channel}' is missing executable permission (+x) enforcement on scripts/",
                    )
                )
            if not has_hooks_chmod:
                errors.append(
                    ValidationError(
                        script_path,
                        "INSTALLER_PARITY_VIOLATION",
                        f"Installer distribution script '{channel}' is missing executable permission (+x) enforcement on hooks/",
                    )
                )

        return scanned, errors

    def validate_downstream_manifest_sync(
        self,
        root_dir: Path,
        templates_dir: Path | None = None,
        agents_dir: Path | None = None,
    ) -> tuple[int, list[ValidationError]]:
        """
        Validates downstream template and manifest synchronization ([SYNC-01]).
        Ensures all plugin include declarations and manifest references uniformly
        use 'agy-matt' and reject any legacy 'agy-pocock' remnants across templates/
        and .agents/ declarative configurations.
        """
        errors: list[ValidationError] = []
        scanned_count = 0

        t_dir = templates_dir if templates_dir is not None else (root_dir / "templates")
        a_dir = agents_dir if agents_dir is not None else (root_dir / ".agents")

        if templates_dir is not None and not t_dir.exists():
            errors.append(ValidationError(t_dir, "DIRECTORY_NOT_FOUND", f"Explicit templates directory not found: {t_dir}"))
            return 0, errors

        if agents_dir is not None and not a_dir.exists():
            errors.append(ValidationError(a_dir, "DIRECTORY_NOT_FOUND", f"Explicit agents directory not found: {a_dir}"))
            return 0, errors

        plugins_json_path = t_dir / "plugins.json"
        if plugins_json_path.exists():
            scanned_count += 1
            try:
                content = plugins_json_path.read_text(encoding="utf-8")
                if "agy-pocock" in content.lower():
                    errors.append(
                        ValidationError(
                            plugins_json_path,
                            "MANIFEST_SYNC_VIOLATION",
                            "Legacy plugin identifier 'agy-pocock' detected in templates/plugins.json; must be 'agy-matt'",
                        )
                    )
                data = json.loads(content)
                if isinstance(data, dict):
                    entries = data.get("entries")
                    if isinstance(entries, list):
                        has_agy_matt = False
                        for entry in entries:
                            if isinstance(entry, dict):
                                include_only = entry.get("include_only")
                                if isinstance(include_only, list):
                                    if "agy-matt" in include_only:
                                        has_agy_matt = True
                                    if any(isinstance(p, str) and "agy-pocock" in p.lower() for p in include_only):
                                        errors.append(
                                            ValidationError(
                                                plugins_json_path,
                                                "MANIFEST_SYNC_VIOLATION",
                                                "Legacy plugin identifier 'agy-pocock' declared in 'include_only'",
                                            )
                                        )
                        if not has_agy_matt:
                            errors.append(
                                ValidationError(
                                    plugins_json_path,
                                    "MANIFEST_SYNC_VIOLATION",
                                    "Missing canonical plugin identifier 'agy-matt' in templates/plugins.json 'include_only'",
                                )
                            )
                    else:
                        errors.append(
                            ValidationError(
                                plugins_json_path,
                                "MANIFEST_SYNC_VIOLATION",
                                "Missing or invalid 'entries' list in templates/plugins.json",
                            )
                        )
                else:
                    errors.append(
                        ValidationError(
                            plugins_json_path,
                            "MANIFEST_SYNC_VIOLATION",
                            "Invalid root format in templates/plugins.json: expected JSON object",
                        )
                    )
            except Exception as exc:
                errors.append(
                    ValidationError(
                        plugins_json_path,
                        "MANIFEST_SYNC_VIOLATION",
                        f"Could not parse templates/plugins.json: {exc}",
                    )
                )

        if t_dir.exists():
            for t_file in sorted(t_dir.rglob("*")):
                if t_file.is_file() and t_file.suffix in (".json", ".md", ".ts", ".html", ".css", ".yaml", ".yml"):
                    if t_file.name == "plugins.json":
                        continue
                    scanned_count += 1
                    try:
                        content = t_file.read_text(encoding="utf-8")
                        if "agy-pocock" in content.lower():
                            errors.append(
                                ValidationError(
                                    t_file,
                                    "LEGACY_IDENTIFIER_DETECTED",
                                    f"Legacy identifier 'agy-pocock' detected in template '{t_file.name}'; must reference canonical 'agy-matt'",
                                )
                            )
                    except Exception as exc:
                        errors.append(
                            ValidationError(t_file, "FILE_READ_ERROR", f"Could not read template file: {exc}")
                        )

        if a_dir.exists():
            plugins_dir = a_dir / "plugins"
            if plugins_dir.exists() and plugins_dir.is_dir():
                for item in sorted(plugins_dir.iterdir()):
                    scanned_count += 1
                    if item.name.lower() == "agy-pocock":
                        errors.append(
                            ValidationError(
                                item,
                                "MANIFEST_SYNC_VIOLATION",
                                "Legacy plugin directory/symlink '.agents/plugins/agy-pocock' detected; must be 'agy-matt'",
                            )
                        )
                    if item.is_symlink():
                        try:
                            target_str = str(item.readlink())
                            if "agy-pocock" in target_str.lower():
                                errors.append(
                                    ValidationError(
                                        item,
                                        "MANIFEST_SYNC_VIOLATION",
                                        f"Symlink '{item.name}' points to legacy target containing 'agy-pocock' ({target_str}); must target canonical 'agy-matt'",
                                    )
                                )
                        except OSError as exc:
                            errors.append(
                                ValidationError(item, "FILE_READ_ERROR", f"Could not read symlink target for '{item.name}': {exc}")
                            )

            agents_plugins_json = a_dir / "plugins.json"
            if agents_plugins_json.exists():
                scanned_count += 1
                try:
                    content = agents_plugins_json.read_text(encoding="utf-8")
                    if "agy-pocock" in content.lower():
                        errors.append(
                            ValidationError(
                                agents_plugins_json,
                                "MANIFEST_SYNC_VIOLATION",
                                "Legacy plugin identifier 'agy-pocock' detected in .agents/plugins.json; must be 'agy-matt'",
                            )
                        )
                    data = json.loads(content)
                    if isinstance(data, dict):
                        entries = data.get("entries")
                        if isinstance(entries, list):
                            has_agy_matt = False
                            for entry in entries:
                                if isinstance(entry, dict):
                                    include_only = entry.get("include_only")
                                    if isinstance(include_only, list):
                                        if "agy-matt" in include_only:
                                            has_agy_matt = True
                                        if any(isinstance(p, str) and "agy-pocock" in p.lower() for p in include_only):
                                            errors.append(
                                                ValidationError(
                                                    agents_plugins_json,
                                                    "MANIFEST_SYNC_VIOLATION",
                                                    "Legacy plugin identifier 'agy-pocock' declared in .agents/plugins.json 'include_only'",
                                                )
                                            )
                            if not has_agy_matt:
                                errors.append(
                                    ValidationError(
                                        agents_plugins_json,
                                        "MANIFEST_SYNC_VIOLATION",
                                        "Missing canonical plugin identifier 'agy-matt' in .agents/plugins.json 'include_only'",
                                    )
                                )
                except Exception as exc:
                    errors.append(
                        ValidationError(agents_plugins_json, "MANIFEST_SYNC_VIOLATION", f"Could not parse .agents/plugins.json: {exc}")
                    )

            for cfg_file in sorted(a_dir.glob("*.json")):
                scanned_count += 1
                try:
                    content = cfg_file.read_text(encoding="utf-8")
                    if "agy-pocock" in content.lower():
                        errors.append(
                            ValidationError(
                                cfg_file,
                                "LEGACY_IDENTIFIER_DETECTED",
                                f"Legacy identifier 'agy-pocock' detected in active config '{cfg_file.name}'; must reference canonical 'agy-matt'",
                            )
                        )
                except Exception as exc:
                    errors.append(
                        ValidationError(cfg_file, "FILE_READ_ERROR", f"Could not read config file: {exc}")
                    )

        seen_wm: set[Path] = set()
        for wm_candidate in (root_dir / "workspace_manifest.txt", t_dir / "workspace_manifest.txt"):
            if wm_candidate.exists() and wm_candidate.resolve() not in seen_wm:
                seen_wm.add(wm_candidate.resolve())
                scanned_count += 1
                try:
                    content = wm_candidate.read_text(encoding="utf-8")
                    if "agy-pocock" in content.lower():
                        errors.append(
                            ValidationError(
                                wm_candidate,
                                "LEGACY_IDENTIFIER_DETECTED",
                                f"Legacy identifier 'agy-pocock' detected in '{wm_candidate.name}'",
                            )
                        )
                    if "agy-matt" not in content:
                        errors.append(
                            ValidationError(
                                wm_candidate,
                                "MANIFEST_SYNC_VIOLATION",
                                f"Missing canonical identifier 'agy-matt' in '{wm_candidate.name}'",
                            )
                        )
                except Exception as exc:
                    errors.append(
                        ValidationError(wm_candidate, "FILE_READ_ERROR", f"Could not read {wm_candidate.name}: {exc}")
                    )

        return scanned_count, errors

    validate_manifest_sync = validate_downstream_manifest_sync





SKIPPED_SCHEMES: tuple[str, ...] = (
    "http://",
    "https://",
    "mailto:",
    "customization://",
    "conversation://",
)

# Canonical workspace path used as fallback alias for documentation cross-references
CANONICAL_REPO_PREFIX: str = "/srv/agy-workspaces/booster/agy-matt"
LINK_PATTERN: re.Pattern = re.compile(
    r"[@!]?\[([^\]]*)\]\((<[^>]+>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)"
)


def slugify_heading(text: str) -> str:
    """GitHub-compatible markdown heading anchor generator."""
    cleaned = re.sub(r"`([^`]+)`", r"\1", text)
    cleaned = re.sub(r"[*_~]", "", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = cleaned.lower().strip()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned, flags=re.UNICODE)
    slug = re.sub(r"[\s_]+", "-", cleaned).strip("-")
    return slug


def validate_task_graph_template_contract(
    template_or_content: Path | str | None = None,
    *,
    root_dir: Path | str | None = None,
    validator: ManifestValidator | None = None,
) -> tuple[bool, list[str]]:
    """
    [TMPL-01] [EDGE-TMPL-PROMPT-FENCE] Functional entrypoint validating canonical task graph template contract.
    """
    v = validator or ManifestValidator()
    return v.validate_task_graph_template_contract(template_or_content, root_dir=root_dir)


DEFAULT_TASK_GRAPH_TEMPLATE: str = (
    "# 🧭 Task Graph: <Feature Title>\n\n"
    "> **Mission**: <Feature description / core objective>\n"
    "> **Current Stage**: Stage I — Inception & Architecture\n\n"
    "## Stage I: Inception & Architecture (Command Center)\n"
    "| # | Phase | Status | Command | Deliverable |\n"
    "| :-: | :--- | :---: | :--- | :--- |\n"
    "| 1 | Inception & Grilling | ✅ completed | `/grill-with-docs` | `GLOSSARY.md`, `docs/adr/` |\n"
    "| 2 | Living / Delta Spec | ✅ completed | `/to-spec` | `.scratch/<slug>/spec.md` |\n"
    "| 3 | Ticket Decomposition | ✅ completed | `/to-tickets` | `.scratch/<slug>/issues/*.md` |\n\n"
    "## Stage II: Implementation Swarm\n"
    "*Progress: 🟡 0 dari N tiket selesai*\n\n"
    "### 🎯 Active Frontier (Ready to Start)\n"
    "- [01: Core Architecture Slice](issues/01-core.md)\n\n"
    "### Status Progress Table\n"
    "| Ticket | Title | Blockers | Status | Verified Commit |\n"
    "| :--- | :--- | :--- | :--- | :--- |\n"
    "| [01](issues/01-core.md) | Core Architecture Slice | None (Frontier) | ready-for-agent | - |\n"
    "| [02](issues/02-dependent-engine.md) | Dependent Engine Slice | 01 | blocked | - |\n\n"
    "## Stage III: Verification & Delivery (Evaluation Pipeline)\n"
    "| # | Phase | Status | Command | Deliverable |\n"
    "| :-: | :--- | :---: | :--- | :--- |\n"
    "| 5 | Two-Axis Code Review | ⬜ pending | `/code-review` | `.scratch/<slug>/code-review.md` |\n"
    "| 6 | Semantic Security Audit (optional / attack-surface dependent) | ⬜ pending | `/security-audit` | `.scratch/<slug>/security-audit.md` |\n"
    "| 7 | Pull Request & Sync | ⬜ pending | `/pr` | Draft PR & Spec Reconciliation |\n\n"
    "## Stage IV: Retrospective\n"
    "| # | Phase | Status | Command | Deliverable |\n"
    "| :-: | :--- | :---: | :--- | :--- |\n"
    "| 8 | Retrospective | ⬜ pending | `/retro` | Automated Checks & Hooks |\n"
)


def load_task_graph_template(
    template_path: Path | str | None = None,
    *,
    root_dir: Path | str | None = None,
    validator: ManifestValidator | None = None,
) -> tuple[str, str | None]:
    """
    [ERR-TMPL-IO] Reads canonical task graph template from disk, trapping missing or unreadable file conditions.
    When skills/to-tickets/resources/task-graph-template.md is missing or unreadable, returns diagnostic:
      [ERR-TMPL-NOT-FOUND] Canonical task graph template missing at skills/to-tickets/resources/task-graph-template.md
    and gracefully falls back to built-in programmatic template strings without throwing unhandled exceptions.

    Returns:
        tuple[content: str, diagnostic: str | None]
    """
    v = validator or ManifestValidator()
    return v.load_task_graph_template(template_path=template_path, root_dir=root_dir)



class LinkIntegrityValidator:
    """
    Validates offline internal Markdown links and anchors across the plugin.
    Scans:
      - root markdown files (*.md)
      - skills/**/*.md
      - agents/**/*.md
      - rules/**/*.md
      - docs/**/*.md
    Resolves:
      - relative paths (e.g. ../grilling/SKILL.md)
      - workspace URIs (file:///srv/agy-workspaces/booster/agy-matt/...)
      - internal anchors (#header-slug, #L10-L20)
    Skips:
      - external web URLs (http://, https://)
      - mailto: links
      - platform-specific UI links (customization://, conversation://)
      - fenced code blocks (``` or ~~~) and inline code spans (`...`)
    """

    def __init__(self):
        self._anchor_cache: dict[Path, set[str]] = {}

    def extract_file_anchors(self, file_path: Path) -> set[str]:
        if file_path in self._anchor_cache:
            return self._anchor_cache[file_path]

        anchors: set[str] = set()
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            self._anchor_cache[file_path] = anchors
            return anchors

        lines = content.splitlines()
        in_fence = False
        slug_counts: dict[str, int] = {}

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            # Markdown headings: # Heading
            m = re.match(r"^#{1,6}\s+(.*)", stripped)
            if m:
                raw_title = m.group(1).strip()
                custom_id_match = re.search(r"\{#([^}]+)\}\s*$", raw_title)
                if custom_id_match:
                    anchors.add(custom_id_match.group(1))
                    raw_title = raw_title[:custom_id_match.start()].strip()

                base_slug = slugify_heading(raw_title)
                if base_slug:
                    count = slug_counts.get(base_slug, 0)
                    slug_counts[base_slug] = count + 1
                    if count == 0:
                        anchors.add(base_slug)
                    else:
                        anchors.add(f"{base_slug}-{count}")
                    # Also store collapsed hyphen version
                    collapsed = re.sub(r"-+", "-", base_slug)
                    anchors.add(collapsed)

            # Explicit HTML anchors: <a name="foo">, <a id="foo">, id="foo"
            for a_match in re.finditer(r"""(?:id|name)=["']([^"']+)["']""", line):
                anchors.add(a_match.group(1))

        self._anchor_cache[file_path] = anchors
        return anchors

    def is_inside_code_span(self, start: int, end: int, line: str) -> bool:
        for span in re.finditer(r"(`+)(.+?)\1", line):
            if span.start() <= start and end <= span.end():
                return True
        return False

    def validate_file(self, file_path: Path, root_dir: Path) -> tuple[int, list[ValidationError]]:
        errors: list[ValidationError] = []
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            return 0, [ValidationError(file_path, "FILE_READ_ERROR", f"Could not read markdown file: {exc}")]

        lines = content.splitlines()
        in_fence = False
        link_count = 0

        for line_no, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            for m in LINK_PATTERN.finditer(line):
                # Ignore links inside inline code spans
                if self.is_inside_code_span(m.start(), m.end(), line):
                    continue

                raw_target = m.group(2)
                if raw_target.startswith("<") and raw_target.endswith(">"):
                    raw_target = raw_target[1:-1]

                # Check skipped schemes
                if raw_target.startswith(SKIPPED_SCHEMES):
                    continue

                link_count += 1

                # Parse URL part vs Fragment
                if "#" in raw_target:
                    url_part, fragment = raw_target.split("#", 1)
                else:
                    url_part, fragment = raw_target, ""

                # Resolve target file path
                if url_part.startswith("file://"):
                    raw_path = unquote(url_part[7:])
                    resolved_root = root_dir.resolve().as_posix()
                    if raw_path.startswith(resolved_root):
                        rel = raw_path[len(resolved_root):].lstrip("/")
                        target_path = (root_dir / rel).resolve()
                    elif raw_path.startswith(CANONICAL_REPO_PREFIX):
                        # Dynamic fallback for cross-environment portability
                        rel = raw_path[len(CANONICAL_REPO_PREFIX):].lstrip("/")
                        target_path = (root_dir / rel).resolve()
                    else:
                        p = Path(raw_path)
                        if not p.is_absolute():
                            target_path = (root_dir / raw_path).resolve()
                        else:
                            target_path = p.resolve()
                elif url_part == "":
                    target_path = file_path.resolve()
                else:
                    target_path = (file_path.parent / unquote(url_part)).resolve()

                # 1. Assert file exists
                if not target_path.exists():
                    if (
                        file_path.name.endswith("-template.md") or "templates" in file_path.parts or "resources" in file_path.parts
                    ) and (
                        url_part.startswith("./issues/")
                        or url_part.startswith("issues/")
                        or url_part.startswith(".scratch/")
                        or "<slug>" in url_part
                        or "<feature-slug>" in url_part
                    ):
                        continue
                    errors.append(
                        ValidationError(
                            file_path,
                            "BROKEN_INTERNAL_LINK",
                            f"Target file does not exist: '{raw_target}' (resolved to '{target_path}')",
                            line=line_no,
                        )
                    )
                    continue

                # 2. Assert anchor exists (if specified)
                if fragment:
                    line_match = re.match(r"^L(\d+)(?:-L?(\d+))?$", fragment)
                    if line_match:
                        start_line = int(line_match.group(1))
                        try:
                            total_lines = len(target_path.read_text(encoding="utf-8", errors="replace").splitlines())
                        except Exception:
                            total_lines = 0
                        if start_line > total_lines:
                            errors.append(
                                ValidationError(
                                    file_path,
                                    "BROKEN_LINE_ANCHOR",
                                    f"Line anchor #{fragment} out of range (target {target_path.name} has {total_lines} lines)",
                                    line=line_no,
                                )
                            )
                    else:
                        anchors = self.extract_file_anchors(target_path)
                        frag_unquoted = unquote(fragment)
                        frag_slug = slugify_heading(frag_unquoted)
                        frag_collapsed = re.sub(r"-+", "-", frag_slug)
                        if (
                            fragment not in anchors
                            and frag_unquoted not in anchors
                            and frag_slug not in anchors
                            and frag_collapsed not in anchors
                            and fragment.lower() not in anchors
                        ):
                            errors.append(
                                ValidationError(
                                    file_path,
                                    "BROKEN_ANCHOR",
                                    f"Anchor #{fragment} not found in target file: '{target_path.name}'",
                                    line=line_no,
                                )
                            )

        return link_count, errors

    def discover_markdown_files(self, root_dir: Path) -> list[Path]:
        files: set[Path] = set()
        # Root .md files
        for p in root_dir.glob("*.md"):
            if p.is_file():
                files.add(p.resolve())
        # Target directories: skills, agents, rules, docs, templates, tests
        for subdir in ("skills", "agents", "rules", "docs", "templates", "tests"):
            target_dir = root_dir / subdir
            if target_dir.exists():
                for p in target_dir.rglob("*.md"):
                    if p.is_file():
                        files.add(p.resolve())
        return sorted(files)

    def validate_all(self, root_dir: Path) -> tuple[int, int, list[ValidationError]]:
        all_errors: list[ValidationError] = []
        files = self.discover_markdown_files(root_dir)
        total_links = 0

        for f in files:
            link_cnt, errs = self.validate_file(f, root_dir)
            total_links += link_cnt
            all_errors.extend(errs)

        return len(files), total_links, all_errors


class DiagnosticReporter:
    """Formats and prints colored, structured integrity reports."""

    def __init__(self, no_color: bool = False):
        self.no_color = no_color or not sys.stdout.isatty()
        self.GREEN = "" if self.no_color else "\033[92m"
        self.RED = "" if self.no_color else "\033[91m"
        self.YELLOW = "" if self.no_color else "\033[93m"
        self.CYAN = "" if self.no_color else "\033[96m"
        self.BOLD = "" if self.no_color else "\033[1m"
        self.RESET = "" if self.no_color else "\033[0m"

    def banner(self):
        print(f"{self.BOLD}{self.CYAN}=== Antigravity Plugin Integrity Validator ==={self.RESET}")

    def report_success(self, category: str, count: int):
        print(f"{self.GREEN}✓{self.RESET} Verified {count} {category} (100% conformant)")

    def report_errors(self, errors: list[ValidationError], repo_root: Path):
        print(f"\n{self.BOLD}{self.RED}Integrity Violations Detected ({len(errors)}):{self.RESET}")
        for err in errors:
            print(f"  {self.RED}✗{self.RESET} {err.format(repo_root)}")

    def report_summary(
        self,
        skill_count: int,
        agent_count: int,
        errors: list[ValidationError],
        manifest_count: int = 0,
        hooks_count: int = 0,
        doc_count: int = 0,
        link_count: int = 0,
        rules_count: int = 0,
        track10_count: int = 0,
        track12_count: int = 0,
        track13_count: int = 0,
        track14_count: int = 0,
    ):
        extra = []
        if manifest_count:
            extra.append(f"{manifest_count} manifest")
        if hooks_count:
            extra.append(f"{hooks_count} hooks")
        if rules_count:
            extra.append(f"{rules_count} rules")
        if track10_count:
            extra.append(f"{track10_count} track 10 invariants")
        if track12_count:
            extra.append(f"{track12_count} track 12 invariants")
        if track13_count:
            extra.append(f"{track13_count} track 13 invariants")
        if track14_count:
            extra.append(f"{track14_count} track 14 invariants")
        if doc_count:
            extra.append(f"{doc_count} docs ({link_count} links)")
        extra_str = f", {', '.join(extra)}" if extra else ""
        print(f"\n{self.BOLD}Summary:{self.RESET} {skill_count} skills, {agent_count} agents{extra_str} scanned.")
        if not errors:
            print(f"{self.BOLD}{self.GREEN}Status: CLEAN — All integrity invariants passed.{self.RESET}\n")
        else:
            print(f"{self.BOLD}{self.RED}Status: FAILED — {len(errors)} violation(s) found.{self.RESET}\n")


def run_integrity_check(
    root_dir: Path,
    verbose: bool = False,
    no_color: bool = False,
    check_collisions: bool = False,
) -> int:
    reporter = DiagnosticReporter(no_color=no_color)
    reporter.banner()

    all_errors: list[ValidationError] = []

    # 1. Frontmatter validation (skills & agents)
    frontmatter_validator = FrontmatterValidator()
    skill_count, agent_count, fm_errors = frontmatter_validator.validate_all(root_dir)
    all_errors.extend(fm_errors)

    if verbose or not fm_errors:
        reporter.report_success("skills frontmatter & strict naming invariants", skill_count)
        reporter.report_success("agents frontmatter, tools & naming invariants", agent_count)

    # 2. Manifest & hooks validation
    manifest_validator = ManifestValidator()
    manifest_path = root_dir / "plugin.json"
    skills_dir = root_dir / "skills"
    hooks_path = root_dir / "hooks.json"

    manifest_scanned = 0
    hooks_scanned = 0

    if manifest_path.exists():
        manifest_scanned = 1
        m_errors = manifest_validator.validate_manifest(manifest_path, skills_dir)
        all_errors.extend(m_errors)
        if verbose or not m_errors:
            reporter.report_success("plugin.json schema & suggestedPrompts synchronization", 1)
    else:
        all_errors.append(ValidationError(manifest_path, "MANIFEST_NOT_FOUND", "Missing required manifest 'plugin.json' at workspace root"))

    if hooks_path.exists():
        hooks_scanned = 1
        h_errors = manifest_validator.validate_hooks(hooks_path)
        all_errors.extend(h_errors)
        if verbose or not h_errors:
            reporter.report_success("hooks.json schema & lifecycle command declarations", 1)

    # 3. Declarative templates & quality gate configuration validation
    templates_dir = root_dir / "templates"
    templates_scanned = 0
    if templates_dir.exists():
        templates_scanned, t_errors = manifest_validator.validate_templates(templates_dir, check_core_assets=True, root_dir=root_dir)
        all_errors.extend(t_errors)
        if verbose or not t_errors:
            reporter.report_success("templates declarative configurations, UI assets & presets", templates_scanned)

    # 3b. Ticket templates execution contract metadata validation
    ticket_templates_dir = skills_dir / "to-tickets" / "resources"
    tt_scanned, tt_errors = manifest_validator.validate_ticket_templates(ticket_templates_dir)
    all_errors.extend(tt_errors)
    if verbose or not tt_errors:
        reporter.report_success("ticket templates execution contract metadata", tt_scanned)

    # 3b2. Canonical task graph template contract schema integrity ([TMPL-01], [ERR-TMPL-CORRUPT], [EDGE-TMPL-PROMPT-FENCE])
    tmpl_ok, tmpl_errors = manifest_validator.validate_task_graph_template_contract(root_dir=root_dir)
    if not tmpl_ok:
        tmpl_file = root_dir / "skills" / "to-tickets" / "resources" / "task-graph-template.md"
        for err in tmpl_errors:
            all_errors.append(ValidationError(tmpl_file, "TASK_GRAPH_TEMPLATE_CONTRACT_VIOLATION", err))
    if verbose or tmpl_ok:
        reporter.report_success("task graph cockpit template contract & subgraph schema", 1)

    project_gate_path = root_dir / ".agents" / "matt-gate.json"
    if project_gate_path.exists():
        p_gate_errors = manifest_validator.validate_gate_config(project_gate_path)
        all_errors.extend(p_gate_errors)
        if verbose or not p_gate_errors:
            reporter.report_success(".agents/matt-gate.json project declarative override", 1)

    # 3c. Rules architecture & Master Composer inclusion validation
    rules_dir = root_dir / "rules"
    rules_scanned, rules_errors = manifest_validator.validate_rules(rules_dir)
    all_errors.extend(rules_errors)
    if verbose or not rules_errors:
        reporter.report_success("rules architecture & master composer inclusion", rules_scanned)

    rules_size, size_errors = manifest_validator.validate_rule_size_limit(rules_dir)
    all_errors.extend(size_errors)
    if verbose or not size_errors:
        reporter.report_success("rules expanded size limit (<= 20,000 bytes)", 1)

    # 3d. Track 10 behavioral decision gates & adversarial invariants validation
    track10_scanned, track10_errors = manifest_validator.validate_track_10_invariants(root_dir)
    all_errors.extend(track10_errors)
    if verbose or not track10_errors:
        reporter.report_success("Track 10 behavioral decision gates & adversarial invariants", track10_scanned)

    # 3e. Living specification engine, workflow schemas & spec templates validation (Track 12)
    spec_scanned, spec_errors = manifest_validator.validate_spec_engine_and_schemas(root_dir)
    all_errors.extend(spec_errors)
    if verbose or not spec_errors:
        reporter.report_success("spec engine, workflow schemas & specification templates", spec_scanned)

    # 3e-2. Research note validator engine & CLI integrity ([CLI-02])
    res_engine_scanned, res_engine_errors = manifest_validator.validate_research_engine(root_dir)
    all_errors.extend(res_engine_errors)
    if verbose or not res_engine_errors:
        reporter.report_success("research note validator engine & CLI integrity ([CLI-02])", res_engine_scanned)


    # 3f. Track 12 living specifications & anti-scenario-loss invariants validation
    track12_scanned, track12_errors = manifest_validator.validate_track_12_invariants(root_dir)
    all_errors.extend(track12_errors)
    if verbose or not track12_errors:
        reporter.report_success("Track 12 living specifications & anti-scenario-loss invariants", track12_scanned)

    # 3g. Track 13 single-ticket micro-swarm invariants validation
    track13_scanned, track13_errors = manifest_validator.validate_track_13_invariants(root_dir)
    all_errors.extend(track13_errors)
    if verbose or not track13_errors:
        reporter.report_success("Track 13 single-ticket micro-swarm invariants", track13_scanned)

    # 3h. Track 14 ticket-centric metadata & lean flow ergonomics validation
    track14_scanned, track14_errors = manifest_validator.validate_track_14_invariants(root_dir)
    all_errors.extend(track14_errors)
    if verbose or not track14_errors:
        reporter.report_success("Track 14 ticket-centric metadata & lean flow ergonomics invariants", track14_scanned)

    # 3i. Downstream manifest & template synchronization (Track 15 [SYNC-01])
    sync_scanned, sync_errors = manifest_validator.validate_downstream_manifest_sync(root_dir)
    all_errors.extend(sync_errors)
    if verbose or not sync_errors:
        reporter.report_success("downstream manifest & template synchronization (agy-matt canonical)", sync_scanned)

    # 3j. Hierarchical worker delegation & context hygiene validation (Track 16)
    hier_scanned, hier_errors = manifest_validator.validate_hierarchical_worker_invariants(root_dir)
    all_errors.extend(hier_errors)
    if verbose or not hier_errors:
        reporter.report_success("hierarchical subagent orchestration & worker context hygiene", hier_scanned)

    # 3k. Adaptive Red-Green micro-worker pipeline & complexity assessment (Track 16 [ORCH-01]-[ORCH-03])
    adaptive_scanned, adaptive_errors = manifest_validator.validate_adaptive_pipeline_invariants(root_dir)
    all_errors.extend(adaptive_errors)
    if verbose or not adaptive_errors:
        reporter.report_success("adaptive Red-Green micro-worker pipeline & complexity assessment", adaptive_scanned)

    # 3l. Orchestration rules & phase boundary specification routing (Track 16 [ROUT-01], Sub-Trigger A3)
    orch_scanned, orch_errors = manifest_validator.validate_orchestration_routing_invariants(root_dir)
    all_errors.extend(orch_errors)
    if verbose or not orch_errors:
        reporter.report_success("orchestration rules & phase boundary specification routing", orch_scanned)

    # 3m. Upstream ticket generation standards & tracer-bullet slicing ceiling (Track 17 [UP-01], [UP-02])
    up_scanned, up_errors = manifest_validator.validate_upstream_ticket_standards(root_dir)
    all_errors.extend(up_errors)
    if verbose or not up_errors:
        reporter.report_success("upstream ticket generation standards & tracer-bullet slicing ceiling", up_scanned)

    # 3n. Pre-flight ticket rejection gate & deterministic validation (Track 17 [REJ-01], [REJ-02])
    pre_scanned, pre_errors = manifest_validator.validate_preflight_ticket_rejection_gate(root_dir)
    all_errors.extend(pre_errors)
    if verbose or not pre_errors:
        reporter.report_success("pre-flight ticket rejection gate & deterministic validation", pre_scanned)

    # 3o. Zero primary session pre-execution invariants (Track 17 [PRE-01], [PRE-02])
    zero_pre_scanned, zero_pre_errors = manifest_validator.validate_zero_pre_execution_invariants(root_dir)
    all_errors.extend(zero_pre_errors)
    if verbose or not zero_pre_errors:
        reporter.report_success("zero primary session pre-execution invariants", zero_pre_scanned)

    # 3p. Decentralized ticket lifecycle invariants (Track 17 [LIFE-01], [LIFE-02])
    life_scanned, life_errors = manifest_validator.validate_ticket_lifecycle_invariants(root_dir)
    all_errors.extend(life_errors)
    if verbose or not life_errors:
        reporter.report_success("decentralized ticket lifecycle claiming & resolution invariants", life_scanned)

    # 3q. Single-ticket termination & anti-frontier-draining invariants (Track 17 [TERM-01], [TERM-02])
    term_scanned, term_errors = manifest_validator.validate_single_ticket_termination_invariants(root_dir)
    all_errors.extend(term_errors)
    if verbose or not term_errors:
        reporter.report_success("single-ticket termination & anti-frontier-draining invariants", term_scanned)

    # 3r. Command Center dynamic frontier filtering invariants (Track 18 [DASH-03])
    dash03_scanned, dash03_errors = manifest_validator.validate_command_center_frontier_filtering_invariants(root_dir)
    all_errors.extend(dash03_errors)
    if verbose or not dash03_errors:
        reporter.report_success("command center dynamic frontier filtering invariants", dash03_scanned)

    # 3s. Command Center session anchor persistence invariants (Track 19 [ANCH-01])
    anchor_scanned, anchor_errors = manifest_validator.validate_session_anchor_invariants(root_dir)
    all_errors.extend(anchor_errors)
    if verbose or not anchor_errors:
        reporter.report_success("command center session anchor persistence invariants", anchor_scanned)

    # 3t. ConnectRPC cross-conversation signal dispatch invariants (Track 19 [SIG-01], [SIG-02])
    sig_scanned, sig_errors = manifest_validator.validate_signal_dispatch_invariants(root_dir)
    all_errors.extend(sig_errors)
    if verbose or not sig_errors:
        reporter.report_success("connectrpc cross-conversation signal dispatch invariants", sig_scanned)

    # 3u. Command Center autonomous reaction & Task Dashboard refresh invariants (Track 19 [REACT-01], [REACT-02])
    react_scanned, react_errors = manifest_validator.validate_command_center_reaction_invariants(root_dir)
    all_errors.extend(react_errors)
    if verbose or not react_errors:
        reporter.report_success("command center autonomous reaction & task dashboard refresh invariants", react_scanned)

    # 3v. Session anchor state & structured review brief invariants (Track 20 [STATE-01], [BRIEF-01])
    state_scanned, state_errors = manifest_validator.validate_session_anchor_state_invariants(root_dir)
    all_errors.extend(state_errors)
    if verbose or not state_errors:
        reporter.report_success("session anchor state & structured review brief invariants", state_scanned)

    # 3w. Verdict routing state machine & Trigger H security audit brief invariants (Track 20 [ROUTE-01], [BRIEF-02])
    route_scanned, route_errors = manifest_validator.validate_verdict_routing_state_machine_invariants(root_dir)
    all_errors.extend(route_errors)
    if verbose or not route_errors:
        reporter.report_success("verdict routing state machine & security audit brief invariants", route_scanned)

    # 3x. Fallback verdict parser & Fix Brief synthesis invariants (Track 20 [ROUTE-02], [FIX-01])
    fix_scanned, fix_errors = manifest_validator.validate_fallback_and_fix_brief_invariants(root_dir)
    all_errors.extend(fix_errors)
    if verbose or not fix_errors:
        reporter.report_success("fallback verdict parser & fix brief synthesis invariants", fix_scanned)

    # 3y. Circuit breaker escalation & PR brief synthesis invariants (Track 20 [FIX-02])
    cb_scanned, cb_errors = manifest_validator.validate_circuit_breaker_invariants(root_dir)
    all_errors.extend(cb_errors)
    if verbose or not cb_errors:
        reporter.report_success("circuit breaker escalation & PR brief synthesis invariants", cb_scanned)

    # 3z. Single-hop builder & adaptive review gate invariants (Track 22 [ORCH-01], [GATE-01], [ADAPT-01], [ADAPT-02], [RECON-01])
    single_hop_scanned, single_hop_errors = manifest_validator.validate_single_hop_builder_invariants(root_dir)
    all_errors.extend(single_hop_errors)
    if verbose or not single_hop_errors:
        reporter.report_success("single-hop builder & adaptive review gate invariants", single_hop_scanned)

    # 3za. Worker report resilience & structural discovery cascade invariants (Track 26 [IDEMP-01], [GATE-01], [DISC-01], [DISC-02], [RECON-01])
    resil_scanned, resil_errors = manifest_validator.validate_worker_report_resilience_invariants(root_dir)
    all_errors.extend(resil_errors)
    if verbose or not resil_errors:
        reporter.report_success("worker report resilience & structural discovery cascade invariants", resil_scanned)

    # 3zb. Dual-domain security gate & routing transparency invariants (Track 27 [ROUTE-01], [BRIEF-02], [ROUTE-03], [ROUTE-04], [RECON-01])
    dual_scanned, dual_errors = manifest_validator.validate_dual_domain_security_gate_invariants(root_dir)
    all_errors.extend(dual_errors)
    if verbose or not dual_errors:
        reporter.report_success("dual-domain security gate & routing transparency invariants", dual_scanned)

    # 3zc. Execution state machine, stage gating & phase reporting airgap invariants (Track 28 [STATE-01], [STATE-02], [DISARM-01], [REPT-01], [AIRGAP-01], [ORCH-03], [RUN-02], [EVAL-01])
    exec_state_scanned, exec_state_errors = manifest_validator.validate_execution_state_invariants(root_dir)
    all_errors.extend(exec_state_errors)
    if verbose or not exec_state_errors:
        reporter.report_success("execution state machine, stage gating & phase reporting airgap invariants", exec_state_scanned)

    # 3zd. Ephemeral worker context & session isolation invariants (Track 31 [EPHM-01], [EPHM-02], ADR 0031)
    ephm_scanned, ephm_errors = manifest_validator.validate_ephemeral_worker_context_invariants(root_dir)
    all_errors.extend(ephm_errors)
    if verbose or not ephm_errors:
        reporter.report_success("ephemeral worker context & session isolation invariants", ephm_scanned)

    # 3ze. Three-tag worker handoff contract invariants (Track 31 [TAG-01], [TAG-02], ADR 0031)
    tag_scanned, tag_errors = manifest_validator.validate_three_tag_worker_handoff_invariants(root_dir)
    all_errors.extend(tag_errors)
    if verbose or not tag_errors:
        reporter.report_success("three-tag worker handoff contract invariants", tag_scanned)

    # 3zf. Active improvement worker adversarial review & failure lineage invariants (Track 31 [IMPR-01], [IMPR-02], ADR 0025, ADR 0031)
    impr_scanned, impr_errors = manifest_validator.validate_active_improvement_worker_invariants(root_dir)
    all_errors.extend(impr_errors)
    if verbose or not impr_errors:
        reporter.report_success("active improvement worker & failure lineage invariants", impr_scanned)

    # 3zg. Bounded verification output invariants (Track 31 [BND-01], [BND-02], ADR 0031)
    bnd_scanned, bnd_errors = manifest_validator.validate_bounded_verification_output_invariants(root_dir)
    all_errors.extend(bnd_errors)
    if verbose or not bnd_errors:
        reporter.report_success("bounded verification output invariants", bnd_scanned)

    # 3zh. Evaluation phase ephemeral prompting & bounded output invariants (Track 32 [EVLP-01], [EVLP-02], ADR 0032)
    eval_scanned, eval_errors = manifest_validator.validate_evaluation_phase_invariants(root_dir)
    all_errors.extend(eval_errors)
    if verbose or not eval_errors:
        reporter.report_success("evaluation phase ephemeral prompting & bounded output invariants", eval_scanned)

    # 3zi. Track 32 evaluation phase resilience, remediation airgap & integrity invariants ([CASC-01], [CASC-02], [EVLP-01], [EVLP-02], [DUDO-01], [REMD-01], [REMD-02], [AIRGAP-02])
    track32_scanned, track32_errors = manifest_validator.validate_track_32_invariants(root_dir)
    all_errors.extend(track32_errors)
    if verbose or not track32_errors:
        reporter.report_success("Track 32 evaluation phase resilience & remediation airgap invariants", track32_scanned)

    # 3zj. Track 33 ticket deconstruction & canonical on-disk persistence invariants ([DISK-01], [DISK-02], ADR 0033)
    decomp_scanned, decomp_errors = manifest_validator.validate_ticket_deconstruction_invariants(root_dir)
    all_errors.extend(decomp_errors)
    if verbose or not decomp_errors:
        reporter.report_success("Track 33 canonical on-disk task graph persistence & mirrored UI projection invariants", decomp_scanned)

    # 3zk. Track 33 in-place Command Center dashboard refresh invariants ([REFRESH-01], [REFRESH-02], ADR 0033)
    refresh_scanned, refresh_errors = manifest_validator.validate_command_center_refresh_invariants(root_dir)
    all_errors.extend(refresh_errors)
    if verbose or not refresh_errors:
        reporter.report_success("Track 33 in-place Command Center dashboard refresh invariants", refresh_scanned)

    # 3zl. Track 36 spec swarm invariants (Two-Stage Ephemeral Spec Swarm, physical seam inspection, bounded output, orchestration rules, Three-Tag handoff)
    spec_swarm_scanned, spec_swarm_errors = manifest_validator.validate_spec_swarm_invariants(root_dir)
    all_errors.extend(spec_swarm_errors)
    if verbose or not spec_swarm_errors:
        reporter.report_success("Track 36 spec swarm invariants & subagent orchestration rules", spec_swarm_scanned)

    # 3zm. Track 34 bug diagnoser ephemeral swarm & orchestration invariants (ADR 0038, [EVAL-01], [EVAL-02])
    track34_scanned, track34_errors = manifest_validator.validate_track_34_invariants(root_dir)
    all_errors.extend(track34_errors)
    if verbose or not track34_errors:
        reporter.report_success("Track 34 bug diagnoser ephemeral swarm & orchestration invariants", track34_scanned)

    # 3zn. Track 35 architecture swarm & phase routing invariants (ADR 0039, [ROUT-01], [ROUT-02], [AIRGAP-03])
    track35_scanned, track35_errors = manifest_validator.validate_track_35_invariants(root_dir)
    all_errors.extend(track35_errors)
    if verbose or not track35_errors:
        reporter.report_success("Track 35 architecture swarm & phase routing invariants", track35_scanned)

    # 3zo. Mechanical Polyglot Subagent Verification Invariant gate ([POLY-01], [ERR-POLY-01])
    polyglot_scanned, polyglot_errors = manifest_validator.validate_polyglot_subagent_verification_invariants(root_dir)
    all_errors.extend(polyglot_errors)
    if verbose or not polyglot_errors:
        reporter.report_success("Polyglot Verification Invariant across subagent definitions ([POLY-01])", polyglot_scanned)

    # 3zp. Mechanical 11-asset installer parity gate across all four distribution channels ([PARITY-02], [ERR-PARITY-01])
    parity_scanned, parity_errors = manifest_validator.validate_installer_asset_parity(root_dir)
    all_errors.extend(parity_errors)
    if verbose or not parity_errors:
        reporter.report_success("11-asset installer parity & cache hygiene across distribution channels", parity_scanned)

    if check_collisions:
        collision_pairs, collision_errors = manifest_validator.check_skill_collisions(skills_dir)
        all_errors.extend(collision_errors)
        if verbose or not collision_errors:
            reporter.report_success("skill descriptions pairwise collision checks", collision_pairs)

    # 4. Scripts permissions & syntax integrity validation
    scripts_dir = root_dir / "scripts"
    hooks_dir = root_dir / "hooks"
    scripts_scanned, script_errors = manifest_validator.validate_scripts(scripts_dir, hooks_dir=hooks_dir)
    all_errors.extend(script_errors)
    if verbose or not script_errors:
        reporter.report_success("scripts & hook scripts permissions, shebangs and syntax", scripts_scanned)

    # 5. Offline internal link integrity validation
    link_validator = LinkIntegrityValidator()
    doc_count, link_count, link_errors = link_validator.validate_all(root_dir)
    all_errors.extend(link_errors)
    if verbose or not link_errors:
        reporter.report_success(f"internal markdown links & anchors across {doc_count} documents", link_count)

    if all_errors:
        reporter.report_errors(all_errors, root_dir)

    reporter.report_summary(
        skill_count=skill_count,
        agent_count=agent_count,
        errors=all_errors,
        manifest_count=manifest_scanned,
        hooks_count=hooks_scanned,
        doc_count=doc_count,
        link_count=link_count,
        rules_count=rules_scanned,
        track10_count=track10_scanned,
        track12_count=track12_scanned,
        track13_count=track13_scanned,
        track14_count=track14_scanned,
    )
    return 0 if not all_errors else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Antigravity Plugin Mechanical Integrity Validator")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Root directory of the plugin workspace")
    parser.add_argument("--verbose", "-v", action="store_true", help="Display verbose validation logging")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    parser.add_argument("--check-collisions", action="store_true", help="Execute pairwise skill collision gate check")
    parser.add_argument("--validate-template", action="store_true", help="Validate canonical task graph template contract schema integrity")
    parser.add_argument("--relocate-templates", action="store_true", help="Relocate domain-specific template assets to local skill resource directories idempotently")

    args = parser.parse_args(argv)
    root_dir = args.root.resolve()

    if getattr(args, "relocate_templates", False):
        v = ManifestValidator()
        relocated = v.relocate_skill_templates(root_dir=root_dir)
        print(f"✅ [SELF-CONT-01] Relocated {len(relocated)} template asset(s) to local skill resources.")
        return 0

    if getattr(args, "validate_template", False):
        v = ManifestValidator()
        is_ok, tmpl_errors = v.validate_task_graph_template_contract(root_dir=root_dir)
        if is_ok:
            print("✅ [TMPL-01] Canonical task graph template adheres to Dual-Layer Cockpit contract.")
            return 0
        else:
            for err in tmpl_errors:
                print(f"❌ {err}", file=sys.stderr)
            return 1

    return run_integrity_check(
        root_dir,
        verbose=args.verbose,
        no_color=args.no_color,
        check_collisions=args.check_collisions,
    )


if __name__ == "__main__":
    sys.exit(main())

