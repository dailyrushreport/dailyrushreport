#!/usr/bin/env python3
"""
Daily Rush Report builder.

Fetches the RSS/Atom feeds listed in feeds.json, applies editor overrides from
picks.json, dedupes, picks a fresh lead, and writes a static index.html.

Safe to run on a schedule (cron / GitHub Actions) with no manual steps:
  * a feed that fails falls back to its last good copy in .state/cache/
  * stale items are dropped (per-feed max_age_hours)
  * the same lead is not reused for the next N builds (settings.avoid_repeat_leads)
  * output is written atomically; if too few stories are found, the previous
    index.html is kept and the script exits non-zero
Only the standard library is used.

Usage:  python3 build.py [--out index.html] [--offline]
"""
import argparse, concurrent.futures as cf, email.utils, hashlib, html, json, os, re, sys, tempfile, time
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, ".state")
CACHE_DIR = os.path.join(STATE_DIR, "cache")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
ARCHIVE_DIR = os.path.join(ROOT, "archive")
TZ = ZoneInfo("America/Chicago")
NOW = datetime.now(timezone.utc)
ATOM = "{http://www.w3.org/2005/Atom}"

def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, file=sys.stderr)

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        log(f"ERROR: {os.path.basename(path)} is not valid JSON ({e}); ignoring it")
        return default

def atomic_write(path, text):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)

# ---------------------------------------------------------------- fetching
def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)

def clean_text(s):
    s = html.unescape(re.sub(r"<[^>]+>", "", s or ""))
    return re.sub(r"\s+", " ", s).strip()

def canonical_url(u):
    p = urllib.parse.urlsplit(u.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if not k.lower().startswith(("utm_", "cmpid", "taid", "at_", "dicbo", "traffic_source"))]
    return urllib.parse.urlunsplit((p.scheme or "https", p.netloc.lower(), p.path, urllib.parse.urlencode(q), ""))

def parse_feed(raw, feed):
    root = ET.fromstring(raw)
    out = []
    for it in root.iter():
        tag = it.tag
        if tag == "item":
            title = it.findtext("title")
            link = it.findtext("link") or ""
            if not link.strip():
                g = it.findtext("guid") or ""
                link = g if g.startswith("http") else ""
            date = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")
        elif tag == ATOM + "entry":
            title = it.findtext(ATOM + "title")
            link = ""
            for l in it.findall(ATOM + "link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", ""); break
            date = it.findtext(ATOM + "published") or it.findtext(ATOM + "updated")
        else:
            continue
        title = clean_text(title)
        if not title or not link.strip().startswith("http"):
            continue
        out.append({"title": title, "url": canonical_url(link), "source": feed["name"],
                    "published": (parse_date(date) or NOW).isoformat(),
                    "column": feed.get("column", 1), "lead_ok": bool(feed.get("lead"))})
    return out

def fetch_feed(feed, settings, offline=False):
    key = hashlib.sha1(feed["url"].encode()).hexdigest()[:16]
    cache = os.path.join(CACHE_DIR, key + ".json")
    if not offline:
        try:
            req = urllib.request.Request(feed["url"], headers={"User-Agent": settings["user_agent"]})
            raw = urllib.request.urlopen(req, timeout=settings.get("timeout_seconds", 10)).read()
            items = parse_feed(raw, feed)
            if not items:
                raise ValueError("no items")
            atomic_write(cache, json.dumps({"fetched": NOW.isoformat(), "items": items}))
            return feed["name"], "ok", items
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:80]}"
    else:
        err = "offline"
    c = load_json(cache, None)
    if c:
        return feed["name"], f"FAILED ({err}) - used cache from {c['fetched'][:16]}Z", c["items"]
    return feed["name"], f"FAILED ({err}) - no cache", []

# ---------------------------------------------------------------- dedupe
STOP = set(("a an the and or of to in on for with at by from as is are was were be been it its this that after "
            "over into about says say said new how why what who will vs report reports live latest news his her "
            "their they you your just more than not out up has have had can could would may might time times different "
            "day days week year years first last one back make makes made get gets got here now still").split())
