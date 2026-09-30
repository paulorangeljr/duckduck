#!/usr/bin/env bash
# Builds the kql DuckDB extension (github.com/saoc90/kql-to-sql) that duckduck's KQL support uses,
# and installs it where duckduck looks by default: ~/.duckduck/extensions/kql.duckdb_extension
#
#   scripts/build_kql_extension.sh            # the pinned commit
#   KQL_TO_SQL_REF=main scripts/build_kql_extension.sh
#
# Needs: git, python3, the .NET 10 SDK (https://dotnet.microsoft.com/download — on Ubuntu 24.04:
# `apt-get install dotnet-sdk-10.0`) and a C toolchain for Native AOT (clang or gcc, zlib).
# Linux / macOS; on Windows run the same steps with the repo's build-extension.ps1.
set -euo pipefail

REF="${KQL_TO_SQL_REF:-04ad97ab24d2d6e380412f965069db10058ec16d}"   # tested with duckduck
DEST="${DUCKDUCK_KQL_EXTENSION:-$HOME/.duckduck/extensions/kql.duckdb_extension}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v dotnet >/dev/null || { echo "dotnet not found: install the .NET 10 SDK first" >&2; exit 1; }

echo "Fetching kql-to-sql@$REF…"
git -C "$WORK" init -q kql-to-sql
git -C "$WORK/kql-to-sql" remote add origin https://github.com/saoc90/kql-to-sql
git -C "$WORK/kql-to-sql" fetch -q --depth 1 origin "$REF"
git -C "$WORK/kql-to-sql" checkout -q FETCH_HEAD
git -C "$WORK/kql-to-sql" submodule update -q --init --depth 1
# the packaging script that turns the native library into a .duckdb_extension
git clone -q --depth 1 https://github.com/duckdb/extension-ci-tools \
  "$WORK/kql-to-sql/src/KqlToSql.DuckDbExtension/extension-ci-tools"

# duckduck's addition: kql_syntax_errors(kql) — the Kusto parser's own syntax errors. kql_to_sql translates
# whatever the parser recovered, so without it a typo quietly becomes another query
# ("T | where x == 1 | projct a" → "SELECT * FROM a"); duckduck checks this first.
python3 - "$WORK/kql-to-sql/src/KqlToSql.DuckDbExtension/KqlExtension.cs" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
if "kql_syntax_errors" not in s:
    s = s.replace(
        'connection.RegisterScalarFunction<string, string>("kql_to_sql", ConvertKqlToSql);',
        'connection.RegisterScalarFunction<string, string>("kql_to_sql", ConvertKqlToSql);\n\n'
        '        connection.RegisterScalarFunction<string, string>("kql_syntax_errors", KqlSyntaxErrors);', 1)
    s = s.replace("    private record KqlExplainRow(", """    /// <summary>The Kusto parser's syntax errors, one per line ("offset: message"); empty when it parses.</summary>
    private static string KqlSyntaxErrors(string kql)
    {
        var code = Kusto.Language.KustoCode.Parse(kql);
        return string.Join("\\n", code.GetSyntaxDiagnostics()
            .Where(d => d.Severity == Kusto.Language.DiagnosticSeverity.Error)
            .Select(d => $"{d.Start}: {d.Message}"));
    }

    private record KqlExplainRow(""", 1)
    assert "KqlSyntaxErrors" in s, "couldn't add kql_syntax_errors: the extension's source changed"
    open(p, "w").write(s)
PY

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) RID=linux-x64 ;;  Linux-aarch64) RID=linux-arm64 ;;
  Darwin-x86_64) RID=osx-x64 ;;   Darwin-arm64) RID=osx-arm64 ;;
  *) echo "unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

echo "Building for $RID (Native AOT — a few minutes the first time)…"
(cd "$WORK/kql-to-sql/src/KqlToSql.DuckDbExtension" && dotnet publish -c Release -r "$RID" -v quiet -nologo)

BUILT="$WORK/kql-to-sql/src/KqlToSql.DuckDbExtension/bin/Release/net10.0/$RID/publish/kql.duckdb_extension"
[ -f "$BUILT" ] || { echo "the build didn't produce kql.duckdb_extension" >&2; exit 1; }
mkdir -p "$(dirname "$DEST")"
cp "$BUILT" "$DEST"
echo "Installed: $DEST"
python3 - "$DEST" <<'PY' || echo "(couldn't check it with the duckdb Python package — install duckduck's requirements and try again)"
import sys, duckdb
c = duckdb.connect(config={"allow_unsigned_extensions": "true"})
c.execute(f"LOAD '{sys.argv[1]}'")
print("Check:", c.execute("SELECT kql_to_sql('T | where x == 1 | take 5')").fetchone()[0])
print("Syntax check:", c.execute("SELECT kql_syntax_errors('T | wherre x')").fetchone()[0])
PY
