#!/usr/bin/env python3
"""release-bot : bump, coupe, backport, livraison et garde-fous du gitflow.

Python standard uniquement (environnement air gapped). La plateforme se choisit
avec PLATFORM=gitlab|github|local. Le bot travaille dans un clone dont le
remote `origin` a les droits de push (token GitLab, deploy key GitHub).

Commandes :
  bump --branch B [--force]     bump de version sur la pointe de B (dev, release/X.Y, main) ;
                                --force : nouvelle RC même si la pointe est déjà un bump (action manuelle)
  deploy --branch B             déploiement factice si HEAD est un commit du bot
  cut X.Y                       crée release/X.Y depuis dev et pose X.Y.0-rc.1
  backports-for --sha S         backport de la MR mergée S selon ses labels backport-X.Y
  backport --sha S --line X.Y --ref N --title T
  scan [--days 7]               rattrape les labels posés après le merge
  livraison X.Y.Z-rc.N          prépare livraison/X.Y.Z et la MR vers main
  freeze X.Y                    gèle une ligne (tag frozen/X.Y)
  rollback X.Y.Z                redéploie en PROD une version déjà livrée (filet, git inchangé)
  guard --target T --source S [--title T] [--labels a,b]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BOT_NAME = os.environ.get("BOT_NAME", "release-bot")
BOT_EMAIL = os.environ.get("BOT_EMAIL", "release-bot@example.invalid")
MAX_LINES = 2
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$")
LINE = re.compile(r"^\d+\.\d+$")
PICKED = re.compile(r"cherry picked from commit ([0-9a-f]{7,40})")
BOT_ENV = {"GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL,
           "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL}


# --------------------------------------------------------------------------- git

def run(*args, check=True, env=None):
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       env={**os.environ, **BOT_ENV, **(env or {})})
    if check and r.returncode:
        sys.exit(f"ERREUR git {' '.join(args)} :\n{r.stdout}{r.stderr}")
    return r


def git(*args, **kw):
    return run(*args, **kw).stdout.strip()


def show(ref, path):
    r = run("show", f"{ref}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def is_ancestor(a, b):
    return run("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def author_email(ref="HEAD"):
    return git("log", "-1", "--format=%ae", ref)


def is_bump(c):
    """Commit de bump authentique : auteur bot ET ne touche que les fichiers de version.
    L'adresse d'auteur seule se falsifie ; le contenu, lui, est vérifiable."""
    if author_email(c) != BOT_EMAIL:
        return False
    files = set(git("diff-tree", "--no-commit-id", "--name-only", "-r", c).split())
    return files <= set(VERSION_FILES)


def remote_branches(pattern):
    out = git("ls-remote", "--heads", "origin", pattern)
    return [l.split("refs/heads/", 1)[1] for l in out.splitlines()]


def tags():
    return git("tag", "--list").split()


def fetch(*refs):
    git("fetch", "-q", "--prune", "--prune-tags", "--tags", "--force", "origin", *refs)


def die(msg):
    sys.exit(f"REFUS : {msg}")


def log(msg):
    print(f"[bot] {msg}", flush=True)


# --------------------------------------------------------------------- versions
# ponytail: 4 fichiers en dur. Un autre fichier de version = une entrée ici.

def _json_get(text):
    d = json.loads(text)
    return d["version"], d


def _yaml_image_tag(lines):
    """Index de la ligne `tag:` du bloc `image:` de premier niveau, ou None."""
    for i, line in enumerate(lines):
        if line.rstrip() == "image:":
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and not lines[j].startswith((" ", "\t")):
                    return None
                if re.match(r"^\s+tag:", lines[j]):
                    return j
    return None


def extract(path, text):
    if text is None:
        return None
    if path == "package.json":
        return _json_get(text)[0]
    if path == "package-lock.json":
        d = json.loads(text)
        return f'{d["version"]}|{d["packages"][""]["version"]}'
    if path == "pyproject.toml":
        m = re.search(r'(?m)^version\s*=\s*"([^"]*)"', text)
        return m and m.group(1)
    if path == "chart/values.yaml":
        lines = text.splitlines()
        i = _yaml_image_tag(lines)
        return i is not None and lines[i].split(":", 1)[1].strip().strip('"')
    raise ValueError(path)


VERSION_FILES = ("package.json", "package-lock.json", "pyproject.toml", "chart/values.yaml")


def read_versions(ref=None):
    return {f: extract(f, show(ref, f) if ref else open(f).read()) for f in VERSION_FILES}


def write_versions(version, image_tag):
    d = json.load(open("package.json"))
    d["version"] = version
    open("package.json", "w").write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    d = json.load(open("package-lock.json"))
    d["version"] = version
    d["packages"][""]["version"] = version
    open("package-lock.json", "w").write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    text = open("pyproject.toml").read()
    text = re.sub(r'(?m)^(version\s*=\s*)"[^"]*"', rf'\g<1>"{version}"', text, count=1)
    open("pyproject.toml", "w").write(text)
    lines = open("chart/values.yaml").read().splitlines()
    i = _yaml_image_tag(lines)
    if i is None:
        die("chart/values.yaml : pas de image.tag")
    lines[i] = re.sub(r"(tag:\s*).*", rf'\g<1>"{image_tag}"', lines[i])
    open("chart/values.yaml", "w").write("\n".join(lines) + "\n")
    # filet : relire ce qui vient d'être écrit (JSON/TOML/YAML cassé ou champ manqué)
    expected = {"package.json": version, "package-lock.json": f"{version}|{version}",
                "pyproject.toml": version, "chart/values.yaml": image_tag}
    got = read_versions()
    if got != expected:
        die(f"auto-vérification du bump : attendu {expected}, lu {got}")


def parse(v):
    m = SEMVER.match(v or "")
    return m and tuple(int(x) if x is not None else None for x in m.groups())


def line_key(line):
    return tuple(int(x) for x in line.split("."))


def finals():
    return sorted(p[:3] for p in map(parse, tags()) if p and p[3] is None)


def next_rc(line):
    """X.Y.Z-rc.N : Z = dernier final X.Y.* + 1 (ou 0), N = dernière RC de X.Y.Z + 1."""
    x, y = line_key(line)
    done = [f[2] for f in finals() if f[:2] == (x, y)]
    z = max(done) + 1 if done else 0
    rcs = [p[3] for p in map(parse, tags()) if p and p[:3] == (x, y, z) and p[3] is not None]
    return f"{x}.{y}.{z}-rc.{max(rcs) + 1 if rcs else 1}"


def frozen(line):
    return f"frozen/{line}" in tags()


def release_lines():
    return sorted((b.split("/", 1)[1] for b in remote_branches("release/*")
                   if LINE.match(b.split("/", 1)[1])), key=line_key)


def commit_and_push(branch, version, tag, new_branch=False):
    git("commit", "-qam", f"chore: bump {version}")
    refs = [f"HEAD:refs/heads/{branch}"] + ([f"refs/tags/{tag}"] if tag else [])
    if tag:
        git("tag", "-f", tag)
    r = run("push", "--atomic", "origin", *refs, check=False)
    if r.returncode:
        git("tag", "-d", tag) if tag else None
        return False
    log(f"{branch} : {version}" + (f", tag {tag}" if tag else ""))
    return True


# --------------------------------------------------------------- plateformes

class Platform:
    """Interface commune : MR, labels, commentaires."""

    def _req(self, method, url, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None


class GitHub(Platform):
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        self.h = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                  "Accept": "application/vnd.github+json"}

    def req(self, method, path, data=None):
        return self._req(method, f"{self.api}/repos/{self.repo}{path}", data, self.h)

    def mrs_for_commit(self, sha):
        return [{"iid": p["number"], "title": p["title"], "merged": bool(p["merged_at"]),
                 "labels": [l["name"] for l in p["labels"]]}
                for p in self.req("GET", f"/commits/{sha}/pulls")]

    def merged_into(self, target, days):
        since = datetime.now(timezone.utc) - timedelta(days=days)
        prs = self.req("GET", f"/pulls?state=closed&base={target}&sort=updated&direction=desc&per_page=100")
        return [{"iid": p["number"], "title": p["title"], "sha": p["merge_commit_sha"],
                 "labels": [l["name"] for l in p["labels"]]}
                for p in prs if p["merged_at"] and
                datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00")) > since]

    def open_mr(self, source, target, title, body, labels=()):
        owner = self.repo.split("/")[0]
        found = self.req("GET", f"/pulls?state=open&head={owner}:{urllib.parse.quote(source)}&base={target}")
        if found:
            return found[0]["html_url"]
        pr = self.req("POST", "/pulls", {"title": title, "head": source, "base": target, "body": body})
        if labels:
            self.req("POST", f"/issues/{pr['number']}/labels", {"labels": list(labels)})
        return pr["html_url"]

    def comments(self, iid):
        return [c["body"] for c in self.req("GET", f"/issues/{iid}/comments?per_page=100")]

    def comment(self, iid, body):
        self.req("POST", f"/issues/{iid}/comments", {"body": body})


class GitLab(Platform):
    def __init__(self):
        self.api = f"{os.environ['CI_API_V4_URL']}/projects/{os.environ['CI_PROJECT_ID']}"
        self.h = {"PRIVATE-TOKEN": os.environ["BOT_TOKEN"]}

    def req(self, method, path, data=None):
        return self._req(method, f"{self.api}{path}", data, self.h)

    def mrs_for_commit(self, sha):
        return [{"iid": m["iid"], "title": m["title"], "merged": m["state"] == "merged",
                 "labels": m["labels"]}
                for m in self.req("GET", f"/repository/commits/{sha}/merge_requests")]

    def merged_into(self, target, days):
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        mrs = self.req("GET", f"/merge_requests?state=merged&target_branch={target}"
                              f"&updated_after={urllib.parse.quote(since)}&per_page=100")
        return [{"iid": m["iid"], "title": m["title"], "sha": m["merge_commit_sha"] or m["sha"],
                 "labels": m["labels"]} for m in mrs]

    def open_mr(self, source, target, title, body, labels=()):
        q = urllib.parse.urlencode({"state": "opened", "source_branch": source, "target_branch": target})
        found = self.req("GET", f"/merge_requests?{q}")
        if found:
            return found[0]["web_url"]
        mr = self.req("POST", "/merge_requests", {
            "source_branch": source, "target_branch": target, "title": title,
            "description": body, "labels": ",".join(labels), "remove_source_branch": True})
        return mr["web_url"]

    def comments(self, iid):
        return [n["body"] for n in self.req("GET", f"/merge_requests/{iid}/notes?per_page=100")]

    def comment(self, iid, body):
        self.req("POST", f"/merge_requests/{iid}/notes", {"body": body})


class Local(Platform):
    """Plateforme simulée par un fichier JSON, pour les tests de scénarios."""

    def __init__(self):
        self.path = os.environ["PLATFORM_STATE"]

    def _load(self):
        return json.load(open(self.path))

    def _save(self, s):
        json.dump(s, open(self.path, "w"), indent=1)

    def mrs_for_commit(self, sha):
        return [m for m in self._load()["mrs"] if m.get("sha") == sha]

    def merged_into(self, target, days):
        return [m for m in self._load()["mrs"] if m.get("target") == target and m.get("sha")]

    def open_mr(self, source, target, title, body, labels=()):
        s = self._load()
        for m in s["mrs"]:
            if m["source"] == source and m["target"] == target and not m.get("sha"):
                return m["iid"]
        iid = len(s["mrs"]) + 1
        s["mrs"].append({"iid": iid, "source": source, "target": target, "title": title,
                         "labels": list(labels), "body": body, "by_bot": True})
        self._save(s)
        return iid

    def comments(self, iid):
        return [c["body"] for c in self._load()["comments"] if c["iid"] == iid]

    def comment(self, iid, body):
        s = self._load()
        s["comments"].append({"iid": iid, "body": body})
        self._save(s)


def platform():
    return {"github": GitHub, "gitlab": GitLab, "local": Local}[os.environ.get("PLATFORM", "local")]()


# ------------------------------------------------------------------ commandes

def cmd_bump(branch, force=False):
    for attempt in range(3):
        fetch(branch)
        git("checkout", "-q", "-B", branch, f"origin/{branch}")
        if is_bump("HEAD") and not force:
            log(f"{branch} : la pointe est déjà un commit du bot, rien à faire")
            return
        if branch == "dev":
            sha = git("rev-parse", "--short=7", "HEAD")
            version, image, tag = f"0.0.0-dev+g{sha}", sha, None
        elif m := re.fullmatch(r"release/(\d+\.\d+)", branch):
            version = image = tag = next_rc(m.group(1))
        elif branch == "main":
            current = read_versions()["package.json"]
            p = parse(current)
            if not p or p[3] is None:
                die(f"main : version {current} inattendue, une livraison apporte une RC")
            version = image = tag = current.split("-rc.")[0]
        else:
            die(f"branche {branch} : pas de bump")
        write_versions(version, image)
        if commit_and_push(branch, version, tag):
            return
        log(f"push refusé (la branche a bougé), nouvel essai {attempt + 2}/3")
    die("bump impossible après 3 essais")


def cmd_deploy(branch):
    if not is_bump("HEAD"):
        log("HEAD n'est pas un bump du bot : pas de déploiement")
        return
    env = {"dev": "DEV", "main": "PROD"}.get(branch) or f"EID {branch.split('/', 1)[1]}"
    v = read_versions()
    log(f"DÉPLOIEMENT FACTICE {v['package.json']} (image {v['chart/values.yaml']}) sur {env}")
    json.dump({"env": env, "versions": v, "sha": git("rev-parse", "HEAD")},
              open("deploy.json", "w"), indent=1)


def cmd_rollback(final):
    """Filet : redéploie en PROD une version déjà livrée, sans rien changer dans git."""
    p = parse(final)
    fetch()
    if not p or p[3] is not None or final not in tags():
        die(f"{final} n'est pas une version livrée (tag final introuvable)")
    current = finals()[-1]
    if p[:3] >= current:
        die(f"{final} n'est pas antérieure à la PROD actuelle ({'.'.join(map(str, current))})")
    git("checkout", "-q", "--detach", final)
    v = read_versions()
    log(f"ROLLBACK FACTICE : PROD redéployée en {v['package.json']} (image {v['chart/values.yaml']}). "
        f"main n'est pas modifiée : la prochaine livraison repartira normalement.")
    json.dump({"env": "PROD", "rollback": True, "versions": v, "sha": git("rev-parse", "HEAD")},
              open("deploy.json", "w"), indent=1)


def cmd_cut(line):
    if not LINE.match(line):
        die("RELEASE doit être de la forme X.Y")
    fetch("dev")
    lines = release_lines()
    if line in lines:
        die(f"release/{line} existe déjà")
    if lines and line_key(line) <= line_key(lines[-1]):
        die(f"{line} n'est pas plus récente que release/{lines[-1]}")
    if finals() and line_key(line) <= finals()[-1][:2]:
        die(f"{line} n'est pas plus récente que la PROD")
    active = [l for l in lines if not frozen(l)]
    if len(active) >= MAX_LINES:
        die(f"déjà {len(active)} lignes actives ({', '.join(active)}) : en geler une d'abord")
    git("checkout", "-q", "-B", f"release/{line}", "origin/dev")
    version = next_rc(line)
    write_versions(version, version)
    if not commit_and_push(f"release/{line}", version, version, new_branch=True):
        die("push de la coupe refusé")


def marker(line):
    return f"<!-- release-bot:backport-{line} -->"


def cmd_backport(sha, line, ref, title, plat=None):
    plat = plat or platform()
    target, branch = f"release/{line}", f"backport/{ref}-vers-{line}"
    fetch()
    if not LINE.match(line) or target not in remote_branches(target):
        plat.comment(ref, f"{marker(line)}\nBackport impossible : la ligne `{target}` n'existe pas.")
        return "absent"
    if frozen(line):
        plat.comment(ref, f"{marker(line)}\nBackport refusé : la ligne {line} est gelée.")
        return "gelée"
    if re.match(r"^feat(\(.*\))?!?:", title):
        plat.comment(ref, f"{marker(line)}\nBackport refusé : une feature (`feat:`) n'entre pas dans un patch.")
        return "feature"
    if git("log", f"origin/{target}", f"--grep=cherry picked from commit {sha}", "--format=%h"):
        log(f"!{ref} déjà backportée dans {target}")
        return "déjà fait"
    if branch in remote_branches(branch):
        url = plat.open_mr(branch, target, f"[{line}] {title} (!{ref})", body(sha, ref), ["backport"])
        log(f"branche {branch} déjà là, MR : {url}")
        return "reprise"
    git("checkout", "-q", "-B", branch, f"origin/{target}")
    parents = len(git("rev-list", "--parents", "-n1", sha).split()) - 1
    r = run("cherry-pick", "--no-commit", *(["-m", "1"] if parents > 1 else []), sha, check=False)
    conflicts = git("diff", "--name-only", "--diff-filter=U")
    if r.returncode or conflicts or not git("diff", "--cached", "--name-only"):
        run("cherry-pick", "--abort", check=False)
        run("reset", "-q", "--hard", f"origin/{target}", check=False)
        if conflicts or r.returncode:
            plat.comment(ref, f"{marker(line)}\nBackport vers `{target}` **en conflit** "
                              f"({conflicts.replace(chr(10), ', ') or 'voir le log'}). À faire par l'auteur :\n"
                              f"```sh\ngit fetch origin\ngit switch -c {branch} origin/{target}\n"
                              f"git cherry-pick -x {'-m 1 ' if parents > 1 else ''}{sha}\n"
                              f"# résoudre, git add, git cherry-pick --continue\n"
                              f"git push -u origin {branch}\n```\nPuis ouvrir la MR vers `{target}`.")
            return "conflit"
        plat.comment(ref, f"{marker(line)}\nBackport vers `{target}` inutile : le changement y est déjà présent.")
        return "vide"
    author = git("log", "-1", "--format=%an <%ae>", sha)
    git("commit", "-q", f"--author={author}", "-m",
        f"{title}\n\nBackport de !{ref} vers {target}.\n\n(cherry picked from commit {sha})")
    git("push", "-q", "-u", "origin", branch)
    url = plat.open_mr(branch, target, f"[{line}] {title} (!{ref})", body(sha, ref), ["backport"])
    log(f"backport !{ref} → {target} : {url}")
    return "ouverte"


def body(sha, ref):
    return (f"Backport automatique de !{ref} (commit `{sha[:12]}`).\n\n"
            "À vérifier : le diff est identique à celui de la MR d'origine.")


def backport_labels(labels):
    return [m.group(1) for l in labels if (m := re.fullmatch(r"backport-(\d+\.\d+)", l))]


def cmd_backports_for(sha):
    plat = platform()
    for mr in plat.mrs_for_commit(sha):
        for line in backport_labels(mr["labels"]):
            log(f"!{mr['iid']} → {line} : {cmd_backport(sha, line, mr['iid'], mr['title'], plat)}")


def cmd_scan(days):
    plat = platform()
    fetch()
    for mr in plat.merged_into("dev", days):
        for line in backport_labels(mr["labels"]):
            if any(marker(line) in c for c in plat.comments(mr["iid"])):
                continue  # déjà signalé (conflit, refus) : pas de relance quotidienne
            log(f"scan !{mr['iid']} → {line} : {cmd_backport(mr['sha'], line, mr['iid'], mr['title'], plat)}")


def audit(line, rc_tag):
    """Chaque correctif d'une ligne plus ancienne doit être dans la RC livrée."""
    missing = []
    for old in release_lines():
        if line_key(old) >= line_key(line):
            continue
        base = git("merge-base", f"origin/release/{old}", "origin/dev")
        for c in git("rev-list", "--no-merges", f"{base}..origin/release/{old}").split():
            if is_bump(c):
                continue
            msg = git("log", "-1", "--format=%B", c)
            m = PICKED.search(msg)
            if m and is_ancestor(m.group(1), rc_tag):
                continue
            if not m and "Justification:" in msg:
                continue
            if git("cherry", rc_tag, c, f"{c}^").startswith("-"):
                continue  # même patch déjà présent sous un autre SHA
            missing.append(f"{c[:9]} ({old}) {msg.splitlines()[0]}")
    if missing:
        die("audit : correctifs absents de " + rc_tag + " :\n  " + "\n  ".join(missing))


