"""The Notebook tab: duckduck in an AWS Glue interactive session (duckduck.glue_session) and JupyterLab behind the web app
(duckduck.notebook). No AWS account here: S3 is moto, the kernel a plain python3 one; the Glue kernel's magics
are checked as text."""

import json
import os
import threading
import time

import pytest

from duckduck import DuckAPI
from duckduck.glue_session import (GlueError, artifact_uris, connect, glue_settings, magics, notebook_template, publish,
                           pyspark_code)
from duckduck.notebook import COOKIE, NotebookError, NotebookServer, notebook_problems
from duckduck.semantic.admin import validate_config


# ---- the Glue session's settings and the notebook it starts from ---------------------------------------------

def test_glue_settings_take_defaults_and_refuse_unknown_keys():
    s = glue_settings({"notebook": {"glue": {"iam_role": "arn:aws:iam::123456789012:role/glue",
                                             "connections": "vpc-a, vpc-b"}}})
    assert s["glue_version"] == "5.0" and s["connections"] == ["vpc-a", "vpc-b"]
    with pytest.raises(GlueError, match="unknown key.*workers"):
        glue_settings({"notebook": {"glue": {"workers": 4}}})
    assert notebook_problems({"glue": {"workers": 4}})
    assert notebook_problems({"kernels": "glue_pyspark"}) and notebook_problems({"port": 1})
    assert notebook_problems({"enabled": True, "dir": "nb", "glue": {"region": "us-east-1"}}) == []


def test_validate_config_knows_the_notebook_block():
    ok = validate_config({"services": {}, "notebook": {"enabled": True}})
    assert not any("notebook" in w for w in ok["warnings"]) and not ok["errors"]
    bad = validate_config({"services": {}, "notebook": {"glue": {"nope": 1}}})
    assert any("notebook.glue" in e for e in bad["errors"])


def test_magics_and_template_install_the_published_wheel_and_connect_to_its_config():
    settings = glue_settings({"notebook": {"glue": {
        "iam_role": "arn:aws:iam::123456789012:role/glue", "region": "us-east-1", "connections": ["vpc"],
        "artifacts": "s3://minhaempresa-artifacts/duckduck/", "extra_python_modules": ["polars==1.30"]}}})
    text = magics(settings, version="9.9.9")
    assert "%iam_role arn:aws:iam::123456789012:role/glue" in text and "%region us-east-1" in text
    assert "%connections vpc" in text and "%idle_timeout 30" in text
    assert ("%additional_python_modules s3://minhaempresa-artifacts/duckduck/duckduck-9.9.9-py3-none-any.whl,"
            "polars==1.30") in text
    nb = notebook_template(settings, version="9.9.9")
    sources = ["".join(c["source"]) for c in nb["cells"]]
    assert nb["metadata"]["kernelspec"]["name"] == "duckduck_glue_pyspark"  # Glue's kernel + SQL cells
    configure = next(s for s in sources if s.startswith("%%configure"))
    assert json.loads(configure.split("\n", 1)[1]) == {"--enable-glue-datacatalog": "true",
                                                       "--datalake-formats": "delta,iceberg"}
    assert 'connect("s3://minhaempresa-artifacts/duckduck/duckduck.json")' in "".join(sources)
    assert "from duckduck.glue_session import connect" in "".join(sources)  # duckduck.glue is the old connector
    assert artifact_uris("s3://b", "1")["config"] == "s3://b/duckduck.json"


def test_template_says_what_is_missing():
    nb = notebook_template(glue_settings({}))
    intro = "".join(nb["cells"][0]["source"])
    assert "notebook.glue.iam_role" in intro and "notebook.glue.artifacts" in intro


# ---- publish and connect over S3 (moto) --------------------------------------------------------------------------

@pytest.fixture
def s3():
    moto = pytest.importorskip("moto")
    import boto3

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="minhaempresa-artifacts")
        yield client


def _config(tmp_path, services):
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps({"services": services}))
    return str(path)


