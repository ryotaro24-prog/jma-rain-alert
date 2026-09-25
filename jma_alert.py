import os
import re
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TEST_NOTIFICATION = os.environ.get("TEST_NOTIFICATION", "").lower() in ("1", "true", "yes")
FEEDS = [
    "https://www.data.jma.go.jp/developer/xml/feed/extra.xml",
    "https://www.data.jma.go.jp/developer/xml/feed/regular.xml",
]

STATE_FILE = "seen_ids.json"
JST = timezone(timedelta(hours=9))
MAX_AGE_MINUTES = 30


def fetch(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "jma-rain-alert/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read()


def local_name(tag):
    return tag.split("}")[-1]


def all_text(root):
    return " ".join(
        text.strip()
        for text in root.itertext()
        if text and text.strip()
    )


def first_text(root, name):
    for elem in root.iter():
        if local_name(elem.tag) == name:
            if elem.text and elem.text.strip():
                return elem.text.strip()
    return ""


def get_product_code(url):
    # Examples may contain strings such as VPBS50.
    match = re.search(r"(VPBS50|VPZJ51|VPCJ51|VPFJ51)", url)
    return match.group(1) if match else ""


def classify(code, title, text):
    combined = f"{title} {text}"

    if "線状降水帯" not in combined:
        return None

    # Weather Disaster Bulletin
    if code == "VPBS50":
        if (
            "線状降水帯直前" in combined
            or "線状降水帯直前予測" in combined
        ):
            return "線状降水帯・直前予測"

        if (
            "線状降水帯発生" in combined
            or "線状降水帯が発生" in combined
        ):
            return "線状降水帯・発生情報"

    # Weather Commentary Information
    if code in ("VPZJ51", "VPCJ51", "VPFJ51"):
        if (
            "半日前" in combined
            or "発生する可能性" in combined
            or "発生するおそれ" in combined
        ):
            return "線状降水帯・半日前予測"

    return None


def load_seen():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(data)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return set()


def save_seen(seen):
    # Keep the state file reasonably small.
    items = list(seen)[-500:]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            items,
            f,
            ensure_ascii=False,
            indent=2,
        )


def parse_atom(data):
    root = ET.fromstring(data)
    entries = []

    for entry in root.iter():
        if local_name(entry.tag) != "entry":
            continue

        entry_id = ""
        updated = ""
        link = ""

        for child in entry:
            name = local_name(child.tag)

            if name == "id":
                entry_id = (child.text or "").strip()

            elif name == "updated":
                updated = (child.text or "").strip()

            elif name == "link":
                href = child.attrib.get("href", "")
                if href.endswith(".xml"):
                    link = href

        if link:
            entries.append(
                {
                    "id": entry_id or link,
                    "updated": updated,
                    "url": link,
                }
            )

    return entries


def is_recent(updated):
    if not updated:
        return True

    try:
        dt = datetime.fromisoformat(
            updated.replace("Z", "+00:00")
        )
        age = (
            datetime.now(timezone.utc) - dt
        ).total_seconds() / 60

        return -5 <= age <= MAX_AGE_MINUTES

    except Exception:
        return True


def send_ntfy(category, title, issue_time, message, source_url):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC is not configured")

    body = (
        f"{category}\n\n"
        f"{title}\n"
        f"発表時刻: {issue_time}\n\n"
        f"{message}\n\n"
        f"気象庁XML: {source_url}"
    )

    priority = 5 if (
        "直前" in category or "発生" in category
    ) else 4

    payload = json.dumps(
        {
            "topic": NTFY_TOPIC,
            "title": "JMA 線状降水帯情報",
            "message": body,
            "priority": priority,
            "tags": ["warning", "rain_cloud"],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://ntfy.sh",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        response.read()


def main():
    if TEST_NOTIFICATION:
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
        send_ntfy(
            "動作テスト",
            "JMA線状降水帯アラート・テスト",
            now,
            "GitHub Actionsからのテスト通知です。",
            "https://www.jma.go.jp/",
        )
        print("Test notification sent.")
        return
        
    seen = load_seen()
    current_ids = set()
    alerts_sent = 0

    for feed_url in FEEDS:
        print(f"Checking: {feed_url}")

        try:
                       entries = parse_atom(fetch(feed_url))
        except Exception as e:
            print(f"Feed error: {e}")
            continue

        for entry in entries:
            entry_id = entry["id"]
            current_ids.add(entry_id)
            url = entry["url"]
            code = get_product_code(url)
            print(f"DEBUG URL: {url} / CODE: {code}")

            if entry_id in seen:
                continue

            # Mark as seen even if it is not an alert.
            # This prevents repeatedly downloading the same entry.
            seen.add(entry_id)

            if not is_recent(entry["updated"]):
                continue

            url = entry["url"]
            code = get_product_code(url)

            if code not in (
                "VPBS50",
                "VPZJ51",
                "VPCJ51",
                "VPFJ51",
            ):
                continue

            try:
                root = ET.fromstring(fetch(url))
            except Exception as e:
                print(f"XML error: {e}")
                continue

            title = first_text(root, "Title")
            report_time = first_text(root, "ReportDateTime")
            text = all_text(root)

            category = classify(code, title, text)

            if not category:
                continue

            headline = first_text(root, "Text") or title

            try:
                dt = datetime.fromisoformat(
                    report_time.replace("Z", "+00:00")
                )
                issue_time = dt.astimezone(JST).strftime(
                    "%Y-%m-%d %H:%M JST"
                )
            except Exception:
                issue_time = report_time or "不明"

            print(f"ALERT: {category} / {title}")

            send_ntfy(
                category,
                title,
                issue_time,
                headline,
                url,
            )

            alerts_sent += 1

    save_seen(seen)

    print(f"Finished. Alerts sent: {alerts_sent}")


if __name__ == "__main__":
    main()
