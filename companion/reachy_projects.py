"""Host-owned project catalog: exact roadmap files, never caller-selected paths."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    title: str
    root: Path
    roadmap: Path


class ProjectCatalog:
    """Configuration grants one file per project, not repository traversal."""

    def __init__(self, projects: tuple[Project, ...] = ()) -> None:
        self.projects = {project.project_id: project for project in projects}
        if len(self.projects) != len(projects) or len(projects) > 32:
            raise ValueError("project catalog must contain at most 32 distinct projects")
        for project in projects:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", project.project_id):
                raise ValueError("invalid project identity")
            if not project.title.strip() or len(project.title) > 120:
                raise ValueError("invalid project title")
            if not project.root.is_absolute():
                raise ValueError("project root must be absolute")
            if (
                project.roadmap.is_absolute()
                or not project.roadmap.parts
                or ".." in project.roadmap.parts
                or project.roadmap.suffix != ".md"
            ):
                raise ValueError("roadmap must be one relative Markdown file")

    @classmethod
    def from_json(cls, raw: str) -> ProjectCatalog:
        if not raw.strip():
            return cls()
        payload = json.loads(raw)
        if not isinstance(payload, dict) or len(payload) > 32:
            raise ValueError("project configuration must be a bounded object")
        projects = []
        for identifier, config in payload.items():
            if not isinstance(config, dict) or set(config) != {"title", "root", "roadmap"}:
                raise ValueError("project requires title, root and roadmap only")
            if any(type(value) is not str for value in config.values()):
                raise ValueError("project fields must be strings")
            projects.append(Project(identifier, config["title"], Path(config["root"]), Path(config["roadmap"])))
        return cls(tuple(projects))

    def public_catalog(self) -> list[dict[str, str]]:
        return [{"project_id": p.project_id, "title": p.title} for p in self.projects.values()]

    def snapshot(self, identifier: str, read_file: Callable[[Path, Path], bytes]) -> dict[str, object]:
        if identifier not in self.projects:
            raise ValueError("project is not registered")
        project = self.projects[identifier]
        raw = read_file(project.root, project.roadmap)
        text = raw.decode("utf-8", errors="strict")
        lines = text.splitlines()
        # Preserve both current direction and late acceptance/status sections.
        numbered = [f"{i}|{line}" for i, line in enumerate(lines, 1)]
        full = "\n".join(numbered)
        if len(full) <= 18_000:
            excerpt, ranges = full, [[1, len(lines)]] if lines else []
        else:
            first, last = [], []
            budget = 8_500
            for line in numbered:
                if len(line) + 1 > budget:
                    break
                first.append(line)
                budget -= len(line) + 1
            budget = 8_500
            for line in reversed(numbered[len(first) :]):
                if len(line) + 1 > budget:
                    break
                last.append(line)
                budget -= len(line) + 1
            excerpt = "\n".join(first + ["[omitted lines: do not infer their contents]"] + list(reversed(last)))
            ranges = ([[1, len(first)]] if first else []) + (
                [[len(lines) - len(last) + 1, len(lines)]] if last else []
            )
        return {
            "project_id": identifier,
            "title": project.title,
            "roadmap": project.roadmap.as_posix(),
            "text": excerpt,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "line_count": len(lines),
            "source_ranges": ranges,
            "truncated": len(full) > 18_000,
            "execution_available": False,
        }