def test_publish_uploads_the_wheel_and_config_and_connect_reads_it_back(tmp_path, s3):
    data = tmp_path / "data"
    data.mkdir()
    (data / "hosts.csv").write_text("host,os\nweb,linux\ndb,windows\n")
    wheel = tmp_path / "duckduck-9.9.9-py3-none-any.whl"
    wheel.write_bytes(b"not really a wheel")
    # the session reads the config from S3: its paths are its own, so an absolute one here
    config = _config(tmp_path, {"lab": {"connector": "files", "path": str(data)}})
    done = publish("s3://minhaempresa-artifacts/duckduck/", config_path=config, wheel=str(wheel), s3_client=s3)
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="minhaempresa-artifacts")["Contents"])
    assert keys == ["duckduck/duckduck-9.9.9-py3-none-any.whl", "duckduck/duckduck.json"]
    assert done.config == "s3://minhaempresa-artifacts/duckduck/duckduck.json"
    assert "%additional_python_modules s3://minhaempresa-artifacts/duckduck/duckduck-9.9.9" in done.report()

    duck, reader = connect(done.config, spark=object(), s3_client=s3, quiet=True)
    assert sorted(duck.sql("SELECT host FROM lab_hosts ORDER BY host").df()["host"]) == ["db", "web"]
    assert reader.duck is duck


def test_publish_refuses_a_config_with_secrets_written_in_it(tmp_path, s3):
    wheel = tmp_path / "duckduck-1-py3-none-any.whl"
    wheel.write_bytes(b"x")
    config = _config(tmp_path, {"sn": {"connector": "servicenow", "instance": "dev1",
                                       "authentication": {"type": "local", "username": "u", "password": "p4ss"}}})
    with pytest.raises(GlueError, match="secrets written in it.*password"):
        publish("s3://minhaempresa-artifacts/x/", config_path=config, wheel=str(wheel), s3_client=s3)
    assert "Contents" not in s3.list_objects_v2(Bucket="minhaempresa-artifacts")  # nothing went up
    publish("s3://minhaempresa-artifacts/x/", config_path=config, wheel=str(wheel), s3_client=s3,
            allow_secrets=True)


def test_connect_names_the_missing_permission(s3):
    with pytest.raises(GlueError, match="s3:GetObject"):
        connect("s3://minhaempresa-artifacts/nope/duckduck.json", spark=object(), s3_client=s3, quiet=True)


# ---- the cell a click on a table adds -------------------------------------------------------------------------------

@pytest.fixture
def glue_duck():
    from duckduck.connectors.lake.glue import GlueTable

    glue = GlueTable.__new__(GlueTable)  # nothing is read: only the kind of table matters
    duck = DuckAPI()
    duck.register_api_function("s3_data_table", glue.table)
    duck.service_of["s3_data_table"] = "s3_data"
    duck.service_prefix["s3_data"] = "s3_data"
    duck.register_api_function("assets", lambda hostname=None, limit=None: [])
    duck.service_of["assets"] = "cmdb"
    duck.register_view("security_proxy", {"table": "s3_data_table",
                                          "args": {"database": "security", "table_name": "proxy_logs"}})
    return duck


def test_a_glue_catalog_table_is_read_by_spark_itself(glue_duck):
    cell = pyspark_code(glue_duck, "SELECT * FROM s3_data.security.proxy_logs LIMIT 100")
    assert cell["how"] == "spark.table"
    assert cell["code"].startswith('proxy_logs = spark.table("security.proxy_logs")')
    assert "proxy_logs.show(20)" in cell["code"]
    where = pyspark_code(glue_duck, "SELECT * FROM s3_data_table WHERE arg.database = 'security' "
                                    "AND arg.table_name = 'proxy_logs' AND status = 403")["code"]
    assert 'spark.table("security.proxy_logs").where("""status = 403""")' in where
    assert pyspark_code(glue_duck, "SELECT * FROM security_proxy")["how"] == "spark.table"  # a saved table over one