SYN = {"doj": "justice", "department": "", "investigation": "investigat", "investigates": "investigat",
       "investigating": "investigat", "investigate": "investigat"}

def stem(w):
    w = SYN.get(w, w)
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 5 and w.endswith(suf):
            return w[: -len(suf)]
    return w

def tokens(t):
    ws = re.findall(r"[a-z0-9]+", t.lower().replace("’", "'").replace("'s", ""))
    return {s for s in (stem(w) for w in ws if w not in STOP and len(w) > 2) if s}

DF = {}
def build_df(items):
    DF.clear()
    for i in items:
        for t in i["_tok"]:
            DF[t] = DF.get(t, 0) + 1

def similar(a, b):
    """IDF-weighted overlap: two headlines are the same story if they share enough rare words."""
    import math
    ta, tb = a["_tok"], b["_tok"]
    if not ta or not tb:
        return False
    n = max(len(DF), 1)
    w = lambda t: math.log(1 + n / DF.get(t, 1))
    shared = ta & tb
    rare = [t for t in shared if DF.get(t, 1) <= 6]
    score = sum(w(t) for t in shared) / min(sum(w(t) for t in ta), sum(w(t) for t in tb))
    return (len(rare) >= 2 and score >= 0.33) or score >= 0.6

def cluster(items):
    """Group near-duplicate stories. Returns list of clusters (lists), each sorted newest first."""
    build_df(items)
    clusters = []
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        for c in clusters:
            if any(similar(it, o) for o in c):
                c.append(it); break
        else:
            clusters.append([it])
    return clusters

# ---------------------------------------------------------------- picks
def active(entry):
    exp = entry.get("expires") if isinstance(entry, dict) else None
    if not exp:
        return True
    try:
        return datetime.strptime(exp, "%Y-%m-%d %H:%M").replace(tzinfo=TZ) > NOW
    except ValueError:
        log(f"WARN: bad expires value {exp!r} (use YYYY-MM-DD HH:MM)"); return True

def blocked(item, block):
    for b in block:
        b = b.lower()
        if b.startswith("http"):
            if canonical_url(b) == item["url"].lower(): return True
        elif b in item["title"].lower():
            return True
    return False

# ---------------------------------------------------------------- BREAKING
def outlet(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])                     # bbc.co.uk
    return ".".join(parts[-2:])                         # foxnews.com

def mark_breaking(shown, S):
    """Red BREAKING label. Editor choice (picks.json "breaking": true/false) always wins and does not
    count toward the cap. Otherwise a story qualifies if 2+ distinct outlets carry it right now, or
    its newest version was published within breaking_recent_minutes. Auto labels are capped at
    breaking_max, strongest first (more outlets, then newest)."""
    recent = timedelta(minutes=S.get("breaking_recent_minutes", 60))
    min_out = S.get("breaking_min_outlets", 2)
    auto = []
    for x in shown:
        if x.get("column") == "laugh":
            continue
        if "breaking_pick" in x:
            x["breaking"] = x["breaking_pick"]
            if x["breaking"]: x["why"] = "editor"
            continue
        if x.get("pinned") or x.get("source") in S.get("breaking_skip_sources", []):
            continue
        multi = x.get("outlets", 1) >= min_out
        fresh = NOW - datetime.fromisoformat(x.get("newest", x["published"])) <= recent
        if multi or fresh:
            x["why"] = (f"{x['outlets']} outlets" if multi else "") + (" + " if multi and fresh else "") + \
                       ("published <1h ago" if fresh else "")
            auto.append(x)
    auto.sort(key=lambda x: (x.get("outlets", 1), x.get("newest", x["published"])), reverse=True)
    for x in auto[:S.get("breaking_max", 5)]:
        x["breaking"] = True

# ---------------------------------------------------------------- social drafts
# Drafts only. Nothing here posts anywhere; there is no posting code and no API keys.
# A human reviews social_drafts/latest.md and runs approve_post.py to mark a draft approved.
SOCIAL_DIR = os.path.join(ROOT, "social_drafts")
X_LIMIT = 280
X_URL_LEN = 23   # X counts every link as 23 characters

