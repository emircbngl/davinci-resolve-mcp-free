"""What PyPI and the MCP Registry are given: one version, one name, and a README that
proves the package is this server (the registry looks for the mcp-name line in it)."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import resolve_pilot

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
WORKFLOW = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")


def test_every_place_names_the_same_version():
    version = PROJECT["version"]
    assert resolve_pilot.__version__ == version
    assert SERVER["version"] == version
    assert [p["version"] for p in SERVER["packages"]] == [version]
    assert (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split("\n## ")[1].split("\n")[0] == version


def test_the_registry_entry_points_at_this_package():
    package = SERVER["packages"][0]
    assert (package["registryType"], package["identifier"]) == ("pypi", PROJECT["name"])
    assert package["transport"] == {"type": "stdio"}
    assert SERVER["name"] == "io.github.emircbngl/" + PROJECT["name"]
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", SERVER["name"])
    assert 1 <= len(SERVER["description"]) <= 100      # the registry refuses a longer one
    assert 1 <= len(SERVER["title"]) <= 100
    assert SERVER["repository"]["url"] == PROJECT["urls"]["Repository"]


def test_the_readme_carries_the_registrys_ownership_line():
    # PyPI shows the README as the package description; the registry reads it there. The
    # name has to be followed by whitespace or the end of the comment, not by a full stop.
    readme = (ROOT / PROJECT["readme"]).read_text(encoding="utf-8")
    assert re.findall(r"mcp-name: (\S+)(?=\s)", readme) == [SERVER["name"]]


def test_the_package_starts_under_its_own_name():
    # `uvx <package>` runs the script named like the package.
    scripts = PROJECT["scripts"]
    assert scripts[PROJECT["name"]] == scripts["resolve-pilot"] == "resolve_pilot.mcp_server:main"


def test_publishing_stores_no_token_and_checks_the_version_first():
    assert "secrets." not in WORKFLOW and "password:" not in WORKFLOW
    assert WORKFLOW.count("id-token: write") == 2 and "\npermissions:\n  contents: read\n" in WORKFLOW
    assert f"PACKAGE: {PROJECT['name']}" in WORKFLOW
    build, pypi, registry = (WORKFLOW.index(f"\n  {job}:\n") for job in ("build", "pypi", "registry"))
    assert build < WORKFLOW.index('if not ref.startswith("refs/tags/v"):') < pypi
    assert pypi < WORKFLOW.index("needs: build") < WORKFLOW.index("pypa/gh-action-pypi-publish@") < registry
    # The registry reads the package from PyPI, so it comes after, and with a checked binary.
    tail = WORKFLOW[registry:]
    assert "needs: [build, pypi]" in tail
    assert tail.index("cosign verify-blob") < tail.index("tar xzf") < tail.index("./mcp-publisher login github-oidc")
    assert "releases/latest" not in WORKFLOW
