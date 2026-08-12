import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.header import Header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import escape, unescape
from zoneinfo import ZoneInfo


SGE_URL = "https://www.sge.com.cn/sjzx/yshqbg?ad_check=1"
GOLD_API_USD_URL = "https://api.gold-api.com/price/XAU"
GOLD_API_CNY_URL = "https://api.gold-api.com/price/XAU/CNY"
FUND_PAGE_URL = "https://fund.eastmoney.com/{code}.html"
NEWS_URL = (
    "https://news.google.com/rss/search?"
    + urllib.parse.urlencode(
        {
            "q": "gold price when:1h",
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )
)
ALERT_THRESHOLD = 0.005
TROY_OUNCE_GRAMS = 31.1034768
IMPORTANT_NEWS_TERMS = (
    "federal reserve",
    "interest rate decision",
    "emergency rate",
    "central bank gold",
    "record high",
    "all-time high",
    "gold surge",
    "gold soars",
    "gold plunge",
    "gold crash",
    "tariff",
    "sanction",
    "ceasefire",
    "\u7f8e\u8054\u50a8",
    "\u592e\u884c\u8d2d\u91d1",
    "\u521b\u65b0\u9ad8",
    "\u66b4\u6da8",
    "\u66b4\u8dcc",
)


def fetch_bytes(url, timeout=25):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/124.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_json(url):
    return json.loads(fetch_bytes(url).decode("utf-8"))


def fetch_sge_price():
    html = fetch_bytes(SGE_URL).decode("utf-8", errors="replace")
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text)
    price_match = re.search(r"Au99\.99\s*\|?\s*([0-9]+(?:\.[0-9]+)?)", text)
    date_match = re.search(
        r"(\d{4})\u5e74(\d{2})\u6708(\d{2})\u65e5\u5ef6\u65f6\u884c\u60c5",
        text,
    )
    if not price_match:
        raise RuntimeError("Au99.99 was not found on the SGE delayed quote page")

    price = float(price_match.group(1))
    if not 100 <= price <= 3000:
        raise RuntimeError(f"SGE returned an implausible Au99.99 price: {price}")
    return {
        "price": price,
        "market_time": "-".join(date_match.groups()) if date_match else "unknown",
        "instrument": "Au99.99",
        "source_kind": "sge",
        "source": "Shanghai Gold Exchange delayed quote",
        "source_urls": [SGE_URL],
        "estimated": False,
    }


def fetch_estimated_price():
    usd_quote = fetch_json(GOLD_API_USD_URL)
    cny_quote = fetch_json(GOLD_API_CNY_URL)
    xau_usd = float(usd_quote["price"])
    usd_cny = float(cny_quote["exchangeRate"])
    price = xau_usd * usd_cny / TROY_OUNCE_GRAMS
    if not 100 <= price <= 3000:
        raise RuntimeError(f"Fallback sources returned an implausible price: {price}")
    return {
        "price": price,
        "market_time": usd_quote.get("updatedAt", "unknown"),
        "instrument": "XAU/USD x USD/CNY",
        "source_kind": "estimate",
        "source": "Gold API spot gold and exchange-rate estimate",
        "source_urls": [GOLD_API_USD_URL, GOLD_API_CNY_URL],
        "estimated": True,
        "xau_usd": xau_usd,
        "usd_cny": usd_cny,
    }


def fetch_quote():
    try:
        return fetch_sge_price()
    except Exception as error:
        print(f"SGE fetch failed; using estimated fallback: {error}", file=sys.stderr)
        return fetch_estimated_price()


def load_portfolio():
    raw = os.environ.get("PORTFOLIO_JSON", "").strip()
    if not raw:
        return None
    portfolio = json.loads(raw)
    required = (
        "fund_code",
        "fund_name",
        "fund_shares",
        "fund_cost_nav",
        "gold_grams",
        "gold_cost_per_gram",
        "gold_allocation_pct",
        "max_loss_pct",
        "risk_profile",
    )
    missing = [key for key in required if key not in portfolio]
    if missing:
        raise RuntimeError(f"Portfolio configuration is missing: {', '.join(missing)}")
    return portfolio


