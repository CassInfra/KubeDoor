"""Isolated Kubernetes API clients and bounded, non-interactive executors."""

from __future__ import annotations

import asyncio
import copy
import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

from .config import parse_kubeconfig, redact_export
from .policy import API_PATH, DIAGNOSTIC_PROGRAMS, TARGET_FLAGS, classify_operation, cli_verb


class ToolError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class _Run:
    task: asyncio.Task | None = None
    process: Any = None
    stream: Any = None
    response: Any = None
    cancelled: bool = False


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _basename(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) or name in {".", ".."} or ".." in name:
        raise ToolError("INVALID_ARGUMENT", "Files must have plain basenames within the execution workspace")
    if name.startswith(".kubedoor-"):
        raise ToolError("INVALID_ARGUMENT", "Reserved workspace filename")
    return name


def _api_path(path: Any) -> str:
    if not isinstance(path, str):
        raise ToolError("INVALID_ARGUMENT", "Kubernetes API path is required")
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if not API_PATH.fullmatch(path) or any(p in {".", ".."} for p in path.split("/")) or any(c in path for c in "\\?#%\r\n\x00") or "//" in path:
        raise ToolError("INVALID_ARGUMENT", "Only absolute Kubernetes API paths on the selected server are allowed")
    return path


def _argv(arguments: dict) -> list[str]:
    argv = arguments.get("argv", [])
    if not isinstance(argv, list) or len(argv) > 512 or any(not isinstance(a, str) or "\x00" in a for a in argv):
        raise ToolError("INVALID_ARGUMENT", "argv must be an array of strings without NUL bytes")
    if sum(len(a) for a in argv) > 131072:
        raise ToolError("INVALID_ARGUMENT", "Command arguments are too large")
    return list(argv)


def _workspace_files(arguments: dict) -> dict[str, str]:
    files = arguments.get("files", {})
    if not isinstance(files, dict) or len(files) > 100:
        raise ToolError("INVALID_ARGUMENT", "files must be a map of basenames to text")
    for name, value in files.items():
        _basename(name)
        if not isinstance(value, str):
            raise ToolError("INVALID_ARGUMENT", "Workspace file contents must be text")
    if sum(len(v.encode()) for v in files.values()) > 10 * 1024 * 1024:
        raise ToolError("INVALID_ARGUMENT", "Workspace files exceed 10 MiB")
    stdin = arguments.get("stdin")
    if stdin is not None and (not isinstance(stdin, str) or len(stdin.encode()) > 10 * 1024 * 1024):
        raise ToolError("INVALID_ARGUMENT", "stdin must be text of at most 10 MiB")
    return dict(files)


