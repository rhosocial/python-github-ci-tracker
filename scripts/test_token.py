"""Test GitHub token access to rhocial org repositories."""

import json
import os
import urllib.error
import urllib.request


def load_token(env_path: str) -> str:
    """Load token from env file without printing it."""
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("GITHUB_FINE_GRAINED_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise ValueError("GITHUB_FINE_GRAINED_TOKEN not found")


def github_get(token: str, path: str) -> dict:
    """Make a GitHub API GET request."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-tracker-test",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        return {"error": e.code, "reason": e.reason, "body": body}


def main() -> None:
    env_path = os.path.join(os.path.dirname(__file__), "..", "local.env")
    token = load_token(env_path)

    # 1. Verify token validity
    print("=== Token Verification ===")
    user = github_get(token, "/user")
    if "error" in user:
        print(f"FAILED: {user['error']} {user['reason']}")
        return
    print(f"Token owner: {user['login']}")
    print(f"User ID: {user['id']}")

    # 2. Check rate limit
    print("\n=== Rate Limit ===")
    rate = github_get(token, "/rate_limit")
    if "resources" in rate:
        core = rate["resources"]["core"]
        print(f"Core: {core['remaining']}/{core['limit']} (reset in {core['reset']})")

    # 3. List rhosocial repos accessible by this token
    print("\n=== Accessible rhosocial Repositories ===")
    repos = github_get(token, "/orgs/rhosocial/repos?per_page=100&sort=updated")
    if isinstance(repos, list):
        print(f"Found {len(repos)} repositories:")
        for repo in repos:
            visibility = "private" if repo["private"] else "public"
            print(f"  - {repo['full_name']} ({visibility}) | {repo['language']}")
    else:
        print(f"Error: {repos.get('error')} {repos.get('reason')}")

    # 4. Check token permissions via probe
    print("\n=== Permission Probes ===")
    test_repo = "rhosocial/python-activerecord"

    # Actions read
    actions = github_get(token, f"/repos/{test_repo}/actions/workflows")
    print(f"Actions read: {'Yes' if 'workflows' in actions else 'No'}")

    # Hooks management
    hooks = github_get(token, f"/repos/{test_repo}/hooks")
    print(f"Hooks management: {'Yes' if isinstance(hooks, list) else 'No'}")


if __name__ == "__main__":
    main()
