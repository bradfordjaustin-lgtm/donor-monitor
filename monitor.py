import time
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from datetime import datetime
import os

# ── SETTINGS (read from GitHub Secrets) ──────────────────────────────────────
SMTP_USER    = os.environ["SMTP_USER"]
SMTP_PASS    = os.environ["SMTP_PASS"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]

# ── DO NOT EDIT BELOW ────────────────────────────────────────────────────────
DONOR_URL           = "https://fairfaxcryobank.com/search/donorprofile.aspx?number=7638"
DONOR_NUMBER        = "7638"
OUT_OF_STOCK_PHRASE = "Notification List is full"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
STATE_FILE = "last_state.txt"

def fetch_vial_section():
    resp = requests.get(DONOR_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    order_div = soup.find("div", class_="order-sp")
    if order_div is None:
        raise ValueError("Could not find the order section on the page.")
    return str(order_div)

def vials_available(section_html):
    return OUT_OF_STOCK_PHRASE not in section_html

def extract_vial_info(section_html):
    soup = BeautifulSoup(section_html, "html.parser")
    table = soup.find("table")
    if not table:
        return "(Could not parse vial table.)"
    rows = table.find_all("tr")
    lines = []
    for row in rows[1:]:
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        text = " | ".join(c for c in cells if c)
        if text and OUT_OF_STOCK_PHRASE not in text and "Unsure of prep" not in text:
            lines.append(text)
    return "\n".join(lines) if lines else "(See page for details)"

def send_email(subject, body):
    msg = MIMEMultipart()
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())

def run():
    print(f"[{datetime.now()}] Monitor check for Donor #{DONOR_NUMBER}")

    try:
        with open(STATE_FILE, "r") as f:
            last_state = f.read().strip()
    except FileNotFoundError:
        last_state = "unknown"

    section   = fetch_vial_section()
    available = vials_available(section)
    new_state = "available" if available else "out_of_stock"

    print(f"  Current state : {new_state}")
    print(f"  Previous state: {last_state}")

    if available and last_state != "available":
        summary = extract_vial_info(section)
        subject = f"Donor #{DONOR_NUMBER} — Vials Now Available!"
        body = (
            f"Donor #{DONOR_NUMBER} now has vials available!\n\n"
            f"Detected: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Vial details:\n{summary}\n\n"
            f"Order here:\n{DONOR_URL}"
        )
        send_email(subject, body)
        print("  ACTION: Alert email sent — vials available!")

    elif not available and last_state == "available":
        subject = f"Donor #{DONOR_NUMBER} — Back Out of Stock"
        body = (
            f"Donor #{DONOR_NUMBER} vials are no longer showing as available.\n"
            f"Detected: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"The monitor is still running and will alert you if they return."
        )
        send_email(subject, body)
        print("  ACTION: Back-out-of-stock email sent.")

    else:
        print("  No change — no email sent.")

    with open(STATE_FILE, "w") as f:
        f.write(new_state)

if __name__ == "__main__":
    run()
