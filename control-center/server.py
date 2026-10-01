from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.yaml"
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
MODEL_DIR = ROOT / "models" / "production"
MLFLOW_URL = os.getenv("MLFLOW_URL", "http://127.0.0.1:5000")
HOST = "127.0.0.1"
PORT = int(os.getenv("CONTROL_CENTER_PORT", "8765"))
PROFILES = ["--profile", "training", "--profile", "airflow", "--profile", "monitoring"]
TASKS: dict[str, dict[str, object]] = {}
TASKS_LOCK = threading.Lock()

START_COMMANDS = {
    "core": ([], ["up", "--build", "-d", "api"]),
    "training": (["training"], ["run", "--build", "--rm", "trainer"]),
    "airflow": (["airflow"], ["up", "--build", "-d", "airflow-webserver", "airflow-scheduler"]),
    "monitoring": (["monitoring"], ["up", "-d", "prometheus", "grafana"]),
    "all": (
        ["airflow", "monitoring"],
        ["up", "--build", "-d", "api", "airflow-webserver", "airflow-scheduler", "prometheus", "grafana"],
    ),
}


def compose_command(args: list[str], profiles: list[str] | None = None) -> list[str]:
    command = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    for profile in profiles or []:
        command.extend(["--profile", profile])
    return command + args


def postgres_sql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        compose_command([
            "exec", "-T", "postgres", "psql", "-U", "admin", "-d", "endpointsecurity",
            "-v", "ON_ERROR_STOP=1", "-tAc", sql,
        ]),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def database_summary() -> dict[str, int] | None:
    result = postgres_sql(
        "SELECT json_build_object(" 
        "'activity_rows', (SELECT count(*) FROM endpoint_activities), "
        "'prediction_rows', (SELECT count(*) FROM prediction_events))::text"
    )
    if result.returncode:
        return None
    return json.loads(result.stdout.strip())


def seed_database(task_id: str) -> None:
    create_table = (
        "CREATE TABLE IF NOT EXISTS endpoint_activities ("
        "activity_id BIGSERIAL PRIMARY KEY, process_name TEXT NOT NULL, "
        "network_connections INTEGER NOT NULL CHECK (network_connections >= 0), "
        "is_attack SMALLINT NOT NULL CHECK (is_attack IN (0, 1)), "
        "imported_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    result = postgres_sql(create_table)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Kunde inte skapa endpoint_activities.")
    count = postgres_sql("SELECT count(*) FROM endpoint_activities")
    if count.returncode:
        raise RuntimeError(count.stderr.strip() or "Kunde inte läsa endpoint_activities.")
    if int(count.stdout.strip() or "0") == 0:
        result = postgres_sql(
            "COPY endpoint_activities (process_name, network_connections, is_attack) "
            "FROM '/seed/endpoint_activities.csv' WITH (FORMAT csv, HEADER true)"
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Kunde inte importera exempel-CSV:n.")
        append_task_output(task_id, "Exempel-CSV importerad till PostgreSQL.")
    else:
        append_task_output(task_id, "PostgreSQL har redan aktivitetsrader; inga dubbletter lades till.")
    summary = database_summary()
    append_task_output(task_id, f"Aktivitetsrader: {summary['activity_rows'] if summary else 'okänt'}")


def run_dvc(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        compose_command(["run", "--rm", "--no-deps", "dvc", *args], ["dvc"]),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def initialize_dvc() -> str:
    config = ROOT / ".dvc" / "config"
    output = []
    if not config.exists():
        result = run_dvc(["init", "--no-scm"])
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or "DVC kunde inte initieras.")
        output.append(result.stdout.strip())
    result = run_dvc(["config", "cache.type", "copy"])
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "DVC cache kunde inte konfigureras.")
    result = run_dvc(["config", "cache.protected", "false"])
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "DVC cache-skydd kunde inte stängas av.")
    return "\n".join(line for line in output if line)


def add_dvc_file(relative_path: str) -> str:
    target = ROOT / relative_path
    if not target.is_file():
        return f"Saknas, hoppar över: {relative_path}"
    pointer = target.with_name(target.name + ".dvc")
    if pointer.exists():
        return f"Redan versionshanterad: {relative_path}"
    result = run_dvc(["add", relative_path.replace("\\", "/")])
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"DVC kunde inte lägga till {relative_path}.")
    return f"DVC-version skapad: {relative_path}"


