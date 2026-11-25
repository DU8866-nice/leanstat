# Contributing

Bug reports are most useful with a small `.lean` snippet that leanstat miscounts: a comment or
string the lexer gets wrong, a declaration it misses, a `sorry` it should or shouldn't count.
Add it to `tests/test_leanstat.py` if you can, and run `python3 -m unittest discover -s tests`.

leanstat stays one file with no dependencies, so it can be dropped into any project and read in
one sitting. It counts; it never builds or checks proofs, and its output should never read as a
verdict on a project.

To add a project to the comparison, add a row to `PROJECTS` in `data/measure.py` and say what the
project is in its own words.
