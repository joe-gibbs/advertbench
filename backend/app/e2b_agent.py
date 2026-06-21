import base64
import json
from collections.abc import Iterable

from .model_config import AdSize
from .settings import get_settings

MAX_TOOL_OUTPUT_CHARS = 12000


class E2BAgentSandbox:
    def __init__(self) -> None:
        if not get_settings().e2b_api_key:
            raise RuntimeError("E2B_API_KEY is not configured")
        try:
            from e2b_code_interpreter import Sandbox  # type: ignore
        except ImportError as error:
            raise RuntimeError("Install e2b-code-interpreter to use E2B generation") from error

        settings = get_settings()
        self._command_timeout_seconds = settings.e2b_command_timeout_seconds
        self._sandbox = Sandbox.create(timeout=settings.e2b_sandbox_timeout_seconds)

    def close(self) -> None:
        close = getattr(self._sandbox, "kill", None) or getattr(self._sandbox, "close", None)
        if close:
            close()

    def run_bash(self, command: str, timeout_seconds: int | None = None) -> dict:
        timeout_seconds = timeout_seconds or self._command_timeout_seconds
        runner = f"""
import json, subprocess
cmd = {json.dumps(command)}
try:
    run = subprocess.run(
        cmd,
        shell=True,
        executable="/bin/bash",
        cwd="/tmp",
        capture_output=True,
        text=True,
        timeout={int(timeout_seconds)},
    )
    result = {{
        "ok": run.returncode == 0,
        "returncode": run.returncode,
        "stdout": run.stdout[-{MAX_TOOL_OUTPUT_CHARS}:],
        "stderr": run.stderr[-{MAX_TOOL_OUTPUT_CHARS}:],
    }}
except subprocess.TimeoutExpired as error:
    result = {{
        "ok": False,
        "returncode": None,
        "stdout": (error.stdout or "")[-{MAX_TOOL_OUTPUT_CHARS}:],
        "stderr": ((error.stderr or "") + "\\ncommand timed out")[-{MAX_TOOL_OUTPUT_CHARS}:],
    }}
print(json.dumps(result))
"""
        return self._run_json(runner)

    def view_image(self, path: str) -> dict:
        runner = f"""
import base64, json, os
path = {json.dumps(path)}
try:
    with open(path, "rb") as f:
        raw = f.read()
    mime = "image/png" if raw.startswith(b"\\x89PNG\\r\\n\\x1a\\n") else "application/octet-stream"
    result = {{
        "ok": mime.startswith("image/"),
        "path": path,
        "mime": mime,
        "bytes": len(raw),
        "data_url": f"data:{{mime}};base64," + base64.b64encode(raw).decode("ascii"),
    }}
except Exception as error:
    result = {{"ok": False, "path": path, "error": str(error)}}
print(json.dumps(result))
"""
        return self._run_json(runner)

    def collect_outputs(self, sizes: Iterable[AdSize]) -> dict[str, bytes]:
        size_specs = [
            {"key": size.key, "width": size.width, "height": size.height, "path": f"/tmp/advertbench-output/{size.key}.png"}
            for size in sizes
        ]
        runner = f"""
import base64, json, struct
sizes = {json.dumps(size_specs)}
files = {{}}
errors = []
for size in sizes:
    path = size["path"]
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if not raw.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
            errors.append(f"{{path}} is not a PNG")
            continue
        width, height = struct.unpack(">II", raw[16:24])
        if width != size["width"] or height != size["height"]:
            errors.append(f"{{path}} is {{width}}x{{height}}, expected {{size['width']}}x{{size['height']}}")
            continue
        files[size["key"]] = base64.b64encode(raw).decode("ascii")
    except Exception as error:
        errors.append(f"{{path}}: {{error}}")
print(json.dumps({{"ok": not errors, "files": files, "errors": errors}}))
"""
        parsed = self._run_json(runner)
        if not parsed.get("ok"):
            raise RuntimeError("; ".join(parsed.get("errors", [])) or "Required output files are missing")
        return {key: base64.b64decode(value) for key, value in parsed["files"].items()}

    def _run_json(self, code: str) -> dict:
        execution = self._sandbox.run_code(code)
        text = getattr(execution, "text", None) or "\n".join(getattr(getattr(execution, "logs", None), "stdout", []) or [])
        if not text.strip():
            raise RuntimeError("E2B returned no tool output")
        return json.loads(text.strip().splitlines()[-1])
