# cron-job.org setup

Create a new HTTP job in cron-job.org with the following values. Do not paste a
token into this repository, an issue, an email body, or a chat.

| Field | Value |
| --- | --- |
| URL | `https://api.github.com/repos/zangao17/gold-cny-monitor/actions/workflows/gold-monitor.yml/dispatches` |
| Method | `POST` |
| Schedule | Every 5 minutes: `0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55` |
| Request body | `{"ref":"main"}` |
| Header | `Accept: application/vnd.github+json` |
| Header | `Authorization: Bearer <external-actions-token>` |
| Header | `X-GitHub-Api-Version: 2026-03-10` |
| Header | `Content-Type: application/json` |

Create a separate fine-grained GitHub personal access token for this job. Set
the resource owner to `zangao17`, limit repository access to
`gold-cny-monitor`, and grant only **Actions: Read and write**. Give the token
a short expiration and renew it before it expires. The publishing token is not
the scheduler token and should be revoked after deployment.

After saving the cron-job.org job, run it once manually. In GitHub Actions, the
matching run should show `workflow_dispatch`. The monitor may start a little
after the five-minute slot when GitHub's hosted runner is queued; the half-hour
GitHub schedule is only a fallback and should not be relied on for five-minute
delivery.
