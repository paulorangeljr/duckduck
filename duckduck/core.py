"""
DuckAPI — consulte APIs externas com SQL.

Suporta push-down de predicados SQL para as funções registradas:

- LIMIT  → passado como ``limit=N`` se a função aceitar o parâmetro
- WHERE  → condições simples (=, LIKE, >, <, >=, <=) são passadas
           como kwargs se a função aceitar aquele nome de parâmetro

As funções registradas recebem os predicados que conhecem e ignoram
os demais; o DuckDB aplica o resto normalmente sobre o DataFrame
retornado.
"""

import ast
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import duckdb
import pandas as pd
import sqlglot
import sqlglot.expressions as exp


# ---------------------------------------------------------------------------
# Push-down context
# ---------------------------------------------------------------------------


@dataclass
class PushDownContext:
    """
    Predicados extraídos do SQL que podem ser enviados à API.

    Atributos
    ---------
    limit : int | None
        Valor do LIMIT da query, se presente.
    filters : dict[str, Any]
        Condições simples do WHERE: ``{nome_coluna: valor}``.
        Operadores suportados para push-down: ``=``, ``LIKE``,
        ``>``, ``<``, ``>=``, ``<=``.

    Exemplos
    --------
    Para a query::

        SELECT * FROM assets(hostname='web') WHERE severity = 'critical' LIMIT 50

    o contexto será::

        PushDownContext(limit=50, filters={"severity": "critical"})

    Note que ``hostname='web'`` já foi passado explicitamente na chamada
    da função e **não** aparece em ``filters``.
    """

    limit: Optional[int] = None
    filters: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers de extração de SQL
# ---------------------------------------------------------------------------


def _literal_value(node: exp.Expression) -> Any:
    """Converte um nó Literal do sqlglot em valor Python."""
    if isinstance(node, exp.Literal):
        if node.is_number:
            try:
                return int(node.this)
            except ValueError:
                return float(node.this)
        return node.this  # string sem aspas
    if isinstance(node, exp.Boolean):
        return node.this
    return None


def _extract_filters(node: exp.Expression, filters: Dict[str, Any]) -> None:
    """
    Percorre a árvore WHERE e extrai condições simples em ``filters``.

    Suporta: col = val | col LIKE val | col > val | col < val | col >= val | col <= val
    Ignora: OR, NOT, subqueries, funções, condições compostas.
    """
    if node is None:
        return

    if isinstance(node, (exp.EQ, exp.Like, exp.GT, exp.LT, exp.GTE, exp.LTE)):
        left, right = node.left, node.right
        if isinstance(left, exp.Column):
            val = _literal_value(right)
            if val is not None:
                filters[left.name.lower()] = val
        return

    if isinstance(node, (exp.And, exp.Where)):
        for child in node.args.values():
            if isinstance(child, exp.Expression):
                _extract_filters(child, filters)
        return


# ---------------------------------------------------------------------------
# DuckAPI
# ---------------------------------------------------------------------------