def test_anything_else_is_read_through_duckduck(glue_duck):
    cell = pyspark_code(glue_duck, "SELECT * FROM assets WHERE hostname = 'web' LIMIT 100")
    assert cell["how"] == "reader.sql"
    assert cell["code"].startswith('assets = reader.sql("""\nSELECT * FROM assets WHERE hostname = \'web\' LIMIT 100')
    assert "LIMIT 100: a first look" in cell["code"]
    placeholders = pyspark_code(glue_duck, "SELECT * FROM s3_data_table(database='<database>', "
                                           "table_name='<table_name>') LIMIT 100")
    assert placeholders["how"] == "reader.sql" and placeholders["code"].startswith("# fill in the '<…>' values")
    joined = pyspark_code(glue_duck, "SELECT * FROM s3_data.security.proxy_logs p JOIN assets a ON a.hostname = p.host")
    assert joined["how"] == "reader.sql"
    quotes = pyspark_code(glue_duck, 'SELECT \'"""\' AS q FROM assets')["code"]
    compile(quotes, "cell", "exec")  # still one Python string
    with pytest.raises(GlueError):
        pyspark_code(glue_duck, "  ")


# ---- the notebooks folder --------------------------------------------------------------------------------------------

def test_new_notebook_writes_the_template_and_never_overwrites(tmp_path):
    server = NotebookServer(str(tmp_path / "nb"), kernels=["glue_pyspark"],
                            glue=glue_settings({"notebook": {"glue": {"artifacts": "s3://b/p/"}}}))
    first = server.new_notebook("proxy logs")
    second = server.new_notebook("proxy logs.ipynb")
    assert (first, second) == ("proxy logs.ipynb", "proxy logs-2.ipynb")
    nb = json.loads((tmp_path / "nb" / first).read_text())
    assert nb["metadata"]["kernelspec"]["name"] == "glue_pyspark"
    assert server.notebooks()[0] in (first, second)
    for bad in ("../up", "a/b", ""):
        with pytest.raises(NotebookError):
            server.new_notebook(bad or " ")
    with pytest.raises(NotebookError, match="isn't offered"):
        server.new_notebook("x", kernel="python3")


def test_from_config_puts_notebooks_next_to_the_config(tmp_path):
    server = NotebookServer.from_config({"notebook": {"dir": "nbs", "glue": {"region": "eu-west-1"}}}, str(tmp_path))
    assert server.root_dir == str(tmp_path / "nbs") and server.glue["region"] == "eu-west-1"
    assert server.kernels == ["duckduck_glue_pyspark"]
    with pytest.raises(NotebookError, match="unknown key"):
        NotebookServer.from_config({"notebook": {"port": 1}}, str(tmp_path))


# ---- JupyterLab behind the app: a real one, through the proxy ----------------------------------------------------------

@pytest.fixture(scope="module")
def jupyter(tmp_path_factory):
    pytest.importorskip("jupyterlab")
    pytest.importorskip("websockets")
    server = NotebookServer(str(tmp_path_factory.mktemp("notebooks")), kernels=["python3"])
    if "python3" not in server.available_kernels():
        pytest.skip("no python3 kernel")
    yield server
    server.stop()


@pytest.fixture
def app_client(jupyter, glue_duck):
    from fastapi.testclient import TestClient

    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.engine import SemanticSearch
    from duckduck.semantic.server import create_app

    search = SemanticSearch.turned_off(glue_duck, "not needed here")
    app = create_app(lambda: search, store=None, console=SQLConsole(glue_duck), notebook=jupyter)
    with TestClient(app) as client:
        yield client


def test_the_proxy_needs_the_cookie_from_the_api(app_client, jupyter):
    assert app_client.get("/api/meta").json()["features"]["notebook"] is True
    assert app_client.get("/notebook/api/status").status_code == 401  # never reached without opening the tab
    opened = app_client.post("/api/notebook/open", json={})
    assert opened.status_code == 200 and opened.json()["url"] == "/notebook/lab"
    assert COOKIE in app_client.cookies
    status = app_client.get("/notebook/api/status")
    assert status.status_code == 200 and "kernels" in status.json()
    assert list(app_client.get("/notebook/api/kernelspecs").json()["kernelspecs"]) == ["python3"]
    lab = app_client.get("/notebook/lab")
    assert lab.status_code == 200 and "duckduck-notebook" in lab.text  # the extension is loaded
    # a browser's own Origin would make Jupyter refuse a write; the proxy is the boundary
    made = app_client.post("/api/notebook/new", json={"name": "made here"})
    # python3 isn't a Glue kernel, but it is the one offered here
    assert made.status_code == 200 and made.json()["path"] == "made here.ipynb"
    contents = app_client.get("/notebook/api/contents", headers={"Origin": "https://elsewhere.example"})
    assert contents.status_code == 200 and "made here.ipynb" in [c["name"] for c in contents.json()["content"]]


