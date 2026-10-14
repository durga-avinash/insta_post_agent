# Insta Post Agent

A free, review-first daily news carousel generator for DMS IIT Delhi's MBA audience. It gathers public RSS coverage and Google Trends RSS signals, joins reports about the same event, ranks business and policy-relevant stories, and creates up to six editable 1080 × 1350 slides. Instagram posting remains manual.

## What the daily run produces

- Up to three India stories and up to three international stories. The slide count changes with the evidence available; six is a ceiling, not a requirement.
- Current high-interest topics matched against real Google Trends traffic buckets for the same region. The bucket is an approximate lower bound, not an exact query count.
- MBA-focused categories: business and markets, economy and policy, geopolitics and security, healthcare, technology and industry, climate and energy, and infrastructure and supply chains. General entertainment and sports stories are excluded unless the headline has an explicit, substantive business or industry angle.
- Publisher reports are deduplicated by canonical URL and grouped by event. Google News results for a specific trend can join independently worded reports; wire copies and identical descriptions do not count as independent corroboration.
- One source-merged editorial record, one evidence log, and one deterministic source-extractive summary per event. The app does not launch a second article crawl to repeat checks already supported by the collected reports.
- Automated approval only when at least two independent curated trusted publishers or primary sources support the item and the automated consistency checks find no review flags. Current trusted stories with a date, conflict, sensitivity, or corroboration flag remain visible as `NEEDS_REVIEW` drafts so the editor can inspect them; they are not publication-approved.
- Slides are created only for fresh same-region Google Trends matches at or above the high-volume threshold, and only for MBA-relevant topics. If the day has no qualifying story, the daily workbook, evidence report, caption status and ZIP are still produced, but the package contains no carousel slides. It never fills a slot with a non-trending fallback.
- No LLM API is required. Headlines, summaries, takeaways, source checks, artwork, workbook, and package are produced deterministically from collected source material. The optional editor helper is not enabled in the daily workflow.

## Editorial slide design

The renderer rebuilds the uploaded `Daily News Nuggets Editorial Template.png` as editable Pillow text and fixed vector geometry; it does not use the sample as a flattened background. It uses the provided original logos from `assets/dms_iitd_logo.png` and `assets/ppc_logo.jpeg`, preserving their aspect ratios. It has no photographs, people, maps, or story-specific illustrations.

The cream, navy, pale-blue, and yellow layout carries a dynamic region/category strip, headline, source-derived summary, up to three source-authored key takeaways (or source-derived figures), date, publisher names, review/trend labels, and slide count. Reusable lead, short-brief, and statistics layouts share the same header and footer. Text wraps and scales to defined limits; any shortened copy remains complete in the workbook and caption and is reported in `summary.json`.

## Sources, trend evidence, and limits

Publisher feeds are configured in [`config/sources.json`](config/sources.json); Google Trends regions are in [`config/trend_sources.json`](config/trend_sources.json). The collector uses RSS metadata, RSS descriptions, and Google News RSS search results. It does not scrape publisher pages or bypass paywalls. RSS can omit context, event dates, corrections, and full article details, so editors must open the original links before manual publication.

The verification log preserves each retrieved URL, publisher, independence group, headline/description comparison, uncertainty flags, decision, and reason. Unknown publishers can remain visible in the workbook as evidence but cannot make a story automatically approved. A confident model response is never used as fact-check certification.

Google Trends may be dominated by entertainment or sports on a given day, and its public RSS has no MBA-only filter. In that case there may be no qualifying MBA story. The workflow still publishes a daily evidence workbook and status ZIP, but it deliberately produces zero slides rather than calling a merely current article “trending.” This keeps trend claims factual, but it cannot guarantee a publishable carousel every day when no high-interest MBA story is visible in the public trend feed.

## Output package

Each run writes `output/YYYY-MM-DD/` with `Daily_News_Report.xlsx`, `carousel_caption.txt`, `verification_manifest.json`, `summary.json`, zero to six slides, and `content_package.zip`. The ZIP includes the workbook, caption, manifest, and every generated slide. The workbook has `News_Master`, `Verification_Log`, and `Publishing_Queue` sheets. URLs, source text, reasons, trend status, and manual review status remain visible for the team.

## Local use

Python 3.11 or later is supported.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m pytest -q
python -m insta_post_agent.cli --output-root output --history-path state/news_history.sqlite3
```

Required output logos are packaged with the repository. Network access is needed only for the configured public feeds; there are no paid services or required secrets.

Settings in `.env.example` are documented values; the application does not load that file automatically. `NEWS_MIN_TREND_SEARCHES` defaults to `10000` (the approximate lower-bound bucket reported by Google Trends), and `NEWS_TREND_QUERIES_PER_GEO` defaults to `20`. `NEWS_TREND_ARTICLES_PER_QUERY` defaults to `20`. A day dominated by sports or entertainment can legitimately produce fewer than six stories, or none, because unrelated topics are excluded.

## Daily GitHub Actions schedule

`.github/workflows/daily-news.yml` runs at `03:30 UTC`, which is `09:00 India Standard Time (UTC+05:30)`, and supports manual dispatch. Leave `force_rebuild` unchecked for ordinary runs; scheduled runs suppress recently selected events and skip a completed same-day batch. Use it only to deliberately regenerate that day's batch after changing code. GitHub can delay scheduled jobs. The code checks source freshness and trend freshness, so a delayed or missed run will not quietly relabel stale reporting as today's news.

The action restores/saves a small SQLite recent-story file through GitHub Actions cache. Cache is best-effort and can expire; a cache miss is recorded and the current batch continues. Generated content is uploaded as a downloadable artifact retained for 14 days and is not committed to the public repository. Public repository Actions usage is generally free within GitHub's applicable limits; GitHub controls account quotas, artifact storage, cache behavior, and scheduling. No billing integration is enabled. GitHub can disable scheduled runs after a prolonged period without repository activity, so check the Actions page periodically.

## Download and review

1. Open the repository's **Actions** tab and choose **Daily news carousel**.
2. Open the latest successful run and download `daily-news-<run id>`.
3. In the ZIP, review `summary.json` and `verification_manifest.json`, then inspect every slide and the workbook's `Verification_Log`.
4. Open the original reporting linked in the caption or workbook. Resolve every `NEEDS_REVIEW` flag. Trend evidence and source credibility are separate checks; a trend match is not a truth certification.
5. Approve and publish manually only after the team has read the sources. There are no Instagram credentials or autoposting features.

## Tests

Automated tests use synthetic, labelled fixtures and mock network/model responses. They cover feed parsing and retries, dates and canonical URLs, clustering and syndicated-copy checks, trend matching and no-fallback gating, MBA relevance, review gating, extractive summaries, required logo assets and the three template layouts, overflow, workbook structure, variable slide counts, repeat-batch handling, caption links, and ZIP completeness. They run without API keys or live news.

