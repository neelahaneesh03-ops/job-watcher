# Job watcher

Runs every hour on GitHub (free) and emails you **only the jobs that appeared since the last check**.

| Source | What it covers |
|---|---|
| LinkedIn "past hour" search | Most big companies. Jobs at companies in `companies.csv` (your 500-company list) are flagged at the top. |
| YC startups (~1,400 hiring, US/remote) | Checked **directly on each company's own job board** (Ashby, Greenhouse, Lever, or its ycombinator.com jobs page). Many of these never get posted to LinkedIn. Small, recent, AI/ML/CV/robotics/biotech startups get a ⭐ gem badge. |

Filters (role titles, US-only, gem rules, LinkedIn searches) live in `config.toml`.

## Setup (about 10 minutes)

1. **Create a repo.** On github.com: New repository → name it `job-watcher` → **Public** (unlimited free Actions minutes) → Create.
   Then "uploading an existing file" → drag in `watcher.py`, `config.toml`, `companies.csv`, `README.md` → Commit.
   Then **Add file → Create new file**, type the name `.github/workflows/watch.yml` (the slashes create folders), paste in the contents of `watch.yml` from this zip → Commit.
   *(Hidden folders like `.github` often get skipped by drag-and-drop, so this way is the safe one.)*
2. **Make a Gmail app password.** Google Account → Security → turn on 2-Step Verification if it isn't already → search "App passwords" → create one named `job-watcher` → copy the 16-character password.
3. **Add secrets.** Repo → Settings → Secrets and variables → Actions → New repository secret:
   - `GMAIL_USER`: your Gmail address
   - `GMAIL_APP_PASSWORD`: the 16-character app password
   - `TO_EMAIL`: where to send the list (optional; defaults to `GMAIL_USER`)
   - `NTFY_TOPIC`: optional phone push alerts. Install the **ntfy** app, subscribe to a hard-to-guess topic name like `haneesh-jobs-8x2k`, and put the same name here.
4. **Test it.** Repo → Actions → job-watcher → **Run workflow**.
   - The first run takes ~5–10 minutes. It figures out which job board each YC company uses and saves a baseline. You'll get a "Job watcher is live" email.
   - After that it runs every hour at :07 and emails only new postings. Runs with nothing new send no email.

## Checking it's working
Open the latest run in the Actions tab and expand **Check for new jobs**. You should see lines like:
```
LinkedIn: 140 postings in window
YC: 1400 hiring companies in your regions
YC: 870 companies on Ashby/Greenhouse/Lever, 5200 open jobs total
```
If `LinkedIn: 0` shows up every hour, LinkedIn is blocking GitHub's servers. The YC part keeps working either way.

All matches are also logged in `state/new_jobs_log.csv` in the repo.

## Notes
- Private repo instead? Change the cron line in `.github/workflows/watch.yml` to `7 */2 * * *` and `window_seconds` in `config.toml` to `7200`, or you'll go over the 2,000 free minutes a month.
- GitHub's scheduled runs can start 5–20 minutes late at busy times.
- The YC company list comes from the open-source `yc-oss/api` dataset and refreshes every run.
