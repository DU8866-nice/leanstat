#!/usr/bin/env python3
"""leanstat: cloc for Lean 4 projects, with the numbers a proof needs.

    python3 leanstat.py PATH                 # summary
    python3 leanstat.py PATH --json          # everything, machine-readable
    python3 leanstat.py PATH --html map.html # interactive treemap of the project

Counts code, comment and blank lines with a real Lean lexer (nested /- -/ comments, strings,
char literals), declarations by kind, `sorry`/`admit` outside comments and strings, declared
axioms, trust-widening features (native_decide, implemented_by, extern, unsafe,
debug.skipKernelTC), heartbeat overrides, the internal import graph, and comparator setups,
whose challenge files hold `sorry` by design.

It reads files; it does not build or check anything. To know whether a proof is correct,
build it and run comparator or `#print axioms`. Standard library only.
"""
from __future__ import annotations

import argparse
import bisect
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict

__version__ = "0.1.0"

SKIP_DIRS = {".git", ".lake", "lake-packages", "build", ".github", "node_modules", "_target"}

# --------------------------------------------------------------------------- lexer

CODE_TOKENS = re.compile(
    r"(?P<open>/-)"
    r"|(?P<line>--[^\n]*)"
    r"|(?P<char>(?<![\w'.!?])'(?:\\.|[^'\\\n])')"
    r"|(?P<raw>(?<![\w'.!?])r(?P<hashes>#*)\")"
    r"|(?P<str>\"(?:\\.|[^\"\\])*\")",
    re.S,
)
COMMENT_TOKENS = re.compile(r"/-|-/")


def strip(text: str):
    """Return (code, comment_mask_text): code with comments and string contents blanked.

    Newlines are kept, so line numbers still line up. The second value has comment text in
    place and code blanked, for telling comment lines from blank ones.
    """
    code_parts, com_parts = [], []
    i, n = 0, len(text)

    def blank(s):
        return re.sub(r"[^\n]", " ", s)

    while i < n:
        m = CODE_TOKENS.search(text, i)
        if not m:
            code_parts.append(text[i:])
            com_parts.append(blank(text[i:]))
            break
        code_parts.append(text[i:m.start()])
        com_parts.append(blank(text[i:m.start()]))
        if m.group("open"):
            depth, j = 1, m.end()
            while depth and j < n:
                c = COMMENT_TOKENS.search(text, j)
                if not c:
                    j = n
                    break
                depth += 1 if c.group(0) == "/-" else -1
                j = c.end()
            seg = text[m.start():j]
            code_parts.append(blank(seg))
            com_parts.append(seg)
            i = j
        elif m.group("line"):
            code_parts.append(blank(m.group(0)))
            com_parts.append(m.group(0))
            i = m.end()
        elif m.group("raw") is not None:
            close = text.find('"' + m.group("hashes"), m.end())
            j = n if close == -1 else close + 1 + len(m.group("hashes"))
            seg = text[m.start():j]
            code_parts.append('r"' + blank(seg[2:-1]) + '"' if len(seg) > 2 else seg)
            com_parts.append(blank(seg))
            i = j
        else:  # string or char literal: keep the quotes, blank the inside
            seg = m.group(0)
            code_parts.append(seg[0] + blank(seg[1:-1]) + seg[-1])
            com_parts.append(blank(seg))
            i = m.end()
    return "".join(code_parts), "".join(com_parts)


# --------------------------------------------------------------------------- per-file analysis

MODIFIERS = r"(?:(?:private|protected|noncomputable|partial|unsafe|nonrec|scoped|local)[ \t]+)*"
DECL_RE = re.compile(
    r"^[ \t]*(?:@\[[^\]\n]*\][ \t]*)?" + MODIFIERS +
    r"(?P<kind>theorem|lemma|def|abbrev|instance|structure|class|inductive|axiom|opaque|example|irreducible_def)\b"
    r"(?:[ \t]+(?:inductive[ \t]+)?(?P<name>[^\s:({\[]+))?",
    re.M,
)
TOP_COMMAND = re.compile(r"^(?:end|namespace|section|open|variable|universe|attribute|set_option|noncomputable section|public section|module|"
                         r"#\w+|import|macro|syntax|elab|notation|infix|infixl|infixr|prefix|postfix|mutual|deriving)\b", re.M)