class DuckAPI:
    """
    Motor SQL que permite consultar funções Python como se fossem tabelas.

    Uso básico
    ----------
    ::

        duck = DuckAPI()

        duck.register_api_function("assets", insightvm.assets)
        duck.register_api_function("vulns",  insightvm.vulnerabilities)

        # Push-down de LIMIT
        duck.sql("SELECT * FROM assets LIMIT 10").df()

        # Filtro de coluna via WHERE → push-down automático
        duck.sql("SELECT * FROM assets WHERE hostname = 'web' LIMIT 50").df()

        # Push-down de WHERE simples
        duck.sql("SELECT * FROM vulns WHERE severity = 'critical'").df()

        # Parâmetro estrutural (monta URL) inline + filtro de coluna no WHERE
        duck.sql('''
            SELECT a.ip, v.title
            FROM assets AS a
            JOIN asset_vulns(asset_id=42) AS v ON true
            WHERE a.hostname = 'web' AND v.severity = 'critical'
        ''').df()

    Convenção: inline vs WHERE
    --------------------------
    A sintaxe ``func(param=val)`` deve ser usada **apenas** para
    parâmetros estruturais que não correspondem a colunas do resultado
    (ex: ``asset_id`` que determina o endpoint ``/assets/{id}/vulns``).

    Filtros de colunas normais pertencem ao ``WHERE`` e são injetados
    automaticamente como kwargs quando a função aceita aquele parâmetro.

    O DuckDB aplica o filtro sobre o resultado de qualquer forma,
    garantindo correção mesmo quando a API retornar dados extras.
    """

    def __init__(self, database: str = ":memory:"):
        self.conn = duckdb.connect(database)
        self.functions: Dict[str, Any] = {}
        self._streaming_functions: Dict[str, Any] = {}
        self._table_counter = 0

    # ------------------------------------------------------------------
    # Registro de funções
    # ------------------------------------------------------------------

    def register_api_function(self, name: str, fetch_function) -> None:
        """
        Registra uma função Python como "tabela" SQL.

        Parameters
        ----------
        name : str
            Nome da tabela na SQL (case-insensitive).
        fetch_function : callable
            Função que retorna list[dict], dict, ou pd.DataFrame.
            Parâmetros com os mesmos nomes de colunas / ``limit``
            recebem push-down automático.
        """
        self.functions[name.lower()] = fetch_function

    def register_streaming_function(self, name: str, iter_function) -> None:
        """
        Registra uma função geradora para uso com ``stream()``.

        Parameters
        ----------
        name : str
            Mesmo nome usado em ``register_api_function``.
        iter_function : callable
            Generator que aceita os mesmos kwargs de filtro que a função
            regular e faz ``yield`` de um ``pd.DataFrame`` por página.
            Não precisa aceitar ``limit`` — stream itera todas as páginas.
        """
        self._streaming_functions[name.lower()] = iter_function

    # ------------------------------------------------------------------
    # Auto-registro de wrappers conhecidos (SharePoint, InsightVM, ...)
    # ------------------------------------------------------------------

    def auto_register(
        self,
        services: Dict[str, Dict[str, Any]],
        secrets: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Instancia e registra automaticamente wrappers de API conhecidos
        (ver ``duckduck.registry.SERVICE_REGISTRY``), sem precisar chamar
        ``register_api_function`` manualmente método a método.

        Parameters
        ----------
        services : dict
            ``{nome: config}``. ``nome`` vira o prefixo das tabelas
            registradas (``{nome}_{tabela}``) — permite múltiplas
            instâncias do mesmo wrapper (ex: ``insightvm_prod`` e
            ``insightvm_dev``).

            ``config`` aceita:

            - ``type`` : str, opcional
                Chave em ``SERVICE_REGISTRY`` (``"sharepoint"``,
                ``"insightvm"``). Default: o próprio ``nome``.
            - ``secret_id`` : str, opcional
                Nome/ARN do segredo no AWS Secrets Manager — requer
                ``secrets=`` fornecido. A referência pode ficar
                hardcoded aqui no código ou vir de env var/config em
                runtime — ``auto_register`` não faz distinção.
            - ``credentials`` : dict, opcional
                Credenciais fornecidas diretamente, sem tocar o AWS
                Secrets Manager (modo offline). Forneça **ou**
                ``secret_id`` **ou** ``credentials``, nunca os dois.
            - demais chaves
                kwargs extras repassados ao construtor do wrapper (ex:
                ``hostname``, ``site_path``, ``default_page_size``).
        secrets : SecretsManager, optional
            Necessário apenas quando algum serviço usa ``secret_id``.

        Returns
        -------
        dict
            ``{nome: instância}`` — para acessar métodos do wrapper que
            não viraram tabela (ex: ``instances["sharepoint"].site_by_path``).

        Exemplos
        --------
        ::

            from duckduck import DuckAPI, SecretsManager

            duck = DuckAPI()
            instances = duck.auto_register(
                {
                    "sharepoint": {
                        "secret_id": "prod/sharepoint/duckduck",  # hardcoded
                        "hostname": "empresa.sharepoint.com",
                        "site_path": "/teams/meutime",
                    },
                    "insightvm": {
                        "credentials": {  # offline — sem AWS Secrets Manager
                            "host": "console.local",
                            "username": "a",
                            "password": "b",
                        },
                    },
                },
                secrets=SecretsManager(region_name="us-east-1"),
            )

            duck.sql("SELECT * FROM sharepoint_list_items WHERE list_name = 'Tarefas'")
            duck.sql("SELECT * FROM insightvm_assets WHERE hostname = 'web-prod'")
        """
        from .registry import SERVICE_REGISTRY

        instances: Dict[str, Any] = {}

        for name, raw_config in services.items():
            config = dict(raw_config)
            service_type = config.pop("type", name)
            spec = SERVICE_REGISTRY.get(service_type)
            if spec is None:
                raise ValueError(
                    f"Serviço '{name}' (type='{service_type}') não é reconhecido. "
                    f"Disponíveis: {', '.join(SERVICE_REGISTRY)}"
                )

            secret_id = config.pop("secret_id", None)
            credentials = config.pop("credentials", None)

            if secret_id and credentials:
                raise ValueError(
                    f"'{name}': forneça 'secret_id' OU 'credentials', não ambos."
                )
            if secret_id:
                if secrets is None:
                    raise ValueError(
                        f"'{name}' usa secret_id='{secret_id}' mas nenhum "
                        "SecretsManager foi passado em auto_register(secrets=...)."
                    )
                credentials = secrets.get_secret(secret_id)
            if credentials is None:
                raise ValueError(
                    f"'{name}': forneça 'secret_id' (AWS Secrets Manager) "
                    "ou 'credentials' (offline)."
                )

            instance = spec.factory(credentials, **config)
            instances[name] = instance

            for table_name, method_name in spec.tables.items():
                self.register_api_function(
                    f"{name}_{table_name}", getattr(instance, method_name)
                )
            for table_name, method_name in spec.streaming_tables.items():
                self.register_streaming_function(
                    f"{name}_{table_name}", getattr(instance, method_name)
                )

        return instances

    # ------------------------------------------------------------------
    # Parse de kwargs inline:  func(x=1, y="a")
    # ------------------------------------------------------------------

    def _parse_kwargs(self, text: str) -> Dict[str, Any]:
        """
        Converte a string de argumentos inline em dict.

        ``pr_id=123, limit=100``  →  ``{"pr_id": 123, "limit": 100}``
        """
        if not text.strip():
            return {}

        expr = ast.parse(f"_f_({text})", mode="eval")
        call = expr.body

        if call.args:
            raise ValueError(
                "Use apenas parâmetros nomeados. "
                "Exemplo: assets(hostname='web', limit=50)"
            )

        kwargs: Dict[str, Any] = {}
        for kw in call.keywords:
            if kw.arg is None:
                raise ValueError("Expansão com **kwargs não é permitida na SQL")
            kwargs[kw.arg] = ast.literal_eval(kw.value)

        return kwargs

    # ------------------------------------------------------------------
    # Extração de push-down do SQL
    # ------------------------------------------------------------------

    def _extract_pushdown(self, query: str) -> PushDownContext:
        """Parseia o SQL com sqlglot e extrai LIMIT e condições WHERE simples."""
        ctx = PushDownContext()

        try:
            parsed = sqlglot.parse_one(query, dialect="duckdb")
        except Exception:
            return ctx

        limit_node = parsed.find(exp.Limit)
        if limit_node is not None:
            try:
                ctx.limit = int(limit_node.expression.this)
            except (ValueError, AttributeError, TypeError):
                pass

        where_node = parsed.find(exp.Where)
        if where_node is not None:
            _extract_filters(where_node, ctx.filters)

        return ctx

    # ------------------------------------------------------------------
    # Merge de kwargs explícitos + push-down
    # ------------------------------------------------------------------

    def _merge_kwargs(
        self,
        fetch_function,
        pushdown: PushDownContext,
        explicit: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Constrói o dict final de kwargs para a chamada da função.

        Prioridade (maior → menor):
        1. kwargs explícitos da chamada SQL  ``func(x=1)``
        2. push-down de WHERE/LIMIT do SQL
        """
        sig = inspect.signature(fetch_function)
        accepted = set(sig.parameters.keys())

        merged: Dict[str, Any] = {}

        if pushdown.limit is not None and "limit" in accepted:
            merged["limit"] = pushdown.limit

        for col, val in pushdown.filters.items():
            if col in accepted:
                merged[col] = val

        merged.update(explicit)

        return merged

    # ------------------------------------------------------------------
    # Validação de assinatura
    # ------------------------------------------------------------------

    def _validate_arguments(
        self,
        function_name: str,
        fetch_function,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Valida kwargs contra a assinatura real da função."""
        sig = inspect.signature(fetch_function)
        try:
            bound = sig.bind(**kwargs)
        except TypeError as err:
            raise ValueError(
                f"Chamada inválida para '{function_name}': {err}. "
                f"Assinatura esperada: {function_name}{sig}"
            ) from err
        bound.apply_defaults()
        return bound.arguments

    # ------------------------------------------------------------------
    # Conversão para DataFrame
    # ------------------------------------------------------------------

    def _to_dataframe(self, data: Any, function_name: str) -> pd.DataFrame:
        """
        Normaliza o retorno da função em DataFrame.

        Aceita:
        - list[dict]
        - dict com chave ``resources``, ``items``, ``data`` ou ``results``
        - pd.DataFrame
        - None  (retorna DataFrame vazio, levanta erro)
        """
        if data is None:
            data = []

        if isinstance(data, dict):
            for key in ("resources", "items", "data", "results"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = [data]

        if isinstance(data, pd.DataFrame):
            df = data
        else:
            df = pd.json_normalize(data)

        if df.empty or len(df.columns) == 0:
            raise ValueError(
                f"A tabela '{function_name}' não retornou dados. "
                "Não é possível determinar as colunas."
            )

        df.columns = [c.replace(".", "_") for c in df.columns]
        return df

    # ------------------------------------------------------------------
    # Materialização
    # ------------------------------------------------------------------

    def _materialize(
        self,
        function_name: str,
        fetch_function,
        kwargs: Dict[str, Any],
    ) -> tuple:
        """
        Chama a função, converte para DataFrame e registra no DuckDB.

        Returns
        -------
        (table_name, df_columns) : (str, list[str])
        """
        validated = self._validate_arguments(function_name, fetch_function, kwargs)
        data = fetch_function(**validated)
        df = self._to_dataframe(data, function_name)

        self._table_counter += 1
        table_name = f"_api_{function_name}_{self._table_counter}"
        self.conn.register(table_name, df)
        return table_name, list(df.columns)

    def _strip_where_conditions(self, query: str, keys: set) -> str:
        """
        Remove condições do WHERE que referenciam colunas em ``keys``.

        Usado para descartar filtros push-down que foram consumidos pela
        função mas não existem como colunas no DataFrame resultado —
        tipicamente parâmetros estruturais como ``site_name``, ``list_name``.
        """
        if not keys:
            return query
        try:
            tree = sqlglot.parse_one(query, dialect="duckdb")
        except Exception:
            return query

        where = tree.find(exp.Where)
        if not where:
            return query

        def _should_keep(node: exp.Expression) -> bool:
            if isinstance(node, (exp.EQ, exp.Like, exp.GT, exp.LT, exp.GTE, exp.LTE)):
                if isinstance(node.this, exp.Column):
                    if node.this.name.lower() in keys:
                        return False
            return True

        def _rebuild(node: exp.Expression):
            if isinstance(node, exp.And):
                left = _rebuild(node.this)
                right = _rebuild(node.expression)
                if left is None:
                    return right
                if right is None:
                    return left
                return exp.And(this=left, expression=right)
            return node if _should_keep(node) else None

        new_condition = _rebuild(where.this)
        if new_condition is None:
            where.pop()
        else:
            where.set("this", new_condition)

        return tree.sql(dialect="duckdb")

    # ------------------------------------------------------------------
    # SQL principal
    # ------------------------------------------------------------------

    def sql(self, query: str):
        """
        Executa uma query SQL, substituindo referências a funções
        registradas pelos DataFrames correspondentes.

        Suporta:
        - ``FROM func``                → push-down de WHERE/LIMIT automático
        - ``FROM func(struct_id=1)``   → parâmetro estrutural (monta URL/path)
                                         + push-down de WHERE/LIMIT
        - ``JOIN func(struct_id=1)``   → idem
        - Múltiplas referências à mesma função ou funções diferentes

        Parâmetros estruturais (``site_name``, ``list_name``, etc.) podem
        aparecer tanto inline quanto no ``WHERE``. Quando estão no WHERE e
        não são colunas do resultado, são automaticamente removidos da
        query antes de o DuckDB executá-la.

        Returns
        -------
        duckdb.DuckDBPyRelation
            Relação do DuckDB. Use ``.df()`` para obter um DataFrame.
        """
        pushdown = self._extract_pushdown(query)
        rewritten = query
        structural_used: set = set()  # filtros WHERE consumidos que não são colunas

        for fn_name, fn in self.functions.items():

            # ---- 1. func(args) ----------------------------------------
            with_args_pat = re.compile(
                rf"\b{re.escape(fn_name)}\s*\((.*?)\)",
                flags=re.IGNORECASE | re.DOTALL,
            )

            while m := with_args_pat.search(rewritten):
                explicit = self._parse_kwargs(m.group(1))
                kwargs = self._merge_kwargs(fn, pushdown, explicit)
                tname, df_cols = self._materialize(fn_name, fn, kwargs)
                # Filtros de WHERE que foram para a função mas não são colunas resultado
                structural_used.update(
                    k for k in pushdown.filters
                    if k in kwargs and k not in df_cols
                )
                rewritten = rewritten[: m.start()] + tname + rewritten[m.end() :]

            # ---- 2. FROM/JOIN func  (sem parênteses) ------------------
            bare_pat = re.compile(
                rf"\b(FROM|JOIN)\s+{re.escape(fn_name)}\b(?!\s*\()",
                flags=re.IGNORECASE,
            )

            while m := bare_pat.search(rewritten):
                kwargs = self._merge_kwargs(fn, pushdown, {})
                tname, df_cols = self._materialize(fn_name, fn, kwargs)
                structural_used.update(
                    k for k in pushdown.filters
                    if k in kwargs and k not in df_cols
                )
                op = m.group(1)
                rewritten = rewritten[: m.start()] + f"{op} {tname}" + rewritten[m.end() :]

        if structural_used:
            rewritten = self._strip_where_conditions(rewritten, structural_used)

        return self.conn.sql(rewritten)

    # ------------------------------------------------------------------
    # Streaming (paginação incremental)
    # ------------------------------------------------------------------

    def stream(self, query: str):
        """
        Executa a query página a página, fazendo ``yield`` de um
        ``pd.DataFrame`` por página à medida que cada requisição retorna.

        Diferente de ``sql()``, não espera todos os dados antes de
        devolver o primeiro resultado — útil para datasets grandes ou
        para exibir progresso no Jupyter.

        Requer que a função tenha sido registrada também via
        ``register_streaming_function()``.

        Limitações
        ----------
        - Suporta apenas uma tabela por query (sem JOINs entre funções).
        - ``LIMIT N`` e ``WHERE`` são aplicados **por página** (não globalmente).
          Para um LIMIT global use ``sql()`` com o LIMIT desejado.
        - ``ORDER BY`` e agregações operam por chunk, não sobre o total.

        Exemplo
        -------
        ::

            duck.register_api_function("assets", r7.assets)
            duck.register_streaming_function("assets", r7.iter_assets)

            for chunk in duck.stream("SELECT * FROM assets WHERE severity = 'critical'"):
                display(chunk)   # exibe conforme chega cada página

        Yields
        ------
        pd.DataFrame
            Resultado da query aplicado sobre cada página da API.
        """
        pushdown = self._extract_pushdown(query)

        for fn_name, iter_fn in self._streaming_functions.items():
            if not re.search(rf"\b{re.escape(fn_name)}\b", query, re.IGNORECASE):
                continue

            # Kwargs explícitos da chamada inline
            explicit: Dict[str, Any] = {}
            inline_pat = re.compile(
                rf"\b{re.escape(fn_name)}\s*\((.*?)\)",
                flags=re.IGNORECASE | re.DOTALL,
            )
            if m := inline_pat.search(query):
                explicit = self._parse_kwargs(m.group(1))

            # Filtros do WHERE que a função geradora aceita (sem limit)
            fn = self.functions.get(fn_name)
            if fn is not None:
                sig = inspect.signature(fn)
                accepted = set(sig.parameters.keys()) - {"limit"}
                kwargs: Dict[str, Any] = {
                    col: val for col, val in pushdown.filters.items() if col in accepted
                }
            else:
                kwargs = dict(pushdown.filters)
            kwargs.update(explicit)

            # Reescreve a query substituindo func(...) / func pelo nome do chunk
            chunk_table = f"_stream_{fn_name}"
            chunk_query = inline_pat.sub(chunk_table, query)
            bare_pat = re.compile(
                rf"\b(FROM|JOIN)\s+{re.escape(fn_name)}\b(?!\s*\()",
                flags=re.IGNORECASE,
            )
            chunk_query = bare_pat.sub(rf"\1 {chunk_table}", chunk_query)

            for chunk_df in iter_fn(**kwargs):
                if chunk_df.empty:
                    continue
                self.conn.register(chunk_table, chunk_df)
                yield self.conn.sql(chunk_query).df()

            return

        raise ValueError(
            f"Nenhuma streaming function registrada para a query.\n"
            f"Use register_streaming_function() para registrar um gerador."
        )

    # ------------------------------------------------------------------

    def close(self) -> None:
        """Fecha a conexão com o DuckDB."""
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
