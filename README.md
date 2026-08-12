# Gold CNY Monitor

Checks the Shanghai Gold Exchange delayed Au99.99 quote every five minutes and
sends a QQ Mail alert when the absolute change from the previous check reaches
0.5%. If the SGE page is unavailable, it estimates CNY per gram from XAU/USD
spot gold and USD/CNY. It also checks recent gold-market headlines and deduplicates
important-news alerts.

Required GitHub Actions secrets:

- `QQ_EMAIL`
- `QQ_SMTP_AUTH_CODE`

The workflow keeps the previous price in a short-lived private Actions artifact.
It also makes one small monthly maintenance commit so GitHub does not disable
the public repository's scheduled workflows for inactivity. A manual run can
force one test email.
