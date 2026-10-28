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
import gzip
import random
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
MEDIA = "{http://search.yahoo.com/mrss/}"
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", re.I)

def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0

def feed_image(it):
    """Best image the feed itself gives for an item: media:content / media:thumbnail (also inside media:group)
    and image enclosures. Largest declared width wins. Returns {img, w, h, credit} or None."""
    cands, item_credit = [], ""
    for c in it.iter(MEDIA + "credit"):
        if (c.text or "").strip():
            item_credit = c.text.strip(); break
    for el in it.iter():
        if el.tag not in (MEDIA + "content", MEDIA + "thumbnail", "enclosure", ATOM + "link"):
            continue
        u = (el.get("url") or el.get("href") or "").strip()
        if not u.startswith("http"):
            continue
        typ, med = (el.get("type") or "").lower(), (el.get("medium") or "").lower()
        if el.tag == ATOM + "link" and el.get("rel") != "enclosure":
            continue
        if not (typ.startswith("image/") or med == "image" or (not typ and IMG_EXT.search(u))
                or (el.tag == MEDIA + "thumbnail")):
            continue
        cr = el.findtext(MEDIA + "credit") or ""
        cands.append({"img": html.unescape(u), "w": _int(el.get("width")), "h": _int(el.get("height")),
                      "credit": (cr.strip() or item_credit), "thumb": el.tag == MEDIA + "thumbnail"})
    if not cands:
        return None
    best = max(cands, key=lambda c: (c["w"], not c["thumb"]))
    best.pop("thumb")
    return best

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
            cats = [c.text or "" for c in it.findall("category")]
        elif tag == ATOM + "entry":
            title = it.findtext(ATOM + "title")
            link = ""
            for l in it.findall(ATOM + "link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", ""); break
            date = it.findtext(ATOM + "published") or it.findtext(ATOM + "updated")
            cats = [c.get("term", "") or c.get("label", "") for c in it.findall(ATOM + "category")]
        else:
            continue
        title = clean_text(title)
        if not title or not link.strip().startswith("http"):
            continue
        im = feed_image(it)
        out.append({**({"image": im} if im else {}),
                    "title": title, "url": canonical_url(link), "source": feed["name"],
                    "published": (parse_date(date) or NOW).isoformat(),
                    "column": feed.get("column", 1), "lead_ok": bool(feed.get("lead")),
                    "cats": [clean_text(c) for c in cats if clean_text(c) and len(c) < 80][:20]})
    return out

def fetch_feed(feed, settings, offline=False):
    key = hashlib.sha1(feed["url"].encode()).hexdigest()[:16]
    cache = os.path.join(CACHE_DIR, key + ".json")
    if not offline:
        try:
            req = urllib.request.Request(feed["url"], headers={"User-Agent": feed.get("user_agent") or settings["user_agent"],
                                                       "Accept": "application/rss+xml, application/xml, text/xml, */*"})
            raw = urllib.request.urlopen(req, timeout=settings.get("timeout_seconds", 10)).read()
            if raw[:2] == b"\x1f\x8b":                  # some servers gzip the feed even when not asked
                raw = gzip.decompress(raw)
            items = parse_feed(raw, feed)
            if not items:
                raise ValueError("no items")
            if feed.get("date_only"):
                # feed gives a date but no time (Just the News): a story we haven't seen before gets the time we
                # first saw it, so new stories sort as new; ones already cached keep their earlier time. On the
                # very first fetch (no cache) everything keeps the bare date, so the backlog doesn't look fresh.
                prev = load_json(cache, None)
                if prev:
                    seen_at = {i["url"]: i["published"] for i in prev.get("items", [])}
                    for i in items:
                        i["published"] = seen_at.get(i["url"]) or max(i["published"], NOW.isoformat())
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

def _w(t):
    import math
    return math.log(1 + max(len(DF), 1) / DF.get(t, 1))

def similar(a, b):
    """IDF-weighted overlap: two headlines are the same story if they share enough rare words.
    Never on a single shared word (e.g. 'Ukraine'), and satire never merges with real news."""
    ta, tb = a["_tok"], b["_tok"]
    if not ta or not tb or bool(a.get("column") == "laugh") != bool(b.get("column") == "laugh"):
        return False
    shared = ta & tb
    if len(shared) < 2:
        return False
    rare = [t for t in shared if DF.get(t, 1) <= 6]
    score = sum(_w(t) for t in shared) / min(sum(_w(t) for t in ta), sum(_w(t) for t in tb))
    return (len(rare) >= 2 and score >= 0.33) or score >= 0.6

def same_story(a, b, S=None):
    """Stricter test used only for red/breaking: on top of similar(), the two headlines must share
    2+ distinctive words (each used by <= breaking_rare_df headlines in today's pool, so 'Trump',
    'Ukraine', 'Senate' don't count) and the shared words must carry >= breaking_min_overlap of the
    LONGER headline's weight (so one long headline can't swallow a short generic one)."""
    S = S or {}
    if not similar(a, b):
        return False
    shared = a["_tok"] & b["_tok"]
    rare = [t for t in shared if DF.get(t, 1) <= S.get("breaking_rare_df", 8)]
    if len(rare) < 2:
        return False
    overlap = sum(_w(t) for t in shared) / max(sum(_w(t) for t in a["_tok"]), sum(_w(t) for t in b["_tok"]))
    return overlap >= S.get("breaking_min_overlap", 0.30)

