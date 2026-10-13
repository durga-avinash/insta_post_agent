# Insta Post Agent

A zero-cost, review-first daily news carousel generator. It gathers public publisher RSS metadata and Google Trends RSS signals, groups likely duplicate coverage, applies transparent editorial checks, draws up to six portrait slides with Pillow, and packages a formatted workbook, caption, and evidence manifest. It does not post to Instagram.

## What it does

- Checks a configurable set of India and world RSS feeds. Feed failures are recorded, not hidden.
- Keeps the source headline, publisher, original URL, publication time, retrieval time, RSS description, and canonical URL with each candidate.
- Deduplicates canonical URLs and clusters similar headlines. It reads Google's public Trending Now RSS exports for India, the United States, and the United Kingdom. A slide is labeled `TRENDING` only when a dated query or its related Google Trends news headline matches a retrieved news headline and its approximate traffic bucket meets `NEWS_MIN_TREND_SEARCHES` (default 10,000). The bucket is an approximate lower bound, not an exact search count; international signals identify a country market, not worldwide totals.
- Approves only clusters with descriptions and timestamped reports from at least two source groups, with no detected uncertainty or sensitive-topic flags. An automated approval is an internal editorial signal, not a fact-check guarantee. Sensitive or developing stories and unresolved disputes need a person to review them. Syndication clues and identical descriptions reduce source-independence credit.
- India and international results are ranked separately, with a hard maximum of three stories in each region; fewer are allowed. If a region has no eligible Google Trends match, the generator uses safe, recent, source-backed newsroom fallback drafts to avoid leaving the team with an empty package. Each fallback slide and caption is labeled `TREND UNCONFIRMED`; it must never be described as trending. When there are no safe fresh articles at all, the batch reports that shortfall rather than inventing a story. `publication_ready` is true only when every selected item passes the automated source checks and has a trend match. `NEEDS_REVIEW` content requires the editor to open and compare the original reports before publication.
- Builds source-bound summaries without an LLM by default. An optional OpenAI-compatible editing helper is available in `editor.py`; it is not enabled in the daily pipeline. No API key is required, and no provider is selected or assumed to be free. If you later wire in a provider, confirm its current free plan and terms first.
- Draws clearly illustrative editorial graphics, not event photographs. Output is 1080 × 1350 pixels (4:5), with text rendered by Pillow.
- Writes `output/YYYY-MM-DD/` with the workbook, caption, evidence manifest, summary, zero to six slides, and a ZIP. The manifest retains feed failures, raw trend queries, approximate volumes, associated news links, match evidence, and verification reasons.

## Sources and usage

Publisher feeds live in [`config/sources.json`](config/sources.json); trend feeds are configured in [`config/trend_sources.json`](config/trend_sources.json). Google documents RSS as an export format on its [Trending Now help page](https://support.google.com/trends/answer/3076011). Publisher feed availability and access policies can change; each run records feed status and continues when an individual feed fails. The application reads RSS only and does not crawl publisher article pages. Review publisher terms before redistributing or using output commercially.

RSS headlines and short descriptions can omit context, corrections, or the original event date. In particular, the workbook's event-date field stays blank unless a separate event date is available; a publication date is shown as a publication date, not silently relabeled as the event date. The manifest explains these limits and retains the retrieved source fields for review.

## Local use

Python 3.11 or later is the supported runtime.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m pytest -q
python -m insta_post_agent.cli --output-root output --history-path state/news_history.sqlite3
```

The app needs outbound HTTPS to the configured public RSS endpoints. The optional model variables in `.env.example` are not needed. Do not place credentials in the public repository.

`NEWS_MIN_TREND_SEARCHES` is read from the process environment and defaults to `10000`; set it to raise or lower the approximate Google Trends traffic bucket required for a `TRENDING` label. GitHub Actions allows a same-day retry to regenerate a package; local runs suppress a completed duplicate by default. The `.env.example` file documents these settings but is not loaded automatically.

## Daily GitHub Actions schedule

`.github/workflows/daily-news.yml` runs at `03:30 UTC`, which is `09:00 India Standard Time (UTC+05:30)`, and supports manual dispatch. GitHub can delay scheduled jobs, so article publication times are checked against a 36-hour window by default; stale and future-dated items are excluded. Workflow concurrency prevents overlapping runs on the same ref. The SQLite history skips exact or similar events seen in the previous two days, and a completed local-date batch cannot be generated twice.

History is a small SQLite file outside the repository. The workflow restores and saves it through GitHub Actions cache, then artifacts and logs are uploaded without committing generated output. Cache is best-effort: GitHub can evict cache entries after seven days without access, and cache availability/limits are controlled by repository and account settings. If the cache is missing, the run continues with a fresh local database and records the limitation. No private database or generated news package is pushed to Git. Workflow retries rebuild today's package so a same-day rerun does not stop before artifact creation; recent stories are still excluded during selection.

The content artifact is retained for 14 days. After a run, open the repository's **Actions** tab, select **Daily news carousel**, open the latest run, and download the `daily-news-<run id>` artifact. The artifact includes the ZIP, workbook, and batch summary. If no safe fresh story is available, the workflow still uploads a valid zero-slide report explaining the shortfall; build failures may upload diagnostics and partial output. Standard GitHub-hosted runners are free for public repositories; artifact/cache storage is still subject to account quotas and retention settings. Public-repository schedules are automatically disabled after 60 days without repository activity, so check the Actions page periodically and re-enable the workflow if GitHub disables it.

GitHub Actions usage and retention depend on the account plan and repository settings. This workflow uses no paid service or billing integration; check the account's Actions usage page before relying on long-term daily runs.

## Review the package

1. Open `summary.json`. Check `workflow_complete`, `content_available`, `trend_selection_mode`, `images_generated`, `selected_by_region`, and `slide_limit_respected`. `GOOGLE_TRENDS_MATCHED` means each selected story matched dated search evidence. `NEWSROOM_FALLBACK_UNCONFIRMED` means those stories were source-backed but Google Trends did not confirm them.
2. Read `verification_manifest.json` and the workbook's `Verification_Log` before using any story.
3. Check every source link and the full linked article, especially for sensitive or developing stories.
4. Review slide attributions and the caption, then publish manually if appropriate.

The package's `APPROVED` status is not a guarantee that every fact is correct. It reports only that the limited automated checks passed. The source RSS descriptions can omit context or corrections; an editor should check the original reports before publication. No Instagram credentials or autoposting code are included.

## Tests

Tests use synthetic, clearly labeled fixtures and mock network/model responses. They cover publisher and Google Trends RSS parsing, approximate traffic buckets, trend matching and freshness, timestamps, URL normalization, deduplication, clustering, classification, independence and review gating, the no-key fallback, LLM payload validation/retry, image dimensions/layout, variable-size and empty packages, workbook sheets, repeat-story history, caption attribution, and package contents. They do not require API keys or live news.
