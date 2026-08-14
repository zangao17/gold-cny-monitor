# Gold CNY Monitor

Checks the Shanghai Gold Exchange delayed Au99.99 quote and sends a QQ Mail
alert at a 0.5% price-change threshold. The primary trigger is an external
cron-job.org HTTP job running every five minutes. GitHub Actions also runs at
minutes 2 and 32 as a fallback because GitHub's own schedules can be delayed.
If the SGE page is unavailable, the monitor estimates CNY per gram from XAU/USD
spot gold and USD/CNY. It also deduplicates important gold-market news alerts.

Required GitHub Actions secrets:

- `QQ_EMAIL`
- `QQ_SMTP_AUTH_CODE`
- `PORTFOLIO_JSON` (optional encrypted holdings and risk-profile configuration)

When `PORTFOLIO_JSON` is configured, alert emails include the latest fund NAV,
indicative accumulated-gold value, combined profit/loss, and a rules-based risk
management reference. Holdings are never committed to this public repository.

The workflow keeps source-specific prices, alert anchors, SGE daily buckets,
and duplicate-suppression markers in a short-lived private Actions artifact.
See [cron-job.org setup](docs/cron-job-org-setup.md) to configure the primary
five-minute trigger. A manual run can force one formatted test email.
