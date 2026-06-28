"""Test fixtures for the s3bak suite.

The suite drives s3bak in-process (cli.main) against a real S3 endpoint. It is
opt-in: set S3BAK_E2E_BUCKET (and the AWS_* / AWS_CONFIG_FILE pointing at the
endpoint) - `source scripts/minio-env.sh` does this for the local MinIO stack.
Each test gets a unique prefix in the bucket and a temp local tree, both torn
down afterwards.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest

from s3bak import cli

PROFILE = os.environ.get("S3BAK_E2E_PROFILE", "s3bak-minio")


def _e2e_bucket() -> str:
    bucket = os.environ.get("S3BAK_E2E_BUCKET")
    if not bucket:
        pytest.skip("S3BAK_E2E_BUCKET not set (run: source scripts/minio-env.sh)")
    return bucket


@dataclass
class Result:
    rc: int
    out: str
    err: str


@pytest.fixture(scope="session")
def s3() -> Any:
    _e2e_bucket()  # skip the whole suite if the endpoint is not configured
    return boto3.Session(profile_name=PROFILE).client("s3")


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

    def config(self, entries: dict[str, dict[str, Any]]) -> None:
        body = (
            f'profile = "{PROFILE}"\n'
            f'prefix = "s3://{self.bucket}/{self.prefix}"\n'
            f"entries = {entries!r}\n"
        )
        self._config.write_text(body)
        self._monkeypatch.setenv("S3BAK_CONFIG", str(self._config))

    def run(self, *args: str, expect_rc: int | None = None) -> Result:
        # capfd captures at the fd level, so subprocess output (the `show` /
        # `diff` children) is captured alongside s3bak's own writes. Drain first
        # so each call sees only its own output.
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
    bucket = _e2e_bucket()
    prefix = f"test/{uuid.uuid4().hex[:12]}"
    root = tmp_path / "work"
    root.mkdir()
    yield Workspace(root, bucket, prefix, s3, monkeypatch, capfd)

    # Teardown: delete every object under the prefix.
    paginator = s3.get_paginator("list_objects_v2")
    objs = [
        {"Key": o["Key"]}
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/")
        for o in page.get("Contents", [])
    ]
    for i in range(0, len(objs), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": objs[i : i + 1000]})
