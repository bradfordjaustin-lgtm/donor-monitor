import smtplib
import requests
import os
import json
import traceback
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
STATE_FILE   = "last_state.json"   # JSON so we can store full vial detail
HEADERS      = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ─────────────────────────────────────────────────────────────────────────────
# PAGE FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_order_section():
    """Fetch the donor page and return the parsed Order Sperm div."""
    resp = requests.get(DONOR_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    order_div = soup.find("div", class_="order-sp")
    if order_div is None:
        raise ValueError(
            "Could not find the 'order-sp' section on the page. "
            "Fairfax may have changed their page layout."
        )
    return order_div


# ─────────────────────────────────────────────────────────────────────────────
# AVAILABILITY DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_available_vials(order_div):
    """
    Scan every product row and return a list of vials that are genuinely
    available to order. Each entry is a dict with keys: name, price, status.

    A vial is considered AVAILABLE if ANY of these four positive signals exist:
      1. <select> in the Quantity column  -> numeric dropdown, in stock
      2. <a class="anc add">              -> Add to Cart button
      3. <a href="tel:...">               -> Please Call link
      4. <div class="almost-gone">        -> Almost Gone banner

    A vial is explicitly NOT available if:
      - Its only action link contains "Notify Me" text (waitlist only)
      - None of the four positive signals are present
    """
    available = []

    rows = order_div.find_all("tr", class_="sys-product-cnt")
    for row in rows:

        # ── Extract vial name ──────────────────────────────────────────────
        name_el = row.find("span", class_="product-name")
        vial_name = name_el.get_text(separator=" ", strip=True) if name_el else "Unknown vial"

        # ── Extract price ──────────────────────────────────────────────────
        price_el = row.find("td", class_=lambda c: c and "sys-price-text" in c)
        price = price_el.get_text(strip=True) if price_el else ""

        # ── Explicit Notify Me guard ───────────────────────────────────────
        # Any row whose sole action is "Notify Me" is on waitlist only.
        action_links = row.find_all("a")
        only_notify_me = (
            len(action_links) == 1
            and "notify me" in action_links[0].get_text(strip=True).lower()
        )
        if only_notify_me:
            continue

        # ── Signal 1: quantity <select> dropdown ───────────────────────────
        select_el   = row.find("select")
        has_select  = select_el is not None

        # ── Signal 2: Add to Cart button ───────────────────────────────────
        add_to_cart    = row.find("a", class_=lambda c: c and "anc" in c and "add" in c)
        has_add_to_cart = add_to_cart is not None

        # ── Signal 3: Please Call link (tel: href, not Notify Me) ──────────
        please_call    = row.find("a", href=lambda h: h and h.startswith("tel:"))
        has_please_call = please_call is not None

        # ── Signal 4: Almost Gone banner ───────────────────────────────────
        almost_gone    = row.find("div", class_="almost-gone")
        has_almost_gone = almost_gone is not None

        # ── Only include rows with at least one positive signal ────────────
        if not (has_select or has_add_to_cart or has_please_call or has_almost_gone):
            continue

        # ── Build human-readable status string ────────────────────────────
        status_parts = []
        if has_almost_gone:
            status_parts.append("ALMOST GONE")
        if has_please_call:
            status_parts.append("Please Call")
        if has_add_to_cart:
            status_parts.append("Add to Cart")
        if has_select:
            opts    = select_el.find_all("option")
            max_qty = opts[-1].get("value", "?") if opts else "?"
            status_parts.append(f"Qty up to {max_qty}")

        available.append({
            "name":   vial_name,
            "price":  price,
            "status": " | ".join(status_parts),
        })

    return available


# ─────────────────────────────────────────────────────────────────────────────
# STATE PERSISTENCE  (JSON file committed back to repo each run)
# ─────────────────────────────────────────────────────────────────────────────

def load_last_state():
    """
    Returns a dict:
      { "vials": [ {"name": ..., "price": ..., "status": ...}, ... ] }
    or None if no previous state exists.
    """
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(available_vials):
    with open(STATE_FILE, "w") as f:
        json.dump({"vials": available_vials}, f, indent=2)


def vial_key(v):
    """Stable identifier for a vial: name + price."""
    return f"{v['name']}|{v['price']}"


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def format_vial_table(vials):
    if not vials:
        return "  (none)"
    return "\n".join(f"  • {v['name']}  {v['price']}  [{v['status']}]" for v in vials)


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


def send_error_email(error_summary):
    """Send a 'monitor failed' alert so you know the script broke."""
    subject = f"⚠️ Donor #{DONOR_NUMBER} Monitor — Error"
    body = (
        f"The availability monitor for Donor #{DONOR_NUMBER} encountered an error\n"
        f"and could not complete its check.\n\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Error details:\n{error_summary}\n\n"
        f"Action needed: check the GitHub Actions log at\n"
        f"https://github.com/bradfordjaustin-lgtm/donor-monitor/actions"
    )
    send_email(subject, body)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def run():
    print(f"[{datetime.now()}] Monitor check for Donor #{DONOR_NUMBER}")

    try:
        # ── Fetch page ─────────────────────────────────────────────────────
        order_div       = fetch_order_section()
        current_vials   = detect_available_vials(order_div)
        last_state      = load_last_state()

        # ── Summarise ──────────────────────────────────────────────────────
        current_keys    = {vial_key(v) for v in current_vials}
        last_vials      = last_state["vials"] if last_state else []
        last_keys       = {vial_key(v) for v in last_vials}

        newly_available = [v for v in current_vials  if vial_key(v) not in last_keys]
        newly_gone      = [v for v in last_vials      if vial_key(v) not in current_keys]

        print(f"  Currently available : {len(current_vials)} vial(s)")
        print(f"  Newly available     : {[v['name'] for v in newly_available]}")
        print(f"  Newly gone          : {[v['name'] for v in newly_gone]}")

        # ── Decide whether to email ────────────────────────────────────────
        # Send one consolidated email whenever the available set changes in
        # any direction: something newly appeared OR something newly vanished.
        something_changed = bool(newly_available or newly_gone)

        if something_changed:
            if current_vials:
                subject = f"Donor #{DONOR_NUMBER} — Vial Availability Updated"
                change_lines = []
                if newly_available:
                    change_lines.append(
                        "NEW — now available:\n" + format_vial_table(newly_available)
                    )
                if newly_gone:
                    change_lines.append(
                        "REMOVED — no longer available:\n" + format_vial_table(newly_gone)
                    )
                body = (
                    f"Donor #{DONOR_NUMBER} vial availability has changed.\n\n"
                    f"Detected: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    + "\n\n".join(change_lines)
                    + f"\n\nFull list currently available:\n"
                    + format_vial_table(current_vials)
                    + f"\n\nOrder here:\n{DONOR_URL}"
                )
            else:
                # Everything gone
                subject = f"Donor #{DONOR_NUMBER} — All Vials Now Out of Stock"
                body = (
                    f"Donor #{DONOR_NUMBER} no longer has any vials available.\n\n"
                    f"Detected: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"Vials that were removed:\n"
                    + format_vial_table(newly_gone)
                    + f"\n\nThe monitor is still running and will alert you if they return."
                )

            send_email(subject, body)
            print(f"  ACTION: Email sent — '{subject}'")

        else:
            print("  No change — no email sent.")

        # ── Persist new state ──────────────────────────────────────────────
        save_state(current_vials)

    except Exception as e:
        error_text = traceback.format_exc()
        print(f"  ERROR: {error_text}")
        try:
            send_error_email(error_text)
            print("  ACTION: Error notification email sent.")
        except Exception as mail_err:
            print(f"  ALSO FAILED to send error email: {mail_err}")
        raise   # re-raise so GitHub Actions marks the run as failed (red X)


if __name__ == "__main__":
    run()