def test_a_kernel_runs_code_over_the_proxied_websocket(app_client):
    app_client.post("/api/notebook/open", json={})
    kernel = app_client.post("/notebook/api/kernels", json={"name": "python3"}).json()
    try:
        with app_client.websocket_connect(f"/notebook/api/kernels/{kernel['id']}/channels") as ws:
            msg_id = "duckduck-test"
            ws.send_text(json.dumps({
                "header": {"msg_id": msg_id, "msg_type": "execute_request", "username": "t", "session": "s",
                           "version": "5.3", "date": ""},
                "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
                "content": {"code": "6 * 7", "silent": False, "store_history": False, "user_expressions": {},
                            "allow_stdin": False, "stop_on_error": True}}))
            deadline = time.time() + 30
            while time.time() < deadline:
                msg = json.loads(ws.receive_text())
                if msg.get("msg_type") == "execute_result" and msg["parent_header"].get("msg_id") == msg_id:
                    assert msg["content"]["data"]["text/plain"] == "42"
                    break
            else:
                pytest.fail("no execute_result over the websocket")
    finally:
        app_client.delete(f"/notebook/api/kernels/{kernel['id']}")


def test_the_page_gets_cells_for_the_tables_it_lists(app_client):
    cell = app_client.post("/api/notebook/code", json={"sql": "SELECT * FROM s3_data.security.proxy_logs LIMIT 100"})
    assert cell.json()["how"] == "spark.table"
    assert app_client.post("/api/notebook/code", json={"sql": ""}).status_code == 400


# ---- in a browser: a click on a table adds a cell (opt-in: DUCKDUCK_BROWSER_TESTS=1) ------------------------------------

@pytest.mark.skipif(os.environ.get("DUCKDUCK_BROWSER_TESTS") != "1", reason="browser tests are off — set "
                    "DUCKDUCK_BROWSER_TESTS=1 (needs Playwright's Chromium)")
def test_clicking_a_table_adds_a_cell_in_jupyterlab(jupyter, glue_duck):
    import glob
    import socket

    import uvicorn
    from playwright.sync_api import sync_playwright

    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.engine import SemanticSearch
    from duckduck.semantic.server import create_app

    search = SemanticSearch.turned_off(glue_duck, "not needed here")
    app = create_app(lambda: search, store=None, console=SQLConsole(glue_duck), notebook=jupyter)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    web = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=web.run, daemon=True).start()
    while not web.started:
        time.sleep(0.05)
    chrome = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome") or [None])[0]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome) if chrome else p.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/")
            page.click('nav button[data-tab="notebook"]')
            page.wait_for_selector("#nbaside aside.tables")  # the table list moved next to JupyterLab
            page.fill("#nbname", "browser")
            page.click("#nbnew")
            page.wait_for_function("document.querySelector('#nbcurrent').textContent.includes('browser')",
                                   timeout=90000)
            page.click('[data-db="cmdb|duckdefault"]')  # databases start closed
            page.locator("#tablelist .titem", has_text="assets").first.click()
            frame = page.frame_locator("#nbframe")
            frame.locator(".jp-Cell .cm-content", has_text='reader.sql("""').first.wait_for(timeout=30000)
            page.click('nav button[data-tab="sql"]')
            assert page.locator("#sqlon aside.tables").count() == 1  # and back
            browser.close()
    finally:
        web.should_exit = True



@pytest.mark.skipif(os.environ.get("DUCKDUCK_BROWSER_TESTS") != "1", reason="browser tests are off — set "
                    "DUCKDUCK_BROWSER_TESTS=1 (needs Playwright's Chromium and pyspark)")
