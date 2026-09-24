"""
InsightVM API wrapper para uso com DuckAPI.

Convenção de uso
----------------
A sintaxe ``FROM func(param=val)`` é reservada para parâmetros
**estruturais** — aqueles que a API precisa para montar a URL ou o
corpo da requisição e que *não* correspondem a colunas do resultado
(ex: ``asset_id`` que vira ``/assets/{id}/vulnerabilities``).

Filtros de colunas normais (``hostname``, ``severity``, ``status``,
etc.) pertencem ao ``WHERE`` do SQL e são injetados na função via
push-down automático do DuckAPI.

Exemplos de queries suportadas
-------------------------------
::

    # Assets paginados — limit vai como page_size da API
    SELECT * FROM assets LIMIT 25

    # Filtro de hostname via WHERE → push-down para a API
    SELECT * FROM assets WHERE hostname = 'web-prod'

    # Combinando WHERE e LIMIT
    SELECT * FROM assets WHERE hostname = 'web-prod' LIMIT 50

    # asset_id é estrutural (monta a URL) → inline obrigatório
    SELECT * FROM asset_vulnerabilities(asset_id=42) LIMIT 100

    # Filtro de coluna via WHERE em endpoint estrutural
    SELECT * FROM asset_vulnerabilities(asset_id=42)
     WHERE severity = 'critical'

    # Políticas: policy_id estrutural, status via WHERE
    SELECT * FROM policy_rules(policy_id=7) WHERE status = 'failed'
"""

