# app.py
# ==========================================================
# INVENTORY EXPANDER + INPUT REFINER + STREAMLIT UI
#
# PIPELINE ORDER:
#   1. refine_input_csv()   ← NEW: typos, separators, dupes, bad tokens
#   2. clean_input_csv()    ← existing: header canonicalization + value maps
#   3. expand_inventory()   ← existing: cartesian variant expansion
#
# CHANGES FROM PREVIOUS VERSION:
#   - Added Section 4B: INPUT REFINER (refine_input_csv + helpers)
#   - split_variant_tokens(): numeric multi-dot typo fix (7.5.8.5 → 7.5,8.5)
#   - should_skip_expansion(): added jewelry_type, jewelry_style, color,
#     material as always-static columns
#   - expand_inventory(): calls refiner before cleaning
#   - Streamlit UI: Pre-flight Report panel shows exactly what was fixed
#   - RefineReport dataclass: structured diff returned from refiner
# ==========================================================

import os, csv, io, json, re, argparse
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product
from typing import Any, Dict, List, Optional, Tuple

try:
    import pytz
    def get_ist_now():
        ist = pytz.timezone("Asia/Kolkata")
        return datetime.now(ist)
except ImportError:
    def get_ist_now():
        return datetime.utcnow()

import pandas as pd

try:
    import streamlit as st
    _HAS_STREAMLIT = True
except Exception:
    _HAS_STREAMLIT = False


# ==========================================================
# 1. FILE PATHS
# ==========================================================
MAPPING_FILE = "data-headers-2025-10-14.csv"
RULES_FILE   = "normalization_rules.json"


# ==========================================================
# 2. HEADER MAPPING
# ==========================================================
VAR_TO_SETTER:      Dict[str, str] = {}
SETTER_TO_CANONICAL: Dict[str, str] = {}

IC_TO_BASE = {
    "metal_type_ic":      "metals",
    "supported_shapes_ic":"shape",
    "size_ic":            "size",
    "center_size_ic":     "center_size",
    "ring_mm_ic":         "ring_mm",
}

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def ensure_mapping_file_exists() -> None:
    if not os.path.exists(MAPPING_FILE):
        df = pd.DataFrame([
            {"Setter name": "stock_num",    "Header variations": "Stock Number,SKU"},
            {"Setter name": "price",        "Header variations": "Price,Base Price,Retail Price,MSRP"},
            {"Setter name": "master_stock", "Header variations": "Master stock,Master Stock,master_stock,masterstock"},
        ])
        df.to_csv(MAPPING_FILE, index=False)

def load_mapping_file() -> None:
    ensure_mapping_file_exists()
    VAR_TO_SETTER.clear()
    SETTER_TO_CANONICAL.clear()
    df_map = pd.read_csv(MAPPING_FILE)
    for _, row in df_map.iterrows():
        setter = str(row.get("Setter name", "")).strip()
        vars_  = [v.strip() for v in str(row.get("Header variations", "")).replace("\r\n", " ").split(",") if v.strip()]
        if setter:
            if vars_:
                SETTER_TO_CANONICAL[setter] = vars_[0]
            for v in vars_:
                VAR_TO_SETTER[_norm(v)] = setter

def update_mapping_manually(header: str, setter: str):
    df = pd.read_csv(MAPPING_FILE)
    valid_setters = df["Setter name"].unique().tolist()
    if setter not in valid_setters:
        raise ValueError(f"Setter '{setter}' not found. Valid: {valid_setters[:20]} ...")
    idx = df[df["Setter name"] == setter].index[0]
    current_vars = str(df.at[idx, "Header variations"])
    if header not in current_vars:
        df.at[idx, "Header variations"] = f"{current_vars}, {header}"
        df.to_csv(MAPPING_FILE, index=False)
        load_mapping_file()

load_mapping_file()


# ==========================================================
# 3. RULES
# ==========================================================
def ensure_rules_file_exists() -> None:
    if not os.path.exists(RULES_FILE):
        base = {
            "version": 1,
            "updated_at": get_ist_now().isoformat(),
            "value_maps": {},
            "global_regex_replacements": [],
            "sku_rules":   {"enabled": True},
            "image_rules": {"enabled": False},
            "price_rules": {"currency": "USD", "default_base_price": 0, "adjustments": {}},
        }
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(base, f, indent=2)

def load_rules() -> Dict[str, Any]:
    ensure_rules_file_exists()
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_rules(rules: Dict[str, Any]) -> None:
    rules = dict(rules or {})
    rules["updated_at"] = get_ist_now().isoformat()
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2)


# ==========================================================
# 4. CSV PARSING / CLEANING
# ==========================================================
def smart_parse(txt: str) -> Dict[str, Any]:
    text = (txt or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    f = io.StringIO(text)
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",\t;|")
        delim = dialect.delimiter
    except Exception:
        delim = ","
    reader = csv.reader(f, delimiter=delim)
    rows   = [r for r in reader if any((c or "").strip() for c in r)]
    return {
        "columns": [c.strip().replace('"', "") for c in rows[0]] if rows else [],
        "rows":    [[(c.strip() if c else None) for c in r] for r in rows[1:]] if rows else [],
    }

def get_setter(header: str) -> Optional[str]:
    return VAR_TO_SETTER.get(_norm(header))

def get_canonical(setter: Optional[str], fallback: str) -> str:
    return SETTER_TO_CANONICAL.get(setter, fallback) if setter else fallback

