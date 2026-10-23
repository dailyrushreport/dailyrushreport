# Daily Rush Report

A headline-first news aggregator. One Python script (standard library only, Python 3.9+)
pulls RSS feeds, dedupes, picks a lead, and writes a single static `index.html`.
Headlines link straight to the original publishers; we only show the headline and link.

## Files
| File | What it is |
|---|---|
| `build.py` | The builder. Run it; it writes `index.html`. |
| `feeds.json` | Feed list + settings (which column, which feeds may supply the lead, freshness limits). |
| `picks.json` | **Editor overrides** — pin a lead, add links, rewrite headlines, block stories. |
| `approve_post.py` | Marks one social draft approved and prints the final text to copy. Never posts. |
| `social_drafts/` | Auto-written X + Instagram drafts for each new lead (`latest.md` = newest). |
| `run_daily.sh` | Wrapper for cron; logs to `build.log`. |
| `.github/workflows/build.yml` | Optional: auto-rebuild every 30 min on GitHub. |
| `archive/` | Snapshot of every build (last 60 kept). |
| `.state/` | Feed cache + lead history (lets the site survive feed outages). Don't edit. |

## Daily workflow
1. **Hands-off:** cron (or GitHub Actions) runs `./run_daily.sh` every 30 min. Nothing else is required.
   ```
   */30 * * * * /path/to/rush-report/run_daily.sh
   ```
2. **Morning editor pass (5–10 min, optional):** open the site, then edit `picks.json`:
   - Pin today's lead:
     ```json
     "lead": {"title": "PLAIN-SPOKEN HEADLINE HERE", "url": "https://publisher.com/story", "source": "WSJ", "expires": "2026-10-23 18:00"}
     ```
   - Add a must-read link to a column (1 = Nation & Politics, 2 = Money & World, 3 = Science, Tech & Oddities):
     ```json
     "pinned": [{"title": "...", "url": "https://...", "source": "Reason", "column": 2, "hot": true, "expires": "2026-10-23 23:59"}]
     ```
   - Give a story the house voice (keep the same URL; **must stay factually accurate to the story — read it first**):
     ```json
     "rewrites": {"https://publisher.com/story": {"title": "Punchy but true headline", "hot": true}}
     ```
   - Hide something: `"block": ["https://exact-url", "keyword in title"]`
   - Run `python3 build.py` (or just wait for the next scheduled run).
3. **Social posts (drafts only — nothing is ever posted automatically):** every time the lead changes
   (pinned or automatic) the build writes `social_drafts/YYYY-MM-DD-HHMM.json` + `.md` and refreshes
   `social_drafts/latest.md`. Each draft is `pending_approval`. Re-runs with the same lead don't make new drafts.
   - Open `social_drafts/latest.md` and read both drafts against the story. To change wording, edit the
     `"text"` in the matching `.json` (keep it factual to the headline).
   - Approve: `python3 approve_post.py social_drafts/2026-10-23-0636.json x` (or `instagram`).
     It marks that draft `approved`, re-checks the X length, and prints the final text. You copy and post it yourself.
   - X drafts: headline first, outlet, link, "Common sense, served fast." and 1–2 hashtags, kept under 280
     characters the way X counts them (every link = 23). Instagram drafts: headline, outlet, "Link in bio", hashtags.
   - There is no posting code and there are no API keys in this project.
   - So the morning routine is: **pin the lead in picks.json → run `python3 build.py` → review `social_drafts/latest.md` → approve.**
4. Set `"lead": null` (or let `expires` pass) to return to the automatic lead.

## How the automation behaves
- **Fresh:** items older than each feed's `max_age_hours` (default 36) are dropped. Stale feeds are skipped on their own.
- **Lead:** chosen from `lead: true` feeds in the last `lead_window_hours`, favoring stories covered by
  several outlets and stories you've rewritten. It stays up `lead_hold_hours` (3) and then rotates; the last
  `avoid_repeat_leads` (4) leads can't come back.
- **Dedupe:** exact URL matching plus near-duplicate headline matching across outlets (one link per story).
  Two headlines are only treated as the same story if they share at least **two** meaningful words (one shared
  word like "Ukraine" is not enough), and a satire headline is never merged with a news headline. Each story group
  is matched against its newest headline only, so unrelated stories can't chain together through a middle one.
- **Balance:** at most `max_per_source_per_column` (3) links from one outlet per column.
- **Failures:** if a feed is down, its last good copy (if still fresh) is used. If the whole build gets fewer
  than 15 stories, the old `index.html` is left alone and the script exits non-zero.