SORRY_RE = re.compile(r"(?<![\w.'])(sorry|admit)(?![\w'!?])")
AXIOM_RE = re.compile(r"^[ \t]*(?:@\[[^\]\n]*\][ \t]*)?(?:(?:private|protected|unsafe)[ \t]+)*axiom[ \t]+([^\s:({\[]+)", re.M)
# Lean's module system adds `public import`, `meta import` and `import all`.
IMPORT_RE = re.compile(r"^[ \t]*(?:(?:public|private|meta)[ \t]+)*import[ \t]+(?:all[ \t]+)?([^\s]+(?:[ \t]+[^\s-][^\s]*)*)", re.M)
FLAGS = {
    "native_decide": re.compile(r"(?<![\w.])native_decide(?![\w'])|Lean\.ofReduceBool"),
    "implemented_by": re.compile(r"implemented_by"),
    "extern": re.compile(r"@\[\s*extern\b"),
    "unsafe": re.compile(r"(?<![\w.])unsafe(?![\w'])"),
    "skipKernelTC": re.compile(r"debug\.skipKernelTC"),
}
HEARTBEATS = re.compile(r"set_option[ \t]+(?:synthInstance\.)?maxHeartbeats[ \t]+(\d+)")
META_RE = re.compile(r"^[ \t]*(?:@\[[^\]\n]*\][ \t]*)?(?:(?:private|protected|scoped|local)[ \t]+)*(macro_rules|macro|syntax|elab_rules|elab)\b", re.M)


def analyze_file(text: str):
    code, comments = strip(text)
    code_lines = comment_lines = blank_lines = 0
    code_rows, com_rows = code.split("\n"), comments.split("\n")
    if text.endswith("\n"):  # the newline ends the last line; it doesn't start another
        code_rows, com_rows = code_rows[:-1], com_rows[:-1]
    for cl, ml in zip(code_rows, com_rows):
        if cl.strip():
            code_lines += 1
        elif ml.strip():
            comment_lines += 1
        else:
            blank_lines += 1
    decls = Counter()
    longest = (0, "", 0)
    starts = [(m.start(), m.group("kind"), m.group("name") or "") for m in DECL_RE.finditer(code)]
    stops = sorted([s for s, _, _ in starts] + [m.start() for m in TOP_COMMAND.finditer(code)] + [len(code)])
    for pos, kind, name in starts:
        decls[kind] += 1
        nxt = stops[bisect.bisect_right(stops, pos)] if bisect.bisect_right(stops, pos) < len(stops) else len(code)
        n = code.count("\n", pos, nxt)
        if n > longest[0]:
            longest = (n, f"{kind} {name}".strip(), code.count("\n", 0, pos) + 1)
    sorry_lines = []
    for m in SORRY_RE.finditer(code):
        line_start = code.rfind("\n", 0, m.start()) + 1
        before = code[line_start:m.start()]
        # not a proof hole: a syntax quotation in a tactic (`(tactic| sorry), q(sorry : t)),
        # a #check/#eval command, or a constructor that happens to be called sorry
        if "`(" in before or "q(" in before or re.match(r"\s*#\w+", before) or re.fullmatch(r"\s*\|\s*", before):
            continue
        sorry_lines.append(code.count("\n", 0, m.start()) + 1)
    axioms = [m.group(1) for m in AXIOM_RE.finditer(code)]
    flags = {k: len(rx.findall(code)) for k, rx in FLAGS.items()}
    beats = [int(x) for x in HEARTBEATS.findall(code)]
    imports = []
    for m in IMPORT_RE.finditer(code):
        imports.extend(m.group(1).split())
    return {
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "code": code_lines, "comment": comment_lines, "blank": blank_lines,
        "decls": dict(decls), "longest": longest, "sorry_lines": sorry_lines, "axioms": axioms,
        "flags": flags, "heartbeats": beats, "imports": imports, "meta": len(META_RE.findall(code)),
    }


# --------------------------------------------------------------------------- project


def lean_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.endswith(".lean"):
                full = os.path.join(dirpath, fn)
                yield full, os.path.relpath(full, root).replace(os.sep, "/")