def canonicalize_headers(columns: List[str]) -> Tuple[List[str], Dict[str, str], List[str]]:
    rename_map, unknown, seen, new_columns = {}, [], {}, []
    for c in columns:
        setter = get_setter(c)
        if setter:
            base     = IC_TO_BASE.get(setter, setter)
            new_name = get_canonical(base, c)
            rename_map[c] = new_name
        else:
            rename_map[c] = c
            unknown.append(c)

    for c in columns:
        nc = rename_map[c]
        seen[nc] = seen.get(nc, 0) + 1
        new_columns.append(f"{nc} ({seen[nc]})" if seen[nc] > 1 else nc)

    return new_columns, {old: new_columns[i] for i, old in enumerate(columns)}, unknown

def clean_input_csv(csv_text: str, rules: Dict[str, Any]) -> Dict[str, Any]:
    parsed = smart_parse(csv_text)
    cols, rows = parsed["columns"], parsed["rows"]
    if not cols:
        return {"cleaned_csv": "", "diff": {}}

    new_cols, rename_map, unknown_cols = canonicalize_headers(cols)

    col_setter_base = [
        IC_TO_BASE.get(get_setter(old), get_setter(old)) if get_setter(old) else None
        for old in cols
    ]

    cleaned_rows = []
    for r in rows:
        rr = []
        for j, v in enumerate(r):
            x = str(v or "").strip().replace(";", ",").replace("|", ",").replace("#", ",")
            x = re.sub(r"\s*,\s*", ",", x)
            if j < len(col_setter_base) and col_setter_base[j]:
                base_key = col_setter_base[j]
                x = rules.get("value_maps", {}).get(base_key, {}).get(x.lower(), x)
            rr.append(x)
        cleaned_rows.append(rr)

    buf = io.StringIO()
    w   = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    w.writerow(new_cols)
    w.writerows(cleaned_rows)

    return {
        "cleaned_csv": buf.getvalue(),
        "diff": {"header_renames": rename_map, "unknown_columns": unknown_cols},
    }


# ==========================================================
# 4B. INPUT REFINER  ← NEW SECTION
# ==========================================================
@dataclass
class RefineReport:
    """Structured diff of every change the refiner made."""
    header_typos_fixed:    List[Dict[str, str]] = field(default_factory=list)
    separators_normalized: List[Dict[str, str]] = field(default_factory=list)
    duplicates_removed:    List[Dict[str, Any]] = field(default_factory=list)
    empty_tokens_removed:  List[Dict[str, Any]] = field(default_factory=list)
    numeric_typos_fixed:   List[Dict[str, str]] = field(default_factory=list)
    total_changes: int = 0

    def add(self, category: str, entry: dict):
        getattr(self, category).append(entry)
        self.total_changes += 1

    def has_issues(self) -> bool:
        return self.total_changes > 0

    def summary_lines(self) -> List[str]:
        out = []
        if self.header_typos_fixed:
            out.append(f"**Header typos fixed ({len(self.header_typos_fixed)}):** " +
                       ", ".join(f'"{e["original"]}" → "{e["fixed"]}"' for e in self.header_typos_fixed))
        if self.separators_normalized:
            out.append(f"**Separators normalized ({len(self.separators_normalized)}):** " +
                       ", ".join(f'column "{e["column"]}" ({e["from"]} → comma)' for e in self.separators_normalized))
        if self.duplicates_removed:
            out.append(f"**Duplicate values removed ({len(self.duplicates_removed)}):** " +
                       ", ".join(f'"{e["column"]}" removed {e["removed"]}' for e in self.duplicates_removed))
        if self.empty_tokens_removed:
            out.append(f"**Empty tokens removed ({len(self.empty_tokens_removed)}):** " +
                       ", ".join(f'column "{e["column"]}"' for e in self.empty_tokens_removed))
        if self.numeric_typos_fixed:
            out.append(f"**Numeric typos fixed ({len(self.numeric_typos_fixed)}):** " +
                       ", ".join(f'"{e["original"]}" → "{e["fixed"]}"' for e in self.numeric_typos_fixed))
        return out


# --- Typo dictionary: maps bad substrings → correct form (lowercase).
#     Keys must be unambiguous enough not to collide with valid words.
_HEADER_TYPO_MAP: Dict[str, str] = {
    "lenght":     "length",
    "lengt":      "length",
    "wieght":     "weight",
    "wheight":    "weight",
    "availble":   "available",
    "avaliable":  "available",
    "jewlery":    "jewelry",
    "jewellery":  "jewelry",
    "pendnat":    "pendant",
    "carot":      "carat",
    "diamnond":   "diamond",
    "bracelat":   "bracelet",
    "earign":     "earring",
    "neclace":    "necklace",
    "siver":      "silver",
    "platium":    "platinum",
}

def _fix_header_typos(header: str) -> Tuple[str, bool]:
    """
    Return (corrected_header, was_changed).
    Fixes each word in the header independently using _HEADER_TYPO_MAP so
    that substring collisions like 'lengt' inside 'length' are avoided.
    """
    original   = header
    words_orig = header.split()
    words_new  = []
    changed    = False

    for word in words_orig:
        low_word = word.lower()
        replaced = False
        for bad, good in _HEADER_TYPO_MAP.items():
            # Only match if the entire word IS the bad token
            # (prevents 'lengt' matching inside already-correct 'length')
            if low_word == bad:
                fixed_word = good.capitalize() if word[0].isupper() else good
                words_new.append(fixed_word)
                replaced = True
                changed  = True
                break
        if not replaced:
            words_new.append(word)

    return " ".join(words_new), changed


