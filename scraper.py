import xml.etree.ElementTree as ET
import gzip
import io
import os
import time
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime

# Define XML Namespace
NAMESPACES = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

MASTER_FILE = "immoscout_listings.txt"  # sab purane + naye links yahan save hote hain


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OAI-SearchBot/1.0; +http://openai.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Safety cap — agar server kabhi hamesha 200 dene lage (ghalati se) to bhi
# infinite loop na bane. Itni bari range kabhi practically nahi aani chahiye.
MAX_CLUSTERS_PER_CATEGORY = 500


def fetch_sitemap(url: str):
    """
    Ek gzipped sub-sitemap fetch/decompress/parse karta hai.
    Return: (status, urls_list)
      status = "ok"        -> mil gaya, urls_list mein links hain
      status = "not_found" -> is index par sitemap exist hi nahi karta (404) -> loop yahan rok dein
      status = "error"     -> koi aur masla (network/parse) -> is index ko skip kar k aage try karein
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
    except Exception as e:
        print(f"  [Error] Request failed for {url.split('/')[-1]}: {e}")
        return "error", []

    if response.status_code == 404:
        return "not_found", []

    try:
        response.raise_for_status()
    except Exception as e:
        print(f"  [Error] Bad status for {url.split('/')[-1]}: {e}")
        return "error", []

    # Decompress Gzip content
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as f:
            xml_content = f.read()
    except Exception as e:
        print(f"  [Error] Failed to decompress file: {e}")
        return "error", []

    # Parse XML tags
    try:
        root = ET.fromstring(xml_content)
        urls = [loc.text for loc in root.findall('.//ns:url/ns:loc', NAMESPACES)]
        return "ok", urls
    except ET.ParseError:
        print("  [Error] Failed to parse XML structure.")
        return "error", []


def scrape_category(category: str) -> list:
    """
    Ek category (BUY ya RENT) k liye pdp-0, pdp-1, pdp-2 ... aise barhta jata hai
    jab tak 404 (sitemap not found) na mil jaye, phir rok deta hai.
    Beech mein agar koi transient error (network glitch) aaye to us cluster ko
    skip kar k agle index par chala jata hai (rukta nahi), taake ek ghalat 500
    error puri scrape na rok de.
    """
    collected = []
    idx = 0
    consecutive_errors = 0

    while idx < MAX_CLUSTERS_PER_CATEGORY:
        url = f"https://www.immoscout24.ch/sitemap/pdp/pdp-{idx}-sitemap-{category}-en.xml.gz"
        filename = url.split("/")[-1]
        print(f"[{category}] Processing cluster: {filename}")

        status, urls = fetch_sitemap(url)

        if status == "not_found":
            print(f"  -> pdp-{idx} 404 mila. {category} category yahan khatam samjhi ja rahi hai.")
            break

        if status == "error":
            consecutive_errors += 1
            print(f"  -> Cluster skip kiya (error #{consecutive_errors} in a row).")
            # Agar lagataar 3 baar real error aaye (na ke 404), tab bhi ruk jao,
            # taake infinite/faulty loop na bane.
            if consecutive_errors >= 3:
                print(f"  -> {category}: 3 lagataar errors, safety k liye ruk rahe hain.")
                break
        else:
            consecutive_errors = 0  # successful fetch, error counter reset
            collected.extend(urls)
            print(f"  -> Extracted {len(urls)} links. Category total so far: {len(collected)}")

        idx += 1
        time.sleep(1.0)  # polite delay

    return collected


def load_existing_links(path: str) -> set:
    """Purani master file se already-known links load karta hai."""
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def send_email(new_links: list, attachment_path: str):
    """Gmail SMTP (App Password) k through naye links ki .txt file email krta hai."""
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    receiver = os.environ["RECEIVER_EMAIL"]

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = (
        f"ImmoScout24 - {len(new_links)} New Listings - "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )

    body = (
        f"{len(new_links)} new listing(s) mile hain is run mein.\n\n"
        f"Attached .txt file mein poori list hai.\n"
    )
    msg.attach(MIMEText(body, "plain"))

    with open(attachment_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="{os.path.basename(attachment_path)}"',
    )
    msg.attach(part)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, receiver, msg.as_string())


if __name__ == "__main__":
    print("Starting dynamic scrape (BUY aur RENT dono k liye jab tak 404 na mile)...")

    # 1. Purane (already known) links load karo
    existing_links = load_existing_links(MASTER_FILE)
    print(f"Loaded {len(existing_links)} previously known links from {MASTER_FILE}.")

    # 2. Fresh scrape - har category khud decide karegi kahan rukna hai
    print("\n--- BUY category ---")
    buy_links = scrape_category("BUY")
    print(f"BUY category total: {len(buy_links)} links")

    print("\n--- RENT category ---")
    rent_links = scrape_category("RENT")
    print(f"RENT category total: {len(rent_links)} links")

    all_extracted_listings = buy_links + rent_links
    current_links = set(all_extracted_listings)

    # 3. Naye links nikalo (jo pehle nahi thay)
    new_links = sorted(current_links - existing_links)
    print(f"\nTotal scraped this run: {len(current_links)} | Brand-new links: {len(new_links)}")

    if new_links:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_links_file = f"new_links_{timestamp}.txt"

        # Naye links ki alag .txt file (email attachment k liye)
        with open(new_links_file, "w", encoding="utf-8") as f:
            for link in new_links:
                f.write(f"{link}\n")

        # Purani master file mein naye links append karo
        with open(MASTER_FILE, "a", encoding="utf-8") as f:
            for link in new_links:
                f.write(f"{link}\n")

        print(f"Appended {len(new_links)} new links to {MASTER_FILE}")

        # 4. Email bhejo
        try:
            send_email(new_links, new_links_file)
            print("Email sent successfully.")
        except Exception as e:
            print(f"[Error] Email sending failed: {e}")

        # Local temp file clean rakhne k liye delete kar dete hain
        # (agar aap GitHub par yeh file bhi save rakhna chahain to yeh line hata dein)
        os.remove(new_links_file)
    else:
        print("Koi naya link nahi mila is run mein. Email nahi bheja gaya.")

    print("\nDone.")
