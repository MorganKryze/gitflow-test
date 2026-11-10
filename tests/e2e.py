#!/usr/bin/env python3
"""Tests de bout en bout sur une vraie plateforme (GitHub ou GitLab).

Le même scénario tourne sur les deux : un clone local joue l'humain, la CI de la
plateforme joue le bot. Chaque étape est notée dans e2e-<plateforme>.jsonl.

Usage : python3 tests/e2e.py github|gitlab [--from N] [--only N,M]
Prérequis : GitHub → `gh` connecté ; GitLab → token dans ~/.config/gitlab-sandbox-token.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("E2E_OUT", os.path.abspath(os.path.join(HERE, "..", "..")))
WORK = os.environ.get("E2E_WORK", "/tmp")
BOT = "release-bot@example.invalid"


def sh(*args, cwd=None, check=True, input=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, input=input)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args)} :\n{r.stdout}{r.stderr}")
    return r


# ---------------------------------------------------------------- GitHub

class GitHub:
    name = "github"
    repo = "MorganKryze/gitflow-test"
    ssh = "git@github.com:MorganKryze/gitflow-test.git"

    def env_name(self, branch):
        return {"dev": "DEV", "main": "PROD"}.get(branch) or f"EID {branch}"

    def api(self, method, path, body=None, ok=True):
        args = ["gh", "api", "-X", method, f"/repos/{self.repo}{path}", "-H", "Accept: application/vnd.github+json"]
        r = sh(*args, *(["--input", "-"] if body is not None else []), check=False,
               input=json.dumps(body) if body is not None else None)
        try:
            data = json.loads(r.stdout) if r.stdout.strip() else None
        except json.JSONDecodeError:
            data = r.stdout
        if ok and r.returncode:
            raise RuntimeError(f"{method} {path} : {r.stdout}{r.stderr}")
        return r.returncode == 0, data

    def open_mr(self, source, target, title, labels=()):
        _, pr = self.api("POST", "/pulls", {"title": title, "head": source, "base": target, "body": "e2e"})
        if labels:
            self.add_labels(pr["number"], labels)
        return pr["number"]

    def add_labels(self, n, labels):
        self.api("POST", f"/issues/{n}/labels", {"labels": list(labels)})
        time.sleep(12)  # laisse l'événement `labeled` relancer les checks

    def checks(self, n, timeout=600, sha=None):
        """Attend que test et guard soient terminés sur la tête de la PR ; renvoie {nom: conclusion}.
        sha : tête attendue (après un push, GitHub met quelques secondes à mettre la PR à jour)."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            _, pr = self.api("GET", f"/pulls/{n}")
            if sha and pr["head"]["sha"] != sha:
                time.sleep(4)
                continue
            _, cr = self.api("GET", f"/commits/{pr['head']['sha']}/check-runs?per_page=100")
            runs = [c for c in cr["check_runs"] if c["name"] in ("test", "guard")]
            if runs and all(c["status"] == "completed" for c in runs) and {c["name"] for c in runs} == {"test", "guard"}:
                time.sleep(3)  # un relancement (labeled) peut arriver juste après
                _, cr2 = self.api("GET", f"/commits/{pr['head']['sha']}/check-runs?per_page=100")
                runs = [c for c in cr2["check_runs"] if c["name"] in ("test", "guard")]
                if all(c["status"] == "completed" for c in runs):
                    latest = {}
                    for c in sorted(runs, key=lambda c: c["started_at"] or ""):
                        latest[c["name"]] = c["conclusion"]
                    return latest
            time.sleep(6)
        raise TimeoutError(f"checks PR #{n}")

    def merge(self, n):
        ok, data = self.api("PUT", f"/pulls/{n}/merge", {"merge_method": "merge"}, ok=False)
        return ok, (data or {}).get("sha") if ok else json.dumps(data)[:300]

    def close(self, n):
        self.api("PATCH", f"/pulls/{n}", {"state": "closed"})

    def busy(self):
        total = 0
        for st in ("queued", "in_progress", "waiting", "requested", "pending"):
            _, d = self.api("GET", f"/actions/runs?status={st}&per_page=1")
            total += d["total_count"]
        return total

    def failed_since(self, ts):
        _, d = self.api("GET", f"/actions/runs?per_page=50&created=%3E%3D{urllib.parse.quote(ts)}")
        return [f"{r['name']}#{r['run_number']} ({r['head_branch']}, {r['event']})"
                for r in d["workflow_runs"] if r["conclusion"] == "failure"]

    def read(self, ref, path):
        ok, d = self.api("GET", f"/contents/{path}?ref={urllib.parse.quote(ref, safe='')}", ok=False)
        return base64.b64decode(d["content"]).decode() if ok else None

    def dispatch(self, action, value=""):
        t0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
        sh("gh", "workflow", "run", "release.yml", "--repo", self.repo, "--ref", "dev",
           "-f", f"action={action}", "-f", f"value={value}")
        for _ in range(100):
            time.sleep(5)
            _, d = self.api("GET", f"/actions/workflows/release.yml/runs?event=workflow_dispatch&created=%3E%3D{t0}")
            runs = d["workflow_runs"]
            if runs and runs[0]["status"] == "completed":
                log = sh("gh", "run", "view", str(runs[0]["id"]), "--repo", self.repo, "--log", check=False).stdout
                return runs[0]["conclusion"] == "success", log
        raise TimeoutError(f"dispatch {action}")

    def open_bot_mrs(self):
        _, prs = self.api("GET", "/pulls?state=open&per_page=100")
        return [{"id": p["number"], "source": p["head"]["ref"], "target": p["base"]["ref"], "title": p["title"]}
                for p in prs if p["user"]["type"] == "Bot"]

    def comments(self, n):
        _, cs = self.api("GET", f"/issues/{n}/comments?per_page=100")
        return [c["body"] for c in cs]

    def env_sha(self, branch):
        _, d = self.api("GET", f"/deployments?environment={urllib.parse.quote(self.env_name(branch))}&per_page=1")
        return d[0]["sha"] if d else None