def overlap(a, b):
    shared = a["_tok"] & b["_tok"]
    return sum(_w(t) for t in shared) / max(min(sum(_w(t) for t in a["_tok"]), sum(_w(t) for t in b["_tok"])), 1e-9)

def cluster(items):
    """Group near-duplicate stories. Returns list of clusters (lists), each sorted newest first.
    Each cluster is seeded by its newest headline and a story joins only if it matches that seed directly
    (best-matching seed wins). No chaining, so 'West Bank soldiers' can't ride in via a loosely similar
    'gunmen' story into an unrelated 'church funeral in Jordan' cluster."""
    build_df(items)
    clusters = []
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        best = max((c for c in clusters if similar(it, c[0])), key=lambda c: overlap(it, c[0]), default=None)
        if best is not None:
            best.append(it)
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

# Opinion pieces never get the red treatment automatically. A story counts as opinion if ANY of:
#   * its feed is marked "opinion": true in feeds.json (opinion-only feeds)
#   * its URL path has an opinion section segment (/opinion/, /op-eds/, /commentary/, /columnists/ ...)
#   * the feed tags it with an opinion category (Opinion, Op-Eds, Commentary, Editorial(s), Columns ...)
#   * the headline itself is labelled (starts with "Opinion:", "OPINION", "Op-Ed:", "Editorial:", "Commentary:")
# A columnist-style "Name: quote" headline alone is NOT treated as opinion.
OPINION_PATH = re.compile(r"/(opinions?|op-?eds?|commentary|columns?|columnists?|editorials?|perspectives?|"
                          r"letters-to-the-editor|beltway-confidential|restoring-america)(/|$)", re.I)
OPINION_CAT = re.compile(r"^(fox-news/)?(opinions?\b.*|op-?eds?|commentary|columns?|columnists?|editorials?|"
                         r"perspectives?|letters to the editor|beltway confidential)$", re.I)
OPINION_TITLE = re.compile(r"^\s*(opinion|op-?ed|editorial|commentary|column)\s*[:|\-\u2014]", re.I)

def opinion_reason(item, feed_flags):
    if item.get("lifestyle"):
        return f"lifestyle section ({item['lifestyle']})"
    if feed_flags.get(item.get("source")):
        return "opinion-only feed"
    path = urllib.parse.urlsplit(item.get("url", "")).path
    m = OPINION_PATH.search(path)
    if m:
        return f"URL section /{m.group(1)}/"
    for c in item.get("cats") or []:
        if OPINION_CAT.match(c.strip()):
            return f"feed category '{c.strip()}'"
    if OPINION_TITLE.match(item.get("title", "")):
        return "headline labelled opinion"
    return None

def outlet_group(item, S):
    """Who really published it. Same-parent feeds count once: grouped by web domain (CNBC + CNBC Economy,
    BBC + BBC Science, Fox News + Fox News Tech), then by settings.outlet_groups (domain -> parent)."""
    d = outlet(item.get("url", ""))
    return (S.get("outlet_groups") or {}).get(d, d)

def red_coverage_map(items, S):
    """Independent recent coverage per story, for red. Only items published in the last breaking_window_hours
    count, and opinion, satire and breaking_skip_sources items never count. For every recent item (the
    'anchor') we collect the outlets whose headline matches the anchor directly with same_story() -- no
    chaining A~B~C, and no reliance on the order-dependent dedupe clusters. Returns ({url: (n_outlets, counted items)}, {url: urls of every headline on that same story})."""
    win = timedelta(hours=S.get("breaking_window_hours", 6))
    skip = set(S.get("breaking_skip_sources", []))
    pool = [x for x in items
            if not x.get("opinion") and x.get("column") != "laugh" and x.get("source") not in skip
            and NOW - datetime.fromisoformat(x["published"]) <= win]
    best = {}
    for anchor in pool:
        groups = {}
        for x in sorted(pool, key=lambda x: x["published"], reverse=True):
            if x is anchor or same_story(anchor, x, S):
                groups.setdefault(outlet_group(x, S), x)
        members = [x for x in pool if x is anchor or same_story(anchor, x, S)]
        for x in members:                      # every headline in the matched set gets the set's count
            if len(groups) > best.get(x["url"], (0,))[0]:
                best[x["url"]] = (len(groups), list(groups.values()), [m["url"] for m in members])
    return {u: (n, its) for u, (n, its, _) in best.items()}, {u: urls for u, (_, _, urls) in best.items()}

def red_qualifies(x, S):
    """True if the story has enough independent, recent coverage for automatic red (before the cap)."""
    newest = x.get("red_newest")
    return bool(x.get("red_outlets", 0) >= S.get("breaking_min_outlets", 3) and newest
                and NOW - datetime.fromisoformat(newest) <= timedelta(hours=S.get("breaking_fresh_hours", 2))
                and not x.get("opinion") and x.get("column") != "laugh")