def append_task_output(task_id: str, line: str) -> None:
    with TASKS_LOCK:
        output = TASKS[task_id]["output"]
        output.append(line)
        del output[:-60]


def version_local_artifacts(task_id: str) -> None:
    append_task_output(task_id, initialize_dvc() or "DVC är initierat.")
    paths = ["data/raw/endpoint_activities.csv"]
    paths.extend(
        path.relative_to(ROOT).as_posix()
        for path in sorted(SNAPSHOT_DIR.glob("*.dump"))
        if path.is_file()
    )
    paths.extend(
        path.relative_to(ROOT).as_posix()
        for path in sorted(MODEL_DIR.glob("*.pkl"))
        if path.is_file()
    )
    for relative_path in paths:
        append_task_output(task_id, add_dvc_file(relative_path))


def export_database_snapshot(task_id: str) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot = SNAPSHOT_DIR / f"endpointsecurity_{stamp}.dump"
    command = compose_command(
        ["exec", "-T", "postgres", "pg_dump", "-U", "admin", "-d", "endpointsecurity", "--format=custom"]
    )
    with snapshot.open("wb") as output:
        result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.PIPE, timeout=180, check=False)
    if result.returncode:
        snapshot.unlink(missing_ok=True)
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "PostgreSQL kunde inte exportera databasen.")
    append_task_output(task_id, f"Databassnapshot sparad: {snapshot.relative_to(ROOT).as_posix()}")
    append_task_output(task_id, initialize_dvc() or "DVC är initierat.")
    append_task_output(task_id, add_dvc_file(snapshot.relative_to(ROOT).as_posix()))


