"""Disposable Docker execution with a bounded, credential-free tool bridge.

No generated code ever executes in the web/worker Python interpreter.
"""
import json
import os
import re
import selectors
import select
import shutil
import subprocess
import time
import threading
import uuid

from ..models import Rejected, canonical


PREFIX = "\x1eISC_RPC:"
BRIDGE_TOOLS = {"catalog", "research", "read_api", "invoke_tool", "validate_data"}
RUNNER = r'''
import json, sys, traceback, types
prefix = "\x1eISC_RPC:"
payload = json.loads(sys.stdin.readline())
counter = 0
def call(name, arguments):
    global counter
    counter += 1
    print(prefix + json.dumps({"type":"tool", "id":counter, "name":name, "arguments":arguments}), flush=True)
    reply = json.loads(sys.stdin.readline())
    if reply.get("id") != counter:
        raise RuntimeError("Invalid bridge response")
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply["result"]
tools = types.ModuleType("tools")
tools.call = call
sys.modules["tools"] = tools
scope = {"__name__":"__main__", "tools":tools, "inputs":payload["inputs"]}
try:
    exec(compile(payload["code"], "agent.py", "exec"), scope)
    print(prefix + json.dumps({"type":"done", "result":scope.get("result")}), flush=True)
except BaseException:
    traceback.print_exc(limit=5)
    sys.exit(1)
'''


