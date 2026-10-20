"""Print an applicant's motivational deposit refund status from cbt_lab.db.

Usage:
    python load_db.py                    # build the database from the CSVs first
    python refund.py RW26-010            # one applicant
    python refund.py RW26-010 RW26-021   # several
    python refund.py liang.wei@orgbeta.example   # by email
    python refund.py                     # everyone (latest application per person)
    python refund.py --csv refund_decisions.csv  # same, one CSV row per person

Refund rules:
    - attended at least 8 of the 9 sessions (weeks 1-8 and 12; a half session counts 0.5)
    - submitted all 9 weekly reflections
    - completed both wellbeing trackers (not in the exports, assumed done)
The voluntary contribution is never refunded.
"""

import csv
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "cbt_lab.db"
WEEKS = [1, 2, 3, 4, 5, 6, 7, 8, 12]
MIN_SESSIONS = 8

# Outcome kinds
DUE, DONATED, NOT_ELIGIBLE, UNDECIDED, CHECK, NO_DEPOSIT, NO_DATA = (
    "due", "donated", "not_eligible", "undecided", "check", "no_deposit", "no_data")


# Deposit outcomes as they appear in Airtable's Motivational Deposit Status.
AIRTABLE_LABEL = {"pending": "Pending", "refunded": "Refunded", "forfeited": "Not Met", "donated": "Donated"}


def weeks_text(weeks):
    return ", ".join(map(str, weeks))


def find_applications(db, keys):
    if not keys:
        return people(db)
    found = []
    for key in keys:
        rows = db.execute(
            "SELECT a.application_id FROM application a JOIN person p USING (person_id) "
            "WHERE a.application_id = ? OR p.email = lower(trim(?))",
            (key, key),
        ).fetchall()
        if not rows:
            print(f"{key}: no application with this ID or email\n")
        found.extend(a for (a,) in rows)
    return found


def people(db):
    """One application per person: their latest. Duplicates were dropped by load_db.py."""
    return [a for (a,) in db.execute(
        "SELECT a.application_id FROM application a JOIN person p USING (person_id) "
        "WHERE a.applied_at = (SELECT MAX(b.applied_at) FROM application b WHERE b.person_id = a.person_id) "
        "ORDER BY p.full_name")]


def unmatched_deposits(db):
    """Deposits paid by someone with no application (payer name, email, amount, currency, date)."""
    return db.execute(
        "SELECT payer_name, payer_email, amount_minor, currency, provider, created_at FROM payment "
        "WHERE application_id IS NULL AND kind = 'charge' AND status = 'succeeded' "
        "AND purpose IN ('deposit', 'combined') ORDER BY payer_name"
    ).fetchall()


def unmatched_note(payment):
    name, email, amount, currency, provider, created = payment
    return (f"Missing: an application. Paid a {currency} {amount / 100:g} deposit ({provider.title()}, "
            f"{created[:10]}, {email}) but has no application in Airtable. Find the application, "
            "or refund the deposit.")


def session_weeks(db, app_id):
    """Per programme week: presence if recorded, None if the session happened
    but no attendance was recorded. Weeks missing from the dict have no session yet."""
    rows = db.execute(
        """
        SELECT s.week_number, a.presence
        FROM group_membership gm
        JOIN session s ON s.group_id = gm.group_id
                      AND s.week_number >= gm.start_week
                      AND (gm.end_week IS NULL OR s.week_number <= gm.end_week)
        LEFT JOIN attendance a ON a.membership_id = gm.membership_id AND a.session_id = s.session_id
        WHERE gm.application_id = ?
        """,
        (app_id,),
    ).fetchall()
    weeks = {}
    for week, value in rows:
        if value is not None or week not in weeks:
            weeks[week] = value
    return weeks


def facts(db, app_id):
    """Everything the refund decision and its explanation need."""
    f = {"app_id": app_id}
    f["name"], f["cohort"], f["status"] = db.execute(
        "SELECT p.full_name, c.name, a.status FROM application a "
        "JOIN person p USING (person_id) JOIN cohort c USING (cohort_id) WHERE a.application_id = ?",
        (app_id,),
    ).fetchone()
    f["deposit"] = db.execute(
        "SELECT amount_minor, currency, outcome FROM deposit WHERE application_id = ?", (app_id,)
    ).fetchone()
    f["memberships"] = db.execute(
        "SELECT g.group_number, gm.start_week, gm.end_week, gm.end_reason FROM group_membership gm "
        'JOIN "group" g USING (group_id) WHERE gm.application_id = ? ORDER BY gm.start_week',
        (app_id,),
    ).fetchall()
    f["issues"] = db.execute(
        "SELECT source, line, message FROM import_issue WHERE application_id = ? ORDER BY issue_id", (app_id,)
    ).fetchall()
    f["payments"] = db.execute(
        "SELECT kind, purpose, amount_minor, currency, status, payer_name FROM payment WHERE application_id = ?",
        (app_id,),
    ).fetchall()
    f["paid"] = sum(p[2] for p in f["payments"]
                    if p[0] == "charge" and p[4] == "succeeded" and p[1] in ("deposit", "combined"))
    f["refunded"] = sum(p[2] for p in f["payments"] if p[0] == "refund" and p[1] in ("deposit", "combined"))

    weeks = session_weeks(db, app_id)
    dropped = bool(f["memberships"]) and f["memberships"][-1][3] == "dropped"
    f["weeks"] = weeks
    f["attended"] = sum(v for v in weeks.values() if v)
    f["unrecorded"] = [w for w, v in sorted(weeks.items()) if v is None]
    f["not_held"] = [w for w in WEEKS if w not in weeks and not dropped] if weeks else []
    f["open_weeks"] = sorted(f["unrecorded"] + f["not_held"])
    f["reflected"] = {w for (w,) in db.execute(
        "SELECT week_number FROM weekly_reflection WHERE application_id = ?", (app_id,))} & set(WEEKS)
    f["missing"] = [w for w in WEEKS if w not in f["reflected"] and w not in f["not_held"]]
    f["duplicate_reflections"] = [(line, msg) for src, line, msg in f["issues"]
                                  if msg.startswith("second reflection")]
    f["kind"] = decide(f)
    return f