def fetch_fund_nav(code):
    url = FUND_PAGE_URL.format(code=code)
    html = fetch_bytes(url).decode("utf-8", errors="replace")
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text)
    patterns = (
        r"\u5355\u4f4d\u51c0\u503c\s*\((\d{4}-\d{2}-\d{2})\)\s*([0-9]+(?:\.[0-9]+)?)",
        r"\u5355\u4f4d\u51c0\u503c\s*\((\d{2}-\d{2})\)\s*[\uff1a:]\s*([0-9]+(?:\.[0-9]+)?)",
    )
    match = next((re.search(pattern, text) for pattern in patterns if re.search(pattern, text)), None)
    if not match:
        raise RuntimeError(f"Latest NAV for fund {code} was not found")
    nav = float(match.group(2))
    if not 0.1 <= nav <= 100:
        raise RuntimeError(f"Fund {code} returned an implausible NAV: {nav}")
    return {"nav": nav, "date": match.group(1), "url": url}


def build_portfolio_report(portfolio, quote):
    try:
        fund = fetch_fund_nav(str(portfolio["fund_code"]))
    except Exception as error:
        fund = None
        print(f"Fund NAV check failed: {error}", file=sys.stderr)
    fund_shares = float(portfolio["fund_shares"])
    fund_cost_nav = float(portfolio["fund_cost_nav"])
    gold_grams = float(portfolio["gold_grams"])
    gold_cost_per_gram = float(portfolio["gold_cost_per_gram"])
    allocation = float(portfolio["gold_allocation_pct"])
    max_loss = float(portfolio["max_loss_pct"])

    fund_cost = fund_shares * fund_cost_nav
    fund_value = fund_shares * fund["nav"] if fund else None
    fund_profit = fund_value - fund_cost if fund_value is not None else None
    fund_return = fund_profit / fund_cost if fund_profit is not None else None

    # ICBC's public web quote is not reliably machine-readable. Use the monitored
    # Au99.99/estimated CNY price as an indicative mark and label it clearly.
    gold_mark = quote["price"]
    gold_cost = gold_grams * gold_cost_per_gram
    gold_value = gold_grams * gold_mark
    gold_profit = gold_value - gold_cost
    gold_return = gold_profit / gold_cost
    combined_cost = fund_cost + gold_cost
    combined_value = fund_value + gold_value if fund_value is not None else None
    combined_profit = combined_value - combined_cost if combined_value is not None else None
    combined_return = combined_profit / combined_cost if combined_profit is not None else None

    if combined_return is not None and combined_return <= -(max_loss / 100):
        risk_code = "max_loss"
        action = (
            f"\u5408\u8ba1\u4e8f\u635f\u5df2\u8fbe\u4f60\u8bbe\u5b9a\u7684 {max_loss:.0f}% \u4e0a\u9650\uff1a"
            "\u505c\u6b62\u52a0\u4ed3\uff0c\u68c0\u67e5\u6301\u4ed3\u662f\u5426\u9700\u8981\u5206\u6279\u964d\u4f4e\u3002"
        )
    elif allocation >= 40:
        risk_code = "high_allocation_plan_v1"
        target_allocation = 35.0
        action = (
            "\u9ec4\u91d1\u5360\u53ef\u6295\u8d44\u8d44\u4ea7\u7ea6 "
            f"{allocation:.0f}%\uff0c\u4e0e\u7a33\u5065\u578b\u548c\u6700\u5927\u53ef\u63a5\u53d7\u4e8f\u635f "
            f"{max_loss:.0f}% \u4e0d\u592a\u5339\u914d\uff1a\u6682\u505c\u52a0\u4ed3\u3002"
        )
        if combined_value is not None and fund:
            reduction_value = combined_value * (1 - target_allocation / allocation)
            reduction_shares = min(fund_shares, reduction_value / fund["nav"])
            action += (
                f"\u82e5\u8981\u628a\u5360\u6bd4\u9010\u6b65\u964d\u5230\u7ea6 {target_allocation:.0f}%\uff0c"
                f"\u53c2\u8003\u51cf\u5c11\u7ea6 {reduction_value:.0f} \u5143\uff0c"
                f"\u7ea6\u7b49\u4e8e\u57fa\u91d1 {reduction_shares:.0f} \u4efd\uff1b"
                f"\u53ef\u5206 3 \u6b21\uff0c\u6bcf\u6b21\u7ea6 {reduction_value / 3:.0f} \u5143/"
                f"{reduction_shares / 3:.0f} \u4efd\u3002"
            )
            if float(portfolio.get("fund_redemption_fee_pct", 0)) == 0:
                action += (
                    "\u4f18\u5148\u8003\u8651\u5df2\u6301\u6709\u8d85\u8fc7 30 \u5929\u3001"
                    "\u5f53\u524d\u4f30\u7b97\u8d4e\u56de\u8d39\u4e3a 0% \u7684\u57fa\u91d1\u3002"
                )
        action += "\u4e0d\u56e0\u5355\u6b21 5 \u5206\u949f\u6da8\u8dcc\u76f4\u63a5\u4ea4\u6613\u3002"
    else:
        risk_code = "normal"
        action = "\u4ed3\u4f4d\u672a\u89e6\u53d1\u98ce\u9669\u7ebf\uff1a\u4ee5\u6301\u6709\u89c2\u5bdf\u4e3a\u4e3b\uff0c\u907f\u514d\u8ffd\u6da8\u3002"

    fund_line = (
        (
            f"{portfolio['fund_name']} ({portfolio['fund_code']})\uff1a"
            f"\u51c0\u503c {fund['nav']:.4f} ({fund['date']})\uff0c"
            f"\u5e02\u503c {fund_value:.2f} \u5143\uff0c"
            f"\u6d6e\u52a8\u76c8\u4e8f {fund_profit:+.2f} \u5143 ({fund_return:+.2%})"
        )
        if fund
        else (
            f"{portfolio['fund_name']} ({portfolio['fund_code']})\uff1a"
            "\u672c\u8f6e\u672a\u53d6\u5230\u6700\u65b0\u51c0\u503c\uff0c\u6682\u4e0d\u4f30\u7b97\u57fa\u91d1\u76c8\u4e8f\u3002"
        )
    )
    lines = [
        "\u6301\u4ed3\u53c2\u8003\uff08\u4ec5\u4f9b\u98ce\u9669\u7ba1\u7406\uff09\uff1a",
        fund_line,
        (
            f"\u5de5\u884c\u79ef\u5b58\u91d1\uff1a{gold_grams:.4f} \u514b\uff0c"
            f"\u6309\u76d1\u63a7\u91d1\u4ef7 {gold_mark:.2f} \u5143/\u514b\u4f30\u7b97\uff0c"
            f"\u6d6e\u52a8\u76c8\u4e8f {gold_profit:+.2f} \u5143 ({gold_return:+.2%})"
        ),
        (
            f"\u5408\u8ba1\uff1a\u4f30\u7b97\u5e02\u503c {combined_value:.2f} \u5143\uff0c"
            f"\u6d6e\u52a8\u76c8\u4e8f {combined_profit:+.2f} \u5143 ({combined_return:+.2%})"
            if combined_value is not None
            else "\u5408\u8ba1\uff1a\u57fa\u91d1\u51c0\u503c\u7f3a\u5931\uff0c\u672c\u8f6e\u4e0d\u8ba1\u7b97\u5408\u8ba1\u76c8\u4e8f\u3002"
        ),
        f"\u53c2\u8003\u5224\u65ad\uff1a{action}",
        (
            "\u6ce8\uff1a\u79ef\u5b58\u91d1\u4e3a\u53c2\u8003\u4f30\u503c\uff0c\u672a\u6263\u5de5\u884c\u5b9e\u65f6\u4e70\u5356\u4ef7\u5dee\u6216\u8d39\u7528\uff1b"
            "\u5b9e\u9645\u4ea4\u6613\u4ee5\u5de5\u884c APP \u62a5\u4ef7\u4e3a\u51c6\u3002"
        ),
        f"\u57fa\u91d1\u51c0\u503c\u6765\u6e90\uff1a{FUND_PAGE_URL.format(code=portfolio['fund_code'])}",
    ]
    return {
        "text": "\n".join(lines),
        "combined_return": combined_return,
        "combined_value": combined_value,
        "combined_profit": combined_profit,
        "allocation": allocation,
        "action": action,
        "risk_code": risk_code,
        "fund": fund,
        "fund_name": portfolio["fund_name"],
        "fund_code": portfolio["fund_code"],
        "fund_shares": fund_shares,
        "fund_value": fund_value,
        "fund_profit": fund_profit,
        "fund_return": fund_return,
        "gold_grams": gold_grams,
        "gold_mark": gold_mark,
        "gold_value": gold_value,
        "gold_profit": gold_profit,
        "gold_return": gold_return,
    }


