#!/usr/bin/env python3
"""GitHub Stats Dashboard — Terminal + HTML visualization of all GitHub activity."""

import subprocess
import json
import sys
import re
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Repos to exclude from stats (e.g. bulk data imports that skew LOC counts)
EXCLUDED_REPOS = {"epstein-files-visualizations"}

# ─── Data Fetching ───────────────────────────────────────────────────────────

def gh_graphql(query):
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout).get("data")


def gh_rest(endpoint, retries=3):
    for attempt in range(retries):
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict) and not data:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return None
        return data
    return None


def fetch_contributions_for_year(year):
    from_date = f"{year}-01-01T00:00:00Z"
    to_date = f"{year}-12-31T23:59:59Z"
    data = gh_graphql(f"""
    {{
      viewer {{
        contributionsCollection(from: "{from_date}", to: "{to_date}") {{
          totalCommitContributions
          totalPullRequestContributions
          totalIssueContributions
          totalPullRequestReviewContributions
          totalRepositoryContributions
          restrictedContributionsCount
          contributionCalendar {{
            totalContributions
            weeks {{
              contributionDays {{
                date
                contributionCount
                weekday
              }}
            }}
          }}
        }}
      }}
    }}
    """)
    if not data:
        return None
    cc = data["viewer"]["contributionsCollection"]
    cc["year"] = year
    return cc


def fetch_rolling_year_contributions():
    """Default contributionsCollection = rolling last 365 days."""
    data = gh_graphql("""
    {
      viewer {
        contributionsCollection {
          totalCommitContributions
          totalPullRequestContributions
          totalIssueContributions
          totalPullRequestReviewContributions
          totalRepositoryContributions
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                date
                contributionCount
                weekday
              }
            }
          }
        }
      }
    }
    """)
    if not data:
        return None
    return data["viewer"]["contributionsCollection"]


def fetch_viewer_info():
    data = gh_graphql("""
    {
      viewer {
        login
        name
        createdAt
        contributionsCollection {
          contributionYears
        }
      }
    }
    """)
    return data["viewer"]


def fetch_repos(cursor=None):
    after = f', after: "{cursor}"' if cursor else ""
    return gh_graphql(f"""
    {{
      viewer {{
        repositories(first: 100, ownerAffiliations: OWNER, orderBy: {{field: PUSHED_AT, direction: DESC}}{after}) {{
          totalCount
          pageInfo {{ hasNextPage endCursor }}
          nodes {{
            name
            isPrivate
            isFork
            pushedAt
            createdAt
            stargazerCount
            description
            primaryLanguage {{ name }}
            repositoryTopics(first: 10) {{ nodes {{ topic {{ name }} }} }}
            languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
              totalSize
              edges {{
                size
                node {{ name color }}
              }}
            }}
            defaultBranchRef {{
              target {{
                ... on Commit {{
                  history {{ totalCount }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}
    """)


def fetch_all_repos():
    all_repos = []
    cursor = None
    while True:
        data = fetch_repos(cursor)
        if not data:
            break
        repos = data["viewer"]["repositories"]
        all_repos.extend(repos["nodes"])
        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]
    return all_repos


def fetch_repo_stats_full(login, repo_name):
    """Fetch per-repo stats with weekly timeline data."""
    data = gh_rest(f"repos/{login}/{repo_name}/stats/contributors")
    result = {"additions": 0, "deletions": 0, "commits": 0, "weekly": []}

    if not data or not isinstance(data, list):
        return result

    # Find owner's data, fall back to aggregate
    target = None
    for contributor in data:
        if contributor.get("author", {}).get("login") == login:
            target = contributor
            break

    if target:
        result["commits"] = target["total"]
        result["additions"] = sum(w["a"] for w in target["weeks"])
        result["deletions"] = sum(w["d"] for w in target["weeks"])
        result["weekly"] = [
            {"week": w["w"], "additions": w["a"], "deletions": w["d"], "commits": w["c"]}
            for w in target["weeks"] if w["a"] > 0 or w["d"] > 0 or w["c"] > 0
        ]
    # If user not found in contributors, leave zeros — don't aggregate others' work

    return result


def fetch_repo_commits(login, repo_name, per_page=30):
    """Fetch recent commit messages for feature extraction."""
    data = gh_rest(f"repos/{login}/{repo_name}/commits?per_page={per_page}&author={login}")
    if not data or not isinstance(data, list):
        return []
    commits = []
    for c in data:
        msg = c.get("commit", {}).get("message", "")
        date = c.get("commit", {}).get("author", {}).get("date", "")
        first_line = msg.split("\n")[0].strip()
        if first_line and not first_line.startswith("Merge"):
            commits.append({"date": date[:10], "message": first_line})
    return commits


def fetch_all_data():
    print("  Fetching profile...")
    viewer = fetch_viewer_info()
    login = viewer["login"]
    name = viewer["name"] or login
    years = sorted(viewer["contributionsCollection"]["contributionYears"])

    # Fetch all years + rolling year in parallel
    print(f"  Fetching contributions for {len(years)} years ({years[0]}-{years[-1]})...")
    yearly_data = {}
    rolling_cal = None

    with ThreadPoolExecutor(max_workers=5) as pool:
        year_futures = {pool.submit(fetch_contributions_for_year, y): y for y in years}
        rolling_future = pool.submit(fetch_rolling_year_contributions)

        for future in as_completed(list(year_futures.keys()) + [rolling_future]):
            if future == rolling_future:
                rolling_cal = future.result()
            else:
                year = year_futures[future]
                result = future.result()
                if result:
                    yearly_data[year] = result

    print(f"  Fetching repositories...")
    repos = fetch_all_repos()

    # Trigger stats for all repos
    print(f"  Warming up diff stats for {len(repos)} repos...")
    repo_names = [r["name"] for r in repos]
    with ThreadPoolExecutor(max_workers=10) as pool:
        pool.map(lambda n: gh_rest(f"repos/{login}/{n}/stats/contributors", retries=1), repo_names)

    time.sleep(3)

    # Fetch full stats + commits for active repos
    print(f"  Fetching diff stats and commit history...")
    repo_stats = {}
    repo_commits = {}

    # Only fetch commits for repos with recent activity (top 30 by push date)
    active_repos = [r["name"] for r in repos[:30]]

    with ThreadPoolExecutor(max_workers=8) as pool:
        stat_futures = {pool.submit(fetch_repo_stats_full, login, r["name"]): r["name"] for r in repos}
        commit_futures = {pool.submit(fetch_repo_commits, login, n): n for n in active_repos}

        for future in as_completed(list(stat_futures.keys()) + list(commit_futures.keys())):
            if future in stat_futures:
                repo_stats[stat_futures[future]] = future.result()
            elif future in commit_futures:
                repo_commits[commit_futures[future]] = future.result()

    for repo in repos:
        repo["stats"] = repo_stats.get(repo["name"], {"additions": 0, "deletions": 0, "commits": 0, "weekly": []})
        repo["recent_commits"] = repo_commits.get(repo["name"], [])

    current_year = max(years)

    return {
        "login": login,
        "name": name,
        "created_at": viewer["createdAt"],
        "years": years,
        "yearly_data": yearly_data,
        "current_year": current_year,
        "current_contributions": yearly_data.get(current_year, {}),
        "rolling_calendar": rolling_cal.get("contributionCalendar", {"totalContributions": 0, "weeks": []}) if rolling_cal else {"totalContributions": 0, "weeks": []},
        "rolling_contributions": rolling_cal or {},
        "repos": repos,
        "total_repos": len(repos),
    }

# ─── Data Analysis ───────────────────────────────────────────────────────────