def decide(f):
    if not f["deposit"]:
        return NO_DEPOSIT
    if not f["weeks"]:
        return NO_DATA
    max_attended = f["attended"] + len(f["open_weeks"])
    if f["attended"] >= MIN_SESSIONS and not f["missing"] and not f["not_held"]:
        return DONATED if f["deposit"][2] == "donated" else DUE
    if max_attended >= MIN_SESSIONS and f["missing"] and len(f["duplicate_reflections"]) >= len(f["missing"]):
        # Missing weeks may just be duplicates with the wrong week label.
        return CHECK
    if max_attended < MIN_SESSIONS or f["missing"] or not f["open_weeks"]:
        return NOT_ELIGIBLE
    return UNDECIDED


def money(f):
    amount, currency, _ = f["deposit"]
    return f"{currency} {amount / 100:g}"


# -- Text report -------------------------------------------------------------

def report(f):
    print(f"{f['app_id']}  {f['name']}  ({f['cohort']}, {f['status']})")
    kind = f["kind"]
    if kind == NO_DEPOSIT:
        print("  Result:       NO DEPOSIT\n")
        return
    print(f"  Deposit:      {money(f)}")
    if kind == NO_DATA:
        print("  Result:       CANNOT ASSESS: no attendance data\n")
        return
    print(f"  Attendance:   {f['attended']:g} / {len(WEEKS)} sessions (need {MIN_SESSIONS})"
          + (f"; not recorded for week {weeks_text(f['unrecorded'])}" if f["unrecorded"] else "")
          + (f"; not held yet: week {weeks_text(f['not_held'])}" if f["not_held"] else ""))
    print(f"  Reflections:  {len(f['reflected'])} / {len(WEEKS)}"
          + (f", missing week {weeks_text(f['missing'])}" if f["missing"] else ""))
    print("  Wellbeing:    assumed complete (not in exports)")
    print(f"  Recorded:     deposit outcome '{f['deposit'][2]}'; "
          f"paid {f['paid'] / 100:g}, refunded {f['refunded'] / 100:g}")
    result = {
        DUE: f"REFUND DUE: {money(f)}",
        DONATED: f"ELIGIBLE, DONATED: participant chose to donate {money(f)}",
        NOT_ELIGIBLE: "NOT ELIGIBLE (deposit kept)",
        UNDECIDED: f"UNDECIDED: depends on attendance for week {weeks_text(f['open_weeks'])}",
        CHECK: "CHECK: missing reflection may be a mislabelled duplicate",
    }[kind]
    print(f"  Result:       {result}")
    for w in record_conflicts(f):
        print(f"  ! {w}")
    for src, line, msg in f["issues"]:
        print(f"  ! import: {src}" + (f" line {line}" if line else "") + f": {msg}")
    print()


def record_conflicts(f):
    outcome = f["deposit"][2]
    eligible = f["kind"] in (DUE, DONATED)
    out = []
    if f["kind"] == DUE and not f["refunded"]:
        out.append("refund is due but no refund has been paid")
    if f["refunded"] and not eligible:
        out.append("a refund was paid, but the rules say the deposit is kept")
    if f["refunded"] and outcome != "refunded":
        out.append(f"a refund was paid but the deposit outcome says '{outcome}'")
    if outcome == "refunded" and not f["refunded"]:
        out.append("deposit outcome says 'refunded' but no refund payment exists")
    return out


# -- CSV ---------------------------------------------------------------------

