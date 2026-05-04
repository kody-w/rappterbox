"""Lineage integrity check for RAPP variants.

A RAPP variant is created by templating from a parent repo (the parent's
code, including its rappid.json, is duplicated into a fresh repo with no
git history). The variant MUST regenerate its own rappid before going
live; otherwise it carries the parent's identity in a non-parent location,
which corrupts the lineage chain.

This module detects that case: the rappid.json identifies us as one of
the known template parents, but our git remote points elsewhere. Boot
guards (and the installer) call assert_initialized() to refuse to
operate until initialize-variant.sh has been run.

Single-parent rule (Constitution Article XXXIV): a variant's parent_rappid
points at the repo whose code it inherited, and ONLY that repo. You
cannot template from wildhaven and claim rapp as your parent — the
chain must reflect actual code lineage.

Stdlib only. Safe to vendor anywhere.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from typing import Any


# Known template repos in the lineage tree. Each entry maps a rappid to
# the canonical github "owner/repo" path of the repo that owns that
# rappid. When this registry's keys appear as a clone's OWN rappid AND
# the clone's git remote points elsewhere, the clone is uninitialized.
#
# Add new entries here when a variant promotes itself to a template.
KNOWN_TEMPLATE_REPOS: dict[str, str] = {
    "0b635450-c042-49fb-b4b1-bdb571044dec": "kody-w/rapp",
    "37ad22f5-ed6d-48b1-b8b4-61019f58a42b": "kody-w/wildhaven-ai-homes-twin",
}


def _normalize_owner_repo(url: str) -> str:
    """Reduce a GitHub URL to lowercase 'owner/repo'.

    Accepts https, ssh, git@ forms; strips .git suffix and trailing slashes.
    Returns '' if the URL doesn't look like a GitHub URL.
    """
    if not url:
        return ""
    s = url.strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    for prefix in ("https://", "http://", "ssh://", "git://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if s.startswith("git@"):
        s = s[len("git@"):]
    if s.startswith("github.com:"):
        s = "github.com/" + s[len("github.com:"):]
    if s.startswith("github.com/"):
        s = s[len("github.com/"):]
    return s.lower()


def _git_remote_url(repo_root: pathlib.Path) -> str:
    """Resolve the origin remote URL, with a parser fallback.

    Primary path: `git -C <root> config --get remote.origin.url` (authoritative).
    Fallback: parse <root>/.git/config directly. Used when the git binary is
    missing or when `git -C` rejects an incomplete repo (no HEAD, etc.).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip()
        if url:
            return url
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # Fallback: parse .git/config directly. Pure stdlib — no git binary needed.
    cfg = pathlib.Path(repo_root) / ".git" / "config"
    if not cfg.exists():
        return ""
    try:
        in_remote_origin = False
        for raw_line in cfg.read_text().splitlines():
            line = raw_line.strip()
            if line.startswith("[remote "):
                in_remote_origin = (line == '[remote "origin"]')
            elif line.startswith("["):
                in_remote_origin = False
            elif in_remote_origin and line.startswith("url"):
                # url = <value> or url=<value>
                _, _, value = line.partition("=")
                return value.strip()
    except OSError:
        pass
    return ""


def _find_repo_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """Walk up from `start` to find the directory containing rappid.json.

    Falls back to the parent of this module's directory.
    """
    if start is None:
        start = pathlib.Path(__file__).resolve().parent.parent
    here = pathlib.Path(start).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "rappid.json").exists():
            return candidate
    return here


