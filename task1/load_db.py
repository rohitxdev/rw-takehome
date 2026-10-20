"""Build cbt_lab.db from the CSV exports, using schema.sql.

Usage:
    python load_db.py            # writes cbt_lab.db next to this file (replaces it)

Cleaning rules applied on the way in are recorded in the import_issue table.
"""

import csv
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).parent
DB_PATH = DATA / "cbt_lab.db"
WEEKS = [1, 2, 3, 4, 5, 6, 7, 8, 12]

# Known alternate or mistyped emails, mapped to the email on the application.
EMAIL_ALIASES = {
    "jfeld88@mailbox.example": "jonas.feld@orgbeta.example",
    "priya.nair@orgalpha.exmaple": "priya.nair@orgalpha.example",
}
# Payments that can't be matched by email: provider ref -> (application, purpose).
PAYMENT_OVERRIDES = {
    "ch_SYNB95C22": ("RW26-014", "contribution"),  # paid by Mateo's employer
}
TEST_EMAILS = {"test@test.example"}

STATUS = {
    "For Matching": "for_matching",
    "Waiting List": "waiting_list",
    "Declined": "declined",
    "In Facilitation Round": "in_facilitation",
    "Program Completed": "completed",
}
STATUS_RANK = ["declined", "waiting_list", "for_matching", "in_facilitation", "completed"]
DEPOSIT_OUTCOME = {
    "Pending": "pending",
    "Refunded": "refunded",
    "Not Met": "forfeited",
    "Donated": "donated",
}
PHQ = {"Not at all": 0, "Several days": 1, "More than half the days": 2, "Nearly every day": 3}


def norm_email(email):
    email = email.strip().lower()
    return EMAIL_ALIASES.get(email, email)


