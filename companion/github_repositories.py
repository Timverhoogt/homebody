"""Read-only owner repository metadata. GitHub authentication stays on the bridge host."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time


class GitHubRepositories:
    def __init__(self, owner="", project_map=None):
        self.owner = owner
        self.project_map = project_map or {}
        if owner and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
            raise ValueError("invalid GitHub owner")
        if not isinstance(self.project_map, dict) or len(self.project_map) > 500:
            raise ValueError("invalid GitHub project map")
        for repository, project in self.project_map.items():
            if (
                not isinstance(repository, str)
                or not self.valid_name(repository)
                or not isinstance(project, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", project)
            ):
                raise ValueError("invalid GitHub project mapping")
        self._cache = None
        self._lock = asyncio.Lock()

    def valid_name(self, name):
        return isinstance(name, str) and bool(
            re.fullmatch(re.escape(self.owner) + r"/[A-Za-z0-9_.-]{1,100}", name, flags=re.IGNORECASE)
        )

    @classmethod
    def from_env(cls):
        return cls(os.getenv("REACHY_GITHUB_OWNER", ""), json.loads(os.getenv("REACHY_GITHUB_PROJECT_MAP_JSON", "{}")))

    def _fetch(self):
        executable = shutil.which("gh")
        if not executable:
            raise RuntimeError("GitHub CLI unavailable")
        # Explicit github.com account. Do not inherit a different GH_HOST or exported work token.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST"}
        }
        env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_PAGER="cat")
        result = subprocess.run(
            [executable, "repo", "list", self.owner, "--limit", "500", "--json", "nameWithOwner,isPrivate,isArchived"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
            env=env,
        )
        if len(result.stdout) > 256000:
            raise ValueError("repository metadata too large")
        return json.loads(result.stdout)

    async def read(self, targets):
        if not self.owner:
            return {"available": False, "repositories": [], "error": "GitHub repository browsing is not configured."}
        async with self._lock:
            try:
                if not self._cache or time.time() - self._cache[0] >= 120:
                    raw = await asyncio.to_thread(self._fetch)
                    if not isinstance(raw, list) or len(raw) > 500:
                        raise ValueError("invalid repository response")
                    rows, seen = [], set()
                    for item in raw:
                        name = item.get("nameWithOwner") if isinstance(item, dict) else None
                        if (
                            not self.valid_name(name)
                            or name.casefold() in seen
                            or type(item.get("isPrivate")) is not bool
                            or type(item.get("isArchived")) is not bool
                        ):
                            raise ValueError("invalid repository metadata")
                        seen.add(name.casefold())
                        rows.append({"repository": name, "private": item["isPrivate"], "archived": item["isArchived"]})
                    self._cache = (time.time(), rows)
                mappings = {k.casefold(): v for k, v in self.project_map.items()}
                rows = []
                for row in self._cache[1]:
                    project = mappings.get(row["repository"].casefold())
                    # Listing or selecting a repo cannot create/register a target or grant tools.
                    ids = sorted(k for k, t in targets.items() if t.project_id == project)
                    rows.append({**row, "target_ids": ids, "url": "https://github.com/" + row["repository"]})
                rows.sort(key=lambda r: (r["archived"], r["repository"].casefold()))
                return {
                    "available": True,
                    "owner": self.owner,
                    "repositories": rows,
                    "checked_at": self._cache[0],
                    "limit": 500,
                    "possibly_truncated": len(rows) == 500,
                }
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                # Do not return subprocess output, credentials or a stale list as fresh success.
                self._cache = None
                return {
                    "available": False,
                    "repositories": [],
                    "error": "GitHub repositories unavailable. Check the bridge host's saved GitHub login.",
                }