def mark_breaking(shown, S):
    """Red headline text (no label), Drudge-style: one, maybe two at a time, sometimes none.
    Editor picks (picks.json "breaking": true/false) are applied first; editor reds DO count toward the cap
    (they're always shown, but leave fewer slots for automatic ones). Automatic red needs
    breaking_min_outlets (3) different independent outlets on the same story within breaking_window_hours (6),
    newest item no older than breaking_fresh_hours (2). Ranked by outlet count, then newest; capped at
    breaking_max (2) in total. Publishing time alone never makes a story red."""
    cap = S.get("breaking_max", 2)
    auto, n_editor = [], 0
    for x in shown:
        x["breaking"] = False
        if x.get("column") == "laugh" or x.get("satire"):
            continue                                   # satire is never red, not even by editor pick
        if "breaking_pick" in x:
            x["breaking"] = x["breaking_pick"]
            if x["breaking"]:
                x["why"] = "editor"; n_editor += 1
            continue
        if x.get("pinned") or x.get("source") in S.get("breaking_skip_sources", []):
            continue
        if x.get("opinion"):
            continue                                   # opinion never auto-red (editor pick above can still force it)
        n, newest = x.get("red_outlets", 0), x.get("red_newest")
        if red_qualifies(x, S):
            age = int((NOW - datetime.fromisoformat(newest)).total_seconds() // 60)
            x["why"] = f"{n} outlets in {S.get('breaking_window_hours', 6)}h, newest {age}m ago: " + \
                       ", ".join(x.get("red_sources", []))
            auto.append(x)
    auto.sort(key=lambda x: (x["red_outlets"], x["red_newest"]), reverse=True)
    slots, used = max(cap - n_editor, 0), set()
    for x in auto:
        story = set(x.get("red_urls", []))
        if story & used:
            x["why_not"] = "same story is already red"       # one red link per story
        elif slots <= 0:
            x["why_not"] = "qualified but over the cap"
        else:
            x["breaking"] = True; slots -= 1; used |= story
    return auto

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

# ---------------------------------------------------------------- photos
# Drudge-style photos: one above the lead, a few in the columns, each from its OWN story. Images are hotlinked
# from the publisher (never downloaded into the repo). Source order: the feed's media:content / media:thumbnail /
# enclosure, else the article page's og:image / twitter:image. Every chosen image is probed (small ranged GET)
# for real pixel size, so tiny images, logos and generic social cards are skipped. Results are cached in
# .state/images.json so the 30-minute rebuilds don't refetch pages. Failures are skipped silently.
IMG_CACHE = os.path.join(STATE_DIR, "images.json")
GENERIC_IMG = re.compile(r"(logo|placeholder|default|fallback|generic|favicon|sprite|spacer|blank\.|social[-_]?share|"
                         r"share[-_]?(image|card)|og[-_]?image[-_]?default|site[-_]?image|/brand(ing)?/|"
                         r"/sp_wp\.|branded_news|overlay-base64|1x1|pixel\.)", re.I)
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def upgrade_img(u):
    """Same picture, bigger size, for CDNs whose size lives in the URL (BBC feed thumbnails are 240px)."""
    return re.sub(r"(ichef\.bbci\.co\.uk/(?:ace|news)/standard/)\d+/", r"\g<1>976/", u)

def img_size(data):
    """Pixel size from the first bytes of a JPEG/PNG/GIF/WebP. (0, 0) if unknown."""
    import struct
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            k = data[12:16]
            if k == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
            if k == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30]); return (w & 0x3FFF, h & 0x3FFF)
            if k == b"VP8L":
                b = int.from_bytes(data[21:25], "little"); return ((b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1)
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1; continue
                m = data[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2; continue
                ln = struct.unpack(">H", data[i + 2:i + 4])[0]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9]); return (w, h)
                i += 2 + ln
    except Exception:
        pass
    return (0, 0)