def x_weight(ch):
    o = ord(ch)
    return 1 if (o <= 4351 or 8192 <= o <= 8205 or 8208 <= o <= 8223 or 8242 <= o <= 8247) else 2

def x_length(text, urls):
    for u in urls:
        text = text.replace(u, "")
    return sum(x_weight(c) for c in text) + X_URL_LEN * len(urls)

def sentence(h):
    h = h.strip()
    return h if h[-1:] in ".?!\u2026" else h + "."

def make_drafts(lead):
    head = (lead.get("display") or lead["title"]).strip()
    src, url = lead.get("source") or "the original publisher", lead["url"]
    tags = ["#DailyRushReport"] + (["#BreakingNews"] if lead.get("breaking") else [])
    tail = " Common sense, served fast. " + " ".join(tags)
    h = head
    while True:
        x = f"{sentence(h)} ({src}) {url}\n\n{tail.strip()}"
        if x_length(x, [url]) <= X_LIMIT or len(h) < 20:
            break
        h = h[: max(len(h) - 10, 20)].rsplit(" ", 1)[0] + "\u2026"   # trim headline, never the facts' meaning
    ig_tags = ["#DailyRushReport", "#News", "#Headlines", "#CommonSense"] + (["#BreakingNews"] if lead.get("breaking") else [])
    ig = f"{sentence(head)} Story via {src}. Link in bio.\n\n" + " ".join(ig_tags)
    return {
        "x": {"text": x, "weighted_length": x_length(x, [url]), "limit": X_LIMIT, "status": "pending_approval"},
        "instagram": {"text": ig, "status": "pending_approval"},
    }

def social_md(d):
    L = d["lead"]
    out = [f"# Social drafts \u2014 {d['created_ct']}", "",
           "**NOT POSTED. Nothing is ever posted automatically.** Review, then approve with:",
           f"`python3 approve_post.py social_drafts/{d['file']} x` or `... instagram` (prints the final text to copy).", "",
           f"Lead: {L['headline']}  ", f"Outlet: {L['source']}  ", f"Link: {L['url']}  ",
           f"Pinned by editor: {'yes' if L['pinned'] else 'no'} \u00b7 BREAKING: {'yes' if L['breaking'] else 'no'}", ""]
    for k, label in (("x", "X"), ("instagram", "Instagram")):
        dr = d["drafts"][k]
        extra = f" \u00b7 {dr['weighted_length']}/{dr['limit']} chars (X counting)" if k == "x" else ""
        appr = f" \u00b7 approved {dr['approved_at']}" if dr.get("approved_at") else ""
        out += [f"## {label} \u2014 status: {dr['status']}{extra}{appr}", "", "```", dr["text"], "```", ""]
    return "\n".join(out)

def write_social_drafts(lead, state):
    """One draft set per lead (keyed by URL). Re-runs with the same lead don't duplicate it."""
    done = state.setdefault("drafted_leads", [])
    if lead["url"] in done:
        log("Social drafts: already drafted for this lead - skipped")
        return None
    stamp = datetime.now(TZ).strftime("%Y-%m-%d-%H%M")
    d = {"file": f"{stamp}.json", "created": NOW.isoformat(),
         "created_ct": datetime.now(TZ).strftime("%a %b %-d, %Y %-I:%M %p CT"),
         "never_auto_posted": True,
         "lead": {"headline": lead.get("display") or lead["title"], "original_headline": lead["title"],
                  "url": lead["url"], "source": lead.get("source", ""), "pinned": bool(lead.get("pinned")),
                  "breaking": bool(lead.get("breaking"))},
         "drafts": make_drafts(lead)}
    atomic_write(os.path.join(SOCIAL_DIR, d["file"]), json.dumps(d, indent=2, ensure_ascii=False))
    md = social_md(d)
    atomic_write(os.path.join(SOCIAL_DIR, f"{stamp}.md"), md)
    atomic_write(os.path.join(SOCIAL_DIR, "latest.md"), md)
    state["drafted_leads"] = (done + [lead["url"]])[-200:]
    log(f"Social drafts: wrote social_drafts/{stamp}.json/.md + latest.md (pending_approval)")
    return d

