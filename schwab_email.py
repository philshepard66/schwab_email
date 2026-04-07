#!/usr/bin/env python3
"""
schwab_email.py

Workflow:
  1. Read Schwab positions page text from stdin (pbpaste | schwab_email.py)
  2. Send text to Claude API with strict HTML email template
  3. Open a draft in Mail.app via AppleScript (rendered as HTML)

Usage:
    pbpaste | schwab_email.py            # pipe clipboard (CMD+A, CMD+C on Schwab page)
    schwab_email.py --dry-run            # print HTML only, don't open Mail
    schwab_email.py --data f.txt         # use saved text file instead of stdin

Version history:
  2026-04-06a  Initial stdin/pbpaste version
  2026-04-06b  Fixed HTML rendering in Mail.app (use HTML file + set html content)
  2026-04-06c  Fixed font sizes (pt->px), forced white text, no hallucinated positions
  2026-04-06d  Body 48pt/white, totals 72pt; +$/–$ on results; pt units to avoid Mail.app
               px/pt confusion; -webkit-text-fill-color for gray text fix; × → x

"""

import os
import sys
import argparse
import subprocess
import datetime
import anthropic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCHWAB_URL  = "https://client.schwab.com/app/accounts/positions"
RECIPIENT   = "phil.shepard@gmail.com"
THRESHOLD   = 100          # min abs(day $ change) to include a stock
MODEL       = "claude-sonnet-4-20250514"

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are generating the HTML body of an email. "
    "Return ONLY raw HTML — no markdown, no code fences, no explanation, "
    "no preamble. Start your response with <div and end with </div>."
)

def build_user_prompt(positions_text: str) -> str:
    day_of_week = datetime.datetime.now().strftime("%a")   # Mon, Tue …
    return f"""
You are generating the HTML body of an email. Return ONLY raw HTML with no markdown, no code fences, no explanation.

CRITICAL: Use ONLY the positions data provided below. Do NOT invent, estimate, or add any stocks not present in the data.

Use this EXACT template. Copy the style attributes character-for-character. Do not change any values.

The outer div and every <p> tag must have style attributes exactly as shown — do not omit or alter them:

<div style="background-color:#000000; padding:40px; text-align:center; font-family:'Times New Roman',serif;">

<p style="font-size:48pt; color:#FFFFFF !important; -webkit-text-fill-color:#FFFFFF; margin:10px 0; font-family:'Times New Roman',serif;">${{TOTAL_VALUE}} <span style="font-size:72pt; font-weight:bold; font-style:italic; color:${{DAY_COLOR}} !important; -webkit-text-fill-color:${{DAY_COLOR}};">${{DAY_CHANGE}}</span> <span style="font-size:48pt; color:#FFFFFF !important; -webkit-text-fill-color:#FFFFFF;">${{TOTAL_GAIN}} ({day_of_week})</span></p>

[ONE <p> PER QUALIFYING STOCK — copy this pattern exactly:]
<p style="font-size:48pt; color:#FFFFFF !important; -webkit-text-fill-color:#FFFFFF; margin:10px 0; font-family:'Times New Roman',serif;">{{SYMBOL}} {{QTY}}x{{PRICE_CHANGE}}=<span style="font-size:72pt; font-weight:bold; font-style:italic; color:{{RESULT_COLOR}} !important; -webkit-text-fill-color:{{RESULT_COLOR}};">{{DOLLAR_RESULT}}</span></p>

</div>

Now fill in using ONLY this Schwab positions data (do not use any other data):

{positions_text}

Rules:
- ONLY include stocks that appear in the data above — no invented tickers
- Only include stocks where abs(day dollar change) >= ${THRESHOLD}
- Sort by abs(day dollar change) descending
- QTY = share count from the data (e.g. 1000, 101, 37) — use exact quantity shown
- PRICE_CHANGE = per-share day change with 2 decimal places, NO sign for positives, negative sign for negatives (e.g. 6.96 or -47.39 or .40)
- DOLLAR_RESULT = QTY x PRICE_CHANGE rounded to nearest whole dollar, formatted with commas, prefix +$ for positives and -$ for negatives (e.g. +$6,499 or -$162 or +$40)
- TOTAL_VALUE = total accounts value from the data, formatted as $432,613
- DAY_CHANGE = total day change from the data, formatted as +$12,324 or -$2,364
- TOTAL_GAIN = total gain/loss from the data, formatted as +$71,035
- DAY_COLOR: #00CC44 if day change >= 0, else #FF3333
- RESULT_COLOR: #00CC44 if dollar result >= 0, else #FF3333
""".strip()