def explain(f):
    """Short reason for anything that isn't a plain application of the rules."""
    kind = f["kind"]
    notes = []

    if kind == NO_DEPOSIT:
        payers = {p[5] for p in f["payments"] if p[5] and p[5] != f["name"]}
        not_placed = {"waiting_list": "On the waiting list", "declined": "Declined the place",
                      "for_matching": "Not yet matched to a group"}
        if f["status"] in not_placed:
            notes.append(f"{not_placed[f['status']]}, so no deposit was taken")
        else:
            notes.append("No deposit was taken"
                         + (f"; the programme was paid by {', '.join(payers)}" if payers else ""))
    elif kind == DONATED:
        notes.append("Met the refund conditions but chose to donate the deposit")
    elif kind == UNDECIDED:
        need = MIN_SESSIONS - f["attended"]
        notes.append(f"Missing: attendance for week {weeks_text(f['open_weeks'])} (not recorded). "
                     f"Has {f['attended']:g} sessions and needs {need:g} more")
    elif kind == CHECK:
        line, msg = f["duplicate_reflections"][0]
        labelled = re.search(r"week (\d+)", msg).group(1)
        notes.append(f"Missing: confirmation of the week {weeks_text(f['missing'])} reflection. Attendance is "
                     f"{f['attended']:g}/9, but the reflection on weekly_reflections.csv line {line} is labelled week "
                     f"{labelled} a second time; if it is really week {weeks_text(f['missing'])}, refund")
    elif kind == NOT_ELIGIBLE:
        if f["memberships"] and f["memberships"][-1][3] == "dropped":
            notes.append(f"Dropped out after week {f['memberships'][-1][2]} "
                         f"(attended {f['attended']:g} of 9 sessions)")
        else:
            if f["attended"] < MIN_SESSIONS:
                text = f"Attended {f['attended']:g} of 9 sessions (needs 8)"
                if f["open_weeks"]:
                    text += f"; week {weeks_text(f['open_weeks'])} is not recorded but can't change the result"
                notes.append(text)
            if f["missing"]:
                notes.append(f"No reflection for week {weeks_text(f['missing'])}")

    if kind in (DUE, DONATED):
        if f["unrecorded"]:
            notes.append(f"Week {weeks_text(f['unrecorded'])} attendance not recorded, but already has "
                         f"{f['attended']:g} sessions")
        moves = [m for m in f["memberships"] if m[3] == "moved"]
        for i, m in enumerate(moves):
            nxt = f["memberships"][f["memberships"].index(m) + 1]
            notes.append(f"Moved from Group {m[0]} to Group {nxt[0]} in week {nxt[1]}; "
                         "attendance counted across both groups")
        if f["status"] == "in_facilitation":
            notes.append("Airtable status is still 'In Facilitation Round'")

    if f["deposit"]:
        outcome = f["deposit"][2]
        airtable = AIRTABLE_LABEL[outcome]
        if f["refunded"] and outcome != "refunded":
            notes.append(f"Already refunded in Stripe; Airtable still says '{airtable}'")
        if outcome == "refunded" and not f["refunded"]:
            notes.append("Airtable says 'Refunded', but no refund was ever paid")
        if not f["paid"]:
            notes.append("No deposit payment on record")
        if any(p[1] == "combined" for p in f["payments"]):
            combined = next(p for p in f["payments"] if p[1] == "combined")
            notes.append(f"Paid one combined {combined[3]} {combined[2] / 100:g} charge, so the deposit "
                         "part is not separated")

    aliases = sorted({m.group(1) for _, _, msg in f["issues"]
                      if (m := re.match(r"email (\S+) mapped to", msg))})
    if aliases:
        notes.append(f"Records under {', '.join(aliases)} matched to this participant")
    for _, _, msg in f["issues"]:
        if msg.startswith("missing Unique ID"):
            notes.append("No Unique ID in Airtable")
        if msg.startswith("Sessions Attended says"):
            notes.append("Attendance sheet total is wrong (" + msg.split("Sessions Attended ")[1] + ")")

    return ". ".join(n for n in notes if n) + ("." if notes else "")


def csv_row(f):
    kind = f["kind"]
    decision = {DUE: "refund", DONATED: "no refund", NOT_ELIGIBLE: "no refund", NO_DEPOSIT: "no refund",
                UNDECIDED: "not yet", CHECK: "not yet", NO_DATA: "not yet"}[kind]
    amount = f["deposit"][0] / 100 if kind == DUE else 0
    return {
        "Participant": f["name"],
        "Decision (refund, no refund or not yet)": decision,
        "Amount (USD)": f"{amount:g}",
        "Why, or what is missing (only if not straightforward)": explain(f),
    }


def write_csv(db, path):
    rows = [csv_row(facts(db, a)) for a in people(db)]
    for p in unmatched_deposits(db):
        rows.append({
            "Participant": p[0],
            "Decision (refund, no refund or not yet)": "not yet",
            "Amount (USD)": "0",
            "Why, or what is missing (only if not straightforward)": unmatched_note(p),
        })
    rows.sort(key=lambda r: r["Participant"])
    with open(path, "w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {path}")


def main(args):
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH.name} not found. Run: python load_db.py")
    db = sqlite3.connect(DB_PATH)
    if args[:1] == ["--csv"]:
        write_csv(db, args[1] if len(args) > 1 else "refund_decisions.csv")
        return
    for app_id in find_applications(db, args):
        report(facts(db, app_id))
    if not args:
        for p in unmatched_deposits(db):
            print(f"(no application)  {p[0]}")
            print(f"  Result:       NOT YET: {unmatched_note(p)}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
