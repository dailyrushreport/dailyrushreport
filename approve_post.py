#!/usr/bin/env python3
"""Mark one social draft as approved and print its final text for copy/paste.

Usage:  python3 approve_post.py social_drafts/2026-10-23-0640.json x
        python3 approve_post.py social_drafts/2026-10-23-0640.json instagram

This script NEVER posts anything. There is no posting code and no API keys anywhere in this project.
You copy the printed text and post it yourself.
"""
import json, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import social_md, atomic_write, x_length, SOCIAL_DIR

def main():
    if len(sys.argv) != 3 or sys.argv[2] not in ("x", "instagram"):
        sys.exit(__doc__)
    path = sys.argv[1]
    if path.endswith(".md"):
        path = path[:-3] + ".json"
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    dr = d["drafts"][sys.argv[2]]
    if sys.argv[2] == "x":
        n = x_length(dr["text"], [d["lead"]["url"]])
        if n > dr["limit"]:
            sys.exit(f"X draft is {n} characters (limit {dr['limit']}). Edit the text in {path} first.")
        dr["weighted_length"] = n
    dr["status"] = "approved"
    dr["approved_at"] = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d %H:%M CT")
    atomic_write(path, json.dumps(d, indent=2, ensure_ascii=False))
    md = social_md(d)
    atomic_write(path[:-5] + ".md", md)
    newest = sorted(f for f in os.listdir(SOCIAL_DIR) if f.endswith(".json"))[-1]
    if os.path.basename(path) == newest:
        atomic_write(os.path.join(SOCIAL_DIR, "latest.md"), md)
    print(f"Approved {sys.argv[2]} draft ({path}). NOT posted - copy the text below and post it yourself:\n")
    print("-" * 60)
    print(dr["text"])
    print("-" * 60)

if __name__ == "__main__":
    main()
