"""One-off: turns the hand-made labels in data/manual/ into data/labels.jsonl for main.py.

Each line in data/manual/*.txt is:  <u> <CODE>[+][?]|company|role
(u = row in data/work.json; + = also post to For You; verified.txt overrides the batch files).
Location, pay, deadline and apply link are pulled from the full message text.
"""
import glob
import json
import os
import re

from classifier import best_link

CODES = {"A": "ai_ds", "S": "software", "C": "cloud_snow", "M": "mass_hiring", "B": "consulting_biz",
         "K": "sales_marketing", "E": "core_engg", "O": "other_jobs", "P": "process_updates",
         "F": "selections", "V": "events", "N": "notices", "U": "unimportant"}
JOBS = {"ai_ds", "software", "cloud_snow", "mass_hiring", "consulting_biz", "sales_marketing", "core_engg",
        "other_jobs"}


def field(text, pattern):
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    v = re.split(r"\s{2,}|[•·|\n]|(?<=\w)\s+(?=[A-Z][a-z]+\s*:)", m.group(1).strip(" :-–"))[0]
    return v.strip(" .,:-–*")[:80] or None


def details(text):
    t = text.replace("*", "")
    info = {
        "location": field(t, r"(?:job\s+)?location\s*[:\-–]\s*([^\n]{2,80})"),
        "pay": field(t, r"(?:ctc|stipend|salary|package)[^:\n]{0,25}[:\-–]\s*([^\n]{2,60})")
               or field(t, r"((?:₹|inr|rs\.?)\s?[\d.,]+\s?(?:lpa|lakhs?|k)?(?:\s?(?:per month|/month|pm))?)"),
        "deadline": field(t, r"(?:deadline|last date|apply before|register (?:on or )?before|on or before)"
                             r"\s*[:\-–]?\s*([^\n]{4,60})"),
        "apply_link": best_link(t),
    }
    if info["pay"] and not re.search(r"\d", info["pay"]):
        info["pay"] = None  # "rs", "Not specified" and other non-amounts
    return info


def main():
    work = json.load(open("data/work.json", encoding="utf-8"))
    raw = {}
    for f in sorted(glob.glob("data/manual/b*.txt")) + ["data/manual/verified.txt"]:
        for line in open(f, encoding="utf-8"):
            if line.strip():
                u, rest = line.rstrip("\n").split(" ", 1)
                raw[int(u)] = rest.split("|")
    relevant = {}  # u -> reason, from the hand check against the resume
    if os.path.exists("data/manual/relevant.txt"):
        for line in open("data/manual/relevant.txt", encoding="utf-8"):
            if line.strip() and not line.startswith("#"):
                u, _, reason = line.partition("|")
                relevant[int(u)] = reason.strip() or None
    out = 0
    with open("data/labels.jsonl", "w", encoding="utf-8") as f:
        for w in work:
            parts = raw[w["u"]]
            code = parts[0]
            cat = CODES[code[0]]
            info = {"category": cat, "for_you": "+" in code and cat in JOBS,
                    "company": (parts[1] if len(parts) > 1 else "") or None,
                    "role": (parts[2] if len(parts) > 2 else "") or None}
            if cat in JOBS:
                info.update(details(w["text"]))
            if relevant:
                info["relevant"] = w["u"] in relevant
                info["reason"] = relevant.get(w["u"])
            for chat_id, msg_id in w["keys"]:  # duplicates share the label
                f.write(json.dumps({"chat_id": chat_id, "id": msg_id, **info}, ensure_ascii=False) + "\n")
                out += 1
    print(f"wrote {out} labels to data/labels.jsonl")


if __name__ == "__main__":
    main()
