#!/usr/bin/env python3
"""
workflow_status.py — Unified HPC Rocoto Workflow Monitor

Usage:
  MACHINE=gaeac7 ./run.sh [myexps.yml] [--dry-run] [--verbose]

Features:
  - Reads `myexps.yml` (default: `<repo_root>/myexps.yml`, copied from template `config.yml`)
    with a `common:` section and an `experiments:` list, deep-merging each experiment's overrides on top of `common:`
  - Queries `rocotostat -s` and `rocotostat -c` for both realtime & retrospective runs
  - Detects new DEAD jobs (MD5-deduplicated), workflow stalls, and hung jobs (log staleness)
  - Sends email alerts via `mail` only on state transitions
  - Saves `<exp>.json` (with rolling 7-day `history`) in `.state/` and pushes directly
    to branch `status-<MACHINE>` (`https://raw.githubusercontent.com/<owner>/<repo>/status-<machine>/<exp>.json`)
    without modifying the working tree or `main` branch
"""

import argparse
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

REPO_ROOT = Path(os.path.abspath(__file__)).parent
CYCLE_RE = re.compile(r"^\d{12}$")
EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

ALLOWED_SCRATCH_PREFIXES = (
    "/scratch",
    "/gpfs/f",
    "/work/noaa",
    "/glade/work",
    "/glade/scratch",
)

MAX_TOTAL_EXPERIMENTS = 60
MAX_USER_EXPERIMENTS = 3
MAX_CONCURRENT_WORKERS = 20


def validate_safe_expdir(expdir_str: str) -> Path:
    """Ensure experiment directory is confined to authorized scratch/work filesystems (Sandbox Whitelist)."""
    p = Path(expdir_str).expanduser().resolve()
    resolved_str = str(p)
    if not any(resolved_str.startswith(prefix) for prefix in ALLOWED_SCRATCH_PREFIXES):
        raise ValueError(
            f"Security Violation: Experiment directory '{expdir_str}' resolves to '{resolved_str}', "
            f"which is outside authorized scratch/work filesystems: {ALLOWED_SCRATCH_PREFIXES}"
        )
    return p


def load_dynamic_experiments(machine: str) -> List[Dict[str, Any]]:
    """Load dynamic experiments from Scheme A local secure state (~/.config/workflow_status/<machine>_dynamic_exps.json)."""
    cfg_dir = Path.home() / ".config" / "workflow_status"
    state_file = cfg_dir / f"{machine}_dynamic_exps.json"
    if not state_file.is_file():
        return []
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        raw_list = data if isinstance(data, list) else (data.get("experiments", []) if isinstance(data, dict) else [])

        # Enforce quota: maximum MAX_USER_EXPERIMENTS (3) per owner
        user_counts: Dict[str, int] = {}
        filtered_list: List[Dict[str, Any]] = []
        for exp in raw_list:
            owner = exp.get("owner", "unknown")
            count = user_counts.get(owner, 0)
            if count < MAX_USER_EXPERIMENTS:
                user_counts[owner] = count + 1
                filtered_list.append(exp)
            else:
                logging.warning("User '%s' exceeded quota of %d experiments. Skipping '%s'.", owner, MAX_USER_EXPERIMENTS, exp.get("name"))
        return filtered_list
    except Exception as exc:
        logging.warning("Failed to read dynamic experiments from %s: %s", state_file, exc)
    return []


def save_dynamic_experiments(machine: str, experiments: List[Dict[str, Any]]) -> None:
    """Save dynamic experiments to Scheme A local secure state (chmod 600)."""
    cfg_dir = Path.home() / ".config" / "workflow_status"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    try:
        cfg_dir.chmod(0o700)
    except Exception:
        pass
    state_file = cfg_dir / f"{machine}_dynamic_exps.json"
    tmp_file = state_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(experiments, indent=2) + "\n", encoding="utf-8")
    try:
        tmp_file.chmod(0o600)
    except Exception:
        pass
    tmp_file.replace(state_file)


ALLOWED_ORGS = ("noaa-gsl", "noaa-oar")
_MEMBER_CACHE: Dict[Tuple[str, str], Tuple[bool, float]] = {}
CACHE_TTL_SEC = 3600


