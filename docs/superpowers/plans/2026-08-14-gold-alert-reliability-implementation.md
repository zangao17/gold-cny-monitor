# Gold Alert Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent missed 0.5% CNY-gold alerts caused by GitHub schedule gaps, source changes, and gradual price moves.

**Architecture:** Move monitor state into a versioned JSON artifact. Price comparisons are made against history for the same source, while an SGE quote also tracks the current-day opening price in 0.5% alert buckets. cron-job.org dispatches the existing workflow every five minutes; a staggered GitHub schedule remains as a fallback.

**Tech Stack:** Python 3 standard library, `unittest`, GitHub Actions, GitHub REST workflow-dispatch endpoint, cron-job.org.

## Global Constraints

- Alert threshold is exactly `0.005` (0.5%).
- Quote emails state prices and changes only; they do not provide investment advice.
- Do not commit email credentials, GitHub tokens, holdings, or cron-job.org API keys.
- Failed email delivery must not advance price-alert state.
- Existing `LAST_PRICE` and `LAST_SOURCE_KIND` state must migrate on the first upgraded run.

---

### Task 1: Stateful Price-Alert Evaluator

**Files:**
- Create: `tests/test_monitor.py`
- Modify: `monitor.py:76-160`

**Interfaces:**
- Consumes: quote dictionaries with `price`, `source_kind`, and optional `open_price` / `market_time`.
- Produces: `evaluate_price_alert(state, quote, now) -> dict`, `commit_price_state(state, decision, now) -> dict`, `sge_session_buckets(state, quote, now) -> dict`, and `apply_sge_session_buckets(state, session_buckets, now) -> None`.

- [ ] **Step 1: Write the failing state-transition tests**

```python
class AlertEvaluationTests(unittest.TestCase):
    def test_cumulative_same_source_declines_trigger_at_half_percent(self):
        state = monitor.initial_monitor_state()
        state = monitor.commit_price_state(
            state, monitor.evaluate_price_alert(state, quote(1000, "international_estimate"), NOW), NOW
        )
        state = monitor.commit_price_state(
            state, monitor.evaluate_price_alert(state, quote(997, "international_estimate"), NOW), NOW
        )
        decision = monitor.evaluate_price_alert(state, quote(994, "international_estimate"), NOW)
        self.assertTrue(decision["should_alert"])
        self.assertIn("绱", "锛?.join(decision["reasons"]))

    def test_sge_source_switch_uses_sge_history_instead_of_suppressing_alert(self):
        state = monitor.initial_monitor_state()
        state["sources"]["sge"] = {"last_price": 955, "anchor_price": 955}
        decision = monitor.evaluate_price_alert(state, quote(936.2, "sge", 955), NOW)
        self.assertTrue(decision["should_alert"])

    def test_repeated_sge_move_in_same_bucket_does_not_alert_twice(self):
        state = monitor.initial_monitor_state()
        first = monitor.evaluate_price_alert(state, quote(994, "sge", 1000), NOW)
        state = monitor.commit_price_state(state, first, NOW)
        repeated = monitor.evaluate_price_alert(state, quote(993, "sge", 1000), NOW)
        self.assertFalse(repeated["should_alert"])
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `python -m unittest tests.test_monitor -v`

Expected: FAIL because `initial_monitor_state`, `evaluate_price_alert`, and `commit_price_state` do not exist.

- [ ] **Step 3: Add pure state helpers**

```python
STATE_VERSION = 2

def initial_monitor_state():
    return {
        "version": STATE_VERSION,
        "sources": {},
        "sge_session": {"date": None, "up_bucket": 0, "down_bucket": 0},
    }

def evaluate_price_alert(state, quote, now):
    source = quote["source_kind"]
    current = float(quote["price"])
    record = state["sources"].get(source)
    if record is None:
        return {"should_alert": False, "reasons": [], "source": source, "price": current,
                "initialize_source": True, "session_buckets": {}}
    reasons = []
    last_ratio = (current - record["last_price"]) / record["last_price"]
    anchor_ratio = (current - record["anchor_price"]) / record["anchor_price"]
    if abs(last_ratio) >= ALERT_THRESHOLD:
        reasons.append(f"鍚屾簮涓婃妫€鏌ュ彉鍖?{last_ratio:+.2%}")
    if abs(anchor_ratio) >= ALERT_THRESHOLD:
        reasons.append(f"鍚屾簮绱鍙樺寲 {anchor_ratio:+.2%}")
    session_buckets = sge_session_buckets(state, quote, now)
    reasons.extend(session_buckets["reasons"])
    return {"should_alert": bool(reasons), "reasons": reasons, "source": source,
            "price": current, "initialize_source": False, "session_buckets": session_buckets}