def _normalize_separator(raw: str, report_col: str, report: RefineReport) -> str:
    """
    Unify separators to comma.  Reports when a non-comma separator was found.
    Handles: #  ;  |
    The multi-dot numeric typo (6.5,7.5.8.5) is also fixed here.
    """
    original = raw
    changed_sep = None

    if "#" in raw:
        raw = raw.replace("#", ",")
        changed_sep = "#"
    if ";" in raw:
        raw = raw.replace(";", ",")
        changed_sep = ";"
    if "|" in raw:
        raw = raw.replace("|", ",")
        changed_sep = "|"

    # Fix numeric multi-dot typo: "7.5.8.5" → "7.5,8.5"
    # Pattern: digit(s).digit(s).digit(s)  →  digit(s).digit(s),digit(s)
    fixed = re.sub(r"(\d+\.\d+)\.(\d)", r"\1,\2", raw)
    if fixed != raw:
        report.add("numeric_typos_fixed", {"column": report_col, "original": raw, "fixed": fixed})
        raw = fixed

    # Normalise spacing around commas
    raw = re.sub(r"\s*,\s*", ",", raw)

    if changed_sep and raw != original:
        report.add("separators_normalized", {"column": report_col, "from": changed_sep})

    return raw


def _dedup_tokens(tokens: List[str], col: str, report: RefineReport) -> List[str]:
    """Remove exact duplicates while preserving order. Reports removals."""
    seen, out = set(), []
    removed = []
    for t in tokens:
        if t in seen:
            removed.append(t)
        else:
            seen.add(t)
            out.append(t)
    if removed:
        report.add("duplicates_removed", {"column": col, "removed": removed})
    return out


def _remove_empty_tokens(tokens: List[str], col: str, report: RefineReport) -> List[str]:
    """Strip empty/whitespace-only tokens. Reports if any were removed."""
    out = [t for t in tokens if t.strip()]
    if len(out) < len(tokens):
        report.add("empty_tokens_removed", {"column": col, "count": len(tokens) - len(out)})
    return out


def _is_variant_header(col: str) -> bool:
    """Available-prefixed columns are the only ones that carry variant tokens."""
    return col.strip().lower().startswith("available ")


def refine_input_csv(csv_text: str) -> Tuple[str, RefineReport]:
    """
    Pre-flight refinement pass that runs BEFORE clean_input_csv().

    What it does:
      1. Fix spelling typos in column headers (e.g. "Lenght" → "Length")
      2. Normalise value separators to comma  (# ; | → ,)
      3. Fix numeric multi-dot typos          (7.5.8.5 → 7.5,8.5)
      4. Remove duplicate variant tokens      (Gold,Gold → Gold)
      5. Remove empty tokens                  (,, → cleaned)

    Returns:
      (refined_csv_text, RefineReport)
    """
    report = RefineReport()
    parsed = smart_parse(csv_text)
    cols   = parsed["columns"]
    rows   = parsed["rows"]

    if not cols:
        return csv_text, report

    # ── Step 1: Header typo correction ──────────────────────────────────
    new_cols: List[str] = []
    for col in cols:
        fixed, changed = _fix_header_typos(col)
        if changed:
            report.add("header_typos_fixed", {"original": col, "fixed": fixed})
        new_cols.append(fixed)

    # ── Steps 2-5: Per-cell value refinement (only for variant columns) ─
    new_rows: List[List[str]] = []
    for row in rows:
        new_row: List[str] = []
        for ci, cell in enumerate(row):
            col_name = new_cols[ci] if ci < len(new_cols) else ""
            val      = str(cell or "").strip()

            if _is_variant_header(col_name):
                # Normalise separators + fix numeric typos
                val = _normalize_separator(val, col_name, report)
                # Tokenise, clean, dedup
                tokens = [t.strip() for t in val.split(",")]
                tokens = _remove_empty_tokens(tokens, col_name, report)
                tokens = _dedup_tokens(tokens, col_name, report)
                val    = ",".join(tokens)
            else:
                # For non-variant cols only fix the # separator (cleaning step
                # handles ; | later), and do NOT split/dedup.
                val = val.replace("#", ",")
                val = re.sub(r"\s*,\s*", ",", val)

            new_row.append(val)

        # Pad short rows to match header length
        while len(new_row) < len(new_cols):
            new_row.append("")

        new_rows.append(new_row)

    # ── Rebuild CSV ──────────────────────────────────────────────────────
    buf = io.StringIO()
    w   = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    w.writerow(new_cols)
    w.writerows(new_rows)

    return buf.getvalue(), report


# ==========================================================
# 5. HELPERS
# ==========================================================
def _strip_available(label: str) -> str:
    return re.sub(r"^available\s+", "", (label or "").strip(), flags=re.IGNORECASE)

def normalize_list(v: str) -> str:
    return ",".join([t.strip() for t in str(v or "").split(",") if t.strip()])

def is_available_col(name: str) -> bool:
    return (name or "").strip().lower().startswith("available ")

def available_base_name(name: str) -> str:
    return _strip_available(name).strip()

def _fmt_ct(val: str) -> str:
    s = (val or "").strip()
    try:
        n = float(s)
        return f"{n:.2f}ct"
    except Exception:
        return s

def _canon_key(label: str) -> str:
    n = _norm(_strip_available(label))
    if n in ["metals", "metal", "metaltype"]:
        return "Metal"
    if n == "shape":
        return "Shape"
    if n in ["centercaratweight", "caratweight", "centercarat",
             "centerstone", "centerstonesize", "caratweight"]:
        return "Center Stone"
    if n in ["size", "ringsize"]:
        return "Ring Size"
    if n in ["shankstyle", "shank"]:
        return "Shank Style"
    if n in ["headstyle", "head"]:
        return "Head Style"
    if n in ["thickness"]:
        return "Thickness"
    if n in ["width"]:
        return "Width"
    if n in ["chainlength", "chainlenght"]:
        return "Chain Length"
    return _strip_available(label)