def source_roots(root):
    """Folders that module names are relative to: each lakefile's folder joined with its srcDir."""
    roots = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in ("lakefile.toml", "lakefile.lean"):
            if fn in filenames:
                try:
                    with open(os.path.join(dirpath, fn), encoding="utf-8") as fh:
                        text = fh.read()
                except OSError:
                    continue
                rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
                rel = "" if rel == "." else rel + "/"
                dirs = re.findall(r'srcDir\s*(?::=|=)\s*"([^"]*)"', text) + ["."]
                for d in dirs:
                    d = d.strip("./")
                    roots.append(rel + (d + "/" if d else ""))
    return sorted(set(roots)) or [""]


def module_index(rels, roots=("",), root_name=""):
    """Module name -> file. Exact paths under a source root win; otherwise any path suffix, shallowest first."""
    idx = {}
    if root_name:  # scanning a package folder itself (e.g. mathlib4/Mathlib): its modules start with its name
        for rel in rels:
            idx.setdefault(root_name + "." + rel[:-5].replace("/", "."), rel)
    for r in sorted(roots, key=len, reverse=True):
        for rel in rels:
            if rel.startswith(r):
                idx.setdefault(rel[len(r):-5].replace("/", "."), rel)
    for rel in sorted(rels, key=lambda x: (x.count("/"), x)):
        parts = rel[:-5].split("/")
        for k in range(len(parts)):
            idx.setdefault(".".join(parts[k:]), rel)
    return idx


def read_comparators(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("comparator") and fn.endswith(".json"):
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict) and "challenge_module" in data:
                    out.append({"file": os.path.relpath(path, root).replace(os.sep, "/"),
                                "challenge": data.get("challenge_module"), "solution": data.get("solution_module"),
                                "theorems": data.get("theorem_names") or [],
                                "axioms": data.get("permitted_axioms") or []})
    return sorted(out, key=lambda c: c["file"])


def project_meta(root):
    meta = {}
    try:
        with open(os.path.join(root, "lean-toolchain"), encoding="utf-8") as fh:
            meta["toolchain"] = fh.read().strip()
    except OSError:
        pass
    for cand in ("lake-manifest.json", "lean/lake-manifest.json"):
        try:
            with open(os.path.join(root, cand), encoding="utf-8") as fh:
                man = json.load(fh)
            for p in man.get("packages", []):
                if p.get("name") == "mathlib":
                    meta["mathlib"] = (p.get("rev") or "")[:10]
            break
        except (OSError, ValueError):
            continue
    return meta


def longest_chain(graph):
    """Length (in modules) of the longest import chain in an acyclic graph."""
    memo = {}
    for start in graph:
        if start in memo:
            continue
        stack = [(start, iter(graph.get(start, ())))]
        onpath = {start}
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                stack.pop()
                onpath.discard(node)
                memo[node] = 1 + max((memo.get(c, 0) for c in graph.get(node, ())), default=0)
            elif nxt not in memo and nxt not in onpath:
                onpath.add(nxt)
                stack.append((nxt, iter(graph.get(nxt, ()))))
    return max(memo.values(), default=0)