# ---------------------------------------------------------------- main build
def build(args):
    cfg = load_json(os.path.join(ROOT, "feeds.json"), None)
    if not cfg:
        sys.exit("feeds.json missing or invalid")
    S = cfg["settings"]
    picks = load_json(os.path.join(ROOT, "picks.json"), {})
    state = load_json(STATE_FILE, {"recent_leads": []})
    feeds = [f for f in cfg["feeds"] if f.get("enabled", True)]

    report, items = [], []
    with cf.ThreadPoolExecutor(16) as ex:
        for f, (name, status, its) in zip(feeds, ex.map(lambda f: fetch_feed(f, S, args.offline), feeds)):
            max_age = timedelta(hours=f.get("max_age_hours", S["default_max_age_hours"]))
            fresh = [i for i in its if NOW - datetime.fromisoformat(i["published"]) <= max_age
                     and datetime.fromisoformat(i["published"]) <= NOW + timedelta(hours=2)]
            report.append((name, status, len(its), len(fresh)))
            items.extend(fresh)

    rewrites = {canonical_url(k): v for k, v in (picks.get("rewrites") or {}).items()}
    block = picks.get("block") or []
    seen, uniq = set(), []
    skip = [p.lower() for p in S.get("skip_if_url_contains", [])]
    skip_t = [p.lower() for p in S.get("skip_if_title_starts_with", [])]
    for i in items:
        if (i["url"] in seen or blocked(i, block) or any(p in i["url"].lower() for p in skip)
                or any(i["title"].lower().startswith(p) for p in skip_t)):
            continue
        seen.add(i["url"])
        rw = rewrites.get(i["url"])
        if rw:
            if isinstance(rw, str): rw = {"title": rw}
            if rw.get("title"):
                i["display"] = rw["title"]
            i["hot"] = rw.get("hot", False)
            if "breaking" in rw:
                i["breaking_pick"] = bool(rw["breaking"])
        i["_tok"] = tokens(i["title"])
        uniq.append(i)

    clusters = cluster(uniq)
    # one representative per cluster: prefer an editor rewrite, else the newest
    reps = []
    for c in clusters:
        rep = next((x for x in c if "display" in x), c[0])
        rep["coverage"] = len({x["source"] for x in c})
        # distinct publishers (by domain) whose headline matches this story directly (not via a chain)
        rep["outlets"] = len({outlet(x["url"]) for x in c if x is rep or similar(x, rep)})
        rep["newest"] = max(x["published"] for x in c)
        rep["_members"] = [f'{x["source"]}: {x["title"]}' for x in c]
        if "breaking_pick" not in rep:
            bp = next((x["breaking_pick"] for x in c if "breaking_pick" in x), None)
            if bp is not None: rep["breaking_pick"] = bp
        reps.append(rep)

    # ---- lead
    lead_pick = picks.get("lead")
    recent = state.get("recent_leads", [])
    if lead_pick and lead_pick.get("url") and active(lead_pick):
        lead = {"title": lead_pick["title"], "display": lead_pick["title"], "url": lead_pick["url"],
                "source": lead_pick.get("source", ""), "hot": True, "pinned": True,
                **({"breaking_pick": bool(lead_pick["breaking"])} if "breaking" in lead_pick else {}),
                "_tok": tokens(lead_pick["title"]), "published": NOW.isoformat()}
        reps = [r for r in reps if r["url"] != canonical_url(lead["url"]) and not similar(r, lead)]
    else:
        window = timedelta(hours=S["lead_window_hours"])
        hold = timedelta(hours=S.get("lead_hold_hours", 3))
        cur = state.get("current_lead") or {}
        # keep the current lead for a few hours so the page doesn't flip on every run...
        keep = next((r for r in reps if r["url"] == cur.get("url")), None)
        if keep and NOW - datetime.fromisoformat(cur["since"]) > hold:
            keep = None
        # ...after that, retire it and don't bring back any of the last N leads
        cands = [keep] if keep else [r for r in reps if r["lead_ok"] and r["column"] != "laugh"
                 and NOW - datetime.fromisoformat(r["published"]) <= window
                 and r["url"] not in recent[-S["avoid_repeat_leads"]:]]
        if not cands:
            cands = [r for r in reps if r["lead_ok"] and r["column"] != "laugh"] or reps
        def score(r):
            age_h = (NOW - datetime.fromisoformat(r["published"])).total_seconds() / 3600
            return r["coverage"] * 3 + (4 if "display" in r else 0) - age_h * 0.35
        lead = max(cands, key=score)
        reps.remove(lead)
    lead.setdefault("hot", True)

    # ---- pinned links
    pinned = [p for p in (picks.get("pinned") or []) if active(p) and p.get("url") and p.get("title")]
    pinned_urls = {canonical_url(p["url"]) for p in pinned}
    reps = [r for r in reps if r["url"] not in pinned_urls]

    # ---- subleads: next strongest lead-eligible stories (prefer different outlets)
    reps.sort(key=lambda r: (r["coverage"], r["published"]), reverse=True)
    subleads, used_src = [], {lead.get("source")}
    for r in reps:
        if len(subleads) >= S["subleads"]: break
        if r["lead_ok"] and r["source"] not in used_src and r["column"] != "laugh":
            subleads.append(r); used_src.add(r["source"])
    for r in subleads: reps.remove(r)

    # ---- columns, newest first, capped per source so no outlet dominates
    cols = {1: [], 2: [], 3: [], "laugh": []}
    for p in pinned:
        c = p.get("column", 1)
        cols.setdefault(c, []).append({"title": p["title"], "display": p["title"], "url": p["url"],
                                       "source": p.get("source", ""), "hot": p.get("hot", False), "pinned": True,
                                       **({"breaking_pick": bool(p["breaking"])} if "breaking" in p else {})})
    per_src = {}
    for r in sorted(reps, key=lambda r: (("display" in r), r["published"]), reverse=True):
        c = r["column"]
        limit = S["laugh_count"] if c == "laugh" else S["per_column"]
        k = (c, r["source"])
        if len(cols[c]) >= limit or per_src.get(k, 0) >= S["max_per_source_per_column"]:
            continue
        per_src[k] = per_src.get(k, 0) + 1
        cols[c].append(r)
    # keep a fixed order within columns: pinned first, then editor-voiced, then newest
    for c in (1, 2, 3):
        cols[c].sort(key=lambda r: (not r.get("pinned"), "display" not in r, ""))

    mark_breaking([lead] + subleads + cols[1] + cols[2] + cols[3], S)

    total = 1 + len(subleads) + sum(len(v) for v in cols.values())
    log("Feed report:")
    for name, status, n, fresh in report:
        log(f"  {name:22s} {status:10s} items={n:3d} fresh={fresh}")
    log(f"BREAKING labels: {sum(1 for x in [lead] + subleads + cols[1] + cols[2] + cols[3] if x.get('breaking'))}"
        + "".join(f"\n    - [{x['why']}] {x.get('display') or x['title']}"
                  + "".join(f"\n        · {m}" for m in x.get("_members", []) if os.environ.get("RR_DEBUG")) for x in [lead] + subleads + cols[1] + cols[2] + cols[3] if x.get('breaking')))
    log(f"Stories placed: {total}; lead = [{lead.get('source')}] {lead.get('display', lead['title'])}")
    if total < 15:
        log("ERROR: fewer than 15 stories - keeping previous index.html"); sys.exit(2)

    page = render(lead, subleads, cols)
    atomic_write(os.path.join(ROOT, args.out), page)
    if os.path.abspath(os.path.join(ROOT, args.out)) != os.path.join(ROOT, "index.html"):
        log(f"Wrote {args.out} (test build: archive, state and social drafts left untouched)"); return
    stamp = datetime.now(TZ).strftime("%Y-%m-%d-%H%M")
    atomic_write(os.path.join(ARCHIVE_DIR, f"{stamp}.html"), page)
    for old in sorted(os.listdir(ARCHIVE_DIR))[:-60]:   # keep the 60 most recent snapshots
        os.remove(os.path.join(ARCHIVE_DIR, old))
    if not lead.get("pinned"):
        if (state.get("current_lead") or {}).get("url") != lead["url"]:
            state["current_lead"] = {"url": lead["url"], "since": NOW.isoformat()}
            recent = (recent + [lead["url"]])[-20:]
    write_social_drafts(lead, state)
    state.update({"recent_leads": recent, "last_build": NOW.isoformat(),
                  "feeds": {n: s for n, s, _, _ in report}})
    atomic_write(STATE_FILE, json.dumps(state, indent=1))
    log(f"Wrote {args.out}")