def test_a_cell_switched_to_sql_leaves_a_dataframe_for_pyspark(tmp_path, glue_duck):
    """The toolbar switch, the duckduck kernel and the session together — on the local-Spark kernel (no AWS)."""
    import glob
    import socket

    pytest.importorskip("pyspark")
    pytest.importorskip("aws_glue_interactive_sessions_kernel")
    import uvicorn
    from playwright.sync_api import sync_playwright

    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.engine import SemanticSearch
    from duckduck.semantic.server import create_app

    server = NotebookServer(str(tmp_path / "nbs"), kernels=["duckduck_local_pyspark"])
    app = create_app(lambda: SemanticSearch.turned_off(glue_duck, "not needed here"), store=None,
                     console=SQLConsole(glue_duck), notebook=server)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    web = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=web.run, daemon=True).start()
    while not web.started:
        time.sleep(0.05)
    chrome = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome") or [None])[0]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome) if chrome else p.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            page.goto(f"http://127.0.0.1:{port}/")
            page.click('nav button[data-tab="notebook"]')
            page.fill("#nbname", "sqlmode")
            page.click("#nbnew")
            page.wait_for_function("document.querySelector('#nbcurrent').textContent.includes('sqlmode')",
                                   timeout=90000)
            frame = page.frame_locator("#nbframe")
            frame.locator(".jp-StatusBar-TextItem", has_text="| Idle").first.wait_for(timeout=90000)
            frame.locator(".jp-Notebook .jp-Cell").last.locator(".cm-content").click()
            page.keyboard.type("hosts = spark.createDataFrame([('web','linux'),('db','windows'),('ci','linux')], "
                               "['host','os'])")
            page.keyboard.press("Shift+Enter")
            frame.locator(".jp-Cell .jp-InputPrompt", has_text="[1]").first.wait_for(timeout=180000)
            # the next cell: SQL, over the DataFrame made above — only the SQL is written, and run at once: its
            # DataFrame is named after the table it reads (hosts → hosts_df, never hosts itself)
            frame.locator(".duckduck-langswitch select").select_option("sql")
            frame.locator(".jp-Cell.jp-mod-active .cm-content").click()
            page.keyboard.type("SELECT os, count(*) AS n FROM hosts GROUP BY os ORDER BY os")
            page.keyboard.press("Shift+Enter")
            result = frame.locator(".duckduck-sqlres").first
            result.locator("table").wait_for(timeout=120000)
            assert "hosts_df = spark.sql" in result.inner_text()
            assert "temp views from DataFrames: hosts" in result.inner_text()
            sql_cell = frame.locator(".jp-Cell.duckduck-sql").first
            assert "%%sql" not in sql_cell.locator(".cm-content").inner_text()
            assert sql_cell.locator(".jp-InputPrompt").get_attribute("data-duckduck-df") == "hosts_df"
            # the next cell is SQL already: the language picked last sticks (and is kept in the notebook)
            frame.locator(".jp-Cell.jp-mod-active.duckduck-sql").wait_for(timeout=10000)
            assert frame.locator(".duckduck-langswitch select").input_value() == "sql"
            # another SQL cell, named by hand
            frame.locator(".jp-Cell.jp-mod-active .cm-content").click()
            page.keyboard.type("SELECT 1 AS one")
            frame.locator(".duckduck-langswitch input").fill("counts")
            frame.locator(".duckduck-langswitch input").press("Enter")
            frame.locator(".jp-Cell.jp-mod-active .cm-content").click()
            page.keyboard.press("Shift+Enter")
            frame.locator(".duckduck-sqlres", has_text="counts = spark.sql").wait_for(timeout=60000)
            # back to PySpark: the cell becomes the code it stood for
            sql_cell.click()
            frame.locator(".duckduck-langswitch select").select_option("python")
            frame.locator(".jp-Cell.jp-mod-active .cm-content", has_text='hosts_df = spark.sql("""').wait_for(
                timeout=10000)
            # and the DataFrame is there for the next PySpark cell (the one left empty after counts is SQL: back
            # to PySpark — which then sticks)
            frame.locator(".jp-Cell").last.locator(".cm-content").click()
            frame.locator(".duckduck-langswitch select").select_option("python")
            assert frame.locator(".jp-Cell.duckduck-sql").count() == 1  # only counts' cell is SQL now
            frame.locator(".jp-Cell").last.locator(".cm-content").click()
            page.keyboard.type("hosts_df.where('n > 1').collect()")
            page.keyboard.press("Shift+Enter")
            frame.locator(".jp-OutputArea-output", has_text="Row(os='linux', n=2)").wait_for(timeout=60000)
            assert "duckduck-sql" not in (frame.locator(".jp-Cell.jp-mod-active").get_attribute("class") or "")
            browser.close()
    finally:
        server.stop()
        web.should_exit = True

