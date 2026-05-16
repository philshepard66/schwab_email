#!/usr/bin/env python3
"""
Schwab portfolio email: reads positions from stdin, generates an HTML summary,
renders to PNG with Chrome headless, and sends via Gmail SMTP with
CID-referenced inline images.

Usage:
    pbpaste | schwab_email3.py          # pipe clipboard; sends immediately
    schwab_email3.py --dry-run          # print HTML only, don't send
    schwab_email3.py --browser          # open HTML preview in browser
    schwab_email3.py --data f.txt       # read from file instead of stdin

Requires: pip3 install Pillow
Gmail app password must be stored in macOS Keychain (see README or --help).

Here's how it works:

schwab % pbpaste | schwab_email3.py

Read 8,298 characters of positions data.
Fetching market indices from Yahoo Finance ...
Subject: Charles Schwab  $645,628 +$25,033 +$251,085 (Mon)
Rendering /Users/philshepard/schwab/email_draft.html -> /Users/philshepard/schwab/email_rendered.png via Chrome headless...
  Cropped email_rendered.png: 1600x6000 -> 1600x2257 (trimmed T=61 B=3682, kept full width)
Rendered PNG: /Users/philshepard/schwab/email_rendered.png
Found today's Schwab snapshot: Schwab 5:11:2026.png
Using Keychain entry: -a phil.shepard@gmail.com -s schwab_email_smtp (16 chars)
Connecting to smtp.gmail.com:465 as phil.shepard@gmail.com ...
Email sent to phil.shepard@gmail.com, phil.shepard@venturecomm.net.
schwab % 

"""

import os
import re
import sys
import html as html_lib
import argparse
import shutil
import smtplib
import subprocess
import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from email.utils import make_msgid
import json
import urllib.request


SCHWAB_URL      = "https://client.schwab.com/app/accounts/positions"
SENDER          = "phil.shepard@gmail.com"
RECIPIENT       = "phil.shepard@gmail.com"

SMTP_HOST       = "smtp.gmail.com"
SMTP_PORT       = 465
SMTP_KEYCHAIN_SERVICE = "schwab_email_smtp"

SCHWAB_DIR      = os.path.expanduser("~/schwab")
DRAFT_PATH      = os.path.join(SCHWAB_DIR, "email_draft.html")
RENDERED_PNG    = os.path.join(SCHWAB_DIR, "email_rendered.png")
SNAPSHOT_PNG    = os.path.join(SCHWAB_DIR, "email_snapshot.png")
SCHWAB_PNG_DIR  = os.path.join(SCHWAB_DIR, "snapshots")

CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
RENDER_WIDTH = 1600
FOOTER_ID = "footer"

# --- Styles (must match exactly for email rendering) ---
P_STYLE = 'style="font-size:48pt; color:#FEFEFE; margin:10px 0; font-family:\'Times New Roman\',Times,serif;"'
GREEN = "#00CC44"
RED   = "#FF3333"


def parse_money(s: str) -> float:
    """Parse a money string like '+$27,768.11' or '-$900.58' or '$648,362.49' into a float."""
    s = s.strip().replace(",", "").replace("$", "")
    return float(s)


