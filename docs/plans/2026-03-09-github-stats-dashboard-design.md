# GitHub Stats Dashboard Design

## Overview
Single Python script (`stats.py`) that fetches GitHub activity data via `gh api graphql`, displays a colorful terminal summary, and generates a self-contained HTML dashboard.

## Data Sources (all via `gh api graphql`)
- Contribution calendar (full year)
- Commit, PR, issue, review counts
- All repos with languages, stars, sizes, push dates
- Contribution streaks

## Terminal Output
- ASCII contribution heatmap with ANSI colors
- Summary stats table
- Language breakdown bar chart
- Top 10 most active repos
- Streak info

## HTML Dashboard (`dashboard.html`)
- Self-contained single file, Chart.js via CDN
- Dark theme, modern design
- Sections: hero stats, heatmap, activity over time, language donut, top repos, work patterns

## Tech
- Python 3, zero external dependencies
- `subprocess` to call `gh`
- `json` for data processing
- String templates for HTML generation