from typing import Any, Dict, List, Optional, Union

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class InsightVM:
    """
    Cliente para a API v3 do Rapid7 InsightVM / Nexpose.

    Parameters
    ----------
    host : str
        Hostname ou IP do console InsightVM (sem protocolo).
    username : str
    password : str
    verify : bool
        Verificação de certificado TLS. Padrão False para ambientes
        com certificados auto-assinados.
    default_page_size : int
        Tamanho de página padrão quando ``limit`` não é passado pelo
        DuckAPI.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify: bool = False,
        default_page_size: int = 500,
    ):
        self.base_url = f"https://{host}/api/3"
        self.verify = verify
        self.default_page_size = default_page_size

        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.verify = verify

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        params = params or {}
        r = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: Dict, params: Optional[Dict] = None) -> Dict:
        params = params or {}
        r = self.session.post(
            f"{self.base_url}{path}",
            params=params,
            json=body,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def _fetch(
        self,
        path: str,
        params: Optional[Dict] = None,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """
        Busca registros de um endpoint paginado.

        Se ``limit`` for fornecido, faz uma única requisição com
        ``size=limit`` (primeira página apenas) — sem iterar todas as
        páginas. Sem ``limit``, pagina completamente com ``default_page_size``.
        """
        params = params or {}

        if limit is not None:
            payload = self._get(path, {**params, "size": limit, "page": 0})
            return payload.get("resources", [])

        # Paginação completa
        page = 0
        all_resources: List[Dict] = []
        while True:
            payload = self._get(path, {**params, "size": self.default_page_size, "page": page})
            resources = payload.get("resources", [])
            all_resources.extend(resources)
            total_pages = payload.get("page", {}).get("totalPages", 1)
            page += 1
            if page >= total_pages:
                break

        return all_resources

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def assets(
        self,
        hostname: Optional[str] = None,
        ip: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista assets, com push-down opcional de hostname, ip e limit.

        Push-down de WHERE
        ------------------
        - ``hostname`` → busca por ``host-name contains <value>``
        - ``ip``       → busca por ``ip-address is <value>``

        Quando hostname ou ip são passados, usa o endpoint de busca
        ``/assets/search`` que suporta filtros server-side.
        Sem filtros, pagina ``/assets`` normalmente.

        Parameters
        ----------
        hostname : str, optional
            Fragmento de hostname para filtro server-side.
        ip : str, optional
            Endereço IP exato para filtro server-side.
        limit : int, optional
            Número máximo de registros (page_size da API).
        """
        if hostname or ip:
            filters = []
            if hostname:
                filters.append({
                    "field": "host-name",
                    "operator": "contains",
                    "value": hostname,
                })
            if ip:
                filters.append({
                    "field": "ip-address",
                    "operator": "is",
                    "value": ip,
                })

            body = {"filters": filters, "match": "all"}
            payload = self._post(
                "/assets/search",
                body,
                params={"size": limit or self.default_page_size, "page": 0},
            )
            return pd.json_normalize(
                payload.get("resources", []), sep="_"
            )

        resources = self._fetch("/assets", limit=limit)
        return pd.json_normalize(resources, sep="_")

    # ------------------------------------------------------------------
    # Vulnerabilities
    # ------------------------------------------------------------------

    def vulnerabilities(
        self,
        severity: Optional[str] = None,
        cvss_score: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista vulnerabilidades do banco de dados do InsightVM.

        Push-down de WHERE
        ------------------
        - ``severity``    → filtra client-side pelo campo severity
          (Critical, Severe, Moderate)
        - ``cvss_score``  → filtra client-side por cvss >= valor

        Parameters
        ----------
        severity : str, optional
            Nível de severidade exato (Critical, Severe, Moderate).
        cvss_score : float, optional
            Score CVSS mínimo.
        limit : int, optional
            Número máximo de registros retornados.
        """
        resources = self._fetch("/vulnerabilities", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if severity and not df.empty and "severity" in df.columns:
            df = df[df["severity"].str.lower() == severity.lower()]

        if cvss_score is not None and not df.empty:
            score_col = next(
                (c for c in df.columns if "cvss" in c.lower() and "score" in c.lower()),
                None,
            )
            if score_col:
                df = df[pd.to_numeric(df[score_col], errors="coerce") >= cvss_score]

        return df

    # ------------------------------------------------------------------
    # Vulnerabilities de um asset específico
    # ------------------------------------------------------------------

    def asset_vulnerabilities(
        self,
        asset_id: int,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Vulnerabilidades encontradas em um asset específico.

        Parameters
        ----------
        asset_id : int
            ID do asset (obrigatório).
        status : str, optional
            ``vulnerable``, ``vulnerable-version``, ``vulnerable-potential``.
        severity : str, optional
            Critical, Severe, Moderate.
        limit : int, optional
            Número máximo de registros.
        """
        resources = self._fetch(f"/assets/{asset_id}/vulnerabilities", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        if severity and not df.empty and "severity" in df.columns:
            df = df[df["severity"].str.lower() == severity.lower()]

        return df

    # ------------------------------------------------------------------
    # Sites
    # ------------------------------------------------------------------

    def sites(
        self,
        name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista sites de scan.

        Parameters
        ----------
        name : str, optional
            Fragmento do nome do site para filtro client-side.
        limit : int, optional
            Número máximo de registros.
        """
        resources = self._fetch("/sites", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "name" in df.columns:
            df = df[df["name"].str.contains(name, case=False, na=False)]

        return df

    # ------------------------------------------------------------------
    # Scan Engines
    # ------------------------------------------------------------------

    def scan_engines(
        self,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Lista scan engines registrados."""
        resources = self._fetch("/scan_engines", limit=limit)
        return pd.json_normalize(resources, sep="_")

    # ------------------------------------------------------------------
    # Scans
    # ------------------------------------------------------------------

    def scans(
        self,
        status: Optional[str] = None,
        site_id: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista execuções de scan.

        Parameters
        ----------
        status : str, optional
            running, finished, stopped, error, paused, aborted, unknown.
        site_id : int, optional
            Filtra scans de um site específico.
        limit : int, optional
            Número máximo de registros.
        """
        path = f"/sites/{site_id}/scans" if site_id else "/scans"
        resources = self._fetch(path, limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        return df

    # ------------------------------------------------------------------
    # Report Templates
    # ------------------------------------------------------------------

    def report_templates(
        self,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Lista templates de relatório disponíveis."""
        payload = self._get("/report_templates")
        resources = payload.get("resources", [payload] if isinstance(payload, dict) else payload)
        df = pd.json_normalize(resources, sep="_")
        if limit:
            df = df.head(limit)
        return df

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def reports(
        self,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Lista relatórios gerados."""
        resources = self._fetch("/reports", limit=limit)
        return pd.json_normalize(resources, sep="_")

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    def tags(
        self,
        name: Optional[str] = None,
        tag_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista tags de assets.

        Parameters
        ----------
        name : str, optional
            Fragmento do nome da tag para filtro client-side.
        tag_type : str, optional
            owner, location, custom, criticality.
        limit : int, optional
            Número máximo de registros.
        """
        resources = self._fetch("/tags", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "name" in df.columns:
            df = df[df["name"].str.contains(name, case=False, na=False)]

        if tag_type and not df.empty and "type" in df.columns:
            df = df[df["type"].str.lower() == tag_type.lower()]

        return df

    # ------------------------------------------------------------------
    # Asset Groups
    # ------------------------------------------------------------------

    def asset_groups(
        self,
        name: Optional[str] = None,
        group_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista grupos de assets.

        Parameters
        ----------
        name : str, optional
            Fragmento do nome do grupo.
        group_type : str, optional
            static ou dynamic.
        limit : int, optional
            Número máximo de registros.
        """
        resources = self._fetch("/asset_groups", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "name" in df.columns:
            df = df[df["name"].str.contains(name, case=False, na=False)]

        if group_type and not df.empty and "type" in df.columns:
            df = df[df["type"].str.lower() == group_type.lower()]

        return df

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def users(
        self,
        login: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista usuários do console.

        Parameters
        ----------
        login : str, optional
            Login exato para filtro client-side.
        limit : int, optional
            Número máximo de registros.
        """
        resources = self._fetch("/users", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if login and not df.empty and "login" in df.columns:
            df = df[df["login"].str.lower() == login.lower()]

        return df

    # ------------------------------------------------------------------
    # Policies (compliance)
    # ------------------------------------------------------------------

    def policies(
        self,
        name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista políticas de compliance disponíveis.

        Parameters
        ----------
        name : str, optional
            Fragmento do nome da política.
        limit : int, optional
            Número máximo de registros.
        """
        resources = self._fetch("/policies", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "title" in df.columns:
            df = df[df["title"].str.contains(name, case=False, na=False)]

        return df

    # ------------------------------------------------------------------
    # Policy Rules
    # ------------------------------------------------------------------

    def policy_rules(
        self,
        policy_id: int,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Regras de uma política de compliance.

        Parameters
        ----------
        policy_id : int
            ID da política (obrigatório).
        status : str, optional
            passed, failed, not-applicable.
        limit : int, optional
            Número máximo de registros.
        """
        resources = self._fetch(f"/policies/{policy_id}/rules", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        return df

    # ------------------------------------------------------------------
    # Remediation Projects
    # ------------------------------------------------------------------

    def remediation_projects(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista projetos de remediação.

        Parameters
        ----------
        status : str, optional
            active, expired, paused, completed.
        limit : int, optional
            Número máximo de registros.
        """
        resources = self._fetch("/remediation/projects", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        return df
