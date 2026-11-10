# gitflow-test — bac à sable

Application factice pour valider le gitflow « une branche par mineure + backport par label »
(voir `SYNTHESE-CORRECTIFS.md` dans le dossier parent).

| Chemin | Rôle |
| --- | --- |
| `package.json`, `package-lock.json`, `pyproject.toml`, `chart/values.yaml` | fichiers de version réécrits par le bot |
| `src/app.py`, `tests/test_app.py` | appli et tests factices (un fichier `FAIL` à la racine rend la CI rouge) |
| `bot/bot.py` | le bot : bump, coupe, backport, livraison, gel, garde-fous (Python standard) |
| `tests/scenarios.py` | rejoue les scénarios en local, sans plateforme : `python3 tests/scenarios.py` |
| `.gitlab-ci.yml` | CI/CD GitLab (variable `BOT_TOKEN` requise) |
| `.github/workflows/` | CI/CD GitHub (secret `BOT_SSH_KEY` = deploy key en écriture) |