def parse_positions(text: str):
    """Parse Schwab clipboard text into summary totals and individual positions.

    Returns (summary_dict, positions_list) where:
        summary = {total_value, day_change, total_gain}
        positions = [{symbol, description, qty, price_change, day_change, cost_basis, gain_loss}, ...]
    """
    lines = text.split("\n")

    # --- Parse summary totals ---
    summary = {}
    for i, line in enumerate(lines):
        if line.strip() == "Total accounts value" and i + 1 < len(lines):
            summary["total_value"] = parse_money(lines[i + 1].strip())
        elif line.strip() == "Total day change" and i + 1 < len(lines):
            summary["day_change"] = parse_money(lines[i + 1].strip())
        elif line.strip().startswith("Total gain/loss") and i + 1 < len(lines):
            summary["total_gain"] = parse_money(lines[i + 1].strip())

    # --- Parse individual positions ---
    # Each position block looks like:
    #   SYMBOL\n\nDESCRIPTION\n$Price\tQty\tPriceChg$\tPriceChg%\tMktVal\tDayChg$\tDayChg%\t\n$CostBasis\n+/-$GainLoss\tGainLoss%
    #
    # The data line has tabs separating: Price, Qty, PriceChg$, PriceChg%, MktVal, DayChg$, DayChg%

    positions = []

    # Find lines that look like stock data: $price\tqty\t...
    data_pattern = re.compile(
        r'^\$[\d,.]+\t'        # Price
        r'([\d,.]+)\t'         # Qty
        r'([+-]?\$[\d,.]+)\t'  # Price Change $
        r'[+-]?[\d,.]+%\t'     # Price Change %
        r'\$[\d,.]+\t'         # Market Value
        r'([+-]?\$[\d,.]+)\t'  # Day Change $
        r'[+-]?[\d,.]+%'       # Day Change %
    )

    for i, line in enumerate(lines):
        m = data_pattern.match(line.strip())
        if not m:
            continue

        qty_str = m.group(1)
        price_chg_str = m.group(2)
        day_chg_str = m.group(3)

        qty = float(qty_str.replace(",", ""))

        # Look backwards for symbol and description
        # Pattern: symbol is 1-5 uppercase letters on its own line, preceded by account header or another position
        symbol = None
        description = None
        for j in range(i - 1, max(i - 5, -1), -1):
            candidate = lines[j].strip()
            if candidate and re.match(r'^[A-Z]{1,5}$', candidate):
                symbol = candidate
                # Description is the next non-empty line after symbol
                for k in range(j + 1, i):
                    desc = lines[k].strip()
                    if desc and not re.match(r'^[A-Z]{1,5}$', desc):
                        description = desc
                        break
                break

        if not symbol:
            continue

        # Look forward for cost basis and gain/loss
        # Next line after data line should be cost basis, then gain/loss
        cost_basis = None
        gain_loss = None
        for j in range(i + 1, min(i + 4, len(lines))):
            next_line = lines[j].strip()
            # Cost basis line: just a dollar amount like "$44,692.01"
            if cost_basis is None and re.match(r'^\$[\d,.]+$', next_line):
                cost_basis = parse_money(next_line)
            # Gain/loss line: "+$9,605.31\t+21.49%"
            elif gain_loss is None and re.match(r'^[+-]\$[\d,.]+\t', next_line):
                gl_match = re.match(r'^([+-]\$[\d,.]+)', next_line)
                if gl_match:
                    gain_loss = parse_money(gl_match.group(1))
            elif next_line == "Incomplete":
                cost_basis = 0
                gain_loss = None

        positions.append({
            "symbol": symbol,
            "description": description or symbol,
            "qty": qty,
            "price_change": parse_money(price_chg_str),
            "day_change": parse_money(day_chg_str),
            "cost_basis": cost_basis,
            "gain_loss": gain_loss,
        })

    return summary, positions


def format_money(val: float, always_sign: bool = False) -> str:
    """Format a float as $X,XXX with optional sign prefix."""
    abs_val = abs(val)
    if abs_val >= 1:
        formatted = f"${abs_val:,.0f}"
    else:
        formatted = f"${abs_val:,.2f}"

    if val < 0:
        return f"-{formatted}"
    elif always_sign:
        return f"+{formatted}"
    else:
        return formatted


def generate_html(positions_text: str) -> str:
    """Parse positions and generate HTML body — no API call needed."""
    summary, positions = parse_positions(positions_text)

    day_of_week = datetime.datetime.now().strftime("%a")

    total_value = format_money(summary.get("total_value", 0))
    day_change = format_money(summary.get("day_change", 0), always_sign=True)
    total_gain = format_money(summary.get("total_gain", 0), always_sign=True)
    day_color = GREEN if summary.get("day_change", 0) >= 0 else RED

    # Summary line
    html_parts = []
    html_parts.append(
        f'<p {P_STYLE}>'
        f'<span style="color:#FEFEFE;">{total_value} </span>'
        f'<span style="font-size:72pt; font-weight:bold; font-style:italic; color:{day_color};">'
        f'{day_change}</span>'
        f'<span style="color:#FEFEFE;"> {total_gain} ({day_of_week})</span>'
        f'</p>'
    )

    # Individual positions (qty > 1 only)
    for pos in positions:
        if pos["qty"] <= 1:
            continue

        qty = round(pos["qty"])
        price_chg = pos["price_change"]
        day_chg = pos["day_change"]
        gain_loss = pos["gain_loss"]

        # Format price change: positive = no sign with leading zero, negative = minus sign
        if price_chg >= 0:
            price_chg_str = f"{price_chg:.2f}"
        else:
            price_chg_str = f"-{abs(price_chg):.2f}"

        dollar_result = format_money(day_chg, always_sign=True)
        result_color = GREEN if day_chg >= 0 else RED

        # Gain/loss
        if gain_loss is not None:
            gain_loss_str = format_money(gain_loss, always_sign=True)
            gain_loss_color = GREEN if gain_loss >= 0 else RED
        else:
            gain_loss_str = "N/A"
            gain_loss_color = "#FEFEFE"

        html_parts.append(
            f'<p {P_STYLE}>'
            f'<span style="color:#FEFEFE;">{pos["symbol"]} {qty}x{price_chg_str}=</span>'
            f'<span style="font-size:72pt; font-weight:bold; font-style:italic; color:{result_color};">'
            f'{dollar_result}</span>'
            f'<span style="color:#FEFEFE;">  </span>'
            f'<span style="font-size:48pt; font-style:italic; color:{gain_loss_color};">'
            f'({gain_loss_str})</span>'
            f'</p>'
        )

    body = "\n".join(html_parts)

    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background-color:#000000;">'
        '<tr><td style="background-color:#000000; padding:40px; text-align:center; '
        "font-family:'Times New Roman',Times,serif;\">"
        f'\n{body}\n'
        '</td></tr></table>'
    )


