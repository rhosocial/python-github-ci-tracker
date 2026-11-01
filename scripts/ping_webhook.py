"""Send a test ping to the CI Tracker webhook."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

REPO = "rhosocial/python-activerecord"
HOOK_ID = 695345980


def load_token() -> str:
    for candidate in (Path.cwd() / "local.env", Path.cwd().parent / "local.env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("GITHUB_FINE_GRAINED_TOKEN="):
                    return line.split("=", 1)[1].strip()
    raise SystemExit("token not found")


async def main() -> None:
    from rhosocial.github_ci_tracker.github.client import GithubClient

    client = GithubClient()
    token = load_token()
    await client.ping_webhook(token, REPO, HOOK_ID)
    print(f"ping sent to hook {HOOK_ID}")


if __name__ == "__main__":
    asyncio.run(main())