def analyze(data):
    cal = data["rolling_calendar"]
    days = []
    for week in cal.get("weeks", []):
        for day in week["contributionDays"]:
            days.append(day)

    # All-time totals
    alltime = {"commits": 0, "prs": 0, "issues": 0, "reviews": 0, "repos_created": 0, "contributions": 0}
    yearly_summary = []
    for year in sorted(data["years"]):
        yd = data["yearly_data"].get(year, {})
        commits = yd.get("totalCommitContributions", 0)
        prs = yd.get("totalPullRequestContributions", 0)
        issues = yd.get("totalIssueContributions", 0)
        reviews = yd.get("totalPullRequestReviewContributions", 0)
        repos = yd.get("totalRepositoryContributions", 0)
        total = yd.get("contributionCalendar", {}).get("totalContributions", 0)
        alltime["commits"] += commits
        alltime["prs"] += prs
        alltime["issues"] += issues
        alltime["reviews"] += reviews
        alltime["repos_created"] += repos
        alltime["contributions"] += total
        yearly_summary.append({
            "year": year, "commits": commits, "prs": prs, "issues": issues,
            "reviews": reviews, "repos_created": repos, "total": total,
        })

    # Streaks
    sorted_days = sorted(days, key=lambda d: d["date"], reverse=True)
    current_streak = 0
    for d in sorted_days:
        if d["contributionCount"] > 0:
            current_streak += 1
        else:
            break

    longest_streak = 0
    streak = 0
    for d in sorted(days, key=lambda d: d["date"]):
        if d["contributionCount"] > 0:
            streak += 1
            longest_streak = max(longest_streak, streak)
        else:
            streak = 0

    # Day of week
    day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    day_totals = defaultdict(int)
    for d in days:
        day_totals[d["weekday"]] += d["contributionCount"]

    # Monthly (rolling year)
    month_totals = defaultdict(int)
    for d in days:
        month_totals[d["date"][:7]] += d["contributionCount"]

    # Languages (skip excluded repos and forks with no user contributions)
    lang_sizes = defaultdict(int)
    lang_colors = {}
    for repo in data["repos"]:
        if repo["name"] in EXCLUDED_REPOS:
            continue
        if repo.get("isFork") and repo.get("stats", {}).get("commits", 0) == 0:
            continue
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            lang_sizes[name] += edge["size"]
            lang_colors[name] = edge["node"]["color"]

    top_languages = sorted(lang_sizes.items(), key=lambda x: x[1], reverse=True)[:15]
    total_lang_size = sum(s for _, s in top_languages) if top_languages else 1

    active_days = sum(1 for d in days if d["contributionCount"] > 0)
    total_days = len(days)
    max_day = max(days, key=lambda d: d["contributionCount"]) if days else {"date": "N/A", "contributionCount": 0}

    # Weekly contribution activity
    weekly_data = []
    for week in cal.get("weeks", []):
        total = sum(d["contributionCount"] for d in week["contributionDays"])
        date = week["contributionDays"][0]["date"]
        weekly_data.append({"date": date, "count": total})

    # ── LOC Timeline (aggregate weekly adds/dels across all repos) ──
    loc_timeline = defaultdict(lambda: {"additions": 0, "deletions": 0, "commits": 0})
    for repo in data["repos"]:
        if repo["name"] in EXCLUDED_REPOS:
            continue
        if repo.get("isFork") and repo.get("stats", {}).get("commits", 0) == 0:
            continue
        for w in repo.get("stats", {}).get("weekly", []):
            ts = w["week"]
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            loc_timeline[date_str]["additions"] += w["additions"]
            loc_timeline[date_str]["deletions"] += w["deletions"]
            loc_timeline[date_str]["commits"] += w["commits"]

    loc_timeline_sorted = [
        {"date": d, **v} for d, v in sorted(loc_timeline.items())
    ]

    # ── LOC by month (aggregate) ──
    loc_by_month = defaultdict(lambda: {"additions": 0, "deletions": 0, "commits": 0})
    for entry in loc_timeline_sorted:
        month_key = entry["date"][:7]
        loc_by_month[month_key]["additions"] += entry["additions"]
        loc_by_month[month_key]["deletions"] += entry["deletions"]
        loc_by_month[month_key]["commits"] += entry["commits"]

    loc_by_month_sorted = [
        {"month": m, **v} for m, v in sorted(loc_by_month.items())
    ]

    # ── Daily velocity (last 90 days from contribution calendar) ──
    daily_velocity = []
    if days:
        sorted_all = sorted(days, key=lambda d: d["date"])
        daily_velocity = sorted_all[-90:]  # last ~3 months

    # ── Per-repo details ──
    repo_details = []
    for repo in data["repos"]:
        if repo["name"] in EXCLUDED_REPOS:
            continue
        stats = repo.get("stats", {})
        is_fork = repo.get("isFork", False)

        # For commit count: prefer user-specific stats API data.
        # GraphQL history.totalCount includes ALL contributors (misleading for forks).
        stats_commits = stats.get("commits", 0)
        graphql_commits = 0
        if repo.get("defaultBranchRef") and repo["defaultBranchRef"].get("target"):
            graphql_commits = repo["defaultBranchRef"]["target"]["history"]["totalCount"]

        # Use stats API commit count (user-specific) when available; fall back to GraphQL only for non-forks
        if stats_commits > 0:
            commit_count = stats_commits
        elif is_fork:
            commit_count = 0  # fork with no user contributions
        else:
            commit_count = graphql_commits

        additions = stats.get("additions", 0)
        deletions = stats.get("deletions", 0)

        # Skip empty forks — no user contributions and no recent commits
        if is_fork and commit_count == 0 and additions == 0 and not repo.get("recent_commits"):
            continue

        loc = repo["languages"].get("totalSize", 0)
        lang_breakdown = [
            {"name": e["node"]["name"], "size": e["size"], "color": e["node"]["color"]}
            for e in repo["languages"]["edges"]
        ]
        topics = [t["topic"]["name"] for t in repo.get("repositoryTopics", {}).get("nodes", [])]

        repo_details.append({
            "name": repo["name"],
            "private": repo["isPrivate"],
            "fork": is_fork,
            "description": repo.get("description") or "",
            "language": repo["primaryLanguage"]["name"] if repo["primaryLanguage"] else "—",
            "stars": repo["stargazerCount"],
            "pushed": repo["pushedAt"][:10] if repo.get("pushedAt") else "—",
            "created": repo["createdAt"][:10] if repo.get("createdAt") else "—",
            "commits": commit_count,
            "additions": additions,
            "deletions": deletions,
            "size_bytes": loc,
            "loc": loc // 40,  # estimate ~40 bytes per line
            "languages": lang_breakdown,
            "topics": topics,
            "weekly_stats": stats.get("weekly", []),
            "recent_commits": repo.get("recent_commits", []),
        })

    # ── Features / milestones extraction ──
    features_by_month = defaultdict(list)
    all_features = []
    for rd in repo_details:
        for c in rd["recent_commits"]:
            month = c["date"][:7]
            entry = {"repo": rd["name"], "message": c["message"], "date": c["date"]}
            features_by_month[month].append(entry)
            # Count feature-like commits (feat:, add, create, build, implement, launch, ship, release, initial)
            msg_lower = c["message"].lower()
            if any(kw in msg_lower for kw in ["feat", "add ", "create", "build", "implement", "launch", "ship", "release", "initial commit"]):
                all_features.append(entry)

    features_by_month_sorted = sorted(features_by_month.items(), reverse=True)

    # ── Project cards (last 3 months, real engineering work) ──
    cutoff_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    # Classify commits by type using prefix conventions
    def classify_commit(msg):
        ml = msg.lower()
        if any(ml.startswith(p) for p in ["feat", "add ", "create", "implement", "build"]):
            return "feature"
        if any(ml.startswith(p) for p in ["fix", "bugfix", "hotfix"]):
            return "fix"
        if any(ml.startswith(p) for p in ["security", "secur", "harden"]):
            return "security"
        if any(ml.startswith(p) for p in ["perf", "optim"]):
            return "performance"
        if any(ml.startswith(p) for p in ["refactor", "clean", "simplify"]):
            return "refactor"
        if any(ml.startswith(p) for p in ["test", "spec"]):
            return "testing"
        if any(ml.startswith(p) for p in ["docs", "content", "readme"]):
            return "docs"
        if any(ml.startswith(p) for p in ["style", "ui", "ux", "design"]):
            return "design"
        if any(kw in ml for kw in ["feat", "feature", "ship", "launch", "release", "initial"]):
            return "feature"
        if any(kw in ml for kw in ["secur", "csp", "xss", "inject", "sanitiz", "harden"]):
            return "security"
        return "other"

    project_cards = []
    for rd in repo_details:
        recent = [c for c in rd["recent_commits"] if c["date"] >= cutoff_date]
        if not recent:
            continue

        # Classify commits
        classified = defaultdict(list)
        for c in recent:
            cat = classify_commit(c["message"])
            classified[cat].append(c["message"])

        # Extract tech stack from languages
        tech_stack = [l["name"] for l in rd["languages"][:5]]

        # Build work summary — deduplicated, grouped by type
        work_items = []
        type_labels = {
            "feature": "Features shipped",
            "fix": "Bugs fixed",
            "security": "Security hardening",
            "performance": "Performance",
            "refactor": "Refactoring",
            "design": "Design & UX",
            "testing": "Testing",
            "docs": "Documentation",
        }
        for cat_key in ["feature", "security", "performance", "design", "fix", "refactor", "testing", "docs"]:
            msgs = classified.get(cat_key, [])
            if msgs:
                unique = list(dict.fromkeys(msgs))[:5]
                work_items.append({
                    "category": type_labels.get(cat_key, cat_key),
                    "items": unique,
                })

        # Get 3-month diff stats from weekly data
        recent_additions = 0
        recent_deletions = 0
        for w in rd.get("weekly_stats", []):
            wdate = datetime.fromtimestamp(w["week"], tz=timezone.utc).strftime("%Y-%m-%d")
            if wdate >= cutoff_date:
                recent_additions += w["additions"]
                recent_deletions += w["deletions"]

        project_cards.append({
            "name": rd["name"],
            "private": rd["private"],
            "description": rd["description"],
            "tech_stack": tech_stack,
            "language": rd["language"],
            "commits_recent": len(recent),
            "additions_recent": recent_additions,
            "deletions_recent": recent_deletions,
            "work_items": work_items,
            "date_range": f"{recent[-1]['date']} → {recent[0]['date']}",
        })

    # Sort by recent commit count (most active first)
    project_cards.sort(key=lambda p: p["commits_recent"], reverse=True)

    # ── Per-repo monthly LOC breakdown (for LOC chart drill-down) ──
    loc_by_month_by_repo = defaultdict(lambda: defaultdict(lambda: {"additions": 0, "deletions": 0}))
    for repo in data["repos"]:
        if repo["name"] in EXCLUDED_REPOS:
            continue
        if repo.get("isFork") and repo.get("stats", {}).get("commits", 0) == 0:
            continue
        for w in repo.get("stats", {}).get("weekly", []):
            if w["additions"] > 0 or w["deletions"] > 0:
                ts = w["week"]
                month_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
                loc_by_month_by_repo[month_key][repo["name"]]["additions"] += w["additions"]
                loc_by_month_by_repo[month_key][repo["name"]]["deletions"] += w["deletions"]
    # Convert to sorted serializable
    loc_month_repo_breakdown = {}
    for month, repos in sorted(loc_by_month_by_repo.items()):
        loc_month_repo_breakdown[month] = sorted(
            [{"repo": r, **v} for r, v in repos.items()],
            key=lambda x: x["additions"] + x["deletions"], reverse=True
        )

    # Count active repos (repos with actual commits)
    active_repos_count = sum(1 for r in repo_details if r["commits"] > 0)

    total_additions = sum(r["additions"] for r in repo_details)
    total_deletions = sum(r["deletions"] for r in repo_details)
    total_size_bytes = sum(r["size_bytes"] for r in repo_details)

    repo_details_by_impact = sorted(repo_details, key=lambda r: r["additions"] + r["deletions"], reverse=True)

    return {
        "days": days,
        "alltime": alltime,
        "yearly_summary": yearly_summary,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "day_totals": {day_names[k]: v for k, v in sorted(day_totals.items())},
        "month_totals": dict(sorted(month_totals.items())),
        "top_languages": top_languages,
        "lang_colors": lang_colors,
        "total_lang_size": total_lang_size,
        "active_days": active_days,
        "total_days": total_days,
        "max_day": max_day,
        "weekly_data": weekly_data,
        "loc_timeline": loc_timeline_sorted,
        "loc_by_month": loc_by_month_sorted,
        "repo_details": repo_details,
        "repo_details_by_impact": repo_details_by_impact,
        "features_by_month": features_by_month_sorted,
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "total_size_bytes": total_size_bytes,
        "features_shipped": len(all_features),
        "active_repos": active_repos_count,
        "daily_velocity": daily_velocity,
        "project_cards": project_cards,
        "loc_month_repo_breakdown": loc_month_repo_breakdown,
    }

