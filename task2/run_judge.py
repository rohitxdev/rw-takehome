"""Run a judge prompt over exchanges.csv and compare it with the clinician's verdicts.

Usage:
    pip install anthropic
    export ANTHROPIC_API_KEY=...
    python run_judge.py                                    # judge_v2.txt -> judge_v2_results.csv
    python run_judge.py --model claude-sonnet-5-5          # same model as judge v1, for a fair comparison
    python run_judge.py --prompt judge_v1.txt --out judge_v1_rerun.csv
    python run_judge.py --dry-run                          # print the first rendered prompt, no API calls

A call that fails, is refused or returns unparseable JSON is recorded as ERROR and
counted as a miss, so a broken judge never looks better than it is.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
LEVELS = ["NO", "ROUTINE", "URGENT"]


def render(template, row):
    # str.format would choke on the JSON braces in the prompt
    return template.replace("{bot}", row["Bot"]).replace("{user_message}", row["User message"])


def parse_verdict(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON in reply: {text!r}")
    data = json.loads(match.group(0))
    verdict = str(data.get("verdict", "")).upper()
    if verdict not in LEVELS:
        raise ValueError(f"bad verdict: {data!r}")
    return verdict, str(data.get("confidence", "")).upper(), data.get("reason", "")


def judge(client, model, prompt):
    import anthropic

    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=4000,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        return "ERROR", "", f"API error: {e}", model
    if response.stop_reason == "refusal":
        return "ERROR", "", "refused", response.model
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        return (*parse_verdict(text), response.model)
    except ValueError as e:
        return "ERROR", "", str(e), response.model


def summarise(results):
    total = len(results)
    matches = [r for r in results if r["Match"] == "Yes"]
    print(f"\nAgreement with clinician: {len(matches)}/{total} ({len(matches) / total:.0%})")

    for level in LEVELS:
        rows = [r for r in results if r["Clinician"] == level]
        hit = sum(r["Judge verdict"] == level for r in rows)
        print(f"  {level:<8} recall: {hit}/{len(rows)}")

    rank = {level: i for i, level in enumerate(LEVELS)}
    under = [r for r in results if r["Judge verdict"] in rank and rank[r["Judge verdict"]] < rank[r["Clinician"]]]
    over = [r for r in results if r["Judge verdict"] in rank and rank[r["Judge verdict"]] > rank[r["Clinician"]]]
    errors = [r for r in results if r["Judge verdict"] == "ERROR"]
    print(f"  Under-called: {len(under)}   Over-called: {len(over)}   Errors: {len(errors)}")

    print("\nBy confidence (agreement):")
    for conf in ["HIGH", "MEDIUM", "LOW"]:
        rows = [r for r in results if r["Confidence"] == conf]
        if rows:
            ok = sum(r["Match"] == "Yes" for r in rows)
            print(f"  {conf:<7} {ok}/{len(rows)}")

    mismatches = [r for r in results if r["Match"] == "No"]
    if mismatches:
        print("\nMismatches:")
        for r in mismatches:
            print(f"  {r['ID']}: clinician={r['Clinician']} judge={r['Judge verdict']} "
                  f"({r['Confidence'] or '-'}) {r['Judge reason']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompt", default="judge_v2.txt")
    parser.add_argument("--out", default=None, help="default: <prompt name>_results.csv")
    parser.add_argument("--exchanges", default="exchanges.csv")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    template = (HERE / args.prompt).read_text(encoding="utf-8")
    with open(HERE / args.exchanges, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    if args.dry_run:
        print(render(template, rows[0]))
        print(f"\n[{len(rows)} rows would be judged with {args.model}]")
        return

    import anthropic

    client = anthropic.Anthropic()
    results = []
    for row in rows:
        verdict, confidence, reason, served_by = judge(client, args.model, render(template, row))
        clinician = row["Human needed (clinician)"].strip().upper()
        results.append({
            "ID": row["ID"],
            "Bot": row["Bot"],
            "Judge verdict": verdict,
            "Confidence": confidence,
            "Judge reason": reason,
            "Clinician": clinician,
            "Match": "Yes" if verdict == clinician else "No",
            "Model": served_by,
        })
        print(f"{row['ID']}: {verdict} ({confidence or '-'}) clinician={clinician}", file=sys.stderr)

    out = HERE / (args.out or f"{Path(args.prompt).stem}_results.csv")
    with open(out, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved {out.name}")
    summarise(results)


if __name__ == "__main__":
    main()