def purity(line, p, rc_tag):
    """Filet : depuis la livraison précédente de la ligne (ou la coupe), seuls des backports
    tracés, des exceptions justifiées et des bumps du bot ont pu entrer dans la RC."""
    prev = f"{p[0]}.{p[1]}.{p[2] - 1}" if p[2] > 0 else None
    base = prev if prev in tags() else git("merge-base", f"origin/release/{line}", "origin/dev")
    bad = []
    for c in git("rev-list", "--no-merges", f"{base}..{rc_tag}").split():
        if is_bump(c):
            continue
        msg = git("log", "-1", "--format=%B", c)
        m = PICKED.search(msg)
        if m and not is_ancestor(m.group(1), "origin/dev"):
            bad.append(f"{c[:9]} référence un commit absent de dev ({m.group(1)[:9]}) : {msg.splitlines()[0]}")
        elif not (m or "Justification:" in msg):
            bad.append(f"{c[:9]} {msg.splitlines()[0]}")
    if bad:
        die(f"pureté de release/{line} : commits ni backportés ni justifiés dans {rc_tag} :\n  " + "\n  ".join(bad))


def cmd_livraison(rc_tag):
    p = parse(rc_tag)
    if not p or p[3] is None:
        die("attendu un tag de RC X.Y.Z-rc.N")
    fetch()
    if rc_tag not in tags():
        die(f"tag {rc_tag} introuvable")
    final, line = rc_tag.split("-rc.")[0], f"{p[0]}.{p[1]}"
    if finals() and p[:3] <= finals()[-1]:
        die(f"{final} n'est pas plus récente que la PROD ({'.'.join(map(str, finals()[-1]))})")
    if not is_ancestor(rc_tag, f"origin/release/{line}"):
        die(f"{rc_tag} n'est pas sur release/{line}")
    audit(line, rc_tag)
    purity(line, p, rc_tag)
    branch = f"livraison/{final}"
    git("checkout", "-q", "-B", branch, rc_tag)
    if not is_ancestor("origin/main", "HEAD"):
        git("merge", "-q", "-s", "ours", "--no-edit", "-m",
            f"Livraison {final} : main devient ancêtre, contenu = {rc_tag}", "origin/main")
    if run("diff", "--quiet", rc_tag, "HEAD", check=False).returncode:
        die("le contenu de la livraison diffère de la RC")
    git("push", "-q", "-f", "-u", "origin", branch)
    url = platform().open_mr(branch, "main", f"Livraison {final}",
                             f"Livre exactement `{rc_tag}` en PROD.", ["livraison"])
    log(f"livraison {final} prête : {url}")