def commit_price_state(state, decision, now):
    source = decision["source"]
    record = state["sources"].setdefault(source, {})
    record["last_price"] = decision["price"]
    record["last_checked_at"] = now.isoformat()
    if decision["initialize_source"] or decision["should_alert"]:
        record["anchor_price"] = decision["price"]
        record["anchor_checked_at"] = now.isoformat()
    apply_sge_session_buckets(state, decision["session_buckets"], now)
    return state
```

Implement the SGE parser so `fetch_sge_price()` returns `open_price`, `high_price`, and `low_price` from the Au99.99 row. Treat a source with no stored record as initialization only; never compare it to another source's value.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `python -m unittest tests.test_monitor -v`

Expected: all three state-transition tests PASS.

- [ ] **Step 5: Add remaining boundary tests and rerun**

```python
def test_first_seen_source_has_no_cross_source_alert(self):
    state = monitor.initial_monitor_state()
    state["sources"]["international_estimate"] = {"last_price": 947, "anchor_price": 947}
    decision = monitor.evaluate_price_alert(state, quote(936, "sge", 955), NOW)
    self.assertFalse(decision["should_alert"])

def test_sge_second_half_percent_bucket_alerts_again(self):
    state = monitor.initial_monitor_state()
    first = monitor.evaluate_price_alert(state, quote(994, "sge", 1000), NOW)
    state = monitor.commit_price_state(state, first, NOW)
    decision = monitor.evaluate_price_alert(state, quote(989, "sge", 1000), NOW)
    self.assertTrue(decision["should_alert"])
```

Run: `python -m unittest tests.test_monitor -v`

Expected: all Task 1 tests PASS, including the first-source and new-bucket cases.

- [ ] **Step 6: Commit the evaluator change**

```bash
git add monitor.py tests/test_monitor.py
git commit -m "fix: preserve gold alerts across source changes"
```

### Task 2: Integrate State, Email Reasons, and Workflow Artifact

**Files:**
- Modify: `monitor.py:349-639`
- Modify: `.github/workflows/gold-monitor.yml:35-90`
- Test: `tests/test_monitor.py`

**Interfaces:**
- Consumes: `state/monitor_state.json` when present, or legacy `LAST_PRICE` / `LAST_SOURCE_KIND` environment values on migration.
- Produces: `state/monitor_state.json` after a completed run and an email reason list containing exact price-alert causes.

- [ ] **Step 1: Write the failing integration tests**

```python
def test_price_state_is_not_committed_when_email_send_raises(self):
    state = monitor.initial_monitor_state()
    decision = monitor.evaluate_price_alert(state, quote(994, "international_estimate"), NOW)
    with self.assertRaises(OSError):
        monitor.deliver_alert_and_commit(state, decision, NOW, raise_send_error)
    self.assertEqual(state["sources"], {})

