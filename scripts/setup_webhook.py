"""Create a real GitHub webhook and send a test ping."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

REPO = "rhosocial/python-activerecord"
WEBHOOK_URL = "http://db-dev-1-n.rho.im:18080/webhooks/github"
SECRET = "whsec-test-12345"


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

    token = load_token()
    client = GithubClient()

    existing = await client.list_webhooks(token, REPO)
    ours = [h for h in existing if (h.get("config") or {}).get("url") == WEBHOOK_URL]
    if ours:
        hook_id = ours[0]["id"]
        print(f"webhook already exists: id={hook_id}")
    else:
        created = await client.create_webhook(
            token,
            REPO,
            {
                "url": WEBHOOK_URL,
                "content_type": "json",
                "secret": SECRET,
                "events": ["workflow_run", "workflow_job"],
            },
        )
        hook_id = created["id"]
        print(f"webhook created: id={hook_id}")
        print(f"  url    : {created['config']['url']}")
        print(f"  events : {created.get('events')}")

    print(f"\nsending ping to hook {hook_id} ...")
    await client.ping_webhook(token, REPO, hook_id)
    print("ping sent -- check the service log for the delivery")


if __name__ == "__main__":
    asyncio.run(main())
