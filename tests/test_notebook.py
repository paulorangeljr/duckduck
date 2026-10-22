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
    assert nb["metadata"]["kernelspec"]["name"] == "glue_pyspark"
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
    assert server.kernels == ["glue_pyspark"]
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