def test_email_reasons_include_sge_open_move(self):
    reasons = ["涓婇噾鎵€ Au99.99 杈冧粖寮€鐩樹笅璺?-1.97%"]
    html = monitor.build_email_html(quote(936.2, "sge", 955), -18.8, -0.0197, "time", None, reasons, None)
    self.assertIn("杈冧粖寮€鐩?, html)

def test_legacy_price_environment_migrates_to_state(self):
    with patch.dict(os.environ, {"LAST_PRICE": "947.84", "LAST_SOURCE_KIND": "international_estimate"}, clear=True):
        state = monitor.load_monitor_state()
    self.assertEqual(state["sources"]["international_estimate"]["last_price"], 947.84)
```

- [ ] **Step 2: Run the integration tests and verify they fail**

Run: `python -m unittest tests.test_monitor -v`

Expected: FAIL because `deliver_alert_and_commit` and state-file loading are not implemented.

- [ ] **Step 3: Implement atomic state persistence and mail ordering**

```python
def load_monitor_state():
    path = os.environ.get("STATE_FILE", "state/monitor_state.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as source:
            state = json.load(source)
        if state.get("version") == STATE_VERSION:
            return state
    state = initial_monitor_state()
    legacy_price = os.environ.get("LAST_PRICE", "").strip()
    legacy_source = os.environ.get("LAST_SOURCE_KIND", "").strip()
    if legacy_price and legacy_source:
        price = float(legacy_price)
        state["sources"][legacy_source] = {"last_price": price, "anchor_price": price}
    return state

def save_monitor_state(state):
    path = os.environ.get("STATE_FILE", "state/monitor_state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as destination:
        json.dump(state, destination, ensure_ascii=False, sort_keys=True)
    os.replace(temporary_path, path)

def deliver_alert_and_commit(state, decision, now, send):
    send()
    return commit_price_state(state, decision, now)
```

Set `STATE_FILE=state/monitor_state.json` in the workflow. Remove separate `last_price.txt` and `last_source_kind.txt` writes after migration. Preserve `last_news_id` and `last_portfolio_risk` inside the JSON document.

Update `main()` so it prints the change from the same source's latest previous check, adds all new price reasons to the existing email layout, sends the email, then persists committed state. If SMTP raises, let the job fail before saving the state artifact.

- [ ] **Step 4: Run all unit tests and verify they pass**

Run: `python -m unittest discover -s tests -v`

Expected: all state, email-reason, migration, and failed-send tests PASS.

- [ ] **Step 5: Validate workflow syntax and state wiring**

Run: `python -c "import pathlib; print(pathlib.Path('.github/workflows/gold-monitor.yml').read_text(encoding='utf-8'))"`

Check: the restore step creates `state/`; `STATE_FILE` points to `state/monitor_state.json`; upload still saves the whole `state/` directory; no step overwrites JSON after `monitor.py` exits.

- [ ] **Step 6: Commit workflow integration**

```bash
git add monitor.py .github/workflows/gold-monitor.yml tests/test_monitor.py
git commit -m "fix: persist gold alert anchors after email delivery"
```

### Task 3: Five-Minute External Trigger and Operator Documentation

**Files:**
- Modify: `.github/workflows/gold-monitor.yml:3-12`
- Modify: `README.md:1-20`
- Create: `docs/cron-job-org-setup.md`

**Interfaces:**
- Consumes: cron-job.org HTTP `POST` request with `Authorization: Bearer <fine-grained token>` and `{"ref":"main"}` JSON body.
- Produces: a `workflow_dispatch` run every five minutes plus a GitHub schedule fallback at minutes 2 and 32.

- [ ] **Step 1: Write the failing configuration assertions**

```python
def test_workflow_keeps_manual_dispatch_and_uses_staggered_fallback():
    workflow = pathlib.Path(".github/workflows/gold-monitor.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert 'cron: "2,32 * * * *"' in workflow
```

- [ ] **Step 2: Run the configuration assertion and verify it fails**

Run: `python -m unittest tests.test_monitor.GoldMonitorWorkflowTests.test_workflow_keeps_manual_dispatch_and_uses_staggered_fallback -v`

Expected: FAIL because the workflow still contains `*/5`.

- [ ] **Step 3: Update workflow and write operator instructions**

```yaml
schedule:
  - cron: "2,32 * * * *"
workflow_dispatch:
```

Document these cron-job.org values without including a token:

```text
URL: https://api.github.com/repos/zangao17/gold-cny-monitor/actions/workflows/gold-monitor.yml/dispatches
Method: POST
Header: Accept: application/vnd.github+json
Header: Authorization: Bearer `<new Actions-write-only token>`
Header: X-GitHub-Api-Version: 2026-03-10
Header: Content-Type: application/json
Body: {"ref":"main"}
Minutes: 0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55
```

State that the external token must be a separate fine-grained token restricted to this repository with `Actions: write` only. Update README to explain the primary external trigger, fallback behavior, and that GitHub schedules can be delayed.

- [ ] **Step 4: Run the full test suite and verify it passes**

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS, including the workflow schedule assertion.

- [ ] **Step 5: Commit trigger configuration and documentation**

```bash
git add .github/workflows/gold-monitor.yml README.md docs/cron-job-org-setup.md tests/test_monitor.py
git commit -m "docs: configure reliable five-minute gold monitor trigger"
```

### Task 4: Publish and Verify Cloud Operation

**Files:**
- Modify: `publish-design-doc.ps1`
- Create: `publish-gold-monitor.ps1`

**Interfaces:**
- Consumes: a short-lived GitHub token with `Contents: write` and `Actions: write` for publishing code.
- Produces: deployed monitor files, a forced test-email run, and a documented external scheduler setup.

- [ ] **Step 1: Build a publisher that uploads only expected files**

Create `publish-gold-monitor.ps1` using the working `gh api` pattern. It must upload only `monitor.py`, `.github/workflows/gold-monitor.yml`, `README.md`, `docs/cron-job-org-setup.md`, and the two spec/plan documents. It must not upload `PORTFOLIO_JSON`, email credentials, local artifacts, tests that expose secrets, or tokens.

- [ ] **Step 2: Publish and run a forced cloud email test**

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\HP\Desktop\瀛︿範agent\publish-gold-monitor.ps1"`

Expected: all listed files report published; `gh workflow run gold-monitor.yml --repo zangao17/gold-cny-monitor -f send_test_email=true` starts; GitHub run ends successfully; QQ Mail receives one formatted test message.

- [ ] **Step 3: Configure cron-job.org with a separate token and inspect runs**

Create the HTTP POST job from `docs/cron-job-org-setup.md`. Check its first three executions and the matching GitHub Action runs. Verify each has `event=workflow_dispatch`, starts near its five-minute slot, restores `state/monitor_state.json`, and does not send a duplicate email without a new alert.

- [ ] **Step 4: Revoke the short-lived publishing token and retain only the external Actions-only token**

Verify: the publishing token is removed from GitHub token settings after deployment; the cron-job.org token is restricted to `Actions: write` on `gold-cny-monitor`.
