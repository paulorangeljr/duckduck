# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Run all tests
python -m pytest tests/

# Run a single test
python -m pytest tests/test_core.py::test_limit_pushdown
```

## Architecture

`duckduck` is a thin SQL engine that lets callers query HTTP APIs as if they were tables. The two main layers are:

1. **`duckduck/core.py` — `DuckAPI`**: Parses SQL with `sqlglot`, extracts push-down predicates, rewrites the query replacing `func(...)` / `FROM func` patterns with in-memory DuckDB table names, then executes the rewritten SQL against registered DataFrames.

2. **`duckduck/rapid7.py` — `InsightVM`**: An example API client whose public methods are registered as DuckAPI tables. Each method accepts the push-down kwargs that DuckAPI injects and returns `list[dict]` or `pd.DataFrame`.

### Push-down flow

```
sql("SELECT * FROM assets WHERE hostname = 'web' LIMIT 10")
        │
        ▼
_extract_pushdown()   →  PushDownContext(limit=10, filters={"hostname": "web"})
        │
        ▼
_merge_kwargs()       →  intersects filters with function signature
                         explicit inline kwargs always win
        │
        ▼
fetch_function(**kwargs)  →  single API call (limit set) or full pagination
        │
        ▼
_to_dataframe()       →  normalises list[dict] / paged dict / DataFrame
        │
        ▼
conn.register(tmp_table, df)
        │
        ▼
conn.sql(rewritten_query)   ←  DuckDB applies remaining predicates
```

`PushDownContext` carries `limit: int | None` and `filters: dict[str, Any]`. Only equality/LIKE/comparison conditions on bare column names are extracted; OR, NOT, and nested expressions are left entirely to DuckDB.

### SQL rewriting

`DuckAPI.sql()` iterates registered functions and rewrites the query with two regex passes per function:

1. `func(kwargs)` — replaces the entire call expression with the temp table name.
2. `FROM/JOIN func` (no parens) — replaces the bare name after the keyword.

Temp table names are `_api_{fn_name}_{counter}`, unique per materialisation.

### Inline syntax convention

`func(param=val)` is **only for structural parameters** — those that determine the API URL path and are not columns in the result (e.g. `asset_id` → `/assets/{id}/vulnerabilities`).

Column filters belong in the `WHERE` clause; DuckAPI pushes them down automatically if the function accepts a matching parameter name.

```sql
-- CORRECT: structural param inline, column filter in WHERE
SELECT * FROM asset_vulnerabilities(asset_id=42) WHERE severity = 'critical'

