#!/usr/bin/env python3
"""Measure a set of Lean projects with leanstat and write a TSV (plus optional treemaps).

    python3 data/measure.py CLONES_DIR OUT_DIR [--maps MAPS_DIR]

CLONES_DIR holds shallow clones named owner_repo (see PROJECTS below). Each project is measured at
whatever commit is checked out; the commit is recorded in the TSV.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import leanstat  # noqa: E402

# (repository, folder inside the clone, what it is, map file name or None)
PROJECTS = [
    ("CocoSgt/riemann-hypothesis-lean", "", "Riemann Hypothesis: the release of 20 Nov 2026 (mirror)", None),
    ("leanprover-community/riemann-hypothesis", "", "Riemann Hypothesis: community cleanup", "riemann-hypothesis.html"),
    ("open-goat/pequalnp-proofs", "", "pequalnp.com: public AI attempt at P vs NP (says P vs NP is not settled)", None),
    ("leanprover-community/mathlib4", "Mathlib", "Mathlib: the whole library (Mathlib/ folder)", "mathlib.html"),
    ("tristanbuckmaster/navier-stokes", "", "Finite-time blow-up for Navier–Stokes (Alpöge, Buckmaster)", "navier-stokes.html"),
    ("mccorvie/riemann-masullo", "", "Masullo's GRH paper (unrefereed), being formalized", None),
    ("teorth/equational_theories", "", "Equational Theories Project", None),
    ("fpvandoorn/carleson", "", "Carleson's theorem", None),
    ("ImperialCollegeLondon/FLT", "", "Fermat's Last Theorem (in progress)", None),
    ("teorth/pfr", "", "Polynomial Freiman–Ruzsa conjecture", None),
    ("thefundamentaltheor3m/Sphere-Packing-Lean", "", "Sphere packing in dimension 8", None),
]
TITLES = {
    "riemann-hypothesis.html": "The Lean proof of the Riemann Hypothesis, mapped",
    "mathlib.html": "Mathlib, mapped",
    "navier-stokes.html": "Navier–Stokes blow-up in Lean, mapped",
}


def main(argv):
    clones, out = argv[0], argv[1]
    maps = argv[argv.index("--maps") + 1] if "--maps" in argv else None
    only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else None
    os.makedirs(out, exist_ok=True)
    rows = []
    for repo, sub, what, map_name in PROJECTS:
        if only and repo not in only:
            continue
        clone = os.path.join(clones, repo.replace("/", "_"))
        if not os.path.isdir(clone):
            print(f"skip {repo}: no clone at {clone}", file=sys.stderr)
            continue
        commit = subprocess.run(["git", "-C", clone, "log", "-1", "--format=%h\t%cd", "--date=short"],
                                capture_output=True, text=True).stdout.strip() or "?\t?"
        r = leanstat.scan(os.path.join(clone, sub) if sub else clone)
        with open(os.path.join(out, repo.replace("/", "_") + ".json"), "w", encoding="utf-8") as fh:
            json.dump(leanstat.public(r), fh, ensure_ascii=False, indent=1)
        if maps and map_name:
            os.makedirs(maps, exist_ok=True)
            with open(os.path.join(maps, map_name), "w", encoding="utf-8") as fh:
                fh.write(leanstat.render_html(r, TITLES.get(map_name)))
        L = r["lines"]
        rows.append([repo, sub or ".", what, *commit.split("\t"), r["files"], L["lines"], L["code"], L["comment"], L["blank"],
                     r["theorems"], sum(r["declarations"].values()), r["sorry"]["total"],
                     "" if r["sorry"]["in_proof"] is None else r["sorry"]["in_proof"], len(r["axioms"]),
                     r["imports"]["longest_chain"], len(r["comparators"]), r["meta"].get("toolchain", "")])
        print(f"{repo}: {L['code']:,} code lines", file=sys.stderr)
    head = ["repo", "folder", "what", "commit", "commit_date", "files", "lines", "code_lines", "comment_lines", "blank_lines",
            "theorems_and_lemmas", "declarations", "sorry_total", "sorry_in_checked_proof", "axioms_declared",
            "longest_import_chain", "comparator_configs", "toolchain"]
    path = os.path.join(out, "projects.tsv")
    existing = {}
    if os.path.exists(path) and only:
        with open(path, encoding="utf-8") as fh:
            for line in fh.read().splitlines()[1:]:
                existing[line.split("\t")[0]] = line.split("\t")
    for row in rows:
        existing[row[0]] = [str(x) for x in row]
    order = [p[0] for p in PROJECTS]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(head) + "\n")
        for repo in order:
            if repo in existing:
                fh.write("\t".join(str(x) for x in existing[repo]) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
