"""
SharePoint wrapper para uso com DuckAPI.

Autentica via Microsoft Identity Platform (Azure AD) e consulta
SharePoint Lists e arquivos via Microsoft Graph API v1.0.

Modos de autenticação
---------------------
- Client secret:        ``SharePoint(tenant_id, client_id, client_secret)``
- Thumbprint + PEM key: ``SharePoint.from_thumbprint(...)``
- Arquivo PFX/P12:      ``SharePoint.from_pfx(...)``  [pip install cryptography]
- PEM cert + PEM key:   ``SharePoint.from_pem_cert(...)``  [pip install cryptography]

Exemplos de queries
-------------------
::

    sp   = SharePoint(tenant_id, client_id, client_secret)
    duck = DuckAPI()

    duck.register_api_function("sites",       sp.sites)
    duck.register_api_function("lists",       sp.lists)
    duck.register_api_function("list_items",  sp.list_items)
    duck.register_api_function("drives",      sp.drives)
    duck.register_api_function("drive_items", sp.drive_items)

    # Todos os sites — id retornado é o site_id dos outros métodos
    duck.sql("SELECT id, displayName, webUrl FROM sites").df()

    # Listas de um site (site_id é estrutural → inline)
    duck.sql("SELECT * FROM lists(site_id='abc123')").df()

    # Items com filtro de coluna via WHERE push-down seria client-side
    # porque o Graph não filtra campos customizados server-side
    duck.sql(
        "SELECT * FROM list_items(site_id='abc123', list_id='def456')"
        " WHERE Status = 'Active' LIMIT 50"
    ).df()

    # Arquivos de um drive
    duck.sql(
        "SELECT name, size, webUrl"
        " FROM drive_items(site_id='abc123', drive_id='ghi789')"
        " WHERE is_file = true"
    ).df()

    # Streaming de lista grande
    duck.register_streaming_function("list_items", sp.iter_list_items)
    for chunk in duck.stream(
        "SELECT * FROM list_items(site_id='abc123', list_id='def456')"
    ):
        display(chunk)
"""

import re
import threading
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd
import requests

try:
    import msal
except ImportError as exc:
    raise ImportError(
        "msal é necessário para autenticação SharePoint.\n"
        "Instale com:  pip install msal"
    ) from exc


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
DEFAULT_PAGE_SIZE = 200


