"""The review of a bundle: the agents' labels (never changed) with the reviewers' work laid over them, the cut, the
people, and what the exports and the decisions file hold. server.py serves it; `mask-review export` uses it offline.

Standard library only, so a bundle can be served on a machine with nothing but Python.

What stays (kept): a label a reviewer removed never does. A label a reviewer added or checked always does, whatever its
score. Every other label goes by its score: it stays when it has no score, when the bundle has no cut, or when its
score is at or above the cut. A label is checked when a reviewer said it is right (Enter, a name, "All are right"), or
when its frame was marked right and the label was shown then (its score at or above the cut of that moment).
"""
from __future__ import annotations

import base64
import collections
import csv
import datetime
import functools
import io
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

ID = r"[A-Za-z0-9_.-]{1,80}"
LABEL_ID = re.compile(r"m\d{1,7}")
NEW_ID = re.compile(r"x[0-9a-z]{4,24}")
EMAIL = re.compile(r"[^@\s]{1,80}@[^@\s]{1,180}\.[^@\s.]{2,24}")
SESSION = re.compile(r"[0-9a-z]{6,24}")
KEPT = ("box", "taxon", "note", "unsure", "removed", "ok", "rev", "by", "at")
AWAY = 25.0  # seconds without a word before someone is no longer shown as here
DECISIONS = "mask-review-decisions"


class Unknown(Exception):
    """The request names no known user."""


class Refused(Exception):
    """The request is not allowed in this mode."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ---------------------------------------------------------------------------------------------- the kept rule
# The page has the same functions (covered, checked, keptL); tests/test_rule.py checks that both agree.
def scored(lab):
    return number(lab.get("s"))


def covered(frame, lab):
    """Shown when the frame was marked right: added by hand, without a score, or at or above that moment's cut."""
    dc = frame.get("done_cut")
    return lab.get("was") is None or not scored(lab) or not number(dc) or lab["s"] >= dc


def checked(frame, lab):
    return bool(lab.get("ok")) or (bool(frame.get("done")) and covered(frame, lab))


def kept(frame, lab, cut):
    if lab.get("removed"):
        return False
    if lab.get("was") is None or checked(frame, lab):
        return True
    return cut is None or not scored(lab) or lab["s"] >= cut


def status(frame, lab, cut):
    if lab.get("removed"):
        return "removed"
    return "kept" if kept(frame, lab, cut) else "under the cut"


def what(lab):
    """What the reviewers did to a label."""
    if lab.get("was") is None:
        return "added"
    if lab.get("removed"):
        return "removed"
    moved = any(abs(a - b) >= 0.5 for a, b in zip(lab["box"], lab["was"]["box"]))  # the page drops the outline at 0.5 too
    a, b = lab["taxon"], lab["was"]["taxon"]
    named = (a and (a.get("id"), a["name"])) != (b and (b.get("id"), b["name"]))
    return " + ".join(w for w, on in (("renamed" if b else "named", named), ("box moved", moved)) if on) or "kept"


def write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.replace(tmp, path)