def probe_img(u, timeout):
    """Fetch the start of the image: confirms it loads, is an image, and gives its real size."""
    req = urllib.request.Request(u, headers={"User-Agent": BROWSER_UA, "Range": "bytes=0-131071",
                                             "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        typ = (r.headers.get("Content-Type") or "").lower()
        total = r.headers.get("Content-Range", "").rsplit("/", 1)[-1] if r.status == 206 else r.headers.get("Content-Length")
        data = r.read(131072)
    if not typ.startswith("image/") or "svg" in typ:
        raise ValueError(f"not a photo ({typ})")
    w, h = img_size(data)
    return w, h, _int(total)

META_RE = re.compile(r"<meta\s[^>]*>", re.I)
def page_image(url, timeout):
    """og:image / twitter:image (+ og:image:width, and a JSON-LD creditText if the page has one)."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        page = r.read(600_000).decode("utf-8", "replace")
    meta = {}
    for tag in META_RE.findall(page):
        k = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)', tag, re.I)
        v = re.search(r'content\s*=\s*["\']([^"\']*)', tag, re.I)
        if k and v:
            meta.setdefault(k.group(1).lower(), html.unescape(v.group(1)).strip())
    u = meta.get("og:image") or meta.get("og:image:secure_url") or meta.get("twitter:image") or meta.get("twitter:image:src")
    if not u:
        return None
    u = urllib.parse.urljoin(url, u)
    cr = re.search(r'"creditText"\s*:\s*"([^"]{2,80})"', page)
    return {"img": u, "w": _int(meta.get("og:image:width")), "h": _int(meta.get("og:image:height")),
            "credit": html.unescape(cr.group(1)) if cr else ""}

def clean_credit(c):
    c = clean_text(c or "")
    c = re.sub(r"^(photo(graph)?s?|image|credit|picture)\s*(credit)?\s*[:\-\u2014]?\s*", "", c, flags=re.I).strip(" .")
    if not c or len(c) > 70 or re.search(r"https?://|www\.", c):
        return ""
    return c

def resolve_photo(it, S, cache, generic):
    """Return a usable photo for one story ({img, w, h, credit}) or None. Uses/updates the cache."""
    key = it["url"]
    hit = cache.get(key)
    if hit and hit.get("v") == 2 and (hit.get("photo") or
            NOW - datetime.fromisoformat(hit["checked"]) < timedelta(hours=6)):   # retry misses after 6h
        return hit.get("photo")
    timeout = S.get("photo_timeout_seconds", 6)
    minw = S.get("photo_min_width", 300)
    cands = []
    fi = it.get("image")
    if fi:
        cands.append(dict(fi, img=upgrade_img(fi["img"]), via="feed"))
    if not fi or (fi.get("w") or 0) < 600 or GENERIC_IMG.search(fi["img"]):
        try:
            pi = page_image(it["url"], timeout)
            if pi and (not fi or pi["img"] != fi["img"]):
                cands.append(dict(pi, via="page"))
        except Exception:
            pass
    best = None
    for c in cands:
        if GENERIC_IMG.search(c["img"]) or c["img"] in generic:
            continue
        try:
            w, h, size = probe_img(c["img"], timeout)
        except Exception:
            continue
        w, h = (w or c.get("w") or 0), (h or c.get("h") or 0)
        if w < minw or (h and h < 150) or (h and not 0.5 <= w / h <= 2.6):
            continue                                    # tiny, banner-shaped or tall strip: skip
        if size and (size < 6000 or size > S.get("photo_max_bytes", 2_000_000)):
            continue                                    # icon-sized file, or a multi-MB original
        c.update(w=w, h=h)
        if best is None or w > best["w"]:
            best = c
    photo = None
    if best:
        photo = {"img": best["img"], "w": best["w"], "h": best["h"], "credit": clean_credit(best.get("credit")),
                 "via": best["via"]}
    cache[key] = {"v": 2, "photo": photo, "checked": NOW.isoformat()}
    return photo

def generic_images(items):
    """An image URL used by two or more different stories is a stock/default card, not the story's photo."""
    seen = {}
    for i in items:
        u = (i.get("image") or {}).get("img")
        if u:
            seen.setdefault(u, set()).add(i["url"])
    return {u for u, s in seen.items() if len(s) > 1}

def photo_ok(it):
    return not (it.get("opinion") or it.get("satire") or it.get("column") == "laugh")

def assign_photos(lead, cols, S, picks, all_items):
    """Lead photo + S.column_photos (default 3) column photos, spread across the three columns,
    each placed directly above its own story near the top of its column."""
    cache = load_json(IMG_CACHE, {})
    cutoff = NOW - timedelta(days=3)
    cache = {k: v for k, v in cache.items() if datetime.fromisoformat(v.get("checked", "2000-01-01T00:00:00+00:00")) > cutoff}
    generic = generic_images(all_items)
    # generic cards seen by the page fetcher for several stories also count
    pc = {}
    for v in cache.values():
        if (v.get("photo") or {}).get("via") == "page":
            pc[v["photo"]["img"]] = pc.get(v["photo"]["img"], 0) + 1
    generic |= {u for u, n in pc.items() if n > 1}
    n_col = int(S.get("column_photos", 3))
    depth = int(S.get("photo_search_depth", 8))          # only look at the top N stories of each column
    want = []
    if photo_ok(lead):
        want.append(lead)
    for c in (1, 2, 3):
        want += [x for x in cols.get(c, [])[:depth] if photo_ok(x)]
    with cf.ThreadPoolExecutor(8) as ex:                   # cached stories return instantly
        list(ex.map(lambda x: resolve_photo(x, S, cache, generic), want))
    got = {x["url"]: resolve_photo(x, S, cache, generic) for x in want}
    # lead: picks.json "lead_image" = URL to force a photo, false to show none
    li = picks.get("lead_image", None)
    if li is False:
        lead.pop("photo", None)
    elif isinstance(li, str) and li.startswith("http"):
        lead["photo"] = {"img": li, "credit": clean_credit(picks.get("lead_image_credit", ""))}
    elif got.get(lead["url"]) and got[lead["url"]]["w"] >= S.get("lead_photo_min_width", 400):
        lead["photo"] = got[lead["url"]]
    # columns: round-robin 1,2,3,1,2,3... taking the next photo-able story, at least 4 stories below the last one
    nxt = {1: 0, 2: 0, 3: 0}
    placed, used_imgs = 0, {(lead.get("photo") or {}).get("img")}
    while placed < n_col:
        progress = False
        for c in (1, 2, 3):
            if placed >= n_col:
                break
            lst = cols.get(c, [])
            for k in range(nxt[c], min(len(lst), depth)):
                ph = got.get(lst[k]["url"])
                if ph and ph["img"] not in used_imgs and photo_ok(lst[k]):
                    lst[k]["photo"] = ph; used_imgs.add(ph["img"])
                    nxt[c] = k + 4; placed += 1; progress = True
                    break
            else:
                nxt[c] = depth
        if not progress:
            break
    atomic_write(IMG_CACHE, json.dumps(cache, indent=0))
    return [x for x in [lead] + cols.get(1, []) + cols.get(2, []) + cols.get(3, []) if x.get("photo")]

def feed_filter(f, items):
    """Per-feed options (all optional, see README "Lifestyle sections"):
    skip_if_url_contains / skip_if_title_starts_with / skip_categories: drop matching items from this feed only;
    title_must_match: regex, keep only matching headlines (e.g. religious-liberty stories from a church news feed);
    strip_title_suffix: text removed from the end of headlines (Google News adds " - Publisher");
    lifestyle: section name ("health", "faith") - see lifestyle_sections in settings."""
    urls = [p.lower() for p in f.get("skip_if_url_contains", [])]
    starts = [p.lower() for p in f.get("skip_if_title_starts_with", [])]
    cats = {c.lower() for c in f.get("skip_categories", [])}
    must = re.compile(f["title_must_match"], re.I) if f.get("title_must_match") else None
    suf = f.get("strip_title_suffix")
    out = []
    for i in items:
        i = dict(i)
        if suf and i["title"].endswith(suf):
            i["title"] = i["title"][:-len(suf)].rstrip()
        if (any(p in i["url"].lower() for p in urls) or any(i["title"].lower().startswith(p) for p in starts)
                or cats & {c.lower() for c in i.get("cats") or []} or (must and not must.search(i["title"]))):
            continue
        if f.get("lifestyle"):
            i["lifestyle"] = f["lifestyle"]
        out.append(i)
    return out

# ---------------------------------------------------------------- main build
def build(args):
    cfg = load_json(os.path.join(ROOT, "feeds.json"), None)
    if not cfg:
        sys.exit("feeds.json missing or invalid")
    S = cfg["settings"]
    picks = load_json(os.path.join(ROOT, "picks.json"), {})
    state = load_json(STATE_FILE, {"recent_leads": []})
    feeds = [f for f in cfg["feeds"] if f.get("enabled", True)]
    opinion_feeds = {f["name"]: True for f in feeds if f.get("opinion")}

    report, items = [], []
    with cf.ThreadPoolExecutor(16) as ex:
        for f, (name, status, its) in zip(feeds, ex.map(lambda f: fetch_feed(f, S, args.offline), feeds)):
            max_age = timedelta(hours=f.get("max_age_hours", S["default_max_age_hours"]))
            fresh = [i for i in its if NOW - datetime.fromisoformat(i["published"]) <= max_age
                     and datetime.fromisoformat(i["published"]) <= NOW + timedelta(hours=2)]
            fresh = feed_filter(f, fresh)
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
        why = opinion_reason(i, opinion_feeds)
        if why:
            i["opinion"] = why
        uniq.append(i)

    clusters = cluster(uniq)
    cov, story_of = red_coverage_map(uniq, S)
    # one representative per cluster: prefer an editor rewrite, else the newest
    reps = []
    for c in clusters:
        rep = next((x for x in c if "display" in x), c[0])
        rep["coverage"] = len({x["source"] for x in c})
        # distinct publishers (by domain) whose headline matches this story directly (not via a chain)
        # (opinion columns about a story don't count as independent news coverage)
        rep["outlets"] = len({outlet(x["url"]) for x in c if x is rep or (similar(x, rep) and not x.get("opinion"))})
        # red coverage must be about THIS headline: the best anchor whose matched set includes rep itself
        n_red, red_items = cov.get(rep["url"], (0, []))
        rep["red_outlets"] = n_red
        if red_items:
            rep["red_newest"] = max(x["published"] for x in red_items)
            rep["red_sources"] = [x["source"] for x in sorted(red_items, key=lambda x: x["published"], reverse=True)]
            rep["red_urls"] = story_of.get(rep["url"], []) + [x["url"] for x in c]
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
        cands = [keep] if keep else [r for r in reps if r["lead_ok"] and r["column"] != "laugh" and not r.get("opinion")
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
    # a headline the red matcher ties to the lead/sub-lead story (worded too differently for the dedupe)
    # isn't repeated as another sub-lead or lower down the page
    top_story = set(story_of.get(lead.get("url"), []))
    for r in reps:
        if len(subleads) >= S["subleads"]: break
        if r["url"] in top_story: continue
        if r["lead_ok"] and r["source"] not in used_src and r["column"] != "laugh" and not r.get("opinion"):
            subleads.append(r); used_src.add(r["source"]); top_story |= set(story_of.get(r["url"], []))
    for r in subleads: reps.remove(r)
    reps = [r for r in reps if r["url"] not in top_story]

    # ---- columns, newest first, capped per source so no outlet dominates
    cols = {1: [], 2: [], 3: [], "laugh": []}
    for p in pinned:
        c = p.get("column", 1)
        cols.setdefault(c, []).append({"title": p["title"], "display": p["title"], "url": p["url"],
                                       "source": p.get("source", ""), "hot": p.get("hot", False), "pinned": True,
                                       **({"breaking_pick": bool(p["breaking"])} if "breaking" in p else {})})
    per_src = {}
    # a feed can set a tighter "max_per_column" than the global max_per_source_per_column
    src_cap = {f["name"]: min(f["max_per_column"], S["max_per_source_per_column"]) for f in feeds if f.get("max_per_column")}
    # a story that qualifies for red always gets a column slot (placed first), so it can't fall off the page
    for r in sorted(reps, key=lambda r: (red_qualifies(r, S), ("display" in r), r["published"]), reverse=True):
        c = r["column"]
        if c == "laugh" or r.get("lifestyle"):
            continue                                   # For Laughs / lifestyle sections are filled below
        limit = S["laugh_count"] if c == "laugh" else S["per_column"]
        k = (c, r["source"])
        if len(cols[c]) >= limit or per_src.get(k, 0) >= src_cap.get(r["source"], S["max_per_source_per_column"]):
            continue
        per_src[k] = per_src.get(k, 0) + 1
        cols[c].append(r)
    # ---- lifestyle sections (health, faith): their own small, capped lists under the Culture column, newest
    # first, at most max_per_source_per_column per outlet. They never lead or turn red (treated as opinion).
    for g, conf in (S.get("lifestyle_sections") or {}).items():
        key, lst, n_src = "life:" + g, [], {}
        for r in sorted((r for r in reps if r.get("lifestyle") == g), key=lambda r: r["published"], reverse=True):
            if len(lst) >= conf.get("max", 2): break
            per = conf.get("max_per_source", S["max_per_source_per_column"])
            if n_src.get(r["source"], 0) >= per: continue
            n_src[r["source"]] = n_src.get(r["source"], 0) + 1
            lst.append(r)
        cols[key] = cols.get(key, []) + lst          # editor pins with column "life:<name>" go first
    # ---- For Laughs: rotate across satire outlets so no single one dominates. Take each outlet's newest
    # story (skipping crude headlines), at most 1 per outlet, and shuffle the outlet order with a seed that
    # changes every hour (Central time), so a different outlet leads and a different mix shows through the day.
    bad = [w.lower() for w in S.get("laugh_skip_words", [])]
    newest = {}
    for r in sorted((r for r in reps if r["column"] == "laugh"), key=lambda r: r["published"], reverse=True):
        t = (r.get("display") or r["title"]).lower()
        if r["source"] not in newest and not any(w in t for w in bad):
            newest[r["source"]] = r
    outlets = sorted(newest)
    random.Random(datetime.now(TZ).strftime("%Y-%m-%d %H")).shuffle(outlets)
    cols["laugh"] = [p for p in cols["laugh"] if p.get("pinned")]
    cols["laugh"] += [newest[o] for o in outlets][:max(0, S["laugh_count"] - len(cols["laugh"]))]

    # keep a fixed order within columns: pinned first, then editor-voiced, then newest
    for c in (1, 2, 3):
        cols[c].sort(key=lambda r: (not r.get("pinned"), "display" not in r, ""))

    qualified = mark_breaking([lead] + subleads + cols[1] + cols[2] + cols[3], S)

    # ---- Drudge layout: only the lead sits above the masthead. The other top stories (sub-leads) move to the
    # top of the columns: each to its own column (1 or 2 for news; 3 only if it's a Culture story), at most
    # 2 per column, overflowing to the other news column. They keep their red status and get class "sub".
    tops = {1: [], 2: [], 3: []}
    for r in subleads:
        home = r["column"] if r["column"] in (1, 2, 3) else 1
        order = [home] + [c for c in (1, 2) if c != home] + ([3] if home != 3 else [])
        dest = next((c for c in order if len(tops[c]) < 2), min(tops, key=lambda c: len(tops[c])))
        r["top"] = True
        tops[dest].append(r)
    for c in (1, 2, 3):
        cols[c] = tops[c] + cols[c]
    subleads = []

    with_photos = assign_photos(lead, cols, S, picks, uniq)
    log(f"Photos: {len(with_photos)}" + "".join(
        f"\n    - {x.get('display') or x['title']} ({x.get('source')}) <- {x['photo']['img']}" for x in with_photos))

    total = 1 + len(subleads) + sum(len(v) for v in cols.values())
    log("Feed report:")
    for name, status, n, fresh in report:
        log(f"  {name:22s} {status:10s} items={n:3d} fresh={fresh}")
    life = [x for k, v in cols.items() if str(k).startswith("life:") for x in v]
    lost = [x for x in [lead] + subleads + cols[1] + cols[2] + cols[3] + life if x.get("opinion") and not x.get("breaking")]
    log(f"Opinion pieces on page (never auto-red): {len(lost)}"
        + "".join(f"\n    - [{x['opinion']}] {x.get('display') or x['title']} ({x.get('source')})" for x in lost))
    log(f"Red (breaking) headlines: {sum(1 for x in [lead] + subleads + cols[1] + cols[2] + cols[3] if x.get('breaking'))}"
        + "".join(f"\n    - [{x['why']}] {x.get('display') or x['title']}"
                  + "".join(f"\n        · {m}" for m in x.get("_members", []) if os.environ.get("RR_DEBUG")) for x in [lead] + subleads + cols[1] + cols[2] + cols[3] if x.get('breaking')))
    for x in qualified:
        if x.get("why_not"):
            log(f"    (not red, {x['why_not']}) [{x['why']}] {x.get('display') or x['title']}")
    log(f"Stories placed: {total}; lead = [{lead.get('source')}] {lead.get('display', lead['title'])}")
    if total < 15:
        log("ERROR: fewer than 15 stories - keeping previous index.html"); sys.exit(2)

    page = render(lead, subleads, cols, feeds, S)
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
# Drudge-style layout (style only, original content): the lead's photo + big bold uppercase sans headline
# ALONE on top (the other top stories open the columns),
# heavy italic "DAILY RUSH REPORT" masthead under it, then three typewriter-font columns split by
# thin rules. Breaking = red headline text (no label). One photo over the lead and a few in the columns,
# each from its own story (hotlinked, credited). No ads.
# UNDERLINES: one toggle - set --link-underline in the <style> block to "none" to switch them off.
HOMES = {"Fox News": "https://www.foxnews.com/", "Newsmax": "https://www.newsmax.com/", "Breitbart": "https://www.breitbart.com/", "Fox Business": "https://www.foxbusiness.com/", "Just the News": "https://justthenews.com/", "NY Post": "https://nypost.com/",
         "Washington Examiner": "https://www.washingtonexaminer.com/", "Axios": "https://www.axios.com/",
         "The Hill": "https://thehill.com/", "Politico": "https://www.politico.com/",
         "Daily Caller": "https://dailycaller.com/", "Daily Wire": "https://www.dailywire.com/",
         "Free Beacon": "https://freebeacon.com/", "RealClearPolitics": "https://www.realclearpolitics.com/",
         "The Free Press": "https://www.thefp.com/", "Reason": "https://reason.com/",
         "National Review": "https://www.nationalreview.com/", "Bloomberg": "https://www.bloomberg.com/",
         "CNBC": "https://www.cnbc.com/", "BBC": "https://www.bbc.com/news",
         "WSJ Opinion": "https://www.wsj.com/opinion", "The Guardian": "https://www.theguardian.com/us",
         "NPR": "https://www.npr.org/", "Ars Technica": "https://arstechnica.com/",
         "The Verge": "https://www.theverge.com/", "ScienceDaily": "https://www.sciencedaily.com/",
         "UPI Odd News": "https://www.upi.com/Odd_News/", "Space.com": "https://www.space.com/",
         "The Jerusalem Post": "https://www.jpost.com/", "The Times of Israel": "https://www.timesofisrael.com/",
         "Fox News Health": "https://www.foxnews.com/health", "Peter Attia MD": "https://peterattiamd.com/",
         "Zoe Harcombe": "https://www.zoeharcombe.com/", "Religion News Service": "https://religionnews.com/",
         "Adventist News Network": "https://adventist.news/",
         "Babylon Bee": "https://babylonbee.com/", "The Onion": "https://theonion.com/",
         "Reductress": "https://reductress.com/", "The Hard Times": "https://thehardtimes.net/",
         "Duffel Blog": "https://www.duffelblog.com/", "ClickHole": "https://clickhole.com/",
         "The Beaverton": "https://www.thebeaverton.com/", "The Daily Mash": "https://www.thedailymash.co.uk/"}

def link(i, cls=""):
    t = html.escape(i.get("display") or i["title"])
    src = html.escape(i.get("source", ""))
    sat = "SATIRE: " if i.get("column") == "laugh" else ""
    classes = " ".join(c for c in (cls, "red" if i.get("breaking") else "") if c)
    c = f' class="{classes}"' if classes else ""
    return (f'<a{c} href="{html.escape(i["url"], quote=True)}" target="_blank" rel="noopener">{sat}{t}</a>'
            + (f' <span class="src">({src})</span>' if src else ""))

def photo(i, cls="ph", lazy=True):
    """Story photo: hotlinked from the publisher, linked to the story, credit underneath. If it fails to load
    in the browser, the whole figure removes itself (no broken-image icon)."""
    p = i.get("photo")
    if not p:
        return ""
    credit = p.get("credit") or re.sub(r" (Economy|Tech|Science|Odd News|Opinion)$", "", i.get("source") or "")
    wh = f' width="{p["w"]}" height="{p["h"]}"' if p.get("w") and p.get("h") else ""
    return (f'<figure class="{cls}"><a href="{html.escape(i["url"], quote=True)}" target="_blank" rel="noopener">'
            f'<img src="{html.escape(p["img"], quote=True)}" alt="{html.escape(i.get("display") or i["title"], quote=True)}"{wh}'
            f'{" loading=" + chr(34) + "lazy" + chr(34) if lazy else ""} referrerpolicy="no-referrer" '
            f'onerror="this.closest(\'figure\').remove()"></a>'
            + (f'<figcaption>(Photo: {html.escape(credit)})</figcaption>' if credit else "") + '</figure>')

def column(label, lst, every=3):
    out = [f'<h2>{label}</h2>']
    for n, i in enumerate(lst):
        if i.get("photo"):
            out.append(photo(i))
        out.append(f'<p class="sub">{link(i)}</p>' if i.get("top") else f'<p>{link(i)}</p>')
        if (n + 1) % every == 0 and n + 1 < len(lst):
            out.append("<hr>")
    return "\n".join(out)

def render(lead, subleads, cols, feeds=(), S=None):
    upd = datetime.now(TZ).strftime("%A, %B %-d, %Y").upper() + " &middot; " + datetime.now(TZ).strftime("%-I:%M %p CT")
    c1 = column("NATION &amp; POLITICS", cols.get(1, []))
    c2 = column("MONEY &amp; WORLD", cols.get(2, []))
    # One Culture column, no subtitles: health, faith, general-interest and satire stories are mixed in among
    # the regular culture stories (satire keeps its "SATIRE:" label). Extras are spread out, alternating
    # between the lifestyle stories and the satire, one after every two regular stories.
    base = list(cols.get(3, []))
    life = [x for g in ((S or {}).get("lifestyle_sections") or {}) for x in cols.get("life:" + g, [])]
    laugh = list(cols.get("laugh", []))
    extras = []
    while life or laugh:
        if life: extras.append(life.pop(0))
        if laugh: extras.append(laugh.pop(0))
    tops_n = sum(1 for x in base if x.get("top") or x.get("pinned"))
    mixed, k, pos = [], 0, tops_n + 2
    for n, x in enumerate(base):
        mixed.append(x)
        if n + 1 >= pos and k < len(extras):
            mixed.append(extras[k]); k += 1; pos = n + 3
    mixed += extras[k:]
    c3 = column("CULTURE", mixed)
    seen, srcs = set(), []
    for f in feeds:
        home = HOMES.get(f["name"])
        if home and f["name"] not in seen and f["name"] != "WSJ Opinion":
            seen.add(f["name"])
            srcs.append((f["name"].upper(), f'<a href="{home}" target="_blank" rel="noopener">{html.escape(f["name"].upper())}</a>'))
    sources = ('<hr>\n<h2>SOURCES</h2>\n<p class="wire">' + "<br>\n".join(a for _, a in sorted(srcs)) + "</p>") if srcs else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Rush Report</title>
<meta name="description" content="The day's headlines, picked with common sense and served fast. Links go to the original publishers.">
<meta http-equiv="refresh" content="900">
<style>
:root{{--link-underline:underline;--red:#f00}}  /* set --link-underline:none to remove all underlines */
html{{background:#fff;color:#000}}
body{{margin:0 auto;max-width:1100px;padding:18px 10px 40px;background:#fff;color:#000;
  font:bold 13px/1.25 "Courier New",Courier,"Nimbus Mono PS","Liberation Mono",monospace}}
a,a:visited,a:hover,a:active{{color:#000;text-decoration:var(--link-underline)}}
a.red,a.red:visited,a.red:hover,a.red:active{{color:var(--red)}}
.top{{text-align:center;font-family:Arial,Helvetica,"Liberation Sans",sans-serif;font-weight:bold}}
.lead{{margin:6px 0 10px;font-size:clamp(26px,4.4vw,46px);line-height:1.12;text-transform:uppercase}}
.lead .src{{font-family:"Courier New",Courier,monospace}}
.col p.sub{{font-size:1.1em}}
.mast{{margin:22px 0 0;text-align:center;font:italic 900 clamp(34px,7.4vw,84px)/1 "Arial Black","Arial Bold",Gadget,Impact,"Helvetica Neue",Arial,sans-serif;
  letter-spacing:-.02em;transform:skewX(-6deg)}}
.date{{margin:6px 0 18px;text-align:center;font-size:11px;font-weight:normal}}
.cols{{display:grid;grid-template-columns:1fr 1fr 1fr}}
.col{{padding:0 10px;min-width:0}}
.col+.col{{border-left:1px solid #000}}
h2{{margin:0 0 8px;font-size:11px;font-weight:normal;letter-spacing:.06em;border-bottom:1px solid #000;padding-bottom:3px}}
.col h2:not(:first-child){{margin-top:4px}}
.col p{{margin:0 0 12px;overflow-wrap:anywhere}}
hr{{border:0;border-top:1px solid #000;margin:10px 0 12px}}
.src{{font-size:11px;font-weight:normal;white-space:nowrap;text-transform:none}}
.wire{{line-height:1.35}}
figure{{margin:0}}
.ph{{margin:0 0 6px}}
.ph img{{display:block;width:100%;max-width:260px;height:auto;border:0}}
.ph figcaption{{font-size:10px;font-weight:normal;margin-top:2px}}
.leadph{{margin:4px auto 6px;width:min(100%,420px)}}
.leadph img{{max-width:100%;max-height:440px;width:100%;object-fit:cover;margin:0 auto}}
.leadph figcaption{{font-family:"Courier New",Courier,monospace;text-align:right}}
p.end{{margin:30px 0 0;font-size:11px;font-weight:normal;text-align:center}}
@media (max-width:700px){{body{{padding:12px 4px 30px;font-size:11px}}.col{{padding:0 4px}}.src{{font-size:9.5px;white-space:normal}}h2{{font-size:9.5px}}}}
@media (max-width:340px){{.cols{{grid-template-columns:1fr}}.col+.col{{border-left:0;border-top:1px solid #000;padding-top:10px}}}}
</style></head>
<body>
<div class="top">
{photo(lead, "ph leadph", lazy=False)}
<p class="lead">{link(lead)}</p>
</div>
<h1 class="mast">DAILY RUSH REPORT</h1>
<p class="date">{upd}</p>
<div class="cols">
<div class="col">
{c1}
</div>
<div class="col">
{c2}
</div>
<div class="col">
{c3}
{sources}
</div>
</div>
<hr>
<p class="end">Links go to original publishers.</p>
</body></html>
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="index.html")
    ap.add_argument("--offline", action="store_true", help="build from cached feeds only")
    build(ap.parse_args())
