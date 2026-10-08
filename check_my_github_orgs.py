#!/usr/bin/env python3
"""
Standalone Diagnostic Tool: Inspect GitHub Identity, Enterprises, and Organization Memberships.

This script diagnoses:
1. Authenticated user identity and active token scopes (OAuth scopes).
2. All organizations visible to the token.
3. Detailed organization membership status (active vs pending, admin vs member).
4. Direct SAML SSO authorization status for NOAA organizations (NOAA-GSL, NOAA-EMC, etc.).
5. Specific reasons if NOAA-GSL membership is not recognized (e.g. missing SAML authorization, pending invite).
"""

import os
import sys
import json
import getpass
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, Tuple


def request_github(url: str, token: str) -> Tuple[int, Dict[str, str], Any]:
    """Perform a GET request to GitHub API with the given token."""
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "NOAA-Diagnostic-Client/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            resp_headers = dict(resp.headers)
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = body
            return status, resp_headers, data
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = dict(e.headers)
        body = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = body
        return status, resp_headers, data
    except urllib.error.URLError as e:
        return 0, {}, {"error": str(e.reason)}


def run_diagnostics(token: str):
    token = token.strip()
    if not token:
        print("[ERROR] Token cannot be empty.")
        return

    print("=" * 70)
    print(" GitHub Identity & Organization Membership Diagnostic Report")
    print("=" * 70)

    # 1. Inspect Authenticated User
    print("\n[Step 1] Fetching Authenticated User Info...")
    status, headers, user_data = request_github("https://api.github.com/user", token)
    if status != 200:
        print(f"[-] Authentication Failed (HTTP {status})!")
        if isinstance(user_data, dict) and "message" in user_data:
            print(f"    Message: {user_data.get('message')}")
        return

    login = user_data.get("login", "")
    name = user_data.get("name") or "(Not specified)"
    email = user_data.get("email") or "(Private / Not public)"
    scopes_str = headers.get("X-OAuth-Scopes", headers.get("x-oauth-scopes", ""))
    scopes = [s.strip() for s in scopes_str.split(",") if s.strip()]

    print(f"[+] Authenticated Username: @{login}")
    print(f"    Full Name             : {name}")
    print(f"    Primary Public Email  : {email}")
    print(f"    Account Type          : {user_data.get('type')}")
    print(f"    Token OAuth Scopes    : {scopes if scopes else '(No explicit scopes)'}")

    has_read_org = any(s in scopes for s in ["read:org", "admin:org", "repo"])
    if not has_read_org:
        print("\n[!] WARNING: Your token DOES NOT have the 'read:org' scope!")
        print("    Without 'read:org', GitHub will hide private and SAML organization memberships from this token.")

    # 2. Query All Visible Organizations
    print("\n[Step 2] Listing All Visible Organizations (/user/orgs)...")
    status, headers, orgs_data = request_github("https://api.github.com/user/orgs", token)
    if status == 200 and isinstance(orgs_data, list):
        if orgs_data:
            print(f"[+] Found {len(orgs_data)} visible organization(s):")
            for o in orgs_data:
                print(f"    - @{o.get('login')} (Name: {o.get('description') or 'N/A'})")
        else:
            print("[-] No organizations returned by /user/orgs.")
    else:
        print(f"[-] /user/orgs returned HTTP {status}: {orgs_data}")

    # 3. Query All Detailed Memberships
    print("\n[Step 3] Querying Detailed Memberships (/user/memberships/orgs?state=all)...")
    status, headers, m_data = request_github("https://api.github.com/user/memberships/orgs?state=all", token)
    found_memberships = []
    if status == 200 and isinstance(m_data, list):
        if m_data:
            print(f"[+] Found {len(m_data)} detailed membership record(s):")
            for m in m_data:
                o_name = m.get("organization", {}).get("login", "")
                role = m.get("role", "")
                state = m.get("state", "")
                found_memberships.append(o_name.lower())
                print(f"    - Organization: @{o_name}")
                print(f"      Role        : {role}")
                print(f"      State       : {state.upper()} ({'Active Member' if state == 'active' else 'Pending Invitation'})")
        else:
            print("[-] No detailed memberships returned.")
    else:
        print(f"[-] /user/memberships/orgs returned HTTP {status}: {m_data}")

    # 4. Target Probing for NOAA Organizations
    print("\n[Step 4] Specific NOAA Organization SAML SSO Probing...")
    target_noaa_orgs = ["NOAA-GSL", "noaa-gsl", "noaa-oar", "NOAA-EMC", "noaa-gfdl", "ufs-community"]

    for org in target_noaa_orgs:
        url = f"https://api.github.com/user/memberships/orgs/{org}"
        status, resp_headers, data = request_github(url, token)
        sso_header = resp_headers.get("X-GitHub-SSO", resp_headers.get("x-github-sso", ""))

        print(f"\n  Checking @{org}:")
        print(f"    Endpoint HTTP Status: {status}")

        if sso_header:
            print(f"    [!] SAML SSO Header Detected: {sso_header}")
            print("    --> Action Required: This token must be authorized for SAML SSO in GitHub settings!")

        if status == 200 and isinstance(data, dict):
            state = data.get("state", "")
            role = data.get("role", "")
            print(f"    [+] VERIFIED MEMBER! State: {state.upper()}, Role: {role.upper()}")
        elif status == 404:
            print("    [-] HTTP 404 Not Found: You are not recognized as a member, or the token lacks SAML SSO authorization.")
        elif status == 403:
            msg = data.get("message", "") if isinstance(data, dict) else str(data)
            print(f"    [-] HTTP 403 Forbidden: {msg}")
        else:
            print(f"    [-] Response: {data}")

    # 5. Diagnostic Summary & Next Steps
    print("\n" + "=" * 70)
    print(" Diagnostic Summary & Remediation Guide")
    print("=" * 70)

    is_gsl_member = "noaa-gsl" in found_memberships or "NOAA-GSL".lower() in found_memberships
    if is_gsl_member:
        print(f"[SUCCESS] Your account @{login} IS confirmed as a member of NOAA-GSL!")
        print("          If the web dashboard failed, ensure your browser token matches this one.")
    else:
        print(f"[FINDING] @{login} is currently NOT returned as an active member of @NOAA-GSL.")
        print("\nCommon Root Causes & Fixes:")
        print("1. SAML SSO Authorization Missing on Token (Most Likely):")
        print("   - Go to: https://github.com/settings/tokens")
        print("   - Find this token in the list, look for the 'Configure SSO' dropdown button next to it.")
        print("   - Click 'Configure SSO' -> Click 'Authorize' next to NOAA-GSL (or NOAA Organization).")
        print("   - Once authorized with NOAA SSO, rerun this script to confirm.")
        print("\n2. Pending Invitation:")
        print("   - If you were invited recently, you may need to accept the invitation at:")
        print("     https://github.com/orgs/NOAA-GSL/invitation")
        print("\n3. Account Discrepancy:")
        print(f"   - Currently logged in as GitHub handle: @{login}")
        print("   - Please verify with your NOAA GSL admin if your GitHub account was registered as @spanNOAA or under another handle.")
    print("=" * 70 + "\n")


def main():
    token = ""
    if len(sys.argv) > 1:
        token = sys.argv[1].strip()
    elif os.environ.get("GITHUB_TOKEN"):
        token = os.environ.get("GITHUB_TOKEN").strip()

    if not token:
        print("Please enter your GitHub Personal Access Token (PAT) to test:")
        token = getpass.getpass("Token (input hidden): ").strip()

    if not token:
        print("[ERROR] No token provided. Exiting.")
        sys.exit(1)

    run_diagnostics(token)


if __name__ == "__main__":
    main()