def jpeg_size(b):
    """(width, height) of a JPEG, or None."""
    if b[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(b) and b[i] == 0xFF:
        marker, n = b[i + 1], int.from_bytes(b[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(b[i + 7:i + 9], "big"), int.from_bytes(b[i + 5:i + 7], "big")
        i += 2 + n
    return None


def clean_taxon(t):
    if t is None:
        return None
    if not (isinstance(t, dict) and isinstance(t.get("name"), str) and 0 < len(t["name"].strip()) <= 200):
        raise ValueError("taxon")
    if not (t.get("id") is None or (isinstance(t["id"], int) and not isinstance(t["id"], bool) and t["id"] > 0)):
        raise ValueError("taxon id")
    return dict(id=t.get("id"), name=t["name"].strip(), rank=str(t.get("rank") or "")[:60])


def clean_cut(v):
    """A cut: a whole number from 0 to 100 (halves go up, as the page's Math.round does)."""
    if v is None:
        return None
    if not number(v) or not 0 <= v <= 100:
        raise ValueError("cut")
    return int(math.floor(float(v) + 0.5))


def frame_checked(frame, cut):
    """A frame counts as checked when it was marked right and no label it keeps now went unseen (a cut lowered since
    can bring in labels nobody looked at)."""
    return bool(frame.get("done")) and all(checked(frame, lab) for lab in frame["labels"] if kept(frame, lab, cut))


class Store:
    """A bundle and its review. mode: "fixed" (the cut is set when the server starts) or "tune" (reviewers move it)."""

    def __init__(self, root, mode: str = "fixed", cut: float | None = None, edits: Path | None = None):
        self.root = Path(root)
        if not (self.root / "index.json").exists():
            raise SystemExit(f"{self.root} is not a bundle (no index.json). Make one with `mask-review build` or `mask-review demo`.")
        self.edits = Path(edits) if edits else self.root / "edits"
        self.new = self.edits / "new"
        self.users_path = self.edits / "users.json"
        self.index = json.loads((self.root / "index.json").read_text())
        self.ids = {f["id"] for f in self.index["frames"]}
        self.by_id = {f["id"]: f for f in self.index["frames"]}
        self.videos = {name for name in (f.get("clip") or f["id"] for f in self.index["frames"])
                       if (self.root / "video" / f"{name}.mp4").exists()}
        self.lock = threading.RLock()
        self.boot = secrets.token_hex(6)
        self.events = collections.deque(maxlen=4000)
        self.people = {}
        if mode not in ("fixed", "tune"):
            raise ValueError("mode")
        self.mode = mode
        self.scored = self.index.get("score") is not None or self._any_score()
        bundle_cut = self.index.get("cut")
        self.cut0 = clean_cut(cut) if cut is not None else clean_cut(bundle_cut) if bundle_cut is not None else (0 if self.scored else None)
        saved = self.saved_cut()
        self.cut = self.cut0 if cut is not None or saved is None else saved["cut"]
        if not self.scored:
            self.cut = self.cut0 = None
        self.taxa_path = self.root / "taxa.json"

    def _any_score(self):
        for f in self.index["frames"][:50]:
            if any("s" in lab for lab in self.base(f["id"])["labels"]):
                return True
        return False

    # ---- frames
    def seq(self):
        return self.events[-1]["seq"] if self.events else 0

    def emit(self, **e):
        self.events.append(dict(e, seq=self.seq() + 1))

    def new_frames(self):
        return [json.loads(p.read_text()) for p in sorted(self.new.glob("*.json"))]

    def exists(self, fr):
        return fr in self.ids or (self.new / f"{fr}.json").exists()

    @functools.lru_cache(maxsize=None)
    def bundled(self, fr):
        return (self.root / "frames" / f"{fr}.json").read_text()

    def base(self, fr):
        return json.loads(self.bundled(fr) if fr in self.ids else (self.new / f"{fr}.json").read_text())

    @functools.lru_cache(maxsize=256)
    def masks(self, fr):
        p = self.root / "masks" / f"{fr}.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def log(self, **line):
        self.edits.mkdir(parents=True, exist_ok=True)
        with open(self.edits / "history.jsonl", "a") as h:
            h.write(json.dumps(dict(line, at=now())) + "\n")

    def listed(self):
        """The frames in order: the bundle's and the reviewers' own, by clip and then by time in it."""
        own = [dict(id=g["id"], clip=g["clip"], at=g["frame_at"]) for g in self.new_frames() if not g["new"]["removed"]]
        if not own:
            return self.index["frames"]
        place = {}
        for k, f in enumerate(self.index["frames"]):
            place.setdefault(f.get("clip"), k)
        return sorted(self.index["frames"] + own, key=lambda f: (place.get(f.get("clip"), len(place)), f.get("at") or 0))

    def saved(self, fr):
        """The reviewers' version of a frame, or None: labels is {id: label}, only those someone changed."""
        p = self.edits / f"{fr}.json"
        if not p.exists():
            return None
        e = json.loads(p.read_text())
        if isinstance(e["labels"], list):  # written before labels had their own revisions
            e["labels"] = {x["id"]: dict(x, ok=False, rev=1, by=e["by"], at=e["at"]) for x in e["labels"]}
        return e

    def merged(self, fr):
        """The frame with the reviewers' version of its labels laid over the agents' (removed ones stay, marked)."""
        f, e = self.base(fr), self.saved(fr)
        f.update(done=False, by="", at="", done_cut=None)
        for lab in f["labels"]:
            lab.update(was=dict(box=lab["box"], taxon=lab["taxon"]), rev=0)
        if e:
            known = {lab["id"]: lab for lab in f["labels"]}
            for i, x in e["labels"].items():
                if i in known:
                    known[i].update({k: x[k] for k in KEPT if k in x})
                else:
                    f["labels"].append(self.added(i, x))
            f.update(done=e["done"], by=e["by"], at=e["at"], done_cut=e.get("done_cut"))
        return f

    @staticmethod
    def added(i, x):
        return dict({k: x.get(k) for k in KEPT}, id=i, poly=[], made="a reviewer", agent="", was=None)

    # ---- people
    def users(self):
        return json.loads(self.users_path.read_text()) if self.users_path.exists() else {}

    def person(self, uid):
        u = self.users().get(uid) if isinstance(uid, str) else None
        if u is None:
            raise Unknown
        return u

    @staticmethod
    def mine(uid, u):
        """What a page may know of its own user."""
        return dict(uid=uid, name=u["name"], email=u.get("email", ""), favs=u["favs"])

    def set_profile(self, body):
        """Make a profile or change it. An email is optional; the same email is the same person, so someone who comes
        back on another computer finds their stars."""
        name, email = (body.get(k).strip() if isinstance(body.get(k), str) else "" for k in ("name", "email"))
        if not 0 < len(name) <= 100:
            raise ValueError("a name is needed")
        if email and not EMAIL.fullmatch(email):
            raise ValueError("that is not an email address")
        with self.lock:
            u = self.users()
            uid = next((k for k, v in u.items() if email and v.get("email", "").lower() == email.lower()), None)
            if not uid and isinstance(body.get("uid"), str) and body["uid"] in u:
                uid = body["uid"]
            uid = uid or "u" + secrets.token_hex(10)
            u[uid] = dict(u.get(uid) or dict(favs=[], made=now()), name=name, email=email)
            write(self.users_path, json.dumps(u, indent=1))
            return self.mine(uid, u[uid])

    def set_fav(self, body):
        t = clean_taxon(body.get("taxon"))
        if t is None:
            raise ValueError("taxon")
        with self.lock:
            u = self.users()
            if not isinstance(body.get("uid"), str) or body["uid"] not in u:
                raise Unknown
            me = u[body["uid"]]
            rest = [x for x in me["favs"] if (x["id"], x["name"]) != (t["id"], t["name"])]
            if body.get("on"):
                rest = me["favs"] if len(rest) < len(me["favs"]) else (rest + [t])[-60:]
            me["favs"] = rest
            write(self.users_path, json.dumps(u, indent=1))
            return dict(favs=rest)

    # ---- the cut
    def saved_cut(self):
        p = self.edits / "cut.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        return d if number(d.get("cut")) else None

    def set_cut(self, body, address=""):
        if self.mode != "tune":
            raise Refused("the cut was fixed when the server started")
        if self.cut is None:
            raise ValueError("the labels carry no scores")
        c = clean_cut(body.get("cut"))
        if c is None:
            raise ValueError("cut")
        with self.lock:
            by = self.person(body.get("uid"))["name"]
            if c != self.cut:
                self.cut = c
                write(self.edits / "cut.json", json.dumps(dict(cut=c, by=by, at=now())))
                self.log(cut=c, by=by, user=body.get("uid"), address=address)
                self.emit(session=body.get("session"), by=by, cut=c)
            return dict(cut=self.cut)

    # ---- saving labels
    def clean(self, f, x):
        """One label as sent, known fields only. Raises ValueError when it is not one."""
        i, b = x.get("id"), x.get("box")
        if not (isinstance(i, str) and (NEW_ID.fullmatch(i) or any(lab["id"] == i for lab in f["labels"]))):
            raise ValueError("id")
        if not isinstance(x.get("base"), int):
            raise ValueError("base")
        if not (isinstance(b, list) and len(b) == 4 and all(number(v) for v in b)):
            raise ValueError("box")
        b = [round(min(max(float(v), 0.0), float(f["w"] if k % 2 == 0 else f["h"])), 1) for k, v in enumerate(b)]
        if b[2] - b[0] < 1 or b[3] - b[1] < 1:
            raise ValueError("box size")
        note = x.get("note") or ""
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("note")
        return i, x["base"], dict(box=b, taxon=clean_taxon(x.get("taxon")), note=note, unsure=bool(x.get("unsure")),
                                  removed=bool(x.get("removed")), ok=bool(x.get("ok")))

    def save(self, body, address="", force=False, why=None):
        """Write the changed labels of one or more frames. A label someone else changed since the page last heard of it is
        left as they made it and sent back (unless force: a decisions file loaded on purpose); so is a label that is not
        one (a box under a pixel, a name too long), with the reason. The rest is saved. Raises ValueError, and writes
        nothing, when the request itself is not a set of frames."""
        if not isinstance(body, dict) or not isinstance(body.get("changes"), list) or not 0 < len(body["changes"]) <= 5000:
            raise ValueError("changes")
        uid, session = body.get("uid"), body.get("session")
        with self.lock:
            by, work, seen = self.person(uid)["name"], [], set()
            for c in body["changes"]:
                fr = c.get("frame") if isinstance(c, dict) else None
                if not (isinstance(fr, str) and re.fullmatch(ID, fr) and self.exists(fr)) or fr in seen:
                    raise ValueError("frame")
                if not (c.get("done") is None or isinstance(c["done"], bool)) or not isinstance(c.get("labels"), list):
                    raise ValueError("labels")
                cut = clean_cut(c.get("cut")) if c.get("cut") is not None else self.cut
                seen.add(fr)
                f = self.base(fr)
                labels, bad = [], []
                for x in c["labels"]:
                    try:
                        labels.append(self.clean(f, x))
                    except (ValueError, TypeError, AttributeError, KeyError) as err:
                        bad.append(dict(id=x.get("id") if isinstance(x, dict) and isinstance(x.get("id"), str) else None, error=str(err)))
                work.append((f, c.get("done"), cut, labels, bad))
            kept, refused, wrong = [], {}, {}
            for f, done, cut, labels, bad in work:
                fr, t = f["id"], now()
                if bad:
                    wrong[fr] = bad
                e = self.saved(fr) or dict(labels={}, done=False, by="", at="")
                wrote = {}
                for i, was, x in labels:
                    rev = e["labels"].get(i, {}).get("rev", 0)
                    if was != rev and not force:
                        refused.setdefault(fr, []).append(i)
                        continue
                    e["labels"][i] = wrote[i] = dict(x, rev=rev + 1, by=by, user=uid, at=t)
                    kept.append(dict(frame=fr, id=i, rev=rev + 1))
                if done is not None:
                    e.update(done=done, by=by, user=uid, at=t, done_cut=cut if done else None)
                if not wrote and done is None:
                    continue
                self.log(frame=fr, address=address, user=uid, by=by, done=done, labels=wrote, **({"why": why} if why else {}))
                write(self.edits / f"{fr}.json", json.dumps(e))
                self.emit(session=session, by=by, frame=fr, done=e["done"], done_by=e["by"], done_cut=e.get("done_cut"),
                          labels=[self.added(i, x) for i, x in wrote.items()])
            return dict(kept=kept, theirs=[dict(frame=fr, refused=ids, labels=self.merged(fr)["labels"]) for fr, ids in refused.items()],
                        bad=[dict(frame=fr, refused=[b["id"] for b in bs], errors=[b["error"] for b in bs], labels=self.merged(fr)["labels"])
                             for fr, bs in wrong.items()])

    # ---- frames taken from the video
    def add_frame(self, body, address=""):
        """A new frame from a moment of the video. Returns its id. Raises ValueError when the request is not one."""
        parent, t, by = body.get("from"), body.get("t"), self.person(body.get("uid"))["name"]
        if not (isinstance(parent, str) and re.fullmatch(ID, parent) and self.exists(parent)):
            raise ValueError("from")
        if not (number(t) and 0 <= t < 36000):
            raise ValueError("time")
        f = self.merged(parent)
        t = round(float(t), 4)
        if not f.get("video") or not f.get("fps"):
            raise ValueError("this frame has no video")
        img, thumb = (base64.b64decode(body.get(k) or "", validate=True) for k in ("jpg", "thumb"))
        if jpeg_size(img) != (f["w"], f["h"]) or not jpeg_size(thumb):
            raise ValueError("picture")
        for g in [self.base(i) for i in self.ids if self.by_id[i].get("clip") == f["clip"]] + self.new_frames():
            if g["clip"] == f["clip"] and abs(g["frame_at"] - t) < 0.5 / f["fps"]:
                if g.get("new", {}).get("removed"):
                    self.drop_frame(g["id"], False, body, address)
                return g["id"]
        fr, d = f"{f['clip']}_t{round(t * 1000):06d}", t - f["frame_at"]
        names = {(x["taxon"]["id"], x["taxon"]["name"]): x["taxon"] for x in f["labels"] if x["taxon"] and not x.get("removed")}
        g = {k: f[k] for k in ("w", "h", "file", "clip", "video", "fps") if k in f}
        g.update(id=fr, labels=[], frame_at=t, names=list(names.values()) or f.get("names", []),
                 new=dict(by=by, at=now(), parent=parent, removed=False))
        for k in ("dive", "depth"):
            if k in f:
                g[k] = f[k]
        if f.get("time"):
            try:
                when = datetime.datetime.strptime(f["time"], "%Y-%m-%d %H:%M:%S") + datetime.timedelta(seconds=d)
                g["time"] = when.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        if f.get("logs"):
            g["logs"] = [dict(x, dt=round(x["dt"] - d)) for x in f["logs"] if number(x.get("dt"))]
        with self.lock:
            self.new.mkdir(parents=True, exist_ok=True)
            write(self.new / f"{fr}.jpg", img)
            write(self.new / f"{fr}_t.jpg", thumb)
            write(self.new / f"{fr}.json", json.dumps(g))
            self.log(frame=fr, address=address, user=body["uid"], new=g["new"], t=t)
            self.emit(session=body.get("session"), by=by, order=[x["id"] for x in self.listed()], frames=[self.merged(fr)])
        return fr

    def drop_frame(self, fr, removed, body, address=""):
        """Take a reviewer's own frame out of the set, or put it back. Nothing is deleted."""
        with self.lock:
            by = self.person(body.get("uid"))["name"]
            g = json.loads((self.new / f"{fr}.json").read_text())
            g["new"]["removed"] = removed
            write(self.new / f"{fr}.json", json.dumps(g))
            self.log(frame=fr, address=address, user=body["uid"], by=by, removed=removed)
            self.emit(session=body.get("session"), by=by, order=[x["id"] for x in self.listed()], frames=[] if removed else [self.merged(fr)])

    def placed(self, fr):
        return dict(id=fr, order=[f["id"] for f in self.listed()], frame=self.merged(fr))

    # ---- the pages
    def sync(self, body):
        """What the others did since the page last asked, and who is here."""
        uid, session = body.get("uid"), body.get("session")
        if not (isinstance(session, str) and SESSION.fullmatch(session)):
            raise ValueError("session")
        with self.lock:
            me, t = self.person(uid), time.monotonic()
            sel = body.get("sel") if isinstance(body.get("sel"), list) else []
            self.people[session] = dict(name=me["name"], frame=str(body.get("frame") or "")[:80], sel=[str(i)[:40] for i in sel[:3]], t=t)
            for k in [k for k, p in self.people.items() if t - p["t"] > AWAY]:
                del self.people[k]
            out = dict(boot=self.boot, seq=self.seq(), favs=me["favs"], page=self.page_made(), cut=self.cut,
                       people=[dict(session=k, name=p["name"], frame=p["frame"], sel=p["sel"]) for k, p in self.people.items() if k != session])
            at = body.get("seq")
            if body.get("boot") != self.boot or not isinstance(at, int) or at > out["seq"] or (self.events and at < self.events[0]["seq"] - 1):
                return dict(out, reload=True)
            return dict(out, events=[e for e in self.events if e["seq"] > at and e.get("session") != session])

    def bye(self, body):
        with self.lock:
            return dict(left=self.people.pop(body.get("session"), None) is not None)

    page_path = Path(__file__).resolve().parent / "web" / "index.html"

    def page_made(self):
        return int(self.page_path.stat().st_mtime)

    def everything(self):
        with self.lock:
            return dict(title=self.index.get("title", "Mask review"), boot=self.boot, seq=self.seq(), page=self.page_made(),
                        built=int((self.root / "index.json").stat().st_mtime), mode=self.mode, cut=self.cut, cut0=self.cut0,
                        score=self.index.get("score") or "score", logs=self.index.get("logs"), video=bool(self.videos),
                        frames=[self.merged(f["id"]) for f in self.listed()])

    # ---- exports
    def rows(self):
        for f in self.listed():
            f = self.merged(f["id"])
            f["labels"] = [x for x in f["labels"] if not (x["was"] is None and x.get("removed"))]  # added by mistake
            yield f

    def export_csv(self, cut=None):
        cut = self.cut if cut is None else cut
        out = io.StringIO()
        w = csv.writer(out)
        video = bool(self.videos)
        head = ["frame", "image", "frame_checked", "checked_by", "label", "status", "kept", "score", "cut", "x", "y", "width", "height",
                "scientific_name", "aphia_id", "rank", "agent_label", "not_sure", "note", "what_changed", "found_by", "label_checked",
                "label_by", "agent_reason"]
        extra = [k for k in ("time", "depth", "dive") if any(k in self.base(f["id"]) for f in self.index["frames"][:20])]
        head += extra + (["clip", "clip_time_s", "new_frame"] if video else [])
        w.writerow(head)
        for f in self.rows():
            for lab in f["labels"]:
                x0, y0, x1, y1 = lab["box"]
                t = lab["taxon"] or {}
                row = [f["id"], f.get("file", ""), "yes" if frame_checked(f, cut) else "partly" if f["done"] else "no", f["by"], lab["id"], status(f, lab, cut),
                       "yes" if kept(f, lab, cut) else "no", lab.get("s", ""), "" if cut is None else cut, x0, y0, round(x1 - x0, 1),
                       round(y1 - y0, 1), t.get("name", ""), t.get("id") or "", t.get("rank", ""), lab.get("agent", ""),
                       "yes" if lab.get("unsure") else "", lab.get("note", ""), what(lab), lab.get("made", ""),
                       "yes" if checked(f, lab) else "no", lab.get("by", ""), lab.get("why", "")]
                row += [f.get(k, "") for k in extra]
                if video:
                    row += [f.get("clip", ""), f.get("frame_at", ""), "yes" if f.get("new") else ""]
                w.writerow(row)
        return out.getvalue().encode()

    def export_coco(self, cut=None, checked_only=False, polygons=False):
        """The kept labels as COCO: one category per name (its AphiaID and rank with it), the exact mask where the box was
        not moved (COCO RLE, or the outline as polygons with polygons=True), the box alone for moved and added boxes."""
        cut = self.cut if cut is None else cut
        cats, images, anns = {}, [], []
        n_frames = 0
        for k, f in enumerate(self.rows(), 1):
            done = frame_checked(f, cut)
            if checked_only and not done:
                continue
            n_frames += 1
            im = dict(id=k, file_name=f.get("file") or f"{f['id']}.jpg", width=f["w"], height=f["h"], frame=f["id"],
                      checked=done, checked_by=f["by"], checked_at=f["at"])
            for key in ("time", "depth", "dive"):
                if key in f:
                    im[key] = f[key]
            if f.get("video"):
                im.update(clip=f["clip"], clip_time_s=f["frame_at"], new_frame=bool(f.get("new")))
            images.append(im)
            masks = self.masks(f["id"]) if f["id"] in self.ids else {}
            for lab in f["labels"]:
                if not kept(f, lab, cut):
                    continue
                t = lab["taxon"] or dict(id=None, name="unnamed", rank="")
                c = cats.setdefault((t.get("id"), t["name"]), dict(id=len(cats) + 1, name=t["name"], aphia_id=t.get("id"), rank=t.get("rank", "")))
                x0, y0, x1, y1 = lab["box"]
                change = what(lab)
                same = "box moved" not in change and lab["was"] is not None
                rle = masks.get(lab["id"]) if same else None
                if same and polygons and lab.get("poly"):
                    seg, area = [[v for p in ring for v in p] for ring in lab["poly"]], None
                elif rle:
                    seg, area = rle, sum(int(v) for v in _counts(rle)[1::2])
                elif same and lab.get("poly"):
                    seg, area = [[v for p in ring for v in p] for ring in lab["poly"]], None
                else:
                    seg, area = [], None
                if area is None:
                    area = round(sum(_poly_area(r) for r in seg), 1) if seg else round((x1 - x0) * (y1 - y0), 1)
                anns.append(dict(id=len(anns) + 1, image_id=k, category_id=c["id"], bbox=[x0, y0, round(x1 - x0, 1), round(y1 - y0, 1)],
                                 area=area, iscrowd=0, segmentation=seg, score=(lab["s"] / 100 if scored(lab) else None),
                                 attributes=dict(label=lab["id"], score=lab.get("s"), change=change, checked=checked(f, lab),
                                                 not_sure=bool(lab.get("unsure")), note=lab.get("note", ""), found_by=lab.get("made", ""),
                                                 agent_label=lab.get("agent", ""), agent_reason=lab.get("why", ""), by=lab.get("by", ""),
                                                 **({"source_id": lab["src"]} if "src" in lab else {}))))
        info = dict(description=f"{self.index.get('title', '')}: the labels kept after review", exported=now(), bundle=self.index.get("uid"),
                    source=self.index.get("source", {}), score=self.index.get("score"), cut=cut, mode=self.mode, frames=n_frames,
                    frames_shown=("checked frames only: marked right, with no label kept now that nobody saw" if checked_only else "all frames"),
                    note=("Kept: not removed, and added or checked by a reviewer or scored at or above the cut. A label whose box a "
                          "reviewer moved or drew has its box only (segmentation []). Masks are COCO RLE unless polygons were asked for."))
        return json.dumps(dict(info=info, images=images, categories=list(cats.values()), annotations=anns)).encode()

    def decisions(self):
        """Everything the reviewers decided, in one file that can be loaded into a bundle built from the same masks."""
        frames = []
        for g in self.listed():
            f, e = self.merged(g["id"]), self.saved(g["id"])
            if not e and not f.get("new"):
                continue
            labs = []
            for lab in f["labels"]:  # every label someone changed, a box drawn and removed again included
                if not lab.get("rev"):
                    continue
                labs.append(dict(id=lab["id"], box0=lab["was"]["box"] if lab["was"] else None, score=lab.get("s"),
                                 **{k: lab.get(k) for k in ("box", "taxon", "note", "unsure", "removed", "ok", "by", "at")}))
            # done: only when someone marked the frame right or opened it again (by is set then); masks0: the masks it
            # started with, so that marking it right is carried over only onto the same masks
            d = dict(frame=f["id"], done=bool(f["done"]) if e and e.get("by") else None, done_cut=f.get("done_cut"), by=f["by"],
                     at=f["at"], masks0=[[lab["id"], lab["was"]["box"]] for lab in f["labels"] if lab["was"] is not None], labels=labs)
            if f.get("new"):
                d["new"] = dict(f["new"], clip=f.get("clip"), clip_time_s=f.get("frame_at"), note="a frame taken from the video; its picture is not in this file")
            frames.append(d)
        return dict(kind=DECISIONS, version=2, bundle=self.index.get("uid"), title=self.index.get("title"), source=self.index.get("source", {}),
                    score=self.index.get("score"), cut=self.cut, mode=self.mode, saved=now(), frames=frames)

    def load_decisions(self, body, address=""):
        """A decisions file laid over this review as the person loading it. A label counts only on the same mask: the same
        frame, label number and starting box. Also reads the review page's files (version 1: a cut and rejections)."""
        d = body.get("decisions") if isinstance(body, dict) else None
        if not isinstance(d, dict) or d.get("kind") != DECISIONS:
            raise ValueError("not a decisions file")
        self.person(body.get("uid"))
        name = str(body.get("name") or "decisions")[:120]
        changes, skipped, n, unmarked = {}, [], 0, 0

        def put(fr, x):
            changes.setdefault(fr, dict(frame=fr, labels=[]))["labels"].append(x)

        if d.get("version") == 1:   # the review page: rejections by mask number and box
            for r in d.get("rejected") or []:
                fr, i = str(r.get("frame")), f"m{int(r.get('n') or 0)}"
                lab = self._label(fr, i)
                if not lab or not _same_box(lab["was"]["box"], r.get("box")):
                    skipped.append(f"{fr} {i}")
                    continue
                put(fr, dict(_state(lab), id=i, base=0, removed=True))
                n += 1
        else:
            for g in d.get("frames") or []:
                fr = str(g.get("frame"))
                if not self.exists(fr):
                    skipped.append(f"{fr} (frame)")
                    continue
                for x in g.get("labels") or []:
                    i = str(x.get("id"))
                    lab = self._label(fr, i)
                    if NEW_ID.fullmatch(i):
                        if lab and lab["was"] is not None:
                            skipped.append(f"{fr} {i}")
                            continue
                    elif not lab or not _same_box(lab["was"]["box"], x.get("box0")):
                        skipped.append(f"{fr} {i}")
                        continue
                    put(fr, dict(id=i, base=0, **{k: x.get(k) for k in ("box", "taxon", "note", "unsure", "removed", "ok")}))
                    n += 1
                if isinstance(g.get("done"), bool):
                    if not self._same_masks(fr, g.get("masks0")):  # marked right on other masks: it says nothing about these
                        unmarked += 1
                        skipped.append(f"{fr} (marked right on other masks)")
                    else:
                        c = changes.setdefault(fr, dict(frame=fr, labels=[]))
                        c["done"] = g["done"]
                        c["cut"] = clean_cut(g["done_cut"]) if number(g.get("done_cut")) and 0 <= g["done_cut"] <= 100 else None
        result = dict(labels=n, frames=len(changes), skipped=len(skipped), examples=skipped[:8], cut=None, refused=0)
        if changes:  # the labels first: when the file is not one, nothing has moved
            r = self.save(dict(uid=body["uid"], session=None, changes=list(changes.values())), address, force=True, why=f"loaded {name}")
            result["refused"] = sum(len(b["refused"]) for b in r["bad"])
            result["labels"] -= result["refused"]
            result["examples"] += [f"{b['frame']} {i}: {e}" for b in r["bad"] for i, e in zip(b["refused"], b["errors"])][:8]
        if self.mode == "tune" and number(d.get("cut")) and 0 <= d["cut"] <= 100 and self.cut is not None:
            result["cut"] = self.set_cut(dict(uid=body["uid"], session=body.get("session"), cut=d["cut"]), address)["cut"]
        result["cut_ignored"] = self.mode != "tune" and number(d.get("cut")) and d.get("cut") != self.cut
        return result

    def _same_masks(self, fr, masks0):
        """Whether a frame starts with these masks: the same label ids with the same boxes (within a pixel)."""
        if fr not in self.ids or not isinstance(masks0, list):
            return False
        have = {lab["id"]: lab["box"] for lab in self.base(fr)["labels"]}
        try:
            given = {str(i): b for i, b in masks0}
        except (TypeError, ValueError):
            return False
        return set(have) == set(given) and all(_same_box(have[i], given[i]) for i in have)

    def _label(self, fr, i):
        if not self.exists(fr):
            return None
        return next((x for x in self.merged(fr)["labels"] if x["id"] == i), None)


def _state(lab):
    return {k: lab.get(k) for k in ("box", "taxon", "note", "unsure", "removed", "ok")}


def _same_box(a, b, tol=1.0):
    return isinstance(b, list) and len(b) == 4 and all(number(v) for v in b) and all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))


def _counts(rle):
    from .rle import counts_of  # numpy-free
    return counts_of(rle)


def _poly_area(flat):
    s = 0.0
    for k in range(0, len(flat) - 2, 2):
        s += flat[k] * flat[k + 3] - flat[k + 2] * flat[k + 1]
    s += flat[-2] * flat[1] - flat[0] * flat[-1]
    return abs(s) / 2