-- CORRECT: column filter in WHERE, pushed down to assets()
SELECT * FROM assets WHERE hostname = 'web-prod' LIMIT 50
```

### `_fetch` vs full pagination

Every API method calls `self._fetch(path, limit=limit)`:

- **`limit` set** → one request, `size=limit, page=0`. Returns immediately.
- **`limit` not set** → iterates all pages with `default_page_size`.

Never pass `limit` as `page_size` to `_paginate`-style loops — that fetches everything N records at a time.

---

## Adding a new API wrapper

Follow the same contract as `InsightVM`:

```python
class MyAPI:
    def __init__(self, ...):
        self.session = requests.Session()
        self.default_page_size = 500

    def _fetch(self, path, params=None, limit=None):
        # single page if limit set, paginate otherwise
        ...

    def records(
        self,
        # structural params (form URL path) — REQUIRED, no default
        resource_id: int,
        # column-filter params (WHERE push-down) — always Optional
        status: Optional[str] = None,
        name: Optional[str] = None,
        # limit is always the last param and always Optional
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        resources = self._fetch(f"/resources/{resource_id}/items", limit=limit)
        df = pd.json_normalize(resources, sep="_")
        if status and "status" in df.columns:
            df = df[df["status"] == status]
        return df
```

**Rules for each method:**

| Concern | Rule |
|---|---|
| Structural param (URL path) | Required positional, no default, document as "obrigatório" |
| Column-filter param | `Optional[X] = None`; filter the DataFrame after fetching |
| `limit` | Always `Optional[int] = None`, always last; pass straight to `_fetch` |
| Return type | `pd.DataFrame`; dot-separated nested keys become `_` via `sep="_"` |
| Server-side filter | Use it when the API supports it (fewer bytes over the wire) |
| Client-side filter | When the API has no filter param; filter the DataFrame, never slice with `head(limit)` — `_fetch` already capped the rows |

**Registration:**

```python
from duckduck import DuckAPI
from duckduck.myapi import MyAPI

api = MyAPI(...)
duck = DuckAPI()
duck.register_api_function("records", api.records)
duck.register_api_function("other",   api.other_method)
```

**Tests:** mock the HTTP session (`requests.Session`) and assert that push-down kwargs reach the method. See `tests/test_core.py` for the `tracked_*` wrapper pattern used to spy on calls.

---

## Streaming (paginação incremental)

`DuckAPI.sql()` materializa todos os dados antes de retornar. Para datasets grandes, use `DuckAPI.stream()`, que faz yield de um `pd.DataFrame` por página à medida que cada requisição HTTP completa.

### Registro duplo

```python
duck.register_api_function("assets", r7.assets)          # para sql()
duck.register_streaming_function("assets", r7.iter_assets)  # para stream()
```

### Uso no Jupyter

```python
for chunk in duck.stream("SELECT * FROM assets WHERE severity = 'critical'"):
    display(chunk)   # aparece conforme cada página chega
```

### Contrato do `iter_*` (InsightVM e wrappers novos)

- Aceita os mesmos filtros de coluna que o método normal (sem `limit` — stream itera tudo).
- Faz `yield pd.DataFrame` por página via `self._iter_pages(path)`.
- Filtros client-side são aplicados antes do yield; páginas que ficam vazias após o filtro são puladas.

```python
def iter_records(self, status=None) -> Iterator[pd.DataFrame]:
    for page in self._iter_pages("/records"):
        df = pd.json_normalize(page, sep="_")
        if status and "status" in df.columns:
            df = df[df["status"] == status]
        if not df.empty:
            yield df
```

### Limitações do `stream()`

| Comportamento | Detalhe |
|---|---|
| Tabelas | Uma por query — sem JOINs entre funções |
| `WHERE` / `SELECT` | Aplicados por chunk (correto) |
| `LIMIT N` | Aplicado por chunk, não globalmente |
| `ORDER BY` / agregações | Operam por chunk, não sobre o total |

---

## SharePoint (Microsoft Graph API)

`duckduck/sharepoint.py` implementa o mesmo contrato acima mas com diferenças de protocolo.

### Autenticação (MSAL)

```python
from duckduck import SharePoint

# Client secret
sp = SharePoint(tenant_id, client_id, client_secret)

# Thumbprint SHA-1 + chave PEM (sem dependência extra)
sp = SharePoint.from_thumbprint(tenant_id, client_id, thumbprint, private_key_pem)

# Arquivo PFX/P12 (requer: pip install cryptography)
sp = SharePoint.from_pfx(tenant_id, client_id, "/path/cert.pfx", pfx_password="...")

# PEM cert + PEM key (requer: pip install cryptography)
sp = SharePoint.from_pem_cert(tenant_id, client_id, private_key_pem, cert_pem)
```

`msal.ConfidentialClientApplication` mantém cache de token em memória; o token é renovado automaticamente quando expira.

### PEM com `\n` literal (segredos do AWS Secrets Manager)

Quando a chave privada/certificado vem de um segredo (`SecretsManager.get_secret`,
env var, etc.) que foi escapado duas vezes, a quebra de linha chega como o
texto literal de 2 caracteres `\n` em vez de um newline real — o PEM fica
inválido para MSAL/`cryptography`. `from_thumbprint` e `from_pem_cert`
corrigem isso automaticamente via `_normalize_pem` (heurística segura: o
corpo base64 de um PEM nunca contém `\`, então só reescreve quando não há
newline real na string). Isso cobre `from_secret` também, já que ele delega
para esses dois construtores.

### Paginação `@odata.nextLink`

O Graph API **não** usa `page`/`totalPages`. Ele retorna `@odata.nextLink` na resposta quando há mais dados:

```json
{"value": [...], "@odata.nextLink": "https://graph.microsoft.com/v1.0/...&$skiptoken=..."}
```

O método `_iter_pages(url)` segue o link até não haver mais. O parâmetro de tamanho de página é `$top=N` (não `size` nem `pageSize`).

```python
def _iter_pages(self, url: str) -> Iterator[List[Dict]]:
    if "$top=" not in url:
        url = self._with_top(url, self.default_page_size)
    next_url = url
    while next_url:
        payload = self._get(next_url)
        items = payload.get("value", [])
        if items:
            yield items
        next_url = payload.get("@odata.nextLink")
```

### Campos aninhados de listas (`_normalize_list_items`)

O Graph API retorna items de lista assim:

```json
{"id": "1", "fields": {"Title": "foo", "Status": "Active"}}
```

`_normalize_list_items()` eleva o sub-objeto `fields` para colunas de primeiro nível, prefixando metadados com `_`:

```
_item_id | _created_at | Title | Status
1        | 2024-01-01  | foo   | Active
```

Isso permite `WHERE Title = 'foo'` diretamente no SQL, sem qualificar com `fields_`.

### Nomes de coluna: internos vs. displayName (`column_names`)

O Graph API expõe os campos de um item de lista pelo **nome interno** da coluna
(`field_1`, `OData__ColorTag`, etc.), que raramente bate com o nome que aparece
no cabeçalho da coluna na UI do SharePoint. `list_items()` / `iter_list_items()`
aceitam `column_names: Literal["display", "internal"] = "display"`:

- **`"display"`** (padrão): busca `/lists/{id}/columns` (cache por `site_id:list_id`
  em `SharePoint._column_map_cache`) e renomeia os campos de `fields` para o
  `displayName` **antes** de registrar o DataFrame no DuckDB. Qualquer `WHERE`/
  `SELECT` da query já opera sobre o nome exibido na web — não há resolução
  adicional no `core.py`, porque o nome já é a coluna real do DataFrame.
- **`"internal"`**: pula a busca de `/columns` (uma requisição HTTP a menos) e
  mantém o nome bruto do Graph.

```python
# padrão — nomes como na web
duck.sql("SELECT * FROM list_items(site_id='abc', list_id='def') WHERE Status = 'Active'")

# nomes internos do Graph, sem request extra
duck.sql("SELECT * FROM list_items(site_id='abc', list_id='def', column_names='internal')")
```

Ao adicionar normalização de campos em outro wrapper: sempre resolva e renomeie
**antes** de `self.conn.register(...)` — nunca depois. Push-down e `WHERE` do
DuckAPI trabalham em cima das colunas do DataFrame já materializado, então
qualquer mapeamento de nomes precisa acontecer nesse ponto único.

### Parâmetros estruturais no SharePoint

`site_id`, `list_id`, `drive_id`, `item_id`, `folder_path` são sempre estruturais (formam a URL). Todos obrigatórios, sem default:

```python
def list_items(self, site_id: str, list_id: str, limit: Optional[int] = None) -> pd.DataFrame:
    url = f"{GRAPH_BASE}/sites/{site_id}/lists/{list_id}/items?expand=fields"
    return self._normalize_list_items(self._fetch(url, limit=limit))
```

```sql
-- site_id e list_id são estruturais → inline
SELECT * FROM list_items(site_id='abc', list_id='def') WHERE Title = 'Report'
```

### Dependências

| Feature | Pacote |
|---|---|
| Base | `msal>=1.20` (já em `[dependencies]`) |
| PFX / PEM cert auth | `cryptography>=41.0` (`pip install "duckduck[cert]"`) |

---

## Auto-registro (`DuckAPI.auto_register`)

Registrar cada método manualmente (`register_api_function`/`register_streaming_function`
método a método) fica repetitivo quando se quer subir todos os wrappers de
uma vez. `DuckAPI.auto_register()` instancia os wrappers conhecidos (ver
`duckduck/registry.py` → `SERVICE_REGISTRY`) e registra todas as tabelas
automaticamente, resolvendo credenciais de três formas — a referência do
segredo pode estar hardcoded no código, vir de uma variável de
ambiente/config em runtime, ou as credenciais podem ser passadas direto
(offline, sem tocar o AWS Secrets Manager):

```python
from duckduck import DuckAPI, SecretsManager

duck = DuckAPI()
instances = duck.auto_register(
    {
        "sharepoint": {
            "secret_id": "prod/sharepoint/duckduck",   # referência hardcoded
            "hostname": "empresa.sharepoint.com",
            "site_path": "/teams/meutime",
        },
        "insightvm": {
            "secret_id": os.environ["INSIGHTVM_SECRET_ID"],  # referência em runtime
        },
        "insightvm_dev": {
            "type": "insightvm",       # múltiplas instâncias do mesmo wrapper
            "credentials": {            # offline — sem AWS Secrets Manager
                "host": "dev.local", "username": "a", "password": "b",
            },
        },
    },
    secrets=SecretsManager(region_name="us-east-1"),
)

duck.sql("SELECT * FROM sharepoint_list_items WHERE list_name = 'Tarefas'")
duck.sql("SELECT * FROM insightvm_assets WHERE hostname = 'web-prod'")
```

### Regras

| Concern | Regra |
|---|---|
| Prefixo de tabela | Sempre `{nome_no_dict}_{tabela}` — evita colisão quando dois serviços expõem a mesma tabela (ex: `sites` em SharePoint e InsightVM) e permite múltiplas instâncias do mesmo wrapper |
| `type` | Opcional; default é o próprio `nome`. Use quando o `nome` não bate com uma chave de `SERVICE_REGISTRY` (ex: `insightvm_dev`) |
| `secret_id` vs `credentials` | Mutuamente exclusivos; `secret_id` requer `secrets=SecretsManager(...)` |
| Segredo do AWS Secrets Manager | JSON plano com as chaves esperadas pelo `from_secret` do wrapper (`tenant_id`/`client_id`/`client_secret` para SharePoint; `host`/`username`/`password` para InsightVM) |
| Retorno | `{nome: instância}` — para chamar métodos que não viraram tabela (ex: `instances["sharepoint"].site_by_path(...)`) |

### Adicionando um wrapper ao auto-registro

1. Implemente `Wrapper.from_secret(cls, secret: dict, **overrides) -> "Wrapper"` na classe do wrapper — decide o modo de autenticação a partir das chaves de `secret` e repassa `overrides` (hostname, site_path, default_page_size, etc.) ao construtor.
2. Acrescente uma entrada em `SERVICE_REGISTRY` (`duckduck/registry.py`) com `factory=Wrapper.from_secret` e os mapas `tables`/`streaming_tables`.

### Dependências

| Feature | Pacote |
|---|---|
| AWS Secrets Manager | `boto3>=1.28` (`pip install "duckduck[aws]"`) — não é necessário no modo offline (`credentials=`) |