class SharePoint:
    """
    Cliente para SharePoint via Microsoft Graph API.

    Ver docstring do módulo para exemplos completos.
    """

    # ------------------------------------------------------------------
    # Construtores
    # ------------------------------------------------------------------

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        default_page_size: int = DEFAULT_PAGE_SIZE,
    ):
        """Autenticação via client secret."""
        self._setup(tenant_id, client_id, client_secret, default_page_size)

    @classmethod
    def from_thumbprint(
        cls,
        tenant_id: str,
        client_id: str,
        thumbprint: str,
        private_key_pem: str,
        passphrase: Optional[bytes] = None,
        default_page_size: int = DEFAULT_PAGE_SIZE,
    ) -> "SharePoint":
        """
        Autenticação via thumbprint SHA-1 + chave privada PEM.

        Equivalente direto ao Python MSAL::

            {"thumbprint": "ABC123...", "private_key": "-----BEGIN PRIVATE KEY-----\\n..."}

        Parameters
        ----------
        thumbprint : str
            Fingerprint SHA-1 em hex (com ou sem ``:``, maiúsculo ou não).
        private_key_pem : str
            Conteúdo PEM da chave privada **ou** caminho para o arquivo ``.pem``/``.key``.
            Se a string não começar com ``-----``, é interpretada como caminho de arquivo.
        passphrase : bytes, optional
            Senha da chave privada, se criptografada.
        """
        import pathlib

        if not private_key_pem.strip().startswith("-----"):
            private_key_pem = pathlib.Path(private_key_pem).read_text()

        credential: Dict[str, Any] = {
            "thumbprint": thumbprint.replace(":", "").upper(),
            "private_key": private_key_pem,
        }
        if passphrase is not None:
            credential["passphrase"] = passphrase
        obj = cls.__new__(cls)
        obj._setup(tenant_id, client_id, credential, default_page_size)
        return obj

    @classmethod
    def from_pfx(
        cls,
        tenant_id: str,
        client_id: str,
        pfx_path: str,
        pfx_password: Optional[str] = None,
        default_page_size: int = DEFAULT_PAGE_SIZE,
    ) -> "SharePoint":
        """
        Autenticação via arquivo PFX/P12.

        Requer: ``pip install cryptography``
        """
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
            )
            from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12
        except ImportError as exc:
            raise ImportError(
                "cryptography é necessário para auth via PFX.\n"
                "Instale com:  pip install cryptography"
            ) from exc

        import pathlib

        pfx_bytes = pathlib.Path(pfx_path).read_bytes()
        pwd = pfx_password.encode() if pfx_password else None
        private_key, certificate, _ = load_pkcs12(pfx_bytes, pwd)

        pem_key = private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        ).decode()
        thumbprint = certificate.fingerprint(hashes.SHA1()).hex()

        return cls.from_thumbprint(
            tenant_id, client_id, thumbprint, pem_key,
            default_page_size=default_page_size,
        )

    @classmethod
    def from_pem_cert(
        cls,
        tenant_id: str,
        client_id: str,
        private_key_pem: str,
        cert_pem: str,
        default_page_size: int = DEFAULT_PAGE_SIZE,
    ) -> "SharePoint":
        """
        Autenticação via PEM (chave privada RSA + certificado X.509).

        Requer: ``pip install cryptography``
        """
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
        except ImportError as exc:
            raise ImportError(
                "cryptography é necessário para auth via PEM cert.\n"
                "Instale com:  pip install cryptography"
            ) from exc

        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        thumbprint = cert.fingerprint(hashes.SHA1()).hex()
        return cls.from_thumbprint(
            tenant_id, client_id, thumbprint, private_key_pem,
            default_page_size=default_page_size,
        )

    # ------------------------------------------------------------------
    # Inicialização interna
    # ------------------------------------------------------------------

    def _setup(
        self,
        tenant_id: str,
        client_id: str,
        credential: Any,
        default_page_size: int,
    ) -> None:
        self.default_page_size = default_page_size
        self._lock = threading.Lock()
        # ConfidentialClientApplication mantém cache de token em memória
        self._msal_app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=credential,
        )

    # ------------------------------------------------------------------
    # Token
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        with self._lock:
            # MSAL devolve token em cache se ainda válido
            result = self._msal_app.acquire_token_silent(GRAPH_SCOPE, account=None)
            if result and "access_token" in result:
                return result["access_token"]

            result = self._msal_app.acquire_token_for_client(scopes=GRAPH_SCOPE)
            if "error" in result:
                raise ValueError(
                    f"Falha na autenticação Azure AD: "
                    f"{result.get('error')} — {result.get('error_description')}"
                )
            return result["access_token"]

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, url: str) -> Dict:
        token = self._get_token()
        r = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _with_top(self, url: str, top: int) -> str:
        """Adiciona ou substitui $top na URL."""
        if "$top=" in url:
            return re.sub(r"\$top=\d+", f"$top={top}", url)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}$top={top}"

    def _iter_pages(self, url: str) -> Iterator[List[Dict]]:
        """
        Gerador que segue @odata.nextLink até o fim.

        Diferente do InsightVM (que usa page/totalPages), o Graph API
        usa @odata.nextLink para paginação. Faz yield de uma lista de
        recursos por página.
        """
        if "$top=" not in url:
            url = self._with_top(url, self.default_page_size)

        next_url: Optional[str] = url
        while next_url:
            payload = self._get(next_url)
            items = payload.get("value", [])
            if items:
                yield items
            next_url = payload.get("@odata.nextLink")

    def _fetch(self, url: str, limit: Optional[int] = None) -> List[Dict]:
        """
        Uma única requisição se limit definido; paginação completa caso contrário.
        """
        if limit is not None:
            payload = self._get(self._with_top(url, limit))
            return payload.get("value", [])

        all_items: List[Dict] = []
        for page in self._iter_pages(url):
            all_items.extend(page)
        return all_items

    # ------------------------------------------------------------------
    # DataFrame helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_list_items(items: List[Dict]) -> pd.DataFrame:
        """
        Eleva os campos do sub-objeto ``fields`` para colunas de primeiro nível.

        O Graph API retorna::

            {"id": "1", "fields": {"Title": "foo", "Status": "Active"}}

        Isso se torna::

            _item_id | _created_at | Title | Status
            1        | 2024-01-01  | foo   | Active

        Metadados internos do Graph ficam prefixados com ``_``.
        Os campos da lista (Title, Status, etc.) ficam no topo — permitindo
        ``WHERE Title = 'foo'`` sem qualificar com ``fields_``.
        """
        records = []
        for item in items:
            record: Dict[str, Any] = {
                "_item_id": item.get("id"),
                "_created_at": item.get("createdDateTime"),
                "_modified_at": item.get("lastModifiedDateTime"),
                "_web_url": item.get("webUrl"),
            }
            record.update(item.get("fields", {}))
            records.append(record)
        return pd.json_normalize(records, sep="_")

    # ------------------------------------------------------------------
    # Resolução de nome → ID
    # ------------------------------------------------------------------

    def _resolve_site(
        self,
        site_id: Optional[str],
        site_name: Optional[str],
    ) -> str:
        """Devolve site_id; faz lookup pelo displayName se só site_name fornecido."""
        if site_id:
            return site_id
        if not site_name:
            raise ValueError("Forneça site_id ou site_name.")
        all_sites = self._fetch(f"{GRAPH_BASE}/sites?search=*")
        name_lower = site_name.lower()
        for s in all_sites:
            if s.get("displayName", "").lower() == name_lower:
                return s["id"]
        raise ValueError(
            f"Site '{site_name}' não encontrado. "
            "Use SELECT * FROM sites para ver os nomes disponíveis."
        )

    def _resolve_list(
        self,
        site_id: str,
        list_id: Optional[str],
        list_name: Optional[str],
    ) -> str:
        """Devolve list_id; faz lookup pelo displayName se só list_name fornecido."""
        if list_id:
            return list_id
        if not list_name:
            raise ValueError("Forneça list_id ou list_name.")
        all_lists = self._fetch(f"{GRAPH_BASE}/sites/{site_id}/lists")
        name_lower = list_name.lower()
        for lst in all_lists:
            if lst.get("displayName", "").lower() == name_lower:
                return lst["id"]
        raise ValueError(
            f"Lista '{list_name}' não encontrada no site '{site_id}'. "
            "Use SELECT * FROM lists(site_id=...) para ver as listas disponíveis."
        )

    # ------------------------------------------------------------------
    # Sites
    # ------------------------------------------------------------------

    def sites(
        self,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lista todos os sites SharePoint acessíveis (requer Sites.Read.All).

        Use o ``id`` retornado como ``site_id``, ou o ``displayName``
        como ``site_name``, nos outros métodos.
        """
        url = f"{GRAPH_BASE}/sites?search=*"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def site_by_path(
        self,
        hostname: str,
        site_path: str,
    ) -> pd.DataFrame:
        """
        Busca um site pelo hostname e caminho.

        Parameters
        ----------
        hostname : str
            Ex: ``contoso.sharepoint.com``
        site_path : str
            Ex: ``/sites/marketing``
        """
        url = f"{GRAPH_BASE}/sites/{hostname}:{site_path}"
        return pd.json_normalize([self._get(url)], sep="_")

    # ------------------------------------------------------------------
    # SharePoint Lists
    # ------------------------------------------------------------------

    def lists(
        self,
        site_id: Optional[str] = None,
        site_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Todas as SharePoint Lists de um site.

        Parameters
        ----------
        site_id : str, optional
            ID do site. Use ``site_id`` **ou** ``site_name``.
        site_name : str, optional
            Nome de exibição do site (``displayName``). Faz lookup automático do ID.

        Exemplos
        --------
        ::

            # por ID (inline)
            duck.sql("SELECT * FROM lists(site_id='abc123')")

            # por nome (WHERE push-down)
            duck.sql("SELECT * FROM lists WHERE site_name = 'Intranet'")
        """
        sid = self._resolve_site(site_id, site_name)
        url = f"{GRAPH_BASE}/sites/{sid}/lists"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def list_columns(
        self,
        site_id: Optional[str] = None,
        list_id: Optional[str] = None,
        site_name: Optional[str] = None,
        list_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Colunas (schema) de uma SharePoint List.

        Aceita IDs diretos ou nomes de exibição (lookup automático).

        Parameters
        ----------
        site_id / site_name : str
            Identificação do site — forneça um dos dois.
        list_id / list_name : str
            Identificação da lista — forneça um dos dois.
        """
        sid = self._resolve_site(site_id, site_name)
        lid = self._resolve_list(sid, list_id, list_name)
        url = f"{GRAPH_BASE}/sites/{sid}/lists/{lid}/columns"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def list_items(
        self,
        site_id: Optional[str] = None,
        list_id: Optional[str] = None,
        site_name: Optional[str] = None,
        list_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Items de uma SharePoint List com campos expandidos.

        Os campos customizados da lista (Title, Status, etc.) aparecem como
        colunas de primeiro nível. Metadados internos do Graph ficam
        prefixados com ``_`` (``_item_id``, ``_created_at``, etc.).

        Aceita IDs diretos ou nomes de exibição (lookup automático).

        Parameters
        ----------
        site_id / site_name : str
            Identificação do site — forneça um dos dois.
        list_id / list_name : str
            Identificação da lista — forneça um dos dois.

        Exemplos
        --------
        ::

            # por IDs (inline)
            duck.sql("SELECT * FROM list_items(site_id='abc', list_id='def')")

            # por nomes (WHERE push-down — lookup automático)
            duck.sql(
                "SELECT * FROM list_items"
                " WHERE site_name = 'Intranet' AND list_name = 'Tarefas'"
            )
        """
        sid = self._resolve_site(site_id, site_name)
        lid = self._resolve_list(sid, list_id, list_name)
        url = f"{GRAPH_BASE}/sites/{sid}/lists/{lid}/items?expand=fields"
        return self._normalize_list_items(self._fetch(url, limit=limit))

    # ------------------------------------------------------------------
    # Drives / Files
    # ------------------------------------------------------------------

    def drives(
        self,
        site_id: Optional[str] = None,
        site_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Document libraries (drives) de um site.

        Parameters
        ----------
        site_id / site_name : str
            Identificação do site — forneça um dos dois.
        """
        sid = self._resolve_site(site_id, site_name)
        url = f"{GRAPH_BASE}/sites/{sid}/drives"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def drive_items(
        self,
        site_id: Optional[str] = None,
        drive_id: Optional[str] = None,
        site_name: Optional[str] = None,
        folder_path: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Arquivos e pastas dentro de um drive (document library).

        Colunas úteis: ``name``, ``size``, ``webUrl``, ``is_file``,
        ``file_mimeType``, ``folder_childCount``, ``lastModifiedDateTime``.

        Parameters
        ----------
        site_id / site_name : str
            Identificação do site — forneça um dos dois.
        drive_id : str
            ID do drive (obrigatório). Obtenha via ``SELECT * FROM drives(...)``.
        folder_path : str, optional
            Caminho relativo à raiz. Ex: ``/Documents/Reports``.
            Sem este parâmetro lista a raiz do drive.
        """
        if not drive_id:
            raise ValueError("drive_id é obrigatório.")
        sid = self._resolve_site(site_id, site_name)
        if folder_path:
            path = f"/sites/{sid}/drives/{drive_id}/root:{folder_path}:/children"
        else:
            path = f"/sites/{sid}/drives/{drive_id}/root/children"

        items = self._fetch(f"{GRAPH_BASE}{path}", limit=limit)
        df = pd.json_normalize(items, sep="_")

        if not df.empty:
            df["is_file"] = (
                df["file_mimeType"].notna()
                if "file_mimeType" in df.columns
                else pd.Series(True, index=df.index)
            )

        return df

    def search_files(
        self,
        site_id: Optional[str] = None,
        query: Optional[str] = None,
        site_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Pesquisa arquivos em um site via Graph API.

        Parameters
        ----------
        site_id / site_name : str
            Identificação do site — forneça um dos dois.
        query : str
            Termo de busca (obrigatório).
        """
        if not query:
            raise ValueError("query é obrigatório.")
        sid = self._resolve_site(site_id, site_name)
        url = f"{GRAPH_BASE}/sites/{sid}/drive/search(q='{query}')"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def file_versions(
        self,
        site_id: Optional[str] = None,
        drive_id: Optional[str] = None,
        item_id: Optional[str] = None,
        site_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Versões de um arquivo específico.

        Parameters
        ----------
        site_id / site_name : str
            Identificação do site — forneça um dos dois.
        drive_id, item_id : str
            Obrigatórios. ``item_id`` vem da coluna ``id`` de ``drive_items()``.
        """
        if not drive_id or not item_id:
            raise ValueError("drive_id e item_id são obrigatórios.")
        sid = self._resolve_site(site_id, site_name)
        url = (
            f"{GRAPH_BASE}/sites/{sid}"
            f"/drives/{drive_id}/items/{item_id}/versions"
        )
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    # ------------------------------------------------------------------
    # Streaming (iter_*) — para uso com DuckAPI.stream()
    # ------------------------------------------------------------------

    def iter_sites(self) -> Iterator[pd.DataFrame]:
        """Faz yield de uma página de sites por vez."""
        for page in self._iter_pages(f"{GRAPH_BASE}/sites?search=*"):
            yield pd.json_normalize(page, sep="_")

    def iter_lists(
        self,
        site_id: Optional[str] = None,
        site_name: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Faz yield de uma página de listas por vez."""
        sid = self._resolve_site(site_id, site_name)
        for page in self._iter_pages(f"{GRAPH_BASE}/sites/{sid}/lists"):
            yield pd.json_normalize(page, sep="_")

    def iter_list_items(
        self,
        site_id: Optional[str] = None,
        list_id: Optional[str] = None,
        site_name: Optional[str] = None,
        list_name: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Faz yield de uma página de items por vez (campos expandidos)."""
        sid = self._resolve_site(site_id, site_name)
        lid = self._resolve_list(sid, list_id, list_name)
        url = f"{GRAPH_BASE}/sites/{sid}/lists/{lid}/items?expand=fields"
        for page in self._iter_pages(url):
            yield self._normalize_list_items(page)

    def iter_drive_items(
        self,
        site_id: Optional[str] = None,
        drive_id: Optional[str] = None,
        site_name: Optional[str] = None,
        folder_path: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Faz yield de uma página de arquivos/pastas por vez."""
        if not drive_id:
            raise ValueError("drive_id é obrigatório.")
        sid = self._resolve_site(site_id, site_name)
        if folder_path:
            path = f"/sites/{sid}/drives/{drive_id}/root:{folder_path}:/children"
        else:
            path = f"/sites/{sid}/drives/{drive_id}/root/children"

        for page in self._iter_pages(f"{GRAPH_BASE}{path}"):
            df = pd.json_normalize(page, sep="_")
            if not df.empty and "file_mimeType" in df.columns:
                df["is_file"] = df["file_mimeType"].notna()
            yield df