# ---- SQL cells: %%sql → df = spark.sql(...), and back ----------------------------------------------------------------

from duckduck.notebook import sqlcells  # noqa: E402


def test_sql_header_names_the_dataframe_and_the_engine():
    assert sqlcells.parse_header("%%sql") == {"name": "df", "engine": "spark", "show": 100}
    assert sqlcells.parse_header("%%sql assets --duckduck --show 5") == {"name": "assets", "engine": "duckduck",
                                                                         "show": 5}
    for bad in ("%%sql a b", "%%sql --show", "%%sql __x"):
        with pytest.raises(sqlcells.SqlCellError):
            sqlcells.parse_header(bad)
    assert sqlcells.split_cell(["# a note", "%%sql x", "SELECT 1"]) == ("%%sql x", "SELECT 1")
    assert sqlcells.header_for("df", "duckduck") == "%%sql --duckduck"


class _Schema:
    def __init__(self, cols):
        self.fields = [type("F", (), {"name": c, "dataType": type("T", (), {"simpleString": lambda s: "string"})()})()
                       for c in cols]


def test_session_code_leaves_the_dataframe_and_reads_dataframes_as_views():
    """The code the session runs, on a stand-in for spark (the real thing: test_sql_cells_on_spark)."""
    pytest.importorskip("pyspark")
    from pyspark.sql import DataFrame

    made = []

    class Result:
        schema = _Schema(["os", "n"])

        def limit(self, n):
            return self

        def collect(self):
            return [("linux", 2), ("windows", None)]

    class Spark:
        def sql(self, query):
            made.append(query)
            return Result()

    hosts = object.__new__(DataFrame)
    views = []
    hosts.createOrReplaceTempView = views.append
    scope = {"spark": Spark(), "hosts": hosts, "other": hosts}
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(sqlcells.session_code("%%sql counts", "SELECT os, count(*) n FROM hosts GROUP BY os;"), scope)
    assert made == ["SELECT os, count(*) n FROM hosts GROUP BY os"]
    assert views == ["hosts"]  # named in the SQL; `other` isn't
    assert scope["counts"] is scope["_sqldf"]
    (_, before), (kind, result), (_, after) = sqlcells.split_output("hello\n" + out.getvalue())
    assert (before, kind, after) == ("hello\n", "result", "")
    assert result["rows"] == [["linux", 2], ["windows", None]] and result["views"] == ["hosts"]
    shown = sqlcells.render(result)
    assert "<th" in shown["text/html"] and "counts</code> = spark.sql" in shown["text/html"]
    assert "linux\t2" in shown["text/plain"]
    with pytest.raises(sqlcells.SqlCellError):
        sqlcells.session_code("%%sql", "  ")


def test_duckduck_sql_cells_need_the_reader():
    scope = {}
    with pytest.raises(NameError, match="connect"):
        exec(sqlcells.session_code("%%sql --duckduck", "SELECT 1"), scope)


def test_the_switch_keeps_sql_cells_as_sql_alone_and_the_metadata_makes_them_run_as_sql():
    meta = {"duckduck": {"language": "sql", "engine": "spark", "name": "counts"}}
    assert sqlcells.from_metadata("SELECT 1", meta) == "%%sql counts\nSELECT 1"
    duckduck = {"duckduck": {"language": "sql", "engine": "duckduck", "name": "df", "show": 5}}
    assert sqlcells.from_metadata("SELECT 1", duckduck) == "%%sql --duckduck --show 5\nSELECT 1"
    assert sqlcells.from_metadata("%%sql x\nSELECT 1", meta) == "%%sql x\nSELECT 1"  # written by hand: as is
    assert sqlcells.from_metadata("x = 1", None) == "x = 1"
    assert sqlcells.from_metadata("x = 1", {"duckduck": {"language": "python"}, "cellId": "a"}) == "x = 1"
    bad_name = {"duckduck": {"language": "sql", "name": "1; import os"}}
    assert sqlcells.from_metadata("SELECT 1", bad_name) == "%%sql\nSELECT 1"  # never a name that isn't one


