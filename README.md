# HPC Workflow Status

A config-driven Python monitoring system for Rocoto-based HPC workflows with a GitHub Pages dashboard, email alerting, and multi-layer failure detection.

## Features

- **Adaptive workflow XML and DB discovery** — automatically discovers unique workflow files (`arps.xml`, `conus3km.xml`, `rrfs.xml`, etc.) and matching databases (`<stem>.db`) directly inside the experiment directory.
- **Zero-template minimalist deployment** — starts with a clean zero-experiment [`config.yml`](config.yml). Monitored experiments can be dynamically added, edited, and removed from the web dashboard by authenticated scientists.
- **Optional untracked [`myexps.yml`](config.yml) overrides** — local static experiments and personal alert recipients can be placed in `myexps.yml` (git-ignored) so local paths never leak into git and `git pull` stays conflict-free.
- **Full `rocotostat` structured parsing** — uses `rocotostat -s` to discover all `Active` cycles + the last $N$ `Done` cycles (works identically for **realtime** and **retrospective** workflows), then parses full task state and cycle wall-clock duration.
- **Dead job detection** — alerts on new `DEAD` jobs with MD5 deduplication (no repeated emails for the same failure).
- **Stall detection** — alerts when no jobs are running/queued/submitting beyond a configurable threshold.
- **Hung job detection** — optionally checks one or more `RUNNING` task types (`fcst`, `jedivar`, etc.) for stale log files, with optional `cancel_and_reboot` auto-remediation.
- **Zero-conflict per-HPC branches (`status-<MACHINE>`)** — each HPC pushes `<exp>.json` directly to its own dedicated branch `status-<MACHINE>` (`https://raw.githubusercontent.com/<owner>/<repo>/status-<machine>/<exp>.json`) without modifying the working tree or `main` branch.
- **Enterprise NOAA Organization SAML SSO & Dynamic Configuration** — allows authenticated scientists in NOAA Organizations (e.g. `noaa-gsl`, `noaa-oar`) to securely add, blind-edit, and remove monitored experiments from the dashboard with strict path sandboxing (`/scratch`, `/gpfs`, `/work`) and user quota controls.
- **Two-layer failure detection**:
  1. **GitHub Actions watchdog** ([`.github/workflows/stale-check.yml`](.github/workflows/stale-check.yml)) — opens a GitHub Issue if any `<exp>.json` on any `status-*` branch goes $>30$ min stale.
  2. **Dashboard UI** ([`docs/index.html`](docs/index.html)) — color-coded staleness indicator visible at a glance.

## Prerequisites

1. **Git SSH access** (`git@github.com:...`) configured on the HPC cluster (with an SSH key that does not prompt for an interactive passphrase when running under `scrontab`).
2. **`pyDAmonitor` Conda environment** (`Miniforge3/envs/pyDAmonitor/bin/python3`), invoked directly by [`run.sh`](run.sh) without needing `conda activate`.
3. **Rocoto** module on the target HPC cluster (automatically loaded by [`run.sh`](run.sh)).
4. *(Optional for dynamic web config)* **GitHub Personal Access Token** authorized for NOAA SAML SSO saved in `~/.config/workflow_status/github_token.txt` (`chmod 600`) or exported as `GITHUB_TOKEN`.

## Supported HPC Systems (`MACHINE` values)

