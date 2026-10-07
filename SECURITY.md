# NOAA Cybersecurity Compliance and Security Architecture

This document outlines the security architecture, threat model, and regulatory compliance of the **HPC Workflow Status** monitoring system. It details how the codebase adheres to the **NOAA IT Security Manual**, the **Department of Commerce (DOC) / NOAA Cybersecurity Rules of Behavior**, and the **NIST SP 800-53 (Rev. 5)** security controls for Federal Information Security Modernization Act (FISMA) Moderate and High baseline systems.

---

## 1. System Overview and Core Design Principles

The HPC Workflow Status system provides real-time monitoring and alerting for Rocoto-based High-Performance Computing (HPC) workflows (e.g., Gaea, Hera, Ursa, Orion, Hercules, Derecho) with a GitHub Pages dashboard.

To operate within NOAA's protected HPC boundaries while leveraging GitHub Enterprise Cloud, the system adheres to three foundational architectural principles:

1. **One-Way Egress Status Diode**: The HPC monitoring agent strictly pushes sanitized, read-only cycle and task metrics outbound to dedicated status branches (`status-<cluster>`). No internal execution ports are exposed to the public internet.
2. **Zero Cloud Persistence of Sensitive Data**: Internal HPC absolute filesystem paths, node topologies, and user credentials are **never** committed to Git, stored in Git branches, or saved in public cloud storage.
3. **Mandatory NOAA Enterprise SAML SSO Enforcement**: Any user-initiated configuration change (adding, blind-editing, or removing monitored workflows) must originate from personnel authenticated through NOAA's Identity, Credential, and Access Management (ICAM) system via Single Sign-On (SSO) with Common Access Card (CAC) / PIV credentials.

---

## 2. NIST SP 800-53 Security Control Mapping

| Control Domain | NIST SP 800-53 Controls | System Implementation | Compliance Status |
| :--- | :--- | :--- | :---: |
| **Identification & Authentication** | IA-2, IA-5, IA-8 | NOAA SAML SSO (CAC/PIV) verification via GitHub Enterprise Organization membership API; zero hardcoded credentials. | **COMPLIANT** |
| **Access Control & Least Privilege** | AC-2, AC-3, AC-6, AC-17 | Role-Based Access Control (RBAC) in web UI; file permissions restricted to `chmod 700` / `chmod 600` on HPC nodes. | **COMPLIANT** |
| **Boundary Protection & Anti-Reconnaissance** | SC-7, SC-8, SC-28 | Complete elimination of internal hostnames (`socket.gethostname()`); masking of internal node FQDNs; cluster-level abstraction only. | **COMPLIANT** |
| **System & Information Integrity** | SI-3, SI-4, SI-10 | Strict list-parameterized subprocess execution (zero `shell=True`); email argument injection prevention. | **COMPLIANT** |
| **Data Confidentiality & Path Sandboxing** | AC-4, MP-6, SC-28 | Strict filesystem whitelist (`ALLOWED_SCRATCH_PREFIXES`); symlink canonicalization; blind-edit web interface. | **COMPLIANT** |
| **Resource Management & Denial of Service** | SC-5 | Hard quotas: 3 experiments per user, 60 total cluster experiments, 20 concurrent worker ceiling with sequential modulo batching. | **COMPLIANT** |
| **Privacy & PII Protection** | Privacy Act / OMB M-17-12 | Zero personal email addresses, phone numbers, or employee names; GitHub username attribution only. | **COMPLIANT** |

---

## 3. Detailed Security Architectures and Safeguards

### 3.1. Identification and Authentication (IA-2, IA-8)
* **NOAA SAML SSO Gatekeeper**:
  * In NOAA GitHub Organizations (`noaa-gsl`, `noaa-oar`), access is gated by SAML SSO backed by NOAA ICAM, requiring government-issued CAC/PIV smart cards and PINs.
  * When a scientist submits a configuration request via the web dashboard, the request must include a Personal Access Token (PAT) authorized for the NOAA Organization SAML SSO.
  * The frontend performs a pre-flight verification against `https://api.github.com/user/memberships/orgs/{org}` to confirm active membership before dispatching requests.
* **HPC Agent In-Flight Membership Verification**:
  * Prior to processing any pending configuration request, the HPC Agent executes `verify_noaa_org_membership(author, token)`.
  * The agent calls GitHub's authoritative API (`/orgs/{org}/memberships/{author}`) to verify that the request author is an active, verified member of `noaa-gsl` or `noaa-oar`.
  * Requests authored by unauthorized or non-SSO authenticated accounts are immediately rejected and closed with an audit warning.
* **Zero Hardcoded Credentials**:
  * No tokens, private keys, or passwords exist in the codebase.
  * Tokens on the HPC are loaded strictly via environment variables (`GITHUB_TOKEN`) or a local user file (`~/.config/workflow_status/github_token.txt`) set to permissions `chmod 600`.

