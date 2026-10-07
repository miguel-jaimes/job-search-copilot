# Merge new companies (dfw_companies.json) into companies.json.
#
#   python add_companies.py                  -> merges dfw_companies.json
#   python add_companies.py other_list.json  -> merges any other list
#
# Rules: a brand-new name is added. A name you already have is only replaced
# if your current entry has no working fetcher (e.g. Southwest was "custom").
# A backup of your old file is saved as companies.backup.json first.

import json
import shutil
import sys

SUPPORTED = {"greenhouse", "lever", "workday", "oracle", "ashby", "amazon"}

new_file = sys.argv[1] if len(sys.argv) > 1 else "dfw_companies.json"
with open("companies.json", encoding="utf-8") as f:
    companies = json.load(f)
with open(new_file, encoding="utf-8") as f:
    new = json.load(f)

shutil.copy("companies.json", "companies.backup.json")
index = {c["name"].lower(): i for i, c in enumerate(companies)}
added, replaced, skipped = [], [], []

for c in new:
    key = c["name"].lower()
    if key not in index:
        companies.append(c)
        added.append(c["name"])
        continue
    old = companies[index[key]]
    if old.get("ats_type") in SUPPORTED and old.get("ats_slug"):
        skipped.append(c["name"])          # you already have a working entry
    else:
        companies[index[key]] = c
        replaced.append(c["name"])

with open("companies.json", "w", encoding="utf-8") as f:
    f.write("[\n" + ",\n".join("  " + json.dumps(c) for c in companies) + "\n]\n")

print(f"Added {len(added)}, replaced {len(replaced)}, already had {len(skipped)}.")
for label, names in (("Replaced", replaced), ("Already had", skipped)):
    if names:
        print(f"  {label}: {', '.join(names)}")
print(f"companies.json now lists {len(companies)} companies (backup: companies.backup.json)")
