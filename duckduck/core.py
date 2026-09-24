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
    ) -> str:
        """Chama a função, converte para DataFrame e registra no DuckDB."""
        validated = self._validate_arguments(function_name, fetch_function, kwargs)
        data = fetch_function(**validated)
        df = self._to_dataframe(data, function_name)

        self._table_counter += 1
        table_name = f"_api_{function_name}_{self._table_counter}"
        self.conn.register(table_name, df)
        return table_name

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

        A sintaxe ``func(param=val)`` deve ser usada apenas para parâmetros
        que **não são colunas** do resultado (ex: IDs que determinam o
        endpoint da API). Filtros de colunas pertencem ao ``WHERE``.

        Returns
        -------
        duckdb.DuckDBPyRelation
            Relação do DuckDB. Use ``.df()`` para obter um DataFrame.
        """
        pushdown = self._extract_pushdown(query)
        rewritten = query

        for fn_name, fn in self.functions.items():

            # ---- 1. func(args) ----------------------------------------
            with_args_pat = re.compile(
                rf"\b{re.escape(fn_name)}\s*\((.*?)\)",
                flags=re.IGNORECASE | re.DOTALL,
            )

            while m := with_args_pat.search(rewritten):
                explicit = self._parse_kwargs(m.group(1))
                kwargs = self._merge_kwargs(fn, pushdown, explicit)
                tname = self._materialize(fn_name, fn, kwargs)
                rewritten = rewritten[: m.start()] + tname + rewritten[m.end() :]

            # ---- 2. FROM/JOIN func  (sem parênteses) ------------------
            bare_pat = re.compile(
                rf"\b(FROM|JOIN)\s+{re.escape(fn_name)}\b(?!\s*\()",
                flags=re.IGNORECASE,
            )

            while m := bare_pat.search(rewritten):
                kwargs = self._merge_kwargs(fn, pushdown, {})
                tname = self._materialize(fn_name, fn, kwargs)
                op = m.group(1)
                rewritten = rewritten[: m.start()] + f"{op} {tname}" + rewritten[m.end() :]

        return self.conn.sql(rewritten)

    # ------------------------------------------------------------------

    def close(self) -> None:
        """Fecha a conexão com o DuckDB."""
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