class PythonSandbox:
    def __init__(self, *, timeout=30, max_tools=8):
        self.timeout, self.max_tools = timeout, max_tools
        self.image = os.environ.get("ISC_PYTHON_IMAGE", "isc-agent-python:local")
        self.host = os.environ.get("ISC_PYTHON_DOCKER_HOST", "unix:///var/run/docker.sock")

    def capabilities(self):
        return {"enabled": os.environ.get("ISC_ENABLE_PYTHON", "1") == "1", "runtime": "docker",
                "image": self.image, "network": "none", "timeout_seconds": self.timeout,
                "max_tool_calls": self.max_tools, "requires": "Docker and a locally built runtime image; never falls back to host execution"}

    def command(self, name):
        if not self.capabilities()["enabled"]:
            raise Rejected("Python execution is disabled by the server operator")
        if not self.host.startswith("unix://"):
            raise Rejected("Python runtime requires a local Docker Unix socket")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", self.image):
            raise Rejected("Configure a valid local Python runtime image reference")
        binary = shutil.which("docker")
        if not binary:
            raise Rejected("Python execution needs Docker Desktop/Engine and the local isc-agent-python:local image; see docs/agent-runtime.md")
        command = [binary, "--host=" + self.host, "run", "--rm", "--pull=never", "-i", "--name", name,
            "--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=32", "--memory=128m", "--memory-swap=128m",
            "--cpus=1", "--ulimit=nofile=64:64", "--ulimit=core=0", "--log-driver=none",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=8m", "--workdir=/tmp"]
        # Docker client proxy defaults must not expose host proxy credentials to code.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            command += ["--env", key + "="]
        return command + ["--entrypoint=python", self.image, "-I", "-u", "-c", RUNNER]

    @staticmethod
    def docker_environment():
        env = os.environ.copy()
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            env.pop(key, None)
        return env

    def run(self, code, inputs, bridge):
        if not isinstance(code, str) or not 1 <= len(code) <= 50000:
            raise Rejected("Python source must contain 1–50,000 characters")
        if not isinstance(inputs, dict) or len(canonical(inputs)) > 100000:
            raise Rejected("Python inputs must be a JSON object of at most 100,000 characters")
        name = "isc-python-" + uuid.uuid4().hex
        command = self.command(name)
        started, calls, total = time.monotonic(), 0, 0
        stdout, stderr, pending, result = [], [], b"", None
        # No shell, host mounts, daemon socket mount, privileged mode or inherited env.
        try:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.docker_environment())
        except OSError:
            raise Rejected("Cannot start Docker Python runtime; check Docker installation and daemon access") from None
        timed_out = threading.Event()
        def stop_container():
            try:
                subprocess.run([command[0], "--host=" + self.host, "rm", "-f", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, env=self.docker_environment())
            except (OSError, subprocess.TimeoutExpired):
                pass
        def deadline():
            timed_out.set()
            if proc.poll() is None:
                proc.kill()
            stop_container()
        watchdog = threading.Timer(self.timeout, deadline)
        watchdog.daemon = True
        watchdog.start()
        selector = selectors.DefaultSelector()
        try:
            os.set_blocking(proc.stdin.fileno(), False)
            def send(data):
                view = memoryview(data)
                while view:
                    if time.monotonic() - started > self.timeout:
                        raise Rejected("Python execution exceeded its wall-time limit")
                    if select.select([], [proc.stdin], [], .1)[1]:
                        try:
                            view = view[os.write(proc.stdin.fileno(), view[:8192]):]
                        except BlockingIOError:
                            continue
            send((canonical({"code": code, "inputs": inputs}) + "\n").encode())
            selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
            selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                if time.monotonic() - started > self.timeout:
                    raise Rejected("Python execution exceeded its wall-time limit")
                for key, _ in selector.select(timeout=.1):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > 200000:
                        raise Rejected("Python execution exceeded its output limit")
                    if key.data == "stderr":
                        stderr.append(chunk.decode(errors="replace"))
                        continue
                    pending += chunk
                    if len(pending) > 100000:
                        raise Rejected("Python output line exceeded its size limit")
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        text = line.decode(errors="replace")
                        marker = text.find(PREFIX)
                        if marker < 0:
                            stdout.append(text)
                            continue
                        if marker:
                            stdout.append(text[:marker])
                            text = text[marker:]
                        try:
                            message = json.loads(text[len(PREFIX):])
                        except ValueError:
                            raise Rejected("Invalid Python bridge message") from None
                        if not isinstance(message, dict):
                            raise Rejected("Invalid Python bridge message")
                        if message.get("type") == "done":
                            result = message.get("result")
                        elif message.get("type") == "tool":
                            calls += 1
                            if calls > self.max_tools:
                                raise Rejected("Python tool-call budget exhausted")
                            name_, args = message.get("name"), message.get("arguments")
                            reply = {"id": message.get("id")}
                            if not isinstance(name_, str) or name_ not in BRIDGE_TOOLS or not isinstance(args, dict):
                                reply["error"] = "Python can call only documented read capabilities with JSON object arguments"
                            else:
                                try:
                                    reply["result"] = bridge(name_, args)
                                except Rejected as exc:
                                    reply["error"] = str(exc)
                            encoded = (canonical(reply) + "\n").encode()
                            if len(encoded) > 200000:
                                encoded = (canonical({"id": message.get("id"), "error": "Tool result too large; request narrower data"}) + "\n").encode()
                            # A live interpreter is waiting for exactly this reply.
                            send(encoded)
                        else:
                            raise Rejected("Unknown Python bridge message")
            if timed_out.is_set() or time.monotonic() - started > self.timeout:
                raise Rejected("Python execution exceeded its wall-time limit")
            code_ = proc.wait(timeout=2)
            if code_ == 125:
                raise Rejected("Docker runtime unavailable: start Docker and build the image with docker build -t isc-agent-python:local -f sandbox/Dockerfile .")
            if pending:
                stdout.append(pending.decode(errors="replace"))
            return {"status": "completed" if code_ == 0 else "failed", "result": result,
                    "stdout": "\n".join(stdout), "stderr": "".join(stderr), "exit_code": code_, "tool_calls": calls,
                    "duration_seconds": round(time.monotonic() - started, 3)}
        except (BrokenPipeError, subprocess.TimeoutExpired):
            if timed_out.is_set():
                raise Rejected("Python execution exceeded its wall-time limit") from None
            raise Rejected("Python runtime terminated unexpectedly; no host execution fallback") from None
        finally:
            watchdog.cancel()
            selector.close()
            if proc.poll() is None:
                proc.kill()
            stop_container()
            proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()