def fetch_important_news(now):
    root = ET.fromstring(fetch_bytes(NEWS_URL))
    cutoff = now.astimezone(ZoneInfo("UTC")) - timedelta(hours=1, minutes=10)
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published_text = (item.findtext("pubDate") or "").strip()
        title_lower = title.lower()
        if not any(term.lower() in title_lower for term in IMPORTANT_NEWS_TERMS):
            continue
        try:
            published = parsedate_to_datetime(published_text)
        except (TypeError, ValueError):
            continue
        if published < cutoff:
            continue
        return {
            "id": hashlib.sha256(link.encode("utf-8")).hexdigest(),
            "title": title,
            "link": link,
            "published": published.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
                "%Y-%m-%d %H:%M:%S Asia/Shanghai"
            ),
        }
    return None


def build_email_html(quote, change, change_ratio, timestamp, portfolio_report, reasons, news):
    def value_color(value):
        if value is None or value == 0:
            return "#4b5563"
        return "#087f5b" if value > 0 else "#c92a2a"

    if change is None:
        change_value = "--"
        change_percent = "\u9996\u6b21\u68c0\u67e5"
    else:
        change_value = f"{change:+.2f} \u5143/\u514b"
        change_percent = f"{change_ratio:+.2%}"
    change_color = value_color(change)
    estimate_note = " (\u4f30\u7b97)" if quote["estimated"] else ""

    holdings_html = ""
    action_html = ""
    note_html = ""
    if portfolio_report:
        fund_return = portfolio_report["fund_return"]
        if portfolio_report["fund"]:
            fund_metrics = (
                f"<strong>{portfolio_report['fund_value']:,.2f} \u5143</strong><br>"
                f"<span style=\"color:{value_color(portfolio_report['fund_profit'])}\">"
                f"{portfolio_report['fund_profit']:+,.2f} \u5143 "
                f"({fund_return:+.2%})</span>"
            )
            fund_detail = (
                f"{portfolio_report['fund_shares']:,.2f} \u4efd<br>"
                f"\u51c0\u503c {portfolio_report['fund']['nav']:.4f} "
                f"({escape(portfolio_report['fund']['date'])})"
            )
        else:
            fund_metrics = "\u51c0\u503c\u6682\u4e0d\u53ef\u7528"
            fund_detail = f"{portfolio_report['fund_shares']:,.2f} \u4efd"

        if portfolio_report["combined_value"] is not None:
            combined_metrics = (
                f"<strong>{portfolio_report['combined_value']:,.2f} \u5143</strong><br>"
                f"<span style=\"color:{value_color(portfolio_report['combined_profit'])}\">"
                f"{portfolio_report['combined_profit']:+,.2f} \u5143 "
                f"({portfolio_report['combined_return']:+.2%})</span>"
            )
        else:
            combined_metrics = "\u57fa\u91d1\u51c0\u503c\u7f3a\u5931\uff0c\u6682\u4e0d\u8ba1\u7b97"

        holdings_html = f"""
          <h2 style="margin:28px 0 10px;font-size:18px;color:#1f2933;">\u6301\u4ed3\u6982\u89c8</h2>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;border:1px solid #d9dee5;font-size:14px;">
            <tr style="background:#f3f5f7;color:#52606d;">
              <th align="left" style="padding:10px;border-bottom:1px solid #d9dee5;">\u54c1\u79cd</th>
              <th align="left" style="padding:10px;border-bottom:1px solid #d9dee5;">\u6570\u91cf / \u4ef7\u683c</th>
              <th align="right" style="padding:10px;border-bottom:1px solid #d9dee5;">\u5e02\u503c / \u76c8\u4e8f</th>
            </tr>
            <tr>
              <td style="padding:12px 10px;border-bottom:1px solid #e7eaee;"><strong>{escape(str(portfolio_report['fund_name']))}</strong><br><span style="color:#7b8794;">{escape(str(portfolio_report['fund_code']))}</span></td>
              <td style="padding:12px 10px;border-bottom:1px solid #e7eaee;line-height:1.6;">{fund_detail}</td>
              <td align="right" style="padding:12px 10px;border-bottom:1px solid #e7eaee;line-height:1.6;">{fund_metrics}</td>
            </tr>
            <tr>
              <td style="padding:12px 10px;"><strong>\u5de5\u884c\u79ef\u5b58\u91d1</strong><br><span style="color:#7b8794;">\u53c2\u8003\u4f30\u503c</span></td>
              <td style="padding:12px 10px;line-height:1.6;">{portfolio_report['gold_grams']:.4f} \u514b<br>{portfolio_report['gold_mark']:,.2f} \u5143/\u514b</td>
              <td align="right" style="padding:12px 10px;line-height:1.6;"><strong>{portfolio_report['gold_value']:,.2f} \u5143</strong><br><span style="color:{value_color(portfolio_report['gold_profit'])}">{portfolio_report['gold_profit']:+,.2f} \u5143 ({portfolio_report['gold_return']:+.2%})</span></td>
            </tr>
            <tr style="background:#fff9e8;">
              <td colspan="2" style="padding:12px 10px;border-top:2px solid #d6b35a;"><strong>\u5408\u8ba1</strong></td>
              <td align="right" style="padding:12px 10px;border-top:2px solid #d6b35a;line-height:1.6;">{combined_metrics}</td>
            </tr>
          </table>
        """
        action_html = f"""
          <h2 style="margin:28px 0 10px;font-size:18px;color:#1f2933;">\u98ce\u9669\u5224\u65ad</h2>
          <div style="padding:14px 16px;background:#edf7f3;border-left:4px solid #087f5b;line-height:1.7;color:#25313c;">
            <strong>\u5f53\u524d\u9ec4\u91d1\u4ed3\u4f4d\uff1a{portfolio_report['allocation']:.0f}%</strong><br>
            {escape(portfolio_report['action'])}
          </div>
        """
        note_html = "<p style=\"margin:12px 0 0;color:#7b8794;font-size:12px;line-height:1.6;\">\u79ef\u5b58\u91d1\u672a\u6263\u5de5\u884c\u5b9e\u65f6\u4e70\u5356\u4ef7\u5dee\u6216\u8d39\u7528\uff0c\u5b9e\u9645\u4ea4\u6613\u4ee5\u5de5\u884c APP \u62a5\u4ef7\u4e3a\u51c6\u3002</p>"

    reason_items = "".join(f"<li style=\"margin:5px 0;\">{escape(reason)}</li>" for reason in reasons)
    news_html = ""
    if news:
        news_html = f"<p style=\"margin:8px 0 0;\"><a href=\"{escape(news['link'], quote=True)}\" style=\"color:#1261a0;\">{escape(news['title'])}</a><br><span style=\"color:#7b8794;\">{escape(news['published'])}</span></p>"
    source_urls = list(quote["source_urls"])
    if portfolio_report:
        source_urls.append(FUND_PAGE_URL.format(code=portfolio_report["fund_code"]))
    source_links = "".join(
        f"<li style=\"margin:5px 0;word-break:break-all;\"><a href=\"{escape(url, quote=True)}\" style=\"color:#1261a0;\">{escape(url)}</a></li>"
        for url in source_urls
    )

    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#eef1f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',Arial,sans-serif;color:#25313c;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#eef1f4;"><tr><td align="center" style="padding:20px 10px;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:680px;background:#ffffff;border-top:5px solid #c99a2e;">
      <tr><td style="padding:24px 24px 18px;background:#202a33;color:#ffffff;">
        <div style="font-size:13px;color:#d6dde4;">{escape(quote['instrument'])}{estimate_note}</div>
        <div style="margin-top:6px;font-size:34px;font-weight:700;line-height:1.2;">{quote['price']:,.2f} <span style="font-size:17px;font-weight:400;">\u5143/\u514b</span></div>
        <div style="margin-top:10px;color:{change_color};font-size:16px;font-weight:600;">{change_value}&nbsp;&nbsp;{change_percent}</div>
      </td></tr>
      <tr><td style="padding:22px 24px 28px;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="font-size:13px;color:#52606d;line-height:1.7;">
          <tr><td style="padding:3px 0;">\u68c0\u67e5\u65f6\u95f4</td><td align="right" style="padding:3px 0;color:#25313c;">{escape(timestamp)}</td></tr>
          <tr><td style="padding:3px 0;">\u6570\u636e\u65f6\u95f4</td><td align="right" style="padding:3px 0;color:#25313c;">{escape(str(quote['market_time']))}</td></tr>
          <tr><td style="padding:3px 0;">\u6570\u636e\u6765\u6e90</td><td align="right" style="padding:3px 0;color:#25313c;">{escape(quote['source'])}</td></tr>
        </table>
        {holdings_html}
        {action_html}
        <h2 style="margin:28px 0 10px;font-size:18px;color:#1f2933;">\u672c\u6b21\u90ae\u4ef6\u539f\u56e0</h2>
        <ul style="margin:0;padding:12px 16px 12px 34px;background:#f6f7f9;line-height:1.6;">{reason_items}</ul>
        {news_html}
        <h2 style="margin:28px 0 10px;font-size:18px;color:#1f2933;">\u6570\u636e\u94fe\u63a5</h2>
        <ul style="margin:0;padding-left:20px;font-size:12px;line-height:1.5;">{source_links}</ul>
        {note_html}
      </td></tr>
    </table>
  </td></tr></table>