def time_stamp():
    now = datetime.datetime.now(tz=ZoneInfo("America/Los_Angeles"))
    ms = now.microsecond // 1000
    now_str = now.strftime(f"\n%-I:%M:%S.{ms:03d} %p PT\n")
    print(now_str)


def read_from_stdin() -> str:
    if sys.stdin.isatty():
        print("Save the schwab mm/dd/yyyy.png")
        print("Paste the Schwab positions page text below.")
        print("(On the Schwab positions page: CMD+A, CMD+C, then come here and CMD+V)")
        print("Press Ctrl+D when done.\n")
    text = sys.stdin.read().strip()
    if not text:
        raise RuntimeError("No positions text received on stdin.")
    print(f"Read {len(text):,} characters of positions data.")
    return text

def fetch_indices() -> str:
    """Fetch DJIA, NASDAQ, S&P 500, Russell 2000 from Yahoo Finance and return HTML."""
    """("^RUT", "Russell 2000"), didn't want to see this one anymore. """
    symbols = [
        ("^DJI", "DJIA"),
        ("^IXIC", "NASDAQ"),
        ("^GSPC", "S&P 500"),
    ]
    parts_line1 = []
    parts_line2 = []
    for symbol, label in symbols:
        try:
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                f"?interval=1d&range=1d"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            meta = data["chart"]["result"][0]["meta"]
            price = meta["regularMarketPrice"]
            prev = meta["chartPreviousClose"]
            change = price - prev
            sign = "+" if change >= 0 else ""
            color = GREEN if change >= 0 else RED
            entry = (
                f'<span style="color:#FEFEFE;">{label} {price:,.2f} </span>'
                f'<span style="font-style:italic; color:{color};">'
                f'{sign}{change:,.2f}</span>'
            )
        except Exception as e:
            print(f"Warning: could not fetch {label}: {e}")
            entry = f'<span style="color:#FEFEFE;">{label} N/A</span>'
        if label in ("DJIA", "NASDAQ"):
            parts_line1.append(entry)
        else:
            parts_line2.append(entry)

    style = (
        'style="font-size:36pt; color:#FEFEFE; margin:10px 0; '
        "font-family:'Times New Roman',Times,serif;\""
    )
    sep = '<span style="color:#FEFEFE;"> &middot; </span>'
    html = (
        f'<p {style}>{sep.join(parts_line1)}</p>'
        f'<p {style}>{sep.join(parts_line2)}</p>'
    )
    return html


def build_subject(html: str) -> str:
    """Concatenate text of all <p> tags except the footer for the subject line."""
    paragraphs = re.findall(
        rf'<p(?![^>]*\bid=["\']{FOOTER_ID}["\'])[^>]*>(.*?)</p>',
        html, re.DOTALL
    )
    parts = []
    for p in paragraphs[:1]:
        text = re.sub(r"<[^>]+>", "", p)
        text = html_lib.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            parts.append(text)
    return "Charles Schwab  " + "  ".join(parts)


def find_todays_png() -> str | None:
    """Return path to today's cropped Schwab snapshot, or None."""
    now = datetime.datetime.now(tz=ZoneInfo("America/Los_Angeles"))
    filename = f"Schwab {now.month}:{now.day}:{now.year}.png"
    path = os.path.join(SCHWAB_PNG_DIR, filename)
    if not os.path.exists(path):
        print(f"No Schwab snapshot found for today ({filename}).")
        return None

    print(f"Found today's Schwab snapshot: {filename}")
    shutil.copy2(path, SNAPSHOT_PNG)
    _crop_background(SNAPSHOT_PNG)
    return SNAPSHOT_PNG