# ─── Terminal Output ─────────────────────────────────────────────────────────

def rgb(r, g, b):
    return f"\033[38;2;{r};{g};{b}m"

def bg_rgb(r, g, b):
    return f"\033[48;2;{r};{g};{b}m"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREENS = [(22,27,34),(14,68,41),(0,109,50),(38,166,65),(57,211,83)]

def heatmap_color(count, max_count):
    if count == 0: return GREENS[0]
    ratio = count / max(max_count, 1)
    if ratio < 0.25: return GREENS[1]
    elif ratio < 0.5: return GREENS[2]
    elif ratio < 0.75: return GREENS[3]
    else: return GREENS[4]

def fmt_num(n):
    return f"{n:,}"


def print_terminal(data, analysis):
    cal = data["rolling_calendar"]
    at = analysis["alltime"]

    print()
    print(f"  {BOLD}{rgb(57,211,83)}╔══════════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"  {BOLD}{rgb(57,211,83)}║  {rgb(255,255,255)}GitHub Stats for {data['name']} (@{data['login']}){RESET}")
    print(f"  {BOLD}{rgb(57,211,83)}║  {DIM}Member since {data['created_at'][:4]} · {data['total_repos']} repositories · {len(data['years'])} years{RESET}")
    print(f"  {BOLD}{rgb(57,211,83)}╚══════════════════════════════════════════════════════════════════════╝{RESET}")
    print()

    # ── All-Time ──
    print(f"  {BOLD}ALL-TIME{RESET}")
    print(f"  {rgb(57,211,83)}{BOLD}{fmt_num(at['contributions'])}{RESET} {DIM}contributions{RESET}  ·  "
          f"{rgb(57,211,83)}{BOLD}{fmt_num(at['commits'])}{RESET} {DIM}commits{RESET}  ·  "
          f"{rgb(57,211,83)}{BOLD}{fmt_num(at['prs'])}{RESET} {DIM}PRs{RESET}  ·  "
          f"{rgb(57,211,83)}{BOLD}{fmt_num(at['repos_created'])}{RESET} {DIM}repos{RESET}")
    print(f"  {rgb(57,211,83)}{BOLD}+{fmt_num(analysis['total_additions'])}{RESET} {DIM}lines added{RESET}  ·  "
          f"{rgb(255,99,71)}{BOLD}-{fmt_num(analysis['total_deletions'])}{RESET} {DIM}deleted{RESET}  ·  "
          f"{rgb(88,166,255)}{BOLD}{fmt_num(analysis['total_additions'] - analysis['total_deletions'])}{RESET} {DIM}net LOC{RESET}")
    print()

    # ── Year-over-Year ──
    print(f"  {BOLD}YEAR-OVER-YEAR{RESET}")
    max_yearly = max((y["total"] for y in analysis["yearly_summary"]), default=1)
    for ys in analysis["yearly_summary"]:
        bar_len = int((ys["total"] / max_yearly) * 35)
        detail = f'{ys["commits"]}c {ys["prs"]}pr {ys["repos_created"]}r'
        print(f"  {DIM}{ys['year']}{RESET} {rgb(57,211,83)}{'█' * bar_len}{RESET} {DIM}{ys['total']:>4} ({detail}){RESET}")
    print()

    # ── Rolling Year ──
    print(f"  {BOLD}ROLLING YEAR{RESET}")
    print(f"  {rgb(57,211,83)}{BOLD}{cal.get('totalContributions', 0)}{RESET} {DIM}contributions{RESET}  ·  "
          f"{rgb(57,211,83)}{BOLD}{analysis['active_days']}{RESET}{DIM}/{analysis['total_days']} active days{RESET}  ·  "
          f"{rgb(57,211,83)}{BOLD}{analysis['current_streak']}{RESET} {DIM}day streak{RESET}  ·  "
          f"{rgb(57,211,83)}{BOLD}{analysis['longest_streak']}{RESET} {DIM}longest streak{RESET}")
    print()

    # ── Heatmap ──
    print(f"  {BOLD}CONTRIBUTION HEATMAP{RESET}")
    weeks = cal.get("weeks", [])
    if weeks:
        max_count = max(d["contributionCount"] for w in weeks for d in w["contributionDays"])
        day_labels = ["", "Mon", "", "Wed", "", "Fri", ""]
        month_header = "     "
        last_month = ""
        for week in weeks:
            first_day = week["contributionDays"][0]
            month = datetime.strptime(first_day["date"], "%Y-%m-%d").strftime("%b")
            if month != last_month:
                month_header += month
                last_month = month
            else:
                month_header += "  "
        print(f"  {DIM}{month_header[:80]}{RESET}")
        for weekday in range(7):
            label = day_labels[weekday]
            row = f"  {DIM}{label:>3}{RESET} "
            for week in weeks:
                for day in week["contributionDays"]:
                    if day["weekday"] == weekday:
                        c = day["contributionCount"]
                        r, g, b = heatmap_color(c, max_count)
                        row += f"{bg_rgb(r, g, b)}  {RESET}"
            print(row)
        legend = f"  {DIM}    Less {RESET}"
        for shade in GREENS:
            legend += f"{bg_rgb(*shade)}  {RESET}"
        legend += f" {DIM}More{RESET}"
        print(legend)
    print()

    # ── LOC by Month ──
    print(f"  {BOLD}LINES OF CODE BY MONTH (recent){RESET}")
    recent_months = analysis["loc_by_month"][-12:]
    if recent_months:
        max_m = max(m["additions"] for m in recent_months) or 1
        for m in recent_months:
            bar_a = int((m["additions"] / max_m) * 25)
            net = m["additions"] - m["deletions"]
            net_str = f"+{fmt_num(net)}" if net >= 0 else fmt_num(net)
            print(f"  {DIM}{m['month']}{RESET} {rgb(57,211,83)}{'█' * bar_a}{RESET} "
                  f"{DIM}+{fmt_num(m['additions'])} -{fmt_num(m['deletions'])} (net {net_str}){RESET}")
    print()

    # ── Languages ──
    print(f"  {BOLD}LANGUAGES{RESET}")
    if analysis["top_languages"]:
        max_lang_size = analysis["top_languages"][0][1]
        for lang, size in analysis["top_languages"][:10]:
            pct = (size / analysis["total_lang_size"]) * 100
            bar_len = int((size / max_lang_size) * 30)
            color_hex = analysis["lang_colors"].get(lang, "#888888")
            r, g, b = int(color_hex[1:3], 16), int(color_hex[3:5], 16), int(color_hex[5:7], 16)
            bar = f"{rgb(r,g,b)}{'█' * bar_len}{RESET}"
            print(f"  {lang:>15} {bar} {DIM}{pct:.1f}%{RESET}")
    print()

    # ── Top Repos ──
    print(f"  {BOLD}TOP REPOSITORIES BY CODE IMPACT{RESET}")
    print(f"  {DIM}{'Name':30} {'Lang':12} {'Commits':>8} {'Added':>10} {'Deleted':>10} {'Net LOC':>10}{RESET}")
    print(f"  {DIM}{'─'*82}{RESET}")
    for repo in analysis["repo_details_by_impact"][:15]:
        if repo["additions"] == 0 and repo["deletions"] == 0 and repo["commits"] == 0:
            continue
        badge = "🔒" if repo["private"] else "🌐"
        print(f"  {badge} {rgb(57,211,83)}{repo['name']:28}{RESET} "
              f"{DIM}{repo['language']:12}{RESET} "
              f"{repo['commits']:>8} "
              f"{rgb(57,211,83)}+{fmt_num(repo['additions']):>9}{RESET} "
              f"{rgb(255,99,71)}-{fmt_num(repo['deletions']):>9}{RESET} "
              f"{DIM}{fmt_num(repo['additions'] - repo['deletions']):>10}{RESET}")
    print()

    # ── Recent Features ──
    print(f"  {BOLD}RECENT FEATURES & WORK{RESET}")
    for month, features in analysis["features_by_month"][:3]:
        print(f"  {DIM}{month}{RESET}")
        # Deduplicate and group by repo
        by_repo = defaultdict(list)
        for f in features:
            by_repo[f["repo"]].append(f["message"])
        for repo, msgs in by_repo.items():
            unique = list(dict.fromkeys(msgs))[:5]
            print(f"    {rgb(57,211,83)}{repo}{RESET}")
            for msg in unique:
                print(f"      {DIM}· {msg[:70]}{RESET}")
    print()

    # ── Day of Week ──
    print(f"  {BOLD}ACTIVITY BY DAY{RESET}")
    max_day_val = max(analysis["day_totals"].values()) if analysis["day_totals"] else 1
    for day, count in analysis["day_totals"].items():
        bar_len = int((count / max_day_val) * 30)
        print(f"  {day:>5} {rgb(57,211,83)}{'█' * bar_len}{RESET} {DIM}{count}{RESET}")
    print()
    print(f"  {DIM}Best day: {analysis['max_day']['date']} with {analysis['max_day']['contributionCount']} contributions{RESET}")
    print()


# ─── HTML Dashboard ──────────────────────────────────────────────────────────

def generate_html(data, analysis):
    cal = data["rolling_calendar"]
    at = analysis["alltime"]

    days_json = json.dumps(analysis["days"])
    weekly_json = json.dumps(analysis["weekly_data"])
    month_labels = json.dumps(list(analysis["month_totals"].keys()))
    month_values = json.dumps(list(analysis["month_totals"].values()))
    lang_labels = json.dumps([l for l, _ in analysis["top_languages"][:12]])
    lang_values = json.dumps([s for _, s in analysis["top_languages"][:12]])
    lang_colors_list = json.dumps([analysis["lang_colors"].get(l, "#888888") for l, _ in analysis["top_languages"][:12]])
    day_labels = json.dumps(list(analysis["day_totals"].keys()))
    day_values = json.dumps(list(analysis["day_totals"].values()))
    yearly_json = json.dumps(analysis["yearly_summary"])
    loc_timeline_json = json.dumps(analysis["loc_timeline"])
    loc_by_month_json = json.dumps(analysis["loc_by_month"])
    repos_json = json.dumps(analysis["repo_details_by_impact"])
    project_cards_json = json.dumps(analysis["project_cards"])
    loc_month_repo_json = json.dumps(analysis["loc_month_repo_breakdown"])

    # Features HTML
    features_html = ""
    for month, features in analysis["features_by_month"][:6]:
        by_repo = defaultdict(list)
        for f in features:
            by_repo[f["repo"]].append(f)
        month_label = datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%B %Y")
        features_html += f'<div class="feature-month"><h3>{month_label}</h3>'
        for repo, commits in by_repo.items():
            unique_msgs = list(dict.fromkeys(c["message"] for c in commits))[:8]
            features_html += f'<div class="feature-repo"><span class="feature-repo-name">{repo}</span>'
            features_html += '<ul class="feature-list">'
            for msg in unique_msgs:
                safe_msg = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                features_html += f'<li>{safe_msg}</li>'
            features_html += '</ul></div>'
        features_html += '</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitHub Stats — {data['name']}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {{
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --text-dim: #7d8590;
    --accent: #39d353; --accent2: #2ea043; --red: #f85149; --blue: #58a6ff; --orange: #f0883e;
    --green-0: #161b22; --green-1: #0e4429; --green-2: #006d32; --green-3: #26a641; --green-4: #39d353;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    line-height: 1.5; padding: 2rem; max-width: 1400px; margin: 0 auto;
  }}
  h1 {{ font-size: 2.5rem; font-weight: 700; margin-bottom: 0.25rem; }}
  h2 {{
    font-size: 0.9rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--text-dim);
    margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 1px solid var(--border);
  }}
  h3 {{ font-size: 1rem; font-weight: 600; color: var(--text); margin-bottom: 0.5rem; }}
  .subtitle {{ color: var(--text-dim); margin-bottom: 2rem; font-size: 1.1rem; }}
  .hero {{
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 0.75rem; margin-bottom: 1.5rem;
  }}
  @media (max-width: 900px) {{ .hero {{ grid-template-columns: repeat(2, 1fr); }} }}
  .stat-card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 1rem; text-align: center;
    overflow: hidden; min-width: 0;
  }}
  .stat-card .number {{
    font-size: 1.6rem; font-weight: 700; color: var(--accent); line-height: 1;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .stat-card .number.red {{ color: var(--red); }}
  .stat-card .number.blue {{ color: var(--blue); }}
  .stat-card .label {{ color: var(--text-dim); font-size: 0.78rem; margin-top: 0.25rem; }}
  .stat-card .sublabel {{ color: var(--text-dim); font-size: 0.65rem; opacity: 0.7; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }}
  .grid-3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1.5rem; }}
  @media (max-width: 900px) {{ .grid-2, .grid-3 {{ grid-template-columns: 1fr; }} }}
  .heatmap-container {{ overflow-x: auto; }}
  .heatmap {{
    display: inline-grid; grid-template-rows: repeat(7, 13px);
    grid-auto-flow: column; gap: 3px;
  }}
  .heatmap-cell {{
    width: 13px; height: 13px; border-radius: 2px; background: var(--green-0); position: relative;
  }}
  .heatmap-cell[data-level="1"] {{ background: var(--green-1); }}
  .heatmap-cell[data-level="2"] {{ background: var(--green-2); }}
  .heatmap-cell[data-level="3"] {{ background: var(--green-3); }}
  .heatmap-cell[data-level="4"] {{ background: var(--green-4); }}
  .heatmap-cell:hover {{ outline: 2px solid var(--accent); outline-offset: -1px; }}
  .heatmap-cell:hover::after {{
    content: attr(data-tip); position: absolute; bottom: 120%; left: 50%;
    transform: translateX(-50%); background: var(--text); color: var(--bg);
    padding: 4px 8px; border-radius: 4px; font-size: 0.7rem;
    white-space: nowrap; z-index: 10; pointer-events: none;
  }}
  .heatmap-legend {{
    display: flex; align-items: center; gap: 4px;
    margin-top: 0.75rem; font-size: 0.75rem; color: var(--text-dim);
  }}
  .heatmap-legend .cell {{ width: 13px; height: 13px; border-radius: 2px; }}
  .streak-badges {{ display: flex; gap: 0.75rem; margin-bottom: 1.5rem; flex-wrap: wrap; }}
  .streak-badge {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 20px; padding: 0.35rem 0.9rem; font-size: 0.82rem;
  }}
  .streak-badge strong {{ color: var(--accent); }}
  canvas {{ max-height: 300px; }}
  .section-label {{
    font-size: 0.72rem; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 0.05em; margin-bottom: 0.5rem;
  }}

  /* Repo table */
  .repo-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  .repo-table th {{
    text-align: left; color: var(--text-dim); font-weight: 600;
    padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--border);
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em;
    cursor: pointer; user-select: none; white-space: nowrap;
  }}
  .repo-table th:hover {{ color: var(--accent); }}
  .repo-table th.sorted {{ color: var(--accent); }}
  .repo-table th.sorted::after {{ content: ' ▾'; }}
  .repo-table th.sorted.asc::after {{ content: ' ▴'; }}
  .repo-table td {{ padding: 0.55rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
  .repo-table tr:hover td {{ background: rgba(57, 211, 83, 0.03); }}
  .repo-name-cell {{ font-weight: 600; color: var(--accent); }}
  .repo-desc {{ color: var(--text-dim); font-size: 0.72rem; margin-top: 2px; max-width: 300px; }}
  .repo-badge {{
    display: inline-block; font-size: 0.62rem; padding: 1px 6px;
    border-radius: 10px; background: var(--border); color: var(--text-dim); margin-right: 3px;
  }}
  .diff-add {{ color: var(--accent); }}
  .diff-del {{ color: var(--red); }}
  .lang-bar {{ display: flex; height: 6px; border-radius: 3px; overflow: hidden; margin-top: 4px; width: 100%; }}
  .lang-bar-segment {{ height: 100%; }}
  .search-bar {{
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 0.5rem 0.75rem; color: var(--text); font-size: 0.85rem;
    width: 300px; margin-bottom: 1rem;
  }}
  .search-bar:focus {{ outline: none; border-color: var(--accent); }}
  .search-bar::placeholder {{ color: var(--text-dim); }}

  /* Drill-down */
  .repo-table tr {{ cursor: pointer; }}
  .repo-detail-row td {{ padding: 0 !important; border-bottom: 1px solid var(--border); }}
  .repo-detail {{
    background: var(--bg); padding: 1rem 1.25rem;
    display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;
  }}
  @media (max-width: 768px) {{ .repo-detail {{ grid-template-columns: 1fr; }} }}
  .repo-detail-chart {{ min-height: 150px; max-height: 200px; }}
  .repo-detail-commits {{ max-height: 200px; overflow-y: auto; }}
  .repo-detail-commits ul {{ list-style: none; padding: 0; }}
  .repo-detail-commits li {{
    font-size: 0.78rem; color: var(--text-dim); padding: 3px 0;
    border-left: 2px solid var(--border); padding-left: 0.5rem; margin-bottom: 2px;
  }}
  .repo-detail-commits li .commit-date {{ color: var(--text-dim); font-size: 0.7rem; opacity: 0.7; margin-right: 0.5rem; }}
  .repo-detail h4 {{ font-size: 0.75rem; text-transform: uppercase; color: var(--text-dim); margin-bottom: 0.5rem; letter-spacing: 0.03em; }}

  /* Project cards */
  .project-cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}
  .project-card {{
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 1rem 1.25rem; transition: border-color 0.2s;
  }}
  .project-card:hover {{ border-color: var(--accent); }}
  .project-card-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.5rem; }}
  .project-card-name {{ font-weight: 700; font-size: 0.95rem; color: var(--accent); }}
  .project-card-badge {{
    font-size: 0.62rem; padding: 1px 6px; border-radius: 10px;
    background: var(--border); color: var(--text-dim);
  }}
  .project-card-desc {{ font-size: 0.78rem; color: var(--text-dim); margin-bottom: 0.6rem; }}
  .project-card-tech {{ display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.6rem; }}
  .project-card-tech span {{
    background: var(--card); border: 1px solid var(--border); border-radius: 4px;
    padding: 1px 8px; font-size: 0.72rem; color: var(--text); font-weight: 500;
  }}
  .project-card-stats {{
    display: flex; gap: 0.75rem; font-size: 0.75rem; color: var(--text-dim);
    margin-bottom: 0.6rem; flex-wrap: wrap;
  }}
  .project-card-stats .add {{ color: var(--accent); }}
  .project-card-stats .del {{ color: var(--red); }}
  .project-card-stats strong {{ color: var(--text); }}
  .project-card-work {{ margin-top: 0.5rem; }}
  .project-card-work-cat {{
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-dim); margin-top: 0.4rem; margin-bottom: 0.15rem; font-weight: 600;
  }}
  .project-card-work ul {{ list-style: none; padding-left: 0.5rem; }}
  .project-card-work li {{
    font-size: 0.76rem; color: var(--text-dim); padding: 2px 0;
    border-left: 2px solid var(--border); padding-left: 0.5rem; margin-bottom: 1px;
  }}
  .project-card-toggle {{
    font-size: 0.72rem; color: var(--blue); cursor: pointer; margin-top: 0.4rem;
    border: none; background: none; padding: 0;
  }}
  .project-card-toggle:hover {{ text-decoration: underline; }}
  .loc-drill-row {{ display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0; border-bottom: 1px solid var(--border); font-size: 0.82rem; }}
  .loc-drill-row:last-child {{ border-bottom: none; }}
  .loc-drill-name {{ color: var(--accent); font-weight: 600; min-width: 200px; }}
  .loc-drill-bar {{ flex: 1; height: 16px; background: var(--border); border-radius: 3px; overflow: hidden; display: flex; }}
  .loc-drill-bar .add {{ background: var(--accent); height: 100%; }}
  .loc-drill-bar .del {{ background: var(--red); height: 100%; }}

  /* Features */
  .features-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 1rem; }}
  .feature-month {{ background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 1rem; }}
  .feature-month h3 {{ color: var(--accent); font-size: 0.9rem; margin-bottom: 0.75rem; }}
  .feature-repo {{ margin-bottom: 0.75rem; }}
  .feature-repo-name {{ font-weight: 600; font-size: 0.82rem; color: var(--text); }}
  .feature-list {{ list-style: none; padding-left: 0.5rem; margin-top: 0.25rem; }}
  .feature-list li {{
    font-size: 0.78rem; color: var(--text-dim); padding: 2px 0;
    border-left: 2px solid var(--border); padding-left: 0.5rem; margin-bottom: 2px;
  }}
