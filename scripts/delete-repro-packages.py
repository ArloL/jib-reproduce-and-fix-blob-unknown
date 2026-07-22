#!/usr/bin/env python3
"""Delete the ephemeral GHCR container packages created by the jib repro
experiment (`.github/workflows/jib*.yaml`).

Each experiment attempt pushes to a unique repo like
`jib-reproduce-and-fix-blob-unknown-repro/<run>-<attempt>-<variant>-<n>`,
so they pile up fast. This lists the user's (or an org's) container
packages, filters by a name substring, and deletes the matches.

Dry-run by default -- pass --yes to actually delete.

Auth: needs a token with `read:packages` + `delete:packages`. Taken from
--token-env (default GITHUB_TOKEN/GH_TOKEN), else `gh auth token`. To
re-scope an existing gh login:

    gh auth refresh --scopes read:packages,delete:packages

Examples:
    python3 scripts/delete-repro-packages.py                 # dry-run, user packages
    python3 scripts/delete-repro-packages.py --yes           # actually delete
    python3 scripts/delete-repro-packages.py --org my-org --yes
    python3 scripts/delete-repro-packages.py --pattern 29898470121   # one run only
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


def get_token(token_env):
    import os

    for name in token_env:
        val = os.environ.get(name)
        if val:
            return val.strip()
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit(
            "No token found. Set GITHUB_TOKEN (with read:packages + "
            "delete:packages) or run `gh auth token`."
        )


def api_request(method, path, token):
    url = path if path.startswith("http") else API + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "delete-repro-packages")
    return urllib.request.urlopen(req)


def list_packages(base_path, token):
    """Yield all container packages, following pagination."""
    page = 1
    while True:
        sep = "&" if "?" in base_path else "?"
        path = f"{base_path}{sep}per_page=100&page={page}"
        try:
            resp = api_request("GET", path, token)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            sys.exit(f"Failed to list packages ({e.code}): {body}")
        batch = json.loads(resp.read())
        if not batch:
            break
        yield from batch
        page += 1


def main():
    parser = argparse.ArgumentParser(
        description="Delete ephemeral GHCR repro container packages."
    )
    parser.add_argument(
        "--pattern",
        default="-repro/",
        help="Only delete packages whose name contains this substring "
        "(default: '-repro/').",
    )
    parser.add_argument(
        "--org",
        default=None,
        help="Delete org-owned packages instead of user-owned.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this, only a dry-run is printed.",
    )
    parser.add_argument(
        "--token-env",
        nargs="+",
        default=["GITHUB_TOKEN", "GH_TOKEN"],
        help="Env var(s) to read the token from (default: GITHUB_TOKEN GH_TOKEN).",
    )
    args = parser.parse_args()

    token = get_token(args.token_env)

    if args.org:
        list_path = f"/orgs/{args.org}/packages?package_type=container"
        del_base = f"/orgs/{args.org}/packages/container/"
    else:
        list_path = "/user/packages?package_type=container"
        del_base = "/user/packages/container/"

    matches = [
        pkg for pkg in list_packages(list_path, token) if args.pattern in pkg["name"]
    ]
    matches.sort(key=lambda p: p["name"])

    if not matches:
        print(f"No container packages match '{args.pattern}'. Nothing to do.")
        return

    print(f"Matched {len(matches)} package(s) containing '{args.pattern}':")
    for pkg in matches:
        print(f"  {pkg['name']}")

    if not args.yes:
        print(
            "\nDry-run only. Re-run with --yes to delete the packages above.",
        )
        return

    print(f"\nDeleting {len(matches)} package(s)...")
    failures = 0
    for pkg in matches:
        encoded = urllib.parse.quote(pkg["name"], safe="")
        try:
            api_request("DELETE", del_base + encoded, token)
            print(f"  deleted: {pkg['name']}")
        except urllib.error.HTTPError as e:
            failures += 1
            body = e.read().decode("utf-8", "replace")
            print(f"  FAILED ({e.code}): {pkg['name']}: {body}", file=sys.stderr)

    deleted = len(matches) - failures
    print(f"\nDone. Deleted {deleted}, failed {failures}.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
