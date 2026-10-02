"""
Credential resolution for ``DuckAPI.auto_register`` via AWS Secrets
Manager.

Usage modes
-----------
Hardcoded secret reference in the code::

    duck.auto_register({
        "sharepoint": {
            "authentication": {
                "type": "aws",
                "region_name": "us-east-1",
                "secret_id": "prod/sharepoint/duckduck",
            },
        },
    })

Reference passed at runtime (env var, external config, etc.)::

    duck.auto_register({
        "sharepoint": {
            "authentication": {
                "type": "aws",
                "secret_id": os.environ["SP_SECRET_ID"],
            },
        },
    })

A specific named AWS profile (multiple accounts configured locally, e.g.
via ``aws configure --profile prod``)::

    duck.auto_register({
        "sharepoint": {
            "authentication": {
                "type": "aws",
                "profile_name": "prod",
                "secret_id": "prod/sharepoint/duckduck",
            },
        },
    })

``auto_register()`` builds a ``SecretsManager`` for you from
``region_name``/``profile_name`` in the ``authentication`` block; build
one directly only when you need to override it (tests, or reusing a
client across calls) via ``auto_register(secrets={"aws": SecretsManager(...)})``.
"""

import json
from typing import Any, Dict, Optional

try:
    import boto3
except ImportError:
    boto3 = None


class SecretsManager:
    """
    Thin wrapper over ``boto3.client('secretsmanager')`` with an
    in-memory cache — each secret is fetched at most once per process.

    The secret's ``SecretString`` must be plain JSON with the keys
    expected by the target wrapper's ``from_secret`` (see
    ``SharePoint.from_secret`` / ``InsightVM.from_secret``).

    Parameters
    ----------
    region_name : str, optional
        AWS region. Without this, uses the boto3 session's default
        configuration (environment variable, ``~/.aws/config``, etc.).
    profile_name : str, optional
        Named AWS profile to use (as configured via ``aws configure
        --profile <name>`` in ``~/.aws/credentials``/``~/.aws/config``) —
        for selecting a specific account when more than one is
        configured locally. Without this, uses the default profile (or
        whatever the environment/instance role otherwise resolves to).
    client : optional
        An already-built boto3 client (or a mock, for tests). When
        provided, ``region_name``/``profile_name`` are ignored and
        ``boto3`` doesn't need to be installed.
    """

    def __init__(
        self,
        region_name: Optional[str] = None,
        profile_name: Optional[str] = None,
        client: Optional[Any] = None,
    ):
        if client is None and boto3 is None:
            raise ImportError(
                "boto3 is required to use AWS Secrets Manager.\n"
                "Install with:  pip install \"duckduck[aws]\"\n"
                "or use authentication.type='local' in your config for offline mode."
            )
        self._client = client if client is not None else boto3.Session(
            profile_name=profile_name
        ).client("secretsmanager", region_name=region_name)
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get_secret(self, secret_id: str) -> Dict[str, Any]:
        """Fetches (and caches) a JSON secret by name or ARN."""
        if secret_id not in self._cache:
            response = self._client.get_secret_value(SecretId=secret_id)
            raw = response.get("SecretString")
            if raw is None:
                raise ValueError(
                    f"Secret '{secret_id}' has no SecretString "
                    "(SecretBinary is not supported)."
                )
            self._cache[secret_id] = json.loads(raw)
        return self._cache[secret_id]
