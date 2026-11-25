"""Tests for leanstat. Run: python3 -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import leanstat as L  # noqa: E402

PROJECT = os.path.join(ROOT, "tests", "fixtures", "project")


class Lexer(unittest.TestCase):
    def test_comments_strings_and_chars(self):
        src = ('/- outer /- inner -/ still comment -/\n'
               'def s := "a -- b /- c"  -- trailing\n'
               "def c := '\"'\n"
               'def d := 1\n'
               'def r := r#"raw "quoted" -/"#\n')
        code, comments = L.strip(src)
        lines = code.split("\n")
        self.assertEqual(lines[0].strip(), "")
        self.assertTrue(lines[1].startswith('def s := "'))
        self.assertNotIn("--", lines[1])
        self.assertIn("def d := 1", lines[3])  # the char literal '"' did not open a string
        self.assertNotIn("quoted", lines[4])
        self.assertIn("outer", comments.split("\n")[0])

    def test_line_counts(self):
        f = L.analyze_file("-- c\n\ntheorem t : True := trivial\n/- a\nb -/\n")
        self.assertEqual((f["code"], f["comment"], f["blank"]), (1, 3, 1))

    def test_sorry_heuristics(self):
        src = ("theorem a : True := by sorry\n"
               "theorem b : True := by admit\n"
               "#check (sorry : Nat)\n"
               "  let s ← `(tactic| sorry)\n"
               "  Term.addTermInfo' i q(sorry : $P) P\n"
               "  | sorry : Proof\n"
               "-- sorry\n"
               'def m := "sorry"\n')
        self.assertEqual(L.analyze_file(src)["sorry_lines"], [1, 2])

    def test_declarations(self):
        src = ("@[simp] theorem a : True := trivial\n"
               "private noncomputable def b := 1\n"
               "protected lemma c : True := trivial\n"
               "instance (priority := 100) : Inhabited Nat := ⟨0⟩\n"
               "class inductive T | x\n"
               "structure S where\n  x : Nat\n"
               "axiom ax : False\n"
               "noncomputable section\n")
        f = L.analyze_file(src)
        self.assertEqual(f["decls"], {"theorem": 1, "def": 1, "lemma": 1, "instance": 1, "class": 1,
                                      "structure": 1, "axiom": 1})
        self.assertEqual(f["axioms"], ["ax"])

    def test_imports_with_the_module_system(self):
        src = "module\npublic import A.B\npublic meta import C\nimport all D\nimport E F\n"
        self.assertEqual(L.analyze_file(src)["imports"], ["A.B", "C", "D", "E", "F"])


class Project(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = L.scan(PROJECT)

    def test_counts(self):
        r = self.r
        self.assertEqual(r["files"], 6)  # .lake is skipped
        self.assertEqual(r["meta"], {"toolchain": "leanprover/lean4:v4.37.0", "mathlib": "0123456789"})
        self.assertEqual(r["declarations"]["theorem"], 4)

    def test_comparator_and_closure(self):
        r = self.r
        self.assertEqual(r["comparators"][0]["challenge_file"], "src/Challenge.lean")
        self.assertEqual(r["solution_closure"]["files"], 3)  # Main, Statements, Lib/A
        self.assertEqual(r["solution_closure"]["unused_files"], 2)  # Lib/Unused, tools/Tool
        self.assertEqual((r["sorry"]["in_proof"], r["sorry"]["in_challenge"], r["sorry"]["elsewhere"]), (0, 1, 1))
        self.assertEqual(r["sorry"]["where_elsewhere"], ["src/Lib/Unused.lean:2"])
        self.assertEqual(r["flags"]["unsafe"]["count"], 1)
        self.assertEqual(r["flags"]["unsafe"]["in_proof"], 0)

    def test_text(self):
        text = L.render_text(self.r)
        self.assertIn("none inside the solution's imports", text)
        self.assertIn("1 in comparator challenge files", text)
        self.assertIn("unsafe ×1 in other files", text)
        self.assertIn("not a proof check", text)

    def test_no_comparator(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "Foo.lean"), "w") as fh:
            fh.write("theorem t : True := by sorry\n")
        with open(os.path.join(tmp, "FooChallenge.lean"), "w") as fh:
            fh.write("theorem t : True := by sorry\n")
        r = L.scan(tmp)
        self.assertIsNone(r["solution_closure"])
        self.assertEqual((r["sorry"]["total"], r["sorry"]["in_challenge"]), (2, 1))


class Imports(unittest.TestCase):
    def test_module_index(self):
        idx = L.module_index(["src/Main.lean", "tools/x/Main.lean", "src/A/B.lean"], ["src/", ""])
        self.assertEqual(idx["Main"], "src/Main.lean")
        self.assertEqual(idx["A.B"], "src/A/B.lean")
        idx = L.module_index(["Algebra/Group.lean"], [""], "Mathlib")
        self.assertEqual(idx["Mathlib.Algebra.Group"], "Algebra/Group.lean")

    def test_longest_chain(self):
        g = {"a": {"b"}, "b": {"c"}, "c": set(), "d": {"c"}}
        self.assertEqual(L.longest_chain(g), 3)
        self.assertEqual(L.longest_chain({"a": {"b"}, "b": {"a"}}), 2)  # a cycle doesn't hang


class Cli(unittest.TestCase):
    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = L.main(list(argv))
        return code, out.getvalue()

    def test_json_and_html(self):
        code, out = self.run_main(PROJECT, "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertNotIn("_files", data)
        self.assertEqual(data["sorry"]["in_proof"], 0)
        path = os.path.join(tempfile.mkdtemp(), "map.html")
        code, _ = self.run_main(PROJECT, "--html", path, "--title", "Demo </script> map")
        self.assertEqual(code, 0)
        page = open(path, encoding="utf-8").read()
        self.assertIn("Demo &lt;/script&gt; map", page)
        self.assertEqual(page.count("</script>"), 1)
        self.assertIn('"Lib"', page)

    def test_errors(self):
        self.assertEqual(self.run_main(os.path.join(PROJECT, "missing"))[0], 2)
        self.assertEqual(self.run_main(tempfile.mkdtemp())[0], 2)


if __name__ == "__main__":
    unittest.main()
