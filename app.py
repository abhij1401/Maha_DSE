"""
app.py  —  MAHA-DSE College Predictor
Direct CSV query edition (long-format cutoff_data.csv)
Author: Abhishek Jadhav
"""

from pathlib import Path
import re
import pandas as pd
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
_ROOT    = Path(__file__).resolve().parent
CSV_PATH = _ROOT / "data" / "processed" / "cutoff_data.csv"

# ── Branch code → name  (Maharashtra DSE CET Cell official codes) ─────────────
BRANCH_MAP: dict[str, str] = {
    "00": "First Year Engineering (Direct)",
    "01": "Architecture",
    "11": "Civil Engineering",
    "12": "Mechanical Engineering",
    "19": "Electrical Engineering",
    "21": "Bio-Medical Engineering",
    "24": "Electronics Engineering",
    "25": "Electronics and Computer Engineering",
    "26": "Computer Science and Engineering",
    "29": "Mechanical Engineering (Sandwich)",
    "35": "Textile Engineering",
    "37": "Computer Engineering",
    "41": "Information Technology",
    "46": "Printing Technology",
    "50": "Instrumentation Engineering",
    "52": "Petroleum Engineering",
    "56": "Electronics and Telecommunication Engineering",
    "60": "Production Engineering",
    "61": "Electronics and Telecommunication Engineering",
    "62": "Civil Engineering",
    "69": "Engineering (Integrated)",
    "70": "Mining Engineering",
    "84": "Chemical Engineering",
    "89": "Chemical Engineering",
    "90": "Automation and Robotics",
    "91": "Artificial Intelligence and Data Science",
    "92": "Artificial Intelligence and Machine Learning",
    "93": "Production Engineering",
    "99": "Electronics and Telecommunication Engineering",
}

# ── Caste → category token map ───────────────────────────────────────────────
CASTE_TOKEN: dict[str, str] = {
    "OPEN": "OPEN", "OBC": "OBC", "SEBC": "SEBC",
    "SC": "SC",     "ST": "ST",   "VJ": "VJA",
    "NT-A": "NTA",  "NT-B": "NTB","NT-C": "NTC",
    "NT-D": "NTD",  "SBC": "SBC", "EWS": "EWS",
}

# Regex to extract tokens from the concatenated category string
_CAT_RE = re.compile(
    r"(PWDR?|DEF|OEW|EWS|[GL](?:OPEN|SC|ST|NTA|NTB|NTC|NTD|OBC|SEBC|SBC|VJA?))"
)

# ── Dataset singleton ────────────────────────────────────────────────────────
_df: pd.DataFrame | None = None


def get_df() -> pd.DataFrame:
    global _df
    if _df is None:
        raw = pd.read_csv(CSV_PATH)
        raw.columns = [c.strip().lower() for c in raw.columns]

        # Decode branch from choice_code (digits 4-5 of the 9-digit code)
        cc = raw["choice_code"].astype(str).str.zfill(9)
        raw["branch_code"] = cc.str[4:6]
        raw["branch_name"] = raw["branch_code"].map(BRANCH_MAP).fillna(
            "Branch " + raw["branch_code"]
        )
        _df = raw
    return _df


# ── Category matching ────────────────────────────────────────────────────────

def _build_tokens(caste: str, domicile: str) -> list[str]:
    """Return priority-ordered category tokens for this caste+domicile."""
    code = CASTE_TOKEN.get(caste.upper(), caste.upper())
    if domicile == "Maharashtra":
        return [f"L{code}", f"G{code}"]   # local first, then state
    return [f"G{code}"]                    # outside MH: state seats only


def _row_matches(category_str: str | float, token_set: set[str]) -> bool:
    if pd.isna(category_str):
        return False
    found = set(_CAT_RE.findall(str(category_str)))
    return bool(found & token_set)


# ── Core prediction ──────────────────────────────────────────────────────────

