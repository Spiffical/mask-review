"""A name list for the page: the taxa reviewers are likely to use, each resolved to its accepted WoRMS record.

    mask-review taxa Ophiuroidea Porifera 123084 --names more_names.txt --map labels.json --out taxa.json

The page searches this list first and WoRMS itself for everything else, so a list is optional. It helps in three ways:
names in it are found offline and ranked first, each carries its line (every AphiaID above it) so the tiles can show
what lies under a name, and `--map` gives each label of the agents a starting name ("brittle star" -> Ophiuroidea).

Sources: names or AphiaIDs on the command line, files of names (.txt one per line, .csv with a `name` or
`scientificName` column and optionally `aphia_id` / `AphiaID`, .json list of names or {name: AphiaID}), a map of the
agents' labels to taxa (.json {label: name or AphiaID}), and BIIGLE label-tree exports (morphotype endings such as
"sp4" are dropped). Every WoRMS answer is kept in <out>.cache.json, so a rerun asks only for what is new. Standard
library only. WoRMS (marinespecies.org) asks that its use be cited; see the README.
"""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REST = "https://www.marinespecies.org/rest/"
MORPH = re.compile(r"\s+(sp\.?\s*\d*|spp\.?|cf\.?.*|indet\.?|undet.*)$", re.I)


class Worms:
    """WoRMS REST with a cache on disk."""

    def __init__(self, cache: Path | None):
        self.path = cache
        self.cache = json.loads(cache.read_text()) if cache and cache.exists() else {}
        self.asked = 0

    def get(self, route):
        if route not in self.cache:
            time.sleep(0.15)
            self.asked += 1
            for attempt in range(4):
                try:
                    req = urllib.request.Request(REST + route, headers={"User-Agent": "mask-review taxa list"})
                    with urllib.request.urlopen(req, timeout=40) as r:
                        body = r.read()
                        self.cache[route] = json.loads(body) if r.status == 200 and body else None
                        break
                except urllib.error.HTTPError as e:
                    if e.code in (204, 400, 404):
                        self.cache[route] = None
                        break
                    time.sleep(2 ** attempt)
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(2 ** attempt)
            else:
                raise SystemExit(f"WoRMS did not answer for {route}")
            if self.asked % 40 == 0:
                self.save()
        return self.cache[route]

    def save(self):
        if self.path:
            self.path.write_text(json.dumps(self.cache))

    def by_id(self, aphia):
        return self.get(f"AphiaRecordByAphiaID/{int(aphia)}")

    def by_name(self, name):
        """The animal of this name: exact names only, animals first, accepted before superseded."""
        rows = self.get(f"AphiaRecordsByName/{urllib.parse.quote(name)}?like=false&marine_only=false") or []
        rows = [r for r in rows if r and r.get("kingdom") in ("Animalia", None)]
        return min(rows, key=lambda r: (r["status"] != "accepted", r["AphiaID"] != r["valid_AphiaID"]), default=None)

    def accepted(self, rec):
        """The accepted record, following one synonym at a time."""
        for _ in range(4):
            if not rec or not rec.get("valid_AphiaID") or rec["valid_AphiaID"] == rec["AphiaID"]:
                return rec
            rec = self.by_id(rec["valid_AphiaID"])
        return rec

    def english(self, aphia):
        rows = self.get(f"AphiaVernacularsByAphiaID/{int(aphia)}") or []
        names = []
        for r in rows:
            v = (r.get("vernacular") or "").strip()
            if r.get("language_code") == "eng" and v and v.lower() not in [n.lower() for n in names]:
                names.append(v)
        return names[:3]

    def line(self, aphia):
        """Everything above a taxon, from the kingdom down: [(AphiaID, name, rank)]."""
        node, out = self.get(f"AphiaClassificationByAphiaID/{int(aphia)}"), []
        while node:
            out.append((node["AphiaID"], node["scientificname"], node.get("rank") or ""))
            node = node.get("child")
        return [x for x in out if x[0] not in (1, int(aphia))]  # 1 is Biota


def entry(worms, rec):
    return dict(id=rec["AphiaID"], name=rec["scientificname"], rank=rec.get("rank") or "", authority=rec.get("authority") or "",
                up=[rec.get(k) for k in ("phylum", "class", "order", "family") if rec.get(k) and rec.get(k) != rec["scientificname"]],
                line=[x[0] for x in worms.line(rec["AphiaID"])], common=worms.english(rec["AphiaID"]), aka=[], src=[])