# ---------------------------------------------------------------- HTML
# Plain newspaper styling: black on white, one column, no graphics, no colour.
def link(i, cls=""):
    t = html.escape(i.get("display") or i["title"])
    src = html.escape(i.get("source", ""))
    sat = "SATIRE: " if i.get("column") == "laugh" else ""
    if i.get("breaking"):
        sat = '<span class="brk">BREAKING</span> ' + sat
    c = f' class="{cls}"' if cls else ""
    return (f'{sat}<a{c} href="{html.escape(i["url"], quote=True)}" target="_blank" rel="noopener">{t}</a>'
            + (f' <span class="src">({src})</span>' if src else ""))

def render(lead, subleads, cols):
    upd = datetime.now(TZ).strftime("%A, %B %-d, %Y &middot; %-I:%M %p CT")
    def ul(lst):
        return "\n".join(f"<li>{link(i)}</li>" for i in lst)
    heads = {1: "NATION &amp; POLITICS", 2: "MONEY &amp; WORLD", 3: "SCIENCE, TECH &amp; ODDITIES", "laugh": "FOR LAUGHS"}
    sections = "\n".join(f'<h2>{heads[c]}</h2>\n<ul>{ul(cols[c])}</ul>'
                         for c in (1, 2, 3, "laugh") if cols.get(c))
    subs = f'<ul class="subs">{ul(subleads)}</ul>' if subleads else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Rush Report</title>
