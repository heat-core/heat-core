#!/usr/bin/env python3
"""Generate dark/light hero and live GitHub telemetry SVGs for heat-core profile."""

from __future__ import annotations

import html
import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

USERNAME = os.environ.get("GITHUB_USERNAME", "heat-core")
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

GRAPHQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    name
    login
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
    repositories(
      first: 100,
      ownerAffiliations: OWNER,
      privacy: PUBLIC,
      orderBy: {field: PUSHED_AT, direction: DESC}
    ) {
      totalCount
      nodes {
        name
        isFork
        isArchived
        pushedAt
        releases(first: 1, orderBy: {field: CREATED_AT, direction: DESC}) {
          totalCount
          nodes { tagName }
        }
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges {
            size
            node { name color }
          }
        }
      }
    }
  }
}
"""

THEMES = {
    "dark": {
        "bg": "#0d1117",
        "panel": "#161b22",
        "border": "#30363d",
        "grid": "#21262d",
        "text": "#f0f6fc",
        "muted": "#8b949e",
        "accent": "#58a6ff",
        "accent_alt": "#a371f7",
        "green": "#3fb950",
        "pill_bg": "#1f242c",
        "pill_border": "#30363d",
        "bar_bg": "#21262d",
    },
    "light": {
        "bg": "#ffffff",
        "panel": "#f6f8fa",
        "border": "#d0d7de",
        "grid": "#eaeef2",
        "text": "#1f2328",
        "muted": "#656d76",
        "accent": "#0969da",
        "accent_alt": "#8250df",
        "green": "#1a7f37",
        "pill_bg": "#f6f8fa",
        "pill_border": "#d0d7de",
        "bar_bg": "#eaeef2",
    },
}

SANS = '-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif'
MONO = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace'


def get_token() -> str:
    """Resolve GitHub token from environment or local gh CLI."""
    for env_key in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(env_key, "").strip()
        if token:
            return token
    try:
        out = subprocess.check_output(["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL)
        return out.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Missing GITHUB_TOKEN / GH_TOKEN and gh CLI token unavailable.") from exc


def fetch_github_data(token: str, login: str) -> dict:
    """Fetch user contributions and authored public repositories via GitHub GraphQL."""
    payload = json.dumps({"query": GRAPHQL_QUERY, "variables": {"login": login}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"{login}-profile-telemetry",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL error {exc.code}: {body}") from exc

    if data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL returned errors: {data['errors']}")
    return data["data"]["user"]


def compute_streaks(weeks: list[dict]) -> tuple[int, int, int]:
    """Return (current_streak, longest_streak, active_days) from contribution weeks."""
    days: list[tuple[str, int]] = []
    for week in weeks:
        for day in week.get("contributionDays", []):
            days.append((day["date"], int(day["contributionCount"])))

    if not days:
        return 0, 0, 0

    days.sort(key=lambda item: item[0])
    active_days = sum(1 for _, count in days if count > 0)

    longest = 0
    running = 0
    for _, count in days:
        if count > 0:
            running += 1
            if running > longest:
                longest = running
        else:
            running = 0

    # Current streak allows today to have 0 commits if yesterday was active.
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    counts_by_date = dict(days)

    cursor_date = datetime.now(timezone.utc).date()
    if counts_by_date.get(today_str, 0) == 0:
        if counts_by_date.get(yesterday_str, 0) > 0:
            cursor_date = cursor_date - timedelta(days=1)
        else:
            return 0, longest, active_days

    current = 0
    while counts_by_date.get(cursor_date.strftime("%Y-%m-%d"), 0) > 0:
        current += 1
        cursor_date = cursor_date - timedelta(days=1)

    return current, longest, active_days


def aggregate_languages(repos: list[dict], exclude_name: str) -> list[dict]:
    """Aggregate language byte shares across non-fork public authored repositories."""
    totals: dict[str, int] = {}
    colors: dict[str, str] = {}
    for repo in repos:
        if repo.get("isFork") or repo.get("name") == exclude_name:
            continue
        for edge in repo.get("languages", {}).get("edges", []):
            lang = edge["node"]["name"]
            size = int(edge["size"])
            totals[lang] = totals.get(lang, 0) + size
            if edge["node"].get("color"):
                colors[lang] = edge["node"]["color"]

    grand_total = sum(totals.values())
    if grand_total <= 0:
        return []

    sorted_langs = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    top = sorted_langs[:5]
    other_bytes = sum(size for _, size in sorted_langs[5:])

    result = [
        {
            "name": name,
            "pct": (size / grand_total) * 100.0,
            "color": colors.get(name, "#8b949e"),
        }
        for name, size in top
    ]
    if other_bytes > 0:
        result.append(
            {
                "name": "Other",
                "pct": (other_bytes / grand_total) * 100.0,
                "color": "#8b949e",
            }
        )
    return result


def build_hero_svg(theme_name: str, display_name: str, login: str) -> str:
    """Render the dual-theme hero header SVG."""
    t = THEMES[theme_name]
    tags = ["LLM · RAG · Agents", "Python & TypeScript", "Electron & FastAPI", "Local-First AI"]
    tag_pills = []
    cursor_x = 32
    for label in tags:
        pill_w = len(label) * 7 + 22
        tag_pills.append(
            f'<g transform="translate({cursor_x}, 112)">'
            f'<rect width="{pill_w}" height="24" rx="6" fill="{t["pill_bg"]}" stroke="{t["pill_border"]}" stroke-width="1"/>'
            f'<text x="{pill_w / 2:.1f}" y="16" text-anchor="middle" font-family=\'{MONO}\' font-size="11" font-weight="500" fill="{t["muted"]}">{html.escape(label)}</text>'
            f'</g>'
        )
        cursor_x += pill_w + 10

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="840" height="156" viewBox="0 0 840 156" fill="none" role="img" aria-label="{html.escape(display_name)} GitHub Profile Banner">
  <defs>
    <linearGradient id="accent-line-{theme_name}" x1="0" y1="0" x2="840" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="{t["accent"]}" />
      <stop offset="55%" stop-color="{t["accent_alt"]}" />
      <stop offset="100%" stop-color="{t["green"]}" />
    </linearGradient>
    <pattern id="bg-grid-{theme_name}" width="24" height="24" patternUnits="userSpaceOnUse">
      <path d="M 24 0 L 0 0 0 24" fill="none" stroke="{t["grid"]}" stroke-width="0.8"/>
    </pattern>
    <clipPath id="card-clip-{theme_name}">
      <rect x="0.5" y="0.5" width="839" height="155" rx="12" />
    </clipPath>
  </defs>

  <g clip-path="url(#card-clip-{theme_name})">
    <rect width="840" height="156" fill="{t["bg"]}" />
    <rect width="840" height="156" fill="url(#bg-grid-{theme_name})" opacity="0.65" />
    <rect x="0" y="0" width="840" height="3" fill="url(#accent-line-{theme_name})" />
  </g>
  <rect x="0.5" y="0.5" width="839" height="155" rx="12" stroke="{t["border"]}" stroke-width="1" />

  <!-- Top terminal breadcrumb -->
  <text x="32" y="34" font-family='{MONO}' font-size="12" fill="{t["muted"]}">~/{html.escape(login)}/overview</text>

  <!-- Right active status pill -->
  <g transform="translate(658, 18)">
    <rect width="150" height="24" rx="12" fill="{t["panel"]}" stroke="{t["border"]}" stroke-width="1"/>
    <circle cx="15" cy="12" r="4" fill="{t["green"]}"/>
    <text x="27" y="16" font-family='{MONO}' font-size="11" font-weight="600" fill="{t["text"]}">REMOTE · ACTIVE</text>
  </g>

  <!-- Main Heading -->
  <text x="32" y="70" font-family='{SANS}' font-size="28" font-weight="700" fill="{t["text"]}">{html.escape(display_name)}</text>
  <rect x="244" y="49" width="94" height="24" rx="6" fill="{t["panel"]}" stroke="{t["border"]}" stroke-width="1"/>
  <text x="291" y="65" text-anchor="middle" font-family='{MONO}' font-size="12" font-weight="600" fill="{t["accent"]}">@{html.escape(login)}</text>

  <!-- Positioning Subtitle -->
  <text x="32" y="96" font-family='{SANS}' font-size="14" font-weight="500" fill="{t["muted"]}">AI / LLM &amp; Software Engineer — Reliable Agents, Local-First AI &amp; Cross-Platform Systems</text>

  <!-- Stack Pills -->
  {"".join(tag_pills)}
</svg>
'''


def build_telemetry_svg(
    theme_name: str,
    total_contributions: int,
    current_streak: int,
    longest_streak: int,
    active_days: int,
    total_public_repos: int,
    original_public_repos: int,
    latest_repo: str,
    latest_push_date: str,
    langs: list[dict],
) -> str:
    """Render the combined 2-panel engineering telemetry SVG card."""
    t = THEMES[theme_name]
    updated_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Stacked language progress bar (width = 352)
    bar_w = 352.0
    bar_segments = []
    offset_x = 0.0
    for idx, lang in enumerate(langs):
        seg_w = (lang["pct"] / 100.0) * bar_w
        if idx == len(langs) - 1:
            seg_w = max(0.0, bar_w - offset_x)
        bar_segments.append(
            f'<rect x="{offset_x:.2f}" y="0" width="{seg_w:.2f}" height="10" fill="{lang["color"]}"/>'
        )
        offset_x += seg_w

    # Language legend rows (2 columns x 3 rows)
    legend_items = []
    for idx, lang in enumerate(langs[:6]):
        col = idx % 2
        row = idx // 2
        lx = 446 + col * 180
        ly = 104 + row * 28
        legend_items.append(
            f'<g transform="translate({lx}, {ly})">'
            f'<circle cx="5" cy="-4" r="4.5" fill="{lang["color"]}"/>'
            f'<text x="16" y="0" font-family=\'{SANS}\' font-size="12" font-weight="600" fill="{t["text"]}">{html.escape(lang["name"])}</text>'
            f'<text x="162" y="0" text-anchor="end" font-family=\'{MONO}\' font-size="11.5" fill="{t["muted"]}">{lang["pct"]:.1f}%</text>'
            f'</g>'
        )

    metrics_rows = [
        ("Contributions (1Y)", f"{total_contributions} ({active_days} active days)", t["green"]),
        ("Active Streak", f"{current_streak}d current · {longest_streak}d longest", t["accent"]),
        ("Public Codebases", f"{total_public_repos} total · {original_public_repos} original", t["accent_alt"]),
        ("Latest Push", f"{latest_repo} ({latest_push_date})", t["muted"]),
    ]

    left_rows = []
    for idx, (label, val, dot_color) in enumerate(metrics_rows):
        ry = 76 + idx * 29
        left_rows.append(
            f'<g transform="translate(36, {ry})">'
            f'<circle cx="5" cy="-4" r="3.5" fill="{dot_color}"/>'
            f'<text x="16" y="0" font-family=\'{SANS}\' font-size="12.5" font-weight="500" fill="{t["muted"]}">{html.escape(label)}</text>'
            f'<text x="352" y="0" text-anchor="end" font-family=\'{MONO}\' font-size="12" font-weight="600" fill="{t["text"]}">{html.escape(val)}</text>'
            f'</g>'
        )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="840" height="224" viewBox="0 0 840 224" fill="none" role="img" aria-label="GitHub Engineering Telemetry">
  <defs>
    <clipPath id="lang-bar-clip-{theme_name}">
      <rect x="0" y="0" width="352" height="10" rx="5" />
    </clipPath>
  </defs>

  <rect x="0.5" y="0.5" width="839" height="223" rx="12" fill="{t["bg"]}" stroke="{t["border"]}" stroke-width="1" />

  <!-- Left Panel: Engineering Pulse -->
  <rect x="18" y="18" width="390" height="168" rx="9" fill="{t["panel"]}" stroke="{t["border"]}" stroke-width="1" />
  <text x="36" y="44" font-family='{MONO}' font-size="11.5" font-weight="700" letter-spacing="0.8" fill="{t["accent"]}">ENGINEERING PULSE</text>
  <line x1="36" y1="54" x2="388" y2="54" stroke="{t["border"]}" stroke-width="1" />
  {"".join(left_rows)}

  <!-- Right Panel: Authored Language Mix -->
  <rect x="426" y="18" width="396" height="168" rx="9" fill="{t["panel"]}" stroke="{t["border"]}" stroke-width="1" />
  <text x="446" y="44" font-family='{MONO}' font-size="11.5" font-weight="700" letter-spacing="0.8" fill="{t["accent"]}">AUTHORED LANGUAGE MIX</text>
  <text x="802" y="44" text-anchor="end" font-family='{SANS}' font-size="11" fill="{t["muted"]}">Non-fork public repos</text>
  <line x1="446" y1="54" x2="802" y2="54" stroke="{t["border"]}" stroke-width="1" />

  <g transform="translate(446, 68)">
    <rect width="352" height="10" rx="5" fill="{t["bar_bg"]}" />
    <g clip-path="url(#lang-bar-clip-{theme_name})">
      {"".join(bar_segments)}
    </g>
  </g>
  {"".join(legend_items)}

  <!-- Footer -->
  <text x="20" y="207" font-family='{MONO}' font-size="10.5" fill="{t["muted"]}">Automated via GitHub Actions · @{html.escape(USERNAME)}</text>
  <text x="820" y="207" text-anchor="end" font-family='{MONO}' font-size="10.5" fill="{t["muted"]}">Synced {html.escape(updated_str)}</text>
</svg>
'''


def main() -> None:
    token = get_token()
    user = fetch_github_data(token, USERNAME)

    display_name = user.get("name") or USERNAME
    calendar = user["contributionsCollection"]["contributionCalendar"]
    total_contributions = int(calendar["totalContributions"])
    current_streak, longest_streak, active_days = compute_streaks(calendar.get("weeks", []))

    repos_conn = user["repositories"]
    repos = repos_conn.get("nodes", [])
    total_public_repos = int(repos_conn.get("totalCount", len(repos)))
    original_public_repos = sum(1 for r in repos if not r.get("isFork"))

    latest_repo = USERNAME
    latest_push_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for r in repos:
        if r.get("name") != USERNAME:
            latest_repo = r["name"]
            if r.get("pushedAt"):
                latest_push_date = r["pushedAt"][:10]
            break

    langs = aggregate_languages(repos, exclude_name=USERNAME)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        (ASSETS_DIR / f"hero-{theme}.svg").write_text(
            build_hero_svg(theme, display_name, USERNAME),
            encoding="utf-8",
        )
        (ASSETS_DIR / f"telemetry-{theme}.svg").write_text(
            build_telemetry_svg(
                theme_name=theme,
                total_contributions=total_contributions,
                current_streak=current_streak,
                longest_streak=longest_streak,
                active_days=active_days,
                total_public_repos=total_public_repos,
                original_public_repos=original_public_repos,
                latest_repo=latest_repo,
                latest_push_date=latest_push_date,
                langs=langs,
            ),
            encoding="utf-8",
        )

    print(
        f"Generated SVGs for @{USERNAME}: {total_contributions} contributions, "
        f"{current_streak}d/{longest_streak}d streak, {len(langs)} top languages."
    )


if __name__ == "__main__":
    main()
