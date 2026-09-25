import os
import re
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ==========================================
# Settings
# ==========================================

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

# JMA PULL-type Atom feeds
FEEDS = [
    "https://www.data.jma.go.jp/developer/xml/feed/extra.xml",
    "https://www.data.jma.go.jp/developer/xml/feed/regular.xml",
]

JST = timezone(timedelta(hours=9))

# Only relatively new JMA messages are considered.
# This also prevents old feed entries from being notified
# when the workflow is first installed.
MAX_AGE_MINUTES = 20


def fetch(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "jma-rain-alert/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read()


def text_of(root):
    """Return all text contained in an XML document."""
    return " ".join(
        t.strip()
        for t in root.itertext()
        if t and t.strip()
    )


def find_text(root, local_name):
    """Find the first XML element by local name, ignoring namespaces."""
    for elem in root.iter():
        if elem.tag.split("}")[-1] == local_name:
            if elem.text and elem.text.strip():
                return elem.text.strip()
    return ""


def product_code(url):
    """
    JMA XML URLs normally contain a filename such as:
    ..._VPBS50_...
    """
    match = re.search(r"_([A-Z]{4}\d{2})_", url)
    return match.group(1) if match else ""


def classify(code, title, full_text):
    """
    Return the alert category we care about.
    """

    combined = f"{title} {full_text}"

    if "線状降水帯" not in combined:
        return None

    # 2026 JMA Weather Disaster Bulletin
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

    # 2026 Weather Commentary Information
    if code in ("VPZJ51", "VPCJ51", "VPFJ51"):
        if (
            "線状降水帯" in combined
            and (
                "半日前" in combined
                or "発生する可能性" in combined
                or "発生するおそれ" in combined
            )
        ):
            return "線状降水帯・半日前予測"

    return None


def send_ntfy(category, title, issue_time, headline, source_url):

    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC is not configured.")

    body = (
        f"{category}\n\n"
        f"{title}\n"
        f"発表時刻: {issue_time}\n\n"
        f"{headline}\n\n"
        f"気象庁XML: {source_url}"
    )

    priority = "urgent" if (
        "直前" in category or "発生" in category
    ) else "high"

    url = "https://ntfy.sh/" + urllib.parse.quote(
        NTFY_TOPIC, safe=""
    )

    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Title": urllib.parse.quote(
                "JMA 線状降水帯情報"
            ),
            "Priority": priority,
            "Tags": "warning,rain_cloud",
        },
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        response.read()


def parse_atom(feed_data):

    root = ET.fromstring(feed_data)

    entries = []

    for entry in root.iter():
        if entry.tag.split("}")[-1] != "entry":
            continue

        entry_id = ""
        updated = ""
        link = ""

        for child in entry:
            name = child.tag.split("}")[-1]

            if name == "id":
                entry_id = child.text or ""

            elif name == "updated":
                updated = child.text or ""

            elif name == "link":
                href = child.attrib.get("href")
                if href and href.endswith(".xml"):
                    link = href

        if link:
            entries.append(
                {
                    "id": entry_id,
                    "updated": updated,
                    "url": link,
                }
            )

    return entries


def recent_enough(updated):

    if not updated:
        return True

    try:
        dt = datetime.fromisoformat(
            updated.replace("Z", "+00:00")
        )

        now = datetime.now(timezone.utc)

        age = (now - dt).total_seconds() / 60

        return -5 <= age <= MAX_AGE_MINUTES

    except Exception:
        return True


def main():

    found = 0

    for feed_url in FEEDS:

        print(f"Checking feed: {feed_url}")

        try:
            feed_data = fetch(feed_url)
            entries = parse_atom(feed_data)

        except Exception as e:
            print(f"Feed error: {e}")
            continue

        for entry in entries:

            if not recent_enough(entry["updated"]):
                continue

            url = entry["url"]
            code = product_code(url)

            # Limit downloads to likely relevant products
            if code not in (
                "VPBS50",
                "VPZJ51",
                "VPCJ51",
                "VPFJ51",
            ):
                continue

            try:
                xml_data = fetch(url)
                root = ET.fromstring(xml_data)

            except Exception as e:
                print(f"XML error: {url}: {e}")
                continue

            full_text = text_of(root)

            title = find_text(root, "Title")
            headline = find_text(root, "Text")
            report_time = find_text(root, "ReportDateTime")

            category = classify(
                code,
                title,
                full_text
            )

            if not category:
                continue

            if not headline:
                headline = title

            try:
                dt = datetime.fromisoformat(
                    report_time.replace("Z", "+00:00")
                )
                issue_time = dt.astimezone(
                    JST
                ).strftime("%Y-%m-%d %H:%M JST")

            except Exception:
                issue_time = report_time or "不明"

            print(
                f"ALERT: {category} / {title}"
            )

            send_ntfy(
                category,
                title,
                issue_time,
                headline,
                url,
            )

            found += 1

    print(f"Finished. Alerts sent: {found}")


if __name__ == "__main__":
    main()
