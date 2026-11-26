"""The page: its script parses, and its kept rule gives the server's answers. Needs Node; skipped without it."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mask_review import store

PAGE = Path(store.__file__).resolve().parent / "web" / "index.html"
NODE = shutil.which("node")


def script():
    s = PAGE.read_text()
    return s[s.index("<script>") + len("<script>"):s.rindex("</script>")]


def cases():
    """Every mix of the states that decide what stays."""
    out = []
    for removed in (False, True):
        for added in (False, True):
            for ok in (False, True):
                for done in (False, True):
                    for done_cut in (None, 30, 70):
                        for s in (None, 20, 50, 80):
                            for cut in (None, 0, 50, 90):
                                lab = dict(removed=removed, ok=ok, was=None if added else dict(box=[0, 0, 1, 1], taxon=None))
                                if s is not None:
                                    lab["s"] = s
                                out.append((dict(done=done, done_cut=done_cut), lab, cut))
    return out


@unittest.skipUnless(NODE, "node is not installed")
class Page(unittest.TestCase):
    def test_script_parses(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(script())
        r = subprocess.run([NODE, "--check", f.name], capture_output=True, text=True)
        Path(f.name).unlink()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_kept_rule_matches_the_server(self):
        src = script()
        names = ("isNum", "scored", "covered", "checked")
        lines = [next(ln for ln in src.splitlines() if re.match(rf"const {n} = ", ln)) for n in names]
        kept = next(ln for ln in src.splitlines() if ln.startswith("const keptL = "))
        js = "\n".join(lines) + "\nlet cut = null;\n" + kept + """
const C = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(C.map(([f, l, c]) => { cut = c; return [keptL(f, l), checked(f, l)]; })));"""
        cs = cases()
        r = subprocess.run([NODE, "-e", js], input=json.dumps(cs), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        want = [[store.kept(f, lab, cut), store.checked(f, lab)] for f, lab, cut in cs]
        bad = [cs[i] for i in range(len(cs)) if got[i] != want[i]]
        self.assertEqual(bad, [], f"{len(bad)} of {len(cs)} cases differ")

    def test_no_onc_specific_text(self):
        """The page is generic: the reviewer-facing text names no institution, network or private project."""
        hits = [f"line {k}: {ln.strip()[:120]}" for k, ln in enumerate(PAGE.read_text().splitlines(), 1)
                if re.search(r"\bONC\b|\bVPN\b|SeaTube", ln)]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
