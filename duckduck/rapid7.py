"""
InsightVM API wrapper para uso com DuckAPI.

Cada método público corresponde a um endpoint da API v3 do InsightVM e
aceita os parâmetros de push-down que o DuckAPI injeta automaticamente:

- ``limit``   → tamanho da página (page_size da API)
- filtros de WHERE com o mesmo nome do parâmetro do método

Exemplos de queries suportadas
-------------------------------
::

    # Assets paginados
    SELECT * FROM assets LIMIT 25

    # Busca por hostname com push-down do filtro
    SELECT * FROM assets(hostname='web-prod')

    # Vulnerabilidades de um asset específico
    SELECT * FROM asset_vulnerabilities(asset_id=42) LIMIT 100

    # WHERE com push-down de severidade
    SELECT * FROM vulnerabilities WHERE severity = 'Critical' LIMIT 50

    # JOIN entre assets e vulnerabilidades
    SELECT a.ip, v.title, v.severity
    FROM assets(hostname='db') AS a
    JOIN asset_vulnerabilities(asset_id=a.id) AS v ON true

    # Políticas e resultados de compliance
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

    def _paginate(self, path: str, params: Optional[Dict] = None) -> List[Dict]:
        """Itera sobre todas as páginas de um endpoint paginado."""
        params = params or {}
        page = 0
        all_resources: List[Dict] = []

        while True:
            payload = self._get(path, {**params, "page": page})
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
        page_size = limit or self.default_page_size

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
                params={"size": page_size, "page": 0},
            )
            return pd.json_normalize(
                payload.get("resources", []), sep="_"
            )

        resources = self._paginate("/assets", {"size": page_size})
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
        page_size = limit or self.default_page_size
        resources = self._paginate("/vulnerabilities", {"size": page_size})

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

        if limit:
            df = df.head(limit)

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
        page_size = limit or self.default_page_size
        resources = self._paginate(
            f"/assets/{asset_id}/vulnerabilities",
            {"size": page_size},
        )

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        if severity and not df.empty and "severity" in df.columns:
            df = df[df["severity"].str.lower() == severity.lower()]

        if limit:
            df = df.head(limit)

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
        page_size = limit or self.default_page_size
        resources = self._paginate("/sites", {"size": page_size})

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "name" in df.columns:
            df = df[df["name"].str.contains(name, case=False, na=False)]

        if limit:
            df = df.head(limit)

        return df

    # ------------------------------------------------------------------
    # Scan Engines
    # ------------------------------------------------------------------

    def scan_engines(
        self,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Lista scan engines registrados."""
        page_size = limit or self.default_page_size
        resources = self._paginate("/scan_engines", {"size": page_size})
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
        page_size = limit or self.default_page_size

        if site_id:
            resources = self._paginate(f"/sites/{site_id}/scans", {"size": page_size})
        else:
            resources = self._paginate("/scans", {"size": page_size})

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        if limit:
            df = df.head(limit)

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
        page_size = limit or self.default_page_size
        resources = self._paginate("/reports", {"size": page_size})
        df = pd.json_normalize(resources, sep="_")
        if limit:
            df = df.head(limit)
        return df

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
        page_size = limit or self.default_page_size
        resources = self._paginate("/tags", {"size": page_size})

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "name" in df.columns:
            df = df[df["name"].str.contains(name, case=False, na=False)]

        if tag_type and not df.empty and "type" in df.columns:
            df = df[df["type"].str.lower() == tag_type.lower()]

        if limit:
            df = df.head(limit)

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
        page_size = limit or self.default_page_size
        resources = self._paginate("/asset_groups", {"size": page_size})

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "name" in df.columns:
            df = df[df["name"].str.contains(name, case=False, na=False)]

        if group_type and not df.empty and "type" in df.columns:
            df = df[df["type"].str.lower() == group_type.lower()]

        if limit:
            df = df.head(limit)

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
        page_size = limit or self.default_page_size
        resources = self._paginate("/users", {"size": page_size})

        df = pd.json_normalize(resources, sep="_")

        if login and not df.empty and "login" in df.columns:
            df = df[df["login"].str.lower() == login.lower()]

        if limit:
            df = df.head(limit)

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
        page_size = limit or self.default_page_size
        resources = self._paginate("/policies", {"size": page_size})

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "title" in df.columns:
            df = df[df["title"].str.contains(name, case=False, na=False)]

        if limit:
            df = df.head(limit)

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
        page_size = limit or self.default_page_size
        resources = self._paginate(
            f"/policies/{policy_id}/rules",
            {"size": page_size},
        )

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        if limit:
            df = df.head(limit)

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
        page_size = limit or self.default_page_size
        resources = self._paginate("/remediation/projects", {"size": page_size})

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        if limit:
            df = df.head(limit)

        return df
