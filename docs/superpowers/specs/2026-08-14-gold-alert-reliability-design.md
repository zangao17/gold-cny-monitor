# Gold Alert Reliability Design

## Goal

Prevent missed gold-price alerts when GitHub's scheduled workflow is delayed, when the quote source changes between Shanghai Gold Exchange (SGE) and an international CNY estimate, or when a large move is made up of several smaller checks.

The alert threshold remains 0.5%. Messages report prices and changes only and do not provide investment advice.

## Triggering Architecture

- Use cron-job.org as the primary five-minute trigger. It sends an authenticated `POST` request to GitHub's `workflow_dispatch` endpoint for `gold-monitor.yml` on `main`.
- Store the GitHub token only in cron-job.org as an `Authorization` header. Use a fine-grained token restricted to `zangao17/gold-cny-monitor` with only `Actions: write` permission.
- Keep a staggered GitHub `schedule` trigger every 30 minutes as a fallback. This avoids depending on GitHub's delayed scheduler for the five-minute requirement while retaining a recovery path if the external trigger is disabled.
- Keep workflow concurrency enabled so overlapping external and fallback runs are serialized.

The system aims to start a check every five minutes, but GitHub runner queueing can still add a short delay. cron-job.org execution history and GitHub Actions history provide the two audit trails.

## Price State

Replace the single previous-price artifact with a versioned JSON state file. Migrate the existing price and source into the new state on the first upgraded run.

For each source (`sge` and `international_estimate`), persist:

- latest checked price and timestamp;
- alert anchor price and timestamp;
- last successfully alerted direction.

Also persist:

- SGE trading date;
- highest upward and downward 0.5% session-move buckets already alerted;
- last news identifier;
- last portfolio-risk state.

State is uploaded as the existing `gold-price-state` artifact. The new state replaces the current separate text files after successful migration.

## Alert Evaluation

Each run reports the change from the latest previous check for the same source. A price email is sent when any of these conditions is newly met:

1. The same-source change since the previous check reaches 0.5%.
2. The same-source cumulative change from its alert anchor reaches 0.5%.
3. During the SGE day session, the move from the current day's Au99.99 opening price enters a new 0.5% severity bucket.

Source changes do not suppress evaluation. They select the matching source history instead. If a source has never been seen, that source is initialized without comparing unlike prices.

After a successfully sent price email, reset that source's cumulative anchor to the current price. For the SGE open-price rule, remember the highest bucket sent in each direction so repeated checks inside the same bucket do not send duplicates. A move from -0.6% to -1.1% is a new alert; repeated prices around -1.1% are not.

News and portfolio-risk alerts retain their existing independent deduplication rules.

## Failure Handling

- If quote retrieval fails, fail the workflow and retain the previous artifact.
- If email sending fails, fail the workflow before advancing price-alert anchors or session buckets, allowing the next run to retry.
- Validate restored state. If the JSON is missing or invalid, migrate legacy files when possible; otherwise initialize safely and log the reset.
- Never print email credentials or authorization tokens.

## Email Content

Keep the existing readable HTML layout. A price alert identifies each trigger separately:

- previous-check change;
- cumulative change from the alert anchor;
- SGE change from today's opening price.

It includes current price, absolute and percentage changes, quote time, source links, and a note when a delayed monitor run has resumed.

## Testing

Add unit tests for the pure alert-evaluation and state-transition logic:

- several sub-threshold declines cumulatively cross 0.5%;
- switching from international to SGE uses SGE history rather than suppressing alerts;
- a first-ever source initializes without a false cross-source alert;
- an SGE move from open triggers once per new 0.5% bucket;
- repeated checks in the same bucket do not resend;
- a failed email leaves alert state eligible for retry;
- legacy state migrates to the versioned format.

Run the tests locally, run a forced cloud test email, then inspect several five-minute external dispatches and their saved state before considering the repair complete.