</style>
</head>
<body>
  <h1>{data['name']}</h1>
  <p class="subtitle">@{data['login']} · Member since {data['created_at'][:4]} · {data['total_repos']} repositories · {len(data['years'])} years on GitHub</p>

  <p class="section-label">All-Time Totals</p>
  <div class="hero">
    <div class="stat-card">
      <div class="number">{fmt_num(at['contributions'])}</div>
      <div class="label">Contributions</div>
    </div>
    <div class="stat-card">
      <div class="number">{fmt_num(at['commits'])}</div>
      <div class="label">Commits</div>
    </div>
    <div class="stat-card">
      <div class="number">{fmt_num(at['prs'])}</div>
      <div class="label">Pull Requests</div>
    </div>
    <div class="stat-card">
      <div class="number">{fmt_num(at['repos_created'])}</div>
      <div class="label">Repos Created</div>
    </div>
  </div>
  <div class="hero">
    <div class="stat-card">
      <div class="number">+{fmt_num(analysis['total_additions'])}</div>
      <div class="label">Lines Added</div>
    </div>
    <div class="stat-card">
      <div class="number red">-{fmt_num(analysis['total_deletions'])}</div>
      <div class="label">Lines Deleted</div>
    </div>
    <div class="stat-card">
      <div class="number blue">{fmt_num(analysis['total_additions'] - analysis['total_deletions'])}</div>
      <div class="label">Net LOC</div>
      <div class="sublabel">from diffs</div>
    </div>
    <div class="stat-card">
      <div class="number">{analysis['features_shipped']}</div>
      <div class="label">Features Shipped</div>
      <div class="sublabel">across {analysis['active_repos']} active repos</div>
    </div>
  </div>

  <div class="streak-badges">
    <span class="streak-badge">🔥 Current streak: <strong>{analysis['current_streak']} days</strong></span>
    <span class="streak-badge">🏆 Longest streak: <strong>{analysis['longest_streak']} days</strong></span>
    <span class="streak-badge">📅 Best day: <strong>{analysis['max_day']['date']}</strong> ({analysis['max_day']['contributionCount']} contributions)</span>
    <span class="streak-badge">📊 Active: <strong>{analysis['active_days']}/{analysis['total_days']}</strong> days</span>
  </div>

  <div class="card">
    <h2>Contribution Heatmap (Rolling Year)</h2>
    <div class="heatmap-container">
      <div class="heatmap" id="heatmap"></div>
    </div>
    <div class="heatmap-legend">
      Less
      <div class="cell" style="background: var(--green-0)"></div>
      <div class="cell" style="background: var(--green-1)"></div>
      <div class="cell" style="background: var(--green-2)"></div>
      <div class="cell" style="background: var(--green-3)"></div>
      <div class="cell" style="background: var(--green-4)"></div>
      More
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>Year over Year</h2>
      <canvas id="yearlyChart"></canvas>
    </div>
    <div class="card">
      <h2>Lines of Code Over Time</h2>
      <canvas id="locTimelineChart"></canvas>
      {"" if not EXCLUDED_REPOS else '<p style="color: var(--text-dim); font-size: 0.68rem; margin-top: 0.5rem; opacity: 0.7;">* ' + str(len(EXCLUDED_REPOS)) + ' repo(s) excluded from LOC stats</p>'}
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>LOC by Month (Additions vs Deletions)</h2>
      <canvas id="locMonthChart"></canvas>
    </div>
    <div class="card">
      <h2>Monthly Contributions</h2>
      <canvas id="monthlyChart"></canvas>
    </div>
  </div>

  <div class="grid-3">
    <div class="card">
      <h2>Languages by Codebase Size</h2>
      <canvas id="langChart"></canvas>
    </div>
    <div class="card">
      <h2>Weekly Activity</h2>
      <canvas id="weeklyChart"></canvas>
    </div>
    <div class="card">
      <h2>Activity by Day of Week</h2>
      <canvas id="dayChart"></canvas>
    </div>
  </div>

  <div class="card">
    <h2>Engineering Work (Last 3 Months)</h2>
    <p style="color: var(--text-dim); font-size: 0.78rem; margin-bottom: 1rem;">What was built, why, and with what tech — click a card to expand details</p>
    <div class="project-cards" id="projectCards"></div>
  </div>

  <div id="locDrilldown" class="card" style="display: none;">
    <h2 id="locDrilldownTitle">Month Breakdown</h2>
    <div id="locDrilldownContent" style="max-height: 400px; overflow-y: auto;"></div>
  </div>

  <div class="card">
    <h2>Repository Breakdown</h2>
    <input type="text" class="search-bar" id="repoSearch" placeholder="Search repos by name, language, or description...">
    <div style="overflow-x: auto;">
      <table class="repo-table" id="repoTable">
        <thead>
          <tr>
            <th data-sort="name">Repository</th>
            <th data-sort="language">Language</th>
            <th data-sort="commits">Commits</th>
            <th data-sort="additions">Lines +</th>
            <th data-sort="deletions">Lines -</th>
            <th data-sort="net">Net</th>
            <th data-sort="net">Net LOC</th>
            <th data-sort="pushed" class="sorted">Last Push</th>
          </tr>
        </thead>
        <tbody id="repoBody"></tbody>
      </table>
    </div>
  </div>

  <p style="text-align: center; color: var(--text-dim); margin-top: 2rem; font-size: 0.8rem;">
    Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')} · Data from GitHub GraphQL + REST API
  </p>

