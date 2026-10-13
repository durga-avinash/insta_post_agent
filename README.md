# Insta Post Agent

A zero-cost, review-first daily news carousel generator. It gathers public RSS metadata, groups likely duplicate coverage, applies transparent editorial checks, draws up to six portrait slides with Pillow, and packages a formatted workbook, caption, and evidence manifest. It does not post to Instagram.

## What it does

- Checks a configurable set of India and world RSS feeds. Feed failures are recorded, not hidden.
- Keeps the source headline, publisher, original URL, publication time, retrieval time, RSS description, and canonical URL with each candidate.
- Deduplicates canonical URLs and clusters similar headlines. Ranking is a published editorial proxy: recency (35 points maximum), distinct publisher coverage (40 maximum), and keyword-based public significance (25 maximum). Search interest is unavailable and always scored as zero. Only stories scoring at least `NEWS_MIN_TREND_SCORE` are selected (default 55/100); the score is not a measured search or social trend.
- Approves only clusters with descriptions and timestamped reports from at least two source groups, with no detected uncertainty or sensitive-topic flags. An automated approval is an internal editorial signal, not a fact-check guarantee. Sensitive or developing stories and unresolved disputes need a person to review them. Syndication clues and identical descriptions reduce source-independence credit.
- Each region is ranked separately and can contribute zero to three qualifying stories. Stories below the trend threshold, rejected by the evidence gate, stale, or recently selected are excluded. Safe source-backed `NEEDS_REVIEW` drafts may be included with a visible warning; the threshold does not replace verification. `workflow_complete` means the package files and all selected slides passed checks, whether it contains zero, one, or up to six slides. `publication_ready` stays false for an empty package or if any selected story still needs human review.
- Builds source-bound summaries without an LLM by default. An optional OpenAI-compatible editing helper is available in `editor.py`; it is not enabled in the daily pipeline. No API key is required, and no provider is selected or assumed to be free. If you later wire in a provider, confirm its current free plan and terms first.
- Draws clearly illustrative editorial graphics, not event photographs. Output is 1080 × 1350 pixels (4:5), with text rendered by Pillow.
- Writes `output/YYYY-MM-DD/` with the workbook, caption, evidence manifest, summary, zero to six slides, and a ZIP. A report with no stories above the threshold is a successful empty report with an explanation; it never fills a shortfall with invented stories.

## Sources and usage

The registry lives in [`config/sources.json`](config/sources.json). RSS endpoints are configurable. The Indian Express lists its India and World feeds in its [RSS directory](https://indianexpress.com/rss/); its directory states that feed use is for personal, non-commercial use and reserves its content rights. The [PIB RSS page](https://www.pib.gov.in/ViewRss.aspx?lang=1&reg=1) lists the official press-release feed used here. BBC, NPR, and Guardian XML feed URLs are also configured. Feed availability and access policies can change; each run records its own feed status. The application reads RSS only and does not crawl publisher article pages. Review publisher terms before redistributing or using output commercially.

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

`NEWS_MIN_TREND_SCORE` is read from the process environment and accepts 0–100; it defaults to 55. For example, PowerShell users can set `$env:NEWS_MIN_TREND_SCORE = "55"` before running the CLI. The `.env.example` file documents this setting but is not loaded automatically.

## Daily GitHub Actions schedule

`.github/workflows/daily-news.yml` runs at `03:30 UTC`, which is `09:00 India Standard Time (UTC+05:30)`, and supports manual dispatch. GitHub can delay scheduled jobs, so article publication times are checked against a 36-hour window by default; stale and future-dated items are excluded. Workflow concurrency prevents overlapping runs on the same ref. The SQLite history skips exact or similar events seen in the previous two days, and a completed local-date batch cannot be generated twice.

History is a small SQLite file outside the repository. The workflow restores and saves it through GitHub Actions cache, then artifacts and logs are uploaded without committing generated output. Cache is best-effort: GitHub can evict cache entries after seven days without access, and cache availability/limits are controlled by repository and account settings. If the cache is missing, the run continues with a fresh local database and records the limitation. No private database or generated news package is pushed to Git.

The content artifact is retained for 14 days. After a run, open the repository's **Actions** tab, select **Daily news carousel**, open the latest run, and download the `daily-news-<run id>` artifact. The artifact includes the ZIP, workbook, and batch summary. A run with no stories above the trend threshold still uploads a valid empty report; build failures may upload diagnostics and partial output. Standard GitHub-hosted runners are free for public repositories; artifact/cache storage is still subject to account quotas and retention settings. Public-repository schedules are automatically disabled after 60 days without repository activity, so check the Actions page periodically and re-enable the workflow if GitHub disables it.

GitHub Actions usage and retention depend on the account plan and repository settings. This workflow uses no paid service or billing integration; check the account's Actions usage page before relying on long-term daily runs.

## Review the package

1. Open `summary.json`. Check `workflow_complete`, `batch_status`, `images_generated`, `selected_by_region`, and `slide_limit_respected`. `NO_STORIES_MET_THRESHOLD` means no candidate cleared the configured floor; the report and manifest explain why. Check `publication_ready` and the selected story statuses separately before using any slide.
2. Read `verification_manifest.json` and the workbook's `Verification_Log` before using any story.
3. Check every source link and the full linked article, especially for sensitive or developing stories.
4. Review slide attributions and the caption, then publish manually if appropriate.

The package's `APPROVED` status is not a promise that the source is correct. It reports only that the limited automated checks passed. No Instagram credentials or autoposting code are included.

## Tests

Tests use synthetic, clearly labeled fixtures and mock network/model responses. They cover feed parsing, timestamps, URL normalization, deduplication, clustering, classification, score components and threshold selection, independence and review gating, the no-key fallback, LLM payload validation/retry, image dimensions/layout, variable-size and empty packages, workbook sheets, repeat-story history, caption attribution, and package contents. They do not require API keys or live news.
