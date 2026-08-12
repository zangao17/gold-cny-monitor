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
from html import unescape
from zoneinfo import ZoneInfo


SGE_URL = "https://www.sge.com.cn/sjzx/yshqbg?ad_check=1"
GOLD_API_USD_URL = "https://api.gold-api.com/price/XAU"
GOLD_API_CNY_URL = "https://api.gold-api.com/price/XAU/CNY"
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
    "缇庤仈鍌?,
    "澶璐噾",
    "鍒涙柊楂?,
    "鏆存定",
    "鏆磋穼",
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


def send_email(subject, body):
    email_address = os.environ["QQ_EMAIL"]
    authorization_code = os.environ["QQ_SMTP_AUTH_CODE"]

    message = EmailMessage()
    message["From"] = email_address
    message["To"] = email_address
    message["Subject"] = str(Header(subject, "utf-8"))
    message.set_content(body)

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

    news = None
    try:
        news = fetch_important_news(now)
    except Exception as error:
        print(f"News check failed without stopping price monitoring: {error}", file=sys.stderr)
    is_news_alert = bool(news and news["id"] != previous_news_id)

    timestamp = now.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
    estimate_note = " (estimate)" if quote["estimated"] else ""
    if change is None:
        change_text = "Change: first cloud reading; no previous price is available."
    elif source_changed:
        change_text = (
            f"Change: {change:+.2f} CNY/g ({change_ratio:+.2%}); "
            "source changed, so this reading does not trigger a price alert."
        )
    else:
        change_text = f"Change: {change:+.2f} CNY/g ({change_ratio:+.2%})"

    source_lines = "\n".join(f"Source: {url}" for url in quote["source_urls"])
    quote_detail = ""
    if quote["estimated"]:
        quote_detail = (
            f"\nXAU/USD: {quote['xau_usd']:.2f} USD/oz"
            f"\nUSD/CNY: {quote['usd_cny']:.4f}"
        )
    summary = (
        f"{quote['instrument']}{estimate_note}: {current_price:.2f} CNY/g\n"
        f"{change_text}\n"
        f"Checked: {timestamp}\n"
        f"Data time: {quote['market_time']}\n"
        f"Data source: {quote['source']}"
        f"{quote_detail}\n"
        f"{source_lines}"
    )
    print(summary)

    reasons = []
    if is_price_alert:
        reasons.append(f"price moved {change_ratio:+.2%}, reaching the 0.5% threshold")
    if is_news_alert:
        reasons.append(f"important gold-market news: {news['title']}")
    if force_test and not reasons:
        reasons.append("manual cloud email test")

    if reasons:
        news_detail = ""
        if news and is_news_alert:
            news_detail = (
                f"\nNews time: {news['published']}"
                f"\nNews link: {news['link']}"
            )
        body = f"{summary}\nTrigger: {'; '.join(reasons)}{news_detail}"
        if force_test and not (is_price_alert or is_news_alert):
            subject = f"[Gold monitor test] Cloud monitoring active: {current_price:.2f} CNY/g"
        else:
            subject = f"[Gold price alert] {current_price:.2f} CNY/g"
        send_email(subject, body)
        print("QQ Mail notification sent.")

    write_outputs(
        {
            "price": f"{current_price:.2f}",
            "source_kind": quote["source_kind"],
            "news_id": news["id"] if news else previous_news_id,
            "alert": str(is_price_alert or is_news_alert).lower(),
            "checked_at": now.isoformat(),
        }
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Monitor failed: {error}", file=sys.stderr)
        raise
