"""
The web app's Notebook tab: a JupyterLab, started by ``serve`` next to the app and reached through it.

- ``NotebookServer`` — ``jupyter lab`` as a child process on 127.0.0.1 (a free port, its own random token,
  ``base_url=/notebook/``), started on first use and stopped with the app. Only the configured kernels (the Glue
  ones by default: ``aws-glue-sessions``) are offered, terminals are off, files live in ``notebook.dir``. The
  duckduck JupyterLab extension (``labextensions/``, built from ``labextension-src/``) is loaded from this package:
  it inserts the code the page sends (a click on a table) into the open notebook.
- ``mount(app, server)`` — ``/notebook/…`` proxied to it (HTTP and websockets), the Jupyter token added on the
  way, so the browser never needs it. Reaching it takes a cookie the page gets from ``POST /api/notebook/open``
  (an API call: the app's token applies), so the proxy is no wider open than the API.
- ``POST /api/notebook/code {sql}`` — the cell a click on a table adds (``duckduck.glue_session.pyspark_code``).
- ``POST /api/notebook/new`` — a notebook from ``duckduck.glue_session.notebook_template`` (the Glue magics and the
  ``connect`` cell from ``notebook.glue`` in duckduck.json); ``GET /api/notebook`` — what's set up and what's
  missing.
"""

import atexit
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from ..common.logs import get_logger

logger = get_logger("notebook")

PREFIX = "/notebook"
COOKIE = "duckduck_notebook"
DEFAULT_KERNELS = ("duckduck_glue_pyspark",)  # the Glue PySpark kernel + SQL cells (kernel.py); glue_pyspark works too
NOTEBOOK_KEYS = frozenset({"enabled", "dir", "kernels", "glue"})
#: where JupyterLab finds the prebuilt extension (``labextensions/duckduck-notebook``, built from ``labextension-src``)
LABEXTENSIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "labextensions")
_NAME = re.compile(r"^[\w][\w .-]{0,80}$")
#: hop-by-hop and length headers the proxy never copies
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length"}
#: request headers dropped on the way in: the proxy is the trust boundary (the cookie is SameSite=Strict), and
#: Jupyter would refuse a browser Origin that isn't its own 127.0.0.1 host
_INBOUND_DROP = _HOP | {"origin", "referer", "authorization", "cookie"}


class NotebookError(RuntimeError):
    """The notebook can't start or a request is wrong — the message says what to do."""


