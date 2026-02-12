# Workout --> GitHub Heatmap Dashboard

Turn your Strava or Garmin activities into GitHub-style contribution heatmaps.  
Automatically generates a free, interactive dashboard updated daily on GitHub Pages.  
**No coding required.**  

- View the Interactive [Activity Dashboard](https://aspain.github.io/git-sweaty/)
- Once setup is complete, this dashboard link will automatically update to your own GitHub Pages URL.


![Dashboard Preview](site/readme-preview-20260212b.png)

## Quick Start

### Python Environment Setup

`scripts/setup_auth.py` now auto-bootstraps a local `.venv` and installs dependencies before continuing.

Manual setup is still available if you prefer explicit environment control.

Use an isolated virtual environment to avoid system-package-manager conflicts (for example Homebrew/PEP 668 `externally-managed-environment` errors on macOS).

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows (Command Prompt):

```bat
py -3 -m venv .venv
.\.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `python3` is not available on your machine, replace it with `python` or `py -3`.

### Option 1 (Recommended): Run the setup script

Fastest path: fork, run one script, and let it configure the repository for you.

1. Fork this repo: [Fork this repository](../../fork)
2. Clone your fork and enter it:

   ```bash
   git clone https://github.com/<your-username>/<repo-name>.git
   cd <repo-name>
   ```
3. Sign in to GitHub CLI:

   ```bash
   gh auth login
   ```

4. Run setup:

   ```bash
   python scripts/setup_auth.py
   ```

   By default this command will:
      - create `.venv` if needed
      - install/update requirements inside `.venv`
      - re-launch setup inside that virtual environment

   To skip auto-bootstrap and use your current Python environment:

   ```bash
   python scripts/setup_auth.py --no-bootstrap-env
   ```

   To also store `GARMIN_EMAIL` and `GARMIN_PASSWORD` repo secrets (not required by default):

   ```bash
   python scripts/setup_auth.py --store-garmin-password-secrets
   ```

   Follow the terminal prompts and choose a source:
      - `strava` (OAuth flow with `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET`)
      - `garmin` (prompts for Garmin email/password, generates `GARMIN_TOKENS_B64`, and stores that secret)
      - unit preference (`US`, `Metric`, or `Custom`)

   If you choose `strava`, create a [Strava API application](https://www.strava.com/settings/api) first and set **Authorization Callback Domain** to `localhost`.

   The setup may take several minutes to complete when run for the first time.  
   If any automation step fails, the script prints steps to remedy the failed step.  
   Once the script succeeds, it will provide the URL for your dashboard.

### Option 2: Manual setup (no local clone required)

1. Fork this repo to your account: [Fork this repository](../../fork)

2. Add `DASHBOARD_SOURCE` repo variable (repo → [Settings → Secrets and variables → Actions](../../settings/variables/actions)):
   - `strava` or `garmin`

3. Add source-specific GitHub secrets (repo → [Settings → Secrets and variables → Actions](../../settings/secrets/actions)):
   - For `strava`:
      - `STRAVA_CLIENT_ID`
      - `STRAVA_CLIENT_SECRET`
      - `STRAVA_REFRESH_TOKEN`
      - You can generate/update the refresh token by running `python3 scripts/setup_auth.py --source strava` locally.
   - For `garmin`:
      - Preferred: `GARMIN_TOKENS_B64`
      - Optional fallback: `GARMIN_EMAIL` and `GARMIN_PASSWORD` (only needed if token secret is not used)

4. Enable GitHub Pages (repo → [Settings → Pages](../../settings/pages)):
   - Under **Build and deployment**, set **Source** to **GitHub Actions**.

5. Run [Sync Heatmaps](../../actions/workflows/sync.yml):
   - If GitHub shows an **Enable workflows** button in [Actions](../../actions), click it first.
   - Go to [Actions](../../actions) → [Sync Heatmaps](../../actions/workflows/sync.yml) → **Run workflow**.
   - Optional: override the source in `workflow_dispatch` input.
   - The same workflow is also scheduled in `.github/workflows/sync.yml` (daily at `15:00 UTC`).

6. Open your live site at `https://<your-username>.github.io/<repo-name>/` after deploy finishes.

### Unit Preference Precedence

- Option 1 stores your unit choice in repo variables:
  - `DASHBOARD_SOURCE`
  - `DASHBOARD_DISTANCE_UNIT`
  - `DASHBOARD_ELEVATION_UNIT`
- When those variables are set, workflow runs use them and override `config.yaml` units.
- If those variables are unset, workflow runs use `config.yaml` units (this is the default for Option 2/manual setup).
- To switch back to `config.yaml`-only unit control, delete those two repo variables in Settings → Secrets and variables → Actions, or:

  ```bash
  gh variable delete DASHBOARD_SOURCE
  gh variable delete DASHBOARD_DISTANCE_UNIT
  gh variable delete DASHBOARD_ELEVATION_UNIT
  ```

Both options run the same workflow, which will:
- restore persisted state from the `dashboard-data` branch (if present)
- sync raw activities into `activities/raw/<source>/` (local-only; not committed)
- normalize + merge into `data/activities_normalized.json` (persisted history)
- aggregate into `data/daily_aggregates.json`
- build `site/data.json`
- commit generated outputs to `dashboard-data` (not `main`)

## Updating Your Repository

- To pull in new updates and features from the original repo, use GitHub's **Sync fork** button on your fork's `main` branch.
- Activity data is stored on a dedicated `dashboard-data` branch and deployed from there, so generated outputs do not need to be committed on `main`.
- `main` is intentionally kept free of generated `data/` and `site/data.json` artifacts so fork sync stays cleaner.
- After syncing, run [Sync Heatmaps](../../actions/workflows/sync.yml) if you want your dashboard refreshed immediately.

## Sport Type Note

By default, all observed activity types from your selected source are included automatically when you run the workflow.
Normalization prefers `sport_type` and falls back to `type` when `sport_type` is unavailable.
Default config keeps raw type names as-is (no grouping buckets or alias remaps).
Source-specific fields are mapped into the same normalized schema before aggregation.
Garmin type keys are normalized to Strava-style canonical names (for example `running` -> `Run`, `indoor_cycling` -> `VirtualRide`) so provider choice does not change dashboard categorization.

To narrow the dashboard to specific sport types:
1. Edit [`config.yaml`](config.yaml) in your fork.
2. Set `activities.include_all_types: false`.
3. Set `activities.types` to only the type values you want.
4. Run [Sync Heatmaps](../../actions/workflows/sync.yml) again.

If you want "include everything except a few", keep `include_all_types: true` and set `activities.exclude_types`.

Example:

```yaml
activities:
  include_all_types: false
  types:
    - Run
    - Ride
    - WeightTraining
```

## Configuration (Optional)

Everything in this section is optional. Defaults work without changes.
Base settings live in `config.yaml`.

Key options:
- `source` (`strava` or `garmin`)
- `sync.start_date` (optional `YYYY-MM-DD` lower bound for history)
- `sync.lookback_years` (optional rolling lower bound; used only when `sync.start_date` is unset)
- `sync.recent_days` (sync recent activities even while backfilling)
- `sync.resume_backfill` (persist cursor to continue older pages across days)
- `sync.prune_deleted` (remove local activities no longer returned by the selected source in the current sync scope)
- `activities.types` (featured/allowed activity types shown first in UI; key name is historical)
- `activities.include_all_types` (when `true`, include all seen sport types; when `false`, include only `activities.types`)
- `activities.exclude_types` (optional `SportType` names to exclude without disabling inclusion of future new types)
- `activities.group_other_types` (when `true`, allow non-Strava grouping buckets like `WaterSports`; default `false`)
- `activities.other_bucket` (fallback group name when no smart match is found)
- `activities.group_aliases` (optional explicit map of a raw/canonical type to a group)
- `activities.type_aliases` (optional map from raw source `sport_type`/`type` values to canonical names)
- `units.distance` (`mi` or `km`)
- `units.elevation` (`ft` or `m`)
- `rate_limits.*` (Strava API throttling caps; ignored for Garmin)

## Notes

- Raw activities are stored locally for processing but are not committed (`activities/raw/` is ignored). This prevents publishing detailed per-activity payloads and GPS location traces.
- If neither `sync.start_date` nor `sync.lookback_years` is set, sync backfills all available history from the selected source.
- A source marker (`data/source_state.json`) is persisted so switching from Strava to Garmin (or back) resets persisted outputs and avoids mixed-source history.
- Strava backfill state is stored in `data/backfill_state_strava.json`; Garmin backfill state is stored in `data/backfill_state_garmin.json`.
- Manual workflow runs include a `full_backfill` toggle that clears persisted pipeline outputs and source backfill cursors before syncing.
- The GitHub Pages site is optimized for responsive desktop/mobile viewing.