| `MACHINE` | Status Branch | Rocoto Module Path | `pyDAmonitor` Base Directory (`Miniforge3`) |
| :--- | :--- | :--- | :--- |
| **`gaeac6`** | `status-gaeac6` | `/gpfs/f6/arfs-gsl/world-shared/gge/rocoto/modulefiles` | `/gpfs/f6/bil-fire10-oar/world-shared/gge/Miniforge3` |
| **`gaeac7`** | `status-gaeac7` | `/gpfs/f7/arfs-gsl/world-shared/gge/rocoto/modulefiles` | `/gpfs/f7/wrfruc/world-shared/gge/Miniforge3` |
| **`hera`** | `status-hera` | `/scratch4/BMC/zrtrr/gge/rocoto_hera/modulefiles` | `/scratch3/BMC/wrfruc/hera/Miniforge3` |
| **`ursa`** | `status-ursa` | `/scratch4/BMC/zrtrr/gge/rocoto/modulefiles` | `/scratch3/BMC/wrfruc/gge/Miniforge3` |
| **`orion`** | `status-orion` | `/work/noaa/zrtrr/gge/rocoto/modulefiles` | `/work/noaa/zrtrr/gge/Miniforge3` |
| **`hercules`** | `status-hercules` | `/work/noaa/zrtrr/gge/hercules/rocoto/modulefiles` | `/work/noaa/zrtrr/gge/hercules/Miniforge3` |
| **`derecho`** | `status-derecho` | `/glade/work/geguo/rocoto/modulefiles` | `/glade/work/geguo/Miniforge3` |

## Quick Start

### 1. Clone the Repo via SSH on HPC

```bash
git clone git@github.com:noaa-gsl/workflow_status.git  # or your fork: git@github.com:spanNOAA/workflow_status.git
cd workflow_status
```

### 2. Configure GitHub Token for Dynamic Web Add/Edit (Recommended)

To enable automatic ingestion of experiments requested through the web dashboard, store your GitHub PAT (with `repo` scope and NOAA SAML SSO authorization):

```bash
mkdir -p ~/.config/workflow_status
echo "ghp_yourTokenHere" > ~/.config/workflow_status/github_token.txt
chmod 600 ~/.config/workflow_status/github_token.txt
```

### 3. Adaptive Discovery & Local Config

By default, `config.yml` starts with `experiments: []`. All monitored experiments can be added directly via the web dashboard.
- Workflow XML and database files (e.g. `arps.xml`/`arps.db`, `conus3km.xml`/`conus3km.db`, `rrfs.xml`/`rrfs.db`) are automatically detected in each experiment directory.
- If you wish to configure local static experiments or personal alert emails without the dashboard, you can optionally copy `config.yml` to `myexps.yml` (git-ignored) and edit it:

```bash
# Optional: only if you prefer local static config files
cp config.yml myexps.yml
```

### 4. Test (`--dry-run`)

Test execution using the cluster's identifier (`gaeac6`, `gaeac7`, `hera`, `ursa`, etc.):

```bash
# Dry run with default config (myexps.yml if present, otherwise config.yml)
MACHINE=gaeac7 ./run.sh --dry-run
```

### 5. Add to `scrontab`

Open your user scrontab (`scrontab -e`):

```bash
# Example for Gaea c7:
#SCRON --partition=cron_c7
#SCRON --account=arfs-gsl
#SCRON --time=00:10:00
#SCRON --mem=8G
#SCRON --dependency=singleton
#SCRON --job-name=workflow_status
#SCRON --output=/dev/null
*/10 * * * * MACHINE=gaeac7 /path/to/workflow_status/run.sh

# Example for Hera / Ursa:
#SCRON --time=00:10:00
#SCRON --mem=8G
#SCRON --dependency=singleton
#SCRON --job-name=workflow_status
#SCRON --output=/dev/null
*/10 * * * * MACHINE=hera /path/to/workflow_status/run.sh
```

## Repository Structure

```
workflow_status/
├── README.md
├── config.yml                   # Example config template (copy to myexps.yml)
├── myexps.yml                   # Untracked local user config (git-ignored)
├── run.sh                       # Thin launcher (uses MACHINE to set Rocoto + pyDAmonitor python3)
├── workflow_status.py           # Core monitoring engine (pushes <exp>.json to branch status-<MACHINE>)
├── docs/                        # GitHub Pages dashboard (reads status-* branches across all HPCs)
│   └── index.html
└── .github/
    └── workflows/
        └── stale-check.yml      # GitHub Actions 30-min staleness watchdog across status-* branches
```
