# Account Usage Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Publish each configured account's balance, cumulative usage, and daily usage as a static GitHub Pages dashboard without adding an external service.

**Architecture:** The check-in process writes a sanitized JSON snapshot when `STATS_OUTPUT_PATH` is configured. A standard-library build script merges that snapshot into one record per Asia/Shanghai calendar day, while GitHub Actions persists the generated site on a `stats-data` branch and deploys the same files to GitHub Pages.

**Tech Stack:** Python 3.11 standard library, pytest, vanilla HTML/CSS/JavaScript, GitHub Actions, GitHub Pages.

---

### Task 1: Statistics data model

**Files:**
- Create: `tests/test_stats.py`
- Create: `utils/stats.py`

**Step 1: Write failing tests**

Cover sanitized account records, deterministic account identifiers, snapshot output, same-day replacement, and cross-day history retention.

**Step 2: Verify RED**

Run: `uv run pytest tests/test_stats.py -q`

Expected: collection fails because `utils.stats` does not exist.

**Step 3: Implement minimal code**

Add pure functions for building account records, writing a snapshot, and updating `data/history.json`. Store no cookie, email, password, API user, or provider configuration data.

**Step 4: Verify GREEN**

Run: `uv run pytest tests/test_stats.py -q`

Expected: all statistics tests pass.

### Task 2: Check-in integration

**Files:**
- Modify: `checkin.py:476`
- Modify: `tests/test_stats.py`

**Step 1: Write a failing integration-level test**

Verify that account result data can be accumulated without requiring or exposing authentication fields.

**Step 2: Verify RED**

Run the targeted test and confirm the missing integration behavior causes the failure.

**Step 3: Implement minimal code**

Collect one statistics record per account in `main()` and write the snapshot only when `STATS_OUTPUT_PATH` is configured. Reuse the existing rounded `quota` and `used_quota` fields to avoid changing the HIGH-risk `get_user_info` path.

**Step 4: Verify GREEN**

Run: `uv run pytest tests/test_stats.py tests/test_checkin_state.py -q`

Expected: all targeted tests pass.

### Task 3: Static site builder and dashboard

**Files:**
- Create: `scripts/build_stats_site.py`
- Create: `web/index.html`
- Modify: `tests/test_stats.py`

**Step 1: Write failing history-builder tests**

Verify the builder creates `latest.json`, `history.json`, and copies the static page into the output directory.

**Step 2: Verify RED**

Run the targeted builder test and confirm it fails before implementation.

**Step 3: Implement minimal builder and page**

Build a responsive, dependency-free table with current balance, cumulative usage, today's usage, status, last update time, and a 30-day SVG sparkline for every account.

**Step 4: Verify GREEN**

Run: `uv run pytest tests/test_stats.py -q`

Expected: all statistics and site-generation tests pass.

### Task 4: GitHub persistence and Pages deployment

**Files:**
- Modify: `.github/workflows/checkin.yml`
- Modify: `README.md`

**Step 1: Configure snapshot generation**

Set `STATS_OUTPUT_PATH=.tmp/stats-snapshot.json` for the existing check-in step.

**Step 2: Persist generated files**

Restore or bootstrap a `stats-data` worktree, run the builder, commit only generated page/data files, and push with the workflow `GITHUB_TOKEN` using `contents: write`.

**Step 3: Deploy Pages**

Upload the generated site with `actions/upload-pages-artifact@v4`, then deploy it from a separate `github-pages` environment job using `actions/deploy-pages@v4` with `pages: write` and `id-token: write`.

**Step 4: Document one-time setup**

Explain that repository Pages source must be set to GitHub Actions and that published aliases and usage data are public unless GitHub Pages access control is available.

### Task 5: Verification

**Files:**
- Verify all changed files

**Step 1: Run tests and quality checks**

Run:
- `uv run pytest tests/`
- `uv run ruff check .`
- `uv run ruff format --check .`

**Step 2: Generate and inspect a sample site**

Use synthetic account values only, write output under `.tmp/`, serve it locally, and verify desktop and mobile layouts without real credentials.

**Step 3: Validate workflow syntax and GitNexus scope**

Parse the workflow as YAML using the existing environment where available, then run `node .gitnexus/run.cjs detect-changes -r anyrouter-check-in` and confirm only the expected statistics and check-in flows are affected.

**Step 4: Commit only when requested**

Use `<需求号>: 添加账号用量统计页面` after the user supplies the requirement number.
