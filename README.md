# disable-eslint-config

Disables the **"use configuration file"** flag for every ESLint tool version
(ESLint, ESLint (deprecated), ESLint9, and any future versions Codacy adds)
across every repository in one or more Codacy organizations.

When this flag is on, Codacy defers to the repository's own `.eslintrc*` /
`eslint.config.*` file instead of the patterns configured in Codacy. This
script turns that off, repo by repo, org by org, via the Codacy API.

## Requirements

- Python 3.9+
- A Codacy **account-level API token** (Account Settings → Access Management
  → API Tokens). Project tokens will not work — this script needs
  organization- and account-scoped endpoints.

## Setup

Always run this from a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
export CODACY_API_TOKEN=your-account-api-token

# See what would change, without touching anything
python disable_eslint_config.py --provider gh --org my-org --dry-run

# Apply it for real (prompts for confirmation)
python disable_eslint_config.py --provider gh --org my-org

# Apply it without the confirmation prompt (e.g. in CI)
python disable_eslint_config.py --provider gh --org my-org --yes
```

### Options

| Flag         | Description                                                                 |
|--------------|-------------------------------------------------------------------------------|
| `--provider` | Git provider: `gh` (GitHub), `gl` (GitLab), or `bb` (Bitbucket). Default `gh`. |
| `--org`      | Organization to process. Repeatable. Omit to process every org the token can see. |
| `--repo`     | Only process repositories whose name contains this substring.              |
| `--dry-run`  | Report what would change without calling the update endpoint.              |
| `--yes`      | Skip the confirmation prompt before making live changes.                   |

If `--org` is omitted, the script calls `GET /user/organizations/{provider}`
and processes every organization the token has access to — use `--org`
and/or `--repo` to scope a run to specific repositories.

## What it does

1. Lists organizations (`--org`, or all orgs visible to the token).
2. Lists every repository already added to Codacy in each organization.
3. For each repository, fetches its configured analysis tools.
4. Filters to tools whose name contains "eslint" (case-insensitive), which
   matches every ESLint variant Codacy exposes.
5. For each matching tool where `usesConfigurationFile` is `true`, sends
   `PATCH .../tools/{toolUuid}` with `{"useConfigurationFile": false}`.
6. Prints a per-repo log line and a final summary (scanned / updated /
   already-disabled / failed).

The script only ever touches ESLint tools' `useConfigurationFile` setting —
it does not enable/disable tools or change any patterns.

## Exit codes

- `0` — completed with no failures.
- `1` — missing token, no organizations found, a run was aborted at the
  confirmation prompt, or one or more repos/tools failed to update (see
  stderr for details).

## Security notes

- The API token is only ever read from the `CODACY_API_TOKEN` environment
  variable — it is never accepted as a command-line argument, so it won't
  end up in shell history or `ps` output.
- Treat the token like a password. If it is ever pasted into a chat,
  ticket, log, or shared terminal, rotate it immediately in Codacy
  (Account Settings → API Tokens) — the old one should be considered
  compromised even if you plan to keep using it.
- A non-dry-run invocation modifies live Codacy settings across every
  matched repository. Always run `--dry-run` first and review the output.
