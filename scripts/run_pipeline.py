import argparse
import os
import re
import subprocess
from typing import Optional

from aggregate import aggregate as aggregate_func
from normalize import normalize as normalize_func
from sync_garmin import sync_garmin
from sync_strava import sync_strava
from utils import ensure_dir, load_config, normalize_source, write_json
from generate_heatmaps import generate as generate_heatmaps

README_MD = "README.md"
SOURCE_STATE_PATH = os.path.join("data", "source_state.json")
RESETTABLE_OUTPUTS = [
    os.path.join("data", "activities_normalized.json"),
    os.path.join("data", "daily_aggregates.json"),
    os.path.join("data", "last_sync_summary.json"),
    os.path.join("data", "last_sync_summary.txt"),
    os.path.join("site", "data.json"),
]
RESETTABLE_STATE_FILES = [
    os.path.join("data", "source_state.json"),
    os.path.join("data", "backfill_state.json"),
    os.path.join("data", "backfill_state_strava.json"),
    os.path.join("data", "backfill_state_garmin.json"),
    os.path.join("data", "athletes.json"),
    os.path.join("data", "athletes_strava.json"),
    os.path.join("data", "athletes_garmin.json"),
]
RESETTABLE_RAW_DIRS = [
    os.path.join("activities", "raw"),
    os.path.join("activities", "raw", "strava"),
    os.path.join("activities", "raw", "garmin"),
]
README_LIVE_SITE_RE = re.compile(
    r"(?im)^(-\s*(?:Live site:\s*\[Interactive Heatmaps\]|View the Interactive \[Activity Dashboard\])\()https?://[^)]+(\)\s*)$",
    re.IGNORECASE,
)


def _write_normalized(items):
    ensure_dir("data")
    write_json(os.path.join("data", "activities_normalized.json"), items)


def _write_aggregates(payload):
    ensure_dir("data")
    write_json(os.path.join("data", "daily_aggregates.json"), payload)


def _repo_slug_from_git() -> Optional[str]:
    env_slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env_slug and "/" in env_slug:
        return env_slug

    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    url = result.stdout.strip()
    # Handles:
    # - https://github.com/owner/repo.git
    # - git@github.com:owner/repo.git
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$", url)
    if not m:
        return None
    return f"{m.group('owner')}/{m.group('repo')}"


def _pages_url_from_slug(slug: str) -> str:
    owner, repo = slug.split("/", 1)
    if repo.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"


def _update_readme_live_site_link() -> None:
    if not os.path.exists(README_MD):
        return

    slug = _repo_slug_from_git()
    if not slug:
        return

    target_url = _pages_url_from_slug(slug)
    with open(README_MD, "r", encoding="utf-8") as f:
        content = f.read()

    updated = README_LIVE_SITE_RE.sub(rf"\1{target_url}\2", content, count=1)
    if updated == content:
        return

    with open(README_MD, "w", encoding="utf-8") as f:
        f.write(updated)


def _load_last_source() -> Optional[str]:
    if not os.path.exists(SOURCE_STATE_PATH):
        return None
    try:
        import json

        with open(SOURCE_STATE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("source")
    if not isinstance(value, str):
        return None
    return value


def _persist_source(source: str) -> None:
    ensure_dir("data")
    write_json(SOURCE_STATE_PATH, {"source": source})


def _clear_outputs_for_source_switch() -> None:
    for path in RESETTABLE_OUTPUTS:
        if os.path.exists(path):
            os.remove(path)


def _clear_state_for_source_switch() -> None:
    for path in RESETTABLE_STATE_FILES:
        if os.path.exists(path):
            os.remove(path)
    for path in RESETTABLE_RAW_DIRS:
        if os.path.isdir(path):
            import shutil

            shutil.rmtree(path)


def _reset_for_source_switch() -> None:
    _clear_outputs_for_source_switch()
    _clear_state_for_source_switch()


def _sync_for_source(source: str, dry_run: bool, prune_deleted: bool):
    if source == "strava":
        return sync_strava(dry_run=dry_run, prune_deleted=prune_deleted)
    if source == "garmin":
        return sync_garmin(dry_run=dry_run, prune_deleted=prune_deleted)
    raise ValueError(f"Unsupported source '{source}'")


def run_pipeline(
    skip_sync: bool,
    dry_run: bool,
    prune_deleted: bool,
    update_readme_link: bool,
) -> None:
    config = load_config()
    source = normalize_source(config.get("source", "strava"))
    previous_source = _load_last_source()
    if previous_source and previous_source != source:
        print(
            f"Source changed from {previous_source} to {source}; "
            "resetting persisted outputs, backfill state, and raw caches for a full fresh sync."
        )
        _reset_for_source_switch()
    elif (
        previous_source is None
        and source != "strava"
        and os.path.exists(os.path.join("data", "activities_normalized.json"))
    ):
        print(
            "No saved source marker found; resetting persisted outputs, backfill state, "
            "and raw caches to avoid mixed-source history."
        )
        _reset_for_source_switch()

    if not skip_sync:
        summary = _sync_for_source(source, dry_run=dry_run, prune_deleted=prune_deleted)
        print(f"Synced ({source}): {summary}")

    items = normalize_func()
    _write_normalized(items)

    aggregates = aggregate_func()
    _write_aggregates(aggregates)

    generate_heatmaps(write_svgs=False)
    if not dry_run:
        _persist_source(source)
    if update_readme_link:
        _update_readme_live_site_link()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run activity sync pipeline")
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prune-deleted", action="store_true")
    parser.add_argument(
        "--update-readme-link",
        action="store_true",
        help="Update README dashboard URL based on the current repository slug.",
    )
    args = parser.parse_args()

    run_pipeline(
        skip_sync=args.skip_sync,
        dry_run=args.dry_run,
        prune_deleted=args.prune_deleted,
        update_readme_link=args.update_readme_link,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
