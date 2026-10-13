"""Serve Workshop V2 and the existing live demo API on one local origin."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .auditor import AuditStore
from .cases import load_cases
from .config import HarnessConfig
from .demo import CURATED_TASKS, DemoHandler
from .settings import Settings


class WorkshopHandler(DemoHandler):
    # Slow reasoning providers can take over a minute even for a short reply.
    # Keep the original demo's defaults, and bound this live classroom variant.
    chat_options = {"model_timeout": 90.0, "total_timeout": 480.0, "max_tokens": 2048}
    extensions_map = {
        **DemoHandler.extensions_map,
        ".md": "text/plain; charset=utf-8",
        ".py": "text/plain; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
        ".ipynb": "application/octet-stream",
    }

    def __init__(self, *args, workspace_root: Path, **kwargs):
        self.workspace_root = workspace_root.resolve()
        super().__init__(*args, **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        redirects = {"/": "/workshop-v2/index.html", "/workshop-v2": "/workshop-v2/",
                     "/demo.html": "/workshop-v2/demo.html", "/homework": "/homework/"}
        if path in redirects:
            self.send_response(302)
            self.send_header("Location", redirects[path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def translate_path(self, path):
        # Expose the teaching directories, never the workspace root or .env.
        request_path = unquote(urlsplit(path).path).replace("\\", "/")
        for name in ("workshop-v2", "presentation", "notebooks", "homework"):
            prefix = f"/{name}/"
            if not request_path.startswith(prefix):
                continue
            relative = request_path[len(prefix):]
            parts = relative.split("/")
            if any(part.startswith(".") for part in parts if part):
                break
            base = (self.workspace_root / name).resolve()
            target = (base / relative).resolve()
            if target.is_relative_to(base):
                return str(target)
        return str(self.workspace_root / ".workshop-unavailable" / "not-found")


def create_server(root: Path, host: str, port: int) -> ThreadingHTTPServer:
    root = root.resolve()
    settings = Settings.load(root / ".env")
    all_cases = {case.id: case for case in load_cases(root / "benchmarks", split="eval")}
    factory = partial(
        WorkshopHandler,
        workspace_root=root,
        directory=str(root / "workshop-v2"),
        settings=settings,
        cases={key: all_cases[key] for key in CURATED_TASKS},
        configs={"baseline": HarnessConfig.from_yaml(root / "configs/baseline.yaml"),
                 "optimized": HarnessConfig.from_yaml(root / "configs/optimized.yaml")},
        artifact_root=root / "artifacts/demo-v2",
        audit_store=AuditStore(),
    )
    return ThreadingHTTPServer((host, port), factory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8867)
    args = parser.parse_args()
    server = create_server(args.root, args.host, args.port)
    print(f"Workshop V2: http://{args.host}:{server.server_port}/workshop-v2/index.html", flush=True)
    print(f"Live demo: http://{args.host}:{server.server_port}/workshop-v2/demo.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