def get_github_token() -> Optional[str]:
    """Retrieve GitHub token from GITHUB_TOKEN environment variable or ~/.config/workflow_status/github_token.txt."""
    env_token = os.environ.get("GITHUB_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()
    token_file = Path.home() / ".config" / "workflow_status" / "github_token.txt"
    if token_file.is_file():
        try:
            val = token_file.read_text(encoding="utf-8").strip()
            if val:
                return val
        except Exception:
            pass
    return None


def get_repo_slug() -> str:
    """Determine repository owner/repo slug from git remote or default to noaa-gsl/workflow_status."""
    try:
        rem = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if rem.returncode == 0 and rem.stdout:
            raw = rem.stdout.strip()
            m = re.search(r"github\.com[:/]([^/]+/[^/\.]+)", raw)
            if m:
                return m.group(1).rstrip(".git")
    except Exception:
        pass
    return "noaa-gsl/workflow_status"


def verify_noaa_org_membership(
    username: str,
    token: Optional[str] = None,
    allowed_orgs: Optional[List[str]] = None,
) -> bool:
    """
    Verify whether `username` is an active, verified member of an authorized NOAA GitHub Organization.
    Ensures that commands originate exclusively from personnel authenticated via NOAA SAML SSO (CAC/PIV).
    """
    if not username:
        return False
    clean_user = username.strip().lstrip("@")
    target_orgs = allowed_orgs or list(ALLOWED_ORGS)
    now = time.time()

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "NOAA-HPC-Workflow-Agent",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for org in target_orgs:
        cache_key = (org.lower(), clean_user.lower())
        if cache_key in _MEMBER_CACHE:
            is_valid, ts = _MEMBER_CACHE[cache_key]
            if now - ts < CACHE_TTL_SEC:
                if is_valid:
                    return True
                continue

        # Check membership details
        url = f"https://api.github.com/orgs/{org}/memberships/{clean_user}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("state") == "active":
                        _MEMBER_CACHE[cache_key] = (True, now)
                        logging.info("Security Verified: User '%s' is an active member of '%s'.", clean_user, org)
                        return True
        except urllib.error.HTTPError as err:
            if err.code == 404:
                _MEMBER_CACHE[cache_key] = (False, now)
            elif err.code in (401, 403):
                # Fallback to public member check if token lacks read:org scope
                pub_url = f"https://api.github.com/orgs/{org}/members/{clean_user}"
                pub_req = urllib.request.Request(pub_url, headers=headers)
                try:
                    with urllib.request.urlopen(pub_req, timeout=10) as pub_resp:
                        if pub_resp.status in (200, 204):
                            _MEMBER_CACHE[cache_key] = (True, now)
                            logging.info("Security Verified: User '%s' is a public member of '%s'.", clean_user, org)
                            return True
                except Exception:
                    pass
            else:
                logging.warning("GitHub API error checking org membership for %s on %s: %s", clean_user, org, err)
        except Exception as exc:
            logging.error("Network error during org membership verification: %s", exc)

    logging.warning("SECURITY REJECTION: User '%s' is NOT verified as an active member of %s.", clean_user, target_orgs)
    return False


def close_issue(repo_slug: str, issue_num: int, token: str, comment: Optional[str] = None) -> None:
    """Close an issue on GitHub and optionally post a processing comment."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "NOAA-HPC-Workflow-Agent",
    }
    if comment:
        c_url = f"https://api.github.com/repos/{repo_slug}/issues/{issue_num}/comments"
        try:
            c_payload = json.dumps({"body": comment}).encode("utf-8")
            c_req = urllib.request.Request(c_url, headers=headers, data=c_payload, method="POST")
            urllib.request.urlopen(c_req, timeout=10)
        except Exception:
            pass

    url = f"https://api.github.com/repos/{repo_slug}/issues/{issue_num}"
    try:
        payload = json.dumps({"state": "closed"}).encode("utf-8")
        req = urllib.request.Request(url, headers=headers, data=payload, method="PATCH")
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        logging.warning("Failed to close issue #%s: %s", issue_num, exc)


def sync_pending_instructions(
    machine: str,
    allowed_orgs: Optional[List[str]] = None,
) -> None:
    """
    Fetch pending experiment configuration requests from the GitHub repository,
    verify the author's NOAA Organization membership (SAML SSO), validate safe paths
    and quotas, update the local secure dynamic state, and close processed issues.
    """
    token = get_github_token()
    if not token:
        return

    repo_slug = get_repo_slug()
    url = f"https://api.github.com/repos/{repo_slug}/issues?labels=exp-config,{machine}&state=open"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "NOAA-HPC-Workflow-Agent",
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return
            issues = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logging.debug("No pending instructions fetched from %s: %s", repo_slug, exc)
        return

    if not issues or not isinstance(issues, list):
        return

    logging.info("Found %d pending experiment configuration requests on GitHub (%s).", len(issues), repo_slug)
    current_exps = load_dynamic_experiments(machine)
    modified = False

    for issue in issues:
        issue_num = issue.get("number")
        author = issue.get("user", {}).get("login", "")
        if not author:
            continue

        repo_owner = repo_slug.split("/")[0] if "/" in repo_slug else ""
        is_repo_admin = bool(repo_owner and author.lower() == repo_owner.lower())

        # 1. Verify author membership in NOAA GSL / OAR organization (repo admin is implicitly authorized)
        if not is_repo_admin and not verify_noaa_org_membership(author, token, allowed_orgs):
            logging.warning("Rejecting request #%s from non-org user '%s'", issue_num, author)
            close_issue(repo_slug, issue_num, token, comment="Rejected: Author is not an active member of authorized NOAA organizations.")
            continue

        # 2. Parse payload from issue body
        body_str = issue.get("body", "") or ""
        try:
            json_match = re.search(r"```json\s*(\{.*?\})\s*```", body_str, re.DOTALL)
            raw_json = json_match.group(1) if json_match else body_str.strip()
            payload = json.loads(raw_json)
        except Exception as exc:
            logging.warning("Failed to parse JSON body for issue #%s: %s", issue_num, exc)
            close_issue(repo_slug, issue_num, token, comment=f"Rejected: Invalid payload format ({exc}).")
            continue

        action = str(payload.get("action", "add")).lower()
        exp_name = str(payload.get("name", "")).strip()
        exp_cluster = str(payload.get("cluster", machine)).strip()

        if exp_cluster != machine or not exp_name:
            continue

        if action == "delete":
            # Verify caller owns the experiment or is an authorized admin
            orig_len = len(current_exps)
            current_exps = [e for e in current_exps if not (e.get("name") == exp_name and (e.get("owner") == author or is_repo_admin))]
            if len(current_exps) < orig_len:
                modified = True
                logging.info("Deleted experiment '%s' requested by @%s (Issue #%s)", exp_name, author, issue_num)
            close_issue(repo_slug, issue_num, token, comment="Processed: Experiment removed from monitoring.")

        elif action == "add":
            expdir_str = str(payload.get("expdir", "")).strip()
            email_str = str(payload.get("email", "")).strip()

            try:
                safe_dir = validate_safe_expdir(expdir_str)
            except Exception as exc:
                logging.error("Path validation failed for issue #%s: %s", issue_num, exc)
                close_issue(repo_slug, issue_num, token, comment=f"Rejected: Path validation failed ({exc}).")
                continue

            # Enforce quota for this author
            user_count = sum(1 for e in current_exps if e.get("owner") == author)
            if user_count >= MAX_USER_EXPERIMENTS:
                logging.warning("User '%s' quota exceeded for issue #%s", author, issue_num)
                close_issue(repo_slug, issue_num, token, comment=f"Rejected: Quota exceeded (max {MAX_USER_EXPERIMENTS} experiments per user).")
                continue

            new_entry: Dict[str, Any] = {
                "name": exp_name,
                "cluster": machine,
                "expdir": str(safe_dir),
                "owner": author,
            }
            if email_str and EMAIL_RE.match(email_str) and not email_str.startswith("-"):
                new_entry["recipients"] = [email_str]

            # Upsert entry
            current_exps = [e for e in current_exps if e.get("name") != exp_name]
            current_exps.append(new_entry)
            modified = True
            logging.info("Added experiment '%s' requested by @%s (Issue #%s)", exp_name, author, issue_num)
            close_issue(repo_slug, issue_num, token, comment="Processed: Experiment added to monitoring.")

    if modified:
        save_dynamic_experiments(machine, current_exps)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge `override` dict on top of `base` dict."""
    result = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def parse_rocoto_time(ts_str: Optional[str]) -> Optional[dt.datetime]:
    """Parse Rocoto summary timestamp like 'Oct 03 2026 19:50:08'."""
    if not ts_str or ts_str == "-":
        return None
    for fmt in ("%b %d %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(ts_str.strip(), fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def compute_wall_time_min(activated: Optional[str], deactivated: Optional[str]) -> Optional[float]:
    t1 = parse_rocoto_time(activated)
    t2 = parse_rocoto_time(deactivated)
    if t1 and t2 and t2 >= t1:
        return round((t2 - t1).total_seconds() / 60.0, 1)
    return None


def run_rocoto_cmd(cmd: List[str], expdir: Path) -> str:
    """Run a rocoto command inside expdir (rocoto module is loaded by run.sh)."""
    proc = subprocess.run(
        cmd,
        cwd=str(expdir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    for line in combined.splitlines():
        if "Error:" in line or "error:" in line:
            logging.warning("rocotostat error in %s: %s", expdir, line.strip())
    return proc.stdout


def parse_rocotostat(expdir: Path, xml: str, db: str, lookback: int = 6) -> List[Dict[str, Any]]:
    """
    1. Run `rocotostat -w <xml> -d <db> -s` to get all activated cycles & timestamps.
    2. Exclude 'Inactive' future cycles and select the last `lookback` cycles (works for realtime & retro).
    3. Run `rocotostat -w <xml> -d <db> -c <selected_cycles>` and parse tasks.
    """
    summary_out = run_rocoto_cmd(["rocotostat", "-w", xml, "-d", db, "-s"], expdir)
    summary_map: Dict[str, Dict[str, Any]] = {}
    cycle_order: List[str] = []

    for raw_line in summary_out.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("CYCLE") or "::" in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        cdate = parts[0]
        if not CYCLE_RE.match(cdate) or int(cdate) >= 210000000000:
            continue
        cstate = parts[1]
        if cstate.lower() == "inactive":
            continue
        activated = " ".join(parts[2:6]) if len(parts) >= 6 else None
        deactivated = None
        if len(parts) >= 10 and parts[6] != "-":
            deactivated = " ".join(parts[6:10])

        summary_map[cdate] = {
            "cdate": cdate,
            "cycle_state": cstate,
            "activated": activated,
            "deactivated": deactivated,
            "wall_time_min": compute_wall_time_min(activated, deactivated),
        }
        cycle_order.append(cdate)

    if not cycle_order:
        return []

    selected_cycles = cycle_order[-lookback:] if lookback > 0 else cycle_order
    if not selected_cycles:
        return []

    cycle_arg = ",".join(selected_cycles)
    tasks_out = run_rocoto_cmd(["rocotostat", "-w", xml, "-d", db, "-c", cycle_arg], expdir)

    cycles_tasks: Dict[str, List[Dict[str, Any]]] = {c: [] for c in selected_cycles}
    for raw_line in tasks_out.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("CYCLE") or line.startswith("=") or "::" in line:
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        cdate, task, jobid, state, exit_status, tries, duration = parts[:7]
        if not CYCLE_RE.match(cdate):
            continue

        if state == "-":
            state = "WAITING"

        task_obj = {
            "name": task,
            "state": state,
            "jobid": None if jobid == "-" else jobid,
            "exit_status": None if exit_status == "-" else int(float(exit_status)),
            "tries": None if tries == "-" else int(float(tries)),
            "duration": None if duration == "-" else float(duration),
        }
        cycles_tasks.setdefault(cdate, []).append(task_obj)

    result: List[Dict[str, Any]] = []
    for cdate in selected_cycles:
        s = summary_map.get(cdate, {})
        result.append(
            {
                "cdate": cdate,
                "cycle_state": s.get("cycle_state", "Unknown"),
                "activated": s.get("activated"),
                "deactivated": s.get("deactivated"),
                "wall_time_min": s.get("wall_time_min"),
                "tasks": cycles_tasks.get(cdate, []),
            }
        )
    return result


def build_status_dict(
    experiment: str,
    cluster: str,
    cycles: List[Dict[str, Any]],
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    counts = {
        "total_cycles": len(cycles),
        "active_cycles": sum(1 for c in cycles if c.get("cycle_state") == "Active"),
        "done_cycles": sum(1 for c in cycles if c.get("cycle_state") == "Done"),
        "total_tasks": 0,
        "succeeded": 0,
        "running": 0,
        "queued": 0,
        "submitting": 0,
        "waiting": 0,
        "expired": 0,
        "dead": 0,
        "other": 0,
    }
    for c in cycles:
        for t in c.get("tasks", []):
            counts["total_tasks"] += 1
            st = (t.get("state") or "").upper()
            if st == "SUCCEEDED":
                counts["succeeded"] += 1
            elif st == "RUNNING":
                counts["running"] += 1
            elif st == "QUEUED":
                counts["queued"] += 1
            elif st == "SUBMITTING":
                counts["submitting"] += 1
            elif st == "WAITING":
                counts["waiting"] += 1
            elif st == "EXPIRED":
                counts["expired"] += 1
            elif st in ("DEAD", "FAILED"):
                counts["dead"] += 1
            else:
                counts["other"] += 1

    status_dict: Dict[str, Any] = {
        "experiment": experiment,
        "cluster": cluster,
        "updated_at": utc_now_iso(),
        "cycles": cycles,
        "summary": counts,
        "alerts": {
            "dead_jobs": [],
            "stall": False,
            "stall_since": None,
            "hung_jobs": [],
        },
    }
    if owner:
        status_dict["owner"] = owner
    return status_dict


def load_state(state_file: Path) -> Dict[str, Any]:
    if state_file.is_file():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            pass
    return {
        "last_check": None,
        "dead_jobs_hash": "",
        "dead_jobs_list": [],
        "stall_since": None,
        "stall_alerted": False,
        "hung_alerted": {},
    }


def save_state(state_file: Path, state: Dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(state_file)


def parse_recipients(raw_recip: Any) -> List[str]:
    raw_list: List[str] = []
    if isinstance(raw_recip, list):
        for item in raw_recip:
            raw_list.extend(re.split(r"[\s,]+", str(item).strip()))
    elif isinstance(raw_recip, str):
        raw_list = re.split(r"[\s,]+", raw_recip.strip())

    clean: List[str] = []
    for r in raw_list:
        r = r.strip()
        if r and not r.startswith("-") and EMAIL_RE.match(r):
            clean.append(r)
        elif r:
            logging.warning("Ignoring invalid or suspicious recipient email: %s", r)
    return clean


def send_email(subject: str, body: str, recipients: List[str], dry_run: bool) -> None:
    if not recipients:
        logging.warning("No recipients configured for alert: %s", subject)
        return
    if dry_run:
        logging.info("[DRY-RUN] Would send email '%s' to %s", subject, ", ".join(recipients))
        return
    try:
        subprocess.run(
            ["mail", "-s", subject] + recipients,
            input=body,
            text=True,
            check=False,
            timeout=30,
        )
        logging.info("Sent alert email '%s' to %s", subject, ", ".join(recipients))
    except Exception as exc:
        logging.error("Failed to send email '%s': %s", subject, exc)


def check_dead_jobs(status: Dict[str, Any], state: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    dead_list: List[Dict[str, Any]] = []
    canonical_lines: List[str] = []

    for c in status.get("cycles", []):
        cdate = c.get("cdate", "")
        for t in c.get("tasks", []):
            if (t.get("state") or "").upper() in ("DEAD", "FAILED"):
                dead_list.append(
                    {
                        "cycle": cdate,
                        "task": t.get("name"),
                        "tries": t.get("tries"),
                        "jobid": t.get("jobid"),
                        "exit_status": t.get("exit_status"),
                    }
                )
                canonical_lines.append(f"{cdate}|{t.get('name')}|{t.get('tries')}")

    status["alerts"]["dead_jobs"] = dead_list

    if not canonical_lines:
        state["dead_jobs_hash"] = ""
        state["dead_jobs_list"] = []
        return False, []

    canonical_lines.sort()
    current_hash = hashlib.md5("\n".join(canonical_lines).encode("utf-8")).hexdigest()
    saved_hash = state.get("dead_jobs_hash", "")

    if current_hash != saved_hash:
        state["dead_jobs_hash"] = current_hash
        state["dead_jobs_list"] = canonical_lines
        return True, dead_list

    return False, dead_list


def is_retro_all_done(status: Dict[str, Any]) -> bool:
    """Return True if this is a retro workflow where all cycles are Done."""
    cycles = status.get("cycles", [])
    if not cycles:
        return False
    summary = status.get("summary", {})
    total_cycles = summary.get("total_cycles", len(cycles))
    done_cycles = summary.get("done_cycles", 0)
    if total_cycles == 0 or done_cycles < total_cycles:
        return False
    latest_cdate = max((c.get("cdate", "") for c in cycles), default="")
    try:
        cyc_dt = dt.datetime.strptime(latest_cdate[:12], "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
        age_hours = (dt.datetime.now(dt.timezone.utc) - cyc_dt).total_seconds() / 3600.0
        return age_hours > 48.0
    except Exception:
        return False


def check_stall(
    status: Dict[str, Any], state: Dict[str, Any], threshold_sec: int
) -> Tuple[bool, int]:
    summary = status.get("summary", {})
    active_jobs = (
        summary.get("running", 0)
        + summary.get("queued", 0)
        + summary.get("submitting", 0)
    )
    now = int(time.time())

    # Exclude completed retro workflows where all cycles are Done
    if is_retro_all_done(status):
        state["stall_since"] = None
        state["stall_alerted"] = False
        status["alerts"]["stall"] = False
        status["alerts"]["stall_since"] = None
        return False, 0

    if active_jobs == 0:
        stall_since = state.get("stall_since")
        if not stall_since:
            state["stall_since"] = now
            state["stall_alerted"] = False
            status["alerts"]["stall"] = False
            status["alerts"]["stall_since"] = None
            return False, 0

        duration = now - int(stall_since)
        if duration > threshold_sec:
            status["alerts"]["stall"] = True
            status["alerts"]["stall_since"] = int(stall_since)
            if not state.get("stall_alerted", False):
                state["stall_alerted"] = True
                return True, duration
        return False, duration
    else:
        state["stall_since"] = None
        state["stall_alerted"] = False
        status["alerts"]["stall"] = False
        status["alerts"]["stall_since"] = None
        return False, 0


def check_hung_jobs(
    status: Dict[str, Any],
    expdir: Path,
    hung_cfg: Dict[str, Any],
    xml: str,
    db: str,
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Check RUNNING jobs against configured log file staleness rules."""
    rules: List[Dict[str, Any]] = []
    if isinstance(hung_cfg.get("tasks"), list):
        rules = hung_cfg["tasks"]
    elif hung_cfg.get("task") and hung_cfg.get("log_pattern"):
        rules = [
            {
                "name": hung_cfg["task"],
                "log_pattern": hung_cfg["log_pattern"],
                "max_idle_sec": int(hung_cfg.get("max_idle_sec", 1200)),
            }
        ]

    if not rules:
        status["alerts"]["hung_jobs"] = []
        return []

    action = hung_cfg.get("action", "alert")
    now = int(time.time())
    hung_found: List[Dict[str, Any]] = []

    for rule in rules:
        task_name = rule.get("name", "fcst")
        pattern = rule.get("log_pattern", "")
        max_idle = int(rule.get("max_idle_sec", 1200))
        if not pattern:
            continue

        for c in status.get("cycles", []):
            cdate = c.get("cdate", "")
            pdy = cdate[:8]
            cyc = cdate[8:10]
            for t in c.get("tasks", []):
                if t.get("name") == task_name and (t.get("state") or "").upper() == "RUNNING":
                    log_path_str = (
                        pattern.replace("{workdir}", str(expdir))
                        .replace("{expdir}", str(expdir))
                        .replace("{cdate}", cdate)
                        .replace("{PDY}", pdy)
                        .replace("{cyc}", cyc)
                    )
                    log_path = Path(log_path_str)
                    if log_path.is_file():
                        mtime = int(log_path.stat().st_mtime)
                        idle_sec = now - mtime
                        if idle_sec > max_idle:
                            jobid = t.get("jobid")
                            hung_item = {
                                "cycle": cdate,
                                "task": task_name,
                                "jobid": jobid,
                                "idle_sec": idle_sec,
                            }
                            hung_found.append(hung_item)
                            if action == "cancel_and_reboot" and not dry_run and jobid:
                                logging.info("Auto-remediating hung job %s (%s %s)", jobid, cdate, task_name)
                                subprocess.run(["scancel", str(jobid)], check=False, timeout=15)
                                time.sleep(5)
                                run_rocoto_cmd(
                                    ["rocotoboot", "-w", xml, "-d", db, "-c", cdate, "-t", task_name],
                                    expdir,
                                )

    status["alerts"]["hung_jobs"] = hung_found
    return hung_found


def write_status_json(status: Dict[str, Any], status_file: Path) -> None:
    """Atomically write `.state/<exp>.json` to disk."""
    status_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = status_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2) + "\n")
    tmp.replace(status_file)


def get_status_branch(cluster: Optional[str] = None) -> str:
    machine = cluster or os.environ.get("MACHINE") or "unknown"
    return f"status-{machine}"


def git_push_status_branch(repo_root: Path, updated_files: List[Path], branch: str, dry_run: bool) -> bool:
    """
    Build a standalone commit containing <exp>.json at the root of `branch`
    using git plumbing (hash-object -> mktree -> commit-tree) and push it to
    `origin <commit_sha>:refs/heads/<branch>`.
    Never modifies the working tree, index, or current branch (`main`).
    """
    if not updated_files:
        return True

    names = [f.name for f in updated_files]
    if dry_run:
        logging.info("[DRY-RUN] Would push to branch '%s': %s", branch, ", ".join(names))
        return True

    existing_blobs: Dict[str, str] = {}
    for f in updated_files:
        ho = subprocess.run(
            ["git", "hash-object", "-w", str(f)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ho.returncode != 0:
            logging.error("git hash-object failed for %s: %s", f.name, ho.stderr.strip())
            return False
        existing_blobs[f.name] = ho.stdout.strip()

    # Also write _index.json listing all <exp>.json files on this branch so the dashboard
    # can discover experiments via raw.githubusercontent.com without hitting api.github.com rate limits
    index_payload = json.dumps({"files": sorted(existing_blobs.keys()), "updated_at": utc_now_iso()}, indent=2) + "\n"
    ho_idx = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo_root),
        input=index_payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if ho_idx.returncode == 0:
        existing_blobs["_index.json"] = ho_idx.stdout.strip()

    tree_lines = [
        f"100644 blob {sha}\t{fname}"
        for fname, sha in sorted(existing_blobs.items())
    ]
    mktree = subprocess.run(
        ["git", "mktree"],
        cwd=str(repo_root),
        input="\n".join(tree_lines) + "\n",
        capture_output=True,
        text=True,
        timeout=10,
    )
    if mktree.returncode != 0:
        logging.error("git mktree failed: %s", mktree.stderr.strip())
        return False
    tree_sha = mktree.stdout.strip()

    msg = f"status update ({branch}) {utc_now_iso()}"
    ct = subprocess.run(
        ["git", "commit-tree", tree_sha, "-m", msg],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if ct.returncode != 0:
        logging.error("git commit-tree failed: %s", ct.stderr.strip())
        return False
    commit_sha = ct.stdout.strip()

    push_proc = subprocess.run(
        ["git", "push", "--force", "origin", f"{commit_sha}:refs/heads/{branch}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if push_proc.returncode == 0:
        logging.info("Pushed %s to origin/%s", ", ".join(names), branch)
        return True

    logging.error("Failed to push to origin/%s: %s", branch, push_proc.stderr.strip())
    return False


def process_experiment(
    exp_cfg: Dict[str, Any],
    state_dir: Path,
    dry_run: bool,
) -> Optional[Path]:
    exp_name = exp_cfg.get("name")
    cluster = exp_cfg.get("cluster") or os.environ.get("MACHINE") or "unknown"
    expdir_str = exp_cfg.get("expdir")
    xml = exp_cfg.get("workflow_xml", "rrfs.xml")
    db = exp_cfg.get("workflow_db", "rrfs.db")

    if not exp_name or not expdir_str:
        logging.error("Missing required experiment fields (name, expdir): %s", exp_cfg)
        return None

    try:
        expdir = validate_safe_expdir(expdir_str)
    except Exception as exc:
        logging.error("Security validation failed for %s: %s", exp_name, exc)
        return None

    logging.info("Processing experiment: %s on %s (%s)", exp_name, cluster, expdir)

    if not expdir.is_dir():
        logging.warning("Experiment directory not accessible on %s: %s — skipping", cluster, expdir)
        return None

    lookback = int(exp_cfg.get("lookback_cycles", 72))
    recipients = parse_recipients(exp_cfg.get("recipients", []))
    subject_prefix = exp_cfg.get("subject_prefix", exp_name)

    checks_cfg = exp_cfg.get("checks", {})
    dead_cfg = checks_cfg.get("dead_jobs", {})
    stall_cfg = checks_cfg.get("stall", {})
    hung_cfg = checks_cfg.get("hung_jobs", {})

    status_file = state_dir / f"{exp_name}.json"
    state_file = state_dir / f"{exp_name}_{cluster}_state.json"
    legacy_state_file = state_dir.parent / f"{exp_name}_{cluster}_state.json"
    state = load_state(state_file if state_file.is_file() else legacy_state_file)

    # 1. Parse rocotostat
    cycles = parse_rocotostat(expdir, xml, db, lookback)
    owner = exp_cfg.get("owner")
    status = build_status_dict(exp_name, cluster, cycles, owner=owner)
    default_exp = str(exp_cfg.get("default_exp", "") or "").strip()
    if exp_cfg.get("default") or default_exp in (f"{cluster}/{exp_name}", exp_name):
        status["default"] = True
    if default_exp:
        status["default_exp"] = default_exp

    # 2. Dead job check
    if dead_cfg.get("enabled", True):
        new_dead, dead_list = check_dead_jobs(status, state)
        if new_dead:
            logging.warning("New DEAD job(s) in %s: %s", exp_name, dead_list)
            lines = [
                f"⚠️  Dead job(s) detected in {exp_name} on {cluster}",
                f"Time: {status['updated_at']}",
                "",
                "Dead jobs:",
                "──────────────────────────────────────────",
            ]
            for d in dead_list:
                lines.append(
                    f"  Cycle: {d['cycle']}  Task: {d['task']}  JobID: {d['jobid']}  Exit: {d['exit_status']}  Tries: {d['tries']}"
                )
            send_email(f"{subject_prefix}: dead job(s)", "\n".join(lines), recipients, dry_run)

    # 3. Stall check
    if stall_cfg.get("enabled", True):
        threshold_sec = int(stall_cfg.get("threshold_sec", 3600))
        new_stall, stall_dur = check_stall(status, state, threshold_sec)
        if new_stall:
            stall_min = stall_dur // 60
            logging.warning("Workflow stalled in %s for %d min", exp_name, stall_min)
            body = (
                f"⚠️  Workflow stalled: {exp_name} on {cluster}\n\n"
                f"Time: {status['updated_at']}\n"
                f"No jobs have been RUNNING, QUEUED, or SUBMITTING for {stall_min} minutes.\n"
                f"Please check the workflow and restart if needed."
            )
            send_email(f"{subject_prefix}: workflow stalled", body, recipients, dry_run)

    # 4. Hung job check
    if hung_cfg.get("enabled", False):
        hung_list = check_hung_jobs(status, expdir, hung_cfg, xml, db, dry_run)
        if hung_list:
            action = hung_cfg.get("action", "alert")
            logging.warning("Hung job(s) in %s: %s", exp_name, hung_list)
            lines = [
                f"⚠️  Hung job(s) detected in {exp_name} on {cluster}",
                f"Time: {status['updated_at']}",
                "",
                "The following RUNNING jobs have stale log files:",
                "──────────────────────────────────────────",
            ]
            for h in hung_list:
                lines.append(
                    f"  Cycle: {h['cycle']}  Task: {h['task']}  JobID: {h['jobid']}  Idle: {h['idle_sec'] // 60}m"
                )
            lines.append("")
            if action == "cancel_and_reboot":
                lines.append("Action taken: Jobs were cancelled and rebooted via rocotoboot.")
            else:
                lines.append("No automatic action taken. Please investigate.")
            send_email(f"{subject_prefix}: hung job(s)", "\n".join(lines), recipients, dry_run)

    # 5. Save deduplication state
    state["last_check"] = status["updated_at"]
    save_state(state_file, state)

    # 6. Write .state/<exp>.json
    write_status_json(status, status_file)
    logging.info("Saved status JSON: %s", status_file)

    s = status["summary"]
    logging.info(
        "Done %s: cycles=%d (active=%d, done=%d), tasks=%d (ok=%d, run=%d, wait=%d, dead=%d)",
        exp_name,
        s["total_cycles"],
        s["active_cycles"],
        s["done_cycles"],
        s["total_tasks"],
        s["succeeded"],
        s["running"],
        s["waiting"],
        s["dead"],
    )
    return status_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified HPC Rocoto Workflow Monitor"
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        help="Path to YAML config file (default: <repo_root>/myexps.yml)",
    )
    parser.add_argument(
        "-c",
        "--config",
        dest="config_opt",
        default=None,
        help="Path to YAML config file (alternative flag to positional argument)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run checks and update local status JSON files without sending emails or pushing to GitHub",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    raw_config_arg = args.config_opt or args.config
    if raw_config_arg:
        config_path = Path(raw_config_arg).expanduser()
        if not config_path.is_absolute():
            caller_pwd = Path(os.environ.get("CALLER_PWD", os.getcwd()))
            if (caller_pwd / config_path).is_file():
                config_path = caller_pwd / config_path
            else:
                config_path = REPO_ROOT / config_path
    else:
        config_path = REPO_ROOT / "myexps.yml"

    config_file = Path(os.path.abspath(config_path))
    if not config_file.is_file():
        print(
            f"ERROR: Config file not found: {config_file}\n"
            f"Please specify a valid YAML file or create the default myexps.yml:\n"
            f"  cp {REPO_ROOT / 'config.yml'} {REPO_ROOT / 'myexps.yml'}",
            file=sys.stderr,
        )
        return 1

    machine = os.environ.get("MACHINE") or "unknown"
    state_dir = REPO_ROOT / ".state" / machine
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = state_dir / "monitor.log"
    if log_file.is_file() and log_file.stat().st_size > 1048576:
        log_file.replace(state_dir / "monitor.log.prev")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s UTC] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logging.Formatter.converter = time.gmtime

    status_branch = get_status_branch()
    logging.info(
        "=== Monitor run started (MACHINE=%s, config=%s, branch=%s, dry_run=%s) ===",
        os.environ.get("MACHINE", "unset"),
        config_file.name,
        status_branch,
        args.dry_run,
    )

    raw_cfg = yaml.safe_load(config_file.read_text()) or {}
    common_cfg = raw_cfg.get("common", {})
    security_cfg = raw_cfg.get("security", {})
    allowed_orgs = security_cfg.get("allowed_orgs", list(ALLOWED_ORGS))

    # Ingest pending experiment configuration requests from NOAA org members
    sync_pending_instructions(machine, allowed_orgs)

    static_experiments = raw_cfg.get("experiments", [])
    dynamic_experiments = load_dynamic_experiments(machine)

    all_experiments = list(static_experiments) + list(dynamic_experiments)
    if not all_experiments:
        logging.info("No experiments currently defined in %s or dynamic state. Waiting for configuration requests.", config_file)
        return 0

    if len(all_experiments) > MAX_TOTAL_EXPERIMENTS:
        logging.warning("Total experiments count (%d) exceeds limit of %d. Truncating to %d.", len(all_experiments), MAX_TOTAL_EXPERIMENTS, MAX_TOTAL_EXPERIMENTS)
        all_experiments = all_experiments[:MAX_TOTAL_EXPERIMENTS]

    merged_list = [deep_merge(common_cfg, exp_item) for exp_item in all_experiments]
    updated_files: List[Path] = []

    # Distribute experiments across at most MAX_CONCURRENT_WORKERS (20) workers.
    # Experiments exceeding 20 are evenly partitioned across the 20 workers and run sequentially within each worker.
    num_workers = min(max(len(merged_list), 1), MAX_CONCURRENT_WORKERS)
    worker_batches: List[List[Dict[str, Any]]] = [[] for _ in range(num_workers)]
    for idx, exp_item in enumerate(merged_list):
        worker_batches[idx % num_workers].append(exp_item)

    def process_worker_batch(batch_items: List[Dict[str, Any]]) -> List[Path]:
        batch_results: List[Path] = []
        for exp in batch_items:
            try:
                res = process_experiment(exp, state_dir, args.dry_run)
                if res is not None:
                    batch_results.append(res)
            except Exception as exc:
                logging.exception("Error processing %s: %s", exp.get("name", "unknown"), exc)
        return batch_results

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as pool:
        future_map = {
            pool.submit(process_worker_batch, batch): i
            for i, batch in enumerate(worker_batches) if batch
        }
        for fut in concurrent.futures.as_completed(future_map):
            try:
                res_list = fut.result()
                if res_list:
                    updated_files.extend(res_list)
            except Exception as exc:
                logging.exception("Worker batch failed: %s", exc)

    ok_count = len(updated_files)
    if ok_count > 0:
        git_push_status_branch(REPO_ROOT, sorted(updated_files), status_branch, args.dry_run)

    logging.info("=== Monitor run completed (%d/%d experiments succeeded) ===", ok_count, len(all_experiments))
    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
