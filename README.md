# Insta Post Agent

A free, review-first daily news carousel generator for DMS IIT Delhi's MBA audience. It gathers public RSS coverage and Google Trends RSS signals, joins reports about the same event, ranks business and policy-relevant stories, and creates up to six editable 1080 × 1350 slides. Instagram posting remains manual.

## What the daily run produces

- Up to three India stories and up to three international stories. The slide count changes with the evidence available; six is a ceiling, not a requirement.
- Current high-interest topics matched against real Google Trends traffic buckets for the same region. The bucket is an approximate lower bound, not an exact query count.
- MBA-focused categories: business and markets, economy and policy, geopolitics and security, healthcare, technology and industry, climate and energy, and infrastructure and supply chains. General entertainment and sports stories are excluded unless the headline has an explicit, substantive business or industry angle.
- Publisher reports are deduplicated by canonical URL and grouped by event. Google News results for a specific trend can join independently worded reports; wire copies and identical descriptions collapse to one coverage group.
- One source-merged editorial record, one evidence log, and one deterministic source-extractive summary per event. The app does not launch a second article crawl to repeat checks already supported by the collected reports.
- Source-screen approval accepts a clean, recent report from one curated trusted publisher; the workbook records when an item was not independently corroborated. Material conflicts, sensitive/developing topics, untrusted sources, missing source text, and malformed dates remain `NEEDS_REVIEW`. `APPROVED` means the curated-source and automated checks passed; it is not a truth guarantee or a substitute for the team's manual publishing decision.
- Fresh same-region Google Trends matches at or above the high-volume threshold rank first. When Trends is absent or off-topic, a second signal can identify a story reported by at least three independent trusted publisher groups within 24 hours. The package labels this `CROSS_PUBLISHER_COVERAGE`; it is a source-pickup proxy, not a search-volume or social-media measurement. Known wire copies and duplicated descriptions do not inflate the count. Remaining daily slots can contain current `EDITORIAL_BACKUP` drafts, explicitly marked as not trend-confirmed. Unsupported, stale, untrusted and off-topic items still do not enter slides.
- When the broad source feeds split one event into separate headlines, the fallback searches at most six ranked, current MBA-relevant headlines per region through public Google News RSS. It accepts only recent, dated, trusted reports that match the event and come from a different normalized publisher domain; the 24-hour filter, wire-copy grouping, and duplicated-description check still apply. This bounded publisher-pickup signal improves coverage without pretending to measure X/Twitter activity or search volume.
- No LLM API is required. Headlines, summaries, takeaways, source checks, artwork, workbook, and package are produced deterministically from collected source material. The optional editor helper is not enabled in the daily workflow.

## Editorial slide design

The renderer rebuilds the uploaded `Daily News Nuggets Editorial Template.png` as editable Pillow text and fixed vector geometry; it does not use the sample as a flattened background. It uses the provided original logos from `assets/dms_iitd_logo.png` and `assets/ppc_logo.jpeg`, preserving their aspect ratios. It has no photographs, people, maps, or story-specific illustrations.

The cream, navy, pale-blue, and yellow layout carries a dynamic region/category strip, headline, source-derived summary, up to three source-authored key takeaways (or source-derived figures), date, publisher names, review/trend labels, and slide count. Reusable lead, short-brief, and statistics layouts share the same header and footer. Text wraps and scales to defined limits; any shortened copy remains complete in the workbook and caption and is reported in `summary.json`.

## Sources, trend evidence, and limits

Publisher feeds are configured in [`config/sources.json`](config/sources.json); Google Trends regions are in [`config/trend_sources.json`](config/trend_sources.json). India discovery now includes RSS feeds listed by Business Standard and Hindustan Times, BBC India, and PIB, with a wider business/markets mix to reduce dependence on blocked or empty feeds. The collector uses RSS metadata, RSS descriptions, and Google News RSS search results. It does not scrape publisher pages or bypass paywalls. RSS can omit context, event dates, corrections, and full article details, so editors should open the original links before manual publication.

The verification log preserves each retrieved URL, publisher, independence group, headline/description comparison, uncertainty flags, decision, and reason. Unknown publishers can remain visible in the workbook as evidence but cannot make a story automatically approved. A confident model response is never used as fact-check certification.

Google Trends may be dominated by entertainment or sports on a given day, and its public RSS has no MBA-only filter. Independent-publisher coverage is a practical free fallback. X/Twitter trend counts are not scraped: there is no configured, dependable no-cost public trend endpoint here, and unofficial scraping would be brittle. If neither trend signal qualifies, the workflow can still create a current trusted-source editor backup; if no suitable current story exists, it reports a shortfall instead of fabricating a post. RSS availability and GitHub scheduling cannot guarantee a valid news story on every date.

## Output package

Each run writes `output/YYYY-MM-DD/` with `Daily_News_Report.xlsx`, `carousel_caption.txt`, `verification_manifest.json`, `summary.json`, zero to six slides, and `content_package.zip`. The ZIP includes the workbook, caption, manifest, and every generated slide. The workbook has `News_Master`, `Verification_Log`, and `Publishing_Queue` sheets. URLs, source text, reasons, trend/coverage evidence, and manual review status remain visible for the team. The summary adds per-region counts at the fresh-cluster, MBA-topic, Google Trends, coverage, backup, and selected stages, so an empty India slot can be diagnosed from the artifact.

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

Settings in `.env.example` are documented values; the application does not load that file automatically. `NEWS_MIN_TREND_SEARCHES` defaults to `10000` (the approximate lower-bound bucket reported by Google Trends), and `NEWS_TREND_QUERIES_PER_GEO` defaults to `20`. `NEWS_TREND_ARTICLES_PER_QUERY` defaults to `20`. `NEWS_COVERAGE_QUERIES_PER_REGION` defaults to `6` and caps the targeted coverage fallback at six candidate searches per region. A day dominated by unrelated topics or with too little trusted current reporting can legitimately produce fewer than six stories.

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

Automated tests use synthetic, labelled fixtures and mock network/model responses. They cover feed parsing and retries, dates and canonical URLs, clustering and syndicated-copy checks, Google Trends and independent-coverage selection, single curated-source approval, MBA relevance, editorial backups, non-repeating source-extractive summary/takeaways, required logo assets and the three template layouts, overflow, workbook structure, variable slide counts, repeat-batch handling, caption links, and ZIP completeness. They run without API keys or live news.
