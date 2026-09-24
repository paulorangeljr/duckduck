"""
Registro central dos wrappers de API suportados por
``DuckAPI.auto_register`` (ver ``core.py``).

Cada entrada mapeia um "type" (``"sharepoint"``, ``"insightvm"``) para:

- ``factory``          : classmethod que constrói a instância a partir de
                          um dict de credenciais (``Wrapper.from_secret``)
- ``tables``            : ``{nome_da_tabela: nome_do_método}`` para ``sql()``
- ``streaming_tables``  : ``{nome_da_tabela: nome_do_método}`` para ``stream()``

Ao adicionar um novo wrapper (ver seção "Adding a new API wrapper" do
CLAUDE.md), acrescente também um ``from_secret`` nele e uma entrada aqui
para que fique disponível em ``DuckAPI.auto_register()``.
"""

from typing import Any, Callable, Dict, NamedTuple

from .rapid7 import InsightVM
from .sharepoint import SharePoint


class ServiceSpec(NamedTuple):
    factory: Callable[..., Any]
    tables: Dict[str, str]
    streaming_tables: Dict[str, str]


SERVICE_REGISTRY: Dict[str, ServiceSpec] = {
    "sharepoint": ServiceSpec(
        factory=SharePoint.from_secret,
        tables={
            "sites": "sites",
            "lists": "lists",
            "list_columns": "list_columns",
            "list_items": "list_items",
            "drives": "drives",
            "drive_items": "drive_items",
            "search_files": "search_files",
            "file_versions": "file_versions",
        },
        streaming_tables={
            "sites": "iter_sites",
            "lists": "iter_lists",
            "list_items": "iter_list_items",
            "drive_items": "iter_drive_items",
        },
    ),
    "insightvm": ServiceSpec(
        factory=InsightVM.from_secret,
        tables={
            "assets": "assets",
            "vulnerabilities": "vulnerabilities",
            "asset_vulnerabilities": "asset_vulnerabilities",
            "sites": "sites",
            "scan_engines": "scan_engines",
            "scans": "scans",
            "report_templates": "report_templates",
            "reports": "reports",
            "tags": "tags",
            "asset_groups": "asset_groups",
            "users": "users",
            "policies": "policies",
            "policy_rules": "policy_rules",
            "remediation_projects": "remediation_projects",
        },
        streaming_tables={
            "assets": "iter_assets",
            "vulnerabilities": "iter_vulnerabilities",
            "asset_vulnerabilities": "iter_asset_vulnerabilities",
            "sites": "iter_sites",
            "scans": "iter_scans",
            "policy_rules": "iter_policy_rules",
        },
    ),
}
