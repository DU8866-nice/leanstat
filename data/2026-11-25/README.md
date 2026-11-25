# Measurements, 25 November 2026

`projects.tsv` has one row per project: the commit measured, files, lines (total, code, comment,
blank), theorems and lemmas, all declarations, `sorry` counts (total, and inside the comparator
solution's imports when there is a comparator config), declared axioms, the longest internal import
chain, the number of comparator configs and the Lean toolchain. The `.json` files are the full
`leanstat --json` output for each project.

How it was made:

1. Shallow clones (`git clone --depth 1`) of each repository on 25 November 2026, named
   `owner_repo`. For Mathlib only the `Mathlib/` folder is measured (the library, without tests,
   `Archive/` or `Counterexamples/`).
2. `python3 data/measure.py CLONES data/2026-11-25 --maps maps`.

`CocoSgt/riemann-hypothesis-lean` is a mirror of the release of 20 November 2026; the
`original` branch of `leanprover-community/riemann-hypothesis` preserves the same release.

Code lines exclude comments and blank lines. Nothing was built; nothing here says whether a proof
is correct.
