#!/usr/bin/env python3
"""Produce an evidence-based weekly Hacker News popularity digest."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")

API = "https://hn.algolia.com/api/v1/search_by_date"
# Algolia exposes at most 1,000 hits per query. Querying the entire week would
# therefore silently omit older records, so the collector uses two-hour shards.
PER_PAGE = 100
SHARD_HOURS = 2
MAX_PAGES_PER_SHARD = 10
USER_AGENT = "Hermes-HN-weekly-digest/1.0"


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=25) as response:
        return json.load(response)


def page_url(start_epoch: int, end_epoch: int, page: int) -> str:
    filters = f"created_at_i>={start_epoch},created_at_i<{end_epoch}"
    return API + "?" + urlencode(
        {"tags": "story", "numericFilters": filters, "hitsPerPage": PER_PAGE, "page": page}
    )


def clean(value: str | None) -> str:
    return " ".join(unescape(value or "").split())


def fetch_shard(bounds: tuple[int, int]) -> list[dict]:
    start_epoch, end_epoch = bounds
    first = fetch_json(page_url(start_epoch, end_epoch, 0))
    pages = int(first.get("nbPages", 0))
    hits = list(first.get("hits", []))
    if pages > MAX_PAGES_PER_SHARD or int(first.get("nbHits", 0)) > pages * PER_PAGE:
        raise RuntimeError(
            f"Algolia returned a truncated shard ({first.get('nbHits')} hits, {pages} pages) "
            f"for {start_epoch}–{end_epoch}; refusing a partial ranking."
        )
    for page in range(1, pages):
        hits.extend(fetch_json(page_url(start_epoch, end_epoch, page)).get("hits", []))
    return hits


def collect(days: int, *, start: dt.datetime | None = None, end: dt.datetime | None = None) -> tuple[list[dict], dt.datetime, dt.datetime]:
    if (start is None) != (end is None):
        raise ValueError('provide both start and end')
    if start is None:
        end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        start = end - dt.timedelta(days=days)
    assert end is not None
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError('bounds must be timezone-aware and increasing')
    if start.microsecond or end.microsecond:
        raise ValueError('bounds must use whole seconds')
    shard_seconds = SHARD_HOURS * 3600
    start_epoch = int(start.timestamp())
    end_epoch = int(end.timestamp())
    shards = [(cursor, min(cursor + shard_seconds, end_epoch)) for cursor in range(start_epoch, end_epoch, shard_seconds)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        payloads = list(pool.map(fetch_shard, shards))

    stories: dict[str, dict] = {}
    for hits in payloads:
        for hit in hits:
            ident = str(hit.get("objectID", ""))
            created = int(hit.get("created_at_i") or 0)
            title = clean(hit.get("title"))
            if not ident or not title or created < start_epoch or created >= end_epoch:
                continue
            stories[ident] = {
                "id": ident,
                "title": title,
                "tags": hit.get("_tags") or [],
                "url": hit.get("url") or "",
                "score": int(hit.get("points") or 0),
                "comments": int(hit.get("num_comments") or 0),
                "created": dt.datetime.fromtimestamp(created, tz=dt.timezone.utc),
            }
    return list(stories.values()), start, end


def rank(stories: list[dict], metric: str) -> list[dict]:
    return sorted(stories, key=lambda item: (item[metric], item["score"], item["comments"], item["id"]), reverse=True)


def choose(stories: list[dict], metric: str, limit: int, exclude: set[str]) -> list[dict]:
    chosen = []
    for story in rank(stories, metric):
        if story["id"] in exclude:
            continue
        chosen.append(story)
        exclude.add(story["id"])
        if len(chosen) == limit:
            break
    return chosen


def render_item(index: int, story: dict) -> str:
    stamp = story["created"].astimezone(UK).strftime("%a %d %b, %H:%M %Z")
    links = [f"HN discussion: https://news.ycombinator.com/item?id={story['id']}"]
    if story["url"]:
        links.append(f"Source: {story['url']}")
    return f"{index}. **{story['title']}** — {story['score']} points, {story['comments']} comments ({stamp})\n   " + " · ".join(links)


def sections(stories: list[dict], limit: int, show_min_score: int) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    # Keep category sections distinct. Score ranking has precedence: the
    # discussion ranking contains the most-commented remaining ordinary stories.
    ordinary = [story for story in stories if not {"ask_hn", "show_hn"}.intersection(story.get("tags", []))]
    ask = [story for story in stories if "ask_hn" in story.get("tags", [])]
    show = [story for story in stories if "show_hn" in story.get("tags", []) and story["score"] >= show_min_score]
    by_score = rank(ordinary, "score")[:limit]
    by_comments = choose(ordinary, "comments", limit, {story["id"] for story in by_score})
    return by_score, by_comments, rank(ask, "comments")[:limit], rank(show, "score")


def markdown(stories: list[dict], start: dt.datetime, end: dt.datetime, limit: int, show_min_score: int = 50) -> str:
    by_score, by_comments, by_ask, by_show = sections(stories, limit, show_min_score)
    local_start, local_end = start.astimezone(UK), end.astimezone(UK)
    title = f"Hacker News weekly roundup — {local_start.date().isoformat()} to {local_end.date().isoformat()}"
    window = f"Window: {local_start.strftime('%Y-%m-%d %H:%M %Z')} to {local_end.strftime('%Y-%m-%d %H:%M %Z')} (Europe/London)."
    lines = [title, "=" * len(title), "", window, "", "Popularity is measured by Hacker News score (votes) and comment count. Score ranking takes precedence, so the discussion list contains the highest-commented remaining stories. All Show HN threads with at least 50 points are included.", "", f"## Top {limit} stories by votes", ""]
    lines.extend(render_item(i, item) for i, item in enumerate(by_score, 1))
    lines.extend(["", f"## Top {limit} discussed stories", ""])
    lines.extend(render_item(i, item) for i, item in enumerate(by_comments, 1))
    lines.extend(["", f"## Top {limit} Ask HN threads by comments", ""])
    lines.extend(render_item(i, item) for i, item in enumerate(by_ask, 1))
    lines.extend(["", "## All popular Show HN threads (50+ points)", ""])
    if by_show:
        lines.extend(render_item(i, item) for i, item in enumerate(by_show, 1))
    else:
        lines.append("None met the 50-point threshold this week.")
    lines.extend(["", f"Data: Hacker News Search API, collected {end.astimezone(UK).strftime('%Y-%m-%d %H:%M %Z')} (Europe/London)."])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--start", help="Frozen inclusive ISO timestamp, paired with --end")
    parser.add_argument("--end", help="Frozen exclusive ISO timestamp, paired with --start")
    parser.add_argument("--format", choices=("markdown", "json", "selected-json"), default="markdown")
    args = parser.parse_args()
    if args.days < 1 or args.limit < 1:
        parser.error("--days and --limit must be positive")
    try:
        bounds = {key: dt.datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None for key, value in [('start', args.start), ('end', args.end)]}
        stories, start, end = collect(args.days, **bounds)
    except ValueError as exc:
        parser.error(str(exc))
    if args.format == "json":
        print(json.dumps({"start": start.isoformat(), "end": end.isoformat(), "stories": stories}, default=str))
    elif args.format == "selected-json":
        headings = [f"Top {args.limit} stories by votes", f"Top {args.limit} discussed stories", f"Top {args.limit} Ask HN threads by comments", "All popular Show HN threads (50+ points)"]
        selected = sections(stories, args.limit, 50)
        print(json.dumps({"start": start.isoformat(), "end": end.isoformat(), "sections": [{"heading": heading, "items": items} for heading, items in zip(headings, selected)]}, default=str))
    else:
        print(markdown(stories, start, end, args.limit), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"weekly_hn_digest: {exc}", file=sys.stderr)
        raise SystemExit(1)
