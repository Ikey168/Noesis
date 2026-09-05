"""Executed only inside the isolated analysis image, with staged JSON inputs."""

import json
import time
from pathlib import Path

import nbformat
from nbclient import NotebookClient


def main():
    settings = json.loads(Path("/input/settings.json").read_text())
    notebook = nbformat.read("/input/notebook.ipynb", as_version=4)
    result = {"status": "failed", "environment": Path("/environment.txt").read_text()}
    try:
        NotebookClient(notebook, timeout=settings["cell_timeout_seconds"], kernel_name="python3",
                       startup_timeout=min(settings["cell_timeout_seconds"], settings["run_timeout_seconds"]),
                       allow_errors=False, store_widget_state=False,
                       resources={"metadata": {"path": "/work"}}).execute()
        result["status"] = "complete"
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc)[:10000])
    finally:
        nbformat.write(notebook, "/output/executed.ipynb")
        Path("/output/result.tmp").write_text(json.dumps(result))
        Path("/output/result.tmp").replace("/output/result.json")
    # Keep bounded tmpfs outputs mounted until the host captures them. Podman's
    # own run timeout remains effective even if the host worker disappears.
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
