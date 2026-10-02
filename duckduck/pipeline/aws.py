"""
The AWS account a pipeline writes with — declared once, in duckduck.json's
``"lake"`` (or in one pipeline file, to write somewhere else):

    "lake": {
      "aws": {"profile": "data-prod", "region": "us-east-1"}
    }

    "aws": {"region": "us-east-1", "role_arn": "arn:aws:iam::123456789012:role/lake-writer"}
    "aws": {"region": "us-east-1", "authentication": {"type": "aws", "secret_id": "prod/lake-writer"}}

``profile`` — a profile of ``~/.aws`` (several accounts on one machine);
``authentication`` — keys from any connector-style block (``local`` /
``aws`` / ``azure`` secret: ``aws_access_key_id`` / ``aws_secret_access_key``
/ ``aws_session_token``); ``role_arn`` (+ ``external_id``,
``session_name``) — a role assumed with whichever of those (or the default
chain) — the usual way to write into another account. Nothing set: the
default chain (environment, the default profile, the machine's role), as
before.

It's applied to what the pipeline itself writes and reads in the lake: the
files (pyarrow S3 and deltalake), the state, the sip, and a Glue catalog
that doesn't say its own ``profile`` / ``authentication``. The connectors
the SQL reads keep their own credentials; Spark writes with the cluster's.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from .spec import PipelineError

AWS_KEYS = {"profile", "region", "role_arn", "external_id", "session_name", "authentication"}


@dataclass(frozen=True)
class AwsAccount:
    region: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    session_token: Optional[str] = None
    profile: Optional[str] = None  # kept for what reads profiles itself (a Glue catalog's own boto3 session)
    role_arn: Optional[str] = None  # the role assumed (the keys are its temporary ones)

    @property
    def has_keys(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def describe(self) -> str:
        how = (f"role {self.role_arn}" if self.role_arn else f"profile {self.profile}" if self.profile
               else "keys from its authentication" if self.has_keys else "the default chain")
        return how + (f" · {self.region}" if self.region else "")


_ACCOUNT: ContextVar[Optional[AwsAccount]] = ContextVar("duckduck_pipeline_aws", default=None)


def current() -> Optional[AwsAccount]:
    """The account of the pipeline running now (None: the default chain)."""
    return _ACCOUNT.get()


@contextlib.contextmanager
def using(account: Optional[AwsAccount]) -> Iterator[None]:
    token = _ACCOUNT.set(account)
    try:
        yield
    finally:
        _ACCOUNT.reset(token)


def aws_problems(block: Any, where: str = "aws") -> List[str]:
    if block is None:
        return []
    if not isinstance(block, dict):
        return [f"{where} is an object: {{\"profile\": …}} | {{\"role_arn\": …}} | {{\"authentication\": {{…}}}}"]
    problems = []
    unknown = sorted(set(block) - AWS_KEYS)
    if unknown:
        problems.append(f"{where}: unknown key(s) {unknown} — it takes {sorted(AWS_KEYS)}")
    for k in AWS_KEYS - {"authentication"}:
        if block.get(k) is not None and not (isinstance(block[k], str) and block[k]):
            problems.append(f"{where}.{k} is text")
    if block.get("authentication") is not None and not isinstance(block["authentication"], dict):
        problems.append(f"{where}.authentication is a block like a connector's: {{\"type\": \"aws\", "
                        "\"secret_id\": \"…\"}}")
    return problems


def _first(d: Dict[str, Any], *names: str) -> Optional[str]:
    for n in names:
        if d.get(n):
            return str(d[n])
    return None


def _session(**kwargs: Any):
    try:
        import boto3
    except ImportError:
        raise PipelineError("an AWS profile or role for the lake needs boto3: pip install \"duckduck[aws]\"") from None
    return boto3.Session(**{k: v for k, v in kwargs.items() if v})


def account_of(block: Optional[Dict[str, Any]], duck: Any = None) -> Optional[AwsAccount]:
    """The account an ``"aws"`` block says (credentials resolved now), or None when there's no block."""
    if not block:
        return None
    problems = aws_problems(block)
    if problems:
        raise PipelineError("; ".join(problems))
    region = block.get("region")
    profile = block.get("profile")
    keys: Dict[str, Any] = {}
    if block.get("authentication"):
        if duck is None:
            from ..core import DuckAPI

            duck = DuckAPI()
        secret = duck.resolve_credentials(block["authentication"], name="lake aws")
        keys = {
            "access_key": _first(secret, "aws_access_key_id", "access_key_id", "access_key"),
            "secret_key": _first(secret, "aws_secret_access_key", "secret_access_key", "secret_key"),
            "session_token": _first(secret, "aws_session_token", "session_token"),
        }
        if not (keys["access_key"] and keys["secret_key"]):
            raise PipelineError("lake aws: the authentication block gave no aws_access_key_id / "
                                "aws_secret_access_key (use \"$secret.<key>\" to name them)")
        region = region or _first(secret, "region_name", "region")
    if block.get("role_arn"):
        session = _session(profile_name=None if keys else profile, region_name=region,
                           aws_access_key_id=keys.get("access_key"), aws_secret_access_key=keys.get("secret_key"),
                           aws_session_token=keys.get("session_token"))
        args = {"RoleArn": block["role_arn"], "RoleSessionName": block.get("session_name") or "duckduck-pipeline"}
        if block.get("external_id"):
            args["ExternalId"] = block["external_id"]
        try:
            creds = session.client("sts").assume_role(**args)["Credentials"]
        except Exception as exc:  # noqa: BLE001 — said in a sentence, with the role
            raise PipelineError(f"lake aws: couldn't assume {block['role_arn']}: {exc}") from None
        return AwsAccount(region=region or session.region_name, access_key=creds["AccessKeyId"],
                          secret_key=creds["SecretAccessKey"], session_token=creds["SessionToken"],
                          role_arn=block["role_arn"])
    if keys:
        return AwsAccount(region=region, **keys)
    if profile:
        session = _session(profile_name=profile, region_name=region)
        creds = session.get_credentials()
        if creds is None:
            raise PipelineError(f"lake aws: the profile {profile!r} has no credentials on this machine")
        frozen = creds.get_frozen_credentials()
        return AwsAccount(region=region or session.region_name, access_key=frozen.access_key,
                          secret_key=frozen.secret_key, session_token=frozen.token, profile=profile)
    return AwsAccount(region=region)


def is_s3(location: str) -> bool:
    return location.split("://", 1)[0].lower() in ("s3", "s3a", "s3n") if "://" in location else False


def s3_filesystem(location: str, account: AwsAccount):
    """A pyarrow S3 filesystem with the account's keys / region, and the path inside it (bucket/key)."""
    import pyarrow.fs as pafs

    kwargs: Dict[str, Any] = {}
    if account.has_keys:
        kwargs.update(access_key=account.access_key, secret_key=account.secret_key)
        if account.session_token:
            kwargs["session_token"] = account.session_token
    if account.region:
        kwargs["region"] = account.region
    return pafs.S3FileSystem(**kwargs), location.split("://", 1)[1]


def delta_options(account: Optional[AwsAccount]) -> Dict[str, str]:
    """deltalake's storage options for the account (a target's own ``storage_options`` win over these)."""
    if account is None:
        return {}
    out: Dict[str, str] = {}
    if account.has_keys:
        out.update(AWS_ACCESS_KEY_ID=account.access_key, AWS_SECRET_ACCESS_KEY=account.secret_key)
        if account.session_token:
            out["AWS_SESSION_TOKEN"] = account.session_token
    if account.region:
        out["AWS_REGION"] = account.region
    return out
