# GitHub Stats Dashboard

Single Python script that fetches all your GitHub activity and generates a colorful terminal summary + self-contained HTML dashboard.

![Terminal + HTML](https://img.shields.io/badge/output-terminal%20%2B%20HTML-brightgreen)
![Python 3](https://img.shields.io/badge/python-3.8%2B-blue)
![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-orange)

## Features

- **All-time stats** across every year of your GitHub account
- **Contribution heatmap** (rolling year)
- **Year-over-year** commit/PR/repo trends
- **Lines of code** from actual diffs (additions/deletions), not byte estimates
- **LOC by month** with clickable drill-down per repo
- **Language breakdown** by codebase size
- **Repository table** — sortable, searchable, with expandable drill-down (weekly chart + recent commits)
- **Engineering work cards** — last 3 months of classified commits per repo (features, fixes, security, performance, etc.)
- **Fork detection** — correctly attributes only your contributions, not upstream commits
- Works with **private repos** (uses your `gh` auth)

## Prerequisites

- [GitHub CLI](https://cli.github.com/) (`gh`) installed and authenticated
- Python 3.8+

## Usage

```bash
python3 stats.py
```

This will:
1. Print a colorful terminal summary
2. Generate `dashboard.html` — open it in any browser

## Configuration

Edit the top of `stats.py` to exclude repos that skew LOC stats (e.g. bulk data imports):

```python
EXCLUDED_REPOS = {"my-bulk-data-repo"}
```

## How It Works

- **GraphQL API** (`gh api graphql`) for contributions, repos, languages, commit history across all years
- **REST API** (`gh api repos/...`) for per-user diff stats (weekly additions/deletions) and recent commits
- **Parallel fetching** with `ThreadPoolExecutor` — warm-up phase triggers GitHub's lazy stats computation, then bulk fetch
- **Zero external dependencies** — stdlib only (`subprocess`, `json`, `concurrent.futures`)
- **Self-contained HTML** — Chart.js loaded via CDN, everything else inline