# ---------------------------------------------------------------------------
# Read positions text from stdin (paste from clipboard)
# ---------------------------------------------------------------------------

def read_from_stdin() -> str:
    """
    Reads Schwab positions page text from stdin.
    Usage:  pbpaste | schwab_email.py
       or:  schwab_email.py   (then paste and press Ctrl+D)
    """
    if sys.stdin.isatty():
        print("Paste the Schwab positions page text below.")
        print("(On the Schwab positions page: CMD+A, CMD+C, then come here and CMD+V)")
        print("Press Ctrl+D when done.\n")
    text = sys.stdin.read().strip()
    if not text:
        raise RuntimeError("No positions text received on stdin.")
    print(f"Read {len(text):,} characters of positions data.")
    return text

# ---------------------------------------------------------------------------
# Generate HTML via Claude
# ---------------------------------------------------------------------------

def generate_html(positions_text: str) -> str:
    print("Calling Claude to generate email HTML ...")
    client = anthropic.Anthropic()

    message = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": build_user_prompt(positions_text)
        }]
    )

    html = message.content[0].text.strip()

    # Strip any accidental markdown fences
    if html.startswith("```"):
        lines = html.split("\n")
        html = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return html

# ---------------------------------------------------------------------------
# Build subject line
# ---------------------------------------------------------------------------

def build_subject(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return "Charles Schwab " + text[:120].strip()

# ---------------------------------------------------------------------------
# Open Mail.app draft via AppleScript
# ---------------------------------------------------------------------------

def open_mail_draft(recipient: str, subject: str, html_body: str):
    """
    Writes HTML to ~/schwab_email_draft.html then uses AppleScript to create
    a Mail draft with the html content property (renders HTML, not raw text).
    """
    tmp_path = os.path.expanduser("~/schwab_email_draft.html")
    with open(tmp_path, "w") as f:
        f.write(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background-color:#000000; margin:0; padding:0;">
{html_body}
</body></html>""")

    safe_subject = subject.replace("\\", "\\\\").replace('"', '\\"')

    # Read HTML from file inside AppleScript — avoids escaping the entire HTML body
    script = f"""
set htmlPath to "{tmp_path}"
set htmlFile to open for access POSIX file htmlPath
set htmlContent to read htmlFile as \u00abclass utf8\u00bb
close access htmlFile

tell application "Mail"
    set newMessage to make new outgoing message with properties {{subject:"{safe_subject}", visible:true}}
    tell newMessage
        make new to recipient at end of to recipients with properties {{address:"{recipient}"}}
        set the html content to htmlContent
    end tell
    activate
end tell
"""
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        print("AppleScript error:", result.stderr)
        print(f"HTML saved to {tmp_path} — opening in browser as fallback.")
        subprocess.run(["open", tmp_path])
    else:
        print(f"Mail draft opened. HTML also saved to {tmp_path}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate Schwab portfolio email draft")
    parser.add_argument("--data",      help="Path to saved Schwab page text (skips Chrome scrape)")
    parser.add_argument("--dry-run",   action="store_true", help="Print HTML only, don't open Mail")
    parser.add_argument("--threshold", type=int, default=THRESHOLD,
                        help=f"Min abs day $ change to show a stock (default {THRESHOLD})")
    args = parser.parse_args()

    # Get positions text
    if args.data:
        print(f"Reading positions from file: {args.data}")
        with open(args.data) as f:
            positions_text = f.read()
    else:
        positions_text = read_from_stdin()

    # Generate HTML
    html = generate_html(positions_text)

    if args.dry_run:
        print("\n--- Generated HTML ---")
        print(html)
        return

    # Open Mail draft
    subject = build_subject(html)
    print(f"Subject: {subject[:100]}")
    open_mail_draft(RECIPIENT, subject, html)


if __name__ == "__main__":
    main()