def read_names(path: Path):
    """(name or AphiaID) pairs from a .txt, .csv or .json file."""
    text = path.read_text()
    if path.suffix.lower() == ".json":
        d = json.loads(text)
        items = d.items() if isinstance(d, dict) else ((x, None) for x in d)
        return [(str(n), a) for n, a in items]
    if path.suffix.lower() == ".csv":
        out = []
        for row in csv.DictReader(text.splitlines()):
            low = {k.lower().strip(): (v or "").strip() for k, v in row.items() if k}
            n = low.get("name") or low.get("scientificname") or low.get("taxon") or ""
            a = low.get("aphia_id") or low.get("aphiaid") or None
            if n or a:
                out.append((n, a))
        return out
    return [(ln.strip(), None) for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def biigle_names(path: Path):
    out = []
    trees = json.loads(path.read_text())
    for tree in trees if isinstance(trees, list) else [trees]:
        for lab in tree.get("labels", []):
            n = MORPH.sub("", lab.get("name", "")).strip()
            if re.fullmatch(r"[A-Z][a-z]+( \([A-Z][a-z]+\))?( [a-z]+)?", n):
                out.append(n)
    return out


def build(names=(), files=(), label_map: Path | None = None, biigle=(), out: Path | None = None, cache: Path | None = None,
          log=print) -> dict:
    """The name list as a dict (written to `out` when given). See the module's docstring."""
    worms = Worms(cache or (out.with_suffix(".cache.json") if out else None))
    wanted = {}  # AphiaID or name -> ({names seen}, {sources})

    def want(name, src, aphia=None):
        k = int(aphia) if aphia is not None and str(aphia).strip().isdigit() else name.strip()
        if not k:
            return
        w = wanted.setdefault(k, (set(), set()))
        if name and not str(name).isdigit():
            w[0].add(name.strip())
        w[1].add(src)

    for n in names:
        want(n if not str(n).isdigit() else "", "list", n if str(n).isdigit() else None)
    for p in files:
        for n, a in read_names(Path(p)):
            want(n, "list", a)
    classes_wanted = {}
    if label_map:
        for label, t in json.loads(Path(label_map).read_text()).items():
            classes_wanted[label] = t
            want(t if not str(t).isdigit() else "", "class", t if str(t).isdigit() else None)
    for p in biigle:
        for n in biigle_names(Path(p)):
            want(n, "BIIGLE")
    taxa, missing, of = {}, [], {}
    for k, (seen, srcs) in wanted.items():
        found = worms.by_id(k) if isinstance(k, int) else worms.by_name(k)
        rec = worms.accepted(found)
        if not rec:
            missing.append(k)
            continue
        of[k] = rec["AphiaID"]
        e = taxa.setdefault(rec["AphiaID"], entry(worms, rec))
        e["aka"] = sorted(set(e["aka"]) | {n for n in seen | {found["scientificname"]} if n != e["name"]})
        old = [r for r in (found, worms.by_id(rec["originalNameUsageID"]) if rec.get("originalNameUsageID") else None) if r]
        for r in old:  # WoRMS often keeps the common name on the superseded record
            if r["AphiaID"] != rec["AphiaID"]:
                e["common"] += [c for c in worms.english(r["AphiaID"]) if c.lower() not in [x.lower() for x in e["common"]]][:3]
                e["aka"] = sorted(set(e["aka"]) | {r["scientificname"]} - {e["name"]})
        e["src"] = sorted(set(e["src"]) | srcs)
    above = {x[0]: [x[1], x[2]] for i in taxa for x in worms.line(i) if x[0] not in taxa}  # taxa met only in a line
    worms.save()
    classes = {}
    for label, t in classes_wanted.items():
        k = int(t) if str(t).isdigit() else str(t).strip()
        if k in of:
            classes[label] = of[k]
        else:
            log(f"label {label!r}: {t!r} is not in WoRMS; it starts without a name")
    d = dict(taxa=sorted(taxa.values(), key=lambda t: t["name"]), above=above, classes=classes)
    if out:
        Path(out).write_text(json.dumps(d, indent=1))
    log(f"{len(taxa)} taxa ({worms.asked} WoRMS calls), {len(classes)} labels with a starting name"
        + (f"; not found in WoRMS by exact name: {sorted(map(str, missing))}" if missing else ""))
    return d


def empty() -> dict:
    return dict(taxa=[], above={}, classes={})
