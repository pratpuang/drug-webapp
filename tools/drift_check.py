#!/usr/bin/env python3
"""
Drift sweep (SLIDE-JUMP-FIX-SPEC.md Part 4) — MEASUREMENT ONLY.

For every drug that appears in a `.block drugs` table on 2+ different wiki
pages, diff what its surrounding ADR / dose / caution / interaction blocks
say about it across those pages, and report where the lists disagree.

This script does NOT edit any wiki content. It only prints a report so Prat
can reconcile the real drift by hand.

Source pages: Knowledge Wiki/wiki/study/{drug-class,cpt,physio,patho}-*.md
(only drug-class-* and cpt-* actually carry `.block drugs` tables today —
physio/patho pages link back to drug pages via crosslink pills instead of
repeating drug data, so they are globbed for completeness but contribute
nothing; see Knowledge Wiki/CLAUDE.md Part D).

Usage:
    python tools/drift_check.py            # print the report
    python tools/drift_check.py --wiki-dir PATH   # override the wiki location
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

# Windows consoles default to a Thai/legacy codepage that can't encode most
# of this report (Thai body text, arrows, emoji markers) - force UTF-8 so a
# plain `python tools/drift_check.py` doesn't crash on its own output.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

REPO_ROOT = Path(__file__).resolve().parent.parent
# The wiki lives in a sibling project folder: .../AI STUFF/Knowledge Wiki
WORKSPACE_ROOT = REPO_ROOT.parents[1]
DEFAULT_WIKI_STUDY = WORKSPACE_ROOT / "Knowledge Wiki" / "wiki" / "study"

GLOBS = ["drug-class-*.md", "cpt-*.md", "physio-*.md", "patho-*.md"]

BLOCK_RE = re.compile(
    r'<div class="block (drugs|adr|inter|caution)" markdown="1">'
)
HEADING_RE = re.compile(r'^# (.+)$', re.MULTILINE)
BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
# strip a trailing "*(ARA)*" / "(PO)" / "(NCC)" aside after a drug name
ASIDE_RE = re.compile(r'\s*\*?\([^)]*\)\*?\s*$')


def extract_block(text, start):
    """text[start] is right after the opening <div ...> tag. Return the
    block's inner content and the index just past its matching </div>,
    tracking nesting depth so a block that itself contains other <div>s
    (none of drugs/adr/inter/caution do in practice, but this stays
    correct either way) is not truncated early."""
    depth = 1
    i = start
    tag_re = re.compile(r'<div\b|</div>')
    for m in tag_re.finditer(text, start):
        if m.group() == '</div>':
            depth -= 1
            if depth == 0:
                return text[start:m.start()], m.end()
        else:
            depth += 1
    return text[start:], len(text)


def split_sections(text):
    """Split a page into (title, body) chunks on H1 headings. Content
    before the first H1 (if any) is kept under title '(page top)'."""
    heads = list(HEADING_RE.finditer(text))
    if not heads:
        return [("(page top)", text)]
    sections = []
    if heads[0].start() > 0:
        sections.append(("(page top)", text[:heads[0].start()]))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        sections.append((h.group(1).strip(), text[h.start():end]))
    return sections


def clean_drug_name(cell):
    """One first-column cell of a drugs table may hold several drug names
    ('**Ramipril** · **Benazepril** · **Quinapril**') or one with a bolded
    class aside ('**Spironolactone** *(ARA)*'). Return a list of plain
    names."""
    names = []
    for part in cell.split('·'):
        part = part.strip()
        if not part:
            continue
        bolds = BOLD_RE.findall(part)
        if bolds:
            names.extend(b.strip() for b in bolds)
        else:
            plain = ASIDE_RE.sub('', part).strip(' *')
            if plain:
                names.append(plain)
    # drop a lone trailing aside like "(ARA)" that wasn't attached to a bold name
    return [ASIDE_RE.sub('', n).strip() for n in names if ASIDE_RE.sub('', n).strip()]


def parse_drugs_table(block_text):
    """Return [(drug_name, extra_cols_dict), ...] for one .block drugs table.
    extra_cols holds any column beyond 'drug' / 'what it treats' (e.g. dose,
    watch out for), keyed by lowercased header, attributed per row."""
    lines = [l for l in block_text.splitlines() if l.strip().startswith('|')]
    if len(lines) < 2:
        return []
    header = [c.strip().lower() for c in lines[0].strip('|').split('|')]
    if not header or header[0] != 'drug':
        return []  # not a proper drugs table (shipping rule #4 header check)
    rows = lines[2:] if len(lines) > 1 and set(lines[1].replace('|', '').strip()) <= set('-: ') else lines[1:]
    out = []
    for row in rows:
        cells = [c.strip() for c in row.strip('|').split('|')]
        if len(cells) != len(header):
            continue
        extras = {header[i]: cells[i] for i in range(1, len(header)) if header[i] != 'what it treats'}
        for name in clean_drug_name(cells[0]):
            out.append((name, extras))
    return out


HEADING2_RE = re.compile(r'^##\s+(.+)$')


def parse_fact_block(block_text, drug_names):
    """For a .block adr / caution / inter block, return {drug_name: [(bold_terms_frozenset, snippet), ...]}
    for every bullet/table-row that explicitly names one of this section's
    drugs. Bullets that mention no specific drug (pure class-wide facts)
    are skipped - they aren't attributable to one drug and so aren't
    comparable across a different page's take on the SAME drug.

    Exception: if the block's OWN '## heading' names exactly one of the
    section's drugs ('## Adverse effects - tramadol specifically'), the
    author has already scoped the whole block to that drug and its bullets
    don't need to keep repeating the name - so every bullet/row in it is
    attributed to that one drug, not just the ones that happen to name it."""
    result = defaultdict(list)
    if not drug_names:
        return result
    # sort longest-first so "Spironolactone" doesn't get shadowed by a
    # coincidental short substring of another listed drug
    sorted_names = sorted(drug_names, key=len, reverse=True)
    name_res = {n: re.compile(r'\b' + re.escape(n) + r'\b', re.IGNORECASE) for n in sorted_names}

    scoped_drug = None
    for line in block_text.splitlines():
        m = HEADING2_RE.match(line.strip())
        if m:
            named = {n for n in sorted_names if name_res[n].search(m.group(1))}
            if len(named) == 1:
                scoped_drug = next(iter(named))
            break

    if scoped_drug:
        table_lines = [l for l in block_text.splitlines() if l.strip().startswith('|')]
        table_body = set()
        if len(table_lines) >= 2 and set(table_lines[1].replace('|', '').strip()) <= set('-: '):
            table_body = set(table_lines[2:])  # exclude header + separator rows
        for line in block_text.splitlines():
            s = line.strip()
            is_bullet = s.startswith('- ')
            is_table_row = line in table_body
            if not (is_bullet or is_table_row):
                continue
            terms = frozenset(t.strip().lower() for t in BOLD_RE.findall(s))
            result[scoped_drug].append((terms, s))
        return result

    # bullets ("- ...", possibly multi-line indented continuations are rare
    # in this corpus - treat each '- ' line as one fact)
    for line in block_text.splitlines():
        s = line.strip()
        if not s.startswith('- '):
            continue
        matched = [n for n in sorted_names if name_res[n].search(s)]
        if not matched:
            continue
        terms = frozenset(t.strip().lower() for t in BOLD_RE.findall(s))
        for n in matched:
            result[n].append((terms, s))

    # table rows (.block inter is sometimes a table: | agent | what happens |)
    lines = [l for l in block_text.splitlines() if l.strip().startswith('|')]
    if len(lines) >= 2:
        header = [c.strip().lower() for c in lines[0].strip('|').split('|')]
        body = lines[2:] if len(lines) > 1 and set(lines[1].replace('|', '').strip()) <= set('-: ') else lines[1:]
        for row in body:
            cells = [c.strip() for c in row.strip('|').split('|')]
            if len(cells) != len(header):
                continue
            row_text = ' | '.join(cells)
            matched = [n for n in sorted_names if name_res[n].search(row_text)]
            if not matched:
                continue
            terms = frozenset(t.strip().lower() for t in BOLD_RE.findall(row_text))
            for n in matched:
                result[n].append((terms, row_text))
    return result


def norm_key(name):
    """Canonical key for grouping the same drug across pages/spellings."""
    return re.sub(r'[^a-z0-9]+', '', name.lower())


def fragment_set(text):
    """Normalize a dose/'watch out for' cell into a comparable set of
    fragments - split on the usual list separators, strip markdown bold and
    whitespace, lowercase. This is deliberately coarser than an exact-string
    compare: 'GERD 1-2 tabs; peptic ulcer 2-4 tabs' and 'peptic ulcer 2-4
    tabs; GERD 1-2 tabs' are the same two facts in a different order, and a
    raw string compare would flag that as drift when it isn't."""
    frags = re.split(r'[;·]', BOLD_RE.sub(r'\1', text))
    return frozenset(f.strip().lower().rstrip('.') for f in frags if f.strip())


