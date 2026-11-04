#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Serve the brainstorming technique library without loading it all into context.

The library is a CSV (category, technique_name, description, detail). `description`
is a short gist — enough to propose and run most techniques. `detail` is optional:
a path (relative to the CSV's directory) to a fuller instruction file for a technique
complex enough to warrant one. Only `show` resolves detail files, and only for the
technique asked for — so the heavy material never enters context until it is run.

Commands:
  categories                  list category names + counts (the cheap entry point)
  list --category C [...]      the index (name + gist) for those categories
  list --all                  the whole index at once — deliberate; large, avoid interactively
  show NAME [NAME ...]         full gist for each, inlining its detail file if it has one
  random [--category C] [-n N]  pick N at random (optionally within categories)
  html --out PATH             write the offline 'browse all' selection page to a file

`list` refuses to run with neither --category nor --all, and `html` writes to a file
rather than stdout: dumping the full catalog into context is a footgun, so reaching the
whole library at once must always be an explicit, deliberate choice.

`--extra PATH` merges a JSON overlay of additional techniques (customize.toml's
`additional_techniques`) into every command. An extra whose technique_name matches
a shipped row (case-insensitive) REPLACES it — retune a shipped technique; others
append, so custom techniques and whole new categories are first-class everywhere —
including the browse page and category draws. (Same overlay semantics as
bmad-advanced-elicitation's pick_methods.py.)

Default output is lean text for an LLM to read; pass --json for structured output.
"""
import argparse
import csv
import hashlib
import html
import json
import random
import sys
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "assets" / "brain-methods.csv"
FIELDS = ("category", "technique_name", "description", "detail", "provenance", "good_for", "audience")
# Optional columns beyond the original four — absent in older CSVs and in --extra
# overlays, so always read through .get/setdefault. `provenance` (classic|signature|
# playful) drives the "Proven & Professional" lead group; `good_for` (a |-separated
# list of goal tags) drives the browse page's goal filter; `audience` (solo|group|either)
# is advisory.
OPTIONAL_FIELDS = ("detail", "provenance", "good_for", "audience")

# --- i18n: an optional language pack ---------------------------------------
# The catalog, the page chrome and the composed prompt are English. A project
# that speaks another language ships a *pack* — a JSON file of translated text
# named `brain-i18n.json` — and every reader-visible string comes from it: the
# page chrome, the technique names and descriptions, the category and goal
# labels, and the prose of the composed prompt.
#
# A pack is found beside the catalog it translates, so the two travel together.
# A project may also keep one in `_bmad/custom/brainstorming/`, the one place
# no installer ever rewrites — a language there survives a reinstall of the
# skill. The shipped catalog has no pack beside it and is never translated by
# a project's pack, so it keeps its English page exactly as before.
#
# A pack never changes a technique's identity: the English `technique_name`
# stays the key the icon sidecar and the `show`/`list` lookups use. On the page
# the translated name simply takes its place, so selection, de-duplication and
# the chip jump-nav keep working in any language.
#
# The composed prompt keeps its machine-readable anchors in English on purpose
# — the skill parses the pasted block for `Facilitation mode:`, `(random pick)`,
# `invent N` and `you choose N` (see SKILL.md) — so a pack translates the prose
# around them without breaking the handover. Those anchors are not overridable.

LANG_FILE_NAME = "brain-i18n.json"

# The English text is the shipped behavior; a pack replaces individual entries.
# Anything a pack omits keeps the English text, so a partial pack is still
# usable.
DEFAULT_STRINGS = {
    # document
    "LANG": "en",
    "TITLE": "BMad Method Brainstorming Selection",
    "L_THEME": "Toggle dark mode",
    "SUB_A": "Compose your session, hit ",
    "SUB_B": ", and paste it back into the chat to begin.",
    "COPY_PROMPT": "Copy prompt",
    # composer
    "L_FACILITATION": "Facilitation",
    "MODE_FACILITATOR": "Facilitator",
    "MODE_CREATIVE_PARTNER": "Creative Partner",
    "MODE_IDEATE": "Ideate for me",
    "L_TECHNIQUES": "Techniques",
    "L_PICKED": "Picked",
    "L_RANDOM": "Random",
    "L_INVENT": "Invent",
    "L_AI_PICKS": "AI picks",
    "L_TOTAL_0": "Total 0 &middot; 3&ndash;4 is the sweet spot",
    "L_JUMP_TO": "Jump to",
    "L_GREAT_FOR": "Great for",
    "BANNER_OK": "&#10003; Copied! Now paste it into the chat to start your session.",
    "FOOTER": "BMad Method &middot; Brainstorming",
    # mode hints
    "HINT_FACILITATOR": "A forcing function for your ideas — I prompt and push, but never supply them.",
    "HINT_CREATIVE_PARTNER": "We riff together — I facilitate and add ideas too, each logged as yours or mine.",
    "HINT_IDEATE": "I run the whole session myself, then show you the result and offer to keep going.",
    # live counter
    "L_TOTAL_PREFIX": "Total ",
    "L_TOTAL_SUFFIX": " · 3–4 is the sweet spot",
    # the composed prompt — the protocol lines stay English (the skill parses them)
    "P_OPEN": "Let's run my brainstorming session.",
    "P_MODE": "Facilitation mode: ",
    "P_TECHNIQUES": "Techniques to use:",
    "P_RANDOM_PICK": "random pick",
    "P_INV_A": "invent ",
    "P_INV_B": " brand-new technique",
    "P_INV_PLURAL": "s",
    "P_INV_C": " on the fly",
    "P_SPIRIT": "invent 1 new technique in the spirit of ",
    "P_CH_A": "you choose ",
    "P_CH_B": " more technique",
    "P_CH_PLURAL": "s",
    "P_CH_C": " that fit my goal",
    "P_THEN": "Then: ",
    "P_AND": "; and ",
    "P_FALLBACK_A": "Run the whole session yourself — pick the techniques, generate the ideas, then show me the result.",
    "P_FALLBACK_B": "Help me choose 3–4 techniques to start.",
    "P_PROMPT_BOX": "Copy this, then paste it into the chat:",
    # clipboard banner (rendered as HTML)
    "BANNER_OK_JS": "✓ Copied! Now paste it into the chat to start your session.",
    "BANNER_FAIL": "⚠ Couldn’t reach the clipboard — copy the text in the box, then paste it into the chat.",
    # page furniture (substituted from Python, not from the template)
    "CLASSIC_GROUP": "Proven & Professional",
    "MORE_GROUP": "More",
    "GOOD_FOR_PREFIX": "Great for: ",
    "GOOD_FOR_SEP": " · ",
    "INVENT_TITLE": "✨ Invent a {cat} technique",
    "INVENT_DESC": "Make up a brand-new technique on the fly, in the spirit of {cat}",
    "TOTAL_SUMMARY": "{n} techniques across {m} categories.",
}

# The skill parses the pasted prompt for these literals (SKILL.md), so a pack
# may not change them — only the prose around them is translatable.
PROTOCOL_KEYS = frozenset(
    {
        "P_MODE", "P_RANDOM_PICK",
        "P_INV_A", "P_INV_B", "P_INV_PLURAL", "P_INV_C", "P_SPIRIT",
        "P_CH_A", "P_CH_B", "P_CH_PLURAL", "P_CH_C",
    }
)

# Where a string lands decides how it must be escaped. A JS string literal
# needs its quotes escaped; an attribute needs the full escape; HTML text
# keeps its entities so `&middot;` and `&#10003;` survive.
_JS_SINGLE_KEYS = frozenset(
    {
        "HINT_FACILITATOR", "HINT_CREATIVE_PARTNER", "HINT_IDEATE",
        "L_TOTAL_PREFIX", "L_TOTAL_SUFFIX",
        "P_MODE", "P_TECHNIQUES", "P_RANDOM_PICK",
        "P_INV_A", "P_INV_B", "P_INV_PLURAL", "P_INV_C", "P_SPIRIT",
        "P_CH_A", "P_CH_B", "P_CH_PLURAL", "P_CH_C",
        "P_THEN", "P_AND", "P_FALLBACK_A", "P_FALLBACK_B", "P_PROMPT_BOX",
        "BANNER_OK_JS", "BANNER_FAIL",
    }
)
_JS_DOUBLE_KEYS = frozenset({"P_OPEN"})
_HTML_ATTR_KEYS = frozenset({"LANG", "L_THEME"})


def find_project_root(start: Path) -> Path | None:
    """Nearest ancestor holding `_bmad/` — the same marker the customization
    resolver uses, so a path token resolves to the project the session runs in."""
    current = start.resolve()
    while True:
        if (current / "_bmad").is_dir():
            return current
        if current.parent == current:
            return None
        current = current.parent


def skill_root() -> Path:
    """The skill directory this script belongs to."""
    return Path(__file__).resolve().parent.parent


def expand_path(value) -> Path:
    """Resolve a path argument, expanding the customization tokens.

    `{project-root}` and `{skill-root}` are the tokens the customization layer
    uses; a value carrying one is resolved here so a catalog override can be
    written as a portable, project-anchored path.
    """
    text = str(value)
    if "{project-root}" in text:
        root = find_project_root(Path.cwd()) or find_project_root(skill_root())
        if root is not None:
            text = text.replace("{project-root}", str(root))
    text = text.replace("{skill-root}", str(skill_root()))
    return Path(text).expanduser()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def pack_candidates(csv_file: Path) -> list[Path]:
    """Where a language pack for this catalog may live, nearest first.

    Beside the catalog it applies to — the two travel together, and a catalog
    copied into a project brings its own translation with it.

    Then the project's own custom layer, `_bmad/custom/brainstorming/` — the
    one place no installer ever rewrites, so a language there survives a
    reinstall of the skill. It applies to any catalog the project owns (one
    under the project root) but not to the skill's own shipped catalog: that
    one ships with an English page, and the skill's test compares the shipped
    asset against a fresh English render.
    """
    csv_file = csv_file.resolve()
    candidates = [csv_file.parent / LANG_FILE_NAME]
    if csv_file == DEFAULT_FILE.resolve():
        return candidates
    root = find_project_root(Path.cwd()) or find_project_root(skill_root())
    if root is None or not _is_inside(csv_file, root):
        return candidates
    candidate = root / "_bmad" / "custom" / "brainstorming" / LANG_FILE_NAME
    if candidate not in candidates:
        candidates.append(candidate)
    return candidates


def load_pack(csv_file: Path) -> dict:
    """The language pack for a catalog.

    A missing pack is the normal case (English). A malformed one is reported to
    stderr and treated as absent — a broken translation must never stop the
    page from rendering.
    """
    for path in pack_candidates(csv_file):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            print(f"# language pack could not be read: {path}: {error}", file=sys.stderr)
            return {}
        if not isinstance(data, dict):
            print(f"# language pack is not a JSON object: {path}", file=sys.stderr)
            return {}
        return data
    return {}


def pack_strings(pack: dict | None) -> dict[str, str]:
    """The pack's string overrides, over the English defaults.

    The protocol anchors are deliberately not overridable: the skill parses the
    pasted prompt for them.
    """
    strings = dict(DEFAULT_STRINGS)
    for key, value in ((pack or {}).get("strings") or {}).items():
        if key in PROTOCOL_KEYS:
            continue
        if key in DEFAULT_STRINGS and isinstance(value, str):
            strings[key] = value
    return strings


def pack_goal_labels(pack: dict | None) -> dict[str, str]:
    """The pack's goal labels, over the shipped ones."""
    labels = dict(GOAL_LABELS)
    for key, value in ((pack or {}).get("goal_labels") or {}).items():
        if isinstance(value, str):
            labels[key] = value
    return labels


def display_category(cat: str, pack: dict | None) -> str:
    """The translated display name of a category slug (English when untranslated)."""
    categories = (pack or {}).get("categories") or {}
    value = categories.get(cat)
    return value if isinstance(value, str) and value else pretty(cat)


def escape_js(value: str, quote: str) -> str:
    """Escape a string for a JavaScript string literal.

    `<` and `>` become escapes so a translated string can never close the
    surrounding `<script>` block.
    """
    out = value.replace("\\", "\\\\").replace(quote, "\\" + quote)
    out = out.replace("<", "\\u003c").replace(">", "\\u003e")
    return out.replace("\r", "\\r").replace("\n", "\\n")


def escape_html_text(value: str) -> str:
    """Escape a string for an HTML text node.

    `&` is left alone on purpose: the shipped strings carry entities
    (`&middot;`, `&ndash;`, `&#10003;`) and a pack may too. `<` and `>` are
    escaped (as numeric references) so a translated string cannot break out of
    its element.
    """
    return value.replace("<", "&#60;").replace(">", "&#62;")


def escape_string(key: str, value: str) -> str:
    """Escape a translated string for the context it is substituted into."""
    if key in _JS_DOUBLE_KEYS:
        return escape_js(value, '"')
    if key in _JS_SINGLE_KEYS:
        return escape_js(value, "'")
    if key in _HTML_ATTR_KEYS:
        return html.escape(value, quote=True)
    return escape_html_text(value)


def apply_pack(rows: list[dict], pack: dict | None) -> list[dict]:
    """Stamp translated display fields onto a copy of the rows.

    The English name stays the technique's identity; `display_*` is what a
    reader sees. A technique the pack does not translate keeps its English
    display, so a partial pack is still usable.
    """
    if not pack:
        return rows
    techniques = pack.get("techniques") or {}
    out: list[dict] = []
    for r in rows:
        r2 = dict(r)
        r2["display_category"] = display_category(r["category"], pack)
        entry = techniques.get(r["technique_name"])
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            entry = {}
        name = entry.get("name")
        description = entry.get("description")
        r2["display_name"] = name if isinstance(name, str) and name else r["technique_name"]
        r2["display_description"] = (
            description if isinstance(description, str) and description else r["description"]
        )
        out.append(r2)
    return out



def load(file: Path) -> list[dict]:
    # utf-8-sig: tolerate BOM-prefixed catalogs (Excel "CSV UTF-8", Notepad)
    with open(file, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FIELDS:
            r.setdefault(k, "")
            r[k] = (r.get(k) or "").strip()
    return rows


def load_extra(file: Path) -> list[dict]:
    """Merge-in techniques from a JSON overlay — a list of
    {category, technique_name, description[, detail]} objects. This is how
    customize.toml's `additional_techniques` become first-class across *every*
    subcommand (categories/list/random/show/html), so the browse page and
    category draws include them too, not just the in-chat flows."""
    data = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError("--extra must be a JSON array of objects")
    rows = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"each --extra entry must be a JSON object, got: {item!r}")
        rows.append({
            "category": str(item.get("category", "")).strip(),
            "technique_name": str(item.get("technique_name", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "detail": str(item.get("detail") or "").strip(),
            "provenance": str(item.get("provenance") or "").strip(),
            "good_for": str(item.get("good_for") or "").strip(),
            "audience": str(item.get("audience") or "").strip(),
        })
    return rows


def merge_extra(rows: list[dict], extras: list[dict]) -> list[dict]:
    """Extras replace a catalog row with the same technique_name (case-insensitive),
    otherwise append — the same overlay semantics as pick_methods.py, so
    customize.toml additional_* entries behave identically across sibling skills."""
    merged = list(rows)
    index = {r["technique_name"].lower(): i for i, r in enumerate(merged)}
    for e in extras:
        key = e["technique_name"].lower()
        if key in index:
            merged[index[key]] = e
        else:
            index[key] = len(merged)
            merged.append(e)
    return merged


def categories(rows: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return sorted(counts.items())


def filter_cats(rows: list[dict], cats: list[str] | None) -> list[dict]:
    if not cats:
        return rows
    wanted = {c.lower() for c in cats}
    return [
        r for r in rows
        if r["category"].lower() in wanted
        or (r.get("display_category") and r["display_category"].lower() in wanted)
    ]


def find(rows: list[dict], names: list[str]) -> tuple[list[dict], list[str]]:
    by_name = {r["technique_name"].lower(): r for r in rows}
    by_display = {r["display_name"].lower(): r for r in rows if r.get("display_name")}
    found, missing = [], []
    for n in names:
        r = by_name.get(n.strip().lower()) or by_display.get(n.strip().lower())
        (found if r else missing).append(r if r else n)
    return found, missing


def resolve_detail(row: dict, csv_dir: Path) -> str | None:
    """Return the contents of a row's detail file, or None if there is no detail
    (or the file is missing — a missing file is reported to stderr, not fatal)."""
    if not row.get("detail"):
        return None
    path = (csv_dir / row["detail"]).resolve()
    if not path.is_file():
        print(f"# detail file not found for {row['technique_name']}: {row['detail']}", file=sys.stderr)
        return None
    return path.read_text(encoding="utf-8").strip()


def fmt_categories(cats: list[tuple[str, int]], as_json: bool) -> str:
    if as_json:
        return json.dumps([{"category": c, "count": n} for c, n in cats])
    return "\n".join(f"{c}\t{n}" for c, n in cats)


def fmt_list(rows: list[dict], as_json: bool) -> str:
    if as_json:
        return json.dumps([{k: r[k] for k in ("category", "technique_name", "description")} for r in rows])
    return "\n".join(f"{r['category']}\t{r['technique_name']}\t{r['description']}" for r in rows)


def fmt_show(rows: list[dict], csv_dir: Path, as_json: bool) -> str:
    if as_json:
        out = []
        for r in rows:
            d = resolve_detail(r, csv_dir)
            entry = {k: r[k] for k in ("category", "technique_name", "description")}
            if d:
                entry["detail"] = d
            out.append(entry)
        return json.dumps(out)
    blocks = []
    for r in rows:
        block = f"## {r['technique_name']}  [{r['category']}]\n{r['description']}"
        d = resolve_detail(r, csv_dir)
        if d:
            block += f"\n\n{d}"
        blocks.append(block)
    return "\n\n".join(blocks)


def pretty(cat: str) -> str:
    """Turn a category slug (e.g. 'speculative_future') into a display name."""
    return cat.replace("_", " ").replace("-", " ").title()


# --- card visuals: a crafted duotone icon + hue per category, plus a per-technique icon ---
# The hues and SVG glyphs are *data*, not logic: they live in the icon sidecar
# (assets/brain-icons.json) so the catalog's visuals can be edited without touching code.
# It maps category slug -> {hue, glyph} and technique name -> svg (inner markup, drawn in
# `currentColor` which the CSS sets to the category hue; the shared CHIP frame is added by
# the renderer). Anything missing falls back here — an unknown category gets a hash-derived
# hue + generic glyph, an unknown/not-yet-iconed technique a neutral mark — so custom
# catalogs always render.

ICON_FILE = DEFAULT_FILE.parent / "brain-icons.json"

CHIP = '<rect x="1.5" y="1.5" width="41" height="41" rx="12" fill="currentColor" fill-opacity="0.12"/>'

_FALLBACK_GLYPH = (
    '<circle cx="22" cy="22" r="11" fill="currentColor" fill-opacity="0.16"/>'
    '<circle cx="22" cy="22" r="11" stroke="currentColor" stroke-width="1.6" fill="none"/>'
    '<circle cx="22" cy="22" r="3.4" fill="currentColor"/>'
)
_FALLBACK_TECH = (
    '<rect x="15" y="15" width="14" height="14" rx="2.5" transform="rotate(45 22 22)" '
    'fill="none" stroke="currentColor" stroke-width="2"/><circle cx="22" cy="22" r="2.4" fill="currentColor"/>'
)


def _load_icons(file: Path = ICON_FILE) -> tuple[dict, dict]:
    """Read the icon sidecar: (category slug -> {hue, glyph}, technique name -> svg).
    A missing or malformed file is non-fatal — everything then uses the fallbacks below."""
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    return (data.get("categories") or {}), (data.get("techniques") or {})


_CATEGORY_STYLES, _TECH_ICONS = _load_icons()


def _hsl_hex(deg: int, s: float, lt: float) -> str:
    import colorsys

    r, g, b = colorsys.hls_to_rgb((deg % 360) / 360, lt, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def category_style(cat: str) -> tuple[str, str]:
    """(hue, glyph markup) for a category — from the sidecar for the shipped set, derived for extras."""
    style = _CATEGORY_STYLES.get(cat)
    if style and style.get("hue"):
        return style["hue"], style.get("glyph") or _FALLBACK_GLYPH
    deg = int(hashlib.md5(cat.encode("utf-8")).hexdigest(), 16) % 360
    return _hsl_hex(deg, 0.58, 0.52), _FALLBACK_GLYPH


def tech_icon(name: str) -> str:
    """The hand-picked line-icon for a specific technique (neutral mark if unknown)."""
    return _TECH_ICONS.get(name, _FALLBACK_TECH)


SELECTOR_TEMPLATE = r"""<!DOCTYPE html>
<html lang="{{LANG}}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<script>
/* set the theme before first paint so there's no light-mode flash */
(function(){ try {
  var t = localStorage.getItem('bmad-theme');
  if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }
  document.documentElement.setAttribute('data-theme', t);
} catch(e){} })();
</script>
<style>
  :root {
    --bg:#f6f7fb; --surface:#fff; --ink:#1c1e2b; --muted:#6b7080;
    --accent:#5b4bdc; --accent-ink:#5b4bdc; --warn:#c0561f;
    --line:#e6e8f0; --control:#eef0f7; --control2:#f1f2f8; --raised:#fff;
    --cnt:#b9bdce; --foot:#aeb2c4; --shadow:rgba(20,20,50,.06);
  }
  :root[data-theme="dark"] {
    --bg:#0f1117; --surface:#171a23; --ink:#e7e9f2; --muted:#9aa0b4;
    --accent:#6d5cf0; --accent-ink:#a99bff; --warn:#e08a4a;
    --line:#2a2f3e; --control:#222634; --control2:#1d212d; --raised:#2c3242;
    --cnt:#5a6076; --foot:#5a6076; --shadow:rgba(0,0,0,.45);
  }
  /* lift the category hue toward white on dark surfaces so deep hues stay legible */
  :root[data-theme="dark"] section > h2 { color:color-mix(in srgb, var(--c) 62%, #fff); }
  :root[data-theme="dark"] .tech .ico { color:color-mix(in srgb, var(--c) 68%, #fff); }
  :root[data-theme="dark"] label.tech:has(input:checked) { border-color:color-mix(in srgb, var(--c) 60%, #fff); }
  .titlerow { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
  .themebtn { flex:none; width:36px; height:36px; border-radius:9px; background:var(--control); color:var(--ink); font-size:17px; line-height:1; display:inline-flex; align-items:center; justify-content:center; }
  .themebtn:hover { background:var(--raised); }
  * { box-sizing:border-box; }
  body { margin:0; font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; background:var(--bg); color:var(--ink); }
  header { position:sticky; top:0; z-index:5; background:var(--surface); padding:20px 0 12px; border-bottom:1px solid var(--line); box-shadow:0 2px 12px var(--shadow); }
  .hwrap { max-width:1120px; margin:0 auto; padding:0 24px; }  /* align header content with the card column on wide screens */
  h1 { margin:0 0 4px; font-size:24px; letter-spacing:-.02em; }
  .sub { margin:0 0 12px; color:var(--muted); font-size:14px; max-width:74ch; }
  button { font:inherit; border:0; border-radius:8px; cursor:pointer; }
  .composer { display:flex; flex-direction:column; gap:9px; margin:6px 0 12px; }
  .grp { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .glabel { font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); min-width:74px; }
  .modes { display:inline-flex; background:var(--control); border-radius:9px; padding:3px; gap:2px; }
  .mode { padding:7px 13px; font-size:14px; font-weight:600; color:var(--muted); background:transparent; }
  .mode.on { background:var(--raised); color:var(--accent-ink); box-shadow:0 1px 3px var(--shadow); }
  .modehint { flex:1 1 240px; min-width:0; font-size:13px; color:var(--muted); font-style:italic; }
  .pill { font-size:13px; color:var(--muted); background:var(--control); padding:6px 12px; border-radius:20px; }
  .pill b { color:var(--accent-ink); }
  .step { display:inline-flex; align-items:center; gap:7px; font-size:13px; color:var(--ink); background:var(--control2); padding:4px 6px 4px 12px; border-radius:20px; }
  .step b { min-width:12px; text-align:center; font-size:14px; color:var(--ink); }
  .step button { width:24px; height:24px; border-radius:50%; background:var(--raised); color:var(--muted); font-size:17px; line-height:22px; text-align:center; box-shadow:0 1px 2px var(--shadow); }
  .step button:hover { color:var(--accent-ink); }
  .total { font-size:12px; color:var(--muted); }
  .total.warn { color:var(--warn); font-weight:600; }
  .bar { display:flex; gap:10px 14px; align-items:center; flex-wrap:wrap; }
  #copy { margin-left:auto; padding:9px 22px; background:var(--accent); color:#fff; font-size:14px; font-weight:700; }
  #copy:hover { filter:brightness(1.07); }
  .chips { flex:1 1 320px; min-width:0; display:flex; gap:7px; flex-wrap:wrap; align-items:center; }
  .chip { font-size:12px; padding:4px 11px; border-radius:16px; border:0; color:#fff; background:var(--cc); font-weight:600; cursor:pointer; }
  .chip:hover { filter:brightness(1.08); }
  .banner { max-height:0; overflow:hidden; transition:max-height .25s ease, padding .22s ease, margin .22s ease; background:linear-gradient(90deg,var(--accent),#8275f2); color:#fff; border-radius:10px; font-weight:700; text-align:center; padding:0 14px; }
  .banner.show { max-height:64px; padding:13px 14px; margin-top:10px; }
  .banner.fail { background:linear-gradient(90deg,var(--warn),#e0894a); }
  main { padding:18px 24px 60px; max-width:1120px; margin:0 auto; }
  section { margin:0 0 26px; }
  section > h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--c); margin:0 0 10px; border-bottom:1px solid color-mix(in srgb, var(--c) 24%, var(--line)); padding-bottom:6px; }
  section > h2 .cnt { color:color-mix(in srgb, var(--c) 45%, var(--cnt)); margin-left:6px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:10px; }
  label.tech { display:flex; gap:12px; align-items:flex-start; background:color-mix(in srgb, var(--c) 5%, var(--surface)); border:1px solid color-mix(in srgb, var(--c) 18%, var(--line)); border-radius:10px; padding:11px 13px; cursor:pointer; transition:border-color .12s, box-shadow .12s, background .12s; }
  label.tech:hover { border-color:color-mix(in srgb, var(--c) 45%, var(--surface)); }
  label.tech input { margin-top:2px; width:17px; height:17px; accent-color:var(--c); flex:none; }
  label.tech:has(input:checked) { border-color:var(--c); background:color-mix(in srgb, var(--c) 12%, var(--surface)); box-shadow:0 0 0 2px color-mix(in srgb, var(--c) 30%, transparent); }
  .tech .ic2 { display:flex; gap:5px; flex:none; }
  .tech .ico { width:40px; height:40px; flex:none; color:var(--c); }
  .tech .n { font-weight:600; display:block; }
  .tech .d { color:var(--muted); font-size:13.5px; display:block; margin-top:2px; }
  .tech .gf { color:var(--accent-ink); font-size:11px; display:block; margin-top:5px; opacity:.85; }
  .grouphdr { margin:30px 0 12px; font-size:12px; text-transform:uppercase; letter-spacing:.14em; font-weight:700; color:var(--c); opacity:.92; border-bottom:1px solid color-mix(in srgb, var(--c) 22%, var(--line)); padding-bottom:7px; }
  main > .grouphdr:first-child { margin-top:2px; }
  :root[data-theme="dark"] .grouphdr { color:color-mix(in srgb, var(--c) 62%, #fff); }
  .goals { display:flex; gap:7px; flex-wrap:wrap; }
  .goal { font-size:12px; padding:5px 12px; border-radius:16px; background:var(--control); color:var(--muted); font-weight:600; }
  .goal:hover { color:var(--ink); }
  .goal.on { background:var(--accent); color:#fff; }
  label.tech.invent { border-style:dashed; background:transparent; }
  label.tech.invent:hover { border-color:var(--c); }
  label.tech.invent .n { color:var(--c); }
  label.tech.hidden { display:none; }
  footer { text-align:center; color:var(--foot); font-size:12px; padding:24px; }
</style>
</head>
<body>
<header>
  <div class="hwrap">
  <div class="titlerow">
    <h1>{{TITLE}}</h1>
    <button id="theme" class="themebtn" type="button" aria-label="{{L_THEME}}" title="{{L_THEME}}"></button>
  </div>
  <p class="sub">{{SUB_A}}<strong>{{COPY_PROMPT}}</strong>{{SUB_B}} {{TOTAL}}</p>

  <div class="composer">
    <div class="grp">
      <span class="glabel">{{L_FACILITATION}}</span>
      <div class="modes" id="modes">
        <button type="button" class="mode on" data-mode="Facilitator">{{MODE_FACILITATOR}}</button>
        <button type="button" class="mode" data-mode="Creative Partner">{{MODE_CREATIVE_PARTNER}}</button>
        <button type="button" class="mode" data-mode="Ideate for me">{{MODE_IDEATE}}</button>
      </div>
      <span class="modehint" id="modehint"></span>
    </div>
    <div class="grp">
      <span class="glabel">{{L_TECHNIQUES}}</span>
      <span class="pill">{{L_PICKED}} <b id="pickN">0</b></span>
      <span class="step">{{L_RANDOM}} <button type="button" data-step="rand" data-d="-1">&minus;</button><b id="randN">0</b><button type="button" data-step="rand" data-d="1">+</button></span>
      <span class="step">{{L_INVENT}} <button type="button" data-step="inv" data-d="-1">&minus;</button><b id="invN">0</b><button type="button" data-step="inv" data-d="1">+</button></span>
      <span class="step">{{L_AI_PICKS}} <button type="button" data-step="ai" data-d="-1">&minus;</button><b id="aiN">0</b><button type="button" data-step="ai" data-d="1">+</button></span>
      <span class="total" id="total">{{L_TOTAL_0}}</span>
      <button id="copy" type="button">{{COPY_PROMPT}}</button>
    </div>
  </div>

  {{GOALBAR}}
  <div class="bar">
    <span class="glabel">{{L_JUMP_TO}}</span>
    <div class="chips" id="chips">{{CHIPS}}</div>
  </div>

  <div class="banner" id="banner">{{BANNER_OK}}</div>
  </div>
</header>
<main>
{{BODY}}
</main>
<footer>{{FOOTER}}</footer>
<script>
(function(){
  var $ = function(id){ return document.getElementById(id); };
  var all = Array.prototype.slice;
  var boxes = all.call(document.querySelectorAll('input[type=checkbox]'));
  var techBoxes = boxes.filter(function(b){ return b.dataset.name; });      // real technique cards
  var inventBoxes = boxes.filter(function(b){ return b.dataset.invent; });  // per-category "invent in the spirit of" cards
  var header = document.querySelector('header');
  var sections = all.call(document.querySelectorAll('section'));
  var state = { mode: 'Facilitator', rand: 0, inv: 0, ai: 0 };
  var MODE_HINTS = {
    'Facilitator': '{{HINT_FACILITATOR}}',
    'Creative Partner': '{{HINT_CREATIVE_PARTNER}}',
    'Ideate for me': '{{HINT_IDEATE}}'
  };
  function setHint(){ $('modehint').textContent = MODE_HINTS[state.mode] || ''; }

  var themeBtn = $('theme');
  function setThemeIcon(){ themeBtn.textContent = document.documentElement.getAttribute('data-theme') === 'dark' ? '☀' : '☾'; }
  themeBtn.addEventListener('click', function(){
    var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('bmad-theme', next); } catch(e){}
    setThemeIcon();
  });

  all.call(document.querySelectorAll('.mode')).forEach(function(b){
    b.addEventListener('click', function(){
      all.call(document.querySelectorAll('.mode')).forEach(function(m){ m.classList.remove('on'); });
      b.classList.add('on');
      state.mode = b.dataset.mode;
      setHint();
    });
  });

  all.call(document.querySelectorAll('[data-step]')).forEach(function(btn){
    btn.addEventListener('click', function(){
      var k = btn.dataset.step, d = parseInt(btn.dataset.d, 10);
      state[k] = Math.max(0, state[k] + d);
      update();
    });
  });

  // Category chips are jump-nav: click one to smooth-scroll its section into view,
  // offsetting by the sticky header's height so the heading isn't hidden beneath it.
  all.call(document.querySelectorAll('.chip')).forEach(function(chip){
    chip.addEventListener('click', function(){
      var sec = null;
      for (var i = 0; i < sections.length; i++){ if (sections[i].dataset.cat === chip.dataset.cat){ sec = sections[i]; break; } }
      if (!sec){ return; }
      var top = sec.getBoundingClientRect().top + window.pageYOffset - header.offsetHeight - 8;
      window.scrollTo({ top: top, behavior: 'smooth' });
    });
  });

  boxes.forEach(function(b){ b.addEventListener('change', update); });

  // A `classic` technique appears twice (lead "Proven & Professional" group + its home
  // category), so de-dupe checked picks by name; the lead copy carries data-lead.
  function checkedTech(){
    var seen = {}, out = [];
    techBoxes.forEach(function(b){
      if (!b.checked || seen[b.dataset.name]) { return; }
      seen[b.dataset.name] = 1;
      out.push(b);
    });
    return out;
  }
  function checkedInvent(){ return inventBoxes.filter(function(b){ return b.checked; }); }

  function update(){
    // rand can't exceed what the pool can supply — keep the counter honest with the draw
    if (state.rand > randomPool().length){ state.rand = randomPool().length; }
    $('pickN').textContent = checkedTech().length;
    $('randN').textContent = state.rand;
    $('invN').textContent = state.inv;
    $('aiN').textContent = state.ai;
    var total = checkedTech().length + state.rand + state.inv + checkedInvent().length + state.ai;
    var t = $('total');
    t.textContent = '{{L_TOTAL_PREFIX}}' + total + '{{L_TOTAL_SUFFIX}}';
    t.classList.toggle('warn', total > 5);
  }

  // "Great for" goal filter: clicking a goal narrows visible cards to those tagged with it.
  var goalBtns = all.call(document.querySelectorAll('.goal'));
  function activeGoals(){ return goalBtns.filter(function(b){ return b.classList.contains('on'); }).map(function(b){ return b.dataset.goal; }); }
  function applyFilter(){
    var act = activeGoals();
    all.call(document.querySelectorAll('label.tech')).forEach(function(lab){
      var inp = lab.querySelector('input');
      if (inp.dataset.invent){ return; }  // invent cards aren't goal-tagged — always visible
      var good = (inp.dataset.good || '').split('|');
      var show = !act.length || act.some(function(g){ return good.indexOf(g) >= 0; });
      lab.classList.toggle('hidden', !show);
    });
  }
  goalBtns.forEach(function(b){ b.addEventListener('click', function(){ b.classList.toggle('on'); applyFilter(); }); });

  function randomPool(){
    var picked = {};
    checkedTech().forEach(function(b){ picked[b.dataset.name] = 1; });
    // draw from unchecked, non-lead copies, skipping anything already picked
    return techBoxes.filter(function(b){ return !b.checked && !b.dataset.lead && !picked[b.dataset.name]; });
  }

  function sample(arr, n){
    var a = arr.slice(), out = [];
    while (out.length < n && a.length){ out.push(a.splice(Math.floor(Math.random() * a.length), 1)[0]); }
    return out;
  }

  function compose(){
    var picks = checkedTech().map(function(b){ return { n: b.dataset.name, c: b.dataset.cat, d: b.dataset.desc, r: false }; });
    var rnd = sample(randomPool(), state.rand).map(function(b){ return { n: b.dataset.name, c: b.dataset.cat, d: b.dataset.desc, r: true }; });
    var techs = picks.concat(rnd);
    var L = ["{{P_OPEN}}", "", '{{P_MODE}}' + state.mode + '.'];
    if (techs.length){
      L.push("", '{{P_TECHNIQUES}}');
      techs.forEach(function(t, i){
        L.push((i + 1) + '.' + (t.r ? ' ({{P_RANDOM_PICK}})' : '') + ' ' + t.n + '  ·  ' + t.c);
        L.push('   ' + t.d);
      });
    }
    var extra = [];
    if (state.inv > 0){ extra.push('{{P_INV_A}}' + state.inv + '{{P_INV_B}}' + (state.inv > 1 ? '{{P_INV_PLURAL}}' : '') + '{{P_INV_C}}'); }
    checkedInvent().forEach(function(b){ extra.push('{{P_SPIRIT}}' + b.dataset.invent); });
    if (state.ai > 0){ extra.push('{{P_CH_A}}' + state.ai + '{{P_CH_B}}' + (state.ai > 1 ? '{{P_CH_PLURAL}}' : '') + '{{P_CH_C}}'); }
    if (extra.length){ L.push("", '{{P_THEN}}' + extra.join('{{P_AND}}') + '.'); }
    if (!techs.length && !extra.length){
      L.push("", state.mode === 'Ideate for me'
        ? '{{P_FALLBACK_A}}'
        : '{{P_FALLBACK_B}}');
    }
    return L.join('\n');
  }

  function fallbackCopy(t){
    var ta = document.createElement('textarea');
    ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch(e){ ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function flash(ok, text){
    var b = $('banner');
    b.classList.toggle('fail', !ok);
    b.innerHTML = ok
      ? '{{BANNER_OK_JS}}'
      : '{{BANNER_FAIL}}';
    b.classList.add('show');
    setTimeout(function(){ b.classList.remove('show'); }, 4500);
    // Last resort on a hard failure: a prefilled, selectable prompt so the text is never lost.
    if (!ok){ window.prompt('{{P_PROMPT_BOX}}', text); }
  }

  $('copy').addEventListener('click', function(){
    var text = compose();
    if (navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(
        function(){ flash(true, text); },
        function(){ flash(fallbackCopy(text), text); }
      );
    } else { flash(fallbackCopy(text), text); }
  });

  setHint();
  setThemeIcon();
  update();
})();
</script>
</body>
</html>
"""


# --- browse-page layout: a "Proven & Professional" lead group, then super-groups ----------
CLASSIC_GROUP = "Proven & Professional"
LEAD_HUE = "#3d4f73"  # a dignified slate for the professional lead group

# Super-group order for the shipped categories. Categories not listed (e.g. user-added
# via --extra) render last under "More", alphabetically — so custom catalogs always show.
CATEGORY_GROUPS = (
    ("Structured & Analytical", ("structured", "deep")),
    ("Creative & Generative", ("creative", "biomimetic", "cultural", "speculative_future", "quantum")),
    ("Wild & Playful", ("wild", "absurdist", "theatrical", "constraint")),
    ("Introspective & Personal", ("introspective_delight", "collaborative")),
)

# Human labels for the `good_for` goal tags; this dict's order is the filter-bar order.
GOAL_LABELS = {
    "feature": "Build a feature",
    "novel": "Novel concept",
    "strategy": "Strategy",
    "planning": "Planning",
    "diagnosis": "Diagnose",
    "personal": "Personal / life",
    "unstuck": "Get unstuck",
}


def _good_for_label(good: str, pack: dict | None = None) -> str:
    labels = pack_goal_labels(pack)
    strings = pack_strings(pack)
    parts = [labels.get(g, g) for g in good.split("|") if g]
    if not parts:
        return ""
    return strings["GOOD_FOR_PREFIX"] + strings["GOOD_FOR_SEP"].join(parts)


def _svg(inner: str) -> str:
    return f'<svg class="ico" viewBox="0 0 44 44" xmlns="http://www.w3.org/2000/svg">{CHIP}{inner}</svg>'


def _card(r: dict, lead: bool = False, pack: dict | None = None) -> str:
    """One technique card. `lead=True` cards live in the cross-cutting professional group;
    they carry their own category hue (inline --c) and data-lead so selection can de-dupe.

    A pack's translated name and description take the card's place; the icon
    still keys on the English technique name, the technique's identity."""
    name = html.escape(r.get("display_name") or r["technique_name"])
    desc = html.escape(r.get("display_description") or r["description"])
    hue, glyph = category_style(r["category"])
    disp_cat = html.escape(r.get("display_category") or pretty(r["category"]))
    good = html.escape(r.get("good_for", ""))
    prov = html.escape(r.get("provenance", ""))
    style = f' style="--c:{hue}"' if lead else ""
    lead_attr = ' data-lead="1"' if lead else ""
    gf = _good_for_label(r.get("good_for", ""), pack)
    gf_html = f'<span class="gf">{html.escape(gf)}</span>' if gf else ""
    return (
        f'<label class="tech"{style}><input type="checkbox" '
        f'data-name="{name}" data-cat="{disp_cat}" data-desc="{desc}" data-good="{good}" data-prov="{prov}"{lead_attr}>'
        f'<span class="ic2">{_svg(glyph)}{_svg(tech_icon(r["technique_name"]))}</span>'
        f'<span><span class="n">{name}</span><span class="d">{desc}</span>{gf_html}</span></label>'
    )


def _invent_card(ident_cat: str, disp_cat: str, glyph: str, pack: dict | None = None) -> str:
    """A dashed 'invent on the fly, in this category's spirit' card appended to each section.

    `data-invent` carries the English category (the identity the skill still
    recognizes); the visible text is the translation."""
    strings = pack_strings(pack)
    title = strings["INVENT_TITLE"].replace("{cat}", disp_cat)
    desc = strings["INVENT_DESC"].replace("{cat}", disp_cat)
    return (
        f'<label class="tech invent"><input type="checkbox" data-invent="{ident_cat}">'
        f'<span class="ic2">{_svg(glyph)}</span>'
        f'<span><span class="n">{html.escape(title)}</span>'
        f'<span class="d">{html.escape(desc)}</span></span></label>'
    )


def html_doc(rows: list[dict], pack: dict | None = None) -> str:
    """Render the self-contained 'browse all techniques' selection page from the catalog.

    Deterministic ordering so the shipped asset can be snapshot-tested against the CSV:
    a cross-cutting "Proven & Professional" lead group (every `classic`-tagged row), then
    the categories in fixed super-group order, then any unlisted/custom categories under
    "More" alphabetically. Techniques render in file order within a category. A `classic`
    row appears both in the lead group and its home category; the page de-dupes on select.
    """
    rows = apply_pack(rows, pack)
    strings = pack_strings(pack)
    goal_labels = pack_goal_labels(pack)
    group_titles = (pack or {}).get("category_groups") or {}

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(r)

    body: list[str] = []
    chips: list[str] = []

    def add_section(cat: str) -> None:
        hue, glyph = category_style(cat)
        ident = html.escape(pretty(cat))
        disp = html.escape(display_category(cat, pack))
        cards = [_card(r, pack=pack) for r in groups[cat]]
        cards.append(_invent_card(ident, disp, glyph, pack))
        chips.append(f'<button type="button" class="chip" data-cat="{disp}" style="--cc:{hue}">{disp}</button>')
        body.append(
            f'<section data-cat="{disp}" style="--c:{hue}"><h2>{disp}<span class="cnt">{len(groups[cat])}</span></h2>'
            f'<div class="grid">{"".join(cards)}</div></section>'
        )

    # 1) lead group — every classic-tagged technique, cross-category (no invent card here)
    classics = [r for r in rows if r.get("provenance", "").lower() == "classic"]
    if classics:
        disp = html.escape(strings["CLASSIC_GROUP"])
        lead_cards = "".join(_card(r, lead=True, pack=pack) for r in classics)
        chips.append(f'<button type="button" class="chip" data-cat="{disp}" style="--cc:{LEAD_HUE}">{disp}</button>')
        body.append(
            f'<section data-cat="{disp}" style="--c:{LEAD_HUE}"><h2>{disp}<span class="cnt">{len(classics)}</span></h2>'
            f'<div class="grid">{lead_cards}</div></section>'
        )

    # 2) shipped categories, in super-group order
    placed = set()
    for group_title, cats in CATEGORY_GROUPS:
        present = [c for c in cats if c in groups]
        if not present:
            continue
        hue, _ = category_style(present[0])
        shown = group_titles.get(group_title) or group_title
        body.append(f'<h2 class="grouphdr" style="--c:{hue}">{html.escape(shown)}</h2>')
        for c in present:
            add_section(c)
            placed.add(c)

    # 3) leftover (custom / --extra) categories, alphabetically
    leftover = sorted(c for c in groups if c not in placed)
    if leftover:
        more = html.escape(strings["MORE_GROUP"])
        body.append(f'<h2 class="grouphdr" style="--c:#8a8f9e">{more}</h2>')
        for c in leftover:
            add_section(c)

    # goal-affinity filter bar — only if the catalog actually carries good_for tags
    present_goals: set[str] = set()
    for r in rows:
        for g in (r.get("good_for", "") or "").split("|"):
            if g:
                present_goals.add(g)
    goalbar = ""
    if present_goals:
        ordered = [g for g in GOAL_LABELS if g in present_goals] + sorted(present_goals - set(GOAL_LABELS))
        gchips = "".join(
            f'<button type="button" class="goal" data-goal="{html.escape(g)}">{html.escape(goal_labels.get(g, g))}</button>'
            for g in ordered
        )
        label = html.escape(strings["L_GREAT_FOR"])
        goalbar = f'<div class="bar"><span class="glabel">{label}</span><div class="goals" id="goals">{gchips}</div></div>'

    total = html.escape(strings["TOTAL_SUMMARY"].format(n=len(rows), m=len(groups)))
    doc = SELECTOR_TEMPLATE
    for key, value in strings.items():
        doc = doc.replace("{{" + key + "}}", escape_string(key, value))
    return (
        doc.replace("{{BODY}}", "\n".join(body))
        .replace("{{CHIPS}}", "".join(chips))
        .replace("{{GOALBAR}}", goalbar)
        .replace("{{TOTAL}}", total)
    )


def pin_utf8(stream):
    """Pin a console stream to UTF-8, keeping its own error handler.

    `--extra` technique text and the technique names echoed to stderr are
    arbitrary user input, so either stream can carry a character the platform
    default cannot encode (cp1252 on Windows) and print() then raises.

    errors= is passed through deliberately: reconfigure(encoding=...) alone
    resets the handler to "strict", which would silently downgrade stderr's
    POSIX default of "backslashreplace" and turn a diagnostic about an
    undecodable path into a traceback.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors=getattr(stream, "errors", None) or "strict")


def main(argv: list[str] | None = None) -> int:
    pin_utf8(sys.stdout)
    pin_utf8(sys.stderr)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", type=Path, default=DEFAULT_FILE, help="technique CSV (default: sibling assets/brain-methods.csv)")
    p.add_argument("--extra", type=Path, help="JSON overlay of additional techniques (customize.toml additional_techniques), merged into every command")
    p.add_argument("--i18n", type=Path, help="language pack (brain-i18n.json); defaults to the one beside the catalog")
    p.add_argument("--json", action="store_true", help="emit structured JSON instead of lean text")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("categories", help="list category names + counts")
    pl = sub.add_parser("list", help="the index: category/name/gist (needs --category or --all)")
    pl.add_argument("--category", action="append", help="filter to a category (repeatable)")
    pl.add_argument("--all", action="store_true", help="dump the entire catalog (deliberate; large)")
    ps = sub.add_parser("show", help="full gist + detail file for named techniques")
    ps.add_argument("names", nargs="+")
    pr = sub.add_parser("random", help="pick techniques at random")
    pr.add_argument("--category", action="append", help="restrict to a category (repeatable)")
    pr.add_argument("-n", type=int, default=1, help="how many (default 1)")
    ph = sub.add_parser("html", help="write the offline 'browse all' selection page")
    ph.add_argument("--out", help="file to write the page to (required; never prints the catalog)")
    args = p.parse_args(argv)

    args.file = expand_path(args.file)
    if not args.file.is_file():
        print(f"error: technique file not found: {args.file}", file=sys.stderr)
        return 2
    rows = load(args.file)
    pack: dict = {}
    if args.i18n is None:
        pack = load_pack(args.file)
    else:
        args.i18n = expand_path(args.i18n)
        if not args.i18n.is_file():
            print(f"error: --i18n file not found: {args.i18n}", file=sys.stderr)
            return 2
        try:
            pack = json.loads(args.i18n.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            print(f"error: could not read --i18n: {e}", file=sys.stderr)
            return 2
        if not isinstance(pack, dict):
            print("error: --i18n must be a JSON object", file=sys.stderr)
            return 2
    if args.extra:
        if not args.extra.is_file():
            print(f"error: --extra file not found: {args.extra}", file=sys.stderr)
            return 2
        try:
            rows = merge_extra(rows, load_extra(args.extra))
        except (OSError, ValueError) as e:
            print(f"error: could not read --extra: {e}", file=sys.stderr)
            return 2
    csv_dir = args.file.resolve().parent
    rows = apply_pack(rows, pack)

    if args.cmd == "categories":
        print(fmt_categories(categories(rows), args.json))
    elif args.cmd == "list":
        if not args.category and not args.all:
            print(
                "error: `list` needs --category (one or more) — or --all to dump the whole "
                "catalog on purpose. Use `categories` for the cheap map, or `random` to draw blind.",
                file=sys.stderr,
            )
            return 2
        print(fmt_list(filter_cats(rows, args.category), args.json))
    elif args.cmd == "show":
        found, missing = find(rows, args.names)
        for m in missing:
            print(f"# not found: {m}", file=sys.stderr)
        if not found:
            return 1
        print(fmt_show(found, csv_dir, args.json))
    elif args.cmd == "random":
        pool = filter_cats(rows, args.category)
        if not pool:
            print("# no techniques match", file=sys.stderr)
            return 1
        n = max(0, min(args.n, len(pool)))  # clamp: never crash on a negative or oversized -n
        print(fmt_list(random.sample(pool, n), args.json))
    elif args.cmd == "html":
        if not args.out:
            print(
                "error: `html` needs --out PATH — it writes the selection page to a file and "
                "never prints the catalog to stdout (which would defeat the point).",
                file=sys.stderr,
            )
            return 2
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_doc(rows, pack), encoding="utf-8")
        print(f"wrote {out} ({len(rows)} techniques, {len(categories(rows))} categories)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
