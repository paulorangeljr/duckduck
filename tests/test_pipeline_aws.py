import json
import sys
import types

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.pipeline import PipelineError, run_pipeline
from duckduck.pipeline import aws
from duckduck.pipeline.catalogs import GlueCatalog
from duckduck.pipeline.settings import lake_problems
from duckduck.pipeline.sip import _filesystem

pytest.importorskip("pyarrow")

KEYS = {"type": "local", "aws_access_key_id": "AKIAEXAMPLE", "aws_secret_access_key": "never-printed",
        "aws_session_token": "tok"}


class FakeSession:
    """boto3.Session stand-in: records how it was built; STS hands back temporary keys."""
    made = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.region_name = kwargs.get("region_name") or "us-east-2"
        FakeSession.made.append({k: v for k, v in kwargs.items() if v is not None})

    def client(self, name):
        session = self

        class STS:
            def assume_role(self, **args):
                session.assumed = args
                return {"Credentials": {"AccessKeyId": "ASIATEMP", "SecretAccessKey": "temp-secret",
                                        "SessionToken": "temp-token"}}
        return STS() if name == "sts" else types.SimpleNamespace(name=name, session=self)

    def get_credentials(self):
        frozen = types.SimpleNamespace(access_key="AKIAPROFILE", secret_key="profile-secret", token=None)
        return types.SimpleNamespace(get_frozen_credentials=lambda: frozen)


@pytest.fixture
def fake_boto3(monkeypatch):
    FakeSession.made = []
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(Session=FakeSession))
    return FakeSession


def test_an_account_from_keys_a_profile_or_a_role(fake_boto3):
    keys = aws.account_of({"region": "eu-west-1", "authentication": KEYS})
    assert (keys.access_key, keys.session_token, keys.region) == ("AKIAEXAMPLE", "tok", "eu-west-1")
    profile = aws.account_of({"profile": "data-prod"})
    assert profile.access_key == "AKIAPROFILE" and profile.region == "us-east-2"
    assert fake_boto3.made[-1] == {"profile_name": "data-prod"}
    role = aws.account_of({"profile": "ops", "role_arn": "arn:aws:iam::123456789012:role/lake-writer",
                           "external_id": "x1"})
    assert (role.access_key, role.session_token) == ("ASIATEMP", "temp-token")
    assert role.describe() == "role arn:aws:iam::123456789012:role/lake-writer · us-east-2"
    assert aws.account_of({"region": "us-east-1"}).has_keys is False  # region only: the default chain's keys
    assert aws.account_of(None) is None


def test_the_lake_block_is_checked():
    assert lake_problems({"aws": {"profile": "p", "account": "x"}}) == [
        "lake.aws: unknown key(s) ['account'] — it takes "
        "['authentication', 'external_id', 'profile', 'region', 'role_arn', 'session_name']"]
    assert lake_problems({"aws": {"profile": ""}}) == ["lake.aws.profile is text"]
    with pytest.raises(PipelineError, match="gave no aws_access_key_id"):
        aws.account_of({"authentication": {"type": "local", "user": "x"}})


def test_s3_paths_use_the_account_and_local_paths_stay_local(tmp_path):
    import pyarrow.fs as pafs

    account = aws.AwsAccount(region="eu-west-1", access_key="AKIA", secret_key="s", session_token="t")
    with aws.using(account):
        fs, path = _filesystem("s3://raw-layer/servicenow/incident")
        local, _ = _filesystem(str(tmp_path))
    assert isinstance(fs, pafs.S3FileSystem) and fs.region == "eu-west-1"
    assert path == "raw-layer/servicenow/incident"
    assert isinstance(local, pafs.LocalFileSystem)
    assert aws.delta_options(account) == {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s",
                                          "AWS_SESSION_TOKEN": "t", "AWS_REGION": "eu-west-1"}


def test_a_glue_catalog_without_its_own_credentials_uses_the_lake_account(fake_boto3):
    account = aws.AwsAccount(region="eu-west-1", access_key="AKIA", secret_key="s")
    with aws.using(account):
        GlueCatalog("glue").client
    assert fake_boto3.made[-1] == {"region_name": "eu-west-1", "aws_access_key_id": "AKIA",
                                   "aws_secret_access_key": "s"}
    with aws.using(account):  # its own profile wins
        GlueCatalog("glue", profile="catalog-account", region="us-east-1").client
    assert fake_boto3.made[-1] == {"profile_name": "catalog-account", "region_name": "us-east-1"}


def test_a_run_writes_with_the_lake_account_and_says_so(tmp_path):
    seen = []

    def hosts():
        seen.append(aws.current())
        return pd.DataFrame({"hostname": ["a", "b"]})

    duck = DuckAPI()
    duck.register_api_function("hosts", hosts)
    config = tmp_path / "duckduck.json"
    config.write_text(json.dumps({"services": {}, "lake": {
        "layers": {"raw": str(tmp_path / "raw")},
        "aws": {"region": "sa-east-1", "authentication": KEYS}}}))
    run = run_pipeline({"pipeline": "h", "sql": "SELECT * FROM hosts", "layer": "raw",
                        "target": {"database": "inv", "table_name": "hosts"}}, duck=duck, config_path=str(config))
    assert seen[0].region == "sa-east-1" and seen[0].access_key == "AKIAEXAMPLE"
    assert aws.current() is None  # only while the run lasts
    assert "aws: keys from its authentication · sa-east-1" in run.report()
    assert "never-printed" not in run.report()
    # a pipeline file's own "aws" wins over duckduck.json's
    run = run_pipeline({"pipeline": "h", "sql": "SELECT * FROM hosts", "layer": "raw", "aws": {"region": "us-west-2"},
                        "target": {"database": "inv", "table_name": "hosts"}}, duck=duck, config_path=str(config))
    assert seen[-1].region == "us-west-2" and not seen[-1].has_keys