<meta name="description" content="The day's headlines, picked with common sense and served fast. Links go to the original publishers.">
<meta http-equiv="refresh" content="900">
<style>
html{{background:#fff;color:#000}}
body{{margin:0 auto;max-width:760px;padding:16px 18px 28px;font:16px/1.4 Georgia,"Times New Roman",serif;background:#fff;color:#000}}
a,a:visited,a:hover,a:active{{color:#000}}
h1{{margin:0;font-size:34px;font-weight:bold;text-align:center}}
.date{{margin:2px 0 0;text-align:center;font-size:14px}}
hr{{border:0;border-top:1px solid #000;margin:14px 0}}
ul{{list-style:none;margin:0;padding:0}}
li{{padding:7px 0;font-size:17px;font-weight:bold}}
.lead{{margin:20px 0 10px;text-align:center}}
.lead a{{font-size:clamp(28px,5vw,40px);line-height:1.15;font-weight:bold}}
.subs li{{text-align:center;font-size:20px}}
h2{{margin:26px 0 4px;font-size:13px;font-weight:normal;letter-spacing:.08em}}
.src{{font-size:13px;font-weight:normal}}
.brk{{color:#c00;font-weight:bold;font-size:.7em;letter-spacing:.04em;vertical-align:.1em}}
p.end{{margin:30px 0 0;font-size:13px;text-align:center}}
@media (max-width:560px){{h1{{font-size:28px}}li{{padding:9px 0}}.subs li{{font-size:18px}}}}
</style></head>
<body>
<h1>DAILY RUSH REPORT</h1>
<p class="date">{upd}</p>
<hr>
<div class="lead">{link(lead)}</div>
{subs}
<hr>
{sections}
<p class="end">Links go to original publishers.</p>
</body></html>
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="index.html")
    ap.add_argument("--offline", action="store_true", help="build from cached feeds only")
    build(ap.parse_args())
