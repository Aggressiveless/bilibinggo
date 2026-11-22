"""Authenticated local API, supervisor and isolated execution registry."""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

from binggo_mcp.config import load_profile


class Runtime:
    def __init__(self, profile=None):
        self.profile = profile or load_profile()
        if self.profile["source_root"] not in sys.path:
            sys.path.insert(0, self.profile["source_root"])
        from src.control_store import ControlStore
        self.store = ControlStore(Path(self.profile["user_home"]) / "data/control")
        self.lock = asyncio.Lock()
        self.owner_id = uuid.uuid4().hex
        self.processes = {}
        for job in self.store.list_jobs(state="running", limit=100):
            owner, _, _ = self.store.job_record(job["job_id"])
            if owner != "app" and job.get("owner_signature") and process_signature(job.get("owner_pid")) != job["owner_signature"]:
                self.store.update_job(job["job_id"], state="interrupted", result={"code": "EXECUTOR_EXITED"})

    def access(self):
        return json.loads((Path(self.profile["user_home"]) / "config/control-access.json").read_text(encoding="utf-8"))

    def descriptor(self):
        from src.control_store import ControlError
        path = self.store.root / "instance.json"
        if not path.exists():
            raise ControlError("INSTANCE_NOT_FOUND", "应用尚未初始化控制接口，请启动升级后的应用")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["instance_id"] != self.profile["instance_id"]:
            raise ControlError("INSTANCE_CHANGED", "描述符不属于登记的数据根")
        endpoint = urlsplit(value["endpoint"])
        if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "::1", "localhost"} or endpoint.username or endpoint.password or endpoint.path:
            raise ControlError("VALIDATION_ERROR", "控制地址必须是登记的本机HTTP端点")
        return value

    async def request(self, method, path, body=None):
        from src.control_store import ControlError
        info = self.descriptor()
        headers = {"Authorization": "Bearer " + self.access()["token"]}
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(120, connect=3)) as client:
                response = await client.request(method, info["endpoint"] + path, json=body, headers=headers)
        except httpx.HTTPError:
            raise ControlError("NETWORK_UNREACHABLE", "无法连接已登记的Binggo控制接口", retryable=True)
        if response.status_code != 200:
            raise ControlError("AUTH_REQUIRED" if response.status_code == 401 else "CONTROL_HTTP_ERROR", f"控制接口HTTP {response.status_code}")
        result = response.json()
        if path == "/api/control/v2/identity" and (result["instance_id"] != info["instance_id"] or result["boot_id"] != info["boot_id"]):
            raise ControlError("INSTANCE_CHANGED", "服务身份与本机描述符不一致")
        return result

    async def invoke(self, name, arguments):
        from src.control_contract import catalog, validate_input, validate_output, envelope, error_result
        from src.control_store import ControlError
        async with self.lock:
            try:
                entry = validate_input(name, arguments)
                if arguments.get("instance_id", self.profile["instance_id"]) != self.profile["instance_id"]:
                    raise ControlError("INSTANCE_NOT_FOUND", "连接仅允许控制已登记的实例")
                scopes = set(self.access()["scopes"])
                if not set(entry["x-authorizationScopes"]).issubset(scopes):
                    raise ControlError("SCOPE_DENIED", "连接没有所需权限")
                local = entry["group"] in {"testing", "development"} or name in {"binggo_instances_list", "binggo_app_start", "binggo_app_stop", "binggo_app_restart"}
                if name in {"binggo_job_get", "binggo_job_wait", "binggo_job_cancel", "binggo_job_events"}:
                    owner, _, value = self.store.job_record(arguments["job_id"])
                    local = owner != "app"
                if local:
                    if entry["group"] == "development" and not self.profile.get("enable_development"):
                        raise ControlError("SCOPE_DENIED", "开发工具未启用")
                    if "expected_boot_id" in arguments and arguments["expected_boot_id"] != self.descriptor()["boot_id"]:
                        raise ControlError("INSTANCE_CHANGED", "应用启动身份已变化")
                    key = None
                    if "idempotency_key" in arguments:
                        key = f"{self.profile['instance_id']}:local-owner:{name}:{arguments['idempotency_key']}"
                        prior = self.store.claim(key, arguments)
                        if prior is not None:
                            return prior
                    try:
                        data = await self.local(name.removeprefix("binggo_"), arguments)
                        result = envelope(self.profile["instance_id"], data, status="accepted" if entry["x-execution"] == "submit-and-return-job-id" else "completed")
                    except Exception as exc:
                        result = error_result(self.profile["instance_id"], exc)
                    if key:
                        self.store.complete(key, result)
                else:
                    # An authenticated identity read precedes every call; a port alone is not identity.
                    await self.request("GET", "/api/control/v2/identity")
                    result = await self.request("POST", "/api/control/v2/tools/" + name, arguments)
                validate_output(name, result)
                return result
            except Exception as exc:
                return error_result(self.profile["instance_id"], exc)

    async def local(self, name, args):
        from src.control_store import ControlError
        if name == "instances_list":
            info = self.descriptor()
            try:
                info = await self.request("GET", "/api/control/v2/identity")
            except ControlError:
                info = {**info, "state": "stopped", "verified_identity": False}
            return {"instances": [info], "next_cursor": None}
        if name in {"app_start", "app_stop", "app_restart"}:
            return self.background(name, lambda job: self.lifecycle(name, args), owner="supervisor")
        if name == "job_get":
            return self.store.job_record(args["job_id"])[2]
        if name == "job_wait":
            deadline = time.monotonic() + args.get("timeout_seconds", 10)
            while True:
                value = self.store.job_record(args["job_id"])[2]
                if value["state"] != "running" or time.monotonic() >= deadline:
                    return value
                await asyncio.sleep(0.2)
        if name == "job_events":
            return self.store.events(args["job_id"], args.get("after_seq", 0), args.get("limit", 100))
        if name == "job_cancel":
            raise ControlError("JOB_NOT_CANCELLABLE", "本机构建/测试任务不支持中途撤销，请等待结果")
        if name == "artifact_get":
            return self.store.read_artifact(args["artifact_id"], args.get("offset_bytes", 0), args.get("max_bytes", 65536))
        if name == "test_suites_list":
            return {"suites": [{"suite_id": key, "modes": ["offline"], "files": files} for key, files in self.suites().items()] +
                    [{"suite_id": "network-read", "modes": ["live_read"]}, {"suite_id": "live-participation", "modes": ["live_write"]}], "next_cursor": None}
        if name == "test_run":
            mode = args["mode"]
            scopes = set(self.access()["scopes"])
            if mode != "offline" and "platform:read" not in scopes or mode == "live_write" and "platform:write" not in scopes:
                raise ControlError("SCOPE_DENIED", "真实网络测试没有对应授权")
            test_run_id = uuid.uuid4().hex
            return self.background("test", lambda job: self.run_test(job, test_run_id, args), owner="test", metadata={"test_run_id": test_run_id})
        if name == "test_get":
            value = self.store.get("test", args["test_run_id"])
            if not value:
                raise ControlError("NOT_FOUND", "未找到测试报告")
            return value
        if name == "build_start":
            return self.background("build", lambda job: self.build(job, args), owner="build")
        if name == "build_get":
            value = self.store.get("build", args["build_id"])
            if not value:
                raise ControlError("NOT_FOUND", "未找到构建")
            return value
        if name in {"release_activate", "release_rollback"}:
            return self.background(name, lambda job: self.release(name, args), owner="supervisor")
        raise ControlError("NOT_FOUND", "未登记的本机操作")

    def background(self, action, function, *, owner, metadata=None):
        metadata = {**(metadata or {}), "owner_pid": os.getpid(), "owner_signature": process_signature(os.getpid())}
        job = self.store.new_job(action, self.descriptor()["boot_id"], owner=owner, metadata=metadata)
        def worker():
            from src.log_redact import redact_obj
            try:
                value = function(job)
                self.store.update_job(job["job_id"], state="success", result=redact_obj(value))
            except Exception as exc:
                from src.control_contract import error_result
                self.store.update_job(job["job_id"], state="error", result=error_result(self.profile["instance_id"], exc))
        # Non-daemon ensures a host disconnect cannot silently kill an ongoing local operation.
        threading.Thread(target=worker, name=f"control-{action}-{job['job_id']}", daemon=False).start()
        return job

    def backend_sync(self, name, arguments):
        return asyncio.run(self.request("POST", "/api/control/v2/tools/binggo_" + name, arguments))

    def base_args(self):
        return {"instance_id": self.profile["instance_id"], "expected_boot_id": self.descriptor()["boot_id"], "idempotency_key": uuid.uuid4().hex}

    def lifecycle(self, action, arguments):
        from src.control_store import ControlError
        if action != "app_start":
            status = self.backend_sync("automation_stop", self.base_args())
            deadline = time.monotonic() + arguments.get("drain_timeout_seconds", 30)
            while True:
                health = self.backend_sync("health_get", {"instance_id": self.profile["instance_id"]})
                if not health["ok"]:
                    raise ControlError("CONTROL_HTTP_ERROR", "停止前无法检查应用状态")
                if not health["data"].get("active_job_id"):
                    break
                if time.monotonic() >= deadline:
                    raise ControlError("JOB_BUSY", "应用仍有运行中的任务，未停止")
                time.sleep(0.5)
            asyncio.run(self.request("POST", "/api/control/v2/shutdown", {}))
            for _ in range(50):
                try:
                    asyncio.run(self.request("GET", "/api/control/v2/identity"))
                except ControlError:
                    break
                time.sleep(0.2)
            else:
                raise ControlError("JOB_BUSY", "应用未退出，未强制结束进程")
        if action != "app_stop":
            try:
                identity = asyncio.run(self.request("GET", "/api/control/v2/identity"))
                return {"already_running": True, "identity": identity}
            except ControlError:
                pass
            command = self.profile["application_command"]
            if not Path(command[0]).is_file():
                raise ControlError("NOT_FOUND", "登记的应用入口不存在")
            env = os.environ.copy()
            # Preserve the application's own network settings, not the MCP host's denied proxy.
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                env.pop(key, None)
            env.update(self.profile.get("application_environment", {}))
            log = self.store.root / "application-start.log"
            with log.open("ab") as output:
                subprocess.Popen(command, cwd=self.profile["source_root"], env=env, stdout=output, stderr=output,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for _ in range(100):
                try:
                    info = asyncio.run(self.request("GET", "/api/control/v2/identity"))
                    return {"started": True, "identity": info}
                except (ControlError, FileNotFoundError):
                    time.sleep(0.2)
            raise ControlError("NETWORK_UNREACHABLE", "启动后控制接口未就绪，查看application-start.log")
        return {"stopped": True}

    @staticmethod
    def suites():
        return {
            "mcp-contract": ["tests/test_mcp_control.py"],
            "api-integration": ["tests/test_control_api.py", "tests/test_api_errors.py", "tests/test_runtime_api.py"],
            "participation-regression": ["tests/test_favorite_optional.py", "tests/test_participation_success.py", "tests/test_participation_validation.py", "tests/test_repost_migration.py", "tests/test_participate_preflight.py", "tests/test_participate_triple.py", "tests/test_job_runner_login.py"],
            "full-regression": ["tests/"],
            "discovery-regression": ["tests/test_lottery_discovery.py"],
            "package-smoke": ["tests/test_frontend_static.py", "tests/test_runtime_api.py"],
            "frontend-unit": ["frontend-unit"], "frontend-e2e": ["frontend-e2e"],
        }

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.profile["source_root"], capture_output=True, text=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()

    def run_test(self, job, identifier, args):
        from src.control_store import ControlError
        from src.control_store import digest
        from src.log_redact import redact_text
        started_at = time.monotonic()
        mode, suite = args["mode"], args["suite_id"]
        report = {"test_run_id": identifier, "job_id": job["job_id"], "suite_id": suite, "mode": mode, "verdict": "running", "passed": 0, "failed": 0,
                  "skipped": 0, "platform_read_requests": 0, "platform_write_requests": 0, "artifacts": [], "commit": self.git("rev-parse", "HEAD"), "failures": []}
        import hashlib
        source_root = Path(self.profile["source_root"])
        files = self.git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
        report.update(dirty_digest=digest({name: hashlib.sha256((source_root/name).read_bytes()).hexdigest()
                                         for name in files if (source_root/name).is_file()}),
                      instance_id=self.profile["instance_id"], boot_id=self.descriptor()["boot_id"],
                      adapter_version="2.0.0", application_version=self.descriptor()["version"], fixture_version="control-v2")
        self.store.put("test", identifier, report)
        try:
            if mode == "offline":
                if suite not in self.suites():
                    raise ControlError("VALIDATION_ERROR", "套件不支持offline")
                sandbox = (self.store.root / "test-runs" / identifier).resolve()
                real_home = Path(self.profile["user_home"]).resolve()
                # A test home cannot be nested under the real home. Use the registered scratch root.
                sandbox = (Path(self.profile["scratch_root"]) / "test-runs" / identifier).resolve()
                if sandbox.is_relative_to(real_home) or real_home.is_relative_to(sandbox):
                    raise ControlError("VALIDATION_ERROR", "测试目录与真实数据根重叠")
                sandbox.mkdir(parents=True)
                env = os.environ.copy()
                env.update(BINGGO_HOME=str(sandbox / "home"), BINGGO_OFFLINE="1", PYTHONPATH=self.profile["source_root"],
                           BINGGO_TEST_REPORT=str(sandbox / "result.json"), PYTHONIOENCODING="utf-8")
                guard = Path(self.profile["source_root"]) / "scripts/offline_guard"
                env["PYTHONPATH"] = str(guard) + os.pathsep + self.profile["source_root"]
                env["PATH"] = str(Path(self.profile["test_python"]).parent) + os.pathsep + env.get("PATH", "")
                env["NODE_OPTIONS"] = '--require="' + str(guard / "node.cjs") + '"'
                env["npm_config_offline"] = "true"
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "BILI_COOKIE", "LLM_API_KEY"):
                    env.pop(key, None)
                command = [self.profile["test_python"], str(Path(self.profile["source_root"]) / "scripts/run_control_tests.py"), suite]
                process = subprocess.run(command, cwd=self.profile["source_root"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                text = redact_text(process.stdout + "\n" + process.stderr)
                report["artifacts"] = [self.store.artifact(f"{suite}.log", text)]
                results = json.loads((sandbox / "result.json").read_text(encoding="utf-8")) if (sandbox / "result.json").exists() else {}
                report.update({key: results.get(key, 0) for key in ("passed", "failed", "skipped")})
                report["verdict"] = "passed" if process.returncode == 0 and report["passed"] > 0 else "failed"
                if process.returncode:
                    report["failures"] = [{"exit_code": process.returncode, "log_artifact_id": report["artifacts"][0]["artifact_id"]}]
            else:
                if mode == "live_read" and suite != "network-read" or mode == "live_write" and suite != "live-participation":
                    raise ControlError("VALIDATION_ERROR", "套件与真实网络模式不匹配")
                target = "network_diagnose" if mode == "live_read" else "participation_execute"
                values = {"targets": ["control", "bilibili"]} if mode == "live_read" else {"plan_id": args["plan_id"]}
                started = self.backend_sync(target, {**self.base_args(), **values})
                if not started["ok"]:
                    raise ControlError(started["error"]["code"], started["error"]["message"])
                original_id = started["data"]["job_id"]
                while True:
                    status = self.backend_sync("job_get", {"instance_id": self.profile["instance_id"], "job_id": original_id})["data"]
                    if status["state"] != "running":
                        break
                    time.sleep(0.5)
                result = status.get("result") or {}
                passed = status["state"] == "success" and (all(v["ok"] for v in result.get("probes", [])) if mode == "live_read" else result.get("participation_outcome") == "joined")
                counts = result.get("network_counts") or {}
                report.update(verdict="passed" if passed else "failed", passed=int(passed), failed=int(not passed), backend_job_id=original_id,
                              platform_read_requests=counts.get("platform_read_requests", 0), platform_write_requests=counts.get("platform_write_requests", 0))
                report["artifacts"] = [self.store.artifact(f"{suite}.json", json.dumps(result, ensure_ascii=False), "application/json")]
            report["elapsed_ms"] = int((time.monotonic() - started_at) * 1000)
            self.store.put("test", identifier, report)
            return {"test_run_id": identifier, "verdict": report["verdict"]}
        except Exception as exc:
            report.update(verdict="blocked", failures=[{"message": redact_text(str(exc))}])
            report["elapsed_ms"] = int((time.monotonic() - started_at) * 1000)
            self.store.put("test", identifier, report)
            raise

    def build(self, job, args):
        from src.control_store import ControlError
        if args["workspace_id"] != self.profile["workspace_id"] or args["commit"] != self.git("rev-parse", "HEAD"):
            raise ControlError("REVISION_CONFLICT", "构建工作区或提交与登记状态不符")
        if self.git("status", "--porcelain") and not args.get("allow_dirty"):
            raise ControlError("REVISION_CONFLICT", "工作区有未提交修改")
        identifier = uuid.uuid4().hex
        output = Path(self.profile["scratch_root"]) / "builds" / identifier
        output.mkdir(parents=True)
        command = [self.profile["test_python"], "-m", "PyInstaller", "--noconfirm", "--distpath", str(output / "dist"), "--workpath", str(output / "work"), "packaging/windows/binggo.spec"]
        result = subprocess.run(command, cwd=self.profile["source_root"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        from src.log_redact import redact_text
        log = self.store.artifact("build.log", redact_text(result.stdout + result.stderr))
        value = {"build_id": identifier, "job_id": job["job_id"], "state": "success" if result.returncode == 0 else "error", "artifacts": [log], "smoke_test_run_id": None, "output_directory": str(output / "dist"), "commit": args["commit"], "business_schema_version": 2}
        self.store.put("build", identifier, value)
        if result.returncode:
            raise ControlError("BUILD_FAILED", "构建失败，查看登记日志")
        smoke = self.package_smoke(output / "dist/Binggo-RepostFix/Binggo-RepostFix.exe", job["job_id"])
        value["smoke_test_run_id"] = smoke["test_run_id"]
        if smoke["verdict"] != "passed":
            value["state"] = "error"
        self.store.put("build", identifier, value)
        if value["state"] != "success":
            raise ControlError("BUILD_FAILED", "包级验收失败，未允许发布")
        return value

    def package_smoke(self, executable, job_id):
        import socket
        identifier = uuid.uuid4().hex
        report = {"test_run_id": identifier, "job_id": job_id, "suite_id": "package-smoke", "mode": "offline", "verdict": "failed",
                  "passed": 0, "failed": 1, "skipped": 0, "platform_read_requests": 0, "platform_write_requests": 0, "artifacts": []}
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        env = os.environ.copy()
        for key in ("BILI_COOKIE", "LLM_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env.pop(key, None)
        env["BINGGO_CONTROL_PORT"] = str(port)
        log_path = executable.parent / "package-smoke.log"
        process = None
        try:
            with log_path.open("wb") as output:
                process = subprocess.Popen([str(executable), "--serve"], cwd=executable.parent, env=env, stdout=output, stderr=output,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            home = executable.parent / "user-data"
            endpoint = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=1) as client:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    try:
                        credential = json.loads((home / "config/control-access.json").read_text(encoding="utf-8"))
                        headers = {"Authorization": "Bearer "+credential["token"]}
                        response = client.get(endpoint+"/api/control/v2/identity", headers=headers)
                        response.raise_for_status()
                        info = response.json()
                        assert info["control_contract_version"] == 2
                        assert client.get(endpoint+"/").status_code == 200
                        report.update(verdict="passed", passed=1, failed=0, application_version=info["version"])
                        client.post(endpoint+"/api/control/v2/shutdown", headers=headers, json={}).raise_for_status()
                        process.wait(timeout=15)
                        break
                    except (OSError, ValueError, httpx.HTTPError):
                        if process.poll() is not None:
                            break
                        time.sleep(0.2)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            from src.log_redact import redact_text
            report["artifacts"] = [self.store.artifact("package-smoke.log", redact_text(log_path.read_text(encoding="utf-8", errors="replace")) if log_path.exists() else "No package log")]
            self.store.put("test", identifier, report)
        return report

    def release(self, name, args):
        import shutil
        from src.control_store import ControlError
        build = self.store.get("build", args.get("build_id", "")) if name == "release_activate" else self.store.get("release", args.get("release_id", ""))
        if not build or build.get("state") != "success" or name == "release_activate" and not build.get("smoke_test_run_id"):
            raise ControlError("RELEASE_NOT_VERIFIED", "构建尚未附加通过的包级验收，未替换程序")
        info = self.descriptor()
        if info.get("business_schema_version") != build.get("business_schema_version"):
            raise ControlError("API_VERSION_UNSUPPORTED", "数据库schema不兼容，未切换版本")
        command = self.profile["application_command"]
        executable = Path(command[0]).resolve()
        if executable.name != "Binggo-RepostFix.exe":
            raise ControlError("VALIDATION_ERROR", "当前登记实例不是可切换的安装包")
        install = executable.parent
        source = Path(build["output_directory"]) / "Binggo-RepostFix" if name == "release_activate" else Path(build["backup_directory"])
        if not (source / executable.name).is_file() or not (source / "_internal").is_dir():
            raise ControlError("NOT_FOUND", "登记版本的程序资源不完整")
        identifier = uuid.uuid4().hex
        backup = (Path(self.profile["scratch_root"]) / "releases" / identifier).resolve()
        backup.mkdir(parents=True)
        # Both operations use exact registered program paths; user-data is never traversed or moved.
        self.lifecycle("app_stop", {"drain_timeout_seconds": 30})
        moved = []
        try:
            for basename in (executable.name, "_internal"):
                target = (install / basename).resolve()
                if target.parent != install.resolve() or basename not in {"Binggo-RepostFix.exe", "_internal"}:
                    raise ControlError("VALIDATION_ERROR", "程序目标越界")
                shutil.move(str(target), str(backup / basename))
                moved.append(basename)
                if (source / basename).is_dir():
                    shutil.copytree(source / basename, target)
                else:
                    shutil.copy2(source / basename, target)
            started = self.lifecycle("app_start", {})
        except Exception:
            for basename in reversed(moved):
                target = install / basename
                if target.exists():
                    failed = backup / (basename+".failed")
                    shutil.move(str(target), str(failed))
                shutil.move(str(backup / basename), str(target))
            self.lifecycle("app_start", {})
            raise
        record = {"release_id": identifier, "state": "success", "backup_directory": str(backup), "business_schema_version": build["business_schema_version"], "build_id": args.get("build_id"), "started": started}
        self.store.put("release", identifier, record)
        return record


def process_signature(pid):
    if not pid:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)]*4)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            return f"{pid}:{(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}"
        finally:
            kernel.CloseHandle(handle)
    try:
        return f"{pid}:{Path('/proc', str(pid), 'stat').read_text().split()[21]}"
    except OSError:
        return None
