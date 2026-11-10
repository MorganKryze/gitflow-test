#!/usr/bin/env python3
"""Rejoue les scénarios du gitflow en local.

Un dépôt nu joue la plateforme, un clone joue le développeur, un autre clone
joue la CI qui lance bot/bot.py avec PLATFORM=local (MR et commentaires dans un
fichier JSON). Usage : python3 tests/scenarios.py [S03 S05 ...]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BOT = os.path.join(ROOT, "bot", "bot.py")
SEED = ["package.json", "package-lock.json", "pyproject.toml", "chart/values.yaml",
        "src/app.py", "tests/test_app.py"]
HUMAN = {"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
         "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}


class Refused(Exception):
    pass


class Sandbox:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="gitflow-")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.dev = os.path.join(self.tmp, "humain")
        self.ci = os.path.join(self.tmp, "ci")
        self.state = os.path.join(self.tmp, "plateforme.json")
        json.dump({"mrs": [], "comments": []}, open(self.state, "w"))
        self.sh(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        self.sh(self.tmp, "clone", "-q", self.origin, self.dev)
        for f in SEED:
            os.makedirs(os.path.dirname(os.path.join(self.dev, f)) or self.dev, exist_ok=True)
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.dev, f))
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        self.git("push", "-q", "origin", "main", "main:dev")
        self.sh(self.tmp, "clone", "-q", self.origin, self.ci)
        self.gestures = 0

    # ------------------------------------------------------------ outils
    def sh(self, cwd, *args, check=True):
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           env={**os.environ, **HUMAN})
        if check and r.returncode:
            raise RuntimeError(f"git {' '.join(args)} : {r.stdout}{r.stderr}")
        return r.stdout.strip()

    def git(self, *args, check=True):
        return self.sh(self.dev, *args, check=check)

    def bot(self, *args, ok=True):
        r = subprocess.run([sys.executable, BOT, *args], cwd=self.ci, capture_output=True, text=True,
                           env={**os.environ, "PLATFORM": "local", "PLATFORM_STATE": self.state})
        out = r.stdout + r.stderr
        if ok and r.returncode:
            raise AssertionError(f"bot {' '.join(args)} a échoué :\n{out}")
        if ok is False and not r.returncode:
            raise AssertionError(f"bot {' '.join(args)} aurait dû refuser :\n{out}")
        return out

    def load(self):
        return json.load(open(self.state))

    def save(self, s):
        json.dump(s, open(self.state, "w"), indent=1)

    def show(self, ref, path):
        self.git("fetch", "-q", "--tags", "--force", "origin")
        return self.git("show", f"{ref}:{path}")

    def version(self, ref):
        return json.loads(self.show(ref, "package.json"))["version"]

    def tags(self):
        self.git("fetch", "-q", "--tags", "--force", "origin")
        return self.git("tag", "--list").split()

    def open_bot_mrs(self):
        return [m for m in self.load()["mrs"] if m.get("by_bot") and not m.get("sha")]

    def comments(self, iid):
        return [c["body"] for c in self.load()["comments"] if c["iid"] == iid]

    # ------------------------------------------------------------ gestes
    def branch(self, name, base, changes, title, extra_commits=()):
        """Crée `name` depuis origin/base avec un commit (changes) et la pousse."""
        self.git("fetch", "-q", "origin")
        self.git("checkout", "-q", "-B", name, f"origin/{base}")
        for commit_changes, commit_title in [(changes, title), *extra_commits]:
            for path, fn in commit_changes.items():
                full = os.path.join(self.dev, path)
                old = open(full).read() if os.path.exists(full) else ""
                open(full, "w").write(fn(old) if callable(fn) else fn)
            self.git("add", "-A")
            self.git("commit", "-qm", commit_title)
        self.git("push", "-q", "-f", "origin", name)

    def guard(self, source, target, title, labels):
        self.sh(self.ci, "fetch", "-q", "origin")
        self.sh(self.ci, "checkout", "-q", "--detach", f"origin/{source}")
        r = subprocess.run([sys.executable, BOT, "guard", "--target", target, "--source", source,
                            "--title", title, "--labels", ",".join(labels)],
                           cwd=self.ci, capture_output=True, text=True,
                           env={**os.environ, "PLATFORM": "local", "PLATFORM_STATE": self.state})
        return r.returncode, r.stdout + r.stderr

    def merge(self, source, target, title, labels=(), method="merge", ci=True):
        """Un humain merge la MR source → target : garde-fous, merge, puis la CI du push."""
        rc, out = self.guard(source, target, title, labels)
        if rc:
            raise Refused(out)
        self.gestures += 1
        self.git("fetch", "-q", "origin")
        self.git("checkout", "-q", "-B", target, f"origin/{target}")
        if method == "merge":
            self.git("merge", "-q", "--no-ff", "-m", f"Merge branch '{source}' into '{target}'\n\n{title}",
                     f"origin/{source}")
        elif method == "squash":
            self.git("merge", "-q", "--squash", f"origin/{source}")
            self.git("commit", "-qm", title)
        else:
            self.git("merge", "-q", "--ff-only", f"origin/{source}")
        self.git("push", "-q", "origin", target)
        sha = self.git("rev-parse", "HEAD")
        s = self.load()
        mr = next((m for m in s["mrs"] if m["source"] == source and m["target"] == target
                   and not m.get("sha")), None)
        if mr is None:
            mr = {"iid": len(s["mrs"]) + 1, "source": source, "target": target}
            s["mrs"].append(mr)
        mr.update(title=title, labels=list(labels), sha=sha)
        self.save(s)
        if ci:
            self.push_ci(target, sha)
        return mr["iid"], sha

    def push_ci(self, branch, sha):
        """Ce que fait la CI sur un push de merge humain."""
        self.bot("bump", "--branch", branch)
        self.bot("deploy", "--branch", branch)
        if branch == "dev":
            self.bot("backports-for", "--sha", sha)

    def mr(self, source, target, changes, title, labels=(), base=None, **kw):
        self.branch(source, base or target, changes, title)
        return self.merge(source, target, title, labels, **kw)

    def merge_bot_mr(self, source_prefix):
        mr = next(m for m in self.open_bot_mrs() if m["source"].startswith(source_prefix))
        return self.merge(mr["source"], mr["target"], mr["title"], mr["labels"])

    def deliver(self, rc_tag):
        self.bot("livraison", rc_tag)
        self.gestures += 1  # lancement du pipeline de livraison
        return self.merge_bot_mr(f"livraison/{rc_tag.split('-rc.')[0]}")


def edit(old, new):
    def fn(text):
        assert old in text, f"{old!r} absent"
        return text.replace(old, new)
    return fn


def append(code):
    return lambda text: text + code


FIX_DISCOUNT = {"src/app.py": edit("return price * (1 - rate)", "return round(price * (1 - rate), 2)")}
FEAT_B = {"src/app.py": append("\n\ndef feature_b():\n    return 'B'\n")}


def prod_330(sb):
    """Situation de départ : 3.3.0 en PROD."""
    sb.mr("feature/a", "dev", {"src/app.py": append("\n\ndef feature_a():\n    return 'A'\n")}, "feat: ajoute A")
    sb.bot("cut", "3.3")
    sb.deliver("3.3.0-rc.1")
    assert sb.version("origin/main") == "3.3.0"
    sb.gestures = 0


# ------------------------------------------------------------------ scénarios

def S01(sb):
    """Feature nominale : bump dev dans les 4 fichiers, image = sha, déploiement DEV."""
    _, sha = sb.mr("feature/a", "dev", FEAT_B, "feat: ajoute B")
    short = sb.git("rev-parse", "--short=7", sha)
    assert sb.version("origin/dev") == f"0.0.0-dev+g{short}"
    lock = json.loads(sb.show("origin/dev", "package-lock.json"))
    assert lock["version"] == lock["packages"][""]["version"] == f"0.0.0-dev+g{short}"
    assert f'version = "0.0.0-dev+g{short}"' in sb.show("origin/dev", "pyproject.toml")
    values = sb.show("origin/dev", "chart/values.yaml")
    assert f'tag: "{short}"' in values and 'tag: "1.4.2"' in values, values
    assert json.load(open(os.path.join(sb.ci, "deploy.json")))["env"] == "DEV"


def S02(sb):
    """Coupe : rc.1, doublon refusé, 2 lignes max, gel, retour en arrière refusé."""
    sb.mr("feature/a", "dev", FEAT_B, "feat: ajoute B")
    sb.bot("cut", "3.3")
    assert sb.version("origin/release/3.3") == "3.3.0-rc.1" and "3.3.0-rc.1" in sb.tags()
    assert "existe déjà" in sb.bot("cut", "3.3", ok=False)
    sb.bot("cut", "3.4")
    assert "lignes actives" in sb.bot("cut", "3.5", ok=False)
    sb.bot("freeze", "3.3")
    sb.bot("cut", "3.5")
    assert "plus récente" in sb.bot("cut", "3.1", ok=False)
    assert "X.Y" in sb.bot("cut", "3.6.0", ok=False)


def S03(sb):
    """3.3.1 pendant que la 3.4 est en dev : la feature 3.4 ne part pas."""
    prod_330(sb)
    sb.mr("feature/b", "dev", FEAT_B, "feat: ajoute B")
    sb.gestures = 0
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    mrs = sb.open_bot_mrs()
    assert len(mrs) == 1 and mrs[0]["target"] == "release/3.3" and mrs[0]["title"].startswith("[3.3] fix:")
    sb.merge_bot_mr("backport/")
    assert sb.version("origin/release/3.3") == "3.3.1-rc.1"
    sb.deliver("3.3.1-rc.1")
    app = sb.show("origin/main", "src/app.py")
    assert sb.version("origin/main") == "3.3.1" and "round(" in app and "feature_b" not in app
    assert sb.gestures == 4, sb.gestures  # MR fix, MR backport, lancer livraison, MR livraison


def S04(sb):
    """3.3.1 pendant la recette 3.4 : deux backports, deux RC indépendantes."""
    prod_330(sb)
    sb.mr("feature/b", "dev", FEAT_B, "feat: ajoute B")
    sb.bot("cut", "3.4")
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3", "backport-3.4"])
    assert sorted(m["target"] for m in sb.open_bot_mrs()) == ["release/3.3", "release/3.4"]
    for target in ("release/3.3", "release/3.4"):
        sb.merge_bot_mr(next(m["source"] for m in sb.open_bot_mrs() if m["target"] == target))
    assert sb.version("origin/release/3.3") == "3.3.1-rc.1"
    assert sb.version("origin/release/3.4") == "3.4.0-rc.2"
    assert "feature_b" not in sb.show("origin/release/3.3", "src/app.py")


def S05(sb):
    """Conflit de cherry-pick : commentaire, résolution manuelle acceptée par les garde-fous."""
    prod_330(sb)
    sb.mr("refacto/remise", "dev", {"src/app.py": edit("return price * (1 - rate)",
                                                       "return price - price * rate")}, "refactor: remise")
    iid, sha = sb.mr("fix/remise", "dev", {"src/app.py": edit("return price - price * rate",
                                                              "return round(price - price * rate, 2)")},
                     "fix: arrondit la remise", ["backport-3.3"])
    assert not sb.open_bot_mrs()
    assert any("en conflit" in c and "cherry-pick -x -m 1" in c for c in sb.comments(iid))
    # l'auteur suit les commandes du commentaire
    b = f"backport/{iid}-vers-3.3"
    sb.git("fetch", "-q", "origin")
    sb.git("checkout", "-q", "-B", b, "origin/release/3.3")
    sb.git("cherry-pick", "-x", "-m", "1", sha, check=False)
    open(os.path.join(sb.dev, "src/app.py"), "w").write(
        sb.show("origin/release/3.3", "src/app.py").replace("return price * (1 - rate)",
                                                            "return round(price * (1 - rate), 2)"))
    sb.git("add", "src/app.py")
    sb.sh(sb.dev, "-c", "core.editor=true", "cherry-pick", "--continue")
    sb.git("push", "-q", "origin", b)
    sb.merge(b, "release/3.3", "[3.3] fix: arrondit la remise")
    assert sb.version("origin/release/3.3") == "3.3.1-rc.1"


def S06(sb):
    """Label posé après le merge : le scan quotidien rattrape, sans doublon."""
    prod_330(sb)
    iid, _ = sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise")
    assert not sb.open_bot_mrs()
    s = sb.load()
    next(m for m in s["mrs"] if m["iid"] == iid)["labels"] = ["backport-3.3"]
    sb.save(s)
    sb.bot("scan")
    sb.bot("scan")
    assert len(sb.open_bot_mrs()) == 1


def S07(sb):
    """Label oublié : la 3.3 n'a pas le fix, mais la 3.4 l'a quand même (échec sûr)."""
    prod_330(sb)
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise")
    sb.bot("cut", "3.4")
    assert "round(" in sb.show("origin/release/3.4", "src/app.py")
    assert "round(" not in sb.show("origin/release/3.3", "src/app.py")


def S08(sb):
    """Bug absent de dev : MR directe refusée sans label exception + justification."""
    prod_330(sb)
    sb.branch("fix-3.3/legacy", "release/3.3", {"src/app.py": edit("return sum(prices)", "return sum(prices or [])")},
              "fix: total sur liste vide")
    try:
        sb.merge("fix-3.3/legacy", "release/3.3", "fix: total sur liste vide")
        raise AssertionError("aurait dû être refusée")
    except Refused as e:
        assert "backport/* ou label `exception`" in str(e)
    try:
        sb.merge("fix-3.3/legacy", "release/3.3", "fix: total sur liste vide", ["exception"])
        raise AssertionError("aurait dû être refusée")
    except Refused as e:
        assert "Justification" in str(e)
    sb.branch("fix-3.3/legacy", "release/3.3", {"src/app.py": edit("return sum(prices)", "return sum(prices or [])")},
              "fix: total sur liste vide\n\nJustification: code réécrit en 3.4, bug absent de dev.")
    sb.merge("fix-3.3/legacy", "release/3.3", "fix: total sur liste vide", ["exception"])
    assert sb.version("origin/release/3.3") == "3.3.1-rc.1"
    sb.bot("cut", "3.4")
    sb.bot("livraison", "3.4.0-rc.1")  # l'audit accepte grâce à la justification


def S09(sb):
    """`git merge origin/dev` dans une branche de backport : bloqué."""
    prod_330(sb)
    sb.mr("feature/b", "dev", FEAT_B, "feat: ajoute B")
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    b = sb.open_bot_mrs()[0]["source"]
    sb.git("fetch", "-q", "origin")
    sb.git("checkout", "-q", "-B", b, f"origin/{b}")
    sb.git("merge", "-q", "--no-edit", "-X", "ours", "origin/dev")
    sb.git("push", "-q", "-f", "origin", b)
    try:
        sb.merge(b, "release/3.3", "[3.3] fix: arrondit la remise")
        raise AssertionError("aurait dû être refusée")
    except Refused as e:
        assert "commit de merge interdit" in str(e)


def S10(sb):
    """Feature : le bot refuse le backport, les garde-fous refusent une MR feat:."""
    prod_330(sb)
    iid, _ = sb.mr("feature/b", "dev", FEAT_B, "feat: ajoute B", ["backport-3.3"])
    assert not sb.open_bot_mrs() and any("feature" in c for c in sb.comments(iid))
    sb.branch("backport/99-vers-3.3", "release/3.3", FEAT_B, "feat: ajoute B")
    try:
        sb.merge("backport/99-vers-3.3", "release/3.3", "[3.3] feat: ajoute B")
        raise AssertionError("aurait dû être refusée")
    except Refused as e:
        assert "feature" in str(e)


def S11(sb):
    """Version modifiée à la main : refusé. Montée de dépendance seule : accepté."""
    sb.branch("feature/v", "dev", {"package.json": edit('"version": "0.0.0-dev+g0000000"', '"version": "9.9.9"')},
              "chore: version")
    try:
        sb.merge("feature/v", "dev", "chore: version")
        raise AssertionError("aurait dû être refusée")
    except Refused as e:
        assert "version modifiée" in str(e)
    sb.mr("feature/dep", "dev", {"package.json": edit('"lib-a": "1.0.0"', '"lib-a": "1.1.0"')}, "chore: lib-a 1.1.0")


def S12(sb):
    """Deux merges avant le bump : un seul bump couvre les deux, le second job ne fait rien."""
    sb.mr("feature/a", "dev", FEAT_B, "feat: ajoute B", ci=False)
    _, sha2 = sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ci=False)
    sb.bot("bump", "--branch", "dev")
    out = sb.bot("bump", "--branch", "dev")
    assert "rien à faire" in out
    short = sb.git("rev-parse", "--short=7", sha2)
    assert sb.version("origin/dev") == f"0.0.0-dev+g{short}"
    assert sb.git("log", "-1", "--format=%ae", "origin/dev") == "release-bot@example.invalid"


def S14(sb):
    """Livraison de la 3.4.0 après la 3.3.1 : audit vert, PROD = RC recettée."""
    prod_330(sb)
    sb.mr("feature/b", "dev", FEAT_B, "feat: ajoute B")
    sb.bot("cut", "3.4")
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3", "backport-3.4"])
    for target in ("release/3.3", "release/3.4"):
        sb.merge_bot_mr(next(m["source"] for m in sb.open_bot_mrs() if m["target"] == target))
    sb.deliver("3.3.1-rc.1")
    sb.deliver("3.4.0-rc.2")
    assert sb.version("origin/main") == "3.4.0"
    diff = sb.git("diff", "--name-only", "3.4.0-rc.2", "origin/main")
    assert set(diff.split()) <= {"package.json", "package-lock.json", "pyproject.toml", "chart/values.yaml"}, diff


def S15(sb):
    """Fix backporté en 3.3 mais pas en 3.4 (coupée avant) : livraison 3.4 bloquée."""
    prod_330(sb)
    sb.bot("cut", "3.4")
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    sb.deliver("3.3.1-rc.1")
    out = sb.bot("livraison", "3.4.0-rc.1", ok=False)
    assert "audit" in out and "arrondit la remise" in out, out


def S16(sb):
    """3.3.2 après une 3.4.0 en PROD : refusé."""
    prod_330(sb)
    sb.bot("cut", "3.4")
    sb.deliver("3.4.0-rc.1")
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    assert "pas plus récente que la PROD" in sb.bot("livraison", "3.3.1-rc.1", ok=False)


def S17(sb):
    """La release a avancé après la RC recettée : on livre la RC, pas la pointe."""
    prod_330(sb)
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    sb.mr("fix/total", "dev", {"src/app.py": edit("return sum(prices)", "return sum(prices or [])")},
          "fix: total sur liste vide", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    assert sb.version("origin/release/3.3") == "3.3.1-rc.2"
    sb.deliver("3.3.1-rc.1")
    app = sb.show("origin/main", "src/app.py")
    assert "round(" in app and "prices or []" not in app
    # le contenu de rc.2 n'a pas de RC livrable : action manuelle « bump »
    assert "rien à faire" in sb.bot("bump", "--branch", "release/3.3")
    sb.bot("bump", "--branch", "release/3.3", "--force")
    assert sb.version("origin/release/3.3") == "3.3.2-rc.1"
    sb.deliver("3.3.2-rc.1")
    assert "prices or []" in sb.show("origin/main", "src/app.py")


def S18(sb):
    """Recette KO : second correctif, rc.2, livraison de rc.2."""
    prod_330(sb)
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    sb.mr("fix/remise-2", "dev", {"src/app.py": edit("round(price * (1 - rate), 2)",
                                                     "round(price * (1 - min(rate, 1)), 2)")},
          "fix: plafonne la remise", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    sb.deliver("3.3.1-rc.2")
    assert sb.version("origin/main") == "3.3.1" and "min(rate, 1)" in sb.show("origin/main", "src/app.py")


def S19(sb):
    """Fix déjà présent sur la ligne : « déjà présent », pas de MR vide."""
    prod_330(sb)
    sb.branch("fix-3.3/remise", "release/3.3", FIX_DISCOUNT,
              "fix: arrondit la remise\n\nJustification: urgence, reporté dans dev ensuite.")
    sb.merge("fix-3.3/remise", "release/3.3", "fix: arrondit la remise", ["exception"])
    iid, _ = sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    assert not sb.open_bot_mrs() and any("déjà présent" in c for c in sb.comments(iid))


def S22(sb):
    """Fix qui monte une dépendance : la montée arrive intacte dans la ligne."""
    prod_330(sb)
    sb.mr("fix/lib-a", "dev", {
        "package.json": edit('"lib-a": "1.0.0"', '"lib-a": "1.0.1"'),
        "package-lock.json": lambda t: t.replace('"lib-a": "1.0.0"', '"lib-a": "1.0.1"').replace(
            '"node_modules/lib-a": {\n      "version": "1.0.0"', '"node_modules/lib-a": {\n      "version": "1.0.1"')},
        "fix: lib-a 1.0.1 (CVE)", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    pkg = json.loads(sb.show("origin/release/3.3", "package.json"))
    lock = json.loads(sb.show("origin/release/3.3", "package-lock.json"))
    assert pkg["dependencies"]["lib-a"] == "1.0.1" and pkg["version"] == "3.3.1-rc.1"
    assert lock["packages"]["node_modules/lib-a"]["version"] == "1.0.1"


def S23(sb):
    """Revert d'un backport : MR d'exception sur la ligne, dev garde le fix."""
    prod_330(sb)
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    sb.git("fetch", "-q", "origin")
    sb.git("checkout", "-q", "-B", "revert/remise", "origin/release/3.3")
    picked = sb.git("log", "--format=%H", "--grep=cherry picked from", "-1", "origin/release/3.3")
    sb.git("revert", "--no-edit", picked)
    sb.git("commit", "-q", "--amend", "-m", "revert: annule l'arrondi\n\nJustification: régression en recette.")
    sb.git("push", "-q", "-f", "origin", "revert/remise")
    sb.merge("revert/remise", "release/3.3", "revert: annule l'arrondi", ["exception"])
    assert sb.version("origin/release/3.3") == "3.3.1-rc.2"
    assert "round(" not in sb.show("origin/release/3.3", "src/app.py")
    assert "round(" in sb.show("origin/dev", "src/app.py")


def S24(sb):
    """Rejeu après panne : pas de doublon, la MR manquante est recréée une fois."""
    prod_330(sb)
    _, sha = sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    sb.bot("backports-for", "--sha", sha)
    assert len(sb.open_bot_mrs()) == 1
    s = sb.load()  # panne simulée : branche poussée, MR jamais créée
    s["mrs"] = [m for m in s["mrs"] if not m.get("by_bot")]
    sb.save(s)
    assert "reprise" in sb.bot("backports-for", "--sha", sha)
    sb.bot("backports-for", "--sha", sha)
    assert len(sb.open_bot_mrs()) == 1


def S25(sb):
    """Ligne gelée : le label est refusé avec un commentaire."""
    prod_330(sb)
    sb.bot("cut", "3.4")
    sb.deliver("3.4.0-rc.1")
    sb.bot("freeze", "3.3")
    iid, _ = sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    assert not sb.open_bot_mrs() and any("gelée" in c for c in sb.comments(iid))


def S26(sb):
    """Méthode de merge : fast-forward = backport incomplet ; squash = backport complet."""
    prod_330(sb)
    two = ({"src/app.py": edit("return sum(prices)", "return sum(prices or [])")}, "fix: total partie 2")
    sb.branch("fix/ff", "dev", FIX_DISCOUNT, "fix: total partie 1", extra_commits=[two])
    sb.merge("fix/ff", "dev", "fix: total", ["backport-3.3"], method="ff")
    sb.merge_bot_mr("backport/")
    app = sb.show("origin/release/3.3", "src/app.py")
    ff_incomplete = "prices or []" in app and "round(" not in app
    sb2 = Sandbox()
    prod_330(sb2)
    sb2.branch("fix/sq", "dev", FIX_DISCOUNT, "fix: total partie 1", extra_commits=[two])
    sb2.merge("fix/sq", "dev", "fix: total", ["backport-3.3"], method="squash")
    sb2.merge_bot_mr("backport/")
    app2 = sb2.show("origin/release/3.3", "src/app.py")
    shutil.rmtree(sb2.tmp)
    assert ff_incomplete, "fast-forward : on attendait un backport incomplet"
    assert "prices or []" in app2 and "round(" in app2, "squash : backport complet attendu"


# ------------------------------------------------------------- pires cas (W)

def W01(sb):
    """Rollback : PROD redéployée en 3.3.0 sans toucher à main ; rollback vers l'actuelle ou l'inconnue refusé."""
    prod_330(sb)
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    sb.deliver("3.3.1-rc.1")
    sb.bot("rollback", "3.3.0")
    d = json.load(open(os.path.join(sb.ci, "deploy.json")))
    assert d["env"] == "PROD" and d["rollback"] and d["versions"]["package.json"] == "3.3.0", d
    assert sb.version("origin/main") == "3.3.1"
    assert "antérieure" in sb.bot("rollback", "3.3.1", ok=False)
    assert "introuvable" in sb.bot("rollback", "9.9.9", ok=False)


def W02(sb):
    """Garde-fous contournés (push admin direct sur la release) : la livraison est bloquée par la pureté."""
    prod_330(sb)
    sb.git("fetch", "-q", "origin")
    sb.git("checkout", "-q", "-B", "release/3.3", "origin/release/3.3")
    open(os.path.join(sb.dev, "src/app.py"), "a").write("\n\ndef en_douce():\n    return 1\n")
    sb.git("commit", "-qam", "chore: correctif poussé en direct")
    sb.git("push", "-q", "origin", "release/3.3")
    sb.push_ci("release/3.3", sb.git("rev-parse", "HEAD"))
    out = sb.bot("livraison", "3.3.1-rc.1", ok=False)
    assert "pureté" in out and "poussé en direct" in out, out


def W03(sb):
    """Fichier de version cassé poussé en direct : le bump échoue visiblement, rien n'est poussé."""
    sb.git("fetch", "-q", "origin")
    sb.git("checkout", "-q", "-B", "dev", "origin/dev")
    open(os.path.join(sb.dev, "chart/values.yaml"), "w").write("replicaCount: 1\n")
    sb.git("commit", "-qam", "chore: values.yaml écrasé")
    sb.git("push", "-q", "origin", "dev")
    before = sb.git("rev-parse", "origin/dev")
    out = sb.bot("bump", "--branch", "dev", ok=False)
    assert "image.tag" in out, out
    sb.git("fetch", "-q", "origin")
    assert sb.git("rev-parse", "origin/dev") == before