- **Junk filter:** `skip_if_url_contains` / `skip_if_title_starts_with` in `feeds.json` (podcasts, video, celebrity pages, etc.).
- Satire (Babylon Bee) only appears in the "For laughs" section at the bottom, each item prefixed **SATIRE:**.

## Red (breaking) headlines
Drudge-style: usually **one, sometimes two, sometimes none**. A breaking story is shown as **red headline text**
(no "BREAKING" word in front). Being new is not enough on its own. A story turns red only when **all** of these are true:
- **3 or more different outlets** are carrying the same story within the **last 6 hours**;
- the **newest** of those reports is **2 hours old or less** (the story is still moving);
- the story is actually on the page (a story that qualifies is always given a slot in its column).

How outlets are counted:
- Same-owner outlets count **once**: BBC + BBC Science, CNBC + CNBC Economy + NBC News, Fox News + Fox Business +
  OutKick, NY Post + WSJ (News Corp). The list is `outlet_groups` in `feeds.json`.
- Opinion, satire and the `breaking_skip_sources` outlets don't count as coverage.
- Matching for red is stricter than the page dedupe: the headlines must share at least **two distinctive words**
  (names, places, things — not common words like "Trump" or "says"), and those words must make up at least
  **30%** of the longer headline. Each headline is compared directly with the others; no chaining.

When more stories qualify than the cap allows, the ones with the **most outlets** win, then the one updated most recently.
Only **one red link per story** (if the same story appears twice, only one copy is red). Hard cap: **2 red on the page**.
The build log shows every red headline with its reason (e.g. "3 outlets in 6h, newest 10m ago: BBC, Washington
Examiner, CNBC") and any story that qualified but wasn't red, with why.

Never red automatically:
- **Opinion.** A story is opinion if any of these is true: its feed has `"opinion": true` in `feeds.json`
  (currently WSJ Opinion and RealClearPolitics); its URL has an opinion section such as `/opinion/`, `/opinions/`,
  `/op-ed/`, `/op-eds/`, `/commentary/`, `/column(s)/`, `/columnists/`, `/editorial(s)/`, `/perspective(s)/`,
  `/letters-to-the-editor/`, `/beltway-confidential/`, `/restoring-america/`; the feed tags it with an opinion category
  (Opinion, Opinions - …, Op-Eds, Commentary, Editorial(s), Columns, fox-news/opinion…); or the headline starts with
  "Opinion:", "Op-Ed:", "Editorial:", "Commentary:" or "Column:". A "Name: quote" headline alone does not count.
  Opinion pieces are also never picked as the lead or a sub-lead. The build log lists opinion stories on the page and why.
- Outlets in `breaking_skip_sources` (RealClearPolitics, National Review, Reason, The Free Press).
- **Satire** (Babylon Bee / "For laughs") — never red, not even by editor pick.

Settings in `feeds.json`: `breaking_max` (2, total red per page), `breaking_min_outlets` (3),
`breaking_window_hours` (6), `breaking_fresh_hours` (2), `breaking_rare_df` (8, how rare a shared word must be),
`breaking_min_overlap` (0.30), `outlet_groups`.

Editor control in `picks.json` — add `"breaking": true` or `"breaking": false` to any `lead`, `pinned`, or `rewrites` entry:
```json
"rewrites": {"https://publisher.com/story": {"breaking": false}}
```
`"breaking": true` forces red even for an opinion piece (your call as editor); `false` turns red off.
Editor-forced red always shows and **does count toward the cap of 2**: with one editor red, at most one
automatic red is added; with two, none. (Satire stays black even with `"breaking": true`.)

## Adding or removing a feed
Add an entry to `feeds.json` → `feeds`: `{"name": "Outlet", "url": "https://…/rss", "column": 2, "lead": false}`.
Set `"enabled": false` to switch one off. Run `python3 build.py` and check the feed report it prints.

## Ground rules (copyright & trust)
- Headline + link only. Never copy article text or images.
- Rewrites must be true to the linked story. When in doubt, use the publisher's headline.
- Styling is original CSS in `render()` inside `build.py`, in the classic Drudge-style look: big bold uppercase
  sans-serif lead headline (plus sub-leads) on top, a heavy italic "DAILY RUSH REPORT" masthead set in system
  fonts (text, not an image), then three columns of bold Courier-style headlines split by thin rules, outlet in
  parentheses after each link, a SOURCES list at the foot of column 3. Black on white; the only colour is red for
  breaking headlines. No logos, photos, ads or copied content from any other site. Satire items read "SATIRE:".
  Underlines: one switch — `--link-underline` at the top of the `<style>` block (`underline` or `none`).
  The `hot` flag in picks.json is still accepted but doesn't change colour.
  Do not use other sites' logos/images or microphone imagery associated with Rush Limbaugh/EIB.
