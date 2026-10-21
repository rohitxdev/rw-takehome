"""Flag bot replies that state a clock time or timezone.

Neither bot can see a participant's group, calendar or the current time, so any
specific time in a reply ("18:00 UTC", "6pm") was invented or pulled from a
knowledge base line that may not apply to this person. Durations and weekdays are
deliberately not matched: "20 minutes a day" and "this week" are normal programme
content and would flag replies the clinician rated OK.

Usage:
    python check_clock_times.py
"""

import csv
import re
from pathlib import Path

HERE = Path(__file__).parent

CLOCK_TIME = re.compile(
    r"\b\d{1,2}[:.]\d{2}\b"                 # 18:00, 6.30
    r"|\b\d{1,2}\s?(?:am|pm)\b"             # 6pm, 10 am
    r"|\b(?:UTC|GMT|BST|CET|CEST|EST|EDT|PST|PDT)\b",
    re.IGNORECASE,
)


def check(reply):
    return [m.group(0) for m in CLOCK_TIME.finditer(reply)]


def main():
    with open(HERE / "exchanges.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    flagged = []
    for row in rows:
        hits = check(row["Bot reply"])
        if hits:
            flagged.append(row)
            print(f"FLAG {row['ID']}  clinician={row['Clinician verdict']}  matched={hits}")

    not_ok = [r for r in rows if r["Clinician verdict"] != "OK"]
    true_pos = [r for r in flagged if r["Clinician verdict"] != "OK"]
    print()
    print(f"Flagged {len(flagged)} of {len(rows)} exchanges")
    print(f"True positives: {len(true_pos)}  False positives: {len(flagged) - len(true_pos)}")
    print(f"Caught {len(true_pos)} of {len(not_ok)} exchanges the clinician rated Not OK or Partly OK")


if __name__ == "__main__":
    main()