def _validate_cli(operation: str, arguments: dict) -> tuple[str, list[str], dict]:
    program = operation if operation in {"kubectl", "istioctl"} else arguments.get("program")
    if operation == "diagnostic" and program not in DIAGNOSTIC_PROGRAMS:
        raise ToolError("UNSUPPORTED_PROGRAM", "Diagnostic program is not enabled")
    argv, files = _argv(arguments), _workspace_files(arguments)
    if program == "curl":
        normalized = []
        boolean_flags = set("qsSfkvIiLNOJVhaBglR#012346p")
        value_flags = set("EDTobcdHuXxAermwyYzCtPUKF")
        for token in argv:
            if token.startswith("-") and not token.startswith("--") and len(token) > 2:
                index = 1
                while index < len(token):
                    flag = token[index]
                    if flag in value_flags:
                        normalized.append("-" + token[index:])
                        break
                    if flag not in boolean_flags:
                        raise ToolError("INVALID_ARGUMENT", "Use separate or full curl option names")
                    normalized.append("-" + flag)
                    index += 1
            else:
                normalized.append(token)
        argv = normalized
    # Only remote kubectl/istioctl arguments after -- may address Pod paths.
    # Diagnostic operands remain local and must be validated after -- as well.
    before_separator = argv if operation == "diagnostic" else (argv[:argv.index("--")] if "--" in argv else argv)
    if operation == "diagnostic":
        for token in argv:
            if not token.startswith("-") or token.startswith("--") or len(token) <= 2:
                continue
            if program == "jq" and not token.startswith("-f") and any(flag not in "crenRsSCMajb0" for flag in token[1:]):
                raise ToolError("INVALID_ARGUMENT", "Use separate jq options for file arguments")
            if program == "yq" and not token.startswith(("-I", "-p", "-o", "-f")):
                raise ToolError("INVALID_ARGUMENT", "Use separate or full yq option names")
            if program == "rg" and "f" in token[1:] and not token.startswith("-f"):
                raise ToolError("INVALID_ARGUMENT", "Use separate rg options for file arguments")
        denied_flags = {
            "jq": {"-L", "--library-path"},
            "yq": {"-s", "--split-exp", "--split-exp-file"},
            "curl": {"-K", "--config", "-n", "--netrc", "--netrc-optional", "--unix-socket", "--abstract-unix-socket", "--proto", "--proto-redir", "-F", "--form"},
            "rg": {"--pre", "--hostname-bin"},
            "openssl": {"-engine", "-provider", "-provider-path", "-config", "-extfile"},
        }.get(program, set())
        if any(a.split("=", 1)[0] in denied_flags or program == "jq" and a.startswith("-L")
               or program == "curl" and a.startswith(("-K", "-F"))
               or program == "yq" and a.startswith("--security-") for a in argv):
            raise ToolError("UNSUPPORTED_COMMAND", "Diagnostic configuration, plugins and external-command hooks are not allowed")
    if operation in {"kubectl", "istioctl"}:
        if any(a.split("=", 1)[0] in TARGET_FLAGS | {"-s"}
               or a.startswith("-s") and not a.startswith("--")
               or operation == "istioctl" and (a == "-c" or a.startswith("-c") and not a.startswith("--"))
               for a in before_separator):
            raise ToolError("TARGET_OVERRIDE", "Cluster target and credentials cannot be overridden")
        verb, _ = cli_verb(argv)
        if verb in {"config", "proxy", "port-forward", "plugin", "krew", "completion"}:
            raise ToolError("UNSUPPORTED_COMMAND", "This command can change credentials or create an unbounded process")
        if any(a in {"-t", "--tty", "--interactive"} or a.startswith("--tty=") for a in before_separator):
            raise ToolError("INVALID_ARGUMENT", "Interactive commands are not supported")
    file_flags = {"-f", "--filename", "-k", "--kustomize", "--values", "--file", "--config", "-K", "--slurpfile", "--rawfile",
                  "-in", "-out", "-key", "-cert", "-CAfile", "-CApath", "-config", "-rand", "--output", "-o", "--patch-file",
                  "--template", "--from-file", "--from-env-file", "--netrc-file", "--cookie-jar", "--cert",
                  "--key", "--cacert", "--capath", "--upload-file", "-T", "--ignore-file", "--files-from", "--argfile"}
    if program == "curl":
        file_flags |= {"-c", "-b", "--cookie", "-E", "-D", "--dump-header"}
    for index, token in enumerate(before_separator):
        if token in {"--raw"} or token.startswith("--raw="):
            value = token.partition("=")[2] if "=" in token else (before_separator[index + 1] if index + 1 < len(before_separator) else "")
            _api_path(value)
            continue
        if index > 0 and before_separator[index - 1] == "--raw":
            continue
        flag, equal, inline = token.partition("=")
        if token.startswith(("-f", "-k", "-K", "-o")) and not token.startswith("--") and len(token) > 2:
            flag, equal, inline = token[:2], "=", token[2:]
        if program == "curl" and token.startswith(("-T", "-b", "-c", "-E", "-D")) and not token.startswith("--") and len(token) > 2:
            flag, equal, inline = token[:2], "=", token[2:]
        is_file = flag in file_flags
        if operation in {"kubectl", "istioctl"} and flag in {"-o", "--output"}:
            is_file = False  # Kubernetes -o selects an output format, not a host file.
            output = inline if equal else (before_separator[index + 1] if index + 1 < len(before_separator) else "")
            if output.startswith(("go-template-file=", "jsonpath-file=")):
                template_path = output.split("=", 1)[1]
                _basename(template_path)
                if template_path not in files:
                    raise ToolError("INVALID_ARGUMENT", "Output template files must be supplied in files")
        if operation == "diagnostic" and ((program == "rg" and flag == "-o") or (program == "yq" and flag in {"-o", "--output-format"})):
            is_file = False
        if operation == "diagnostic" and ((program == "curl" and flag in {"-f", "-k"}) or (program == "yq" and flag == "-f")):
            is_file = False
        if is_file:
            value = inline if equal else (before_separator[index + 1] if index + 1 < len(before_separator) else "")
            if program == "curl" and flag in {"-b", "--cookie"} and "=" in value:
                continue  # A cookie name=value is literal data, not a file.
            # jq --rawfile/--slurpfile take a variable name before the filename.
            if flag in {"--slurpfile", "--rawfile"}:
                value = before_separator[index + 2] if index + 2 < len(before_separator) else ""
            if flag == "--from-file" and "=" in value:
                value = value.split("=", 1)[1]
            if value == "-":
                continue
            _basename(value)
            output_flag = operation == "diagnostic" and flag in {"-out", "--output", "-o", "--cookie-jar", "-c", "-D", "--dump-header"}
            if not output_flag and value not in files:
                raise ToolError("INVALID_ARGUMENT", "Referenced files must be supplied in files")
        if re.match(r"(?i)^file://", token) or re.search(r"(?:^|[=@])(?:[A-Za-z]:[\\/]|/[^/])|(?:^|[=@/\\])\.\.[/\\]", token):
            # URLs used by curl are network inputs; filesystem URLs are never allowed.
            if not re.match(r"^https?://", token):
                raise ToolError("INVALID_ARGUMENT", "Host filesystem paths are not available to tools")
        if token.startswith("file:"):
            input_file = token.removeprefix("file:")
            _basename(input_file)
            if input_file not in files:
                raise ToolError("INVALID_ARGUMENT", "Diagnostic files must be supplied in files")
        if operation == "diagnostic" and program == "curl" and "@" in token and not re.match(r"^https?://", token):
            input_file = token.rsplit("@", 1)[1].split(";", 1)[0]
            _basename(input_file)
            if input_file not in files:
                raise ToolError("INVALID_ARGUMENT", "curl upload files must be supplied in files")
        if operation in {"kubectl", "istioctl"} and flag in {"-f", "--filename", "-k", "--kustomize"} and equal and "://" in inline:
            raise ToolError("INVALID_ARGUMENT", "Remote manifests are forbidden")
    return str(program), argv, files