def predict_colleges(
    percentile: float,
    caste: str,
    gender: str,
    domicile: str,
    cap_round: int,
    preferred_branch_names: list[str],
) -> list[dict]:
    df = get_df()
    tokens     = _build_tokens(caste, domicile)
    token_set  = set(tokens)

    # ── 1. Round filter ──────────────────────────────────────────────────────
    # Round 1 in the CSV has NO percentile (only rank).
    # We use Round 2 data as the cutoff reference in both cases,
    # but label it correctly.  Stage-II preferred over Stage-I.
    sub = df[df["round"] == 2].copy()

    # ── 2. Category filter ───────────────────────────────────────────────────
    sub = sub[sub["category"].apply(lambda c: _row_matches(c, token_set))]

    # ── 3. Keep only rows with a valid percentile cutoff ─────────────────────
    sub = sub[sub["percentile"].notna()].copy()

    if sub.empty:
        return []

    # ── 4. Branch filter (if user selected specific branches) ────────────────
    if preferred_branch_names:
        # Normalize both sides to lowercase for case-insensitive match
        selected_lower = {b.lower() for b in preferred_branch_names}
        sub = sub[sub["branch_name"].str.lower().isin(selected_lower)]

    if sub.empty:
        return []

    # ── 5. Prefer Stage-II over Stage-I for same choice_code ─────────────────
    stage2 = sub[sub["stage"] == "Stage-II"]
    stage1 = sub[sub["stage"] == "Stage-I"]
    stage2_codes = set(stage2["choice_code"].unique())
    combined = pd.concat(
        [stage2, stage1[~stage1["choice_code"].isin(stage2_codes)]],
        ignore_index=True,
    )

    # ── 6. Score each row ─────────────────────────────────────────────────────
    results = []
    for _, row in combined.iterrows():
        cutoff = float(row["percentile"])
        diff   = round(percentile - cutoff, 2)

        if diff >= 8:
            tier, label, chance = "safe",        "Safe",        min(99, 85 + diff)
        elif diff >= 4:
            tier, label, chance = "high_chance", "High Chance", 70 + diff * 2
        elif diff >= 0:
            tier, label, chance = "target",      "Target",      50 + diff * 5
        elif diff >= -4:
            tier, label, chance = "dream",       "Dream",       max(25, 45 + diff * 5)
        else:
            tier, label, chance = "very_dream",  "Very Dream",  max(5,  30 + diff * 3)

        results.append({
            "college_code":  str(row.get("college_code", "")),
            "college_name":  str(row.get("college_name", "Unknown")).strip(),
            "branch_name":   str(row.get("branch_name", "")),
            "branch_code":   str(row.get("branch_code", "")),
            "choice_code":   str(row.get("choice_code", "")),
            "stage":         str(row.get("stage", "")),
            "cutoff":        round(cutoff, 2),
            "diff":          diff,
            "chance":        round(min(99, max(1, chance)), 1),
            "tier":          tier,
            "tier_label":    label,
            "round":         int(row.get("round", cap_round)),
        })

    # ── 7. Sort order ─────────────────────────────────────────────────────────
    # Goal: start from colleges closest to user's percentile (most competitive
    # they can realistically get), then progressively easier.
    # For high scorers (≥90): open with ~50% chance colleges first so they
    # see aspirational picks at the top, then safe bets below.
    if percentile >= 90:
        # Sort: closest diff to 0 first (cutoff ≈ your percentile = ~50% chance),
        # then negative diff (dream), then larger positive diff (safe).
        # This gives the "50% → safer" order requested.
        results.sort(key=lambda c: abs(c["diff"]))
    else:
        # For others: show best realistic chances first (cutoff just below theirs),
        # then go down to dream colleges.
        results.sort(key=lambda c: c["diff"], reverse=True)

    return results[:200]


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    df     = get_df()
    rounds = sorted(int(r) for r in df["round"].dropna().unique())
    # Unique branch names from actual data (for checkbox list)
    branches = sorted(df["branch_name"].dropna().unique().tolist())
    return render_template(
        "index.html",
        years=[],          # removed from form
        branches=branches, # real branch names from CSV
        rounds=rounds,
    )


@app.route("/predict", methods=["POST"])
def predict():
    try:
        percentile  = float(request.form["percentile"])
        caste       = request.form["caste"]
        gender      = request.form["gender"]
        domicile    = request.form["domicile"]
        cap_round   = int(request.form["cap_round"])
        pref_branches = request.form.getlist("preferred_branches")

        colleges = predict_colleges(
            percentile=percentile,
            caste=caste,
            gender=gender,
            domicile=domicile,
            cap_round=cap_round,
            preferred_branch_names=pref_branches,
        )

        return render_template(
            "result.html",
            percentile=percentile,
            caste=caste,
            gender=gender,
            domicile=domicile,
            cap_round=cap_round,
            academic_year=2024,
            selected_branches=pref_branches,
            colleges=colleges,
            total=len(colleges),
        )

    except Exception as e:
        import traceback
        return render_template("error.html", error=f"{e}\n\n{traceback.format_exc()}")


@app.route("/api/branches/<int:year>")
def api_branches(year):
    df = get_df()
    return jsonify(sorted(df["branch_name"].dropna().unique().tolist()))


@app.route("/api/years")
def api_years():
    df = get_df()
    return jsonify(sorted(int(y) for y in df["academic_year"].dropna().unique()))


@app.route("/api/rounds")
def api_rounds():
    df = get_df()
    return jsonify(sorted(int(r) for r in df["round"].dropna().unique()))


@app.route("/health")
def health():
    df = get_df()
    return {"status": "ok", "rows": len(df), "branches": len(df["branch_name"].unique())}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)