def scan_wiki(wiki_study_dir):
    """Returns {norm_key: {'names': set(display names),
                            'occurrences': [{'page', 'section', 'dose', 'facts': {block_type: [(terms, snippet), ...]}}, ...]}}"""
    drugs = defaultdict(lambda: {'names': set(), 'occurrences': []})
    files = []
    for pattern in GLOBS:
        files.extend(sorted(wiki_study_dir.glob(pattern)))
    files = sorted(set(files))

    for path in files:
        text = path.read_text(encoding='utf-8')
        for title, body in split_sections(text):
            blocks = {}  # type -> raw content
            for m in BLOCK_RE.finditer(body):
                btype = m.group(1)
                content, _end = extract_block(body, m.end())
                blocks.setdefault(btype, []).append(content)
            if 'drugs' not in blocks:
                continue
            for drugs_block in blocks['drugs']:
                rows = parse_drugs_table(drugs_block)
                if not rows:
                    continue
                section_drug_names = [r[0] for r in rows]
                facts_by_type = {}
                for btype in ('adr', 'caution', 'inter'):
                    for block_content in blocks.get(btype, []):
                        parsed = parse_fact_block(block_content, section_drug_names)
                        for name, items in parsed.items():
                            facts_by_type.setdefault(name, {}).setdefault(btype, []).extend(items)
                for name, extras in rows:
                    key = norm_key(name)
                    drugs[key]['names'].add(name)
                    drugs[key]['occurrences'].append({
                        'page': path.name,
                        'section': title,
                        'dose': extras,
                        'facts': facts_by_type.get(name, {}),
                    })
    return drugs


