#!/usr/bin/env python3
"""
Bootstrap activity-provider auth and GitHub setup for this repository.

This script performs:
0) Optional local virtualenv bootstrap (.venv + requirements install).
1) Provider-specific auth/bootstrap (Strava OAuth or Garmin credentials).
2) GitHub secret + variable updates via gh CLI.
3) Best-effort GitHub setup automation (workflows, pages, first run).
"""

import argparse
import base64
import getpass
import html
import http.server
import io
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import json
import webbrowser
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple


TOKEN_ENDPOINT = "https://www.strava.com/oauth/token"
AUTHORIZE_ENDPOINT = "https://www.strava.com/oauth/authorize"
CALLBACK_PATH = "/exchange_token"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 180
DEFAULT_SOURCE = "strava"
VENV_DIRNAME = ".venv"

STATUS_OK = "OK"
STATUS_SKIPPED = "SKIPPED"
STATUS_MANUAL_REQUIRED = "MANUAL_REQUIRED"

UNIT_PRESETS = {
    "us": ("mi", "ft"),
    "metric": ("km", "m"),
}
REPO_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/?$",
    re.IGNORECASE,
)
REPO_SSH_RE = re.compile(
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+)$",
    re.IGNORECASE,
)
REPO_SLUG_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)$")


@dataclass
class StepResult:
    name: str
    status: str
    detail: str
    manual_help: Optional[str] = None


@dataclass
class CallbackResult:
    code: Optional[str] = None
    error: Optional[str] = None


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    result: CallbackResult = CallbackResult()
    expected_state: str = ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404, "Not Found")
            return

        query = urllib.parse.parse_qs(parsed.query)
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        error = query.get("error", [""])[0]

        if error:
            self.__class__.result.error = f"Strava returned error: {error}"
        elif not code:
            self.__class__.result.error = "Missing code query parameter in callback URL."
        elif state != self.__class__.expected_state:
            self.__class__.result.error = "State mismatch in callback. Please retry."
        else:
            self.__class__.result.code = code

        message = "Authorization received. You can close this tab and return to the terminal."
        if self.__class__.result.error:
            message = f"Authorization failed: {self.__class__.result.error}"

        safe_message = html.escape(message, quote=True)
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Strava Auth</title></head><body>"
            f"<p>{safe_message}</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def _run_stream(cmd: list[str], *, cwd: Optional[str] = None) -> None:
    subprocess.run(cmd, check=True, cwd=cwd)


def _first_stderr_line(stderr: str) -> str:
    text = (stderr or "").strip()
    if not text:
        return "Unknown error."
    return text.splitlines()[0]


def _is_transient_gh_failure(stderr: str) -> bool:
    text = (stderr or "").lower()
    transient_tokens = [
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection refused",
    ]
    return any(token in text for token in transient_tokens)


def _isatty() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _prompt(value: Optional[str], label: str, secret: bool = False) -> str:
    if value:
        return value.strip()
    if secret:
        return _prompt_secret_masked(f"{label}: ").strip()
    return input(f"{label}: ").strip()


def _prompt_secret_masked(prompt: str) -> str:
    if not _isatty():
        return getpass.getpass(prompt)

    try:
        import termios
        import tty
    except ImportError:
        return getpass.getpass(prompt)

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    chars: list[str] = []
    sys.stdout.write(prompt)
    sys.stdout.flush()

    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x7f", "\x08"):
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if ch == "\x04":
                if not chars:
                    raise EOFError("Input closed.")
                continue
            if ord(ch) < 32:
                continue
            chars.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)

    return "".join(chars)


def _assert_gh_ready() -> None:
    if shutil.which("gh") is None:
        raise RuntimeError(
            "GitHub CLI (`gh`) is required. Install it from https://cli.github.com/ and run `gh auth login`."
        )

    status = _run(["gh", "auth", "status"], check=False)
    if status.returncode != 0:
        raise RuntimeError(
            "GitHub CLI is not authenticated. Run `gh auth login` and re-run this script."
        )


def _assert_repo_access(repo: str) -> None:
    check = _run(
        ["gh", "repo", "view", repo, "--json", "nameWithOwner"],
        check=False,
    )
    if check.returncode != 0:
        detail = _first_stderr_line(check.stderr)
        raise RuntimeError(f"Unable to access repository '{repo}' with current gh auth context: {detail}")