def scan(root, challenge_globs=()):
    root = os.path.abspath(root)
    files = {}
    for full, rel in lean_files(root):
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                files[rel] = analyze_file(fh.read())
        except OSError:
            continue
    rels = sorted(files)
    idx = module_index(rels, source_roots(root), os.path.basename(root.rstrip("/")))
    comparators = read_comparators(root)
    challenge = set()
    for c in comparators:
        f = idx.get(c["challenge"]) if c["challenge"] else None
        if f:
            challenge.add(f)
            c["challenge_file"] = f
    for g in challenge_globs:
        rx = re.compile("^" + re.escape(g).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$")
        challenge |= {r for r in rels if rx.match(r)}

    tot = Counter()
    decls = Counter()
    flags = Counter()
    axioms = []
    beats_zero, beats_max = 0, 0
    graph = defaultdict(set)
    imported_by = Counter()
    external = Counter()
    for rel in rels:
        f = files[rel]
        for k in ("lines", "code", "comment", "blank"):
            tot[k] += f[k]
        decls.update(f["decls"])
        flags.update({k: v for k, v in f["flags"].items() if v})
        axioms += [f"{a} ({rel})" for a in f["axioms"]]
        beats_zero += sum(1 for b in f["heartbeats"] if b == 0)
        beats_max = max([beats_max] + [b for b in f["heartbeats"] if b])
        for imp in f["imports"]:
            tgt = idx.get(imp)
            if tgt and tgt != rel:
                graph[rel].add(tgt)
                imported_by[tgt] += 1
            else:
                external[imp.split(".")[0]] += 1
    # What the checked theorems can depend on: everything the comparator solution modules import.
    solutions = [idx.get(c["solution"]) for c in comparators if c.get("solution") and idx.get(c["solution"])]
    reachable = set()
    todo = list(solutions)
    while todo:
        f = todo.pop()
        if f in reachable:
            continue
        reachable.add(f)
        todo.extend(graph.get(f, ()))
    if not challenge and not comparators:
        challenge = {r for r in rels if re.search(r"Challenge[^/]*\.lean$", r)}
    sorry_proof, sorry_challenge, sorry_other = [], [], []
    for rel in rels:
        for ln in files[rel]["sorry_lines"]:
            loc = f"{rel}:{ln}"
            if solutions and rel in reachable:
                sorry_proof.append(loc)
            elif rel in challenge:
                sorry_challenge.append(loc)
            else:
                sorry_other.append(loc)
    unused = [r for r in rels if solutions and r not in reachable and r not in challenge]
    biggest = sorted(rels, key=lambda r: -files[r]["code"])[:10]
    longest = max(((files[r]["longest"][0], files[r]["longest"][1], r, files[r]["longest"][2]) for r in rels),
                  default=(0, "", "", 0))
    flag_files = defaultdict(list)
    flags_in_proof = Counter()
    for rel in rels:
        for k, v in files[rel]["flags"].items():
            if v:
                flag_files[k].append(rel)
                if solutions and rel in reachable:
                    flags_in_proof[k] += v
    return {
        "root": root,
        "name": os.path.basename(root.rstrip("/")),
        "meta": project_meta(root),
        "files": len(rels),
        "lines": dict(tot),
        "declarations": dict(decls),
        "theorems": decls["theorem"] + decls["lemma"],
        "biggest_files": [{"file": r, "code": files[r]["code"]} for r in biggest],
        "longest_declaration": {"lines": longest[0], "name": longest[1], "file": longest[2], "line": longest[3]},
        "sorry": {"total": len(sorry_proof) + len(sorry_challenge) + len(sorry_other),
                  "in_proof": len(sorry_proof) if solutions else None, "in_challenge": len(sorry_challenge),
                  "elsewhere": len(sorry_other), "where_proof": sorry_proof[:50], "where_elsewhere": sorry_other[:50]},
        "solution_closure": {"files": len(reachable), "code": sum(files[r]["code"] for r in reachable),
                             "unused_files": len(unused), "unused_code": sum(files[r]["code"] for r in unused)} if solutions else None,
        "axioms": axioms,
        "flags": {k: {"count": flags[k], "in_proof": flags_in_proof[k] if solutions else None, "files": flag_files[k][:20]}
                  for k in FLAGS},
        "heartbeats": {"set_to_zero": beats_zero, "largest": beats_max},
        "metaprogramming": sum(files[r]["meta"] for r in rels),
        "imports": {"internal_edges": sum(len(v) for v in graph.values()), "longest_chain": longest_chain(graph),
                    "most_imported": [{"file": r, "importers": n} for r, n in imported_by.most_common(10)],
                    "external_roots": dict(external.most_common(10))},
        "comparators": comparators,
        "challenge_files": sorted(challenge),
        "_files": files,
    }


# --------------------------------------------------------------------------- output


def fmt(n):
    return f"{n:,}"


def render_text(r, top=5):
    L = r["lines"]
    d = r["declarations"]
    out = []
    meta = ", ".join(f"{k} {v}" for k, v in r["meta"].items())
    out.append(f"leanstat  {r['name']}" + (f"  ({meta})" if meta else ""))
    out.append("")
    out.append(f"  files          {fmt(r['files'])}")
    out.append(f"  lines          {fmt(L.get('lines', 0))}   code {fmt(L.get('code', 0))} · comments {fmt(L.get('comment', 0))} · blank {fmt(L.get('blank', 0))}")
    kinds = ["theorem", "lemma", "def", "abbrev", "instance", "structure", "class", "inductive", "example"]
    out.append(f"  declarations   {fmt(sum(d.values()))}   " + " · ".join(f"{k} {fmt(d[k])}" for k in kinds if d.get(k)))
    ld = r["longest_declaration"]
    if ld["lines"]:
        out.append(f"  longest decl   {fmt(ld['lines'])} lines: {ld['name']} ({ld['file']}:{ld['line']})")
    if r["biggest_files"]:
        b = r["biggest_files"][0]
        out.append(f"  biggest file   {fmt(b['code'])} code lines: {b['file']}")
    im = r["imports"]
    ext = ", ".join(f"{k} ×{fmt(v)}" for k, v in list(im["external_roots"].items())[:4])
    out.append(f"  imports        {fmt(im['internal_edges'])} internal edges · longest chain {fmt(im['longest_chain'])} modules"
               + (f" · imports of {ext}" if ext else ""))
    out.append("")
    for c in r["comparators"]:
        out.append(f"  comparator     {c['file']}: {len(c['theorems'])} theorem(s), challenge {c['challenge']}"
                   f"{' (' + c['challenge_file'] + ')' if c.get('challenge_file') else ' (file not found)'}, "
                   f"solution {c['solution']}, axioms {', '.join(c['axioms']) or 'none listed'}")
    s = r["sorry"]
    sc = r.get("solution_closure")
    if sc:
        out.append(f"  checked proof  the solution module(s) import {fmt(sc['files'])} files ({fmt(sc['code'])} code lines); "
                   f"{fmt(sc['unused_files'])} other files ({fmt(sc['unused_code'])} lines) are not imported by it")
        if s["in_proof"]:
            out.append(f"  sorry/admit    {fmt(s['in_proof'])} inside the solution's imports, e.g. " + ", ".join(s["where_proof"][:top]))
        else:
            out.append("  sorry/admit    none inside the solution's imports")
        if s["in_challenge"]:
            out.append(f"                 {fmt(s['in_challenge'])} in comparator challenge files, where statements are left as sorry on purpose")
        if s["elsewhere"]:
            out.append(f"                 {fmt(s['elsewhere'])} in files the solution doesn't import, e.g. " + ", ".join(s["where_elsewhere"][:3]))
    elif s["total"]:
        out.append(f"  sorry/admit    {fmt(s['total'])}"
                   + (f" ({fmt(s['in_challenge'])} in files named *Challenge*)" if s["in_challenge"] else "")
                   + ", e.g. " + ", ".join((s["where_elsewhere"] or s["where_proof"])[:top]))
    else:
        out.append("  sorry/admit    none")
    out.append("  axioms         " + ("; ".join(r["axioms"][:top]) + (f" (+{len(r['axioms']) - top} more)" if len(r["axioms"]) > top else "")
                                   if r["axioms"] else "none declared"))
    fl = [f"{k} ×{v['count']}" for k, v in r["flags"].items() if v["count"]]
    if sc:
        inside = [f"{k} ×{v['in_proof']}" for k, v in r["flags"].items() if v.get("in_proof")]
        out.append("  trust flags    " + (" · ".join(inside) + " inside the solution's imports" if inside
                                         else "none inside the solution's imports") + (f" ({' · '.join(fl)} in other files)" if fl and not inside else ""))
    else:
        out.append("  trust flags    " + (" · ".join(fl) if fl else "none (no native_decide, implemented_by, extern, unsafe, skipKernelTC)"))
    hb = r["heartbeats"]
    out.append(f"  heartbeats     maxHeartbeats 0 ×{fmt(hb['set_to_zero'])}" + (f" · largest explicit value {fmt(hb['largest'])}" if hb["largest"] else ""))
    out.append("")
    out.append("  Counts only. A clean list here is not a proof check: build the project and run comparator or #print axioms.")
    return "\n".join(out)


def public(r):
    return {k: v for k, v in r.items() if not k.startswith("_")}


def tree_for_html(r):
    """Nested [name, code, sorry, flags, children] lists for the treemap."""
    root = {"n": r["name"], "c": {}}
    ch = set(r["challenge_files"])
    for rel, f in r["_files"].items():
        node = root
        parts = rel.split("/")
        for p in parts[:-1]:
            node = node["c"].setdefault(p, {"n": p, "c": {}})
        status = 3 if rel in ch else (2 if f["sorry_lines"] else (1 if f["axioms"] or any(f["flags"].values()) else 0))
        node["c"][parts[-1]] = {"n": parts[-1], "v": max(f["code"], 1), "s": status,
                                "d": sum(f["decls"].values())}

    def pack(node):
        if "v" in node:
            return [node["n"], node["v"], node["s"], node["d"]]
        return [node["n"], [pack(c) for c in node["c"].values()]]

    return pack(root)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--ink:#1d1d1b;--muted:#6b6a65;--line:#e4e2dc;--bg:#fbfaf8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
header{padding:14px 18px 6px}h1{font-size:18px;margin:0 0 2px}.sub{color:var(--muted);font-size:13px}
.stats{display:flex;flex-wrap:wrap;gap:8px 22px;padding:6px 18px 10px;font-size:13px}.stats b{font-size:15px;font-variant-numeric:tabular-nums}
.bar{display:flex;align-items:center;gap:10px;padding:4px 18px 8px;font-size:13px;flex-wrap:wrap}
.crumbs a{color:#2a78d6;cursor:pointer;text-decoration:none}.crumbs span{color:var(--muted)}
.legend{margin-left:auto;display:flex;gap:12px;color:var(--muted)}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
#wrap{position:relative;margin:0 18px 18px;height:calc(100vh - 150px);min-height:420px;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}
canvas{display:block;width:100%;height:100%}
#tip{position:absolute;pointer-events:none;background:#1d1d1b;color:#fff;padding:6px 9px;border-radius:6px;font-size:12px;display:none;max-width:420px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
footer{padding:0 18px 16px;color:var(--muted);font-size:12px}
</style></head><body>
<header><h1>__TITLE__</h1><div class="sub">__SUBTITLE__</div></header>
<div class="stats">__STATS__</div>
<div class="bar"><div class="crumbs" id="crumbs"></div>
<div class="legend"><span><i style="background:#c8363b"></i>has sorry</span><span><i style="background:#3d6fb6"></i>comparator challenge</span><span><i style="background:#8a5cc2"></i>axiom or trust flag</span></div></div>
<div id="wrap"><canvas id="c"></canvas><div id="tip"></div></div>
<footer>Area is code lines (comments and blank lines excluded). Click a folder to zoom in, click the path above to zoom out. Made with <a href="https://github.com/DU8866-nice/leanstat">leanstat</a>. Counts only, not a proof check.</footer>
<script>
const DATA = __DATA__;
const HUES = [32, 145, 215, 350, 270, 52, 185, 12];
const STATUS = {2:"#c8363b",3:"#3d6fb6",1:"#8a5cc2"};
const cv = document.getElementById("c"), ctx = cv.getContext("2d"), tip = document.getElementById("tip");
function collapse(n){ while (typeof n[1] !== "number" && n[1].length === 1 && typeof n[1][0][1] !== "number") n = [n[0] + "/" + n[1][0][0], n[1][0][1]]; return n; }
let path = [collapse(DATA)], rects = [];
function total(n){ if (typeof n[1] === "number") return n[1]; if (n._t === undefined) n._t = n[1].reduce((a,k)=>a+total(k),0); return n._t; }
function count(n){ if (typeof n[1] === "number") return 1; if (n._f === undefined) n._f = n[1].reduce((a,k)=>a+count(k),0); return n._f; }
function worst(n){ if (typeof n[1] === "number") return n[2]; if (n._w === undefined){ let w=0; for (const k of n[1]){ const s=worst(k); if (s===2) {w=2;break;} if (s>w) w=s; } n._w=w; } return n._w; }
function squarify(items, x, y, w, h, out){
  items = items.filter(i=>i.v>0); if (!items.length || w <= 0 || h <= 0) return;
  const sum = items.reduce((a,i)=>a+i.v,0), scale = (w*h)/sum; let row=[], rest=items.slice();
  const ratio = (row, side) => { const s=row.reduce((a,i)=>a+i.v*scale,0); let mx=0,mn=Infinity; for(const i of row){const a=i.v*scale; mx=Math.max(mx,a); mn=Math.min(mn,a);} return Math.max(side*side*mx/(s*s), (s*s)/(side*side*mn)); };
  const flush = () => { const s=row.reduce((a,i)=>a+i.v*scale,0);
    if (w>=h){ const cw=s/h; let cy=y; for(const i of row){ const ch=i.v*scale/cw; out.push({...i,x,y:cy,w:cw,h:ch}); cy+=ch; } x+=cw; w-=cw; }
    else { const ch=s/w; let cx=x; for(const i of row){ const c2=i.v*scale/ch; out.push({...i,x:cx,y,w:c2,h:ch}); cx+=c2; } y+=ch; h-=ch; }
    row=[]; };
  while (rest.length){ const side=Math.min(w,h), item=rest[0];
    if (!row.length || ratio(row.concat([item]), side) <= ratio(row, side)){ row.push(item); rest.shift(); } else flush(); }
  if (row.length) flush();
}
let HUE_DEPTH = 0;
function layout(node, x, y, w, h, depth, hue, out){
  const kids = node[1].map(k=>({node:k, v:total(k)})).sort((a,b)=>b.v-a.v);
  const placed = []; squarify(kids, x, y, w, h, placed);
  placed.forEach((p, i) => {
    const isDir = typeof p.node[1] !== "number", hh = depth === HUE_DEPTH ? HUES[i % HUES.length] : (depth < HUE_DEPTH ? 32 : hue);
    const open = isDir && depth < 2 && p.w > 40 && p.h > 30;
    out.push({...p, depth, isDir, open, hue: hh, shade: i % 3});
    if (open){ const head = depth === 0 ? 17 : 14; layout(p.node, p.x+2, p.y+head, p.w-4, p.h-head-2, depth+1, hh, out); }
  });
}
function draw(){
  const dpr = window.devicePixelRatio||1, W=cv.clientWidth, H=cv.clientHeight;
  cv.width=W*dpr; cv.height=H*dpr; ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H);
  rects=[]; const cur=path[path.length-1];
  if (typeof cur[1] === "number") return;
  const kidsT = cur[1].map(total), sumT = kidsT.reduce((a,b)=>a+b,0);
  HUE_DEPTH = (Math.max(...kidsT) > 0.8 * sumT) ? 1 : 0;  // one folder holds nearly everything: color its children
  layout(cur, 0, 0, W, H, 0, 0, rects);
  for (const r of rects){
    const st = r.isDir ? worst(r.node) : r.node[2];
    const light = r.open ? 70 - r.depth*6 : 84 - r.shade*4 - r.depth*3;
    ctx.fillStyle = (!r.isDir && STATUS[st]) ? STATUS[st] : `hsl(${r.hue} 42% ${light}%)`;
    ctx.fillRect(r.x, r.y, r.w, r.h);
    ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.lineWidth = r.depth === 0 ? 2 : 1; ctx.strokeRect(r.x, r.y, r.w, r.h);
    if (r.isDir && STATUS[st] && r.w > 8 && r.h > 8){ const s = Math.min(10, r.w/3, r.h/3); ctx.fillStyle = STATUS[st];
      ctx.beginPath(); ctx.moveTo(r.x+r.w-s, r.y); ctx.lineTo(r.x+r.w, r.y); ctx.lineTo(r.x+r.w, r.y+s); ctx.closePath(); ctx.fill(); }
    const show = r.open || (r.w > 54 && r.h > 16);
    if (show){
      ctx.fillStyle = (!r.isDir && STATUS[st]) ? "#fff" : "#1d1d1b";
      ctx.font = (r.depth === 0 ? "600 12px" : "11px") + " -apple-system,Segoe UI,sans-serif";
      const label = r.node[0] + (r.depth === 0 && r.w > 150 ? "  " + total(r.node).toLocaleString() : "");
      ctx.save(); ctx.beginPath(); ctx.rect(r.x+2, r.y, r.w-4, r.h); ctx.clip(); ctx.fillText(label, r.x+5, r.y+12); ctx.restore();
    }
  }
  const crumbs = document.getElementById("crumbs"); crumbs.innerHTML="";
  path.forEach((n,i)=>{ if(i){ const s=document.createElement("span"); s.textContent=" / "; crumbs.appendChild(s);} const a=document.createElement("a"); a.textContent=n[0]; a.onclick=()=>{ path=path.slice(0,i+1); draw(); }; crumbs.appendChild(a); });
}
function hit(x,y){ let best=null; for (const r of rects) if (x>=r.x && x<r.x+r.w && y>=r.y && y<r.y+r.h) { if (!best || r.depth>best.depth) best=r; } return best; }
cv.addEventListener("mousemove", e=>{ const b=cv.getBoundingClientRect(), r=hit(e.clientX-b.left, e.clientY-b.top);
  if(!r){tip.style.display="none";return;}
  const n=r.node; tip.style.display="block"; tip.style.left=Math.min(e.clientX-b.left+12, b.width-300)+"px"; tip.style.top=(e.clientY-b.top+12)+"px";
  tip.textContent = r.isDir ? `${n[0]}/ · ${total(n).toLocaleString()} code lines · ${count(n).toLocaleString()} files` : `${n[0]} · ${n[1].toLocaleString()} code lines · ${n[3]} declarations` + ({2:" · has sorry",3:" · challenge file",1:" · axiom or trust flag"}[n[2]]||""); });
cv.addEventListener("mouseleave", ()=>tip.style.display="none");
cv.addEventListener("click", e=>{ const b=cv.getBoundingClientRect(), r=hit(e.clientX-b.left, e.clientY-b.top); if(!r) return;
  const top = rects.find(q=>q.isDir && q.depth===0 && r.x>=q.x && r.x<q.x+q.w && r.y>=q.y && r.y<q.y+q.h);
  if (top){ path.push(collapse(top.node)); draw(); } });
window.addEventListener("resize", draw); draw();
</script></body></html>
"""


def render_html(r, title=None):
    L = r["lines"]
    stats = [("files", r["files"]), ("code lines", L.get("code", 0)), ("theorems + lemmas", r["theorems"]),
             ("all declarations", sum(r["declarations"].values())),
             ("sorry in the checked proof" if r.get("solution_closure") else "sorry", r["sorry"]["in_proof"] if r.get("solution_closure") else r["sorry"]["total"]), ("axioms declared", len(r["axioms"])),
             ("longest import chain", r["imports"]["longest_chain"])]
    stats_html = "".join(f"<span><b>{fmt(v)}</b> {html.escape(k)}</span>" for k, v in stats)
    meta = " · ".join(f"{k} {v}" for k, v in r["meta"].items())
    sub = html.escape(meta) if meta else ""
    t = title or f"{r['name']}: leanstat"
    data = json.dumps(tree_for_html(r), separators=(",", ":"), ensure_ascii=False)
    return (HTML_TEMPLATE.replace("__TITLE__", html.escape(t)).replace("__SUBTITLE__", sub)
            .replace("__STATS__", stats_html).replace("__DATA__", data.replace("</", "<\\/")))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="leanstat", description="Count what is in a Lean 4 project.")
    ap.add_argument("path", nargs="?", default=".", help="project folder (default .)")
    ap.add_argument("--json", action="store_true", help="print everything as JSON")
    ap.add_argument("--html", metavar="FILE", help="write an interactive treemap report")
    ap.add_argument("--title", help="title for the HTML report")
    ap.add_argument("--challenge", action="append", default=[], metavar="GLOB",
                    help="treat matching files as challenge files (sorry expected); comparator configs are read automatically")
    ap.add_argument("--version", action="version", version=f"leanstat {__version__}")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.path):
        print(f"leanstat: {args.path} is not a folder", file=sys.stderr)
        return 2
    r = scan(args.path, args.challenge)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    if r["files"] == 0:
        print("leanstat: no .lean files found", file=sys.stderr)
        return 2
    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(render_html(r, args.title))
    if args.json:
        print(json.dumps(public(r), ensure_ascii=False, indent=1))
    else:
        print(render_text(r))
        if args.html:
            print(f"\n  wrote {args.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