<script>
const DAYS = {days_json};
const WEEKLY = {weekly_json};
const MONTH_LABELS = {month_labels};
const MONTH_VALUES = {month_values};
const LANG_LABELS = {lang_labels};
const LANG_VALUES = {lang_values};
const LANG_COLORS = {lang_colors_list};
const DAY_LABELS = {day_labels};
const DAY_VALUES = {day_values};
const YEARLY = {yearly_json};
const LOC_TIMELINE = {loc_timeline_json};
const LOC_BY_MONTH = {loc_by_month_json};
const REPOS = {repos_json};
const PROJECT_CARDS = {project_cards_json};
const LOC_MONTH_REPOS = {loc_month_repo_json};

Chart.defaults.color = '#7d8590';
Chart.defaults.borderColor = '#30363d';
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";
const fmt = n => n.toLocaleString();

// ── Heatmap ──
(function() {{
  const heatmap = document.getElementById('heatmap');
  const maxCount = Math.max(...DAYS.map(d => d.contributionCount), 1);
  DAYS.forEach(day => {{
    const cell = document.createElement('div');
    cell.className = 'heatmap-cell';
    const c = day.contributionCount;
    const ratio = c / maxCount;
    let level = 0;
    if (c > 0) level = ratio < 0.25 ? 1 : ratio < 0.5 ? 2 : ratio < 0.75 ? 3 : 4;
    cell.setAttribute('data-level', level);
    cell.setAttribute('data-tip', `${{day.date}}: ${{c}} contribution${{c !== 1 ? 's' : ''}}`);
    heatmap.appendChild(cell);
  }});
}})();