def write_draft_html(body_html: str) -> None:
    page = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    html, body {{
      background-color: #000000;
      margin: 0;
      padding: 0;
      width: {RENDER_WIDTH}px;
    }}
  </style>
</head>
<body>
{body_html}
</body>
</html>"""
    with open(DRAFT_PATH, "w") as f:
        f.write(page)


def render_html_to_png(html_path: str, png_path: str) -> None:
    """Render HTML to PNG via Chrome headless, then crop black padding."""
    if not os.path.exists(CHROME_APP):
        raise RuntimeError(
            f"Google Chrome not found at {CHROME_APP}. "
            "Install Chrome or edit CHROME_APP at the top of this script."
        )

    file_url = f"file://{html_path}"
    tall_height = 6000

    print(f"Rendering {html_path} -> {png_path} via Chrome headless...")
    cmd = [
        CHROME_APP,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-sandbox",
        f"--window-size={RENDER_WIDTH},{tall_height}",
        "--default-background-color=000000",
        f"--screenshot={png_path}",
        "--virtual-time-budget=2000",
        file_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(png_path):
        print("Chrome stdout:", result.stdout)
        print("Chrome stderr:", result.stderr)
        raise RuntimeError("Chrome headless screenshot failed.")

    _crop_background(png_path)
    print(f"Rendered PNG: {png_path}")


def _crop_background(png_path: str, bg_color=(0, 0, 0), tolerance=8, buffer_px=8) -> None:
    """Crop uniform-background rows from top and bottom only, preserving width."""
    try:
        from PIL import Image
    except ImportError:
        print("  *** WARNING: Pillow not installed. Run: pip3 install Pillow")
        print("  *** Without Pillow, rendered PNG will have lots of black padding.")
        return

    img = Image.open(png_path).convert("RGB")
    w, h = img.size
    pixels = img.load()
    bg_r, bg_g, bg_b = bg_color

    def row_has_content(y: int) -> bool:
        for x in range(0, w, 8):
            r, g, b = pixels[x, y]
            if (abs(r - bg_r) > tolerance or
                abs(g - bg_g) > tolerance or
                abs(b - bg_b) > tolerance):
                return True
        return False

    first_row = 0
    for y in range(h):
        if row_has_content(y):
            first_row = y
            break

    last_row = h - 1
    for y in range(h - 1, -1, -1):
        if row_has_content(y):
            last_row = y
            break

    top    = max(0, first_row - buffer_px)
    bottom = min(h, last_row + buffer_px)

    if top > 0 or bottom < h:
        cropped = img.crop((0, top, w, bottom))
        cropped.save(png_path)
        print(f"  Cropped {os.path.basename(png_path)}: "
              f"{w}x{h} -> {w}x{bottom - top} "
              f"(trimmed T={top} B={h - bottom}, kept full width)")


def get_smtp_password() -> str:
    result = subprocess.run(
        ["security", "find-generic-password",
         "-a", SENDER,
         "-s", SMTP_KEYCHAIN_SERVICE,
         "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not read SMTP password from Keychain "
            f"(-a {SENDER} -s {SMTP_KEYCHAIN_SERVICE}). "
            f"Add it with: security add-generic-password "
            f"-a {SENDER} -s {SMTP_KEYCHAIN_SERVICE} -w 'APP-PASSWORD'"
        )
    pw = result.stdout.strip()
    print(f"Using Keychain entry: -a {SENDER} -s {SMTP_KEYCHAIN_SERVICE} ({len(pw)} chars)")
    return pw


def send_via_smtp(subject: str, rendered_png: str,
                  snapshot_png: str | None) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = SENDER
    msg["To"]      = RECIPIENT

    rendered_cid = make_msgid(domain="schwab.local")
    snapshot_cid = make_msgid(domain="schwab.local")
    rendered_cid_bare = rendered_cid[1:-1]
    snapshot_cid_bare = snapshot_cid[1:-1]

    html_parts = ['<div style="background-color:#000000; margin:0; padding:0;">']
    html_parts.append(
        f'<img src="cid:{rendered_cid_bare}" '
        f'style="display:block; width:100%; max-width:{RENDER_WIDTH}px; '
        f'height:auto; border:0; margin:0; padding:0;" '
        f'alt="Portfolio summary">'
    )
    if snapshot_png:
        html_parts.append('<div style="height:40px; background-color:#000000;">&nbsp;</div>')
        html_parts.append(
            f'<img src="cid:{snapshot_cid_bare}" '
            f'style="display:block; width:100%; max-width:{RENDER_WIDTH}px; '
            f'height:auto; border:0; margin:0; padding:0;" '
            f'alt="Schwab snapshot">'
        )
    html_parts.append("</div>")
    html_body = "\n".join(html_parts)

    msg.set_content(subject)
    msg.add_alternative(html_body, subtype="html")

    html_payload = msg.get_payload()[1]

    with open(rendered_png, "rb") as f:
        html_payload.add_related(
            f.read(),
            maintype="image", subtype="png",
            cid=rendered_cid,
            disposition="inline",
            filename=os.path.basename(rendered_png),
        )

    if snapshot_png:
        with open(snapshot_png, "rb") as f:
            html_payload.add_related(
                f.read(),
                maintype="image", subtype="png",
                cid=snapshot_cid,
                disposition="inline",
                filename=os.path.basename(snapshot_png),
            )

    password = get_smtp_password()
    print(f"Connecting to {SMTP_HOST}:{SMTP_PORT} as {SENDER} ...")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(SENDER, password)
        smtp.send_message(msg)
    print(f"Email sent to {RECIPIENT}.")


def main():
    parser = argparse.ArgumentParser(description="Generate & send Schwab portfolio email")
    parser.add_argument("--data",      help="Path to saved Schwab page text (skips stdin)")
    parser.add_argument("--dry-run",   action="store_true", help="Print HTML only, don't send")
    parser.add_argument("--browser",   action="store_true", help="Open HTML preview in browser")
    args = parser.parse_args()

    os.makedirs(SCHWAB_PNG_DIR, exist_ok=True)

    print()

    tz = ZoneInfo("America/Los_Angeles")
    start = datetime.datetime.now(tz=tz)


    if args.data:
        print(f"Reading positions from file: {args.data}")
        with open(args.data) as f:
            positions_text = f.read()
    else:
        positions_text = read_from_stdin()

    html = generate_html(positions_text)

    print("Fetching market indices from Yahoo Finance ...")
    indices_html = fetch_indices()

    pt = datetime.datetime.now(tz=ZoneInfo("America/Los_Angeles"))
    timestamp_str = pt.strftime("%a %b %-d, %Y %-I:%M %p PT")

    footer_html = (
       f'<p id="{FOOTER_ID}" style="font-size:36pt; color:#FEFEFE; '
       f'margin:30px 0 60px 0; font-family:\'Times New Roman\',Times,serif;">'
       f'Charles Schwab &middot; Positions &middot; {timestamp_str}</p>'
       f'<div style="height:60px;">&nbsp;</div>'
       f'<p><br><br></p>'
    )

    new_html, n = re.subn(
        r"</td>\s*</tr>\s*</table>\s*$",
        indices_html + footer_html + "</td></tr></table>",
        html.rstrip(),
    )

    html = new_html if n == 1 else html + indices_html + footer_html + f'<p><br><br></p>'

    if args.dry_run:
        print("\n--- Generated HTML ---")
        print(html)
        return

    subject = build_subject(html)
    print(f"Subject: {subject}")

    write_draft_html(html)

    if args.browser:
        subprocess.run(["open", DRAFT_PATH])
        print(f"Opened {DRAFT_PATH} in browser. (Preview only - not sent.)")
        return

    render_html_to_png(DRAFT_PATH, RENDERED_PNG)
    snapshot_png = find_todays_png()
    send_via_smtp(subject, RENDERED_PNG, snapshot_png)

    end = datetime.datetime.now(tz=tz)
    diff = end - start
    fmt = "%-I:%M:%S.%f %p"

# %-I   Hour (12-hour clock, no leading zero)    → 2
# %M    Minutes (zero-padded)                    → 07
# %S    Seconds (zero-padded)                    → 03
# %f    Microseconds (6 digits)                  → 042516
# %p    AM/PM                                    → PM
# PT    Literal text "Pacific Time"
# So it produces something like: 2:07:03.042516 PM PT

#Start: 6:03:00.997252 PM PT
#  End: 6:03:08.167292 PM PT
# Diff: 7.170040s


    print()
    print(f"Start: {start.strftime(fmt)}")
    #       Start: 6:03:00.997252 PM PT

    print(f"  End: {end.strftime(fmt)}")
    print(f" Diff: {diff.total_seconds():.6f}s")
    print()

if __name__ == "__main__":
    main()