def W04(sb):
    """Usurpation : commit signé au nom du bot dans une MR de backport → refusé par les garde-fous."""
    prod_330(sb)
    sb.git("fetch", "-q", "origin")
    sb.git("checkout", "-q", "-B", "backport/77-vers-3.3", "origin/release/3.3")
    open(os.path.join(sb.dev, "src/app.py"), "a").write("\n\ndef cache():\n    return 1\n")
    sb.git("add", "-A")
    subprocess.run(["git", "commit", "-qm", "chore: bump 3.3.1-rc.1"], cwd=sb.dev, check=True,
                   env={**os.environ, **HUMAN, "GIT_AUTHOR_NAME": "release-bot",
                        "GIT_AUTHOR_EMAIL": "release-bot@example.invalid"})
    sb.git("push", "-q", "-f", "origin", "backport/77-vers-3.3")
    try:
        sb.merge("backport/77-vers-3.3", "release/3.3", "[3.3] chore: bump")
        raise AssertionError("aurait dû être refusée")
    except Refused as e:
        assert "au nom du bot" in str(e), e


def W05(sb):
    """Trailer de cherry-pick inventé, poussé en direct sur la release → livraison bloquée."""
    prod_330(sb)
    sb.git("fetch", "-q", "origin")
    sb.git("checkout", "-q", "-B", "release/3.3", "origin/release/3.3")
    open(os.path.join(sb.dev, "src/app.py"), "a").write("\n\ndef porte():\n    return 1\n")
    sb.git("commit", "-qam", "fix: discret\n\n(cherry picked from commit " + "d" * 40 + ")")
    sb.git("push", "-q", "origin", "release/3.3")
    sb.push_ci("release/3.3", sb.git("rev-parse", "HEAD"))
    out = sb.bot("livraison", "3.3.1-rc.1", ok=False)
    assert "absent de dev" in out, out


