# Slide-jump fix — spec (locked 2026-08-31, grilled with Prat)

Source of the problem: Warin's fresh-eyes audit (2026-08-31). Header→slide opens the WRONG deck
~1 in 4 app-wide (physio 55% · principles 45% · CPT-disease 38% · drug-class 25% · patho 13% ·
32-symptom 0%). Cause: a TF-IDF word-frequency ranker with only a *soft* 2× home-deck multiplier —
a nudge, not a rule, so any deck that out-mentions the home deck by >2× wins, wrong subject and all.
Raw crawl of all 1795 headings + rankings saved by Warin (grep for more cases).

## Part 1 — Header → slide ranking

1. **Hard subject-gate.** A heading may only open decks whose manifest `subject` == the subject of
   its page's home deck (page → `sources:`/`data-sh` → deck → `subject`; every deck carries an
   explicit `"subject"` field, 160 decks). This kills every cross-SUBJECT disaster (renal physiology
   → antibiotics; UGIB clinical → pharmacokinetics), which are the worst offenders.
2. **Within-subject: key-term requirement.** The winning in-subject deck must actually contain the
   heading's key term(s); rank by term specificity, not raw mention count. This fixes the
   same-subject wrong picks the gate can't touch (e.g. CCB page "Comparison & Clinical Use" → ACE/ARB
   deck — both antihypertensive pharmacology). Home-deck 2× boost stays.
3. **Two-tier fallback** when no confident in-subject match:
   - confident match → open it;
   - else, if the **home deck** is the page's own source → open the home deck (right lecture, use the
     page-flipper) — never a lying button, it's where the page came from;
   - else (truly slide-less **scaffolding/synthesis** headings, e.g. "TWO GOALS, TWO DRUG SETS") →
     **unclickable** (no button). Extends the existing scaffolding-heading suppression.
4. **Cross-subject exceptions = curated, not automatic.** After build, the verification sweep lists
   every heading that LOST a cross-subject pick under the gate. Prat/Matcha review that list; only the
   genuinely-right ones get an explicit **`link_overrides`** entry (the codebase already uses this
   pattern). Default is the wall; the handful of real cross-subject links are added back by hand.

### Verification (mandatory, per Knowledge Wiki shipping rules)
- Re-run the ranking over **all 1795 headings** using the app's OWN shipped functions (not a
  re-implementation), diff new #1 vs old #1. Confirm: wrong-rate drops hard, AND the *correct*
  "home≠top" finer-deck cases (e.g. Cardiac-function → dedicated cardiac deck) do NOT regress.
- Report every heading that changed, both directions; eyeball a sample of each.
- ⚠️ ~half of "home≠top" in the audit were the algo being RIGHT (a more specific same-subject deck) —
  the fix must preserve those.

### Related (NOT in scope unless Prat says so)
The wiki notes drug→slide links (`SLIDE_INDEX`) have the same mention-count bug. Prat scoped this job
to HEADINGS. Flag drug→slide as a follow-up; don't fold it in silently.

## Part 2 — Slide viewer (the weird half-zoom)

Clicking a slide currently zooms awkwardly inside its panel box. Replace with a **full-screen
lightbox**: pops out of the panel, fits the whole slide to the screen, then **zoom + pan** inside it
(pinch on iPad/phone, scroll-or-click to zoom on PC, drag to pan), esc / tap-outside closes. Must
work on phone, iPad and desktop — these are lecture slides with small Thai text he needs to read, so
fit-to-screen alone is not enough.
- Verify in real Chrome (CDP) at desktop AND narrow/mobile width before shipping.

## Part 3 — Confidence signal on slide matches (QoL, rides the panel)

When the best in-subject match is weak (low score / backed by ~1 mention — i.e. near the "no
confident match" boundary of Part 1's two-tier fallback), do NOT present it bolded/solid. Show a
subtle **"weak match"** tell in the slide panel so Prat can self-filter thin picks. Same surface as
the lightbox rebuild; reuse the confidence threshold from Part 1's fallback so the two agree.

## Part 4 — Drift sweep (lane D consistency linter) — MEASUREMENT ONLY

A standalone script (`tools/`) that reads the **wiki source pages** (`Knowledge Wiki/wiki/study/
drug-class-*.md` + physio/patho/cpt pages — that's where the drift is authored, not index.html) and,
for every drug appearing on 2+ pages (~153 candidates), **diffs its ADR / dose / caution / interaction
lists across pages.** Output: a report listing each drug with divergent lists and exactly which page
says what.
- 🔴 **It does NOT edit content.** It flags drift for Prat to reconcile. The audit only *confirmed* 3
  drift cases (spironolactone, tramadol, carbamazepine) and left ≥4 unchecked (clonazepam,
  ketoconazole, prednisolone, diltiazem) — this gets the TRUE count before we pick the big
  single-source fix (lanes A/B/C, deferred).
- Reuse the drug-row parsing shape already proven (first column of `class="block drugs"` tables).

## Ship
Commit as Prat **incrementally, one part per commit** (ranking → lightbox+confidence → drift linter),
no push until Prat authorizes / verifies. Then curl the deployed URL and grep for a
string only the new build contains before calling it live (shipping rule #1).