def cmd_freeze(line):
    fetch()
    git("tag", f"frozen/{line}", f"origin/release/{line}")
    git("push", "-q", "origin", f"refs/tags/frozen/{line}")
    log(f"ligne {line} gelée (penser à passer release/{line} en « No one » et arrêter EID {line})")


def cmd_guard(target, source, title, labels):
    fetch(target)
    rng, problems = f"origin/{target}..HEAD", []
    commits = git("rev-list", rng).split()
    spoofed = [c[:9] for c in git("rev-list", "--no-merges", rng).split()
               if author_email(c) == BOT_EMAIL and target != "main"]
    if spoofed:
        problems.append(f"commit(s) signé(s) au nom du bot dans une MR : {', '.join(spoofed)}")
    if target.startswith("release/"):
        if not (source.startswith("backport/") or "exception" in labels):
            problems.append("vers une release : branche backport/* ou label `exception` obligatoire")
        if git("rev-list", "--merges", rng):
            problems.append("commit de merge interdit (un `git merge origin/dev` ferait entrer la version suivante)")
        subjects = [git("log", "-1", "--format=%s", c) for c in commits]
        if any(re.match(r"^(\[\d+\.\d+\] )?feat(\(.*\))?!?:", s) for s in [title, *subjects] if s):
            problems.append("feature (`feat:`) interdite dans un patch")
        if "exception" in labels and not source.startswith("backport/"):
            if not any("Justification:" in git("log", "-1", "--format=%B", c) for c in commits):
                problems.append("exception : un commit doit contenir une ligne `Justification:`")
    if target == "main" and not source.startswith("livraison/"):
        problems.append("vers main : seulement une branche livraison/* préparée par le bot")
    if target != "main":
        base = git("merge-base", f"origin/{target}", "HEAD")
        before, after = read_versions(base), read_versions("HEAD")
        changed = [f for f in VERSION_FILES if before[f] != after[f]]
        if changed:
            problems.append(f"version modifiée à la main dans {', '.join(changed)} (c'est le rôle du bot)")
    if problems:
        die("garde-fous :\n  - " + "\n  - ".join(problems))
    log(f"garde-fous OK ({source} → {target})")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("deploy").add_argument("--branch", required=True)
    bp = sub.add_parser("bump")
    bp.add_argument("--branch", required=True)
    bp.add_argument("--force", action="store_true")
    sub.add_parser("cut").add_argument("line")
    sub.add_parser("backports-for").add_argument("--sha", required=True)
    b = sub.add_parser("backport")
    for a in ("--sha", "--line", "--ref", "--title"):
        b.add_argument(a, required=True)
    sub.add_parser("scan").add_argument("--days", type=int, default=7)
    sub.add_parser("livraison").add_argument("rc_tag")
    sub.add_parser("freeze").add_argument("line")
    sub.add_parser("rollback").add_argument("final")
    g = sub.add_parser("guard")
    g.add_argument("--target")
    g.add_argument("--source", required=True)
    g.add_argument("--title", default="")
    g.add_argument("--labels", default="")
    a = ap.parse_args()

    if a.cmd == "bump":
        cmd_bump(a.branch, a.force)
    elif a.cmd == "deploy":
        cmd_deploy(a.branch)
    elif a.cmd == "cut":
        cmd_cut(a.line)
    elif a.cmd == "backports-for":
        cmd_backports_for(a.sha)
    elif a.cmd == "backport":
        print(cmd_backport(a.sha, a.line, a.ref, a.title))
    elif a.cmd == "scan":
        cmd_scan(a.days)
    elif a.cmd == "livraison":
        cmd_livraison(a.rc_tag)
    elif a.cmd == "freeze":
        cmd_freeze(a.line)
    elif a.cmd == "rollback":
        cmd_rollback(a.final)
    elif a.cmd == "guard":
        target = a.target
        if not target and (m := re.fullmatch(r"backport/.+-vers-(\d+\.\d+)", a.source)):
            target = f"release/{m.group(1)}"
        if not target and a.source.startswith("livraison/"):
            target = "main"
        if not target:
            log("pas de cible déduite : rien à contrôler")
            return
        cmd_guard(target, a.source, a.title, [l.strip() for l in a.labels.split(",") if l.strip()])


if __name__ == "__main__":
    main()
