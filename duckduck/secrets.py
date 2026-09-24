"""
Resolução de credenciais para ``DuckAPI.auto_register`` via AWS Secrets
Manager, com fallback para credenciais fornecidas diretamente (offline).

Modos de uso
------------
Referência de segredo hardcoded no código::

    from duckduck import DuckAPI, SecretsManager

    duck = DuckAPI()
    duck.auto_register(
        {"sharepoint": {"secret_id": "prod/sharepoint/duckduck"}},
        secrets=SecretsManager(region_name="us-east-1"),
    )

Referência passada em runtime (env var, config externa, etc.)::

    duck.auto_register(
        {"sharepoint": {"secret_id": os.environ["SP_SECRET_ID"]}},
        secrets=SecretsManager(),
    )

Offline — sem tocar o AWS Secrets Manager::

    duck.auto_register({
        "sharepoint": {
            "credentials": {
                "tenant_id": "...", "client_id": "...", "client_secret": "...",
            },
        },
    })
"""

import json
from typing import Any, Dict, Optional

try:
    import boto3
except ImportError:
    boto3 = None


class SecretsManager:
    """
    Wrapper fino sobre ``boto3.client('secretsmanager')`` com cache em
    memória — cada segredo é buscado no máximo uma vez por processo.

    O ``SecretString`` do segredo deve ser um JSON plano com as chaves
    esperadas pelo ``from_secret`` do wrapper de destino (ver
    ``SharePoint.from_secret`` / ``InsightVM.from_secret``).

    Parameters
    ----------
    region_name : str, optional
        Região AWS. Sem isso, usa a configuração padrão da sessão boto3
        (variável de ambiente, ``~/.aws/config``, etc.).
    client : optional
        Cliente boto3 já construído (ou um mock, para testes). Quando
        fornecido, ``region_name`` é ignorado e ``boto3`` não precisa
        estar instalado.
    """

    def __init__(self, region_name: Optional[str] = None, client: Optional[Any] = None):
        if client is None and boto3 is None:
            raise ImportError(
                "boto3 é necessário para usar AWS Secrets Manager.\n"
                "Instale com:  pip install \"duckduck[aws]\"\n"
                "ou use credentials= em auto_register() para modo offline."
            )
        self._client = client if client is not None else boto3.client(
            "secretsmanager", region_name=region_name
        )
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get_secret(self, secret_id: str) -> Dict[str, Any]:
        """Busca (e cacheia) um segredo JSON pelo nome ou ARN."""
        if secret_id not in self._cache:
            response = self._client.get_secret_value(SecretId=secret_id)
            raw = response.get("SecretString")
            if raw is None:
                raise ValueError(
                    f"Segredo '{secret_id}' não tem SecretString "
                    "(SecretBinary não é suportado)."
                )
            self._cache[secret_id] = json.loads(raw)
        return self._cache[secret_id]
