"""
Credential resolution for ``DuckAPI.auto_register`` via Azure Key Vault.

Counterpart to ``duckduck.secrets.SecretsManager`` (AWS Secrets Manager):
same ``get_secret(secret_id) -> dict`` contract, so ``auto_register()``
treats both backends the same way — selected per service via
``authentication.type: "azure"`` in the config, never instantiated
directly by most callers.

Authentication to the vault itself uses ``azure.identity.DefaultAzureCredential``
(environment variables, managed identity, ``az login``, etc.) rather than a
hardcoded client secret in the config — that would just move the
chicken-and-egg secrets problem one level down instead of solving it.

Usage
-----
::

    from duckduck import DuckAPI

    duck = DuckAPI()
    duck.auto_register({
        "sharepoint": {
            "connector": "sharepoint",
            "authentication": {
                "type": "azure",
                "vault_url": "https://my-vault.vault.azure.net/",
                "secret_id": "prod-sharepoint",
            },
        },
    })
"""

import json
from typing import Any, Dict, Optional

try:
    from azure.identity import DefaultAzureCredential
except ImportError:
    DefaultAzureCredential = None

try:
    from azure.keyvault.secrets import SecretClient
except ImportError:
    SecretClient = None


class AzureKeyVaultSecrets:
    """
    Thin wrapper over ``azure.keyvault.secrets.SecretClient`` with an
    in-memory cache — each secret is fetched at most once per process.

    The secret's value must be a JSON object with the keys expected by
    the target connector's ``from_secret`` (see ``SharePoint.from_secret``
    / ``InsightVM.from_secret``), same as an AWS Secrets Manager
    ``SecretString``.

    Parameters
    ----------
    vault_url : str
        E.g. ``"https://my-vault.vault.azure.net/"``.
    tenant_id : str, optional
        Azure AD tenant to authenticate against, when the caller (or the
        machine's ``az login`` session) has access to more than one.
        Passed to ``DefaultAzureCredential`` as both
        ``interactive_browser_tenant_id`` and via
        ``additionally_allowed_tenants`` — covers the interactive-browser
        and CLI/env credentials in the default chain, but not every
        possible one; for full control over tenant selection, build the
        credential yourself and pass it via ``credential=``.
    credential : optional
        An azure-identity credential. Defaults to
        ``DefaultAzureCredential()`` (env vars, managed identity,
        ``az login``, etc.) — the vault itself isn't meant to need its
        own hardcoded secret in the config.
    client : optional
        An already-built ``SecretClient`` (or a mock, for tests). When
        provided, ``vault_url``/``tenant_id``/``credential`` are ignored
        and the ``azure-identity``/``azure-keyvault-secrets`` packages
        don't need to be installed.
    """

    def __init__(
        self,
        vault_url: Optional[str] = None,
        tenant_id: Optional[str] = None,
        credential: Optional[Any] = None,
        client: Optional[Any] = None,
    ):
        if client is None and SecretClient is None:
            raise ImportError(
                "azure-identity and azure-keyvault-secrets are required to use "
                "Azure Key Vault.\n"
                "Install with:  pip install \"duckduck[azure]\"\n"
                "or use authentication.type='local' in your config for offline mode."
            )
        if credential is None and client is None:
            credential = DefaultAzureCredential(
                **(
                    {
                        "interactive_browser_tenant_id": tenant_id,
                        "additionally_allowed_tenants": [tenant_id],
                    }
                    if tenant_id
                    else {}
                )
            )
        self._client = client if client is not None else SecretClient(
            vault_url=vault_url, credential=credential
        )
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get_secret(self, secret_id: str) -> Dict[str, Any]:
        """Fetches (and caches) a secret by name, parsing its value as JSON."""
        if secret_id not in self._cache:
            raw = self._client.get_secret(secret_id).value
            try:
                self._cache[secret_id] = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Secret '{secret_id}' in Azure Key Vault is not valid JSON. "
                    "Store it as a JSON object with the keys your connector needs "
                    "(see SharePoint.from_secret / InsightVM.from_secret)."
                ) from exc
        return self._cache[secret_id]