def W06(sb):
    """MR vers main depuis une fausse branche livraison/ au contenu modifié → refusée."""
    prod_330(sb)
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    sb.bot("livraison", "3.3.1-rc.1")
    sb.git("fetch", "-q", "origin")
    sb.git("checkout", "-q", "-B", "livraison/3.3.1", "origin/livraison/3.3.1")
    open(os.path.join(sb.dev, "src/app.py"), "a").write("\n\ndef ajout_tardif():\n    return 1\n")
    sb.git("commit", "-qam", "fix: ajout tardif")
    sb.git("push", "-q", "-f", "origin", "livraison/3.3.1")
    try:
        sb.merge("livraison/3.3.1", "main", "Livraison 3.3.1")
        raise AssertionError("aurait dû être refusée")
    except Refused as e:
        assert "identique à aucune RC" in str(e), e


def W07(sb):
    """Tags bidons posés à la main (6.6.6, 3.3.0-rc.9) : ignorés, versions et livraisons intactes."""
    prod_330(sb)
    sb.git("fetch", "-q", "origin")
    sb.git("tag", "6.6.6", "origin/dev")
    sb.git("tag", "3.3.1-rc.9", "origin/dev")
    sb.git("push", "-q", "origin", "6.6.6", "3.3.1-rc.9")
    sb.bot("cut", "3.4")
    assert sb.version("origin/release/3.4") == "3.4.0-rc.1"
    sb.mr("fix/remise", "dev", FIX_DISCOUNT, "fix: arrondit la remise", ["backport-3.3"])
    sb.merge_bot_mr("backport/")
    assert sb.version("origin/release/3.3") == "3.3.1-rc.1"
    sb.deliver("3.3.1-rc.1")


SCENARIOS = [S01, S02, S03, S04, S05, S06, S07, S08, S09, S10, S11, S12, S14, S15, S16, S17, S18, S19,
             S22, S23, S24, S25, S26, W01, W02, W03, W04, W05, W06, W07]


def main():
    wanted = set(sys.argv[1:])
    results = []
    for fn in SCENARIOS:
        if wanted and fn.__name__ not in wanted:
            continue
        sb = Sandbox()
        try:
            fn(sb)
            results.append((fn.__name__, "OK", fn.__doc__))
            print(f"OK    {fn.__name__}  {fn.__doc__}")
        except Exception as e:  # noqa: BLE001
            results.append((fn.__name__, "ÉCHEC", f"{fn.__doc__}\n{e}"))
            print(f"ÉCHEC {fn.__name__}  {fn.__doc__}\n      {e}")
            if os.environ.get("DEBUG"):
                traceback.print_exc()
                print(f"      dépôts gardés dans {sb.tmp}")
                continue
        shutil.rmtree(sb.tmp, ignore_errors=True)
    failed = [r for r in results if r[1] != "OK"]
    print(f"\n{len(results) - len(failed)}/{len(results)} scénarios OK")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