def _normalize_repo_slug(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None

    m = REPO_URL_RE.match(raw)
    if m:
        repo = m.group("repo")
        if repo.endswith(".git"):
            repo = repo[:-4]
        return f"{m.group('owner')}/{repo}"

    m = REPO_SSH_RE.match(raw)
    if m:
        repo = m.group("repo")
        if repo.endswith(".git"):
            repo = repo[:-4]
        return f"{m.group('owner')}/{repo}"

    m = REPO_SLUG_RE.match(raw)
    if m:
        return f"{m.group('owner')}/{m.group('repo')}"

    return None


def _repo_slug_from_git() -> Optional[str]:
    result = _run(["git", "config", "--get", "remote.origin.url"], check=False)
    if result.returncode != 0:
        return None
    return _normalize_repo_slug(result.stdout.strip())


def _repo_slug_from_gh_context() -> Optional[str]:
    result = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return _normalize_repo_slug(result.stdout.strip())


def _resolve_repo_slug(explicit_repo: Optional[str]) -> Optional[str]:
    candidates = [
        explicit_repo,
        _repo_slug_from_git(),
        os.environ.get("GH_REPO"),
        _repo_slug_from_gh_context(),
    ]
    for candidate in candidates:
        normalized = _normalize_repo_slug(candidate)
        if normalized:
            return normalized
    return None


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _in_virtualenv() -> bool:
    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    real_prefix = getattr(sys, "real_prefix", None)
    return bool(real_prefix or (sys.prefix != base_prefix))


def _venv_python_path(venv_dir: str) -> str:
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def _bootstrap_env_and_reexec(args: argparse.Namespace) -> None:
    if args.no_bootstrap_env or args.env_bootstrapped or _in_virtualenv():
        return

    root = _project_root()
    requirements = os.path.join(root, "requirements.txt")
    if not os.path.exists(requirements):
        return

    venv_dir = os.path.join(root, VENV_DIRNAME)
    venv_python = _venv_python_path(venv_dir)
    if not os.path.exists(venv_python):
        print("\nCreating local virtual environment (.venv)...")
        _run_stream([sys.executable, "-m", "venv", venv_dir], cwd=root)

    print("Installing Python dependencies into .venv...")
    _run_stream([venv_python, "-m", "pip", "install", "--upgrade", "pip"], cwd=root)
    _run_stream([venv_python, "-m", "pip", "install", "-r", requirements], cwd=root)

    script_path = os.path.abspath(__file__)
    child_args = [arg for arg in sys.argv[1:] if arg != "--env-bootstrapped"]
    child_args.append("--env-bootstrapped")
    print("Re-launching setup inside .venv...")
    raise SystemExit(subprocess.call([venv_python, script_path, *child_args], cwd=root))


def _set_secret(name: str, value: str, repo: str) -> None:
    cmd = ["gh", "secret", "set", name, "--repo", repo]
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        result = _run(cmd, input_text=value, check=False)
        if result.returncode == 0:
            return
        stderr = (result.stderr or "").strip()
        if attempt < max_attempts and _is_transient_gh_failure(stderr):
            sleep_seconds = min(8, 2 ** (attempt - 1))
            print(
                f"Transient error setting secret {name}; retrying in {sleep_seconds}s "
                f"(attempt {attempt}/{max_attempts})..."
            )
            time.sleep(sleep_seconds)
            continue
        detail = f": {stderr.splitlines()[0]}" if stderr else ""
        raise RuntimeError(f"Failed to set GitHub secret {name}{detail}")


def _set_variable(name: str, value: str, repo: str) -> None:
    cmd = ["gh", "variable", "set", name, "--repo", repo, "--body", value]
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        result = _run(cmd, check=False)
        if result.returncode == 0:
            return
        stderr = (result.stderr or "").strip()
        if attempt < max_attempts and _is_transient_gh_failure(stderr):
            sleep_seconds = min(8, 2 ** (attempt - 1))
            print(
                f"Transient error setting variable {name}; retrying in {sleep_seconds}s "
                f"(attempt {attempt}/{max_attempts})..."
            )
            time.sleep(sleep_seconds)
            continue
        detail = f": {stderr.splitlines()[0]}" if stderr else ""
        raise RuntimeError(f"Failed to set GitHub variable {name}{detail}")


def _authorize_and_get_code(
    client_id: str,
    redirect_uri: str,
    scope: str,
    port: int,
    timeout_seconds: int,
    open_browser: bool,
) -> str:
    state = secrets.token_urlsafe(20)
    OAuthCallbackHandler.result = CallbackResult()
    OAuthCallbackHandler.expected_state = state

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "force",
        "scope": scope,
        "state": state,
    }
    auth_url = f"{AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"

    print("\nOpen this URL to authorize Strava access:")
    print(auth_url)

    with ReusableTCPServer(("localhost", port), OAuthCallbackHandler) as server:
        server.timeout = 1
        if open_browser:
            webbrowser.open(auth_url, new=1, autoraise=True)

        print(f"\nWaiting for callback on {redirect_uri} (timeout: {timeout_seconds}s)...")
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            server.handle_request()
            if OAuthCallbackHandler.result.code or OAuthCallbackHandler.result.error:
                break

    if OAuthCallbackHandler.result.error:
        raise RuntimeError(OAuthCallbackHandler.result.error)
    if not OAuthCallbackHandler.result.code:
        raise TimeoutError("Timed out waiting for Strava OAuth callback.")
    return OAuthCallbackHandler.result.code