# ---------------------------------------------------------------- GitLab

class GitLab:
    name = "gitlab"
    pid = "88601123"
    ssh = "git@gitlab.com:MorganKryze/gitflow-test-gtilab.git"

    def __init__(self):
        self.token = open(os.path.expanduser("~/.config/gitlab-sandbox-token")).read().strip()
        self.base = f"https://gitlab.com/api/v4/projects/{self.pid}"
        self.expect_pipeline = {}

    def env_name(self, branch):
        return {"dev": "DEV", "main": "PROD"}.get(branch) or "eid/" + branch.replace("/", "-").replace(".", "-")

    def api(self, method, path, body=None, ok=True, raw=False):
        req = urllib.request.Request(f"{self.base}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"PRIVATE-TOKEN": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                txt = r.read().decode()
                return True, (txt if raw else (json.loads(txt) if txt else None))
        except urllib.error.HTTPError as e:
            txt = e.read().decode()
            if ok:
                raise RuntimeError(f"{method} {path} : {e.code} {txt}")
            return False, f"{e.code} {txt}"

    def open_mr(self, source, target, title, labels=()):
        _, mr = self.api("POST", "/merge_requests", {"source_branch": source, "target_branch": target, "title": title,
                                                     "labels": ",".join(labels), "remove_source_branch": True})
        return mr["iid"]

    def add_labels(self, iid, labels):
        self.api("PUT", f"/merge_requests/{iid}", {"add_labels": ",".join(labels)})
        # GitLab ne relance pas le pipeline de MR sur un changement de label
        _, p = self.api("POST", f"/merge_requests/{iid}/pipelines")
        self.expect_pipeline[iid] = p["id"]

    def checks(self, iid, timeout=900, sha=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            _, ps = self.api("GET", f"/merge_requests/{iid}/pipelines")
            ps = [p for p in ps if p["id"] >= self.expect_pipeline.get(iid, 0) and (not sha or p["sha"] == sha)]
            if ps and ps[0]["status"] in ("success", "failed", "canceled", "skipped"):
                _, jobs = self.api("GET", f"/pipelines/{ps[0]['id']}/jobs")
                return {j["name"]: j["status"] for j in jobs}
            time.sleep(8)
        raise TimeoutError(f"pipeline MR !{iid}")

    def merge(self, iid):
        for _ in range(30):
            _, mr = self.api("GET", f"/merge_requests/{iid}")
            if mr["detailed_merge_status"] not in ("checking", "unchecked", "preparing", "ci_still_running", "approvals_syncing"):
                break
            time.sleep(4)
        ok, d = self.api("PUT", f"/merge_requests/{iid}/merge", {"should_remove_source_branch": True}, ok=False)
        return ok, d.get("merge_commit_sha") if ok else f"{d} (statut {mr['detailed_merge_status']})"

    def close(self, iid):
        self.api("PUT", f"/merge_requests/{iid}", {"state_event": "close"})

    def busy(self):
        n = 0
        for st in ("created", "waiting_for_resource", "preparing", "pending", "running"):
            _, d = self.api("GET", f"/pipelines?status={st}&per_page=20")
            n += len(d)
        return n

    def failed_since(self, ts):
        _, d = self.api("GET", f"/pipelines?status=failed&updated_after={urllib.parse.quote(ts)}&per_page=50")
        return [f"pipeline {p['id']} ({p['ref']}, {p['source']})" for p in d]

    def read(self, ref, path):
        ok, d = self.api("GET", f"/repository/files/{urllib.parse.quote(path, safe='')}/raw?ref={urllib.parse.quote(ref, safe='')}",
                         ok=False, raw=True)
        return d if ok else None

    def dispatch(self, action, value=""):
        _, p = self.api("POST", "/pipeline", {"ref": "dev", "variables": [
            {"key": "ACTION", "value": action}, {"key": "VALUE", "value": value}]})
        for _ in range(150):
            time.sleep(6)
            _, p = self.api("GET", f"/pipelines/{p['id']}")
            if p["status"] in ("success", "failed", "canceled"):
                _, jobs = self.api("GET", f"/pipelines/{p['id']}/jobs")
                logs = "".join(self.api("GET", f"/jobs/{j['id']}/trace", raw=True)[1] for j in jobs)
                return p["status"] == "success", logs
        raise TimeoutError(f"dispatch {action}")

    def open_bot_mrs(self):
        _, mrs = self.api("GET", "/merge_requests?state=opened&per_page=100")
        return [{"id": m["iid"], "source": m["source_branch"], "target": m["target_branch"], "title": m["title"]}
                for m in mrs if set(m["labels"]) & {"backport", "livraison"}]

    def comments(self, iid):
        _, ns = self.api("GET", f"/merge_requests/{iid}/notes?per_page=100")
        return [n["body"] for n in ns if not n["system"]]

    def env_sha(self, branch):
        _, envs = self.api("GET", f"/environments?name={urllib.parse.quote(self.env_name(branch))}")
        if not envs:
            return None
        _, e = self.api("GET", f"/environments/{envs[0]['id']}")
        return (e.get("last_deployment") or {}).get("sha")


# ---------------------------------------------------------------- scénario

class Run:
    def __init__(self, p):
        self.p = p
        self.ws = os.path.join(WORK, f"e2e-{p.name}")
        self.out = os.path.join(OUT_DIR, f"e2e-{p.name}.jsonl")
        if not os.path.isdir(self.ws):
            sh("git", "clone", "-q", p.ssh, self.ws)
        self.ok = self.ko = 0
        self.mrs = {}  # nom logique → id

    def git(self, *a, check=True):
        return sh("git", *a, cwd=self.ws, check=check).stdout.strip()

    def note(self, step, what, ok, detail=""):
        mark = "✅" if ok else "❌"
        self.ok += ok
        self.ko += not ok
        line = {"t": time.strftime("%H:%M:%S"), "step": step, "ok": ok, "what": what, "detail": str(detail)[:1500]}
        open(self.out, "a").write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"{mark} [{step}] {what}" + (f"\n     {str(detail)[:600]}" if detail and not ok else ""), flush=True)
        return ok

    def idle(self, timeout=900):
        t0, calm = time.time(), 0
        time.sleep(10)
        while time.time() - t0 < timeout:
            calm = calm + 1 if self.p.busy() == 0 else 0
            if calm >= 2:
                return
            time.sleep(7)
        raise TimeoutError("la plateforme ne redevient pas inactive")

    def branch(self, name, base, changes, message):
        self.git("fetch", "-q", "--prune", "origin")
        self.git("checkout", "-q", "-B", name, f"origin/{base}")
        for path, fn in changes.items():
            full = os.path.join(self.ws, path)
            old = open(full).read() if os.path.exists(full) else ""
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w").write(fn(old) if callable(fn) else fn)
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        self.git("push", "-q", "-f", "origin", name)

    def flow(self, key, source, target, changes, title, labels=(), expect_merge=True, base=None, message=None):
        """Branche + MR + CI + merge. Renvoie le SHA de merge (ou None si refusé)."""
        self.branch(source, base or target, changes, message or title)
        mr = self.p.open_mr(source, target, title, labels)
        self.mrs[key] = mr
        res = self.p.checks(mr)
        ok, sha = self.p.merge(mr)
        self.note(key, f"MR {source} → {target} : CI {res}, merge {'OK' if ok else 'refusé'}",
                  ok == expect_merge, sha)
        if ok:
            self.idle()
        return sha if ok else None

    def merge_bot(self, key, target, contains=""):
        mrs = [m for m in self.p.open_bot_mrs() if m["target"] == target and contains in m["title"]]
        if not self.note(key, f"MR du bot vers {target} ouverte ({contains or 'toute'})", len(mrs) == 1, mrs):
            return None
        res = self.p.checks(mrs[0]["id"])
        ok, sha = self.p.merge(mrs[0]["id"])
        self.note(key, f"merge MR du bot {mrs[0]['source']} : CI {res}", ok, sha)
        self.idle()
        return sha

    def version(self, ref):
        txt = self.p.read(ref, "package.json")
        return json.loads(txt)["version"] if txt else None

    def expect_version(self, key, ref, expected):
        v = self.version(ref)
        return self.note(key, f"{ref} en {expected}", v == expected, f"lu : {v}")

    def tags(self):
        return [l.split("refs/tags/")[1] for l in self.git("ls-remote", "--tags", "origin").splitlines()]

    def deliver(self, key, rc, expect=True, contains=""):
        ok, log = self.p.dispatch("livraison", rc)
        if not self.note(key, f"pipeline de livraison {rc} {'réussi' if expect else 'refusé'}", ok == expect,
                         "" if ok == expect else log[-1500:]):
            return log
        if not expect:
            self.note(key, f"refus motivé ({contains})", contains in log, log[-800:])
            return log
        self.idle()
        final = rc.split("-rc.")[0]
        self.merge_bot(key, "main", f"Livraison {final}")
        self.expect_version(key, "main", final)
        self.note(key, f"tag {final} posé", final in self.tags())
        self.note(key, "PROD déployée sur la pointe de main",
                  self.p.env_sha("main") == self.git("ls-remote", "origin", "refs/heads/main").split()[0],
                  self.p.env_sha("main"))
        return log


def app_edit(old, new):
    def fn(t):
        assert old in t, f"{old!r} absent"
        return t.replace(old, new)
    return fn


def append(code):
    return lambda t: t + code


def story(r, only=None, start=1):
    p = r.p
    T0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def step(n):
        return (only is None or n in only) and n >= start

    if step(1):  # S01
        sha = r.flow("S01", "feature/a", "dev", {"src/app.py": append("\n\ndef feature_a():\n    return 'A'\n")}, "feat: ajoute A")
        short = r.git("rev-parse", "--short=7", sha) if sha else "?"
        r.git("fetch", "-q", "origin")
        r.expect_version("S01", "dev", f"0.0.0-dev+g{short}")
        values = p.read("dev", "chart/values.yaml") or ""
        r.note("S01", "values.yaml : image.tag = sha, sidecar intact", f'tag: "{short}"' in values and 'tag: "1.4.2"' in values, values)
        r.note("S01", "DEV déployé sur la pointe de dev", p.env_sha("dev") == r.git("rev-parse", "origin/dev"), p.env_sha("dev"))

    if step(2):  # S02
        ok, log = p.dispatch("cut", "3.3")
        r.note("S02", "coupe 3.3", ok, log[-1200:])
        r.idle()
        r.expect_version("S02", "release/3.3", "3.3.0-rc.1")
        r.note("S02", "tag 3.3.0-rc.1", "3.3.0-rc.1" in r.tags())
        r.git("fetch", "-q", "origin")
        r.note("S02", "EID 3.3 déployé", p.env_sha("release/3.3") == r.git("rev-parse", "origin/release/3.3"), p.env_sha("release/3.3"))
        ok, log = p.dispatch("cut", "3.3")
        r.note("S02", "seconde coupe 3.3 refusée (« existe déjà »)", not ok and "existe déjà" in log, log[-600:])

    if step(3):
        r.deliver("S02-livraison", "3.3.0-rc.1")

    if step(4):  # S03
        r.flow("S03", "feature/b", "dev", {"src/app.py": append("\n\ndef feature_b():\n    return 'B'\n")}, "feat: ajoute B")
        r.flow("S03", "fix/remise", "dev", {"src/app.py": app_edit("return price * (1 - rate)", "return round(price * (1 - rate), 2)")},
               "fix: arrondit la remise", ["backport-3.3"])
        r.merge_bot("S03", "release/3.3", "arrondit la remise")
        r.expect_version("S03", "release/3.3", "3.3.1-rc.1")
        app = p.read("release/3.3", "src/app.py") or ""
        r.note("S03", "release/3.3 a le fix et pas la feature B", "round(" in app and "feature_b" not in app)

    if step(5):  # S05
        r.flow("S05", "refacto/total", "dev", {"src/app.py": app_edit("return sum(prices)", "return sum(p for p in prices)")},
               "refactor: total")
        sha = r.flow("S05", "fix/total", "dev", {"src/app.py": app_edit("return sum(p for p in prices)", "return sum(p for p in prices or [])")},
                     "fix: total sur liste vide", ["backport-3.3"])
        mr = r.mrs["S05"]
        r.note("S05", "pas de MR de backport (conflit)", not [m for m in p.open_bot_mrs() if "liste vide" in m["title"]])
        r.note("S05", "commentaire « en conflit » avec les commandes", any("en conflit" in c and "cherry-pick -x -m 1" in c for c in p.comments(mr)),
               p.comments(mr))
        b = f"backport/{mr}-vers-3.3"
        r.git("fetch", "-q", "origin")
        r.git("checkout", "-q", "-B", b, "origin/release/3.3")
        r.git("cherry-pick", "-x", "-m", "1", sha, check=False)
        src = p.read("release/3.3", "src/app.py").replace("return sum(prices)", "return sum(prices or [])")
        open(os.path.join(r.ws, "src/app.py"), "w").write(src)
        r.git("add", "src/app.py")
        sh("git", "-c", "core.editor=true", "cherry-pick", "--continue", cwd=r.ws)
        r.git("push", "-q", "-f", "origin", b)
        mr2 = p.open_mr(b, "release/3.3", "[3.3] fix: total sur liste vide")
        res = p.checks(mr2)
        ok, _ = p.merge(mr2)
        r.note("S05", f"résolution manuelle acceptée : CI {res}", ok)
        r.idle()
        r.expect_version("S05", "release/3.3", "3.3.1-rc.2")

    if step(6):  # garde-fous S09 S10 S11
        r.git("fetch", "-q", "origin")
        r.git("checkout", "-q", "-B", "backport/999-vers-3.3", "origin/release/3.3")
        r.git("merge", "-q", "--no-edit", "-X", "ours", "origin/dev")
        r.git("push", "-q", "-f", "origin", "backport/999-vers-3.3")
        mr = p.open_mr("backport/999-vers-3.3", "release/3.3", "[3.3] fix: contamination")
        res = p.checks(mr)
        ok, msg = p.merge(mr)
        r.note("S09", f"merge de dev dans un backport : guard {res.get('guard')}, merge refusé", not ok, msg)
        p.close(mr)
        r.flow("S10", "backport/998-vers-3.3", "release/3.3", {"src/app.py": append("\n\ndef feature_c():\n    return 'C'\n")},
               "[3.3] feat: ajoute C", expect_merge=False, message="feat: ajoute C")
        p.close(r.mrs["S10"])
        r.flow("S11", "feature/version", "dev", {"package.json": lambda t: t.replace(json.loads(t)["version"], "9.9.9", 1)},
               "chore: version à la main", expect_merge=False)
        p.close(r.mrs["S11"])
        r.flow("S11", "feature/lib-a", "dev", {"package.json": app_edit('"lib-a": "1.0.0"', '"lib-a": "1.1.0"')}, "chore: lib-a 1.1.0")

    if step(7):  # S08 exception
        r.branch("fix-3.3/legacy", "release/3.3", {"src/app.py": app_edit("return f\"Bonjour {name}\"", "return f\"Bonjour {name or 'inconnu'}\"")},
                 "fix: salut sans nom")
        mr = p.open_mr("fix-3.3/legacy", "release/3.3", "fix: salut sans nom")
        res = p.checks(mr)
        r.note("S08", f"sans label : guard en échec ({res.get('guard')})", res.get("guard") in ("failure", "failed"))
        p.add_labels(mr, ["exception"])
        res = p.checks(mr)
        r.note("S08", f"label exception sans justification : guard en échec ({res.get('guard')})", res.get("guard") in ("failure", "failed"))
        r.git("commit", "-q", "--amend", "-m", "fix: salut sans nom\n\nJustification: code réécrit en 3.4, bug absent de dev.")
        r.git("push", "-q", "-f", "origin", "fix-3.3/legacy")
        if r.p.name == "gitlab":
            r.p.expect_pipeline.pop(mr, None)
        res = p.checks(mr, sha=r.git("rev-parse", "HEAD"))
        ok, _ = p.merge(mr)
        r.note("S08", f"avec justification : CI {res}, merge", ok)
        r.idle()
        r.expect_version("S08", "release/3.3", "3.3.1-rc.3")

    if step(8):  # S04
        ok, log = p.dispatch("cut", "3.4")
        r.note("S04", "coupe 3.4", ok, log[-800:])
        r.idle()
        r.expect_version("S04", "release/3.4", "3.4.0-rc.1")
        r.flow("S04", "fix/discount-neg", "dev", {"src/app.py": app_edit("def discount(price, rate):\n", "def discount(price, rate):\n    rate = max(rate, 0)\n")},
               "fix: remise négative", ["backport-3.3", "backport-3.4"])
        r.merge_bot("S04", "release/3.3", "remise négative")
        r.merge_bot("S04", "release/3.4", "remise négative")
        r.expect_version("S04", "release/3.3", "3.3.1-rc.4")
        r.expect_version("S04", "release/3.4", "3.4.0-rc.2")
        r.note("S04", "EID 3.3 et EID 3.4 déployés chacun sur sa ligne",
               p.env_sha("release/3.3") == r.git("ls-remote", "origin", "refs/heads/release/3.3").split()[0]
               and p.env_sha("release/3.4") == r.git("ls-remote", "origin", "refs/heads/release/3.4").split()[0])

    if step(9):
        r.deliver("S14-3.3.1", "3.3.1-rc.4")
        app = p.read("main", "src/app.py") or ""
        r.note("S14", "PROD 3.3.1 sans les features 3.4", "feature_b" not in app and "round(" in app)

    if step(10):  # S15 + S06
        r.flow("S15", "fix/greet", "dev", {"src/app.py": app_edit('return f"Bonjour {name}"', 'return f"Bonjour {name.strip()}"')},
               "fix: nom sans espaces", ["backport-3.3"])
        r.merge_bot("S15", "release/3.3", "nom sans espaces")
        r.expect_version("S15", "release/3.3", "3.3.2-rc.1")
        r.deliver("S15", "3.4.0-rc.2", expect=False, contains="nom sans espaces")
        p.add_labels(r.mrs["S15"], ["backport-3.4"])
        ok, log = p.dispatch("scan")
        r.note("S06", "scan : backport rattrapé après ajout du label", ok, log[-800:])
        r.idle()
        r.merge_bot("S06", "release/3.4", "nom sans espaces")
        r.expect_version("S06", "release/3.4", "3.4.0-rc.3")
        r.deliver("S14-3.4.0", "3.4.0-rc.3")

    if step(11):  # S16 + W01
        r.deliver("S16", "3.3.2-rc.1", expect=False, contains="pas plus récente que la PROD")
        ok, log = p.dispatch("rollback", "3.3.1")
        r.note("W01", "rollback PROD vers 3.3.1", ok and "ROLLBACK FACTICE" in log and "3.3.1" in log, log[-800:])
        r.expect_version("W01", "main", "3.4.0")

    if step(12):  # S12 / S13 concurrence
        r.flow("S13-prep", "fix/conc", "dev", {"src/conc.py": "VALEUR = 1\n"}, "fix: valeur concurrente", ["backport-3.4"])
        bp = [m for m in p.open_bot_mrs() if "valeur concurrente" in m["title"]]
        r.branch("feature/c1", "dev", {"src/c1.py": "C1 = 1\n"}, "feat: c1")
        r.branch("feature/c2", "dev", {"src/c2.py": "C2 = 2\n"}, "feat: c2")
        ids = [p.open_mr("feature/c1", "dev", "feat: c1"), p.open_mr("feature/c2", "dev", "feat: c2")]
        ids += [m["id"] for m in bp]
        for i in ids:
            p.checks(i)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        results = [p.merge(i) for i in ids]
        r.note("S12", f"3 merges en rafale ({len(ids)} MR)", all(ok for ok, _ in results), results)
        r.idle()
        fails = p.failed_since(ts)
        r.note("S13", "aucun pipeline en échec après la rafale", not fails, fails)
        r.git("fetch", "-q", "origin")
        r.note("S12", "pointe de dev = commit du bot", r.git("log", "-1", "--format=%ae", "origin/dev") == BOT)
        r.expect_version("S13", "release/3.4", "3.4.1-rc.1")

    if step(13):  # S21 base périmée
        r.branch("refacto/saluer", "dev", {"src/app.py": app_edit("def greet(name):", "def saluer(name):"),
                                            "tests/test_app.py": app_edit('app.greet("Ada")', 'app.saluer("Ada")')}, "refactor: greet devient saluer")
        r.branch("feature/test-bob", "dev", {"tests/test_app.py": append('assert app.greet("Bob") == "Bonjour Bob"\n')}, "test: Bob")
        a = p.open_mr("refacto/saluer", "dev", "refactor: greet devient saluer")
        b = p.open_mr("feature/test-bob", "dev", "test: Bob")
        ra, rb = p.checks(a), p.checks(b)
        r.git("fetch", "-q", "origin")
        before = r.git("rev-parse", "origin/dev")
        oka, _ = p.merge(a)
        r.idle()
        r.git("fetch", "-q", "origin")
        before = r.git("rev-parse", "origin/dev")
        okb, msg = p.merge(b)
        r.note("S21", f"MR B verte sur base périmée ({rb}) : merge {'accepté' if okb else 'refusé : ' + str(msg)[:120]}", oka, (ra, rb))
        r.idle()
        r.git("fetch", "-q", "origin")
        tip_author = r.git("log", "-1", "--format=%ae", "origin/dev")
        r.note("S21", "test après merge rouge : pas de bump ni de déploiement DEV" if okb else
               "plateforme a refusé le merge périmé (protection native)", (tip_author != BOT) if okb else True,
               f"pointe {tip_author}, DEV {p.env_sha('dev')}")
        if okb:
            r.flow("S21", "fix/greet-alias", "dev", {"src/app.py": append("\n\ngreet = saluer\n")}, "fix: alias greet")
            r.git("fetch", "-q", "origin")
            r.note("S21", "après correctif, le bump reprend", r.git("log", "-1", "--format=%ae", "origin/dev") == BOT)

    if step(14):  # S20 droits
        r.git("fetch", "-q", "origin")
        for ref, what in (("dev", "push direct sur dev"), ("release/3.4", "push direct sur release/3.4"), ("main", "push direct sur main")):
            r.git("checkout", "-q", "-B", "tmp-droits", f"origin/{ref}")
            open(os.path.join(r.ws, "INTRUS"), "w").write("x\n")
            r.git("add", "INTRUS")
            r.git("commit", "-qm", "chore: intrus")
            res = sh("git", "push", "origin", f"HEAD:{ref}", cwd=r.ws, check=False)
            r.note("S20", f"{what} refusé", res.returncode != 0, (res.stderr or res.stdout)[-300:])
        res = sh("git", "push", "--force", "origin", "origin/release/3.3~1:refs/heads/release/3.3", cwd=r.ws, check=False)
        r.note("S20", "force-push sur release/3.3 refusé", res.returncode != 0, res.stderr[-300:])
        res = sh("git", "push", "origin", "origin/dev:refs/tags/6.6.6", cwd=r.ws, check=False)
        r.note("S20", "tag posé à la main refusé", res.returncode != 0, res.stderr[-300:])
        if res.returncode == 0:
            sh("git", "push", "origin", ":refs/tags/6.6.6", cwd=r.ws, check=False)
        res = sh("git", "push", "origin", "origin/dev:refs/heads/release/9.9", cwd=r.ws, check=False)
        r.note("S20", "création manuelle de release/9.9 refusée", res.returncode != 0, res.stderr[-300:])
        if res.returncode == 0:
            sh("git", "push", "origin", ":refs/heads/release/9.9", cwd=r.ws, check=False)

    if step(15):  # S25 gel
        ok, log = p.dispatch("freeze", "3.3")
        r.note("S25", "gel de la ligne 3.3", ok, log[-600:])
        r.idle()
        r.flow("S25", "fix/gel", "dev", {"src/gel.py": "GEL = 1\n"}, "fix: après gel", ["backport-3.3"])
        mr = r.mrs["S25"]
        r.note("S25", "backport refusé avec commentaire « gelée »", any("gelée" in c for c in p.comments(mr)), p.comments(mr))

    r.note("FIN", f"pipelines en échec depuis le début (attendus : refus volontaires) : {p.failed_since(T0)}", True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("platform", choices=["github", "gitlab"])
    ap.add_argument("--from", dest="start", type=int, default=1)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    r = Run(GitHub() if a.platform == "github" else GitLab())
    only = {int(x) for x in a.only.split(",")} if a.only else None
    try:
        story(r, only, a.start)
    finally:
        print(f"\n{r.ok} OK, {r.ko} écarts — détail : {r.out}")
    sys.exit(1 if r.ko else 0)


if __name__ == "__main__":
    main()