def read_csv(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(enumerate(csv.DictReader(f), start=2))  # (file line, row)


def cents(amount):
    return round(float(amount) * 100) if amount.strip() else None


def int_or_none(value):
    return int(value) if value.strip() else None


def presence(cell):
    try:
        return float(cell)
    except ValueError:
        return None


class Loader:
    def __init__(self, db):
        self.db = db
        self.person_by_email = {}
        self.app_by_person_cohort = {}   # (person_id, cohort name) -> application_id
        self.cohort_ids = {}

    def issue(self, source, line, message, application_id=None):
        self.db.execute(
            "INSERT INTO import_issue (source, line, application_id, message) VALUES (?, ?, ?, ?)",
            (source, line, application_id, message),
        )

    def cohort(self, name):
        if name not in self.cohort_ids:
            cur = self.db.execute("INSERT INTO cohort (name) VALUES (?)", (name,))
            self.cohort_ids[name] = cur.lastrowid
        return self.cohort_ids[name]

    def person(self, email, full_name, preferred_name=None, country=None, timezone=None):
        if email not in self.person_by_email:
            cur = self.db.execute(
                "INSERT INTO person (email, full_name, preferred_name, country, timezone) "
                "VALUES (?, ?, ?, ?, ?)",
                (email, full_name, preferred_name, country, timezone),
            )
            self.person_by_email[email] = cur.lastrowid
        return self.person_by_email[email]

    def application_for(self, email, year):
        """The application for this email in the cohort running in `year`."""
        person_id = self.person_by_email.get(norm_email(email))
        for (pid, cohort), app_id in self.app_by_person_cohort.items():
            if pid == person_id and cohort.split()[0] == str(year):
                return app_id
        return None

    # -- applications.csv ---------------------------------------------------

    def load_applications(self):
        src = "applications.csv"
        rows = read_csv(src)
        # Keep the most advanced application when a person applied twice to one cohort.
        best = {}
        for line, r in rows:
            key = (norm_email(r["Email Address"]), r["Cohort"])
            rank = STATUS_RANK.index(STATUS[r["Overall Status"]])
            if key in best:
                kept_line, kept, kept_rank = best[key]
                skipped = (line, r) if rank <= kept_rank else (kept_line, kept)
                if rank > kept_rank:
                    best[key] = (line, r, rank)
                self.issue(src, skipped[0], "duplicate application for the same cohort; skipped",
                           skipped[1]["Unique ID"] or None)
            else:
                best[key] = (line, r, rank)

        for line, r, _ in sorted(best.values(), key=lambda v: v[0]):
            email = norm_email(r["Email Address"])
            if email != r["Email Address"]:
                self.issue(src, line, f"email normalised from {r['Email Address']!r}", r["Unique ID"] or None)
            app_id = r["Unique ID"].strip()
            if not app_id:
                app_id = f"NOID-L{line}"
                self.issue(src, line, f"missing Unique ID; assigned {app_id}", app_id)
            person_id = self.person(email, r["Full Name"], r["Preferred Name"], r["Country"], r["Timezone"])
            self.db.execute(
                "INSERT INTO application VALUES (?, ?, ?, ?, ?, ?)",
                (app_id, person_id, self.cohort(r["Cohort"]),
                 r["Application Timestamp"].replace(".000Z", "Z"),
                 STATUS[r["Overall Status"]], 1 if r["Sharing Consent"] == "Yes" else 0),
            )
            self.app_by_person_cohort[(person_id, r["Cohort"])] = app_id

            outcome = DEPOSIT_OUTCOME.get(r["Motivational Deposit Status"])
            placed = STATUS[r["Overall Status"]] in ("in_facilitation", "completed")
            if outcome and placed and r["Motivational Deposit Amount"]:
                self.db.execute(
                    "INSERT INTO deposit (application_id, amount_minor, outcome) VALUES (?, ?, ?)",
                    (app_id, cents(r["Motivational Deposit Amount"]), outcome),
                )
            elif r["Motivational Deposit Amount"] and not placed:
                self.issue(src, line, "deposit amount on an applicant not placed in a group; no deposit created",
                           app_id)

    # -- attendance.csv -----------------------------------------------------

    def load_attendance(self, reflection_sessions):
        src = "attendance.csv"
        rows = read_csv(src)
        summer = "2026 Summer"

        # Groups and their facilitators.
        group_ids, held = {}, defaultdict(set)
        for line, r in rows:
            number = int(r["Group"])
            if number not in group_ids:
                name = r["Facilitator"]
                email = re.sub(r"[^a-z.]", "", name.lower().replace(" ", ".")) + "@facilitator.invalid"
                fac_id = self.person(email, name)
                self.db.execute("INSERT OR IGNORE INTO facilitator (person_id) VALUES (?)", (fac_id,))
                cur = self.db.execute(
                    'INSERT INTO "group" (group_number, cohort_id, facilitator_id) VALUES (?, ?, ?)',
                    (number, self.cohort(summer), fac_id),
                )
                group_ids[number] = cur.lastrowid
                self.issue(src, line, f"facilitator {name} has no email; placeholder {email}")
            for w in WEEKS:
                if presence(r[str(w)]) is not None:
                    held[number].add(w)

        # A session happened if anyone has attendance for it, or a member reflected on it.
        session_ids = {}
        for number, gid in group_ids.items():
            weeks = held[number] | reflection_sessions.get(number, set())
            for w in sorted(weeks):
                if w not in held[number]:
                    self.issue(src, None, f"Group {number} week {w}: reflections exist but no attendance recorded")
                cur = self.db.execute("INSERT INTO session (group_id, week_number) VALUES (?, ?)", (gid, w))
                session_ids[(number, w)] = cur.lastrowid

        # Memberships: one per attendance row, ended where the values stop.
        by_app = defaultdict(list)
        for line, r in rows:
            app_id = self.application_for(r["Email"], 2026)
            if not app_id:
                self.issue(src, line, f"no 2026 application for {r['Email']}; row skipped")
                continue
            if norm_email(r["Email"]) != r["Email"]:
                self.issue(src, line, f"email {r['Email']} mapped to {norm_email(r['Email'])}", app_id)
            by_app[app_id].append((line, r))

        for app_id, app_rows in by_app.items():
            spans = []
            for line, r in app_rows:
                values = {w: presence(r[str(w)]) for w in WEEKS}
                filled = [w for w, v in values.items() if v is not None]
                text = [w for w in WEEKS if r[str(w)].strip() and values[w] is None]
                spans.append((min(filled), max(filled), text, line, r, values))
            spans.sort()
            for i, (start, last, text, line, r, values) in enumerate(spans):
                number = int(r["Group"])
                later_held = [w for w in held[number] if w > last]
                end, reason = None, None
                if text:
                    end, reason = last, "dropped"
                    self.issue(src, line, f"week {text[0]} says {r[str(text[0])]!r}; recorded as dropped after week {last}",
                               app_id)
                elif later_held:
                    end = last
                    reason = "moved" if i + 1 < len(spans) else "dropped"
                cur = self.db.execute(
                    "INSERT INTO group_membership (application_id, cohort_id, group_id, start_week, end_week, end_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (app_id, self.cohort(summer), group_ids[number], start, end, reason),
                )
                membership_id = cur.lastrowid
                for w, v in values.items():
                    if v is not None:
                        self.db.execute(
                            "INSERT INTO attendance VALUES (?, ?, ?, ?)",
                            (membership_id, session_ids[(number, w)], group_ids[number], v),
                        )
                if r["Sessions Attended"] and float(r["Sessions Attended"]) != sum(v for v in values.values() if v):
                    self.issue(src, line, f"Sessions Attended says {r['Sessions Attended']} but the cells add up to "
                                          f"{sum(v for v in values.values() if v):g}", app_id)

    # -- weekly_reflections.csv ---------------------------------------------

    def reflection_rows(self):
        """Cleaned reflection rows plus the (group, week) sessions they prove happened."""
        src = "weekly_reflections.csv"
        cleaned, sessions = [], defaultdict(set)
        for line, r in read_csv(src):
            email = norm_email(r["Email Address"])
            if email in TEST_EMAILS:
                self.issue(src, line, f"test submission from {email}; skipped")
                continue
            submitted = datetime.strptime(r["Timestamp"], "%m/%d/%Y %H:%M:%S")
            app_id = self.application_for(email, submitted.year)
            if not app_id:
                self.issue(src, line, f"no application for {email} in {submitted.year}; skipped")
                continue
            raw = r["Email Address"].strip().lower()
            if raw != email:
                self.issue(src, line, f"email {raw} mapped to {email}", app_id)
            if not r["Week Number"].strip():
                self.issue(src, line, "no week number (empty submission); skipped", app_id)
                continue
            week = int(r["Week Number"])
            if r["Group Number"].strip() and submitted.year == 2026:
                sessions[int(r["Group Number"])].add(week)
            cleaned.append((line, r, app_id, week, submitted))
        return cleaned, sessions

    def load_reflections(self, cleaned):
        src = "weekly_reflections.csv"
        cols = list(cleaned[0][1].keys())
        q = cols[3:7]
        seen = {}
        for line, r, app_id, week, submitted in cleaned:
            if (app_id, week) in seen:
                self.issue(src, line, f"second reflection for week {week} (first on line {seen[(app_id, week)]}); "
                                      "skipped, may be a mislabelled week", app_id)
                continue
            seen[(app_id, week)] = line
            self.db.execute(
                "INSERT INTO weekly_reflection (application_id, week_number, submitted_at, phq_nervous, "
                "phq_worrying, phq_interest, phq_down, difficulty, attended, engaging, facilitator, "
                "felt_supported, liked, wished, home_practice) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (app_id, week, submitted.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 *(PHQ.get(r[c]) for c in q),
                 r[cols[7]] or None,
                 {"Yes": 1, "No": 0}.get(r[cols[8]]),
                 int_or_none(r[cols[9]]), int_or_none(r[cols[10]]), int_or_none(r[cols[11]]),
                 r[cols[12]] or None, r[cols[13]] or None, int_or_none(r[cols[14]])),
            )

    # -- payments.csv -------------------------------------------------------

    def wise_application(self, description, year):
        """Match 'MD Noor R.' / 'MD refund Noor R.' to an application by first name + last initial."""
        m = re.search(r"(\w+) (\w)\.$", description)
        if not m:
            return None
        first, initial = m.groups()
        for (pid, cohort), app_id in self.app_by_person_cohort.items():
            name = self.db.execute("SELECT full_name FROM person WHERE person_id = ?", (pid,)).fetchone()[0]
            parts = name.split()
            if parts[0] == first and parts[-1][0] == initial and cohort.split()[0] == str(year):
                return app_id
        return None

    def load_payments(self):
        src = "payments.csv"
        wise_charges = {}
        rows = read_csv(src)
        # Charges before refunds, so a Wise refund can point at its charge.
        rows.sort(key=lambda lr: float(lr[1]["Amount"]) < 0)
        for line, r in rows:
            ref, desc = r["id"], r["Description"]
            if r["Channel"].startswith("Wise"):
                created = datetime.strptime(r["Created (UTC)"], "%d/%m/%Y")
                app_id = self.wise_application(desc, created.year)
                provider, status = "wise", "succeeded"
                self.issue(src, line, f"manual Wise entry: matched {desc!r} to {app_id} by name; "
                                      "status blank, assumed succeeded; fee unknown", app_id)
            else:
                created = datetime.strptime(r["Created (UTC)"], "%Y-%m-%d %H:%M")
                app_id = self.application_for(r["Customer Email"], created.year)
                provider = "stripe"
                status = "failed" if r["Status"] == "Failed" else "succeeded"

            # 'MD ...' in manual Wise entries = motivational deposit.
            is_deposit = "deposit" in desc.lower() or desc.startswith("MD ")
            purpose = "deposit" if is_deposit else (
                "contribution" if "contribution" in desc.lower() else "combined")
            if ref in PAYMENT_OVERRIDES:
                app_id, purpose = PAYMENT_OVERRIDES[ref]
                self.issue(src, line, f"payer {r['Customer Name']} matched to {app_id} by hand", app_id)
            if not app_id:
                self.issue(src, line, f"payment {ref} matches no application; stored unmatched")
            if purpose == "combined":
                self.issue(src, line, f"{desc!r}: deposit and contribution not separated", app_id)

            amount = cents(r["Amount"])
            kind = "refund" if amount < 0 else "charge"
            refund_of = wise_charges.get(app_id) if kind == "refund" else None
            cur = self.db.execute(
                "INSERT INTO payment (provider, provider_ref, application_id, kind, refund_of, purpose, "
                "amount_minor, currency, fee_minor, status, created_at, payer_name, payer_email) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (provider, ref, app_id, kind, refund_of, purpose, abs(amount), r["Currency"],
                 cents(r["Fee"]), status, created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 r["Customer Name"] or None, r["Customer Email"] or None),
            )
            if provider == "wise" and kind == "charge":
                wise_charges[app_id] = cur.lastrowid

            # Stripe records a refund on the charge; store it as its own row.
            refunded = cents(r["Amount Refunded"] or "0")
            if refunded:
                self.db.execute(
                    "INSERT INTO payment (provider, provider_ref, application_id, kind, refund_of, purpose, "
                    "amount_minor, currency, status, created_at) VALUES (?, ?, ?, 'refund', ?, ?, ?, ?, 'succeeded', NULL)",
                    (provider, f"{ref}-refund", app_id, cur.lastrowid, purpose, refunded, r["Currency"]),
                )


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()
    db = sqlite3.connect(DB_PATH)
    db.executescript((DATA / "schema.sql").read_text(encoding="utf-8"))
    db.execute("PRAGMA foreign_keys = ON")
    loader = Loader(db)
    with db:
        loader.load_applications()
        cleaned, reflection_sessions = loader.reflection_rows()
        loader.load_attendance(reflection_sessions)
        loader.load_reflections(cleaned)
        loader.load_payments()
    for table in ["person", "application", "group_membership", "attendance", "weekly_reflection",
                  "deposit", "payment", "import_issue"]:
        print(f"{table:18} {db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:4} rows")
    print(f"Wrote {DB_PATH}")


if __name__ == "__main__":
    main()
