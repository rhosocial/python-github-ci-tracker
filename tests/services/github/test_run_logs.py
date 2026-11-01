# tests/services/github/test_run_logs.py
"""The run archive: what form arrived, and what each job got.

GitHub serves the same endpoint with two different layouts -- per-step files soon
after the run finishes, one merged file per job once the archive goes cold -- and
says nothing about which one it sent. These tests pin the reading of both, and
the three cases that are easy to get wrong: ``system.txt`` is not a signal, a
job holding only ``system.txt`` is not a job with steps, and a partially filled
archive must answer per job rather than picking one verdict for the run.
"""

from __future__ import annotations

import zipfile

import pytest
from conftest import make_archive

from rhosocial.github_ci_tracker.services.github import GithubRuns
from rhosocial.github_ci_tracker.services.github.logs import RunLogArchive

REPO = "acme/demo"
RUN = 900_001

STEP_ARCHIVE = {
    "Python 3.10/1_Set up job.txt": "\ufeffset up\n",
    "Python 3.10/11_Test.txt": "one failure\n",
    "Python 3.10/system.txt": "Requested labels: ubuntu-latest\n",
    "0_Python 3.10.txt": "set up\none failure\n",
    "Python 3.12/1_Set up job.txt": "other set up\n",
    "1_Python 3.12.txt": "other set up\n",
}

COLD_ARCHIVE = {
    "Python 3.10/system.txt": "Requested labels: ubuntu-latest\n",
    "0_Python 3.10.txt": "set up\none failure\n",
    "Python 3.12/system.txt": "Requested labels: ubuntu-latest\n",
    "1_Python 3.12.txt": "other set up\n",
}


class TestWhichFormArrived:
    def test_step_files_mean_the_step_form(self) -> None:
        archive = RunLogArchive.from_bytes(make_archive(STEP_ARCHIVE))

        assert archive.form == "step"
        assert archive.has_steps is True

    def test_system_txt_alone_is_not_a_step_signal(self) -> None:
        """The cold archive carries system.txt and no step files; that is "job"."""
        archive = RunLogArchive.from_bytes(make_archive(COLD_ARCHIVE))

        assert archive.form == "job"
        assert archive.has_steps is False
        assert archive.knows_job("Python 3.10") is True, "the directory exists"

    def test_a_half_filled_archive_answers_per_job(self) -> None:
        """A run whose last job is still finishing has steps for some jobs only."""
        entries = dict(STEP_ARCHIVE)
        entries.pop("Python 3.12/1_Set up job.txt")
        archive = RunLogArchive.from_bytes(make_archive(entries))

        assert archive.form == "step"
        assert archive.steps_for("Python 3.10") != {}
        assert archive.steps_for("Python 3.12") == {}
        assert archive.knows_job("Python 3.12") is True

    def test_a_job_the_archive_never_mentions_is_unknown(self) -> None:
        archive = RunLogArchive.from_bytes(make_archive(STEP_ARCHIVE))

        assert archive.knows_job("Python 3.9") is False


class TestWhatIsInside:
    def test_step_files_are_keyed_by_number_not_by_name(self) -> None:
        """The file name is the rendered command; only the number is stable."""
        archive = RunLogArchive.from_bytes(make_archive(STEP_ARCHIVE))

        steps = archive.steps_for("Python 3.10")

        assert sorted(steps) == [1, 11]
        assert steps[11].name == "Test"
        assert steps[11].content == "one failure\n"
        assert steps[11].job == "Python 3.10"

    def test_the_merged_text_is_available_for_both_forms(self) -> None:
        for entries in (STEP_ARCHIVE, COLD_ARCHIVE):
            archive = RunLogArchive.from_bytes(make_archive(entries))
            assert archive.merged_for("Python 3.10") == "set up\none failure\n"

    def test_system_txt_is_not_exposed(self) -> None:
        """It is the runner's scheduling log; the decision was not to keep it."""
        archive = RunLogArchive.from_bytes(make_archive(STEP_ARCHIVE))

        assert "Requested labels" not in archive.steps_for("Python 3.10")[1].content
        assert all(
            "Requested labels" not in text for text in archive.merged.values()
        ), "a merged file is the job's log, not system.txt"

    def test_the_bom_is_kept_for_the_storage_layer_to_strip(self) -> None:
        archive = RunLogArchive.from_bytes(make_archive(STEP_ARCHIVE))

        assert archive.steps_for("Python 3.10")[1].content.startswith("\ufeff")

    def test_an_unknown_entry_is_ignored_rather_than_guessed(self) -> None:
        entries = dict(STEP_ARCHIVE)
        entries["Python 3.10/notes.md"] = "not a log\n"
        entries["README.txt"] = "not a job\n"

        archive = RunLogArchive.from_bytes(make_archive(entries))

        assert sorted(archive.steps_for("Python 3.10")) == [1, 11]
        assert archive.job_names == {"Python 3.10", "Python 3.12"}

    def test_a_repeated_step_number_keeps_the_first(self) -> None:
        entries = {
            "j/1_First.txt": "first\n",
            "j/1_Second.txt": "second\n",
        }

        archive = RunLogArchive.from_bytes(make_archive(entries))

        assert archive.steps_for("j")[1].content == "first\n"

    def test_a_body_that_is_not_a_zip_is_a_failure_not_an_empty_archive(self) -> None:
        with pytest.raises(zipfile.BadZipFile):
            RunLogArchive.from_bytes(b"not a zip")


class TestTheGroupAndTheTransport:
    async def test_the_archive_is_parsed_for_the_token_bound_repository(
        self, read_transport
    ) -> None:
        read_transport.archive_bytes = make_archive(STEP_ARCHIVE)

        archive = await GithubRuns(read_transport).get_run_logs(REPO, RUN)

        assert archive.form == "step"
        assert read_transport.calls == [
            ("get_run_logs", {"token": None, "repo": REPO, "run_id": RUN})
        ]

    async def test_a_missing_archive_stays_an_error(self, read_transport) -> None:
        read_transport.error = zipfile.BadZipFile("no archive")
        read_transport.archive_bytes = b""

        with pytest.raises(zipfile.BadZipFile):
            await GithubRuns(read_transport).get_run_logs(REPO, RUN)

    def test_the_client_asks_for_the_runs_logs_endpoint(self) -> None:
        """A typo here would 404 as "no logs" and look like an expired archive."""
        import asyncio

        from rhosocial.github_ci_tracker.services.github.client import AsyncGithubClient

        seen: list[str] = []

        class _Transport:
            is_closed = False

            async def request(self, method: str, url: str, **_: object):
                import httpx2

                seen.append(url)
                return httpx2.Response(
                    200,
                    content=make_archive(COLD_ARCHIVE),
                    request=httpx2.Request(method, "https://api.github.com"),
                )

            async def aclose(self) -> None:
                return None

        client = AsyncGithubClient(max_retries=1)
        client._client = _Transport()  # noqa: SLF001 - the transport seam under test

        asyncio.run(client.get_run_logs(None, REPO, RUN))

        assert seen == [f"/repos/{REPO}/actions/runs/{RUN}/logs"]