def test_the_kernel_reads_the_cells_metadata():
    pytest.importorskip("aws_glue_interactive_sessions_kernel")
    import inspect

    from duckduck.notebook.kernel import _kernel_class

    assert "cell_meta" in inspect.signature(_kernel_class().do_execute).parameters  # ipykernel then hands it over


def test_the_kernel_sends_session_code_for_sql_and_shows_the_answer_as_a_table():
    pytest.importorskip("aws_glue_interactive_sessions_kernel")
    from duckduck.notebook.kernel import _kernel_class

    kernel = _kernel_class()
    sent, errors = [], []
    fake = type("K", (), {"iopub_socket": None, "_send_error_output": lambda self, m: errors.append(m),
                          "send_response": lambda self, sock, kind, content: sent.append((kind, content))})()
    code = kernel._handle_sql_code(fake, ["%%sql hosts", "SELECT 1"])
    assert "__duckduck_sql('SELECT 1', 'hosts', 'spark', 100)" in code
    assert kernel._handle_sql_code(fake, ["%%sql a b", "SELECT 1"]) is None and "doesn't take" in errors[0]
    payload = sqlcells.MARK + json.dumps({"name": "df", "columns": [["x", "int"]], "rows": [[1]], "show": 100,
                                          "more": False, "views": []}) + sqlcells.END
    kernel._send_output(fake, payload)
    assert sent[0][0] == "display_data" and "text/html" in sent[0][1]["data"]


def test_glue_tables_and_others_come_as_sql_cells_too(glue_duck):
    glue = pyspark_code(glue_duck, "SELECT * FROM s3_data.security.proxy_logs WHERE status = 403 LIMIT 100")
    assert glue["sql"] == "%%sql proxy_logs\nSELECT * FROM security.proxy_logs\nWHERE status = 403"
    other = pyspark_code(glue_duck, "SELECT * FROM assets LIMIT 100")
    assert other["sql"] == "%%sql assets --duckduck\nSELECT * FROM assets LIMIT 100"


def test_the_local_gateway_answers_like_a_glue_session():
    from duckduck.notebook.kernel import LocalGateway

    gateway = LocalGateway()
    gateway.namespace["spark"] = None  # no session needed for plain Python
    first = gateway.run_statement("s", "x = 6\nx * 7")["Id"]
    assert gateway.get_statement("s", first)["Statement"]["Output"]["Data"]["TextPlain"] == "42\n"
    failed = gateway.get_statement("s", gateway.run_statement("s", "1/0")["Id"])["Statement"]["Output"]
    assert failed["Status"] == "error" and failed["ErrorName"] == "ZeroDivisionError"


def test_sql_cells_on_spark(spark):
    """The session code on a real Spark: a DataFrame variable read as a table, the answer left as a DataFrame."""
    import contextlib
    import io

    scope = {"spark": spark, "hosts": spark.createDataFrame([("web", "linux"), ("db", "windows"), ("ci", "linux")],
                                                            ["host", "os"])}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(sqlcells.session_code("%%sql --show 1", "SELECT os, count(*) AS n FROM hosts GROUP BY os ORDER BY os"),
             scope)
    assert [tuple(r) for r in scope["df"].collect()] == [("linux", 2), ("windows", 1)]
    result = next(v for k, v in sqlcells.split_output(out.getvalue()) if k == "result")
    assert result["columns"] == [["os", "string"], ["n", "bigint"]] and result["rows"] == [["linux", 2]]
    assert result["more"] is True


@pytest.fixture(scope="module")
def spark():
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    try:
        session = (SparkSession.builder.master("local[1]").appName("duckduck-notebook-tests")
                   .config("spark.ui.enabled", "false").config("spark.ui.showConsoleProgress", "false")
                   .config("spark.sql.shuffle.partitions", "2").getOrCreate())
    except Exception as exc:  # no Java, …
        pytest.skip(f"no local Spark: {exc}")
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


def test_the_switch_conversions_and_automatic_names_in_javascript():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    folder = os.path.join(os.path.dirname(__file__), "..", "duckduck", "notebook", "labextension-src")
    done = subprocess.run([node, "toggle.test.mjs"], cwd=folder, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr
