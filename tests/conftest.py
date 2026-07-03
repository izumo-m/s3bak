"""Test fixtures for the s3bak suite.

The suite drives s3bak in-process (cli.main) against moto's in-memory S3 mock,
so it is hermetic - no Docker, no network, no credentials. (The scripts/ MinIO
stack remains for manual testing against a real endpoint.)

Each test gets a fresh mock, a unique prefix in the test bucket, and a temp
local tree; output is captured with capfd so subprocess output (the `diff`
child) is seen alongside s3bak's own writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from s3bak import cli

PROFILE = "s3bak-test"
BUCKET = "s3bak-test-bucket"
REGION = "us-east-1"


@dataclass
class Result:
    rc: int
    out: str
    err: str


@pytest.fixture
def s3(monkeypatch: Any, tmp_path: Path) -> Any:
    # An isolated AWS config carrying the named profile with dummy credentials,
    # so the store's `boto3.Session(profile_name=...)` resolves under moto
    # without reading the developer's ~/.aws or a sourced MinIO env. A named
    # profile does NOT fall back to AWS_* env credentials, so they live in the
    # profile here. Everything below runs inside the mock.
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    awscfg = tmp_path / "awsconfig"
    awscfg.write_text(
        f"[profile {PROFILE}]\n"
        f"region = {REGION}\n"
        f"aws_access_key_id = testing\n"
        f"aws_secret_access_key = testing\n"
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(awscfg))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "awscreds"))
    with mock_aws():
        client = boto3.Session(profile_name=PROFILE).client("s3")
        client.create_bucket(Bucket=BUCKET)
        yield client


class Workspace:
    """A backup workspace: a temp local tree plus a unique S3 prefix."""

    def __init__(self, root: Path, bucket: str, prefix: str, s3: Any, monkeypatch: Any, capfd: Any):
        self.root = root
        self.bucket = bucket
        self.prefix = prefix  # key prefix within the bucket (no s3:// scheme)
        self.s3 = s3
        self._monkeypatch = monkeypatch
        self._capfd = capfd
        self._config = root / "config.py"

    def write(self, relpath: str, content: str = "") -> Path:
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def config(self, entries: dict[str, dict[str, Any]], **settings: Any) -> None:
        body = (
            f'profile = "{PROFILE}"\n'
            f'prefix = "s3://{self.bucket}/{self.prefix}"\n'
            f"entries = {entries!r}\n"
        )
        # Optional extra top-level settings (e.g. max_concurrency=7).
        for key, value in settings.items():
            body += f"{key} = {value!r}\n"
        self._config.write_text(body)
        self._monkeypatch.setenv("S3BAK_CONFIG", str(self._config))

    def run(self, *args: str, expect_rc: int | None = None) -> Result:
        # capfd captures at the fd level, so subprocess output (the `diff` child)
        # is captured alongside s3bak's own writes. Drain first so each call sees
        # only its own output.
        self._capfd.readouterr()
        try:
            code = cli.main(list(args)) or 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        captured = self._capfd.readouterr()
        res = Result(rc=code, out=captured.out, err=captured.err)
        if expect_rc is not None:
            assert res.rc == expect_rc, (
                f"rc={res.rc} (expected {expect_rc})\nout={res.out!r}\nerr={res.err!r}"
            )
        return res

    def keys(self) -> set[str]:
        """Object keys under the workspace prefix, relative to it."""
        found: set[str] = set()
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/"):
            for obj in page.get("Contents", []):
                found.add(obj["Key"][len(self.prefix) + 1 :])
        return found


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: Any, s3: Any, capfd: Any) -> Any:
    prefix = f"test/{uuid.uuid4().hex[:12]}"
    root = tmp_path / "work"
    root.mkdir()
    # No teardown: moto discards all state when the s3 fixture's mock exits.
    return Workspace(root, BUCKET, prefix, s3, monkeypatch, capfd)
