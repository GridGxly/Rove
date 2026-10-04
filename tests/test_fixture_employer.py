"""Prove that CI's employer rejects bad requests independently of Rove's own logs."""

import hashlib
import threading
from contextlib import contextmanager

import httpx
import pytest

from rove import benchmark


@contextmanager
def employer():
    server = benchmark.FixtureServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            yield client, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def application():
    fields = [(name, (None, value)) for name, value in benchmark.FIXTURE_ANSWERS.items()]
    return [*fields, ("resume", ("resume.pdf", benchmark.FIXTURE_RESUME, "application/pdf"))]


@pytest.mark.parametrize("fault", ["empty", "missing", "wrong_name", "resume", "duplicate"])
def test_employer_rejects_incomplete_incorrect_or_duplicate_fields(fault):
    fields = application()
    if fault == "missing":
        fields = [f for f in fields if f[0] != "email"]
    elif fault == "wrong_name":
        fields[0] = ("first", (None, "Wrong applicant"))
    elif fault == "resume":
        fields[-1] = ("resume", ("resume.pdf", b"different bytes", "application/pdf"))
    elif fault == "duplicate":
        fields.append(fields[0])
    with employer() as (client, server):
        url = benchmark.FIXTURE_JOB + "/apply"
        result = client.post(url, json={}) if fault == "empty" else client.post(url, files=fields)
        assert result.status_code == 422
        assert server.receipts == []
        assert client.get(url + "/done").status_code == 404


def test_employer_records_received_bytes_and_rejects_a_second_submission():
    with employer() as (client, server):
        url = benchmark.FIXTURE_JOB + "/apply"
        assert client.post(url, files=application()).status_code == 200
        assert client.get(url + "/done").status_code == 200
        assert client.post(url, files=application()).status_code == 409
        assert len(server.receipts) == 1
        assert server.receipts[0]["fields"] == benchmark.FIXTURE_ANSWERS
        assert (
            server.receipts[0]["resume_sha256"]
            == hashlib.sha256(benchmark.FIXTURE_RESUME).hexdigest()
        )