def diff_occurrences(occurrences):
    """Given all occurrences of one drug (across possibly several pages),
    return a list of drift findings: {'kind', 'block'/'col', 'a': {...}, 'b': {...}}.
    Only pairs on DIFFERENT pages are compared - two sections on the SAME
    page are just that page's own organisation, not cross-page drift."""
    findings = []
    for i in range(len(occurrences)):
        for j in range(i + 1, len(occurrences)):
            a, b = occurrences[i], occurrences[j]
            if a['page'] == b['page']:
                continue

            # --- dose / extra table columns (exact-string compare) ---
            common_cols = set(a['dose']) & set(b['dose'])
            for col in common_cols:
                va, vb = a['dose'][col].strip(), b['dose'][col].strip()
                if va and vb and fragment_set(va) != fragment_set(vb):
                    findings.append({
                        'kind': 'dose', 'col': col,
                        'a': {'page': a['page'], 'section': a['section'], 'text': va},
                        'b': {'page': b['page'], 'section': b['section'], 'text': vb},
                    })

            # --- adr / caution / inter (bold-term set compare) ---
            for btype in ('adr', 'caution', 'inter'):
                fa, fb = a['facts'].get(btype), b['facts'].get(btype)
                if not fa or not fb:
                    continue  # named on only one side - a coverage gap, not a conflict; skip (measurement stays conservative)
                terms_a = frozenset().union(*[t for t, _ in fa])
                terms_b = frozenset().union(*[t for t, _ in fb])
                if terms_a != terms_b:
                    findings.append({
                        'kind': btype,
                        'a': {'page': a['page'], 'section': a['section'],
                              'text': '; '.join(s for _, s in fa)},
                        'b': {'page': b['page'], 'section': b['section'],
                              'text': '; '.join(s for _, s in fb)},
                        'only_a': sorted(terms_a - terms_b),
                        'only_b': sorted(terms_b - terms_a),
                    })
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--wiki-dir', type=Path, default=DEFAULT_WIKI_STUDY,
                     help='override the wiki/study directory to scan')
    args = ap.parse_args()

    if not args.wiki_dir.is_dir():
        print(f"Wiki study dir not found: {args.wiki_dir}")
        raise SystemExit(1)

    drugs = scan_wiki(args.wiki_dir)

    candidates = {k: v for k, v in drugs.items()
                  if len({o['page'] for o in v['occurrences']}) >= 2}

    print(f"Scanned {args.wiki_dir}")
    print(f"Drugs found in .block drugs tables: {len(drugs)}")
    print(f"Drugs appearing on 2+ pages (drift candidates): {len(candidates)}")
    print()

    total_findings = 0
    drugs_with_drift = 0
    for key in sorted(candidates, key=lambda k: sorted(candidates[k]['names'])[0].lower()):
        v = candidates[key]
        findings = diff_occurrences(v['occurrences'])
        if not findings:
            continue
        drugs_with_drift += 1
        total_findings += len(findings)
        display = ' / '.join(sorted(v['names']))
        pages = sorted({o['page'] for o in v['occurrences']})
        print(f"## {display}  ({', '.join(pages)})")
        for f in findings:
            if f['kind'] == 'dose':
                print(f"  [dose:{f['col']}] {f['a']['page']} ({f['a']['section']}): {f['a']['text']}")
                print(f"           vs {f['b']['page']} ({f['b']['section']}): {f['b']['text']}")
            else:
                print(f"  [{f['kind']}] {f['a']['page']} ({f['a']['section']}) says: {f['a']['text']}")
                print(f"        {f['b']['page']} ({f['b']['section']}) says: {f['b']['text']}")
                if f['only_a']:
                    print(f"        only on {f['a']['page']}: {', '.join(f['only_a'])}")
                if f['only_b']:
                    print(f"        only on {f['b']['page']}: {', '.join(f['only_b'])}")
        print()

    print(f"--- {total_findings} drift findings across {drugs_with_drift} drugs "
          f"(of {len(candidates)} candidates on 2+ pages) ---")


if __name__ == '__main__':
    main()
