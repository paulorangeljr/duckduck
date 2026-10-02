"""Secret backends: ``get_secret(secret_id) -> dict``."""

from .aws import SecretsManager
from .azure import AzureKeyVaultSecrets

__all__ = ["SecretsManager", "AzureKeyVaultSecrets"]