def _pretty_value(key: str, val: str) -> str:
    v = (val or "").strip()
    k = (key or "").lower()
    if "shape" in k:
        return v.title()
    if "metal" in k:
        return v.title()
    if "center" in k or "carat" in k or "ct" in k:
        return _fmt_ct(v)
    return v


# ==========================================================
# 5B. EXPANSION CONTROL
# ==========================================================
NON_EXPANDABLE_EXACT_NORM = {
    "masterstock",
    "stocknumber",
    "sku",
    "shorttitle",
    "description",
    "price",
    "notes",
    "totalvarients",
    "totalvariants",
    # ── NEW: jewelry taxonomy columns are always static ──
    "jewelrytype",
    "jewelrystyle",
    "style",
    "category",
    "material",
    "color",
    "gender",
    "collection",
}

NON_EXPANDABLE_PREFIXES_NORM = [
    "description",
    "imageurl",
]

def _strip_duplicate_header_suffix(col_name: str) -> str:
    return re.sub(r"\s*\(\d+\)\s*$", "", (col_name or "").strip())

def should_skip_expansion(col_name: str) -> bool:
    base_name = _strip_duplicate_header_suffix(col_name)
    if is_available_col(base_name):
        return False
    c = _norm(base_name)
    if c in NON_EXPANDABLE_EXACT_NORM:
        return True
    return any(c.startswith(prefix) for prefix in NON_EXPANDABLE_PREFIXES_NORM)


def split_variant_tokens(raw_val: Any) -> List[str]:
    """
    Split a variant cell into individual option tokens.

    Changes from previous version:
    - Numeric multi-dot fix applied here too as a safety net in case
      the refiner was skipped:  7.5.8.5  →  7.5,8.5
    """
    raw = str(raw_val or "")
    raw = raw.replace("#", ",")
    raw = raw.replace(";", ",")
    raw = raw.replace("|", ",")

    # ── Safety net: fix numeric multi-dot typo ────────────────────────
    raw = re.sub(r"(\d+\.\d+)\.(\d)", r"\1,\2", raw)

    return [x.strip() for x in raw.split(",") if x.strip()] or [""]


# ==========================================================
# 6. TITLE / DESCRIPTION
# ==========================================================
def infer_style_name_from_title(base_title: str, varying_options: List[str]) -> str:
    t    = (base_title or "").strip()
    if not t:
        return ""
    opts = sorted({o.strip() for o in varying_options if o and o.strip()}, key=len, reverse=True)
    for o in opts:
        t = re.sub(rf"(?i)\b{re.escape(o)}\b", " ", t)
    t = re.sub(r"\b\d+(\.\d+)?\s*ct\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*[/,|]\s*", " ", t).strip()
    t = re.sub(r"\s+-\s+", " ", t).strip()
    return t

def _find_first_pos(text: str, needles: List[str]) -> Optional[int]:
    t    = (text or "").lower()
    best = None
    for n in needles:
        n2 = (n or "").strip().lower()
        if not n2:
            continue
        m = re.search(rf"\b{re.escape(n2)}\b", t)
        if m:
            best = m.start() if best is None else min(best, m.start())
    return best

def infer_variant_title_order(original_title: str,
                               parts: Dict[str, str],
                               all_options_by_key: Dict[str, List[str]]) -> List[str]:
    title  = (original_title or "").strip()
    keys   = [k for k in parts.keys() if parts.get(k)]
    scored = []
    for k in keys:
        selected = parts[k]
        needles  = [selected] + all_options_by_key.get(k, [])
        pos      = _find_first_pos(title, needles)
        scored.append((pos if pos is not None else 10**9, k))
    scored.sort(key=lambda x: x[0])
    return [k for _, k in scored]

def build_variant_short_title(original_title: str,
                               style_name_base: str,
                               parts: Dict[str, str],
                               all_options_by_key: Dict[str, List[str]]) -> str:
    style      = (style_name_base or "").strip() or "Item"
    order_keys = infer_variant_title_order(original_title, parts, all_options_by_key)
    front      = [parts[k] for k in order_keys if (parts.get(k) or "").strip()]
    return " ".join(front + [style]).strip() if front else style

def build_variant_description(style_name_base: str,
                               parts: Dict[str, str],
                               all_options_by_key: Dict[str, List[str]],
                               original_title: str) -> str:
    title        = build_variant_short_title(original_title, style_name_base, parts, all_options_by_key)
    ordered_keys = infer_variant_title_order(original_title, parts, all_options_by_key)
    lines        = [f"- {k}: {parts[k]}" for k in ordered_keys if parts.get(k)]
    lines       += [f"- {k}: {v}" for k, v in parts.items() if k not in ordered_keys and v]
    return (title + "\n\nVariant Details:\n" + "\n".join(lines)).strip()


# ==========================================================
# 7. PRICING
# ==========================================================
def _to_float(x: Any, default: float = 0.0) -> float:
    if x is None:
        return default
    s = str(x).strip()
    if not s:
        return default
    s = s.replace(",", "")
    s = re.sub(r"[^0-9.\-]", "", s)
    try:
        return float(s)
    except Exception:
        return default

def price_rules_enabled(rules: Dict[str, Any]) -> bool:
    pr = (rules or {}).get("price_rules")
    return isinstance(pr, dict) and isinstance(pr.get("adjustments"), dict) and len(pr.get("adjustments")) > 0

