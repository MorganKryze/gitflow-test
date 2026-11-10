#!/usr/bin/env python3
"""Phase A : ce qu'un compte Developer peut et ne peut pas faire (GitLab).

Le Developer agit avec son token (~/.config/gitlab-sandbox-dev-token), en HTTPS.
L'Owner (token habituel) sert seulement à préparer et à vérifier.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import e2e  # noqa: E402
from e2e import sh  # noqa: E402


class DevGitLab(e2e.GitLab):
    def __init__(self):
        super().__init__()
        self.token = open(os.path.expanduser("~/.config/gitlab-sandbox-dev-token")).read().strip()


owner = e2e.Run(e2e.GitLab())
dev = DevGitLab()
ws = os.path.join(e2e.WORK, "droits-dev")
url = f"https://oauth2:{dev.token}@gitlab.com/MorganKryze/gitflow-test-gtilab.git"
if not os.path.isdir(ws):
    sh("git", "clone", "-q", url, ws)


def g(*a, check=True):
    return sh("git", *a, cwd=ws, check=check)


def note(what, ok, detail=""):
    owner.note("A-droits", what, ok, detail)


g("fetch", "-q", "origin")
for ref in ("dev", "release/3.6", "main"):
    g("checkout", "-q", "-B", "tmp", f"origin/{ref}")
    open(os.path.join(ws, "INTRUS_DEV"), "w").write("x\n")
    g("add", "INTRUS_DEV")
    g("-c", "user.name=dev", "-c", "user.email=dev@example.com", "commit", "-qm", "chore: intrus dev")
    r = g("push", "origin", f"HEAD:{ref}", check=False)
    note(f"Developer : push direct sur {ref} refusé", r.returncode != 0, r.stderr[-200:])
r = g("push", "origin", "origin/dev:refs/tags/9.9.9", check=False)
note("Developer : tag posé à la main refusé", r.returncode != 0, r.stderr[-200:])
r = g("push", "origin", "origin/dev:refs/heads/release/9.6", check=False)
note("Developer : création de release/9.6 refusée", r.returncode != 0, r.stderr[-200:])

# Un dev merge sa propre MR vers dev
g("checkout", "-q", "-B", "feature/dev-perso", "origin/dev")
open(os.path.join(ws, "src", "dev_perso.py"), "w").write("D = 1\n")
g("add", "-A")
g("-c", "user.name=dev", "-c", "user.email=dev@example.com", "commit", "-qm", "feat: travail du dev")
g("push", "-q", "-f", "origin", "feature/dev-perso")
mr = dev.open_mr("feature/dev-perso", "dev", "feat: travail du dev")
res = dev.checks(mr)
ok, d = dev.merge(mr)
note(f"Developer : merge de sa MR vers dev autorisé (CI {res})", ok, d)
owner.idle()

# Un dev tente de merger vers une release : MR de backport ouverte par le bot
g("checkout", "-q", "-B", "fix/dev-backport", "origin/dev")
open(os.path.join(ws, "src", "dev_fix.py"), "w").write("F = 1\n")
g("add", "-A")
g("-c", "user.name=dev", "-c", "user.email=dev@example.com", "commit", "-qm", "fix: correctif du dev")
g("push", "-q", "-f", "origin", "fix/dev-backport")
mr = dev.open_mr("fix/dev-backport", "dev", "fix: correctif du dev")
owner.p.api("PUT", f"/merge_requests/{mr}", {"add_labels": "backport-3.6"})  # le dev n'a pas le droit Label
dev.checks(mr)
ok, d = dev.merge(mr)
note("Developer : merge du fix vers dev", ok, d)
owner.idle()
bp = [m for m in owner.p.open_bot_mrs() if m["target"] == "release/3.6" and "correctif du dev" in m["title"]]
note("bot : MR de backport vers release/3.6 ouverte", len(bp) == 1, bp)
if bp:
    dev.checks(bp[0]["id"])
    ok, d = dev.merge(bp[0]["id"])
    note("Developer : merge vers release/3.6 refusé (réservé aux Maintainers)", not ok, d)
    ok, d = owner.p.merge(bp[0]["id"])
    note("Maintainer : merge vers release/3.6 autorisé", ok, d)
    owner.idle()

# Un dev tente une action réservée au responsable de release
ok, d = dev.api("POST", "/pipeline", {"ref": "dev", "variables": [{"key": "ACTION", "value": "scan"}]}, ok=False)
if not ok:
    note("Developer : lancement d'un pipeline manuel sur dev refusé par GitLab", True, d)
else:
    pid = d["id"]
    for _ in range(60):
        time.sleep(6)
        _, p = owner.p.api("GET", f"/pipelines/{pid}")
        if p["status"] in ("success", "failed", "canceled", "skipped"):
            break
    _, jobs = owner.p.api("GET", f"/pipelines/{pid}/jobs")
    st = {j["name"]: j["status"] for j in jobs}
    note(f"Developer : pipeline manuel sur dev accepté par GitLab ; action « {st} »",
         st.get("manual") != "success", st)
print(f"\n{owner.ok} OK, {owner.ko} écarts")