def notebook_problems(block: Any) -> List[str]:
    """What's wrong with a config's ``notebook`` block (for ``validate_config``)."""
    if block is None:
        return []
    if not isinstance(block, dict):
        return ['notebook is an object: {"enabled": true, "dir": "notebooks", "glue": {…}}']
    problems = [f"notebook: unknown key {k!r} (it takes {', '.join(sorted(NOTEBOOK_KEYS))})"
                for k in sorted(set(block) - NOTEBOOK_KEYS)]
    if "kernels" in block and not (isinstance(block["kernels"], list) and all(isinstance(k, str) for k in block["kernels"])):
        problems.append('notebook.kernels is a list of kernel names, e.g. ["glue_pyspark"]')
    if "glue" in block:
        from ..glue_session import GlueError, glue_settings

        try:
            glue_settings({"notebook": block})
        except GlueError as exc:
            problems.append(str(exc))
    return problems


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NotebookServer:
    """One JupyterLab for the app (see the module docstring)."""

    def __init__(self, root_dir: str, kernels: Optional[List[str]] = None, glue: Optional[Dict[str, Any]] = None,
                 startup_timeout: float = 60.0):
        self.root_dir = os.path.abspath(root_dir)
        self.kernels = list(kernels or DEFAULT_KERNELS)
        self.glue = dict(glue or {})
        self.startup_timeout = startup_timeout
        self.token = secrets.token_hex(24)  # Jupyter's own: only the proxy sends it
        self.cookie = secrets.token_urlsafe(32)  # what the browser shows the proxy
        self.port: Optional[int] = None
        self.process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._log: Optional[Any] = None

    @classmethod
    def from_config(cls, config: Dict[str, Any], base_dir: str) -> "NotebookServer":
        block = (config or {}).get("notebook") or {}
        problems = notebook_problems(block)
        if problems:
            raise NotebookError("; ".join(problems))
        from ..glue_session import glue_settings

        folder = block.get("dir") or "notebooks"
        if not os.path.isabs(folder):
            folder = os.path.join(base_dir, folder)
        return cls(folder, block.get("kernels"), glue_settings(config))

    # ---- the process --------------------------------------------------------------------------------------------

    @property
    def upstream(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def available_kernels(self) -> Dict[str, str]:
        """Installed kernels by name → where (only the ones this server offers matter) — duckduck's own (``kernel.py``)
        whenever their packages are installed: JupyterLab gets their specs from this server."""
        from .kernel import kernel_specs

        try:
            from jupyter_client.kernelspec import KernelSpecManager
        except ImportError:
            return {}
        return {**KernelSpecManager().find_kernel_specs(), **{name: "duckduck" for name in kernel_specs()}}

    def problems(self) -> List[str]:
        out = []
        try:
            import jupyterlab  # noqa: F401
        except ImportError:
            out.append('JupyterLab isn\'t installed on the server: pip install "duckduck[notebook]"')
            return out
        found = self.available_kernels()
        offered = [k for k in self.kernels if k in found]
        if not offered:
            glue = any("glue_" in k for k in self.kernels)
            out.append(f"none of the kernels {self.kernels} is installed"
                       + (' — pip install "duckduck[notebook]" (aws-glue-sessions)' if glue else ""))
        if not os.path.isfile(os.path.join(LABEXTENSIONS, "duckduck-notebook", "package.json")):
            out.append("the duckduck JupyterLab extension isn't built (duckduck/notebook/labextensions: npm install && npm run build in labextension-src) — "
                       "clicking a table can't add a cell")
        if any("glue_" in k for k in self.kernels):
            if not self.glue.get("iam_role"):
                out.append("notebook.glue.iam_role isn't set — each notebook has to set %iam_role itself")
            if not self.glue.get("artifacts"):
                out.append("notebook.glue.artifacts isn't set — run python -m duckduck.glue_session publish s3://… and "
                           "set it to that prefix, so the session installs duckduck")
        return out

    def _config_file(self) -> str:
        config = {
            "ServerApp": {"ip": "127.0.0.1", "port": self.port, "port_retries": 0, "open_browser": False,
                          "base_url": PREFIX + "/", "root_dir": self.root_dir, "terminals_enabled": False,
                          "allow_remote_access": False, "quit_button": False,
                          # containers often run as root; it only listens on 127.0.0.1 behind the proxy
                          "allow_root": hasattr(os, "geteuid") and os.geteuid() == 0},
            "IdentityProvider": {"token": self.token},
            "KernelSpecManager": {"allowed_kernelspecs": self.kernels},
            "LabApp": {"extra_labextensions_path": [LABEXTENSIONS], "news_url": None, "check_for_updates_class":
                       "jupyterlab.handlers.announcements.NeverCheckForUpdate"},
            "LabServerApp": {"extra_labextensions_path": [LABEXTENSIONS]},
        }
        folder = tempfile.mkdtemp(prefix="duckduck-jupyter-")
        path = os.path.join(folder, "jupyter_server_config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        # duckduck's kernels: their specs in a data folder of this server's own (JUPYTER_PATH)
        from .kernel import write_kernel_specs

        write_kernel_specs(os.path.join(folder, "data", "kernels"))
        # no "news from Jupyter?" prompt inside the app: a page config in this config dir turns it off
        os.makedirs(os.path.join(folder, "labconfig"), exist_ok=True)
        with open(os.path.join(folder, "labconfig", "page_config.json"), "w", encoding="utf-8") as fh:
            json.dump({"disabledExtensions": {"@jupyterlab/apputils-extension:announcements": True}}, fh)
        return path

    def start(self) -> None:
        """Starts JupyterLab (once) and waits until it answers."""
        with self._lock:
            if self.running:
                return
            try:
                import jupyterlab  # noqa: F401
            except ImportError:
                raise NotebookError('JupyterLab isn\'t installed on the server: pip install "duckduck[notebook]"') \
                    from None
            os.makedirs(self.root_dir, exist_ok=True)
            self.port = _free_port()
            config = self._config_file()
            self._log = tempfile.NamedTemporaryFile(prefix="duckduck-jupyter-", suffix=".log", delete=False)
            data = os.path.join(os.path.dirname(config), "data")
            env = {**os.environ, "JUPYTER_CONFIG_DIR": os.path.dirname(config), "JUPYTER_PLATFORM_DIRS": "1",
                   "JUPYTER_PATH": os.pathsep.join(p for p in (data, os.environ.get("JUPYTER_PATH")) if p)}
            self.process = subprocess.Popen(
                [sys.executable, "-m", "jupyterlab", f"--config={config}"],
                stdout=self._log, stderr=subprocess.STDOUT, cwd=self.root_dir, env=env)
            atexit.register(self.stop)
            self._wait()
            logger.info("JupyterLab ready on 127.0.0.1:%s (kernels %s, notebooks in %s)", self.port,
                        ", ".join(self.kernels), self.root_dir)

    def _wait(self) -> None:
        import urllib.request

        deadline = time.time() + self.startup_timeout
        url = f"{self.upstream}{PREFIX}/api/status"
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise NotebookError(f"JupyterLab stopped right away — its log: {self.log_tail()}")
            try:
                req = urllib.request.Request(url, headers={"Authorization": f"token {self.token}"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:  # noqa: BLE001 — not up yet
                time.sleep(0.3)
        self.stop()
        raise NotebookError(f"JupyterLab didn't answer in {self.startup_timeout:.0f}s — its log: {self.log_tail()}")

    def log_tail(self, lines: int = 15) -> str:
        if not self._log:
            return ""
        try:
            with open(self._log.name, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:]).strip()
        except OSError:
            return ""

    def stop(self) -> None:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None

    # ---- notebooks ----------------------------------------------------------------------------------------------

    def new_notebook(self, name: Optional[str] = None, kernel: Optional[str] = None) -> str:
        """A notebook from the Glue template in ``root_dir`` → its path relative to it (never overwrites)."""
        from ..glue_session import notebook_template

        name = (name or "glue-session").strip()
        if name.endswith(".ipynb"):
            name = name[:-6]
        if not _NAME.match(name) or ".." in name:
            raise NotebookError("a notebook name is letters, digits, spaces, '-', '_' or '.'")
        kernel = kernel or next((k for k in self.kernels if k in self.available_kernels()), self.kernels[0])
        if kernel not in self.kernels:
            raise NotebookError(f"kernel {kernel!r} isn't offered here ({', '.join(self.kernels)})")
        os.makedirs(self.root_dir, exist_ok=True)
        path, n = f"{name}.ipynb", 1
        while os.path.exists(os.path.join(self.root_dir, path)):
            n += 1
            path = f"{name}-{n}.ipynb"
        with open(os.path.join(self.root_dir, path), "w", encoding="utf-8") as fh:
            json.dump(notebook_template(self.glue, kernel=kernel), fh, indent=1)
        return path

    def notebooks(self) -> List[str]:
        if not os.path.isdir(self.root_dir):
            return []
        return sorted((f for f in os.listdir(self.root_dir) if f.endswith(".ipynb")),
                      key=lambda f: -os.path.getmtime(os.path.join(self.root_dir, f)))

    def status(self) -> Dict[str, Any]:
        return {"running": self.running, "dir": self.root_dir, "kernels": self.kernels,
                "installed_kernels": sorted(set(self.kernels) & set(self.available_kernels())),
                "notebooks": self.notebooks()[:50], "problems": self.problems(),
                "glue": {k: v for k, v in self.glue.items() if v not in (None, "", [])}}


# ---- the proxy ----------------------------------------------------------------------------------------------------

def mount(app: Any, server: "NotebookServer", duck: Any = None) -> None:
    """``/api/notebook*`` and the ``/notebook/…`` proxy on a FastAPI app; ``duck()`` → the DuckAPI whose tables the
    page lists (for ``POST /api/notebook/code``)."""
    import httpx
    from fastapi import Body, HTTPException, Request, WebSocket
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    client: Dict[str, Any] = {}

    def http() -> Any:
        if "c" not in client:
            client["c"] = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=False)
        return client["c"]

    def allowed(cookies: Dict[str, str]) -> bool:
        return secrets.compare_digest(cookies.get(COOKIE, ""), server.cookie)

    @app.get("/api/notebook")
    def notebook_status():
        return JSONResponse(server.status())

    @app.post("/api/notebook/open")
    def notebook_open():
        try:
            server.start()
        except NotebookError as exc:
            raise HTTPException(503, str(exc))
        response = JSONResponse({**server.status(), "url": f"{PREFIX}/lab"})
        response.set_cookie(COOKIE, server.cookie, httponly=True, samesite="strict", path=PREFIX)
        return response

    @app.post("/api/notebook/new")
    def notebook_new(body: Dict[str, Any] = Body(default={})):
        try:
            path = server.new_notebook(body.get("name"), body.get("kernel"))
        except NotebookError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse({"path": path, "url": f"{PREFIX}/lab/tree/{path}"})

    @app.post("/api/notebook/code")
    def notebook_code(body: Dict[str, Any] = Body(default={})):
        from ..glue_session import GlueError, pyspark_code

        if duck is None:
            raise HTTPException(404, "no tables to read here")
        try:
            return JSONResponse(pyspark_code(duck(), str(body.get("sql") or "")))
        except GlueError as exc:
            raise HTTPException(400, str(exc))

    @app.api_route(PREFIX + "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def notebook_proxy(path: str, request: Request):
        if not allowed(request.cookies):
            return Response("Open the Notebook tab in duckduck first.", status_code=401, media_type="text/plain")
        if not server.running:
            return Response("JupyterLab isn't running — open the Notebook tab again.", status_code=503,
                            media_type="text/plain")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _INBOUND_DROP}
        others = "; ".join(f"{k}={v}" for k, v in request.cookies.items() if k != COOKIE)
        if others:
            headers["cookie"] = others
        headers["authorization"] = f"token {server.token}"
        url = f"{server.upstream}{PREFIX}/{path}"
        upstream = await http().send(
            http().build_request(request.method, url, params=request.query_params, headers=headers,
                                 content=await request.body()), stream=True)
        out = [(k, v) for k, v in upstream.headers.multi_items() if k.lower() not in _HOP]

        async def body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        response = StreamingResponse(body(), status_code=upstream.status_code)
        for k, v in out:
            response.headers.append(k, v)
        return response

    @app.websocket(PREFIX + "/{path:path}")
    async def notebook_socket(ws: WebSocket, path: str):
        import asyncio

        import websockets

        if not allowed(ws.cookies) or not server.running:
            await ws.close(code=4401)
            return
        protocols = [p.strip() for p in (ws.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
        query = ("?" + str(ws.url.query)) if ws.url.query else ""
        target = f"ws://127.0.0.1:{server.port}{PREFIX}/{path}{query}"
        others = "; ".join(f"{k}={v}" for k, v in ws.cookies.items() if k != COOKIE)
        extra = {"Authorization": f"token {server.token}", **({"Cookie": others} if others else {})}
        try:
            upstream = await websockets.connect(target, additional_headers=extra, subprotocols=protocols or None,
                                                max_size=None, open_timeout=20)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notebook websocket %s: %s", path, exc)
            await ws.close(code=1011)
            return
        await ws.accept(subprotocol=upstream.subprotocol)

        async def to_upstream():
            try:
                while True:
                    message = await ws.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])
            finally:
                await upstream.close()

        async def to_browser():
            try:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await ws.send_bytes(message)
                    else:
                        await ws.send_text(message)
            except Exception:  # noqa: BLE001 — the browser went away
                pass
            finally:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(to_upstream(), to_browser(), return_exceptions=True)



__all__ = ["COOKIE", "DEFAULT_KERNELS", "NotebookError", "NotebookServer", "PREFIX", "mount", "notebook_problems"]