def compute_variant_price(base_price_value: Any, parts: Dict[str, str], rules: Dict[str, Any]) -> str:
    pr          = rules.get("price_rules", {}) or {}
    base        = _to_float(base_price_value, default=_to_float(pr.get("default_base_price", 0), 0.0))
    adjustments = pr.get("adjustments", {}) or {}

    def nk(s: str) -> str:
        return _norm(s or "")

    adj_norm: Dict[str, Dict[str, float]] = {}
    for k, mapping in adjustments.items():
        kk = nk(k)
        adj_norm[kk] = {}
        if isinstance(mapping, dict):
            for val, amt in mapping.items():
                adj_norm[kk][nk(val)] = _to_float(amt, 0.0)

    price = base
    for k, v in (parts or {}).items():
        kk = nk(k)
        vv = nk(v)
        if kk in adj_norm and vv in adj_norm[kk]:
            price += adj_norm[kk][vv]

    return f"{price:.2f}"


# ==========================================================
# 8. IMAGE URLS
# ==========================================================
def image_rules_enabled(rules: Dict[str, Any]) -> bool:
    ir = (rules or {}).get("image_rules")
    return isinstance(ir, dict) and ir.get("enabled") is True and bool(ir.get("base_url"))

def _img_safe_token(s: str, upper: bool = True, strip_non_alnum: bool = True) -> str:
    t = str(s or "").strip()
    if strip_non_alnum:
        t = re.sub(r"[^A-Za-z0-9]", "", t)
    return t.upper() if upper else t

def _image_token_for(key: str, value: str, image_rules: Dict[str, Any]) -> str:
    abbr   = (image_rules.get("abbr") or {}).get(key) or {}
    if value in abbr:
        return str(abbr[value])
    v_norm = (value or "").strip().lower()
    for k2, v2 in abbr.items():
        if (k2 or "").strip().lower() == v_norm:
            return str(v2)
    fb = image_rules.get("fallback", {}) or {}
    if fb.get("use_raw_if_missing_abbr", True):
        return _img_safe_token(value, upper=fb.get("upper", True), strip_non_alnum=fb.get("strip_non_alnum", True))
    return ""

def generate_image_urls(master_stock: str, parts: Dict[str, str], rules: Dict[str, Any]) -> Dict[str, str]:
    ir       = rules.get("image_rules", {}) or {}
    base_url = str(ir.get("base_url") or "")
    ext      = str(ir.get("suffix") or "")
    joiner   = str(ir.get("joiner") or "_")
    order    = ir.get("order") or ["Master stock", "Metal"]
    pos      = (ir.get("variant_suffix_position") or "after_ext").strip().lower()

    fb             = ir.get("fallback", {}) or {}
    upper          = fb.get("upper", True)
    strip_non_alnum = fb.get("strip_non_alnum", True)

    tokens = []
    for k in order:
        if k == "Master stock":
            tokens.append(_img_safe_token(master_stock, upper=upper, strip_non_alnum=strip_non_alnum))
        elif k in parts and parts[k]:
            tokens.append(_image_token_for(k, parts[k], ir))

    filename_base = joiner.join([t for t in tokens if t])
    out           = {}
    variants      = ir.get("variants") or []

    if variants:
        for v in variants:
            col = v.get("column")
            if not col:
                continue
            prefix = str(v.get("path_prefix") or "")
            suf    = str(v.get("path_suffix") or "")
            fname  = filename_base + suf + ext if pos == "before_ext" else filename_base + ext + suf
            out[col] = base_url + prefix + fname
        return out

    for i in range(1, 5):
        suf   = f"_{i}"
        fname = filename_base + suf + ext if pos == "before_ext" else filename_base + ext + suf
        out[f"Image URL {i}"] = base_url + fname
    return out


# ==========================================================
# 9. SKU SHORTENING
# ==========================================================
def sku_rules_enabled(rules: Dict[str, Any]) -> bool:
    sr = (rules or {}).get("sku_rules")
    return isinstance(sr, dict) and sr.get("enabled", True) is True

def shorten_sku(master_stock: str, parts: Dict[str, str], rules: Dict[str, Any]) -> str:
    sr               = (rules or {}).get("sku_rules", {}) or {}
    joiner           = str(sr.get("joiner") or "-")
    fallback_max_len = int(sr.get("fallback_max_len") or 8)
    abbr_map         = sr.get("abbr", {}) or {}

    preferred_order = sr.get("order")
    if not isinstance(preferred_order, list) or not preferred_order:
        preferred_order = sorted(parts.keys())

    keys_in_order = [k for k in preferred_order if k in parts and parts.get(k)]
    remaining     = sorted([k for k in parts.keys() if k not in keys_in_order and parts.get(k)])
    final_keys    = keys_in_order + remaining

    def abbr_token(key: str, val: str) -> str:
        val = (val or "").strip()
        if not val:
            return ""
        d = abbr_map.get(key, {}) if isinstance(abbr_map.get(key, {}), dict) else {}
        if val in d:
            return str(d[val]).strip()
        v_norm = val.lower().strip()
        for k2, v2 in d.items():
            if str(k2).lower().strip() == v_norm:
                return str(v2).strip()
        tok = re.sub(r"[^A-Za-z0-9]", "", val).upper()
        return tok[:fallback_max_len] if tok else ""

    tokens = [str(master_stock).strip()]
    for k in final_keys:
        t = abbr_token(k, parts.get(k, ""))
        if t:
            tokens.append(t)

    seen, out = set(), []
    for t in tokens:
        if t and t not in seen:
            out.append(t)
            seen.add(t)

    return joiner.join(out)