class KubernetesExecutor:
    """Each instance owns one selected cluster and an independent ApiClient.

    Authorization is performed by the shared gateway before execute. This class
    validates binding, freezes supported write preconditions, and executes once.
    """

    def __init__(self, kubeconfig: dict | None = None, context: str | None = None):
        self.parsed = parse_kubeconfig(kubeconfig, context) if kubeconfig is not None else None
        if self.parsed and not self.parsed["context"]:
            raise ValueError("Select a kubeconfig context before creating an executor")
        self.context = self.parsed["context"] if self.parsed else context
        self._client = None
        self._configuration = None
        self._initialization_lock = asyncio.Lock()
        self._runs: dict[str, _Run] = {}
        self._closed = False
        self._cert_dir = tempfile.TemporaryDirectory(prefix="kubedoor-certs-")
        self._secret_values = []
        if self.parsed:
            user = self.parsed["config"]["users"][0]["user"]
            self._secret_values = [v for v in user.values() if isinstance(v, str) and v]

    def _export(self, value: Any) -> Any:
        value = redact_export(value)
        if isinstance(value, str):
            for secret in self._secret_values:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {k: self._export(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._export(v) for v in value]
        return value

    def _result(self, success: bool, data: Any = None, *, error: dict | None = None, **extra) -> dict:
        result = {"success": success, "data": self._export(data), "source": "kubernetes",
                  "observed_at": _timestamp(), "truncated": False, **extra}
        if error:
            result["error"] = self._export(error)
        return result

    def _credential_token(self) -> str:
        # SDK releases use either BearerToken or authorization. The public getter
        # runs the in-cluster refresh hook before a projected token is exported
        # to kubectl/istioctl's short-lived kubeconfig.
        authorization = (self._configuration.get_api_key_with_prefix("BearerToken")
                         or self._configuration.get_api_key_with_prefix("authorization") or "")
        token = re.sub(r"(?i)^bearer\s+", "", authorization)
        for value in (authorization, token):
            if value and value not in self._secret_values:
                self._secret_values.append(value)
        return token

    async def _initialize(self):
        async with self._initialization_lock:
            if self._closed:
                raise ToolError("CLOSED", "Executor is closed")
            if self._client is not None:
                return
            from kubernetes import client, config
            from urllib3.util.retry import Retry

            configuration = client.Configuration()
            if self.parsed:
                config.load_kube_config_from_dict(self.parsed["config"], context=self.context,
                                                  client_configuration=configuration, persist_config=False,
                                                  temp_file_path=self._cert_dir.name)
            else:
                config.load_incluster_config(client_configuration=configuration)
            # Disable transport retries and redirects, particularly for writes.
            configuration.retries = Retry(total=0, redirect=0, raise_on_redirect=False)
            self._configuration = configuration
            self._client = client.ApiClient(configuration=configuration)
            self._credential_token()

    async def _api(self, arguments: dict, run: _Run, timeout: float) -> Any:
        await self._initialize()
        path = _api_path(arguments.get("path"))
        method = str(arguments.get("method", "GET")).upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ToolError("INVALID_ARGUMENT", "Unsupported HTTP method")
        query = arguments.get("query", {})
        if not isinstance(query, dict) or any(not isinstance(k, str) for k in query):
            raise ToolError("INVALID_ARGUMENT", "query must be an object")
        content_type = arguments.get("content_type") or ("application/json-patch+json" if isinstance(arguments.get("body"), list)
                                                       else "application/merge-patch+json" if method == "PATCH" else "application/json")
        if content_type not in {"application/json", "application/json-patch+json", "application/merge-patch+json",
                                "application/strategic-merge-patch+json", "application/apply-patch+yaml"}:
            raise ToolError("INVALID_ARGUMENT", "Unsupported Kubernetes content type")

        def request():
            self._credential_token()
            # kubernetes>=37: param_serialize builds the URL, headers and auth; call_api returns the
            # unread urllib3 response for every HTTP status, so API errors are mapped below.
            serialized = self._client.param_serialize(
                method=method, resource_path=path, query_params=list(query.items()),
                header_params={"Accept": "application/json", "Content-Type": content_type},
                body=arguments.get("body"), auth_settings=["BearerToken"])
            response = self._client.call_api(*serialized, _request_timeout=(min(10, timeout), timeout)).response
            run.response = response
            try:
                if run.cancelled:
                    raise ToolError("CANCELLED", "Operation cancelled")
                # Preserve complete resource lists (the UI may list 10,000 objects).
                payload = response.read(64 * 1024 * 1024 + 1)
                if len(payload) > 64 * 1024 * 1024:
                    raise ToolError("OUTPUT_TOO_LARGE", "API response exceeds 64 MiB; use Kubernetes pagination")
                text = payload.decode("utf-8", errors="replace")
                try:
                    value = json.loads(text) if text else None
                except json.JSONDecodeError:
                    value = text
                if response.status >= 300:
                    message = value.get("message", "Kubernetes request failed") if isinstance(value, dict) else "Kubernetes request failed"
                    code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 429: "RATE_LIMITED"}.get(response.status, "API_ERROR")
                    raise ToolError(code, message, response.status)
                return value
            finally:
                response.close()
                response.release_conn()
                run.response = None

        return await asyncio.to_thread(request)

    async def _check_preconditions(self, arguments: dict, run: _Run, timeout: float):
        for expected in arguments.get("_preconditions", []):
            if expected.get("must_not_exist"):
                try:
                    await self._api({"path": expected["path"]}, run, timeout)
                except ToolError as exc:
                    if exc.code == "NOT_FOUND":
                        continue
                    raise
                raise ToolError("CONFLICT", "Resource was created after approval was prepared")
            current = await self._api({"path": expected["path"]}, run, timeout)
            metadata = current.get("metadata", {}) if isinstance(current, dict) else {}
            if metadata.get("resourceVersion") != expected.get("resourceVersion") or metadata.get("uid") != expected.get("uid"):
                raise ToolError("CONFLICT", "Resource changed after approval was prepared; prepare a new operation")

    async def execute(self, operation: str, arguments: dict, call_id: str = "", timeout: float = 120) -> dict:
        if not isinstance(arguments, dict):
            return self._result(False, error={"code": "INVALID_ARGUMENT", "message": "arguments must be an object"})
        if self._closed:
            return self._result(False, error={"code": "CLOSED", "message": "Executor is closed"})
        if call_id and call_id in self._runs:
            return self._result(False, error={"code": "CALL_IN_PROGRESS", "message": "call_id is already running"})
        run = _Run()
        if call_id:
            self._runs[call_id] = run

        async def perform():
            await self._check_preconditions(arguments, run, timeout)
            if run.cancelled:
                raise ToolError("CANCELLED", "Operation cancelled")
            if operation == "api":
                data = await self._api(arguments, run, timeout)
                # A full resource list can be tens of MiB: redact it off the event loop.
                return await asyncio.to_thread(self._result, True, data)
            if operation in {"kubectl", "istioctl", "diagnostic"}:
                return await self._cli(operation, arguments, run, timeout)
            if operation == "pod_exec":
                return await self._pod_exec(arguments, run, timeout)
            raise ToolError("UNSUPPORTED_OPERATION", "Unsupported operation")

        run.task = asyncio.create_task(perform())
        try:
            result = await asyncio.wait_for(run.task, timeout=max(0.01, float(timeout)))
            if run.cancelled:
                return self._result(False, error={"code": "CANCELLED", "message": "Operation cancelled; a submitted write may already have completed"})
            return result
        except asyncio.TimeoutError:
            run.cancelled = True
            await self._stop_run(run)
            return self._result(False, error={"code": "TIMEOUT", "message": "Operation timed out; a submitted write may already have completed"})
        except asyncio.CancelledError:
            run.cancelled = True
            await self._stop_run(run)
            return self._result(False, error={"code": "CANCELLED", "message": "Operation cancelled; a submitted write may already have completed"})
        except ToolError as exc:
            error = {"code": exc.code, "message": str(exc)}
            if exc.status is not None:
                error["status"] = exc.status
            return self._result(False, error=error)
        except Exception as exc:
            # SDK exception bodies can contain submitted credentials. Export only
            # the validated status/message, never an arbitrary exception dump.
            status = getattr(exc, "status", None)
            code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT"}.get(status, "EXECUTION_ERROR")
            error = {"code": code, "message": "Kubernetes operation failed; check connectivity, credentials and arguments"}
            if isinstance(status, int):
                error["status"] = status
            return self._result(False, error=error)
        finally:
            if call_id and self._runs.get(call_id) is run:
                self._runs.pop(call_id, None)

    async def _cli(self, operation: str, arguments: dict, run: _Run, timeout: float) -> dict:
        program, argv, files = _validate_cli(operation, arguments)
        executable = shutil.which(program)
        if executable is None:
            raise ToolError("PROGRAM_UNAVAILABLE", f"{program} is not installed")
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "PATHEXT", "LANG", "LC_ALL") if key in os.environ}
        env.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        with tempfile.TemporaryDirectory(prefix="kubedoor-tool-") as directory:
            for name, content in files.items():
                Path(directory, name).write_text(content, encoding="utf-8")
            if operation == "diagnostic":
                if program == "jq":
                    argv = ["-L", directory, *argv]
                elif program == "yq":
                    argv = ["--security-disable-file-ops", "--security-disable-env-ops", *argv]
                elif program == "curl":
                    argv = ["--disable", "--proto", "=http,https", "--proto-redir", "=http,https", *argv]
            if operation in {"kubectl", "istioctl"}:
                await self._initialize()
                config_path = Path(directory, ".kubedoor-kubeconfig.yaml")
                if self.parsed:
                    config_data = self.parsed["config"]
                    selected_context = self.context
                else:
                    configuration = self._configuration
                    user = {"token": self._credential_token()}
                    cluster = {"server": configuration.host, "insecure-skip-tls-verify": not configuration.verify_ssl}
                    if configuration.ssl_ca_cert:
                        import base64
                        cluster["certificate-authority-data"] = base64.b64encode(Path(configuration.ssl_ca_cert).read_bytes()).decode()
                    selected_context = "kubedoor-incluster"
                    config_data = {"apiVersion": "v1", "kind": "Config", "current-context": selected_context,
                                   "contexts": [{"name": selected_context, "context": {"cluster": "selected", "user": "selected"}}],
                                   "clusters": [{"name": "selected", "cluster": cluster}], "users": [{"name": "selected", "user": user}]}
                config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")
                if os.name != "nt":
                    config_path.chmod(0o600)
                argv = ["--kubeconfig", str(config_path), "--context", selected_context, *argv]
                env["KUBECONFIG"] = str(config_path)
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            run.process = await asyncio.create_subprocess_exec(executable, *argv, cwd=directory, env=env,
                                                              stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                              stderr=asyncio.subprocess.PIPE, **options)
            limit = min(max(int(arguments.get("output_limit", 512 * 1024)), 1024), 4 * 1024 * 1024)

            async def consume(reader):
                saved, size, truncated = [], 0, False
                while chunk := await reader.read(65536):
                    if size < limit:
                        saved.append(chunk[:limit - size])
                    size += len(chunk)
                    truncated |= size > limit
                return b"".join(saved).decode("utf-8", errors="replace"), truncated

            async def feed():
                if arguments.get("stdin") is not None:
                    run.process.stdin.write(arguments["stdin"].encode())
                    await run.process.stdin.drain()
                run.process.stdin.close()

            try:
                stdout, stderr, _ = await asyncio.gather(consume(run.process.stdout), consume(run.process.stderr), feed())
                exit_code = await run.process.wait()
            except BaseException:
                await self._terminate_process(run.process)
                raise
            finally:
                run.process = None

            def export():
                data = {"stdout": self._cli_export(stdout[0]), "stderr": self._cli_export(stderr[0])}
                return self._result(exit_code == 0, data, source=program, exit_code=exit_code,
                                    truncated=stdout[1] or stderr[1],
                                    error={"code": "COMMAND_FAILED", "message": f"{program} exited with status {exit_code}"} if exit_code else None)

            # Parsing and redacting megabytes of output must not block the event loop.
            return await asyncio.to_thread(export)

    def _cli_export(self, text: str) -> str:
        """Recognize structured CLI resources before exporting their text."""
        try:
            value = json.loads(text)
            if isinstance(value, (dict, list)):
                return json.dumps(self._export(value), ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
        if re.search(r"(?m)^kind:\s*Secret(?:List)?\s*$", text):
            try:
                documents = list(yaml.safe_load_all(text))
                return yaml.safe_dump_all(self._export(documents), sort_keys=False, allow_unicode=True)
            except (yaml.YAMLError, RecursionError):
                # An incomplete/truncated secret document cannot be exported.
                return "[REDACTED: incomplete Secret output]"
        return self._export(text)

    async def _pod_exec(self, arguments: dict, run: _Run, timeout: float) -> dict:
        await self._initialize()
        self._credential_token()
        argv = _argv(arguments)
        if not argv:
            raise ToolError("INVALID_ARGUMENT", "Pod command argv is required")
        for field in ("namespace", "pod"):
            if not isinstance(arguments.get(field), str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", arguments[field]):
                raise ToolError("INVALID_ARGUMENT", "A valid namespace and pod are required")
        container = arguments.get("container")
        if container is not None and (not isinstance(container, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", container)):
            raise ToolError("INVALID_ARGUMENT", "Invalid container")
        limit = min(max(int(arguments.get("output_limit", 512 * 1024)), 1024), 4 * 1024 * 1024)

        def run_stream():
            from kubernetes import client
            from kubernetes.stream import stream

            # stream temporarily replaces ApiClient.request: use a separate client
            # so concurrent REST calls retain their own HTTP implementation.
            websocket_client = client.ApiClient(configuration=copy.deepcopy(self._configuration))
            response = None
            try:
                response = stream(client.CoreV1Api(websocket_client).connect_get_namespaced_pod_exec,
                                  arguments["pod"], arguments["namespace"], command=argv, container=container,
                                  stdin=False, stdout=True, stderr=True, tty=False, _preload_content=False,
                                  _request_timeout=timeout)
                run.stream = response
                started, outputs, sizes, truncated = time.monotonic(), [[], []], [0, 0], False
                while response.is_open():
                    if run.cancelled:
                        raise ToolError("CANCELLED", "Pod execution cancelled")
                    if time.monotonic() - started > timeout:
                        raise ToolError("TIMEOUT", "Pod execution timed out")
                    response.update(timeout=min(1, timeout))
                    for index, (peek, read) in enumerate(((response.peek_stdout, response.read_stdout), (response.peek_stderr, response.read_stderr))):
                        while peek():
                            value = read()
                            if sizes[index] < limit:
                                outputs[index].append(value[:limit - sizes[index]])
                            sizes[index] += len(value)
                            truncated |= sizes[index] > limit
                exit_code = response.returncode
                if exit_code is None:
                    raise ToolError("UNKNOWN_EXIT_STATUS", "Pod stream closed without an exit status")
                return self._result(exit_code == 0, {"stdout": "".join(outputs[0]), "stderr": "".join(outputs[1])},
                                    source="pod_exec", exit_code=exit_code, truncated=truncated,
                                    error={"code": "COMMAND_FAILED", "message": f"Pod command exited with status {exit_code}"} if exit_code else None)
            finally:
                if response is not None:
                    response.close()
                run.stream = None
                websocket_client.close()

        return await asyncio.to_thread(run_stream)

    async def test_connection(self) -> dict:
        version = await self.execute("api", {"method": "GET", "path": "/version"}, timeout=15)
        if not version["success"]:
            return version
        identity_response = await self.execute("api", {
            "method": "POST", "path": "/apis/authentication.k8s.io/v1/selfsubjectreviews",
            "body": {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}}, timeout=15)
        user_info = (identity_response.get("data") or {}).get("status", {}).get("userInfo", {}) if identity_response["success"] else {}
        username = user_info.get("username")
        if username == "system:anonymous":
            return self._result(False, error={"code": "ANONYMOUS_IDENTITY", "message": "The API server authenticated this connection as system:anonymous"})
        core = await self.execute("api", {"path": "/api"}, timeout=15)
        groups = await self.execute("api", {"path": "/apis"}, timeout=15)
        namespace = self.parsed["config"]["contexts"][0]["context"].get("namespace", "default") if self.parsed else "default"
        capabilities = [("list_namespaces", "", "namespaces", "list", None, None),
                        ("list_pods", "", "pods", "list", namespace, None),
                        ("read_pod_logs", "", "pods", "get", namespace, "log"),
                        ("patch_deployments", "apps", "deployments", "patch", namespace, None),
                        ("exec_pods", "", "pods", "create", namespace, "exec")]
        permissions = []
        for name, group, resource, verb, ns, subresource in capabilities:
            attributes = {"group": group, "resource": resource, "verb": verb}
            if ns:
                attributes["namespace"] = ns
            if subresource:
                attributes["subresource"] = subresource
            permission = await self.execute("api", {
                "method": "POST", "path": "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                "body": {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview", "spec": {"resourceAttributes": attributes}}}, timeout=15)
            status = (permission.get("data") or {}).get("status", {}) if permission["success"] else {}
            permissions.append({"name": name, "attributes": attributes, "allowed": status.get("allowed"),
                                "reason": status.get("reason", ""), **({"error": permission["error"]} if not permission["success"] else {})})
        protected_probe = None
        if not username:
            # Discovery/version may be publicly accessible. Confirm a protected
            # request while explicitly retaining an unverified identity status.
            protected_probe = await self.execute("api", {"path": "/api/v1/namespaces", "query": {"limit": 1}}, timeout=15)
            if not protected_probe["success"]:
                protected_probe = await self.execute("api", {"path": f"/api/v1/namespaces/{namespace}/pods", "query": {"limit": 1}}, timeout=15)
            if not protected_probe["success"]:
                return self._result(False, {"version": version["data"], "identity_status": "identity_unverified", "permissions": permissions},
                                    error={"code": "IDENTITY_UNVERIFIED", "message": "Identity review and protected resource access could not be verified"})
        data = {"version": version["data"], "identity_status": "verified" if username else "identity_unverified",
                "identity": user_info, "discovery": {"core": core, "groups": groups}, "permissions": permissions}
        if not username:
            data["identity_review_error"] = identity_response.get("error")
            data["protected_access_verified"] = True
        return self._result(True, data)

    async def _terminate_process(self, process):
        if process is None or process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                                                          stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            await process.wait()

    async def _stop_run(self, run: _Run):
        run.cancelled = True
        for target in (run.stream, run.response):
            if target is not None:
                try:
                    target.close()
                except Exception:
                    pass
        await self._terminate_process(run.process)

    async def cancel(self, call_id: str):
        run = self._runs.get(call_id)
        if run is not None:
            await self._stop_run(run)
            if run.task is not None and not run.task.done():
                run.task.cancel()

    async def close(self):
        self._closed = True
        for call_id in list(self._runs):
            await self.cancel(call_id)
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
        self._cert_dir.cleanup()

    async def prepare(self, operation: str, arguments: dict, timeout: float = 120) -> dict:
        if not isinstance(arguments, dict):
            return {"success": False, "arguments": {}, "preview": {}, "read_only": False,
                    "reason": "Invalid arguments", "error": {"code": "INVALID_ARGUMENT", "message": "arguments must be an object"}}
        frozen = copy.deepcopy(arguments)
        policy = classify_operation(operation, frozen)
        preview = {"operation": operation, "arguments": self._export(frozen), "dry_run_supported": False}
        result = {"success": True, "arguments": frozen, "preview": preview, **policy}
        run = _Run()

        async def perform():
            if operation == "api":
                _api_path(frozen.get("path"))
                if not policy["read_only"]:
                    await self._prepare_api(frozen, preview, run)
            elif operation in {"kubectl", "istioctl", "diagnostic"}:
                program, argv, _ = _validate_cli(operation, frozen)
                preview["command"] = [program, *argv]
                if operation == "kubectl" and cli_verb(argv)[0] == "apply":
                    await self._prepare_apply(frozen, preview, run)
            elif operation == "pod_exec":
                _argv(frozen)
                preview["command"] = frozen.get("argv", [])
            else:
                raise ToolError("UNSUPPORTED_OPERATION", "Unsupported operation")
            if frozen.get("_preconditions"):
                result["preconditions"] = copy.deepcopy(frozen["_preconditions"])
            preview["arguments"] = self._export(frozen)

        try:
            await asyncio.wait_for(perform(), timeout=max(0.01, float(timeout)))
        except asyncio.TimeoutError:
            await self._stop_run(run)
            result.update(success=False, error={"code": "TIMEOUT", "message": "Operation preparation timed out"})
        except asyncio.CancelledError:
            await self._stop_run(run)
            raise
        except ToolError as exc:
            error = {"code": exc.code, "message": self._export(str(exc))}
            if exc.status is not None:
                error["status"] = exc.status
            result.update(success=False, error=error)
        except Exception:
            result.update(success=False, error={"code": "PREPARATION_FAILED", "message": "Cannot validate the selected operation"})
        return result

    async def _prepare_api(self, frozen: dict, preview: dict, run: _Run):
        method = str(frozen.get("method", "GET")).upper()
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        path, before = _api_path(frozen["path"]), None
        preconditions = []
        if method != "POST":
            before = await self._api({"path": path}, run, 30)
            metadata = before.get("metadata", {}) if isinstance(before, dict) else {}
            if metadata.get("resourceVersion"):
                preconditions.append({"path": path, "resourceVersion": metadata["resourceVersion"], "uid": metadata.get("uid")})
                body = frozen.get("body")
                if method == "DELETE":
                    body = copy.deepcopy(body) if isinstance(body, dict) else {"apiVersion": "v1", "kind": "DeleteOptions"}
                    body["preconditions"] = {"resourceVersion": metadata["resourceVersion"], "uid": metadata.get("uid")}
                elif isinstance(body, dict):
                    body.setdefault("metadata", {})["resourceVersion"] = metadata["resourceVersion"]
                elif isinstance(body, list):
                    body.insert(0, {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]})
                frozen["body"] = body
        dry = copy.deepcopy(frozen)
        dry["query"] = {**dry.get("query", {}), "dryRun": "All"}
        try:
            after = await self._api(dry, run, 30)
        except ToolError as exc:
            if exc.status in {400, 405, 422} and re.search(r"dry.?run.*(?:not supported|unsupported|not allowed)", str(exc), re.I):
                preview["dry_run_unavailable_reason"] = "Server does not support dry-run for this operation"
                return
            raise
        if method == "POST" and isinstance(frozen.get("body"), dict) and frozen["body"].get("metadata", {}).get("name"):
            preconditions.append({"path": f"{path}/{frozen['body']['metadata']['name']}", "must_not_exist": True})
        frozen["_preconditions"] = preconditions
        preview.update(dry_run_supported=True, diff=self._diff(before, after), result=self._export(after))

    def _diff(self, before: Any, after: Any) -> str:
        a = json.dumps(self._export(before), indent=2, ensure_ascii=False, sort_keys=True).splitlines()
        b = json.dumps(self._export(after), indent=2, ensure_ascii=False, sort_keys=True).splitlines()
        return "\n".join(difflib.unified_diff(a, b, fromfile="current", tofile="proposed", lineterm=""))[:131072]

    async def _prepare_apply(self, frozen: dict, preview: dict, run: _Run):
        argv, files = _argv(frozen), _workspace_files(frozen)
        sources = []
        for index, token in enumerate(argv):
            if token in {"-f", "--filename"} and index + 1 < len(argv):
                sources.append(argv[index + 1])
            elif token.startswith("--filename="):
                sources.append(token.split("=", 1)[1])
            elif token.startswith("-f") and not token.startswith("--") and len(token) > 2:
                sources.append(token[2:])
        if not sources:
            preview["dry_run_unavailable_reason"] = "Manifest-based preparation requires -f with supplied files or stdin"
            return
        namespace = self.parsed["config"]["contexts"][0]["context"].get("namespace", "default") if self.parsed else "default"
        for index, token in enumerate(argv):
            if token in {"-n", "--namespace"} and index + 1 < len(argv):
                namespace = argv[index + 1]
            elif token.startswith("--namespace="):
                namespace = token.split("=", 1)[1]
        preconditions, changes, discoveries = [], [], {}

        def objects(document, depth=0):
            if document is None:
                return
            if not isinstance(document, dict) or not isinstance(document.get("apiVersion"), str) or not isinstance(document.get("kind"), str):
                raise ToolError("INVALID_ARGUMENT", "Each manifest must have apiVersion and kind")
            if document["kind"] == "List":
                if depth >= 16 or not isinstance(document.get("items"), list):
                    raise ToolError("INVALID_ARGUMENT", "Manifest List must contain an items array with at most 16 nested lists")
                for item in document["items"]:
                    yield from objects(item, depth + 1)
            else:
                yield document

        for source in sources:
            content = frozen.get("stdin", "") if source == "-" else files[source]
            try:
                documents = list(yaml.safe_load_all(content))
            except yaml.YAMLError as exc:
                raise ToolError("INVALID_ARGUMENT", "Manifest is not valid YAML/JSON") from exc
            for document in (item for root in documents for item in objects(root)):
                if len(changes) >= 1000:
                    raise ToolError("INVALID_ARGUMENT", "Manifest preparation supports at most 1000 resources")
                metadata = document.setdefault("metadata", {})
                if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"]:
                    raise ToolError("INVALID_ARGUMENT", "Manifest apply requires an explicit resource name")
                version = document["apiVersion"]
                discovery_path = f"/apis/{version}" if "/" in version else f"/api/{version}"
                _api_path(discovery_path)
                if discovery_path not in discoveries:
                    discoveries[discovery_path] = await self._api({"path": discovery_path}, run, 30)
                discovery = discoveries[discovery_path]
                resource = next((r for r in discovery.get("resources", []) if r.get("kind") == document["kind"] and "/" not in r["name"]), None)
                if not resource:
                    raise ToolError("RESOURCE_UNAVAILABLE", "Manifest resource kind is not served by this cluster")
                prefix = f"{discovery_path}/namespaces/{metadata.get('namespace', namespace)}" if resource.get("namespaced") else discovery_path
                path = _api_path(f"{prefix}/{resource['name']}/{metadata['name']}")
                try:
                    before = await self._api({"path": path}, run, 30)
                    current_meta = before["metadata"]
                    metadata["resourceVersion"] = current_meta["resourceVersion"]
                    preconditions.append({"path": path, "resourceVersion": current_meta["resourceVersion"], "uid": current_meta.get("uid")})
                except ToolError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                    before = None
                    preconditions.append({"path": path, "must_not_exist": True})
                changes.append({"resource": path, "diff": self._diff(before, document)})
            updated = yaml.safe_dump_all(documents, sort_keys=False)
            if source == "-":
                frozen["stdin"] = updated
            else:
                frozen.setdefault("files", {})[source] = updated
        frozen["_preconditions"] = preconditions
        # Remove caller output/dry-run settings before generating the exact preview.
        dry_argv, index = [], 0
        while index < len(argv):
            token = argv[index]
            if token in {"-o", "--output", "--dry-run"}:
                index += 2
                continue
            if token.startswith(("--output=", "--dry-run=")):
                index += 1
                continue
            dry_argv.append(token)
            index += 1
        dry = {**frozen, "argv": [*dry_argv, "--dry-run=server", "-o", "json"]}
        validated = await self._cli("kubectl", dry, run, 30)
        if not validated["success"]:
            raise ToolError("DRY_RUN_FAILED", "Server-side manifest dry-run failed; review tool arguments and cluster access")
        preview.update(dry_run_supported=True, changes=changes, validation=validated["data"])
