import asyncio
import subprocess
from types import SimpleNamespace

from companion.github_repositories import GitHubRepositories


def test_repository_metadata_is_read_only_bounded_and_mapped(monkeypatch):
    source = GitHubRepositories("Timverhoogt", {"Timverhoogt/homebody": "homebody"})
    calls = []

    def fetch():
        calls.append(1)
        return [
            {"nameWithOwner": "Timverhoogt/homebody", "isPrivate": False, "isArchived": False},
            {"nameWithOwner": "Timverhoogt/private-repo", "isPrivate": True, "isArchived": False},
        ]

    monkeypatch.setattr(source, "_fetch", fetch)

    async def check():
        result = await source.read({"existing": SimpleNamespace(project_id="homebody")})
        assert result["available"] and len(result["repositories"]) == 2
        assert result["repositories"][0]["target_ids"] == ["existing"]
        assert result["repositories"][1]["target_ids"] == []
        assert result["repositories"][1]["private"] is True
        assert result["repositories"][1]["url"] == "https://github.com/Timverhoogt/private-repo"
        assert (await source.read({}))["repositories"][0]["target_ids"] == []
        assert calls == [1]

    asyncio.run(check())


def test_invalid_repo_and_errors_do_not_disclose_output(monkeypatch):
    for data in [
        [{"nameWithOwner": "attacker/elsewhere", "isPrivate": True, "isArchived": False}],
        [{"nameWithOwner": "Timverhoogt/../../secret", "isPrivate": True, "isArchived": False}],
    ]:
        source = GitHubRepositories("Timverhoogt")
        monkeypatch.setattr(source, "_fetch", lambda data=data: data)
        assert asyncio.run(source.read({}))["repositories"] == []
    source = GitHubRepositories("Timverhoogt")

    def fail():
        raise subprocess.CalledProcessError(1, "gh", output="SECRET fixture")

    monkeypatch.setattr(source, "_fetch", fail)
    result = asyncio.run(source.read({}))
    assert not result["available"] and "SECRET" not in str(result)
    assert not asyncio.run(GitHubRepositories().read({}))["available"]


def test_cli_uses_saved_host_login_and_fixed_read_only_args(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "never-forward")
    monkeypatch.setenv("GITHUB_TOKEN", "never-forward")
    monkeypatch.setenv("GH_HOST", "evos-it.ghe.com")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")

    def run(args, **kwargs):
        assert args == [
            "/usr/bin/gh",
            "repo",
            "list",
            "Timverhoogt",
            "--limit",
            "500",
            "--json",
            "nameWithOwner,isPrivate,isArchived",
        ]
        assert kwargs["env"]["GH_HOST"] == "github.com"
        assert "GH_TOKEN" not in kwargs["env"] and "GITHUB_TOKEN" not in kwargs["env"]
        assert kwargs["timeout"] == 15 and kwargs["check"]
        return SimpleNamespace(stdout="[]")

    monkeypatch.setattr("subprocess.run", run)
    assert GitHubRepositories("Timverhoogt")._fetch() == []


def test_repository_route_requires_runtime_workspace_authority_even_without_targets(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from test_project_roadmap import context

    from companion.hermes_reachy_bridge import Bridge

    source = GitHubRepositories("Timverhoogt")
    monkeypatch.setattr(
        source,
        "_fetch",
        lambda: [{"nameWithOwner": "Timverhoogt/private-repo", "isPrivate": True, "isArchived": False}],
    )

    async def check():
        bridge = Bridge(api_key="owner-fixture", hermes_url="http://unused.test")
        bridge.github_repositories = source
        app = web.Application(middlewares=[bridge.kids_latch_middleware])
        app.router.add_post("/v1/agent/session", bridge.broker_session)
        app.router.add_post("/v1/agent/native/{action}", bridge.native_workspace_action)
        headers = {"Authorization": "Bearer owner-fixture", "X-Reachy-Device-Id": "catalog-fixture"}
        state = context()
        async with TestClient(TestServer(app)) as client:
            url = "/v1/agent/native/repositories"
            assert (await client.post(url, json={"context": state})).status == 401
            assert (await client.post(url, json={"context": state}, headers=headers)).status == 403
            assert (await client.post("/v1/agent/session", json={"context": state}, headers=headers)).status == 200
            response = await client.post(url, json={"context": state}, headers=headers)
            assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
            assert (await response.json())["repositories"][0]["private"]
            assert (
                await client.post(url, json={"context": state, "repository": "arbitrary"}, headers=headers)
            ).status == 409
            state["capability_profile"] = "conversation"
            state["session_generation"] += 1
            state["requested_session_generation"] = state["session_generation"]
            assert (await client.post("/v1/agent/session", json={"context": state}, headers=headers)).status == 200
            assert (await client.post(url, json={"context": state}, headers=headers)).status == 403

    asyncio.run(check())