# ==========================================================
# 10. OUTPUT COLUMN ORDER
# ==========================================================
def _singularize_guess(s: str) -> str:
    t   = (s or "").strip()
    low = t.lower().strip()
    if low.endswith("ies") and len(low) > 3:
        return t[:-3] + "y"
    if low.endswith("ses") and len(low) > 3:
        return t[:-2]
    if low.endswith("s") and not low.endswith("ss") and len(low) > 2:
        return t[:-1]
    return t

def _pair_base_for_available(avail_col: str, all_keys: set) -> Optional[str]:
    base_raw   = _strip_available(avail_col).strip()
    candidates = [
        base_raw,
        _singularize_guess(base_raw),
        base_raw.title(),
        _singularize_guess(base_raw).title(),
    ]
    for c in candidates:
        if c in all_keys:
            return c
    return None

def build_ordered_headers(final_rows:      List[Dict[str, str]],
                           original_cols:  List[str],
                           has_price_column: bool,
                           include_images: bool) -> List[str]:
    priority = ["Master stock", "Stock Number", "Short Title", "Description"]
    if has_price_column:
        priority.append("Price")
    if include_images:
        for i in range(1, 5):
            priority.append(f"Image URL {i}")

    all_keys = set()
    for r in final_rows:
        all_keys.update(r.keys())

    out_h:   List[str] = []
    for h in priority:
        if h in all_keys and h not in out_h:
            out_h.append(h)

    visited = set(out_h)

    def add_col(c: str):
        if c in all_keys and c not in visited:
            out_h.append(c)
            visited.add(c)

    def is_avail(k: str) -> bool:
        return str(k).strip().lower().startswith("available ")

    for c in original_cols:
        if c in priority:
            continue
        c_str = str(c or "").strip()
        if not c_str:
            continue

        if is_avail(c_str):
            base = _pair_base_for_available(c_str, all_keys)
            if base:
                add_col(base)
            add_col(c_str)
            continue

        add_col(c_str)

        partner_exact = f"Available {c_str}"
        if partner_exact in all_keys:
            add_col(partner_exact)
            continue

        if not c_str.lower().endswith("s"):
            partner_plural = f"Available {c_str}s"
            if partner_plural in all_keys:
                add_col(partner_plural)
                continue

        sing = _singularize_guess(c_str)
        if sing != c_str:
            partner_sing = f"Available {sing}"
            if partner_sing in all_keys:
                add_col(partner_sing)

    for k in sorted(all_keys):
        add_col(k)

    return out_h


# ==========================================================
# 11. EXPAND INVENTORY
# ==========================================================
def apply_ui_overrides(rules:          Dict[str, Any],
                        enable_sku:    bool,
                        enable_images: bool,
                        enable_pricing: bool) -> Dict[str, Any]:
    r = json.loads(json.dumps(rules or {}))
    r.setdefault("sku_rules", {})
    r.setdefault("image_rules", {})
    r.setdefault("price_rules", {})
    r["sku_rules"]["enabled"]       = bool(enable_sku)
    r["image_rules"]["enabled"]     = bool(enable_images)
    r["price_rules"]["_ui_enabled"] = bool(enable_pricing)
    return r

