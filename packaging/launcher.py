"""Desktop launcher for the packaged (PyInstaller) builds.

Starts the MeshWX web server and opens the dashboard in the default browser.
Used as the entry point for the Windows/Linux standalone bundles; the Docker
image and `python -m app.main` path do NOT use this.
"""
from __future__ import annotations

import os
import threading
import time
import webbrowser

# Sensible defaults for a double-click launch; env vars still override.
os.environ.setdefault("MESH_WX_HOST", "127.0.0.1")
os.environ.setdefault("MESH_WX_PORT", "8000")


def _open_browser(url: str) -> None:
    time.sleep(1.5)  # give uvicorn a moment to bind
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    host = os.environ.get("MESH_WX_HOST", "127.0.0.1")
    port = os.environ.get("MESH_WX_PORT", "8000")
    # 0.0.0.0 isn't browsable; point the browser at loopback.
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{browse_host}:{port}"
    print("=" * 60)
    print(f"  MeshWX is starting - open {url}")
    print("  Keep this window open. Close it to stop MeshWX.")
    print("=" * 60)
    threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    from app.main import main as run_server
    run_server()


if __name__ == "__main__":
    main()
