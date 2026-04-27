import smtplib
import requests
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from datetime import datetime

# ── SETTINGS (read from GitHub Secrets) ──────────────────────────────────────
SMTP_USER    = os.environ["SMTP_USER"]
SMTP_PASS    = os.environ["SMTP_PASS"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]

# ── DO NOT EDIT BELOW ─────────────────────────────────────────────────────────
DONOR_URL    = "https://fairfaxcryobank.com/search/donorprofile.aspx?number=7638"
DONOR_NUMBER = "7638"
STATE_FILE   = "last_state.txt"
HEADERS      = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def fetch_order_section():
    """Fetch the page and return the parsed Order Sperm div."""
    resp = requests.get(DONOR_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    order_div = soup.find("div", class_="order-sp")
    if order_div is None:
        raise ValueError("Could not find the 'order-sp' section on the page.")
    return order_div


def detect_available_vials(order_div):
    """
    Scan every product row in the Order Sperm table and return a list of
    vials that are genuinely available to order.

    A vial is considered available if ANY of these positive signals are present
    in its row:
      1. A <select> element in the Quantity column  (numeric dropdown = in stock)
      2. An 'Add to Cart' link/button               (class="anc add")
      3. A 'Please Call' link                        (href="tel:..." class="anc call")
      4. A <div class="almost-gone"> element         (limited stock banner)

    Rows that only show 'Notify Me' are NOT available.
    The 'Notification List is full' message means nothing is available.
    """
    available = []

    rows = order_div.find_all("tr", class_="sys-product-cnt")
    for row in rows:
        # ── Extract vial name ──────────────────────────────────────────────
        name_el = row.find("span", class_="product-name")
        vial_name = name_el.get_text(strip=True) if name_el else "Unknown vial"

        # ── Extract price ──────────────────────────────────────────────────
        price_el = row.find("td", class_=lambda c: c and "sys-price-text" in c)
        price = price_el.get_text(strip=True) if price_el else ""

        # ── Signal 1: quantity <select> dropdown ───────────────────────────
        has_select = row.find("select") is not None

        # ── Signal 2: Add to Cart button ───────────────────────────────────
        add_to_cart = row.find("a", class_=lambda c: c and "anc" in c and "add" in c)
        has_add_to_cart = add_to_cart is not None

        # ── Signal 3: Please Call link ─────────────────────────────────────
        # Distinguishable from Notify Me by its tel: href
        please_call = row.find("a", href=lambda h: h and h.startswith("tel:"))
        has_please_call = please_call is not None

        # ── Signal 4: Almost Gone banner ───────────────────────────────────
        almost_gone = row.find("div", class_="almost-gone")
        has_almost_gone = almost_gone is not None

        if has_select or has_add_to_cart or has_please_call or has_almost_gone:
            status_parts = []
            if has_almost_gone:
                status_parts.append("ALMOST GONE")
            if has_please_call:
                status_parts.append("Please Call")
            if has_add_to_cart:
                status_parts.append("Add to Cart")
            if has_select:
                # Count how many quantity options there are
                opts = row.find("select").find_all("option")
                status_parts.append(f"Qty up to {opts[-1]['value'] if opts else '?'}")

            available.append({
                "name":   vial_name,
                "price":  price,
                "status": " | ".join(status_parts),
            })

    return available


def format_vial_summary(available_vials):
    lines = []
    for v in available_vials:
        lines.append(f"  • {v['name']}  {v['price']}  [{v['status']}]")
    return "\n".join(lines)


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

    # ── Load previous state ────────────────────────────────────────────────
    try:
        with open(STATE_FILE, "r") as f:
            last_state = f.read().strip()   # "available" or "out_of_stock"
    except FileNotFoundError:
        last_state = "unknown"

    # ── Fetch and analyse ──────────────────────────────────────────────────
    order_div      = fetch_order_section()
    available_vials = detect_available_vials(order_div)
    new_state      = "available" if available_vials else "out_of_stock"

    print(f"  Current state : {new_state}")
    print(f"  Previous state: {last_state}")
    if available_vials:
        for v in available_vials:
            print(f"    {v['name']} {v['price']} [{v['status']}]")

    # ── Send notifications only on state transitions ───────────────────────
    if available_vials and last_state != "available":
        summary = format_vial_summary(available_vials)
        subject = f"Donor #{DONOR_NUMBER} — Vials Now Available!"
        body = (
            f"Donor #{DONOR_NUMBER} now has vials available to order!\n\n"
            f"Detected: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Available vials:\n{summary}\n\n"
            f"Order here:\n{DONOR_URL}"
        )
        send_email(subject, body)
        print("  ACTION: Alert email sent — vials available!")

    elif not available_vials and last_state == "available":
        subject = f"Donor #{DONOR_NUMBER} — Back Out of Stock"
        body = (
            f"Donor #{DONOR_NUMBER} vials are no longer showing as available.\n"
            f"Detected: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"The monitor is still running and will alert you if they return."
        )
        send_email(subject, body)
        print("  ACTION: Back-out-of-stock email sent.")

    else:
        print("  No state change — no email sent.")

    # ── Persist new state ──────────────────────────────────────────────────
    with open(STATE_FILE, "w") as f:
        f.write(new_state)


if __name__ == "__main__":
    run()