</body></html>"""


def send_email(subject, body, html_body=None):
    email_address = os.environ["QQ_EMAIL"]
    authorization_code = os.environ["QQ_SMTP_AUTH_CODE"]

    message = EmailMessage()
    message["From"] = email_address
    message["To"] = email_address
    message["Subject"] = str(Header(subject, "utf-8"))
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=context, timeout=30) as smtp:
        smtp.login(email_address, authorization_code)
        smtp.send_message(message)


def write_outputs(values):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    quote = fetch_quote()
    current_price = quote["price"]
    previous_price_text = os.environ.get("LAST_PRICE", "").strip()
    previous_source = os.environ.get("LAST_SOURCE_KIND", "").strip()
    previous_news_id = os.environ.get("LAST_NEWS_ID", "").strip()
    previous_portfolio_risk = os.environ.get("LAST_PORTFOLIO_RISK", "").strip()
    previous_price = float(previous_price_text) if previous_price_text else None
    source_changed = bool(previous_source and previous_source != quote["source_kind"])

    change = current_price - previous_price if previous_price is not None else None
    change_ratio = change / previous_price if previous_price else None
    is_price_alert = (
        change_ratio is not None
        and not source_changed
        and abs(change_ratio) >= ALERT_THRESHOLD
    )
    force_test = os.environ.get("FORCE_TEST_EMAIL", "false").lower() == "true"

    portfolio_report = None
    try:
        portfolio = load_portfolio()
        if portfolio:
            portfolio_report = build_portfolio_report(portfolio, quote)
    except Exception as error:
        print(f"Portfolio check failed without stopping price monitoring: {error}", file=sys.stderr)

    news = None
    try:
        news = fetch_important_news(now)
    except Exception as error:
        print(f"News check failed without stopping price monitoring: {error}", file=sys.stderr)
    is_news_alert = bool(news and news["id"] != previous_news_id)
    is_portfolio_alert = bool(
        portfolio_report
        and portfolio_report["risk_code"] != "normal"
        and portfolio_report["risk_code"] != previous_portfolio_risk
    )

    timestamp = now.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
    estimate_note = " (\u4f30\u7b97)" if quote["estimated"] else ""
    if change is None:
        change_text = "\u53d8\u52a8\uff1a\u9996\u6b21\u4e91\u7aef\u68c0\u67e5\uff0c\u6682\u65e0\u4e0a\u6b21\u4ef7\u683c\u3002"
    elif source_changed:
        change_text = (
            f"\u53d8\u52a8\uff1a{change:+.2f} \u5143/\u514b ({change_ratio:+.2%})\uff1b"
            "\u6570\u636e\u6e90\u53d1\u751f\u53d8\u5316\uff0c\u672c\u6b21\u4e0d\u89e6\u53d1\u4ef7\u683c\u9884\u8b66\u3002"
        )
    else:
        change_text = f"\u53d8\u52a8\uff1a{change:+.2f} \u5143/\u514b ({change_ratio:+.2%})"

    source_lines = "\n".join(f"\u6765\u6e90\uff1a{url}" for url in quote["source_urls"])
    quote_detail = ""
    if quote["estimated"]:
        quote_detail = (
            f"\nXAU/USD: {quote['xau_usd']:.2f} USD/oz"
            f"\nUSD/CNY: {quote['usd_cny']:.4f}"
        )
    summary = (
        f"{quote['instrument']}{estimate_note}\uff1a{current_price:.2f} \u5143/\u514b\n"
        f"{change_text}\n"
        f"\u68c0\u67e5\u65f6\u95f4\uff1a{timestamp}\n"
        f"\u6570\u636e\u65f6\u95f4\uff1a{quote['market_time']}\n"
        f"\u6570\u636e\u6765\u6e90\uff1a{quote['source']}"
        f"{quote_detail}\n"
        f"{source_lines}"
    )
    if portfolio_report:
        summary = f"{summary}\n\n{portfolio_report['text']}"
    print(summary)

    reasons = []
    if is_price_alert:
        reasons.append(
            f"\u4ef7\u683c\u53d8\u52a8 {change_ratio:+.2%}\uff0c\u5df2\u8fbe\u5230 0.5% \u9608\u503c"
        )
    if is_news_alert:
        reasons.append(f"\u91cd\u8981\u9ec4\u91d1\u5e02\u573a\u65b0\u95fb\uff1a{news['title']}")
    if is_portfolio_alert:
        reasons.append(f"\u6301\u4ed3\u98ce\u9669\u63d0\u9192\uff1a{portfolio_report['action']}")
    if force_test and not reasons:
        reasons.append("\u624b\u52a8\u4e91\u7aef\u90ae\u4ef6\u6d4b\u8bd5")

    if reasons:
        news_detail = ""
        if news and is_news_alert:
            news_detail = (
                f"\n\u65b0\u95fb\u65f6\u95f4\uff1a{news['published']}"
                f"\n\u65b0\u95fb\u94fe\u63a5\uff1a{news['link']}"
            )
        body = f"{summary}\n\u89e6\u53d1\u539f\u56e0\uff1a{'; '.join(reasons)}{news_detail}"
        if force_test and not (is_price_alert or is_news_alert or is_portfolio_alert):
            subject = (
                f"[\u9ec4\u91d1\u76d1\u63a7\u6d4b\u8bd5] \u4e91\u7aef\u76d1\u63a7\u5df2\u542f\u7528\uff1a"
                f"{current_price:.2f} \u5143/\u514b"
            )
        else:
            subject = f"[\u91d1\u4ef7\u9884\u8b66] {current_price:.2f} \u5143/\u514b"
        html_body = build_email_html(
            quote,
            change,
            change_ratio,
            timestamp,
            portfolio_report,
            reasons,
            news if is_news_alert else None,
        )
        send_email(subject, body, html_body)
        print("QQ Mail notification sent.")

    write_outputs(
        {
            "price": f"{current_price:.2f}",
            "source_kind": quote["source_kind"],
            "news_id": news["id"] if news else previous_news_id,
            "portfolio_risk": (
                portfolio_report["risk_code"]
                if portfolio_report
                else previous_portfolio_risk
            ),
            "alert": str(is_price_alert or is_news_alert or is_portfolio_alert).lower(),
            "checked_at": now.isoformat(),
        }
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Monitor failed: {error}", file=sys.stderr)
        raise