def expand_inventory(csv_text:       str,
                     rules:          Optional[Dict[str, Any]] = None,
                     enable_sku:     bool = True,
                     enable_images:  bool = True,
                     enable_pricing: bool = True,
                     skip_refiner:   bool = False,
                     ) -> Tuple[str, Dict[str, Any]]:
    """
    Full pipeline:
      refine_input_csv  →  clean_input_csv  →  cartesian expansion

    Parameters
    ----------
    skip_refiner : bool
        Set True only in tests where you want to bypass the refiner.
        Default False (refiner always runs in normal usage).
    """
    if rules is None:
        rules = load_rules()

    rules = apply_ui_overrides(rules, enable_sku=enable_sku,
                               enable_images=enable_images, enable_pricing=enable_pricing)

    # ── Stage 1: Refine ──────────────────────────────────────────────────
    refine_report: Optional[RefineReport] = None
    if not skip_refiner:
        csv_text, refine_report = refine_input_csv(csv_text)

    # ── Stage 2: Clean / canonicalize headers ───────────────────────────
    cleaned      = clean_input_csv(csv_text, rules)
    cleaned_csv  = cleaned["cleaned_csv"]
    clean_diff   = cleaned["diff"]

    data         = smart_parse(cleaned_csv)
    cols, rows   = list(data["columns"]), [list(r) for r in data["rows"]]
    if not rows:
        return "", {"error": "No rows found after cleaning."}

    def _h(s: str) -> str:
        return _norm(s or "")

    MASTER_INPUT_CANDIDATES = {"master stock", "masterstock", "master_stock"}
    master_idx = next((i for i, c in enumerate(cols)
                       if _h(c) in MASTER_INPUT_CANDIDATES), -1)
    if master_idx == -1:
        raise ValueError("❌ Input file must contain a 'Master stock' column.")

    price_idx       = next((i for i, c in enumerate(cols) if _h(c) == _h("Price")), -1)
    has_price_column = (price_idx != -1)

    img_norm_set    = {_norm(f"Image URL {i}") for i in range(1, 5)}
    img_cols_present = any(_norm(c) in img_norm_set for c in cols)
    include_images  = img_cols_present or ((not img_cols_present) and image_rules_enabled(rules))

    stock_col_name       = next((c for c in cols if _h(c) in {_h("Stock Number"), _h("SKU")}), "Stock Number")
    short_title_col_name = next((c for c in cols if _h(c) == _h("Short Title")), None)

    v_flags = []
    for i in range(len(cols)):
        col_name = cols[i]
        if should_skip_expansion(col_name):
            v_flags.append(False)
        else:
            v_flags.append(
                any(
                    len(split_variant_tokens(r[i])) > 1
                    for r in rows
                    if i < len(r) and r[i]
                )
            )

    final_rows:  List[Dict[str, str]] = []
    master_out_h = "Master stock"
    stock_out_h  = "Stock Number"

    for i, row in enumerate(rows):
        master_val = str(row[master_idx] or "").strip()
        if not master_val:
            master_val = f"MASTER-{i+1:03}"

        base_price_value = row[price_idx] if has_price_column and price_idx < len(row) else None

        original_title = ""
        if short_title_col_name and short_title_col_name in cols:
            original_title = str(row[cols.index(short_title_col_name)] or "").strip()

        exp_meta = []
        for idx in range(len(cols)):
            col_name = cols[idx]
            if should_skip_expansion(col_name):
                continue
            raw_val = str(row[idx] or "")
            tokens  = split_variant_tokens(raw_val)
            exp_meta.append({
                "col":          col_name,
                "tokens":       tokens,
                "orig":         raw_val.replace("#", ","),
                "varies":       v_flags[idx],
                "is_available": is_available_col(col_name),
                "available_base": available_base_name(col_name) if is_available_col(col_name) else "",
                "idx":          idx,
            })

        varying_options: List[str] = []
        for meta in exp_meta:
            if meta["varies"]:
                varying_options.extend([x.strip() for x in str(meta["orig"] or "").split(",") if x.strip()])

        style_name_base = infer_style_name_from_title(original_title, varying_options)
        if not style_name_base:
            style_name_base = original_title or master_val

        all_options_by_key: Dict[str, List[str]] = {}
        for meta in exp_meta:
            if meta["varies"]:
                label = meta["available_base"] if meta["is_available"] else _strip_available(meta["col"])
                key   = _canon_key(label)
                opts  = [x.strip() for x in str(meta["orig"] or "").split(",") if x.strip()]
                all_options_by_key[key] = [_pretty_value(key, o) for o in opts]

        for combo in product(*[x["tokens"] for x in exp_meta]):
            new_r: Dict[str, str] = {}
            for idx, c in enumerate(cols):
                new_r[c] = str(row[idx] or "").strip()

            new_r[master_out_h] = master_val
            parts: Dict[str, str] = {}

            for meta, token in zip(exp_meta, combo):
                token = (token or "").strip()
                if meta["is_available"]:
                    base_name             = meta["available_base"]
                    new_r[f"Available {base_name}"] = normalize_list(meta["orig"])
                    new_r[base_name]      = token
                    if meta["varies"] and token:
                        key        = _canon_key(base_name)
                        parts[key] = _pretty_value(key, token)
                    continue

                out_col      = meta["col"]
                new_r[out_col] = token
                if meta["varies"] and token:
                    key        = _canon_key(out_col)
                    parts[key] = _pretty_value(key, token)

            existing_sku = str(new_r.get(stock_col_name, "")).strip()
            if existing_sku:
                new_r[stock_out_h] = existing_sku
            else:
                new_r[stock_out_h] = (shorten_sku(master_val, parts, rules)
                                      if sku_rules_enabled(rules) else master_val)

            new_r["Short Title"]  = build_variant_short_title(
                original_title, style_name_base, parts, all_options_by_key)
            new_r["Description"]  = build_variant_description(
                style_name_base, parts, all_options_by_key, original_title)

            for k in list(new_r.keys()):
                if str(k).strip().lower().startswith("available "):
                    new_r[k] = normalize_list(str(new_r[k] or "").replace("#", ","))

            if has_price_column:
                if (bool(rules.get("price_rules", {}).get("_ui_enabled", True))
                        and price_rules_enabled(rules)):
                    new_r["Price"] = compute_variant_price(base_price_value, parts, rules)
                else:
                    new_r["Price"] = str(base_price_value or "").strip()

            if (not img_cols_present) and image_rules_enabled(rules):
                img_map = generate_image_urls(master_stock=master_val, parts=parts, rules=rules)
                for k, v in img_map.items():
                    new_r[k] = v

            final_rows.append(new_r)

    out_h = build_ordered_headers(final_rows, cols, has_price_column, include_images)

    buf = io.StringIO()
    w   = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    w.writerow(out_h)
    w.writerows([[r.get(h, "") for h in out_h] for r in final_rows])

    return buf.getvalue(), {
        "rows_out":          len(final_rows),
        "has_price_column":  has_price_column,
        "images_generated":  (not img_cols_present) and image_rules_enabled(rules),
        "sku_shortened":     sku_rules_enabled(rules),
        "pricing_applied":   (has_price_column
                              and bool(rules.get("price_rules", {}).get("_ui_enabled", True))
                              and price_rules_enabled(rules)),
        "refine_report":     refine_report,
        "clean_diff":        clean_diff,
    }


# ==========================================================
# 12. STREAMLIT FRONTEND
# ==========================================================
def _render_refine_report(report: RefineReport):
    """Render a structured Pre-flight Report inside Streamlit."""
    if not report.has_issues():
        st.success("✅ No issues found — your file is clean.")
        return

    st.warning(f"🔧 Refiner fixed **{report.total_changes}** issue(s) automatically before expansion.")

    if report.header_typos_fixed:
        with st.expander(f"Header typos fixed ({len(report.header_typos_fixed)})", expanded=True):
            for e in report.header_typos_fixed:
                st.markdown(f'- `{e["original"]}` → **`{e["fixed"]}`**')

    if report.separators_normalized:
        with st.expander(f"Separators normalised to comma ({len(report.separators_normalized)})"):
            for e in report.separators_normalized:
                st.markdown(f'- Column **{e["column"]}**: `{e["from"]}` → `,`')

    if report.numeric_typos_fixed:
        with st.expander(f"Numeric typos fixed ({len(report.numeric_typos_fixed)})", expanded=True):
            for e in report.numeric_typos_fixed:
                st.markdown(f'- Column **{e["column"]}**: `{e["original"]}` → `{e["fixed"]}`')

    if report.duplicates_removed:
        with st.expander(f"Duplicate values removed ({len(report.duplicates_removed)})"):
            for e in report.duplicates_removed:
                st.markdown(f'- Column **{e["column"]}**: removed {e["removed"]}')

    if report.empty_tokens_removed:
        with st.expander(f"Empty tokens removed ({len(report.empty_tokens_removed)})"):
            for e in report.empty_tokens_removed:
                st.markdown(f'- Column **{e["column"]}**')