// ── Year over Year ──
new Chart(document.getElementById('yearlyChart'), {{
  type: 'bar',
  data: {{
    labels: YEARLY.map(y => y.year),
    datasets: [
      {{ label: 'Commits', data: YEARLY.map(y => y.commits), backgroundColor: '#39d353', borderRadius: 3, borderSkipped: false }},
      {{ label: 'PRs', data: YEARLY.map(y => y.prs), backgroundColor: '#58a6ff', borderRadius: 3, borderSkipped: false }},
      {{ label: 'Repos', data: YEARLY.map(y => y.repos_created), backgroundColor: '#f0883e', borderRadius: 3, borderSkipped: false }},
    ]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 8 }} }} }},
    scales: {{
      y: {{ beginAtZero: true, stacked: true, grid: {{ color: '#21262d' }}, title: {{ display: true, text: 'Contributions', color: '#7d8590', font: {{ size: 11 }} }} }},
      x: {{ stacked: true, grid: {{ display: false }} }}
    }}
  }}
}});

// ── LOC Timeline ──
(function() {{
  // Compute running total of net lines
  let running = 0;
  const runningData = LOC_TIMELINE.map(w => {{
    running += w.additions - w.deletions;
    return {{ date: w.date, net: running, additions: w.additions, deletions: w.deletions }};
  }});

  new Chart(document.getElementById('locTimelineChart'), {{
    type: 'line',
    data: {{
      labels: runningData.map(d => d.date),
      datasets: [{{
        label: 'Cumulative Net LOC',
        data: runningData.map(d => d.net),
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88, 166, 255, 0.1)',
        fill: true, tension: 0.2, pointRadius: 0, pointHitRadius: 10, borderWidth: 2,
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        y: {{ grid: {{ color: '#21262d' }}, title: {{ display: true, text: 'Net Lines of Code', color: '#7d8590', font: {{ size: 11 }} }}, ticks: {{ callback: v => v >= 1000 ? (v/1000).toFixed(0) + 'k' : v }} }},
        x: {{ grid: {{ display: false }}, ticks: {{
          maxTicksLimit: 10,
          callback: function(val) {{
            const d = new Date(this.getLabelForValue(val));
            return d.toLocaleString('default', {{ month: 'short', year: '2-digit' }});
          }}
        }} }}
      }}
    }}
  }});
}})();

