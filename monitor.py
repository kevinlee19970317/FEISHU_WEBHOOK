import os
import smtplib
import sqlite3
from datetime import date, timedelta
from email.mime.text import MIMEText

import requests
import yaml
import json
import time

DB_PATH = "prices.db"
RATE_LIMIT_ABORTED = False


def env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_db(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          route TEXT NOT NULL,
          depart_date TEXT NOT NULL,
          price REAL NOT NULL,
          is_direct INTEGER NOT NULL,
          fetched_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          route TEXT NOT NULL,
          depart_date TEXT NOT NULL,
          price REAL NOT NULL,
          alerted_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def send_feishu(webhook: str, text: str):
    payload = {"msg_type": "text", "content": {"text": text}}
    r = requests.post(webhook, json=payload, timeout=15)
    r.raise_for_status()


def send_email(subject: str, body: str):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    sender = os.getenv("SMTP_FROM", user or "")
    receiver = os.getenv("ALERT_EMAIL_TO")

    required = [host, user, password, sender, receiver]
    if not all(required):
        raise RuntimeError("Missing SMTP_* or ALERT_EMAIL_TO environment variables")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(sender, [receiver], msg.as_string())


def notify_dual(feishu_webhook: str, text: str, email_enabled: bool):
    send_feishu(feishu_webhook, text)
    if email_enabled:
        send_email("机票降价提醒", text)


def save_price(conn, route, depart_date, price, is_direct):
    conn.execute(
        """
        INSERT INTO prices(route, depart_date, price, is_direct, fetched_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        """,
        (route, depart_date, price, 1 if is_direct else 0),
    )
    conn.commit()


def get_avg_30d(conn, route, depart_date):
    cur = conn.execute(
        """
        SELECT AVG(price)
        FROM prices
        WHERE route = ?
          AND depart_date = ?
          AND fetched_at >= datetime('now', '-30 days')
        """,
        (route, depart_date),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def already_alerted_recently(conn, route, depart_date, price, hours=24):
    cur = conn.execute(
        """
        SELECT 1
        FROM alerts
        WHERE route = ?
          AND depart_date = ?
          AND price = ?
          AND alerted_at >= datetime('now', ?)
        LIMIT 1
        """,
        (route, depart_date, price, f"-{hours} hours"),
    )
    return cur.fetchone() is not None


def save_alert(conn, route, depart_date, price):
    conn.execute(
        """
        INSERT INTO alerts(route, depart_date, price, alerted_at)
        VALUES (?, ?, ?, datetime('now'))
        """,
        (route, depart_date, price),
    )
    conn.commit()


def should_alert(current_price, avg_30d, threshold_ratio):
    return avg_30d is not None and avg_30d > 0 and current_price <= avg_30d * threshold_ratio


def should_force_alert() -> bool:
    return (os.getenv("FORCE_ALERT", "false").strip().lower() == "true")


def fetch_prices_via_rapidapi(origin, dest, start_date, end_date, direct_only=True):
    global RATE_LIMIT_ABORTED
    if RATE_LIMIT_ABORTED:
        return []
    endpoint = env_or_default("RAPIDAPI_FLIGHTS_URL", "https://booking-com15.p.rapidapi.com/api/v1/flights/getMinPrice")
    host = (os.getenv("RAPIDAPI_HOST") or "").strip()
    key = (os.getenv("RAPIDAPI_KEY") or "").strip()

    if not endpoint or not host or not key:
        if os.getenv("DEBUG_MONITOR", "false").lower() == "true":
            print("debug: rapidapi skipped because endpoint/host/key missing")
        return []

    currency = env_or_default("RAPIDAPI_CURRENCY", "CNY")
    from_id = os.getenv("RAPIDAPI_FROM_ID_TEMPLATE", "{code}.AIRPORT").format(code=origin)
    to_id = os.getenv("RAPIDAPI_TO_ID_TEMPLATE", "{code}.AIRPORT").format(code=dest)

    params = {
        "fromId": from_id,
        "toId": to_id,
        "departDate": start_date,
        "returnDate": end_date,
        "cabinClass": os.getenv("RAPIDAPI_CABIN_CLASS", "ECONOMY"),
        "currency_code": currency,
    }

    extra = os.getenv("RAPIDAPI_EXTRA_PARAMS", "")
    if extra:
        try:
            params.update(json.loads(extra))
        except json.JSONDecodeError:
            pass

    headers = {
        "x-rapidapi-host": host,
        "x-rapidapi-key": key,
        "Content-Type": "application/json",
    }

    if os.getenv("DEBUG_MONITOR", "false").lower() == "true":
        print(f"debug: request endpoint={endpoint}")
        print(
            "debug: "
            f"fromId={params.get('fromId')} "
            f"toId={params.get('toId')} "
            f"departDate={params.get('departDate')}"
        )

    max_retries = int(env_or_default("RAPIDAPI_RETRIES", "2"))
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(endpoint, headers=headers, params=params, timeout=30)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait_seconds = 0
                if retry_after and retry_after.isdigit():
                    wait_seconds = int(retry_after)
                else:
                    wait_seconds = min(2 ** attempt, 8)
                if os.getenv("DEBUG_MONITOR", "false").lower() == "true":
                    print(f"debug: rapidapi rate limited (429), retry_after={retry_after}, wait={wait_seconds}s, attempt={attempt+1}")
                if attempt < max_retries:
                    time.sleep(wait_seconds)
                    continue
                RATE_LIMIT_ABORTED = True
                print("warn: RapidAPI 429 持续触发，已中止本次剩余路线请求（避免继续撞限流）")
                return []
            r.raise_for_status()
            payload = r.json()
            break
        except requests.RequestException as exc:
            if os.getenv("DEBUG_MONITOR", "false").lower() == "true":
                print(f"debug: request failed: {exc}")
            return []

    if os.getenv("DEBUG_MONITOR", "false").lower() == "true":
        if isinstance(payload, dict):
            print(f"debug: response top_keys={list(payload.keys())}")
        else:
            print(f"debug: response type={type(payload)}")

    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if isinstance(data, str):
        # Some endpoints return data as message string on soft failures
        if os.getenv("DEBUG_MONITOR", "false").lower() == "true":
            print(f"debug: data message={data}")
        return []
    if not isinstance(data, dict):
        data = {}

    if os.getenv("DEBUG_MONITOR", "false").lower() == "true":
        preview = str(payload)
        print(f"debug: payload_preview={preview[:400]}")

    # getMinPrice style response
    min_price = data.get("minPrice") or data.get("price") or payload.get("minPrice")
    if min_price is not None:
        try:
            min_price_value = float(min_price)
        except (TypeError, ValueError):
            min_price_value = None
        if min_price_value is not None:
            return [{
                "depart_date": start_date,
                "price": min_price_value,
                "is_direct": True,
                "url": endpoint,
            }]

    # searchFlights style response
    flights = data.get("flights", [])
    results = []
    for f in flights:
        price_obj = f.get("price") or {}
        total = price_obj.get("units") or price_obj.get("total") or f.get("priceValue")
        depart_date = f.get("departureDate") or start_date
        is_non_stop = f.get("isDirect")
        if is_non_stop is None:
            segments = f.get("segments") or []
            is_non_stop = len(segments) <= 1

        if total is None:
            continue

        results.append({
            "depart_date": str(depart_date)[:10],
            "price": float(total),
            "is_direct": bool(is_non_stop),
            "url": f.get("deepLink") or endpoint,
        })

    return results

def build_mock_prices(start_day, route):
    depart_date = (start_day + timedelta(days=7)).isoformat()
    mock_price = float(os.getenv("MOCK_PRICE", "1200"))
    mock_is_direct = os.getenv("MOCK_IS_DIRECT", "true").lower() == "true"
    return [{
        "depart_date": depart_date,
        "price": mock_price,
        "is_direct": mock_is_direct,
        "url": f"https://example.com/flights/{route}/{depart_date}",
    }]


def fetch_prices_for_route(origin, dest, start_date, end_date, direct_only=True):
    """
    默认不抓取真实数据（避免非法爬取），返回空。

    若要联调提醒链路，可设置环境变量 MOCK_PRICE_MODE=true
    来生成一条模拟票价数据。
    """
    route = f"{origin}-{dest}"
    if os.getenv("MOCK_PRICE_MODE", "false").lower() == "true":
        start_day = date.fromisoformat(start_date)
        return build_mock_prices(start_day, route)

    rapidapi_results = fetch_prices_via_rapidapi(origin, dest, start_date, end_date, direct_only)
    if rapidapi_results:
        return rapidapi_results

    return []


def main():
    webhook = os.getenv("FEISHU_WEBHOOK")
    if not webhook:
        raise RuntimeError("Missing FEISHU_WEBHOOK secret")

    cfg = load_config()
    threshold_ratio = float(cfg.get("threshold_ratio", 0.7))
    lookahead_days = int(cfg.get("lookahead_days", 90))
    direct_only = bool(cfg.get("direct_only", True))
    email_enabled = bool(cfg.get("email_enabled", True))

    start = date.today()
    end = start + timedelta(days=lookahead_days)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    fetched_count = 0
    triggered_count = 0

    if os.getenv("MOCK_PRICE_MODE", "false").lower() == "true":
        mock_baseline = float(os.getenv("MOCK_BASELINE_PRICE", "2000"))
        baseline_depart = (start + timedelta(days=7)).isoformat()
        for origin in cfg["origins"]:
            for dest in cfg["destinations"]:
                route = f"{origin}-{dest}"
                conn.execute(
                    """
                    INSERT INTO prices(route, depart_date, price, is_direct, fetched_at)
                    VALUES (?, ?, ?, 1, datetime('now', '-1 day'))
                    """,
                    (route, baseline_depart, mock_baseline),
                )
        conn.commit()

    for origin in cfg["origins"]:
        for dest in cfg["destinations"]:
            route = f"{origin}-{dest}"
            prices = fetch_prices_for_route(origin, dest, start.isoformat(), end.isoformat(), direct_only)
            sleep_seconds = float(env_or_default("REQUEST_INTERVAL_SECONDS", "0"))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

            for item in prices:
                depart_date = item["depart_date"]
                price = float(item["price"])
                is_direct = bool(item.get("is_direct", False))
                url = item.get("url", "")

                if direct_only and not is_direct:
                    continue

                save_price(conn, route, depart_date, price, is_direct)
                fetched_count += 1
                avg_30d = get_avg_30d(conn, route, depart_date)

                if should_alert(price, avg_30d, threshold_ratio) or should_force_alert():
                    if already_alerted_recently(conn, route, depart_date, price, hours=24):
                        continue

                    drop = (1 - price / avg_30d) * 100
                    text = (
                        f"✈️ 机票降价提醒（直飞）\n"
                        f"航线: {route}\n"
                        f"出发: {depart_date}\n"
                        f"当前价: ¥{price:.0f}\n"
                        f"近30天均价: ¥{avg_30d:.0f}\n"
                        f"降幅: {drop:.1f}%\n"
                        f"链接: {url}"
                    )
                    notify_dual(webhook, text, email_enabled)
                    save_alert(conn, route, depart_date, price)
                    triggered_count += 1

    conn.close()
    print(f"fetched_records={fetched_count}")
    print(f"alerts_sent={triggered_count}")
    if fetched_count == 0:
        print("hint: fetched_records=0 通常表示 API 没返回可解析票价，请检查 RAPIDAPI_* 参数与 fromId/toId 格式")
    if RATE_LIMIT_ABORTED:
        print("hint: 本次请求被 RapidAPI 限流中止，可降低频率/路线数，或增大 REQUEST_INTERVAL_SECONDS")


if __name__ == "__main__":
    main()