def run_streamlit_app():
    if not _HAS_STREAMLIT:
        raise RuntimeError("Streamlit is not installed. Run: pip install streamlit")

    st.set_page_config(page_title="Inventory Expander", layout="wide")
    st.title("Jewelry Builder")

    if "rules" not in st.session_state:
        st.session_state["rules"] = load_rules()

    # ── Sidebar ──────────────────────────────────────────────────────────
    st.sidebar.header("Run Options")
    enable_sku     = st.sidebar.toggle("Enable SKU shortening",
                                        value=bool(st.session_state["rules"]
                                                   .get("sku_rules", {}).get("enabled", True)))
    enable_images  = st.sidebar.toggle("Enable Image URL generation",
                                        value=bool(st.session_state["rules"]
                                                   .get("image_rules", {}).get("enabled", False)))
    enable_pricing = st.sidebar.toggle("Enable Pricing adjustments", value=True)
    enable_refiner = st.sidebar.toggle("Enable Input Refiner (recommended)", value=True)

    st.sidebar.divider()
    st.sidebar.header("Rules Editor (JSON)")

    rules_text = st.sidebar.text_area(
        "Edit normalization_rules.json",
        value=json.dumps(st.session_state["rules"], indent=2),
        height=420,
    )

    colA, colB = st.sidebar.columns(2)
    with colA:
        if st.button("Validate JSON"):
            try:
                _ = json.loads(rules_text)
                st.sidebar.success("✅ JSON is valid.")
            except Exception as e:
                st.sidebar.error(f"❌ Invalid JSON: {e}")
    with colB:
        if st.button("Save rules.json"):
            try:
                new_rules = json.loads(rules_text)
                save_rules(new_rules)
                st.session_state["rules"] = new_rules
                st.sidebar.success("✅ Saved.")
            except Exception as e:
                st.sidebar.error(f"❌ Save failed: {e}")

    st.sidebar.download_button(
        "Download rules.json",
        data=rules_text.encode("utf-8"),
        file_name="normalization_rules.json",
        mime="application/json",
    )

    # ── Main ─────────────────────────────────────────────────────────────
    st.markdown("Upload a CSV with `Master stock` and any `Available …` variant columns.")
    up = st.file_uploader("Upload CSV", type=["csv", "txt"])
    if not up:
        st.stop()

    raw   = up.read().decode("utf-8", errors="ignore")
    rules = st.session_state["rules"]

    # ── Pre-flight: show refiner output before user runs expansion ───────
    st.subheader("① Pre-flight check")
    if enable_refiner:
        refined_csv, preflight_report = refine_input_csv(raw)
        _render_refine_report(preflight_report)
    else:
        refined_csv = raw
        st.info("Input Refiner is disabled — raw file will be used as-is.")

    # ── Header cleaning diff ─────────────────────────────────────────────
    st.subheader("② Header mapping")
    c = clean_input_csv(refined_csv, rules)
    col1, col2 = st.columns(2)
    with col1:
        st.write("**Unknown columns** (not in mapping file):")
        st.write(c["diff"].get("unknown_columns", []) or "None")
    with col2:
        st.write("**Header renames applied:**")
        renames = {k: v for k, v in c["diff"].get("header_renames", {}).items() if k != v}
        st.write(renames or "None")

    st.divider()

    # ── Run Expansion ─────────────────────────────────────────────────────
    st.subheader("③ Expand variants")
    if st.button("▶ Run Expansion", type="primary"):
        try:
            expanded, meta = expand_inventory(
                raw,                          # always start from raw; refiner runs internally
                rules=rules,
                enable_sku=enable_sku,
                enable_images=enable_images,
                enable_pricing=enable_pricing,
                skip_refiner=(not enable_refiner),
            )
        except ValueError as e:
            st.error(str(e))
            st.stop()

        st.success(f"Done. **{meta['rows_out']}** variant rows generated.")

        # Show inline refine report from expansion run
        report: Optional[RefineReport] = meta.get("refine_report")
        if report and report.has_issues():
            with st.expander("Refiner applied the following fixes during expansion"):
                for line in report.summary_lines():
                    st.markdown(line)

        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Rows out",      meta["rows_out"])
        col_b.metric("SKU shortened", "Yes" if meta["sku_shortened"]  else "No")
        col_c.metric("Pricing applied","Yes" if meta["pricing_applied"] else "No")

        st.download_button(
            label="⬇ Download Expanded CSV",
            data=expanded.encode("utf-8"),
            file_name="expanded.csv",
            mime="text/csv",
        )

        st.subheader("Preview (first 50 rows)")
        df_preview = pd.read_csv(io.StringIO(expanded))
        st.dataframe(df_preview.head(50), use_container_width=True)


# ==========================================================
# 13. ENTRYPOINT
# ==========================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["streamlit"], default="streamlit")
    args, _ = parser.parse_known_args()
    run_streamlit_app()

if __name__ == "__main__":
    main()