### 3.2. Access Control and Least Privilege (AC-3, AC-6)
* **Local State Protection**:
  * Dynamic configurations on the HPC are stored in `~/.config/workflow_status/` with directory mode `0700` (`rwx------`) and file mode `0600` (`rw-------`). Other users on shared multi-tenant cluster filesystems cannot read or modify these files.
* **Role-Based Access Control (RBAC)**:
  * The web dashboard restricts experiment management actions (Edit and Delete) exclusively to the verified experiment owner (`@username`) or designated administrators (`ADMIN_USERS`). Non-owners cannot view or access management buttons.

### 3.3. Boundary Protection and Anti-Reconnaissance (SC-7, SC-28)
* **Host Information Concealment**:
  * Calls to `socket.gethostname()` have been completely removed. Internal compute and login node Fully Qualified Domain Names (FQDNs) (e.g., `fe4.hpc.ncep.noaa.gov`, `ufe04`) are never recorded in status JSON payloads, Git branch names, or logs.
  * The system strictly refers to clusters by logical, high-level identifiers (e.g., `gaeac7`, `ursa`, `hera`, `orion`, `hercules`, `derecho`).
* **Git Status Isolation**:
  * Status updates are pushed directly to dedicated, orphaned branches (`status-<cluster>`) using low-level Git plumbing (`hash-object`, `mktree`, `commit-tree`). The `main` branch and working tree are never modified during monitoring.

### 3.4. Input Validation, Sandboxing, and Command Injection Prevention (SI-10, AC-4)
* **Filesystem Sandbox Whitelist**:
  * All experiment directories submitted via web or configuration are passed through `validate_safe_expdir()`.
  * Directory paths must resolve within authorized, shared scratch and work filesystems:
    * `/scratch` (Ursa, Hera)
    * `/gpfs/f` (Gaea c6/c7)
    * `/work/noaa` (Orion, Hercules)
    * `/glade/work`, `/glade/scratch` (Derecho)
  * Paths are canonicalized using `Path.resolve()` to follow and verify symlinks. Attempts to traverse directories (`../`), access root paths (`/etc`, `/usr`), or escape into private home directories (`/home`) trigger an immediate security exception.
* **Subprocess Parameterization**:
  * All external command executions (`mail`, `scancel`, `rocotostat`, `rocotoboot`, `git`) use explicit list arguments (`subprocess.run(cmd, shell=False)`). No command strings are executed via a shell interpreter, preventing arbitrary shell command injection (CWE-78).
* **Email Recipient Sanitization**:
  * Email recipient lists are parsed through `EMAIL_RE` to enforce valid RFC email format. Arguments starting with `-` are rejected to prevent CLI option injection into the system `mail` utility.

### 3.5. Denial of Service and Resource Throttling (SC-5)
* **Workload and Process Caps**:
  * `MAX_USER_EXPERIMENTS = 3`: Prevents any single user from monopolizing monitoring capacity.
  * `MAX_TOTAL_EXPERIMENTS = 60`: Hard ceiling on the total number of monitored workflows per HPC cluster.
  * `MAX_CONCURRENT_WORKERS = 20`: Prevents fork storms on cluster login and cron nodes.
* **Modulo Partitioning with Sequential Execution**:
  * If the total number of experiments exceeds 20, workloads are partitioned evenly across 20 worker threads using modulo distribution (`worker_batches[idx % 20]`).
  * Experiments assigned to each worker execute sequentially, ensuring peak memory consumption remains below 400 MB, well within Slurm memory allocations for `scrontab` jobs (`#SCRON --mem=8G`).

### 3.6. Privacy and Personally Identifiable Information (PII) Protection
* **Exclusion of Personal Contact Information**:
  * No personal email addresses, phone numbers, or employee identifiers are committed to the repository.
  * All configuration examples and HTML placeholders use fictitious government templates (`first.last@noaa.gov`).
  * Web interfaces display ownership strictly as GitHub handles (`@username`), preventing the harvesting of government email addresses.

---

## 4. Enterprise Deployment Checklist

When deploying to a NOAA production environment:

1. **Repository Ownership**:
   * Host or transfer the repository under the official NOAA Organization (e.g., `github.com/noaa-gsl/workflow_status`).
2. **Access-Controlled Dashboard**:
   * In GitHub Repository Settings -> Pages, select **"Restrict to organization members"**. This ensures the monitoring dashboard is accessible only to personnel authenticated via NOAA SAML SSO.
3. **Cluster Agent Setup**:
   * Ensure user SSH keys are registered on GitHub for automated Git pushing under `scrontab`.
   * If web-based dynamic configuration is utilized, store an SSO-authorized Personal Access Token in `~/.config/workflow_status/github_token.txt` with permissions `chmod 600`.
4. **Third-Party Telemetry**:
   * The optional `healthchecks.io` dead-man's switch is inactive by default (requires `healthchecks_uuid.txt`). If an explicit Authority to Operate (ATO) for `hc-ping.com` is not in place, rely on the built-in GitHub Actions watchdog (`.github/workflows/stale-check.yml`) for staleness alerting.
