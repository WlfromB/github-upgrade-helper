#!/usr/bin/env python3
"""
github-upgrade-helper — мини-приложение для деплоя репозиториев на свой сервер.

Зачем: GitHub Actions для приватных репо лимитирован бюджетом; этот хелпер
запускает тот же деплой (fetch -> checkout -> docker compose build/up ->
health-check) напрямую с сервера, из браузера.

Только стандартная библиотека Python 3.9+ (никаких pip-зависимостей).

Запуск:  python3 server.py            (конфиг — config.json рядом)
API и логи запусков — см. README.md; инструкция продублирована в UI ( / ).
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
LIB_DEPLOY = BASE / "lib" / "deploy.sh"
CONFIG_PATH = BASE / "config.json"
RUNS_DIR = BASE / "runs"
INDEX = STATIC / "index.html"

MAX_LOG_LINES = 4000          # в памяти на запуск
MAX_RUNS_HISTORY = 50
REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# Известные проекты WlfromB: авто-подсказки compose-сервиса и health-проверки
# при добавлении репозитория (можно поменять в UI после добавления).
WELL_KNOWN = {
    "auth-fitness-app": ("auth-app", "http://127.0.0.1:8000/actuator/health", '"status":"UP"', "service"),
    "notification-fitness": ("notification-app", "http://127.0.0.1:8082/actuator/health", '"status":"UP"', "service"),
    "manage-fitness-service": ("manage-app", "http://127.0.0.1:8083/actuator/health", '"status":"UP"', "service"),
    "fitness-frontend": ("frontend", "http://127.0.0.1:3000/health", "healthy", "service"),
    "decision-service": ("decision-app", "http://127.0.0.1:8084/actuator/health", '"status":"UP"', "service"),
    "telegram-queue-gateway": ("telegram-gateway", "http://127.0.0.1:8086/health", '"status":"UP"', "service"),
    "webpush-python-service": ("push-gateway", "http://127.0.0.1:8087/health", '"status":"UP"', "service"),
    "accumulate-queue": ("liquibase-authdb liquibase-notifdb", "", "", "infra"),
}

DEFAULT_CONFIG = {
    "settings": {
        "bind": "127.0.0.1",
        "port": 8790,
        "password": "",                  # если задан — HTTP Basic auth
        "workspaceRoot": "/opt",         # куда клонировать новые репо
        "composeDir": "/opt/accumulate-queue",
        "githubToken": "",               # PAT по умолчанию (scope: repo)
    },
    "repos": [],
}

# ───────────────────────────── config ─────────────────────────────

_lock = threading.RLock()
_runs = {}          # runId -> run dict
_deploy_lock = threading.Lock()   # один деплой одновременно


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.setdefault("settings", {})
    for key, val in DEFAULT_CONFIG["settings"].items():
        cfg["settings"].setdefault(key, val)
    cfg.setdefault("repos", [])
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


# ───────────────────────────── git helpers ─────────────────────────────

def normalize_repo_url(raw: str):
    """Принимает git/https/page URL, возвращает (slug 'owner/name', https_url) или бросает ValueError."""
    s = raw.strip()
    m = re.match(r"^([\w.-]+)/([\w.-]+)$", s)                     # owner/name
    if m:
        slug = f"{m.group(1)}/{m.group(2)}"
    else:
        s2 = re.sub(r"\.git$", "", s)
        m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", s2)
        if not m:
            raise ValueError("Не похоже на GitHub-репозиторий. Пример: https://github.com/WlfromB/fitness-frontend")
        slug = f"{m.group(1)}/{m.group(2)}"
    return slug, f"https://github.com/{slug}.git"


def url_with_token(url: str, token: str) -> str:
    return url.replace("https://", f"https://x-access-token:{token}@")


def git_env(token: str) -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        env["GITHUB_TOKEN"] = token          # для lib/deploy.sh
    return env


def run_cmd(cmd, cwd=None, env=None, timeout=120):
    p = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, (p.stdout or "").strip()


def repo_token(cfg: dict, repo: dict) -> str:
    return repo.get("token") or cfg["settings"].get("githubToken") or ""


def ls_remote_heads(url: str, token: str):
    u = url_with_token(url, token) if token else url
    rc, out = run_cmd(["git", "ls-remote", "--heads", "--tags", u], timeout=60)
    if rc != 0:
        return None, out or "git ls-remote не удался"
    refs = []
    for line in out.splitlines():
        sha, ref = (line.split("\t") + [""])[:2]
        refs.append({"ref": ref.replace("refs/heads/", "").replace("refs/tags/", ""), "sha": sha[:10],
                     "type": "tag" if ref.startswith("refs/tags/") else "branch"})
    refs.sort(key=lambda r: (r["type"] != "branch", r["ref"].lower()))
    return refs, None


# ───────────────────────────── deploy runs ─────────────────────────────

def new_run(repo: dict, ref: str, dry_run: bool) -> dict:
    run = {
        "id": uuid.uuid4().hex[:12],
        "repoId": repo["id"], "repoName": repo["name"], "ref": ref,
        "dryRun": dry_run,
        "status": "queued", "startedAt": None, "finishedAt": None,
        "log": [f"[queued] деплой {repo['slug']} @ {ref}" + (" (dry-run)" if dry_run else "")],
    }
    with _lock:
        _runs[run["id"]] = run
    RUNS_DIR.mkdir(exist_ok=True)
    (RUNS_DIR / f"{run['id']}.log").write_text("")
    return run


def append_log(run: dict, line: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    with _lock:
        run["log"].append(f"[{stamp}] {line}")
        if len(run["log"]) > MAX_LOG_LINES:
            del run["log"][: len(run["log"]) - MAX_LOG_LINES]
    with open(RUNS_DIR / f"{run['id']}.log", "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")


def deploy_worker(cfg_snapshot: dict, repo: dict, run: dict) -> None:
    lib = str(LIB_DEPLOY)
    if not LIB_DEPLOY.exists():
        append_log(run, f"ОШИБКА: не найден {lib}")
        run["status"] = "failed"
        run["finishedAt"] = time.time()
        return

    token = repo_token(cfg_snapshot, repo)
    env = dict(os.environ)
    env.update({
        "REF": run["ref"],
        "REPO_SLUG": repo["slug"],
        "CLONE_DIR": repo["cloneDir"],
        "COMPOSE_DIR": repo.get("composeDir") or cfg_snapshot["settings"]["composeDir"],
        "SERVICE": repo.get("service") or "",
        "MODE": repo.get("mode") or "service",
        "HEALTH_URL": repo.get("healthUrl") or "",
        "HEALTH_GREP": repo.get("healthGrep") or '"status":"UP"',
        "GITHUB_TOKEN": token,
        "GIT_TERMINAL_PROMPT": "0",
    })
    if run["dryRun"]:
        env["DRY_RUN"] = "1"

    with _deploy_lock:                       # один деплой за раз
        run["status"] = "running"
        run["startedAt"] = time.time()
        append_log(run, f"запуск: bash lib/deploy.sh (REF={run['ref']}, SERVICE={env['SERVICE'] or '-'})")
        try:
            proc = subprocess.Popen(
                ["/bin/bash", lib], cwd=str(BASE), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            for line in proc.stdout:
                append_log(run, line.rstrip("\n"))
            rc = proc.wait()
        except Exception as exc:  # noqa: BLE001 — в лог, не в crash
            append_log(run, f"ОШИБКА запуска: {exc}")
            rc = -1
        run["status"] = "ok" if rc == 0 else "failed"
        run["finishedAt"] = time.time()
        append_log(run, f"завершено со статусом {run['status'].upper()} (код {rc})")


def start_deploy(cfg: dict, repo: dict, ref: str, dry_run: bool) -> dict:
    run = new_run(repo, ref, dry_run)
    threading.Thread(target=deploy_worker, args=(json.loads(json.dumps(cfg)), repo, run), daemon=True).start()
    return run


# ───────────────────────────── repos api ─────────────────────────────

def add_repo(cfg: dict, payload: dict):
    slug, url = normalize_repo_url(payload.get("url", ""))
    name = slug.split("/")[-1]
    if any(r["slug"].lower() == slug.lower() for r in cfg["repos"]):
        raise ValueError(f"{slug} уже добавлен")

    token = (payload.get("token") or "").strip() or cfg["settings"].get("githubToken", "")
    svc, health, grep, mode = WELL_KNOWN.get(name, ("", "", "", "service"))

    clone_dir = payload.get("cloneDir") or str(Path(cfg["settings"]["workspaceRoot"]) / name)
    if not Path(clone_dir, ".git").exists():
        append_note = f"клонирую в {clone_dir}…"
        u = url_with_token(url, token) if token else url
        Path(clone_dir).parent.mkdir(parents=True, exist_ok=True)
        rc, out = run_cmd(["git", "clone", u, clone_dir], timeout=600)
        if rc != 0:
            raise ValueError(f"clone не удался: {out[-500:]}. Для приватного репо укажите токен с scope repo.")
    else:
        append_note = f"найден существующий клон {clone_dir}"

    repo = {
        "id": uuid.uuid4().hex[:10],
        "name": name, "slug": slug, "url": url, "token": token,
        "cloneDir": clone_dir,
        "service": payload.get("service") or svc,
        "mode": mode,
        "healthUrl": payload.get("healthUrl") or health,
        "healthGrep": grep,
        "composeDir": cfg["settings"]["composeDir"],
        "defaultRef": payload.get("defaultRef") or "main",
        "note": append_note,
    }
    cfg["repos"].append(repo)
    save_config(cfg)
    return repo


def sanitize_repo(r: dict) -> dict:
    pub = {k: v for k, v in r.items() if k not in ("token",)}
    pub["hasToken"] = bool(r.get("token"))
    return pub


def repo_runs(repo_id: str):
    with _lock:
        runs = [r for r in _runs.values() if r["repoId"] == repo_id]
    runs.sort(key=lambda r: r["startedAt"] or r["finishedAt"] or 0, reverse=True)
    return runs


# ───────────────────────────── http ─────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "github-upgrade-helper/1.0"

    # -- plumbing --
    def log_message(self, fmt, *args):  # тихий доступ-лог
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _authorized(self) -> bool:
        pwd = load_config()["settings"].get("password") or ""
        if not pwd:
            return True
        auth = self.headers.get("Authorization", "")
        want = "Basic " + __import__("base64").b64encode(f"admin:{pwd}".encode()).decode()
        return hmac.compare_digest(auth, want)

    # -- routing --
    def do_GET(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="github-upgrade-helper"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        cfg = load_config()
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/" or path == "/index.html":
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            with _lock:
                runs = sorted(_runs.values(), key=lambda r: r["startedAt"] or 0, reverse=True)[:20]
            self._json(200, {
                "settings": {k: v for k, v in cfg["settings"].items() if k != "password"} | {"hasPassword": bool(cfg["settings"].get("password"))},
                "repos": [sanitize_repo(r) for r in cfg["repos"]],
                "runs": runs,
            })
        elif path.startswith("/api/runs/"):
            rid = path.split("/")[-1]
            with _lock:
                run = _runs.get(rid)
            self._json(200, run or {"error": "run not found"})
        elif path.startswith("/api/repos/") and path.endswith("/refs"):
            repo = _find(cfg, path.split("/")[3])
            if not repo:
                return self._json(404, {"error": "repo not found"})
            refs, err = ls_remote_heads(repo["url"], repo_token(cfg, repo))
            self._json(200, {"refs": refs or [], "error": err})
        elif path.startswith("/api/repos/") and path.endswith("/test"):
            repo = _find(cfg, path.split("/")[3])
            if not repo:
                return self._json(404, {"error": "repo not found"})
            refs, err = ls_remote_heads(repo["url"], repo_token(cfg, repo))
            ok = refs is not None
            self._json(200, {"ok": ok, "message": (f"доступ есть, веток/тегов: {len(refs)}" if ok else err)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        cfg = load_config()
        path = self.path.split("?")[0].rstrip("/")

        if path == "/api/repos":
            try:
                repo = add_repo(cfg, self._body())
                self._json(200, {"repo": sanitize_repo(repo)})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
        elif path.startswith("/api/repos/") and path.endswith("/deploy"):
            repo = _find(cfg, path.split("/")[3])
            if not repo:
                return self._json(404, {"error": "repo not found"})
            payload = self._body()
            ref = (payload.get("ref") or repo.get("defaultRef") or "main").strip()
            if not REF_RE.match(ref):
                return self._json(400, {"error": "недопустимый ref"})
            run = start_deploy(cfg, repo, ref, bool(payload.get("dryRun")))
            self._json(200, {"runId": run["id"]})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        cfg = load_config()
        path = self.path.split("?")[0].rstrip("/")
        if path.startswith("/api/repos/"):
            rid = path.split("/")[-1]
            before = len(cfg["repos"])
            cfg["repos"] = [r for r in cfg["repos"] if r["id"] != rid]
            if len(cfg["repos"]) == before:
                return self._json(404, {"error": "repo not found"})
            save_config(cfg)
            self._json(200, {"ok": True})   # клон на диске НЕ трогаем
        else:
            self._json(404, {"error": "not found"})


def _find(cfg: dict, rid: str):
    return next((r for r in cfg["repos"] if r["id"] == rid), None)


def main():
    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)
    s = cfg["settings"]
    RUNS_DIR.mkdir(exist_ok=True)
    httpd = ThreadingHTTPServer((s["bind"], int(s["port"])), Handler)
    print(f"github-upgrade-helper: http://{s['bind']}:{s['port']}"
          f"  (пароль: {'включён' if s.get('password') else 'ВЫКЛЮЧЕН — только localhost/VPN!'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