// ── LOC by Month (clickable) ──
const locMonthChart = new Chart(document.getElementById('locMonthChart'), {{
  type: 'bar',
  data: {{
    labels: LOC_BY_MONTH.map(m => {{
      const [y, mo] = m.month.split('-');
      return new Date(y, mo-1).toLocaleString('default', {{ month: 'short', year: '2-digit' }});
    }}),
    datasets: [
      {{ label: 'Lines Added', data: LOC_BY_MONTH.map(m => m.additions), backgroundColor: '#39d353', borderRadius: 2, borderSkipped: false }},
      {{ label: 'Lines Deleted', data: LOC_BY_MONTH.map(m => -m.deletions), backgroundColor: '#f85149', borderRadius: 2, borderSkipped: false }},
    ]
  }},
  options: {{
    responsive: true,
    onClick: (e, elements) => {{
      if (elements.length > 0) {{
        const idx = elements[0].index;
        const monthKey = LOC_BY_MONTH[idx].month;
        showLocDrilldown(monthKey);
      }}
    }},
    plugins: {{
      legend: {{ labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 8 }} }},
      tooltip: {{ callbacks: {{ footer: () => 'Click to drill down' }} }}
    }},
    scales: {{
      y: {{ stacked: true, grid: {{ color: '#21262d' }}, title: {{ display: true, text: 'Lines (added / deleted)', color: '#7d8590', font: {{ size: 11 }} }}, ticks: {{ callback: v => v >= 1000 ? (v/1000).toFixed(0)+'k' : v <= -1000 ? (v/1000).toFixed(0)+'k' : v }} }},
      x: {{ stacked: true, grid: {{ display: false }}, ticks: {{ maxTicksLimit: 15 }} }}
    }}
  }}
}});

// ── Monthly Contributions ──
new Chart(document.getElementById('monthlyChart'), {{
  type: 'bar',
  data: {{
    labels: MONTH_LABELS.map(m => {{
      const [y, mo] = m.split('-');
      return new Date(y, mo-1).toLocaleString('default', {{ month: 'short' }});
    }}),
    datasets: [{{ data: MONTH_VALUES, backgroundColor: '#26a641', borderRadius: 4, borderSkipped: false }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ beginAtZero: true, grid: {{ color: '#21262d' }}, title: {{ display: true, text: 'Contributions', color: '#7d8590', font: {{ size: 11 }} }} }},
      x: {{ grid: {{ display: false }} }}
    }}
  }}
}});

// ── Weekly Activity ──
new Chart(document.getElementById('weeklyChart'), {{
  type: 'line',
  data: {{
    labels: WEEKLY.map(w => w.date),
    datasets: [{{
      data: WEEKLY.map(w => w.count),
      borderColor: '#39d353', backgroundColor: 'rgba(57,211,83,0.1)',
      fill: true, tension: 0.3, pointRadius: 0, pointHitRadius: 10, borderWidth: 2,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ beginAtZero: true, grid: {{ color: '#21262d' }}, title: {{ display: true, text: 'Contributions / week', color: '#7d8590', font: {{ size: 11 }} }} }},
      x: {{ grid: {{ display: false }}, ticks: {{
        maxTicksLimit: 10,
        callback: function(val) {{
          return new Date(this.getLabelForValue(val)).toLocaleString('default', {{ month: 'short' }});
        }}
      }} }}
    }}
  }}
}});

// ── Languages ──
new Chart(document.getElementById('langChart'), {{
  type: 'doughnut',
  data: {{
    labels: LANG_LABELS,
    datasets: [{{ data: LANG_VALUES, backgroundColor: LANG_COLORS, borderColor: '#161b22', borderWidth: 2 }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ position: 'right', labels: {{ padding: 8, usePointStyle: true, pointStyle: 'circle', font: {{ size: 11 }} }} }} }}
  }}
}});

// ── Day of Week ──
new Chart(document.getElementById('dayChart'), {{
  type: 'bar',
  data: {{
    labels: DAY_LABELS,
    datasets: [{{
      data: DAY_VALUES,
      backgroundColor: DAY_VALUES.map(v => {{
        const max = Math.max(...DAY_VALUES);
        return `rgba(57, 211, 83, ${{0.3 + (v/max)*0.7}})`;
      }}),
      borderRadius: 4, borderSkipped: false,
    }}]
  }},
  options: {{
    responsive: true, indexAxis: 'y',
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ beginAtZero: true, grid: {{ color: '#21262d' }} }}, y: {{ grid: {{ display: false }} }} }}
  }}
}});

// ── Project Cards (interactive) ──
(function() {{
  const container = document.getElementById('projectCards');
  if (!PROJECT_CARDS.length) {{
    container.innerHTML = '<p style="color:var(--text-dim)">No recent activity in the last 3 months.</p>';
    return;
  }}

  const catIcons = {{
    'Features shipped': '🚀', 'Bugs fixed': '🔧', 'Security hardening': '🔒',
    'Performance': '⚡', 'Refactoring': '♻️', 'Design & UX': '🎨',
    'Testing': '🧪', 'Documentation': '📝'
  }};

  PROJECT_CARDS.forEach(card => {{
    const el = document.createElement('div');
    el.className = 'project-card';

    const techHtml = card.tech_stack.map(t =>
      `<span>${{t}}</span>`
    ).join('');

    const net = card.additions_recent - card.deletions_recent;
    const netStr = net >= 0 ? `+${{fmt(net)}}` : fmt(net);

    // Show first 2 work categories collapsed, rest hidden
    let workHtml = '';
    card.work_items.forEach((wi, i) => {{
      const icon = catIcons[wi.category] || '📦';
      const items = wi.items.map(it => {{
        const safe = it.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        return `<li>${{safe}}</li>`;
      }}).join('');
      const hidden = i >= 2 ? ' style="display:none" data-extra="true"' : '';
      workHtml += `<div${{hidden}}><div class="project-card-work-cat">${{icon}} ${{wi.category}} (${{wi.items.length}})</div><ul>${{items}}</ul></div>`;
    }});

    const hasMore = card.work_items.length > 2;
    const toggleBtn = hasMore
      ? `<button class="project-card-toggle" onclick="this.parentElement.querySelectorAll('[data-extra]').forEach(e=>e.style.display=e.style.display==='none'?'block':'none');this.textContent=this.textContent.includes('more')?'Show less':'Show ${{card.work_items.length - 2}} more categories…'">Show ${{card.work_items.length - 2}} more categories…</button>`
      : '';

    const desc = card.description
      ? `<div class="project-card-desc">${{card.description.replace(/&/g,'&amp;').replace(/</g,'&lt;')}}</div>`
      : '';

    el.innerHTML = `
      <div class="project-card-header">
        <span class="project-card-name">${{card.name}}</span>
        <span class="project-card-badge">${{card.private ? 'Private' : 'Public'}}</span>
      </div>
      ${{desc}}
      <div class="project-card-tech">${{techHtml}}</div>
      <div class="project-card-stats">
        <span><strong>${{card.commits_recent}}</strong> commits</span>
        <span class="add">+${{fmt(card.additions_recent)}}</span>
        <span class="del">-${{fmt(card.deletions_recent)}}</span>
        <span>net ${{netStr}}</span>
        <span style="color:var(--text-dim);font-size:0.7rem">${{card.date_range}}</span>
      </div>
      <div class="project-card-work">${{workHtml}}${{toggleBtn}}</div>
    `;
    container.appendChild(el);
  }});
}})();

