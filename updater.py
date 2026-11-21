"""
updater.py - Automatic update checker and installer for Rorishper.

Features:
- Dual-mode update support:
    1. Git mode: performs 'git pull' if running inside a cloned repository.
    2. Direct GitHub ZIP mode: downloads and extracts latest repository archive if running without git.
- Background non-blocking check on startup.
- Automatically updates dependencies if 'requirements.txt' changes.
- Preserves user configuration ('settings.json'), models, and virtual environments.
- Windows process respawning for seamless 1-click restart.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "settings.json"
VERSION_FILE = APP_DIR / "version.json"
REQUIREMENTS_FILE = APP_DIR / "requirements.txt"

# GitHub Repository Configuration
REPO_OWNER = "RohanDas28"
REPO_NAME = "Rorishper"
BRANCH = "main"

# Default metadata fallback
DEFAULT_VERSION = {
    "version": "1.0.1",
    "commit": "075fced",
    "release_date": "2026-11-21",
    "description": "Auto-update support"
}


def _file_md5(path: Path) -> str:
    """Compute MD5 checksum of a file if it exists."""
    if not path.is_file():
        return ""
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def is_git_repo() -> bool:
    """Check if the application is running inside a Git clone with git installed."""
    git_dir = APP_DIR / ".git"
    if not git_dir.is_dir():
        return False
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
        )
        return res.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def get_local_version_info() -> dict:
    """Return local version information from version.json and git."""
    info = dict(DEFAULT_VERSION)
    if VERSION_FILE.is_file():
        try:
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                info.update(loaded)
        except Exception:
            pass

    if is_git_repo():
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                info["commit"] = res.stdout.strip()
        except Exception:
            pass

    return info


def fetch_remote_commit_sha() -> tuple[str | None, str | None]:
    """Fetch the latest commit SHA for the branch from GitHub API.
    Returns (short_sha, full_sha) or (None, None) if unreachable."""
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits/{BRANCH}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Rorishper-AutoUpdater/1.0",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            full_sha = data.get("sha", "")
            return full_sha[:7], full_sha
    except Exception:
        # Fallback: try raw version.json from GitHub
        raw_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/version.json"
        try:
            raw_req = urllib.request.Request(
                raw_url,
                headers={"User-Agent": "Rorishper-AutoUpdater/1.0"},
            )
            with urllib.request.urlopen(raw_req, timeout=4) as resp:
                remote_data = json.loads(resp.read().decode("utf-8"))
                commit = remote_data.get("commit", "")
                if commit:
                    return commit[:7], commit
        except Exception:
            pass
    return None, None


def check_for_updates() -> tuple[bool, dict, str]:
    """Check if an update is available on GitHub.
    Returns:
        (has_update: bool, update_info: dict, message: str)
    """
    local = get_local_version_info()
    local_commit = local.get("commit", "").strip()

    # If git is available, we can also check git fetch
    if is_git_repo():
        try:
            fetch_res = subprocess.run(
                ["git", "fetch", "--quiet", "origin", BRANCH],
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8,
            )
            if fetch_res.returncode == 0:
                # Compare HEAD with origin/main
                rev_local = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(APP_DIR),
                    stdout=subprocess.PIPE,
                    text=True,
                    timeout=2,
                ).stdout.strip()
                rev_remote = subprocess.run(
                    ["git", "rev-parse", f"origin/{BRANCH}"],
                    cwd=str(APP_DIR),
                    stdout=subprocess.PIPE,
                    text=True,
                    timeout=2,
                ).stdout.strip()

                if rev_local and rev_remote and rev_local != rev_remote:
                    update_info = {
                        "local_commit": rev_local[:7],
                        "remote_commit": rev_remote[:7],
                        "mode": "git",
                    }
                    return True, update_info, f"New version available ({rev_remote[:7]})"
                elif rev_local and rev_remote and rev_local == rev_remote:
                    return False, local, "Rorishper is up to date"
        except Exception:
            pass  # Fall through to HTTP check

    # HTTP check via GitHub API / raw version file
    remote_short, remote_full = fetch_remote_commit_sha()
    if not remote_short:
        return False, local, "Could not reach update server (check internet)"

    # If the remote commit is different from our local commit, an update is available
    if not local_commit or remote_short != local_commit:
        update_info = {
            "local_commit": local_commit or "initial",
            "remote_commit": remote_short,
            "mode": "git" if is_git_repo() else "download",
        }
        return True, update_info, f"New version available ({remote_short})"

    return False, local, "Rorishper is up to date"


def apply_update(update_info: dict | None = None) -> tuple[bool, str]:
    """Download and apply the latest update.
    Preserves user settings, .venv, and models. Runs pip install if requirements changed.
    Returns:
        (success: bool, status_message: str)
    """
    req_hash_before = _file_md5(REQUIREMENTS_FILE)

    # 1. GIT UPDATE PATH
    if is_git_repo():
        try:
            # Stash any uncommitted tracked changes to avoid merge conflicts
            subprocess.run(
                ["git", "stash"],
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            pull_res = subprocess.run(
                ["git", "pull", "--ff-only", "origin", BRANCH],
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=25,
            )
            if pull_res.returncode != 0:
                # If fast-forward failed, try normal pull
                pull_res = subprocess.run(
                    ["git", "pull", "origin", BRANCH],
                    cwd=str(APP_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=25,
                )

            # Pop stash if we stashed anything
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )

            if pull_res.returncode != 0:
                return False, f"Git pull failed: {pull_res.stderr.strip()[:100]}"

        except Exception as e:
            return False, f"Git update error: {e}"

    # 2. ZIP DOWNLOAD PATH (Git-less)
    else:
        zip_urls = [
            f"https://codeload.github.com/{REPO_OWNER}/{REPO_NAME}/zip/refs/heads/{BRANCH}",
            f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/heads/{BRANCH}.zip",
        ]
        download_success = False
        last_error = None

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = Path(tmpdir) / "update.zip"
                for z_url in zip_urls:
                    try:
                        req = urllib.request.Request(
                            z_url,
                            headers={"User-Agent": "Rorishper-AutoUpdater/1.0"},
                        )
                        with urllib.request.urlopen(req, timeout=35) as resp, open(zip_path, "wb") as f_out:
                            shutil.copyfileobj(resp, f_out)
                        download_success = True
                        break
                    except Exception as err:
                        last_error = err
                        continue

                if not download_success:
                    return False, f"Failed to download update package: {last_error}"

                extract_dir = Path(tmpdir) / "extracted"
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)

                # Locate extracted root folder (typically Rorishper-main)
                subfolders = [p for p in extract_dir.iterdir() if p.is_dir()]
                source_root = subfolders[0] if subfolders else extract_dir

                # Copy files over, protecting user config, venv, and cache
                protected_dir_names = {
                    ".venv",
                    "venv",
                    ".git",
                    "__pycache__",
                    "models",
                    "huggingface",
                }

                for item in source_root.rglob("*"):
                    rel = item.relative_to(source_root)
                    if any(part in protected_dir_names for part in rel.parts):
                        continue
                    if rel.name == "settings.json" and (APP_DIR / "settings.json").is_file():
                        # Preserve existing user settings (hotkey, preferences, model)
                        continue

                    dest = APP_DIR / rel
                    if item.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    elif item.is_file():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest)

        except Exception as e:
            return False, f"Download update error: {e}"

    # 3. Check if dependencies changed
    req_hash_after = _file_md5(REQUIREMENTS_FILE)
    if req_hash_before and req_hash_after and req_hash_before != req_hash_after:
        try:
            python_exe = sys.executable
            # If using .venv, prefer the venv python
            venv_python = APP_DIR / ".venv" / "Scripts" / "python.exe"
            if venv_python.is_file():
                python_exe = str(venv_python)

            subprocess.run(
                [python_exe, "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)],
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        except Exception:
            pass

    # Save new commit into version.json if available
    if update_info and "remote_commit" in update_info:
        try:
            current_meta = get_local_version_info()
            current_meta["commit"] = update_info["remote_commit"]
            with open(VERSION_FILE, "w", encoding="utf-8") as f:
                json.dump(current_meta, f, indent=2)
        except Exception:
            pass

    return True, "Update applied successfully"


def restart_application(on_before_quit=None):
    """Cleanly relaunch Rorishper and exit the current process."""
    if on_before_quit:
        try:
            on_before_quit()
        except Exception:
            pass

    app_script = APP_DIR / "app.py"
    venv_pythonw = APP_DIR / ".venv" / "Scripts" / "pythonw.exe"
    venv_python = APP_DIR / ".venv" / "Scripts" / "python.exe"

    if venv_pythonw.is_file():
        python_bin = str(venv_pythonw)
    elif venv_python.is_file():
        python_bin = str(venv_python)
    else:
        python_bin = sys.executable

    # Launch new instance in background detached
    try:
        if os.name == "nt":
            creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([python_bin, str(app_script)], cwd=str(APP_DIR), creationflags=creationflags)
        else:
            subprocess.Popen([python_bin, str(app_script)], cwd=str(APP_DIR))
    except Exception:
        subprocess.Popen([python_bin, str(app_script)], cwd=str(APP_DIR))

    sys.exit(0)
