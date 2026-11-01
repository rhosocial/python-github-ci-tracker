"""Full flow test: trigger CI, list runs, get results, webhook."""

import json
import os
import time
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


def github_request(token: str, method: str, path: str, data: dict | None = None) -> dict:
    """Make a GitHub API request."""
    url = f"https://api.github.com{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-tracker-test",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:1000]
        return {"error": e.code, "reason": e.reason, "body": body}


def main() -> None:
    env_path = os.path.join(os.path.dirname(__file__), "..", "local.env")
    token = load_token(env_path)

    repo = "rhosocial/python-activerecord"
    branch = "feature/typed-columns"
    workflow_file = "test.yml"

    print("=" * 60)
    print("1. TRIGGER WORKFLOW")
    print("=" * 60)

    # First, find the workflow ID
    workflows = github_request(token, "GET", f"/repos/{repo}/actions/workflows")
    if "workflows" not in workflows:
        print(f"Failed to list workflows: {workflows}")
        return

    workflow_id = None
    for wf in workflows["workflows"]:
        if wf["path"].endswith(workflow_file):
            workflow_id = wf["id"]
            print(f"Found workflow: {wf['name']} (ID: {workflow_id}, path: {wf['path']})")
            break

    if not workflow_id:
        print(f"Workflow {workflow_file} not found!")
        return

    # Trigger the workflow
    trigger_result = github_request(
        token,
        "POST",
        f"/repos/{repo}/actions/workflows/{workflow_id}/dispatches",
        {"ref": branch},
    )

    if "error" in trigger_result:
        print(f"Trigger failed: {trigger_result['error']} {trigger_result['reason']}")
        print(trigger_result.get("body", ""))
        return

    print("Trigger accepted (202 No Content)")

    # Wait a moment for the run to be created
    time.sleep(3)

    print("\n" + "=" * 60)
    print("2. LIST RUNS (with filter)")
    print("=" * 60)

    # List runs for this workflow
    runs = github_request(
        token,
        "GET",
        f"/repos/{repo}/actions/runs?workflow_id={workflow_id}&branch={branch}&per_page=5",
    )

    if "workflow_runs" not in runs:
        print(f"Failed to list runs: {runs}")
        return

    run_list = runs["workflow_runs"]
    print(f"Found {len(run_list)} runs:")
    for run in run_list:
        print(f"  - Run #{run['run_number']} (ID: {run['id']})")
        print(f"    Status: {run['status']}, Conclusion: {run.get('conclusion', 'N/A')}")
        print(f"    Branch: {run['head_branch']}, Commit: {run['head_sha'][:8]}")
        print(f"    Created: {run['created_at']}")

    if not run_list:
        print("No runs found!")
        return

    # Pick a completed run for log testing
    completed_run = next((r for r in run_list if r["status"] == "completed"), None)
    if not completed_run:
        print("No completed run found for log test")
        return
    run_id = completed_run["id"]

    print("\n" + "=" * 60)
    print("3. GET RUN DETAILS")
    print("=" * 60)

    run_detail = github_request(token, "GET", f"/repos/{repo}/actions/runs/{run_id}")
    if "id" not in run_detail:
        print(f"Failed to get run details: {run_detail}")
        return

    print(f"Run ID: {run_detail['id']}")
    print(f"Name: {run_detail['name']}")
    print(f"Status: {run_detail['status']}")
    print(f"Conclusion: {run_detail.get('conclusion', 'N/A')}")
    print(f"Branch: {run_detail['head_branch']}")
    print(f"Commit: {run_detail['head_sha'][:8]}")
    print(f"Message: {run_detail.get('head_commit', {}).get('message', 'N/A')[:60]}")
    print(f"Actor: {run_detail.get('actor', {}).get('login', 'N/A')}")
    print(f"Started: {run_detail.get('run_started_at', 'N/A')}")
    print(f"URL: {run_detail['html_url']}")

    print("\n" + "=" * 60)
    print("4. GET RUN JOBS")
    print("=" * 60)

    jobs = github_request(token, "GET", f"/repos/{repo}/actions/runs/{run_id}/jobs")
    if "jobs" not in jobs:
        print(f"Failed to get jobs: {jobs}")
        return

    job_list = jobs["jobs"]
    print(f"Found {len(job_list)} jobs:")
    for job in job_list:
        print(f"  - {job['name']} (ID: {job['id']})")
        print(f"    Status: {job['status']}, Conclusion: {job.get('conclusion', 'N/A')}")
        print(f"    Runner: {job.get('runner_name', 'N/A')}")
        print(f"    Steps: {len(job.get('steps', []))}")
        for step in job.get("steps", []):
            print(f"      {step['number']}. {step['name']} - {step['status']}")

    # Get logs for the first job
    if job_list:
        first_job = job_list[0]
        job_id = first_job["id"]

        print("\n" + "=" * 60)
        print("5. GET JOB LOGS")
        print("=" * 60)

        # Test 1: Get logs for a completed job
        print(f"\n--- Test: Get logs for completed job (Job ID: {job_id}) ---")
        logs = github_request(token, "GET", f"/repos/{repo}/actions/jobs/{job_id}/logs")
        if isinstance(logs, dict) and "error" in logs:
            print(f"Failed to get logs: {logs['error']} {logs['reason']}")
        else:
            log_text = str(logs)
            lines = log_text.split("\n")
            print(f"Log lines: {len(lines)}")
            print("First 10 lines:")
            for line in lines[:10]:
                print(f"  {line[:100]}")
            if len(lines) > 10:
                print(f"  ... ({len(lines) - 10} more lines)")

        # Test 2: Try to get logs for a queued/in-progress job (should fail gracefully)
        print("\n--- Test: Get logs for queued job (should fail gracefully) ---")
        queued_job = next((j for j in job_list if j["status"] != "completed"), None)
        if queued_job:
            queued_job_id = queued_job["id"]
            print(f"Trying to get logs for queued job (ID: {queued_job_id})...")
            queued_logs = github_request(
                token, "GET", f"/repos/{repo}/actions/jobs/{queued_job_id}/logs"
            )
            if isinstance(queued_logs, dict) and "error" in queued_logs:
                print(f"Expected failure: {queued_logs['error']} {queued_logs['reason']}")
                print("This is expected - logs are not available for queued/in-progress jobs.")
            else:
                print(f"Unexpectedly got logs for queued job: {str(queued_logs)[:200]}")
        else:
            print("No queued job found to test failure case.")

    print("\n" + "=" * 60)
    print("6. WEBHOOK MANAGEMENT")
    print("=" * 60)

    # List existing webhooks
    hooks = github_request(token, "GET", f"/repos/{repo}/hooks")
    if isinstance(hooks, list):
        print(f"Existing webhooks: {len(hooks)}")
        for hook in hooks:
            print(f"  - ID: {hook['id']}, URL: {hook['config'].get('url', 'N/A')}")
            print(f"    Events: {hook.get('events', [])}")
            print(f"    Active: {hook.get('active', False)}")

        # Check if our webhook already exists
        webhook_url = "https://example.com/webhooks/github"
        existing = [h for h in hooks if h["config"].get("url") == webhook_url]
        if existing:
            print(f"\nWebhook already exists (ID: {existing[0]['id']})")
        else:
            # Create a test webhook
            print("\nCreating test webhook...")
            new_hook = github_request(
                token,
                "POST",
                f"/repos/{repo}/hooks",
                {
                    "name": "web",
                    "active": True,
                    "events": ["workflow_run", "workflow_job"],
                    "config": {
                        "url": webhook_url,
                        "content_type": "json",
                        "secret": "test-secret-123",
                    },
                },
            )
            if "id" in new_hook:
                print(f"Created webhook ID: {new_hook['id']}")
                # Delete it immediately (cleanup)
                print("Cleaning up (deleting test webhook)...")
                github_request(token, "DELETE", f"/repos/{repo}/hooks/{new_hook['id']}")
                print("Deleted.")
            else:
                print(f"Failed to create webhook: {new_hook}")
    else:
        print(f"Failed to list hooks: {hooks}")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