// ── LOC Month Chart Drill-Down ──
function showLocDrilldown(monthKey) {{
  const panel = document.getElementById('locDrilldown');
  const title = document.getElementById('locDrilldownTitle');
  const content = document.getElementById('locDrilldownContent');
  const repos = LOC_MONTH_REPOS[monthKey];
  if (!repos || repos.length === 0) {{ panel.style.display = 'none'; return; }}

  const [y, m] = monthKey.split('-');
  const label = new Date(y, m-1).toLocaleString('default', {{ month: 'long', year: 'numeric' }});
  title.textContent = `${{label}} — Repo Breakdown`;

  const maxVal = Math.max(...repos.map(r => r.additions + r.deletions));
  content.innerHTML = repos.map(r => {{
    const total = r.additions + r.deletions;
    const addPct = maxVal > 0 ? (r.additions / maxVal * 100) : 0;
    const delPct = maxVal > 0 ? (r.deletions / maxVal * 100) : 0;
    const net = r.additions - r.deletions;
    const netStr = net >= 0 ? `<span class="diff-add">+${{fmt(net)}}</span>` : `<span class="diff-del">${{fmt(net)}}</span>`;
    return `<div class="loc-drill-row">
      <span class="loc-drill-name">${{r.repo}}</span>
      <div class="loc-drill-bar"><div class="add" style="width:${{addPct}}%"></div><div class="del" style="width:${{delPct}}%"></div></div>
      <span class="diff-add" style="min-width:80px;text-align:right">+${{fmt(r.additions)}}</span>
      <span class="diff-del" style="min-width:80px;text-align:right">-${{fmt(r.deletions)}}</span>
      <span style="min-width:80px;text-align:right">${{netStr}}</span>
    </div>`;
  }}).join('');

  panel.style.display = 'block';
  panel.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
}}

// ── Repo Table with Drill-Down ──
let currentSort = 'pushed';
let sortDir = -1;
let expandedRepo = null;
const detailCharts = {{}};

function renderRepos(filter = '') {{
  const body = document.getElementById('repoBody');
  expandedRepo = null;
  Object.values(detailCharts).forEach(c => c.destroy());

  const filtered = REPOS.filter(r =>
    !filter || r.name.toLowerCase().includes(filter) ||
    r.language.toLowerCase().includes(filter) ||
    (r.description || '').toLowerCase().includes(filter)
  );

  filtered.sort((a, b) => {{
    let va = a[currentSort], vb = b[currentSort];
    if (currentSort === 'net') {{ va = a.additions - a.deletions; vb = b.additions - b.deletions; }}
    if (typeof va === 'string') return sortDir * va.localeCompare(vb);
    return sortDir * (va - vb);
  }});

  body.innerHTML = filtered.map((r, i) => {{
    const net = r.additions - r.deletions;
    const netClass = net >= 0 ? 'diff-add' : 'diff-del';
    const netStr = net >= 0 ? `+${{fmt(net)}}` : fmt(net);
    const desc = r.description ? `<div class="repo-desc">${{r.description}}</div>` : '';
    const badge = r.private ? '<span class="repo-badge">Private</span>' : '<span class="repo-badge">Public</span>';
    const forkBadge = r.fork ? '<span class="repo-badge" style="background:#1f6feb33;color:#58a6ff">Fork</span>' : '';
    const stars = r.stars > 0 ? `<span class="repo-badge">★ ${{r.stars}}</span>` : '';
    const langBar = r.languages && r.languages.length > 0 && r.size_bytes > 0
      ? `<div class="lang-bar">${{r.languages.map(l =>
          `<div class="lang-bar-segment" style="width:${{(l.size/r.size_bytes*100).toFixed(1)}}%;background:${{l.color}}" title="${{l.name}}: ${{fmt(l.size)}} bytes"></div>`
        ).join('')}}</div>`
      : '';
    return `<tr data-repo="${{r.name}}" onclick="toggleDetail('${{r.name.replace(/'/g, "\\'")}}')">
      <td><span class="repo-name-cell">${{r.name}}</span> ${{badge}}${{forkBadge}}${{stars}}${{desc}}${{langBar}}</td>
      <td>${{r.language}}</td>
      <td>${{fmt(r.commits)}}</td>
      <td class="diff-add">+${{fmt(r.additions)}}</td>
      <td class="diff-del">-${{fmt(r.deletions)}}</td>
      <td class="${{netClass}}">${{netStr}}</td>
      <td>${{fmt(net)}}</td>
      <td style="color:var(--text-dim)">${{r.pushed}}</td>
    </tr>`;
  }}).join('');
}}

function toggleDetail(repoName) {{
  const existing = document.getElementById('detail-' + repoName);
  if (existing) {{
    if (detailCharts[repoName]) {{ detailCharts[repoName].destroy(); delete detailCharts[repoName]; }}
    existing.remove();
    expandedRepo = null;
    return;
  }}
  // Close any open detail
  document.querySelectorAll('.repo-detail-row').forEach(el => {{
    const name = el.id.replace('detail-', '');
    if (detailCharts[name]) {{ detailCharts[name].destroy(); delete detailCharts[name]; }}
    el.remove();
  }});

  const repo = REPOS.find(r => r.name === repoName);
  if (!repo) return;
  expandedRepo = repoName;

  const tr = document.querySelector(`tr[data-repo="${{repoName}}"]`);
  if (!tr) return;

  const detailRow = document.createElement('tr');
  detailRow.className = 'repo-detail-row';
  detailRow.id = 'detail-' + repoName;

  const weekly = repo.weekly_stats || [];
  const commits = repo.recent_commits || [];

  // Build commits list
  const commitsList = commits.length > 0
    ? commits.slice(0, 20).map(c =>
        `<li><span class="commit-date">${{c.date}}</span>${{c.message}}</li>`
      ).join('')
    : '<li>No recent commits available</li>';

  detailRow.innerHTML = `<td colspan="8"><div class="repo-detail">
    <div>
      <h4>Weekly Activity</h4>
      <div class="repo-detail-chart"><canvas id="chart-${{repoName}}"></canvas></div>
    </div>
    <div>
      <h4>Recent Commits (${{commits.length}})</h4>
      <div class="repo-detail-commits"><ul>${{commitsList}}</ul></div>
    </div>
  </div></td>`;

  tr.after(detailRow);

  // Render weekly chart if data exists
  if (weekly.length > 0) {{
    const ctx = document.getElementById('chart-' + repoName);
    if (ctx) {{
      detailCharts[repoName] = new Chart(ctx, {{
        type: 'bar',
        data: {{
          labels: weekly.map(w => {{
            const d = new Date(w.week * 1000);
            return d.toLocaleDateString('default', {{ month: 'short', year: '2-digit' }});
          }}),
          datasets: [
            {{ label: 'Added', data: weekly.map(w => w.additions), backgroundColor: '#39d353', borderRadius: 2, borderSkipped: false }},
            {{ label: 'Deleted', data: weekly.map(w => -w.deletions), backgroundColor: '#f85149', borderRadius: 2, borderSkipped: false }},
          ]
        }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 6, font: {{ size: 10 }} }} }} }},
          scales: {{
            y: {{ stacked: true, grid: {{ color: '#21262d' }}, ticks: {{ font: {{ size: 10 }}, callback: v => v >= 1000 ? (v/1000).toFixed(0)+'k' : v <= -1000 ? (v/1000).toFixed(0)+'k' : v }} }},
            x: {{ stacked: true, grid: {{ display: false }}, ticks: {{ font: {{ size: 9 }}, maxTicksLimit: 12 }} }}
          }}
        }}
      }});
    }}
  }}
}}

document.querySelectorAll('.repo-table th').forEach(th => {{
  th.addEventListener('click', (e) => {{
    e.stopPropagation();
    const col = th.dataset.sort;
    if (currentSort === col) sortDir *= -1;
    else {{ currentSort = col; sortDir = -1; }}
    document.querySelectorAll('.repo-table th').forEach(t => {{ t.classList.remove('sorted','asc'); }});
    th.classList.add('sorted');
    if (sortDir === 1) th.classList.add('asc');
    renderRepos(document.getElementById('repoSearch').value.toLowerCase());
  }});
}});

document.getElementById('repoSearch').addEventListener('input', e => {{
  renderRepos(e.target.value.toLowerCase());
}});

renderRepos();
</script>
</body>
</html>"""
    return html


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    data = fetch_all_data()
    analysis = analyze(data)
    print_terminal(data, analysis)

    html = generate_html(data, analysis)
    with open("dashboard.html", "w") as f:
        f.write(html)
    print(f"  {BOLD}{rgb(57,211,83)}✓{RESET} Dashboard saved to {BOLD}dashboard.html{RESET}")
    print(f"  {DIM}Open in browser: open dashboard.html{RESET}")
    print()

if __name__ == "__main__":
    main()