def check_lineage(repo_root: pathlib.Path | str | None = None) -> dict[str, Any]:
    """Inspect rappid.json + git remote and report lineage status.

    Returned dict always has: status, message, hint, repo_root, rappid,
    parent_rappid, parent_repo, git_remote, name, role.

    status is one of:
      - "self": this repo IS one of the known template roots, identity ok.
      - "master": this repo declares role=master with no parent (species root).
      - "variant_initialized": rappid is unique, parent_repo set, remote != parent.
      - "variant_uninitialized": rappid matches a known template's rappid but
        git remote is somewhere else (template was cloned, init never ran).
      - "lineage_mismatch": variant rappid.json is internally inconsistent
        (no parent_repo, or parent_repo == own remote).
      - "no_rappid": no rappid.json found.
    """
    root = pathlib.Path(repo_root) if repo_root else _find_repo_root()
    rappid_path = root / "rappid.json"

    base = {
        "repo_root": str(root),
        "rappid": None,
        "parent_rappid": None,
        "parent_repo": None,
        "git_remote": None,
        "name": None,
        "role": None,
    }

    if not rappid_path.exists():
        return {
            **base,
            "status": "no_rappid",
            "message": f"No rappid.json at {rappid_path}.",
            "hint": "This does not look like a RAPP repository.",
        }

    try:
        data = json.loads(rappid_path.read_text())
    except json.JSONDecodeError as e:
        return {
            **base,
            "status": "lineage_mismatch",
            "message": f"rappid.json is not valid JSON: {e}.",
            "hint": "Fix the JSON syntax and re-check.",
        }

    rappid = data.get("rappid")
    parent_rappid = data.get("parent_rappid")
    parent_repo = data.get("parent_repo")
    role = data.get("role")
    name = data.get("name")
    remote = _git_remote_url(root)
    remote_or = _normalize_owner_repo(remote)
    parent_or = _normalize_owner_repo(parent_repo or "")

    base.update({
        "rappid": rappid,
        "parent_rappid": parent_rappid,
        "parent_repo": parent_repo,
        "git_remote": remote,
        "name": name,
        "role": role,
    })

    # Case 1: rappid matches a known template root.
    if rappid in KNOWN_TEMPLATE_REPOS:
        canonical = KNOWN_TEMPLATE_REPOS[rappid]
        if remote_or == canonical:
            return {
                **base,
                "status": "self",
                "message": f"This IS {canonical} (template root, rappid {rappid}).",
                "hint": "",
            }
        return {
            **base,
            "status": "variant_uninitialized",
            "message": (
                f"rappid.json identifies this repo as {canonical} "
                f"(rappid {rappid}), but the git remote is "
                f"{remote or '<unset>'}. This is an uninitialized "
                f"template clone — the parent's identity must not "
                f"travel to a child repo."
            ),
            "hint": (
                "Run `bash installer/initialize-variant.sh` to generate "
                "a fresh rappid and rewrite rappid.json for this variant."
            ),
        }

    # Case 2: declared species root.
    if role == "master" and parent_rappid is None and parent_repo is None:
        return {
            **base,
            "status": "master",
            "message": f"Species root: {name or rappid}.",
            "hint": "",
        }

    # Case 3: variant. parent_repo MUST be set; it MUST NOT equal own remote.
    if not parent_repo:
        return {
            **base,
            "status": "lineage_mismatch",
            "message": "Variant rappid.json has no parent_repo set.",
            "hint": "Re-run installer/initialize-variant.sh to set parent_repo.",
        }

    if remote_or and remote_or == parent_or:
        return {
            **base,
            "status": "lineage_mismatch",
            "message": (
                f"Variant declares parent_repo={parent_repo}, but the git "
                f"remote is the same repo. A variant cannot be its own parent."
            ),
            "hint": "Re-run installer/initialize-variant.sh to fix the lineage.",
        }

    return {
        **base,
        "status": "variant_initialized",
        "message": (
            f"Initialized variant: name={name or '<unset>'}, "
            f"rappid={rappid}, parent={parent_repo}."
        ),
        "hint": "",
    }


def assert_initialized(repo_root: pathlib.Path | str | None = None) -> dict[str, Any]:
    """Refuse to boot if the lineage check fails.

    Bypassable by setting RAPP_SKIP_LINEAGE_CHECK=1 (for emergency repair
    work that needs to load the agents anyway). The bypass is logged.
    """
    info = check_lineage(repo_root)
    bad = info["status"] in ("variant_uninitialized", "lineage_mismatch", "no_rappid")
    if bad and os.environ.get("RAPP_SKIP_LINEAGE_CHECK") == "1":
        print(f"[lineage] WARNING: bypassed via RAPP_SKIP_LINEAGE_CHECK. {info['message']}")
        return info
    if bad:
        msg = (
            "REFUSE TO BOOT: lineage check failed.\n\n"
            f"  status:  {info['status']}\n"
            f"  repo:    {info['repo_root']}\n"
            f"  remote:  {info['git_remote'] or '<unset>'}\n"
            f"  rappid:  {info['rappid'] or '<unset>'}\n\n"
            f"{info['message']}\n\n"
            f"{info['hint']}\n\n"
            "(see utils/lineage_check.py — set RAPP_SKIP_LINEAGE_CHECK=1 "
            "to bypass for emergency repair only.)"
        )
        raise SystemExit(msg)
    return info


def main() -> int:
    # CLI: walk up from cwd so the user can run from anywhere inside the repo.
    info = check_lineage(pathlib.Path.cwd())
    print(json.dumps(info, indent=2))
    if info["status"] in ("self", "master", "variant_initialized"):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