def mlflow_runs() -> list[dict[str, object]]:
    query = urlencode({"experiment_name": "EndpointSecurityML_Docker"})
    try:
        with urlopen(f"{MLFLOW_URL}/api/2.0/mlflow/experiments/get-by-name?{query}", timeout=5) as response:
            experiment = json.load(response)["experiment"]
        request = Request(
            f"{MLFLOW_URL}/api/2.0/mlflow/runs/search",
            data=json.dumps({"experiment_ids": [experiment["experiment_id"]], "max_results": 30}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            runs = json.load(response).get("runs", [])
    except HTTPError as error:
        if error.code == 404:
            return []
        raise RuntimeError(f"MLflow svarade med HTTP {error.code}.") from error
    except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as error:
        raise RuntimeError("MLflow går inte att läsa just nu.") from error

    results = []
    for run in runs:
        data = run.get("data", {})
        metrics = {item["key"]: item["value"] for item in data.get("metrics", [])}
        tags = {item["key"]: item["value"] for item in data.get("tags", [])}
        results.append({
            "run_id": run.get("info", {}).get("run_id"),
            "status": run.get("info", {}).get("status"),
            "start_time": run.get("info", {}).get("start_time"),
            "model_version": tags.get("model_version", "okänd"),
            "metrics": metrics,
        })
    return results


def workshop_artifacts() -> dict[str, object]:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    def describe(path: Path) -> dict[str, object]:
        return {
            "name": path.name,
            "bytes": path.stat().st_size,
            "modified": path.stat().st_mtime,
            "versioned": path.with_name(path.name + ".dvc").exists(),
        }

    snapshots = sorted(SNAPSHOT_DIR.glob("*.dump"), key=lambda path: path.stat().st_mtime, reverse=True)
    models = sorted(MODEL_DIR.glob("*.pkl"), key=lambda path: path.stat().st_mtime, reverse=True)
    pointers = list((ROOT / "data").rglob("*.dvc")) + list((ROOT / "models").rglob("*.dvc"))
    return {
        "dvc_initialized": (ROOT / ".dvc" / "config").exists(),
        "dvc_pointer_count": len(pointers),
        "database": database_summary(),
        "snapshots": [describe(path) for path in snapshots],
        "models": [describe(path) for path in models],
    }


def run_task(task_id: str, command: list[str]) -> None:
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with TASKS_LOCK:
            TASKS[task_id]["state"] = "running"
        assert process.stdout is not None
        for line in process.stdout:
            with TASKS_LOCK:
                output = TASKS[task_id]["output"]
                output.append(line.rstrip())
                del output[:-60]
        return_code = process.wait()
        if return_code == 0 and command[-1:] == ["trainer"]:
            try:
                version_local_artifacts(task_id)
            except Exception as error:
                append_task_output(task_id, f"Träningen lyckades, men DVC-versionering misslyckades: {error}")
        with TASKS_LOCK:
            TASKS[task_id]["state"] = "success" if return_code == 0 else "failed"
            TASKS[task_id]["return_code"] = return_code
    except Exception as error:
        with TASKS_LOCK:
            TASKS[task_id]["state"] = "failed"
            TASKS[task_id]["output"].append(f"Kunde inte köra Docker Compose: {error}")


def create_task(label: str) -> str:
    task_id = uuid.uuid4().hex[:12]
    with TASKS_LOCK:
        TASKS[task_id] = {"id": task_id, "label": label, "state": "starting", "output": [], "return_code": None}
    return task_id


def launch_task(label: str, command: list[str]) -> str:
    task_id = create_task(label)
    threading.Thread(target=run_task, args=(task_id, command), daemon=True).start()
    return task_id


def docker_status() -> list[dict[str, object]]:
    result = subprocess.run(
        compose_command(["ps", "--all", "--format", "json"], PROFILES),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Docker svarar inte. Starta Docker Desktop/Engine och försök igen.")
    containers = []
    for line in result.stdout.splitlines():
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return containers


class ControlCenterHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, status: int, body: dict[str, object] | list[object]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 16_384:
            raise ValueError("Ogiltig storlek på förfrågan.")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("Förfrågan måste vara ett JSON-objekt.")
        return value

    def local_request_allowed(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]")
        if host not in {"localhost", "127.0.0.1"}:
            return False
        if self.client_address[0] not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            page = (ROOT / "control-center" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        if path == "/README.md":
            document = (ROOT / "README.md").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(document)
            return
        if path == "/api/status":
            try:
                self.send_json(200, {"containers": docker_status()})
            except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                self.send_json(503, {"error": str(error)})
            return
        if path == "/api/runs":
            try:
                self.send_json(200, {"runs": mlflow_runs()})
            except RuntimeError as error:
                self.send_json(503, {"error": str(error), "runs": []})
            return
        if path == "/api/workshop":
            try:
                self.send_json(200, workshop_artifacts())
            except OSError as error:
                self.send_json(500, {"error": str(error)})
            return
        if path.startswith("/api/artifacts/"):
            self.send_artifact(path)
            return
        if path.startswith("/api/tasks/"):
            task_id = path.removeprefix("/api/tasks/")
            with TASKS_LOCK:
                task = TASKS.get(task_id)
                if task:
                    self.send_json(200, task)
                else:
                    self.send_json(404, {"error": "Uppgiften hittades inte."})
            return
        self.send_json(404, {"error": "Sidan hittades inte."})

    def do_POST(self) -> None:
        if not self.local_request_allowed():
            self.send_json(403, {"error": "Kontrollpanelen tar bara emot lokala anrop från samma sida."})
            return
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/start":
                self.start_service(body)
            elif path == "/api/stop":
                self.stop_services()
            elif path == "/api/snapshot":
                self.start_snapshot()
            elif path == "/api/seed-data":
                self.start_seed()
            elif path == "/api/version-artifacts":
                self.start_versioning()
            elif path == "/api/github-secrets":
                self.set_github_secrets(body)
            else:
                self.send_json(404, {"error": "Åtgärden hittades inte."})
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})

    def start_service(self, body: dict[str, object]) -> None:
        action = body.get("action")
        if not isinstance(action, str) or action not in START_COMMANDS:
            self.send_json(400, {"error": "Välj en giltig tjänst att starta."})
            return
        profiles, args = START_COMMANDS[action]
        task_id = launch_task(action, compose_command(args, profiles))
        self.send_json(202, {"task_id": task_id})

    def stop_services(self) -> None:
        task_id = launch_task("Stoppa tjänster", compose_command(["down"], PROFILES))
        self.send_json(202, {"task_id": task_id})

    def start_snapshot(self) -> None:
        task_id = create_task("Exportera och DVC-versionera PostgreSQL")

        def worker() -> None:
            try:
                with TASKS_LOCK:
                    TASKS[task_id]["state"] = "running"
                export_database_snapshot(task_id)
                with TASKS_LOCK:
                    TASKS[task_id]["state"] = "success"
                    TASKS[task_id]["return_code"] = 0
            except Exception as error:
                append_task_output(task_id, str(error))
                with TASKS_LOCK:
                    TASKS[task_id]["state"] = "failed"
                    TASKS[task_id]["return_code"] = 1

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"task_id": task_id})

    def start_seed(self) -> None:
        task_id = create_task("Ladda exempeldata till PostgreSQL")

        def worker() -> None:
            try:
                with TASKS_LOCK:
                    TASKS[task_id]["state"] = "running"
                seed_database(task_id)
                with TASKS_LOCK:
                    TASKS[task_id]["state"] = "success"
                    TASKS[task_id]["return_code"] = 0
            except Exception as error:
                append_task_output(task_id, str(error))
                with TASKS_LOCK:
                    TASKS[task_id]["state"] = "failed"
                    TASKS[task_id]["return_code"] = 1

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"task_id": task_id})

    def start_versioning(self) -> None:
        task_id = create_task("DVC-versionera data och modeller")

        def worker() -> None:
            try:
                with TASKS_LOCK:
                    TASKS[task_id]["state"] = "running"
                version_local_artifacts(task_id)
                with TASKS_LOCK:
                    TASKS[task_id]["state"] = "success"
                    TASKS[task_id]["return_code"] = 0
            except Exception as error:
                append_task_output(task_id, str(error))
                with TASKS_LOCK:
                    TASKS[task_id]["state"] = "failed"
                    TASKS[task_id]["return_code"] = 1

        threading.Thread(target=worker, daemon=True).start()
        self.send_json(202, {"task_id": task_id})

    def send_artifact(self, request_path: str) -> None:
        parts = [unquote(part) for part in request_path.split("/")]
        if len(parts) != 5 or parts[2] != "artifacts" or parts[3] not in {"snapshots", "models"} or Path(parts[4]).name != parts[4]:
            self.send_json(404, {"error": "Filen hittades inte."})
            return
        base = SNAPSHOT_DIR if parts[3] == "snapshots" else MODEL_DIR
        target = (base / parts[4]).resolve()
        if target.parent != base.resolve() or not target.is_file():
            self.send_json(404, {"error": "Filen hittades inte."})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with target.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def set_github_secrets(self, body: dict[str, object]) -> None:
        repository = body.get("repository")
        username = body.get("docker_username")
        token = body.get("docker_token")
        if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            self.send_json(400, {"error": "Ange repository som ägare/repo."})
            return
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", username):
            self.send_json(400, {"error": "Ange ett giltigt Docker Hub-användarnamn."})
            return
        if not isinstance(token, str) or not token.strip() or len(token) > 5000:
            self.send_json(400, {"error": "Klistra in en giltig Docker Hub access token."})
            return
        if not shutil.which("gh"):
            self.send_json(503, {"error": "GitHub CLI (gh) saknas. Installera den och kör gh auth login först."})
            return
        auth = subprocess.run(["gh", "auth", "status", "--hostname", "github.com"], capture_output=True, timeout=15, check=False)
        if auth.returncode:
            self.send_json(401, {"error": "GitHub CLI är inte inloggad. Kör gh auth login i terminalen."})
            return

        for name, value in (("DOCKER_USERNAME", username), ("DOCKER_PASSWORD", token)):
            result = subprocess.run(
                ["gh", "secret", "set", name, "--repo", repository],
                input=value,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            if result.returncode:
                self.send_json(502, {"error": f"Kunde inte spara {name}. Kontrollera repo och behörigheten för GitHub CLI."})
                return
        self.send_json(200, {"ok": True, "secrets": ["DOCKER_USERNAME", "DOCKER_PASSWORD"]})


if __name__ == "__main__":
    print(f"EndpointSecurityML kontrollpanel: http://{HOST}:{PORT}")
    print("Kontrollpanelen är endast tillgänglig från den här datorn. Avsluta med Ctrl+C.")
    ThreadingHTTPServer((HOST, PORT), ControlCenterHandler).serve_forever()