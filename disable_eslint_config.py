#!/usr/bin/env python3
"""Disable the "use configuration file" flag for every ESLint tool version
across every repository in one or more Codacy organizations.

Requires the CODACY_API_TOKEN environment variable (an account-level Codacy
API token) and the packages in requirements.txt installed in a venv:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    export CODACY_API_TOKEN=...
    python disable_eslint_config.py --provider gh --org my-org --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field

import requests

API_BASE = "https://api.codacy.com/api/v3"


# Identifies one repository on a Git provider, as Codacy addresses it.
@dataclass
class Repo:
    provider: str
    owner: str
    name: str


# Running totals printed at the end of the run.
@dataclass
class Summary:
    repos_scanned: int = 0
    tools_already_disabled: int = 0
    tools_updated: int = 0
    tools_failed: int = 0
    repos_failed: list[str] = field(default_factory=list)


# Thin wrapper around the Codacy REST API: auth header, retries, and
# pagination, plus the handful of endpoints this script actually needs.
class CodacyClient:
    def __init__(self, token: str, max_retries: int = 5):
        self.session = requests.Session()
        self.session.headers["api-token"] = token
        self.max_retries = max_retries

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        # Retries on HTTP 429, backing off using Retry-After (or a default
        # exponential backoff) since the Codacy API rate-limits heavy use.
        url = f"{API_BASE}{path}"
        resp = None
        for attempt in range(1, self.max_retries + 1):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429 and attempt < self.max_retries:
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(wait)
                continue
            return resp
        return resp

    def paginate(self, path: str, params: dict | None = None):
        # Codacy list endpoints return a `pagination.cursor` when there is
        # another page; keep following it until it's absent.
        params = dict(params or {})
        while True:
            resp = self._request("GET", path, params=params)
            resp.raise_for_status()
            body = resp.json()
            yield from body.get("data", [])
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    def list_organizations(self, provider: str):
        # GET /user/organizations/{provider} - every org the token can see.
        yield from self.paginate(f"/user/organizations/{provider}")

    def list_organization_repositories(self, provider: str, org: str):
        # GET /organizations/{provider}/{org}/repositories - every repo
        # already added to Codacy for this org.
        yield from self.paginate(
            f"/organizations/{provider}/{org}/repositories",
            params={"filter": "Synced"},
        )

    def list_repository_tools(self, provider: str, org: str, repo: str):
        # GET .../tools - every analysis tool configured for one repo,
        # including its `usesConfigurationFile` setting.
        resp = self._request(
            "GET",
            f"/analysis/organizations/{provider}/{org}/repositories/{repo}/tools",
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def configure_tool(self, provider: str, org: str, repo: str, tool_uuid: str, body: dict):
        # PATCH .../tools/{toolUuid} - flips settings (here,
        # useConfigurationFile) for one tool on one repo.
        resp = self._request(
            "PATCH",
            f"/analysis/organizations/{provider}/{org}/repositories/{repo}/tools/{tool_uuid}",
            json=body,
        )
        resp.raise_for_status()
        return resp


def is_eslint_tool(name: str) -> bool:
    # Matches every ESLint variant Codacy exposes (ESLint, ESLint9,
    # ESLint (deprecated), future versions, etc.) by a simple substring check.
    return "eslint" in name.lower()


def process_repo(client: CodacyClient, repo: Repo, dry_run: bool, summary: Summary) -> None:
    # Fetch this repo's tool list; a failure here means the repo is skipped
    # entirely rather than aborting the whole run.
    try:
        tools = client.list_repository_tools(repo.provider, repo.owner, repo.name)
    except requests.HTTPError as exc:
        print(f"  ! {repo.owner}/{repo.name}: failed to list tools ({exc})", file=sys.stderr)
        summary.repos_failed.append(f"{repo.owner}/{repo.name}")
        return

    # Walk each ESLint tool found and disable "use configuration file"
    # unless it's already off.
    for tool in tools:
        name = tool.get("name", "")
        if not is_eslint_tool(name):
            continue

        settings = tool.get("settings", {})
        if not settings.get("usesConfigurationFile"):
            print(f"  = {repo.owner}/{repo.name}: {name} already not using a configuration file")
            summary.tools_already_disabled += 1
            continue

        print(f"  * {repo.owner}/{repo.name}: {name} uses a configuration file -> disabling")
        if dry_run:
            summary.tools_updated += 1
            continue

        try:
            client.configure_tool(
                repo.provider,
                repo.owner,
                repo.name,
                tool["uuid"],
                {"useConfigurationFile": False},
            )
            summary.tools_updated += 1
        except requests.HTTPError as exc:
            print(
                f"  ! {repo.owner}/{repo.name}: failed to update {name} ({exc})",
                file=sys.stderr,
            )
            summary.tools_failed += 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--provider", default="gh", choices=["gh", "gl", "bb"])
    parser.add_argument(
        "--org",
        action="append",
        dest="orgs",
        help="Organization (remoteOrganizationName) to process. Repeatable. "
        "If omitted, every organization visible to the token is processed.",
    )
    parser.add_argument(
        "--repo",
        help="Only process repositories whose name contains this substring.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without calling the update endpoint.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt before making live changes.",
    )
    return parser.parse_args()


def confirm_live_run(orgs: list[str]) -> bool:
    # Client-facing safety net: a live (non-dry-run) run mutates repository
    # settings, so require an explicit yes unless --yes was passed.
    orgs_display = ", ".join(orgs) if orgs else "every organization visible to the token"
    answer = input(
        f"About to disable 'use configuration file' for ESLint tools in: {orgs_display}\n"
        "This makes live changes in Codacy. Continue? [y/N] "
    )
    return answer.strip().lower() in ("y", "yes")


def main() -> int:
    args = parse_args()

    # The token is never accepted as a CLI argument so it can't leak into
    # shell history or `ps` output; it must come from the environment.
    token = os.environ.get("CODACY_API_TOKEN")
    if not token:
        print("CODACY_API_TOKEN environment variable is required", file=sys.stderr)
        return 1

    if not args.dry_run and not args.yes and not confirm_live_run(args.orgs or []):
        print("Aborted.")
        return 1

    client = CodacyClient(token)

    # Default to every organization the token can see if none was given.
    orgs = args.orgs
    if not orgs:
        orgs = [o["name"] for o in client.list_organizations(args.provider)]
        if not orgs:
            print(f"No organizations found for provider {args.provider}", file=sys.stderr)
            return 1

    # Walk org -> repo -> tool, updating/reporting as we go.
    summary = Summary()
    for org in orgs:
        print(f"== Organization: {args.provider}/{org} ==")
        for repo_data in client.list_organization_repositories(args.provider, org):
            name = repo_data["name"]
            if args.repo and args.repo not in name:
                continue
            repo = Repo(provider=args.provider, owner=repo_data.get("owner", org), name=name)
            summary.repos_scanned += 1
            process_repo(client, repo, args.dry_run, summary)

    verb = "would be updated" if args.dry_run else "updated"
    print(
        f"\nScanned {summary.repos_scanned} repositories. "
        f"{summary.tools_updated} ESLint tool configuration(s) {verb}, "
        f"{summary.tools_already_disabled} already disabled, "
        f"{summary.tools_failed} failed."
    )
    if summary.repos_failed:
        print(f"Repositories that could not be read: {', '.join(summary.repos_failed)}", file=sys.stderr)

    return 1 if (summary.tools_failed or summary.repos_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