def _exchange_code_for_tokens(client_id: str, client_secret: str, code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Strava token exchange failed with HTTP status {exc.code}.") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", "unknown network error")
        raise RuntimeError(f"Strava token exchange request failed: {reason}.") from None

    try:
        response_payload = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError("Unexpected token response format from Strava.") from None

    refresh_token = response_payload.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Strava response did not include refresh_token.")
    return response_payload


def _parse_iso8601_utc(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pages_url_from_slug(slug: str) -> str:
    owner, repo = slug.split("/", 1)
    if repo.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"


def _prompt_choice(
    prompt: str,
    choices: dict[str, str],
    default: Optional[str] = None,
    invalid_message: Optional[str] = None,
) -> str:
    while True:
        answer = input(prompt).strip().lower()
        if not answer:
            if default is not None:
                answer = default
            else:
                if invalid_message:
                    print(invalid_message)
                else:
                    allowed = ", ".join(sorted(choices.keys()))
                    print(f"Please enter one of: {allowed}")
                continue
        if answer in choices:
            return choices[answer]
        if invalid_message:
            print(invalid_message)
        else:
            allowed = ", ".join(sorted(choices.keys()))
            print(f"Please enter one of: {allowed}")


def _prompt_source() -> str:
    print("\nChoose activity source:")
    print("  1) Strava")
    print("  2) Garmin")
    selected = _prompt_choice(
        "Selection (enter 1 or 2): ",
        {"1": "strava", "2": "garmin"},
        default=None,
        invalid_message="Please enter '1' or '2'.",
    )
    return selected


def _resolve_source(args: argparse.Namespace, interactive: bool) -> str:
    if args.source:
        return args.source
    if interactive:
        return _prompt_source()
    return DEFAULT_SOURCE


def _generate_garmin_token_store_b64(email: str, password: str) -> str:
    try:
        import garth
    except ImportError:
        raise RuntimeError(
            "Missing Garmin auth dependency 'garth'. Re-run setup without --no-bootstrap-env."
        ) from None

    def _token_store_ready(directory: str) -> bool:
        return (
            os.path.isfile(os.path.join(directory, "oauth1_token.json"))
            and os.path.isfile(os.path.join(directory, "oauth2_token.json"))
        )

    def _hydrate_from_legacy_file(path: str, directory: str) -> None:
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        oauth1 = payload.get("oauth1_token")
        oauth2 = payload.get("oauth2_token")
        if isinstance(oauth1, dict):
            with open(os.path.join(directory, "oauth1_token.json"), "w", encoding="utf-8") as f:
                json.dump(oauth1, f)
        if isinstance(oauth2, dict):
            with open(os.path.join(directory, "oauth2_token.json"), "w", encoding="utf-8") as f:
                json.dump(oauth2, f)

    def _encode_dir_as_zip_b64(directory: str) -> str:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for root, _dirs, files in os.walk(directory):
                for filename in sorted(files):
                    full_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(full_path, directory)
                    archive.write(full_path, arcname=rel_path)
        data = buffer.getvalue()
        if not data:
            raise RuntimeError("Generated Garmin token store archive was empty.")
        return base64.b64encode(data).decode("ascii")

    with tempfile.TemporaryDirectory(prefix="garmin-token-store-") as tmpdir:
        try:
            garth.login(email, password)
            save_errors: list[str] = []
            try:
                garth.save(tmpdir)
            except Exception as exc:
                save_errors.append(str(exc))

            if not _token_store_ready(tmpdir):
                legacy_path = os.path.join(tmpdir, "garth-session.json")
                try:
                    garth.save(legacy_path)
                except Exception as exc:
                    save_errors.append(str(exc))
                _hydrate_from_legacy_file(legacy_path, tmpdir)

            if not _token_store_ready(tmpdir):
                details = "; ".join(save_errors) if save_errors else "unknown save failure"
                raise RuntimeError(
                    "garth token store is incomplete (expected oauth1_token.json and oauth2_token.json). "
                    f"Save details: {details}"
                )

            return _encode_dir_as_zip_b64(tmpdir)
        except Exception as exc:
            raise RuntimeError(
                f"Unable to generate GARMIN_TOKENS_B64 from provided Garmin credentials: {exc}"
            ) from None


def _prompt_units() -> Tuple[str, str]:
    print("\nChoose unit system:")
    print("  1) US (miles + feet)")
    print("  2) Metric (km + meters)")
    system = _prompt_choice(
        "Selection (enter 1 or 2): ",
        {"1": "us", "2": "metric"},
        default=None,
        invalid_message="Please enter '1' or '2'.",
    )
    return UNIT_PRESETS[system]


def _resolve_garmin_auth_values(args: argparse.Namespace, interactive: bool) -> Tuple[str, str, str]:
    token_store_b64 = (args.garmin_token_store_b64 or "").strip()
    email = (args.garmin_email or "").strip()
    password = (args.garmin_password or "").strip()

    if not token_store_b64:
        if not email:
            if interactive:
                email = _prompt(None, "GARMIN_EMAIL")
            else:
                raise RuntimeError(
                    "Missing Garmin credentials in non-interactive mode. "
                    "Provide --garmin-token-store-b64 or --garmin-email/--garmin-password."
                )
        if not password:
            if interactive:
                password = _prompt(None, "GARMIN_PASSWORD", secret=True)
            else:
                raise RuntimeError(
                    "Missing Garmin credentials in non-interactive mode. "
                    "Provide --garmin-token-store-b64 or --garmin-email/--garmin-password."
                )
        print("Generating GARMIN_TOKENS_B64 from Garmin credentials...")
        token_store_b64 = _generate_garmin_token_store_b64(email, password)

    return token_store_b64, email, password


def _resolve_units(args: argparse.Namespace, interactive: bool) -> Tuple[str, str]:
    if args.unit_system:
        return UNIT_PRESETS[args.unit_system]

    if interactive:
        return _prompt_units()

    raise RuntimeError(
        "Missing unit selection in non-interactive mode. "
        "Provide --unit-system {us|metric}."
    )


def _add_step(
    steps: list[StepResult],
    name: str,
    status: str,
    detail: str,
    manual_help: Optional[str] = None,
) -> None:
    steps.append(StepResult(name=name, status=status, detail=detail, manual_help=manual_help))


def _try_enable_actions_permissions(repo: str) -> Tuple[bool, str]:
    def _current_permissions() -> Tuple[Optional[bool], Optional[str]]:
        result = _run(
            ["gh", "api", f"repos/{repo}/actions/permissions"],
            check=False,
        )
        if result.returncode != 0:
            return None, None
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None, None
        enabled = payload.get("enabled")
        allowed_actions = payload.get("allowed_actions")
        return enabled if isinstance(enabled, bool) else None, (
            str(allowed_actions) if isinstance(allowed_actions, str) else None
        )

    errors: list[str] = []
    attempts = [
        [
            "gh",
            "api",
            "-X",
            "PUT",
            f"repos/{repo}/actions/permissions",
            "-F",
            "enabled=true",
            "-f",
            "allowed_actions=all",
        ],
        [
            "gh",
            "api",
            "-X",
            "PUT",
            f"repos/{repo}/actions/permissions",
            "-F",
            "enabled=true",
        ],
    ]
    for cmd in attempts:
        result = _run(cmd, check=False)
        if result.returncode == 0:
            enabled, allowed_actions = _current_permissions()
            if enabled:
                if allowed_actions:
                    return (
                        True,
                        f"Repository Actions are enabled (allowed_actions={allowed_actions}).",
                    )
                return True, "Repository Actions permissions configured."
            return True, "Repository Actions permissions configured."
        errors.append(_first_stderr_line(result.stderr))

    enabled, allowed_actions = _current_permissions()
    if enabled:
        if allowed_actions:
            return (
                True,
                (
                    "Repository Actions are already enabled "
                    f"(allowed_actions={allowed_actions}); API update was not required."
                ),
            )
        return True, "Repository Actions are already enabled; API update was not required."

    if errors:
        # Deduplicate while preserving order for concise summaries.
        ordered_unique = list(dict.fromkeys(errors))
        return False, "; ".join(ordered_unique)
    return False, "Unable to configure repository Actions permissions automatically."


def _try_enable_workflows(repo: str, workflows: list[str]) -> Tuple[bool, str]:
    failures = []
    for workflow in workflows:
        result = _run(
            ["gh", "workflow", "enable", workflow, "--repo", repo],
            check=False,
        )
        if result.returncode != 0:
            failures.append(f"{workflow}: {_first_stderr_line(result.stderr)}")
    if failures:
        return False, "; ".join(failures)
    return True, "sync.yml and pages.yml are enabled."


def _get_pages_build_type(repo: str) -> Optional[str]:
    result = _run(
        ["gh", "api", f"repos/{repo}/pages", "--jq", ".build_type"],
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip().lower()
    return value if value else None


def _try_configure_pages(repo: str) -> Tuple[bool, str]:
    current = _get_pages_build_type(repo)
    if current == "workflow":
        return True, "GitHub Pages already configured for GitHub Actions."

    attempts = [
        ["gh", "api", "-X", "PUT", f"repos/{repo}/pages", "-f", "build_type=workflow"],
        ["gh", "api", "-X", "POST", f"repos/{repo}/pages", "-f", "build_type=workflow"],
    ]
    errors = []
    for cmd in attempts:
        result = _run(cmd, check=False)
        if result.returncode == 0 and _get_pages_build_type(repo) == "workflow":
            return True, "GitHub Pages configured to deploy from GitHub Actions."
        if result.returncode != 0:
            errors.append(_first_stderr_line(result.stderr))

    final_build_type = _get_pages_build_type(repo)
    if final_build_type == "workflow":
        return True, "GitHub Pages configured to deploy from GitHub Actions."

    if errors:
        return False, "; ".join(errors)
    return False, "Unable to configure GitHub Pages build type automatically."


def _try_dispatch_sync(repo: str, source: str) -> Tuple[bool, str]:
    with_source = _run(
        ["gh", "workflow", "run", "sync.yml", "--repo", repo, "-f", f"source={source}"],
        check=False,
    )
    if with_source.returncode == 0:
        return True, "Dispatched sync.yml via workflow_dispatch."

    stderr_line = _first_stderr_line(with_source.stderr)
    if "Unexpected inputs provided" in stderr_line and "source" in stderr_line:
        without_source = _run(
            ["gh", "workflow", "run", "sync.yml", "--repo", repo],
            check=False,
        )
        if without_source.returncode == 0:
            return (
                True,
                "Dispatched sync.yml via workflow_dispatch (workflow does not declare 'source' input; using DASHBOARD_SOURCE variable/default).",
            )
        return False, _first_stderr_line(without_source.stderr)

    return False, stderr_line


def _try_dispatch_pages(repo: str) -> Tuple[bool, str]:
    result = _run(["gh", "workflow", "run", "pages.yml", "--repo", repo], check=False)
    if result.returncode != 0:
        return False, _first_stderr_line(result.stderr)
    return True, "Dispatched pages.yml via workflow_dispatch."


def _watch_run(repo: str, run_id: int) -> Tuple[bool, str]:
    print(f"\nWatching workflow run {run_id}...")
    watch = subprocess.run(
        ["gh", "run", "watch", str(run_id), "--repo", repo, "--exit-status"],
        check=False,
    )
    if watch.returncode == 0:
        return True, "Workflow run completed (see output above)."
    return False, "Workflow run failed or could not be watched to completion."


def _find_latest_workflow_run(
    repo: str,
    workflow: str,
    event: str,
    not_before: datetime,
    poll_attempts: int = 12,
    sleep_seconds: int = 2,
    progress_label: Optional[str] = None,
) -> Tuple[Optional[int], Optional[str]]:
    if progress_label:
        timeout_seconds = poll_attempts * sleep_seconds
        print(f"\nWaiting for {progress_label} (up to {timeout_seconds}s)...")
    for attempt in range(1, poll_attempts + 1):
        result = _run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                workflow,
                "--event",
                event,
                "--limit",
                "10",
                "--json",
                "databaseId,url,createdAt",
            ],
            check=False,
        )
        if result.returncode == 0:
            try:
                runs = json.loads(result.stdout or "[]")
            except json.JSONDecodeError:
                runs = []
            for run in runs:
                created_at = _parse_iso8601_utc(str(run.get("createdAt", "")))
                if created_at is None:
                    continue
                if created_at >= not_before:
                    run_id = run.get("databaseId")
                    run_url = run.get("url")
                    if isinstance(run_id, int):
                        if progress_label:
                            print(f"Detected {progress_label}.")
                        return run_id, str(run_url) if run_url else None
        if progress_label and (attempt == 1 or attempt % 5 == 0):
            print(f"Still waiting for {progress_label}... ({attempt}/{poll_attempts})")
        time.sleep(sleep_seconds)
    return None, None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap provider auth and automate GitHub setup for this repository."
    )
    parser.add_argument(
        "--source",
        choices=["strava", "garmin"],
        default=None,
        help="Activity source to configure.",
    )
    parser.add_argument(
        "--no-bootstrap-env",
        action="store_true",
        help="Skip automatic local virtualenv bootstrap (.venv + requirements install).",
    )
    parser.add_argument(
        "--env-bootstrapped",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--client-id", default=None, help="Strava client ID.")
    parser.add_argument(
        "--client-secret",
        default=None,
        help="Strava client secret.",
    )
    parser.add_argument(
        "--garmin-token-store-b64",
        default=None,
        help="Garmin token store as base64 (optional; generated from email/password if omitted).",
    )
    parser.add_argument(
        "--garmin-email",
        default=None,
        help="Garmin account email (used to generate GARMIN_TOKENS_B64 when token is omitted).",
    )
    parser.add_argument(
        "--garmin-password",
        default=None,
        help="Garmin account password (used to generate GARMIN_TOKENS_B64 when token is omitted).",
    )
    parser.add_argument(
        "--store-garmin-password-secrets",
        action="store_true",
        help="Deprecated: GARMIN_EMAIL and GARMIN_PASSWORD are now stored automatically when provided.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Optional GitHub repo in OWNER/REPO form. If omitted, the script auto-detects it.",
    )
    parser.add_argument(
        "--unit-system",
        choices=["us", "metric"],
        default=None,
        help="Units preset for dashboard metrics.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Local callback port.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="Seconds to wait for OAuth callback.",
    )
    parser.add_argument(
        "--scope",
        default="read,activity:read_all",
        help="Strava OAuth scopes.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open browser; print auth URL only.",
    )
    parser.add_argument(
        "--no-auto-github",
        action="store_true",
        help="Skip GitHub Pages/workflow automation after setting secrets and units.",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Do not watch the first workflow run after dispatching it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _bootstrap_env_and_reexec(args)
    interactive = _isatty()

    if args.port < 1 or args.port > 65535:
        raise ValueError("--port must be between 1 and 65535.")
    if args.timeout <= 0:
        raise ValueError("--timeout must be a positive number of seconds.")

    _assert_gh_ready()

    repo = _resolve_repo_slug(args.repo)
    if not repo:
        if interactive:
            while True:
                response = input("GitHub repository (OWNER/REPO): ").strip()
                repo = _normalize_repo_slug(response)
                if repo:
                    break
                print("Please enter repository as OWNER/REPO.")
        else:
            raise RuntimeError(
                "Unable to determine repository in non-interactive mode. "
                "Re-run with --repo OWNER/REPO."
            )
    _assert_repo_access(repo)
    print(f"Using repository: {repo}")
    source = _resolve_source(args, interactive)

    distance_unit, elevation_unit = _resolve_units(args, interactive)

    print("\nUpdating repository secrets via gh...")
    configured_secret_names: list[str] = []
    athlete_name = ""
    if source == "strava":
        if interactive and not args.client_id:
            print("\nEnter your Strava API credentials from https://www.strava.com/settings/api")
        if not interactive and not args.client_id:
            raise RuntimeError("Missing STRAVA_CLIENT_ID in non-interactive mode. Re-run with --client-id.")
        if not interactive and not args.client_secret:
            raise RuntimeError("Missing STRAVA_CLIENT_SECRET in non-interactive mode. Re-run with --client-secret.")

        client_id = _prompt(args.client_id, "STRAVA_CLIENT_ID")
        client_secret = _prompt(args.client_secret, "STRAVA_CLIENT_SECRET", secret=True)
        if not client_id or not client_secret:
            if interactive:
                raise ValueError("Both STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET are required.")
            raise RuntimeError(
                "Missing Strava credentials in non-interactive mode. "
                "Provide both --client-id and --client-secret."
            )

        redirect_uri = f"http://localhost:{args.port}{CALLBACK_PATH}"
        code = _authorize_and_get_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=args.scope,
            port=args.port,
            timeout_seconds=args.timeout,
            open_browser=not args.no_browser,
        )

        tokens = _exchange_code_for_tokens(client_id, client_secret, code)
        refresh_token = tokens["refresh_token"]

        _set_secret("STRAVA_CLIENT_ID", client_id, repo)
        _set_secret("STRAVA_CLIENT_SECRET", client_secret, repo)
        _set_secret("STRAVA_REFRESH_TOKEN", refresh_token, repo)
        configured_secret_names.extend(
            ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"]
        )
        athlete = tokens.get("athlete") or {}
        athlete_name = " ".join(
            [str(athlete.get("firstname", "")).strip(), str(athlete.get("lastname", "")).strip()]
        ).strip()
    elif source == "garmin":
        token_store_b64, garmin_email, garmin_password = _resolve_garmin_auth_values(args, interactive)
        if token_store_b64:
            _set_secret("GARMIN_TOKENS_B64", token_store_b64, repo)
            configured_secret_names.append("GARMIN_TOKENS_B64")
        if garmin_email and garmin_password:
            _set_secret("GARMIN_EMAIL", garmin_email, repo)
            _set_secret("GARMIN_PASSWORD", garmin_password, repo)
            configured_secret_names.extend(["GARMIN_EMAIL", "GARMIN_PASSWORD"])
    else:
        raise RuntimeError(f"Unsupported source: {source}")

    steps: list[StepResult] = []
    repo_url = f"https://github.com/{repo}"
    workflow_url = f"{repo_url}/actions/workflows/sync.yml"
    pages_url = f"{repo_url}/settings/pages"
    actions_url = f"{repo_url}/actions"
    actions_settings_url = f"{repo_url}/settings/actions"
    variables_settings_url = f"{repo_url}/settings/variables/actions"

    variable_errors = []
    print("Updating repository variables via gh...")
    for name, value in [
        ("DASHBOARD_SOURCE", source),
        ("DASHBOARD_DISTANCE_UNIT", distance_unit),
        ("DASHBOARD_ELEVATION_UNIT", elevation_unit),
    ]:
        try:
            _set_variable(name, value, repo)
        except RuntimeError as exc:
            variable_errors.append(str(exc))

    if variable_errors:
        _add_step(
            steps,
            name="Store dashboard variables",
            status=STATUS_MANUAL_REQUIRED,
            detail=f"Could not store one or more dashboard variables automatically: {variable_errors[0]}",
            manual_help=(
                f"Open {variables_settings_url} and set DASHBOARD_SOURCE={source}, "
                f"DASHBOARD_DISTANCE_UNIT={distance_unit} "
                f"and DASHBOARD_ELEVATION_UNIT={elevation_unit}."
            ),
        )
    else:
        _add_step(
            steps,
            name="Store dashboard variables",
            status=STATUS_OK,
            detail=(
                "Saved DASHBOARD_SOURCE="
                f"{source}, DASHBOARD_DISTANCE_UNIT={distance_unit}, "
                f"DASHBOARD_ELEVATION_UNIT={elevation_unit}."
            ),
        )
    print("\nCredentials configured.")
    if athlete_name:
        print(f"Authorized athlete: {athlete_name}")
    print(f"Source set: {source}")
    if configured_secret_names:
        print(f"Secrets set: {', '.join(configured_secret_names)}")
    if not variable_errors:
        print(
            "Variables set: "
            f"DASHBOARD_SOURCE={source}, DASHBOARD_DISTANCE_UNIT={distance_unit}, "
            f"DASHBOARD_ELEVATION_UNIT={elevation_unit}"
        )

    if args.no_auto_github:
        _add_step(
            steps,
            name="GitHub automation",
            status=STATUS_SKIPPED,
            detail="Skipped (--no-auto-github).",
            manual_help=f"Run the workflow manually: {workflow_url}",
        )
    else:
        enabled, detail = _try_enable_actions_permissions(repo)
        _add_step(
            steps,
            name="Actions permissions",
            status=STATUS_OK if enabled else STATUS_MANUAL_REQUIRED,
            detail=detail if enabled else f"Could not configure automatically: {detail}",
            manual_help=None if enabled else f"Open {actions_settings_url} and allow Actions/workflows.",
        )

        workflows_enabled, workflow_detail = _try_enable_workflows(repo, ["sync.yml", "pages.yml"])
        _add_step(
            steps,
            name="Enable workflows",
            status=STATUS_OK if workflows_enabled else STATUS_MANUAL_REQUIRED,
            detail=workflow_detail if workflows_enabled else f"Could not enable automatically: {workflow_detail}",
            manual_help=None if workflows_enabled else f"Open {actions_url} and click 'Enable workflows' if shown.",
        )

        pages_configured, pages_detail = _try_configure_pages(repo)
        _add_step(
            steps,
            name="GitHub Pages source",
            status=STATUS_OK if pages_configured else STATUS_MANUAL_REQUIRED,
            detail=pages_detail if pages_configured else f"Could not configure automatically: {pages_detail}",
            manual_help=None if pages_configured else f"Open {pages_url} and set Source to 'GitHub Actions'.",
        )

        dispatch_started_at = datetime.now(timezone.utc)
        dispatched, dispatch_detail = _try_dispatch_sync(repo, source)
        _add_step(
            steps,
            name="Run first sync workflow",
            status=STATUS_OK if dispatched else STATUS_MANUAL_REQUIRED,
            detail=dispatch_detail if dispatched else f"Could not dispatch automatically: {dispatch_detail}",
            manual_help=None if dispatched else f"Open {workflow_url} and click 'Run workflow'.",
        )

        run_id: Optional[int] = None
        run_url: Optional[str] = None
        sync_watch_ok = False
        if dispatched:
            run_id, run_url = _find_latest_workflow_run(
                repo=repo,
                workflow="sync.yml",
                event="workflow_dispatch",
                not_before=dispatch_started_at,
                progress_label="Sync workflow run",
            )
            if run_url:
                _add_step(
                    steps,
                    name="Locate run URL",
                    status=STATUS_OK,
                    detail=f"Workflow run URL: {run_url}",
                )
            else:
                _add_step(
                    steps,
                    name="Locate run URL",
                    status=STATUS_MANUAL_REQUIRED,
                    detail="Dispatched workflow but could not resolve run URL automatically.",
                    manual_help=f"Open {workflow_url} to view the latest run.",
                )

            if args.no_watch:
                _add_step(
                    steps,
                    name="Watch workflow run",
                    status=STATUS_SKIPPED,
                    detail="Skipped (--no-watch).",
                    manual_help=run_url or workflow_url,
                )
            elif run_id is not None:
                watched, watch_detail = _watch_run(repo, run_id)
                sync_watch_ok = watched
                _add_step(
                    steps,
                    name="Watch workflow run",
                    status=STATUS_OK if watched else STATUS_MANUAL_REQUIRED,
                    detail=watch_detail,
                    manual_help=None if watched else (run_url or workflow_url),
                )
            else:
                _add_step(
                    steps,
                    name="Watch workflow run",
                    status=STATUS_SKIPPED,
                    detail="Skipped because run ID could not be determined.",
                    manual_help=workflow_url,
                )

            pages_run_id: Optional[int] = None
            pages_run_url: Optional[str] = None
            pages_workflow_url = f"{repo_url}/actions/workflows/pages.yml"
            pages_discovery_start = dispatch_started_at
            if args.no_watch:
                _add_step(
                    steps,
                    name="Watch Pages deploy",
                    status=STATUS_SKIPPED,
                    detail="Skipped (--no-watch).",
                    manual_help=pages_workflow_url,
                )
            elif run_id is None:
                _add_step(
                    steps,
                    name="Watch Pages deploy",
                    status=STATUS_SKIPPED,
                    detail="Skipped because sync run ID could not be determined.",
                    manual_help=pages_workflow_url,
                )
            elif not sync_watch_ok:
                _add_step(
                    steps,
                    name="Watch Pages deploy",
                    status=STATUS_SKIPPED,
                    detail="Skipped because sync run did not finish cleanly in CLI watch.",
                    manual_help=pages_workflow_url,
                )
            else:
                pages_run_id, pages_run_url = _find_latest_workflow_run(
                    repo=repo,
                    workflow="pages.yml",
                    event="workflow_run",
                    not_before=pages_discovery_start,
                    poll_attempts=75,
                    sleep_seconds=2,
                    progress_label="Pages deploy run",
                )
                if pages_run_url:
                    _add_step(
                        steps,
                        name="Locate Pages deploy run",
                        status=STATUS_OK,
                        detail=f"Deploy Pages run URL: {pages_run_url}",
                    )
                else:
                    manual_dispatched, manual_dispatch_detail = _try_dispatch_pages(repo)
                    if manual_dispatched:
                        pages_run_id, pages_run_url = _find_latest_workflow_run(
                            repo=repo,
                            workflow="pages.yml",
                            event="workflow_dispatch",
                            not_before=pages_discovery_start,
                            poll_attempts=30,
                            sleep_seconds=2,
                            progress_label="manual Pages deploy run",
                        )
                        if pages_run_url:
                            _add_step(
                                steps,
                                name="Locate Pages deploy run",
                                status=STATUS_OK,
                                detail=f"Deploy Pages run URL: {pages_run_url}",
                            )
                        else:
                            _add_step(
                                steps,
                                name="Locate Pages deploy run",
                                status=STATUS_MANUAL_REQUIRED,
                                detail=(
                                    "Could not find a Deploy Pages run after sync completed, and "
                                    "manual pages dispatch did not surface a run URL automatically."
                                ),
                                manual_help=pages_workflow_url,
                            )
                    else:
                        _add_step(
                            steps,
                            name="Locate Pages deploy run",
                            status=STATUS_MANUAL_REQUIRED,
                            detail=(
                                "Could not find a Deploy Pages run after sync completed, and "
                                f"automatic pages dispatch failed: {manual_dispatch_detail}"
                            ),
                            manual_help=pages_workflow_url,
                        )

                if pages_run_id is not None:
                    watched_pages, pages_watch_detail = _watch_run(repo, pages_run_id)
                    _add_step(
                        steps,
                        name="Watch Pages deploy",
                        status=STATUS_OK if watched_pages else STATUS_MANUAL_REQUIRED,
                        detail=pages_watch_detail if watched_pages else "Could not monitor Deploy Pages to completion.",
                        manual_help=None if watched_pages else (pages_run_url or pages_workflow_url),
                    )
                elif pages_run_url is not None:
                    _add_step(
                        steps,
                        name="Watch Pages deploy",
                        status=STATUS_MANUAL_REQUIRED,
                        detail="Found Deploy Pages run URL but could not resolve run ID for watch.",
                        manual_help=pages_run_url,
                    )

    print("\nSetup summary:")
    for step in steps:
        print(f"- [{step.status}] {step.name}: {step.detail}")
        if step.status == STATUS_MANUAL_REQUIRED and step.manual_help:
            print(f"  Manual: {step.manual_help}")

    dashboard_url = _pages_url_from_slug(repo)
    has_manual_steps = any(step.status == STATUS_MANUAL_REQUIRED for step in steps)
    if has_manual_steps:
        print("\nSetup completed with manual steps remaining.")
        print(f"Dashboard URL: {dashboard_url}")
    elif args.no_auto_github:
        print("\nSetup completed. GitHub automation was skipped (--no-auto-github).")
        print(f"Run sync.yml to publish, then open: {dashboard_url}")
    elif args.no_watch:
        print("\nSetup completed. Workflows were started but not watched (--no-watch).")
        print(f"Check Actions for completion, then open: {dashboard_url}")
    else:
        print(f"\nYour dashboard is now live at {dashboard_url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
