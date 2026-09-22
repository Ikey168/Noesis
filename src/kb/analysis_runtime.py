"""Rootless Podman boundary for credential-free, network-free nbclient runs."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


class AnalysisRuntimeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class PodmanNotebookRuntime:
    def __init__(self, *, runner_path=None):
        self.binary = shutil.which("podman")
        self.runner = Path(runner_path or Path(__file__).resolve().parents[2] / "scripts/analysis_container_runner.py").resolve()
        # These are host runtime settings only; none are copied into the container.
        self.env = {key: os.environ[key] for key in ("PATH", "HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "DBUS_SESSION_BUS_ADDRESS", "LANG") if key in os.environ}

    def _command(self, args, timeout=10):
        if not self.binary:
            raise AnalysisRuntimeError("runtime_unavailable", "rootless Podman is required for notebook isolation")
        try:
            return subprocess.run([self.binary, *args], capture_output=True, timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired:
            raise AnalysisRuntimeError("runtime_timeout", "container runtime command exceeded its deadline") from None

    def _read(self, name, filename, maximum, timeout):
        # The fixed reader runs inside the container; host code never follows an
        # output symlink or extracts an untrusted archive into the filesystem.
        script = """import os,sys,stat
try:
 fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW)
except FileNotFoundError:
 sys.exit(3)
if not stat.S_ISREG(os.fstat(fd).st_mode): sys.exit(4)
data=os.read(fd,int(sys.argv[2])+1)
os.close(fd)
if len(data)>int(sys.argv[2]): sys.exit(5)
sys.stdout.buffer.write(data)
"""
        response = self._command(["exec", name, "/usr/local/bin/python", "-I", "-c", script, "/output/" + filename, str(maximum)], timeout=timeout)
        if response.returncode == 3:
            return None
        if response.returncode:
            raise AnalysisRuntimeError("output_unavailable", "container output is missing, oversized, or not a regular file")
        return response.stdout

    def execute(self, manifest, frozen_inputs, *, run_id, cancelled=lambda: False):
        image = manifest["environment"]["image_id"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise AnalysisRuntimeError("invalid_environment", "execution requires an immutable local image SHA256 identity")
        budgets = manifest["budgets"]
        deadline = time.monotonic() + budgets["run_timeout_seconds"]
        name = "noesis-analysis-" + re.sub(r"[^a-z0-9]", "", run_id)[-40:]
        with tempfile.TemporaryDirectory(prefix="noesis-analysis-input-") as temporary:
            root = Path(temporary) / "input"
            root.mkdir(mode=0o755)
            (root / "notebook.ipynb").write_text(json.dumps(manifest["notebook"]))
            (root / "datasets.json").write_text(json.dumps(frozen_inputs))
            (root / "parameters.json").write_text(json.dumps(manifest["parameters"]))
            (root / "settings.json").write_text(json.dumps(budgets))
            # Copy the trusted runner into the same read-only input mount.
            (root / "runner.py").write_bytes(self.runner.read_bytes())
            for path in root.iterdir():
                path.chmod(0o644)
            try:
                info = self._command(["info", "--format", "{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}}"], timeout=min(10, max(1, deadline-time.monotonic())))
                if info.returncode or info.stdout.strip() != b"true v2":
                    raise AnalysisRuntimeError("isolation_unavailable", "rootless Podman with cgroups v2 is required")
                args = ["run", "--detach", "--pull=never", "--name", name, "--network=none", "--read-only", "--read-only-tmpfs=false", "--stop-timeout=0",
                        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=1000:1000", "--pids-limit=128",
                        "--memory", str(budgets["memory_mb"]) + "m", "--memory-swap", str(budgets["memory_mb"]) + "m",
                        "--cpus", str(budgets["cpus"]), "--timeout", str(max(1, int(deadline-time.monotonic()))), "--log-driver=none",
                        "--tmpfs", "/tmp:rw,size=128m,mode=1777", "--tmpfs", "/work:rw,size=128m,mode=1777",
                        "--tmpfs", f"/output:rw,size={budgets['max_output_bytes']},mode=1777",
                        "--volume", f"{root}:/input:ro,Z", "--workdir=/work", "--entrypoint=/usr/local/bin/python",
                        image, "-I", "/input/runner.py"]
                if cancelled():
                    return {"status": "cancelled", "error": "cancelled before container launch"}
                launch = self._command(args, timeout=min(20, max(1, deadline-time.monotonic())))
                if launch.returncode:
                    raise AnalysisRuntimeError("container_start_failed", launch.stderr.decode(errors="replace")[:1000])
                while time.monotonic() < deadline:
                    if cancelled():
                        return {"status": "cancelled", "error": "cancelled during execution"}
                    remaining = deadline - time.monotonic()
                    try:
                        content = self._read(name, "result.json", 20000, timeout=min(5, max(0.1, remaining)))
                    except AnalysisRuntimeError as exc:
                        state = self._command(["inspect", "--format", "{{.State.Running}} {{.State.OOMKilled}}", name], timeout=2)
                        if state.stdout.strip().startswith(b"false"):
                            return {"status": "failed", "error_type": "container_stopped", "oom_killed": state.stdout.strip().endswith(b"true")}
                        raise exc
                    if content is not None:
                        result = json.loads(content)
                        notebook = self._read(name, "executed.ipynb", budgets["max_output_bytes"], timeout=min(5, max(0.1, deadline-time.monotonic())))
                        if notebook is None:
                            raise AnalysisRuntimeError("output_unavailable", "runner returned no executed notebook")
                        result["notebook"] = json.loads(notebook)
                        result["isolation"] = {"runtime": "rootless-podman", "network": "none", "image_id": image,
                                               "application_credentials_inherited": False, "budgets": budgets}
                        return result
                    time.sleep(min(0.2, max(0, deadline-time.monotonic())))
                return {"status": "timeout", "error": "end-to-end notebook deadline exhausted"}
            finally:
                # Includes a launch timeout: the daemon may have created the named
                # container before the client timed out. Only this run name is removed.
                if self.binary:
                    self._command(["rm", "--force", "--ignore", "--time", "0", name], timeout=10)
