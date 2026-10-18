# Security and testing checks

Every pull request and every push to `dev`/`main` runs these workflows on
GitHub-hosted runners with no cloud credentials. `security.yml` also runs weekly,
because new CVEs are published against code that has not changed.

| Kind | Tool | Workflow | Blocks merge when |
|---|---|---|---|
| Unit | pytest + moto (in-process) | `ci.yml` | any test fails |
| Dynamic, process level | real worker subprocesses against a moto **server** | `ci.yml` (`tests/dynamic`) | resume, fencing or SIGTERM-suspend breaks |
| Dynamic, container + Kubernetes | image built, run as a Job on kind against a host emulator | `e2e.yml` | resume across Pod replacement or stale-token rejection breaks |
| Chart | `helm lint`, render assertions, rejection of unsafe values | `ci.yml` | unpinned/foreign image, bad labels, unschedulable barrier |
| SAST | Ruff (`S` = flake8-bandit, `B`, `E`, `F`, `UP`) | `security.yml` | any finding |
| SAST | Bandit (SARIF to code scanning) | `security.yml` | medium or higher severity |
| SAST | CodeQL `security-extended` for Python **and GitHub Actions** | `security.yml` | alerts surface in code scanning |
| SCA | pip-audit on `workloads/requirements.txt` (shipped) and `requirements-dev.txt` | `security.yml` | any known vulnerability in shipped deps |
| SCA / secrets / IaC | Trivy `fs` (vuln, secret, misconfig) | `security.yml` | fixable HIGH/CRITICAL |
| IaC | Trivy `config` on the rendered Helm chart | `security.yml` | MEDIUM+ misconfiguration |
| Image | Trivy `image` | `e2e.yml`, `build-images.yml` | fixable HIGH/CRITICAL; the push is skipped |
| SBOM | Trivy CycloneDX (repo + each pushed image) | `security.yml`, `build-images.yml` | — (artifact) |
| New dependencies | GitHub dependency review | `security.yml` (PR only) | HIGH+ advisory introduced |
| Updates | Dependabot (pip, Docker base image, Actions) | `.github/dependabot.yml` | — |
| Registry drift | `scripts/check_registry.py` | `ci.yml` | any image outside the shared GHCR namespace |

## Trivy installation

`scripts/install_trivy.sh` downloads one Trivy release by exact version and
verifies a SHA-256 committed in the script. The workflows deliberately do not
use `aquasecurity/trivy-action`: its tags and `setup-trivy` were hijacked in the
March 2026 Trivy supply-chain compromise (CVE-2026-33634). Upgrade by changing
the version and checksum together, to a release that is at least two weeks old.

Accepted findings live in `.trivyignore`, each with a reason.

## Running locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                      # unit + dynamic process tests
ruff check . && bandit -c pyproject.toml -r workloads scripts
pip-audit -r workloads/requirements.txt
bash scripts/install_trivy.sh .bin && .bin/trivy fs --ignorefile .trivyignore .
python3 scripts/check_registry.py
```

The kind e2e needs Docker and kind; follow the steps in `.github/workflows/e2e.yml`.

## Repository settings to enable (manual)

* Code scanning shows SARIF uploads; it is available for public repositories.
* Branch protection on `dev`/`main` requiring `CI`, `Security` and `E2E`.
* Dependency graph (required by dependency review and Dependabot).
