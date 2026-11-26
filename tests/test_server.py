"""The server and the review state, on a made-up bundle: no data, no network, standard library only."""
import base64
import csv
import io
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mask_review import rle, server, store

SAB = dict(id=159463, name="Anoplopoma fimbria", rank="Species")
OPH = dict(id=123084, name="Ophiuroidea", rank="Class")


def square_mask(h, w, box):
    """A boolean mask (as nested lists turned into RLE without numpy)."""
    x0, y0, x1, y1 = box
    counts, flat = [], []
    for x in range(w):
        for y in range(h):
            flat.append(x0 <= x < x1 and y0 <= y < y1)
    run, val = 0, False
    for v in flat:
        if v == val:
            run += 1
        else:
            counts.append(run)
            run, val = 1, v
    counts.append(run)
    return {"size": [h, w], "counts": rle._to_string(counts)}


def make_bundle(cut=50, video=True):
    root = Path(tempfile.mkdtemp())
    for d in ("frames", "img", "thumb", "video", "masks"):
        (root / d).mkdir()
    frame = dict(id="f1", w=100, h=80, dive=1, depth=900.0, time="2024-01-01 00:00:00", file="v.mp4", clip="c", fps=30.0,
                 frame_at=1.0, logs=[dict(name="x", taxon=None, dt=9, n=1, comment="")], video="c" if video else "",
                 labels=[dict(id="m1", box=[10, 10, 30, 20], made="SAM 3", agent="sablefish", taxon=SAB, s=80, why="clear fish",
                              poly=[[[10, 10], [30, 10], [30, 20], [10, 20]]]),
                         dict(id="m2", box=[50, 50, 60, 60], made="a detector", agent="", taxon=None, s=40,
                              poly=[[[50, 50], [60, 50], [60, 60]]])])
    (root / "frames/f1.json").write_text(json.dumps(frame))
    (root / "masks/f1.json").write_text(json.dumps({"m1": square_mask(80, 100, (10, 10, 30, 20))}))
    (root / "index.json").write_text(json.dumps(dict(format=2, title="T", uid="abc123", score="vote", cut=cut,
                                                     frames=[dict(id="f1", n=2, unnamed=1, clip="c", at=1.0)])))
    (root / "crop/f1").mkdir(parents=True)
    (root / "crop/f1/m1.jpg").write_bytes(b"\xff\xd8tile")
    (root / "video/c.mp4").write_bytes(bytes(range(200)))
    (root / "taxa.json").write_text(json.dumps(dict(taxa=[], classes={})))
    return root


class Base(unittest.TestCase):
    mode, cut = "fixed", None

    def setUp(self):
        self.root = make_bundle()
        self.start()

    def start(self, mode=None, cut="same"):
        self.http = server.make(self.root, mode=mode or self.mode, cut=self.cut if cut == "same" else cut, port=0, quiet=True)
        self.s = self.http.store
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def stop(self):
        self.http.shutdown()
        self.http.server_close()

    def tearDown(self):
        self.stop()
        shutil.rmtree(self.root)

    def get(self, path):
        with urllib.request.urlopen(self.url + path) as r:
            return r.read()

    def send(self, path, body):
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def user(self, name="Reviewer", email="reviewer@example.org"):
        return self.send("/api/profile", dict(name=name, email=email))[1]["uid"]

    def label(self, i, frame="f1", **change):
        lab = next((x for x in json.loads(self.get(f"/api/frame/{frame}"))["labels"] if x["id"] == i), dict(box=[1, 1, 9, 9], taxon=None, rev=0))
        return dict({k: lab.get(k) for k in ("box", "taxon", "note", "unsure", "removed", "ok")}, id=i, base=lab["rev"]) | change

    def put(self, labels, done=None, frame="f1", uid=None, session="aaaaaa", cut=None):
        change = dict(frame=frame, done=done, labels=labels)
        if cut is not None:
            change["cut"] = cut
        return self.send("/api/save", dict(uid=uid or self.user(), session=session, changes=[change]))

    def sync(self, uid, session, seq=0, boot=None, **more):
        return self.send("/api/sync", dict(dict(uid=uid, session=session, seq=seq, boot=boot or self.s.boot, frame="f1", sel=[]), **more))

    def coco(self, frames="all", seg=None):
        return json.loads(self.get(f"/export/labels.coco.json?frames={frames}" + (f"&seg={seg}" if seg else "")))

    def csv(self):
        return {r["label"]: r for r in csv.DictReader(io.StringIO(self.get("/export/labels.csv").decode()))}


class Editing(Base):
    """What the label editor did, kept as it was."""

    def test_round_trip_and_exports(self):
        code, d = self.put([self.label("m1", removed=True), self.label("m2", taxon=OPH, box=[50, 50, 70, 70], ok=True),  # naming checks it
                            self.label("xabcd12", taxon=SAB, note="half in shadow"), self.label("xabcd13", removed=True)], done=True)
        self.assertEqual((code, [k["rev"] for k in d["kept"]], d["theirs"]), (200, [1, 1, 1, 1], []))
        f = json.loads(self.get("/api/frame/f1"))
        by = {lab["id"]: lab for lab in f["labels"]}
        self.assertTrue(by["m1"]["removed"] and f["done"] and f["by"] == "Reviewer")
        self.assertEqual((by["m2"]["taxon"], by["m2"]["by"]), (OPH, "Reviewer"))
        self.assertIsNone(by["m2"]["was"]["taxon"])
        self.assertIsNone(by["xabcd12"]["was"])
        rows = self.csv()
        self.assertEqual({k: r["what_changed"] for k, r in rows.items()}, {"m1": "removed", "m2": "named + box moved", "xabcd12": "added"})
        self.assertEqual((rows["m2"]["aphia_id"], rows["m2"]["label_by"], rows["m2"]["status"]), ("123084", "Reviewer", "kept"))
        coco = self.coco("checked")
        self.assertEqual({a["attributes"]["label"] for a in coco["annotations"]}, {"m2", "xabcd12"})
        self.assertEqual([a["segmentation"] for a in coco["annotations"]], [[], []])  # moved and drawn boxes keep no mask
        self.assertEqual(json.loads((self.root / "frames/f1.json").read_text())["labels"][0]["box"], [10, 10, 30, 20])

    def test_label_tick(self):
        self.put([self.label("m1", ok=True)])
        self.assertEqual({k: r["label_checked"] for k, r in self.csv().items()}, dict(m1="yes", m2="no"))
        self.put([], done=True, cut=0)  # marked right while every label was shown
        self.assertEqual({r["label_checked"] for r in self.csv().values()}, {"yes"})

    def test_two_reviewers(self):
        a, b = self.user("A", "a@example.org"), self.user("B", "b@example.org")
        m1, m2 = self.label("m1", taxon=OPH), self.label("m2", taxon=OPH)  # both as first loaded
        self.assertEqual(self.put([m1], uid=a)[1]["theirs"], [])
        code, d = self.put([dict(m1, taxon=SAB), m2], uid=b, session="bbbbbb")  # m1 came too late, m2 is fine
        self.assertEqual(([k["id"] for k in d["kept"]], d["theirs"][0]["frame"], d["theirs"][0]["refused"]), (["m2"], "f1", ["m1"]))
        self.assertEqual({x["id"]: (x["taxon"], x["by"]) for x in d["theirs"][0]["labels"]}, {"m1": (OPH, "A"), "m2": (OPH, "B")})
        self.assertEqual(self.put([self.label("m1", taxon=SAB)], uid=b)[1]["theirs"], [])
        lines = [json.loads(x) for x in (self.root / "edits/history.jsonl").read_text().splitlines() if '"labels"' in x]
        self.assertEqual([(x["by"], list(x["labels"])) for x in lines], [("A", ["m1"]), ("B", ["m2"]), ("B", ["m1"])])

    def test_sync(self):
        a, b = self.user("A", "a@example.org"), self.user("B", "b@example.org")
        all_ = json.loads(self.get("/api/all"))
        self.assertEqual((all_["seq"], [f["id"] for f in all_["frames"]], all_["boot"], all_["mode"], all_["cut"]), (0, ["f1"], self.s.boot, "fixed", 50))
        self.assertEqual(self.sync(a, "aaaaaa", sel=["m1"])[1]["people"], [])
        self.put([self.label("m1", taxon=OPH)], uid=a)
        mine, theirs = self.sync(a, "aaaaaa")[1], self.sync(b, "bbbbbb")[1]
        self.assertEqual((mine["events"], theirs["seq"]), ([], 1))
        e = theirs["events"][0]
        self.assertEqual((e["frame"], e["by"], e["labels"][0]["id"], e["labels"][0]["taxon"], e["labels"][0]["rev"]), ("f1", "A", "m1", OPH, 1))
        self.assertEqual([(p["name"], p["frame"]) for p in theirs["people"]], [("A", "f1")])
        self.assertEqual(self.send("/api/bye", dict(session="aaaaaa"))[1], dict(left=True))
        self.assertEqual([self.sync(b, "bbbbbb", seq=1)[1][k] for k in ("events", "people")], [[], []])
        self.assertTrue(self.sync(b, "bbbbbb", boot="other")[1]["reload"])
        self.assertTrue(self.sync(b, "bbbbbb", seq=9)[1]["reload"])
        for text in (self.get("/api/all"), self.get("/export/labels.csv"), self.get("/export/labels.coco.json"),
                     self.get("/export/decisions.json"), json.dumps(theirs).encode()):
            self.assertNotIn(b"example.org", text)  # emails stay on the server
            self.assertNotIn(a.encode(), text)
        self.assertNotIn("example.org", (self.root / "edits/history.jsonl").read_text())

    def test_profiles_and_stars(self):
        a = self.user("A", "a@example.org")
        self.assertEqual(self.send("/api/profile", dict(name="A again", email="A@Example.org"))[1]["uid"], a)  # the same email is the same person
        self.assertEqual(self.send("/api/profile", dict(uid=a, name="A", email="new@example.org"))[1]["uid"], a)
        self.assertEqual(self.send("/api/hello", dict(uid=a))[1]["email"], "new@example.org")
        n1, n2 = (self.send("/api/profile", dict(name="No email"))[1]["uid"] for _ in range(2))
        self.assertNotEqual(n1, n2)  # without an email, a profile is its browser's
        self.assertEqual(self.send("/api/profile", dict(uid=n1, name="No email, renamed", email=""))[1]["uid"], n1)
        for bad in (dict(name="", email="x@example.org"), dict(name="X", email="x"), dict(name="X", email="a b@example.org"), []):
            self.assertEqual(self.send("/api/profile", bad)[0], 400)
        for t, on, want in ((SAB, True, [SAB]), (OPH, True, [SAB, OPH]), (SAB, True, [SAB, OPH]), (SAB, False, [OPH])):
            self.assertEqual(self.send("/api/fav", dict(uid=a, taxon=t, on=on))[1]["favs"], want)
        self.assertEqual(self.sync(a, "aaaaaa")[1]["favs"], [OPH])
        for path, body in (("/api/hello", dict(uid="nope")), ("/api/fav", dict(uid="nope", taxon=SAB, on=True)), ("/api/sync", dict(uid=None, session="aaaaaa")),
                           ("/api/save", dict(uid="nope", changes=[dict(frame="f1", labels=[])])), ("/api/cut", dict(uid="nope", cut=5))):
            self.assertIn(self.send(path, body)[0], (401, 403))

    def test_edits_from_before_label_revisions(self):
        (self.root / "edits/f1.json").write_text(json.dumps(dict(rev=3, by="Old", at="t", done=True, labels=[
            dict(id="m1", box=[1, 1, 5, 5], taxon=OPH, note="", unsure=True, removed=False), dict(id="m2", box=[50, 50, 60, 60], taxon=None, note="", unsure=False, removed=True)])))
        f = json.loads(self.get("/api/frame/f1"))
        self.assertEqual((f["done"], f["labels"][0]["taxon"], f["labels"][0]["rev"], f["labels"][1]["removed"]), (True, OPH, 1, True))
        self.assertEqual(self.put([self.label("m1", taxon=SAB)])[1]["kept"][0]["rev"], 2)
        self.assertTrue(json.loads(self.get("/api/frame/f1"))["labels"][1]["removed"])

    def test_video_ranges(self):
        for asked, code, want in ((None, 200, bytes(range(200))), ("bytes=10-19", 206, bytes(range(10, 20))), ("bytes=190-", 206, bytes(range(190, 200))),
                                  ("bytes=-5", 206, bytes(range(195, 200)))):
            req = urllib.request.Request(self.url + "/video/c.mp4", headers={"Range": asked} if asked else {})
            with urllib.request.urlopen(req) as r:
                self.assertEqual((r.status, r.read()), (code, want))
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/video/f1.mp4")
        e.exception.close()

    def test_new_frame(self):
        def jpg(w, h):
            return base64.b64encode(b"\xff\xd8\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" * 20).decode()
        uid = self.user()
        ask = {"from": "f1", "t": 3.0, "uid": uid, "session": "aaaaaa", "jpg": jpg(100, 80), "thumb": jpg(24, 19)}

        def order():
            return [x["id"] for x in json.loads(self.get("/api/all"))["frames"]]
        code, d = self.send("/api/new", ask)
        self.assertEqual((code, d["id"], d["order"], d["frame"]["new"]["by"]), (200, "c_t003000", ["f1", "c_t003000"], "Reviewer"))
        e = self.sync(uid, "bbbbbb")[1]["events"][0]
        self.assertEqual((e["order"], e["frames"][0]["id"]), (["f1", "c_t003000"], "c_t003000"))
        self.assertEqual(self.send("/api/new", ask)[1]["id"], "c_t003000")
        self.assertEqual(self.send("/api/new", dict(ask, t=1.01))[1]["id"], "f1")
        f = json.loads(self.get("/api/frame/c_t003000"))
        self.assertEqual((f["time"], f["logs"][0]["dt"], f["labels"], f["names"], f["frame_at"]), ("2024-01-01 00:00:02", 7, [], [SAB], 3.0))
        self.assertEqual(order(), ["f1", "c_t003000"])
        self.assertEqual(self.get("/img/c_t003000.jpg")[:2], b"\xff\xd8")
        self.assertEqual(len(self.get("/thumb/c_t003000.jpg")), 31)
        self.assertEqual(self.put([self.label("xabcd12", "c_t003000", taxon=SAB)], done=True, frame="c_t003000")[0], 200)
        self.assertEqual([(i["file_name"], i["clip_time_s"], i["new_frame"]) for i in self.coco("checked")["images"]], [("v.mp4", 3.0, True)])
        self.assertEqual(self.send("/api/new/c_t003000", dict(uid=uid, removed=True))[1]["order"], ["f1"])
        self.assertEqual(self.sync(uid, "bbbbbb", seq=2)[1]["events"][0]["order"], ["f1"])
        self.assertEqual(self.coco("checked")["images"], [])
        self.assertEqual(self.send("/api/new", ask)[1]["id"], "c_t003000")  # taken again: it comes back with its labels
        self.assertEqual(len(json.loads(self.get("/api/all"))["frames"][1]["labels"]), 1)
        for bad in (dict(ask, t=-1), dict(ask, t=True), dict(ask, jpg=jpg(10, 10)), dict(ask, jpg="!!"), dict(ask, **{"from": "../x"}), []):
            self.assertEqual(self.send("/api/new", bad)[0], 400)
        self.assertEqual(self.send("/api/new", dict(ask, uid="nope"))[0], 401)
        self.assertEqual(self.send("/api/new/f1", dict(uid=uid, removed=True))[0], 400)

    def test_refusals(self):
        ok, uid = self.label("m1"), self.user()
        one = dict(frame="f1", labels=[ok])
        for changes in ([dict(one, frame="nope")], [dict(one, frame="../f1")], [one, one], [dict(one, done="yes")], [], "x", [dict(frame="f1")],
                        [dict(one, cut=101)], [dict(one, cut="5")]):  # requests that are not a set of frames: nothing is written
            self.assertEqual(self.send("/api/save", dict(uid=uid, session="aaaaaa", changes=changes))[0], 400)
        self.assertEqual(self.send("/api/sync", dict(uid=uid, session="../x"))[0], 400)
        self.assertEqual(self.send("/api/nothing", {})[0], 400)
        for bad in (dict(ok, id="../x"), dict(ok, id="m9"), dict(ok, box=[5, 5, 5, 5]), dict(ok, box=[1, 2, 3]), dict(ok, taxon=dict(id="1", name="x")),
                    dict(ok, base=None), dict(ok, note=5), dict(ok, taxon=dict(id=None, name="x" * 201)), "x"):
            code, d = self.put([bad], uid=uid)  # a label that is not one is refused on its own
            self.assertEqual((code, d["kept"], len(d["bad"]), len(d["bad"][0]["refused"])), (200, [], 1, 1), bad)
        self.assertFalse((self.root / "edits/f1.json").exists())
        code, d = self.put([self.label("m2", taxon=OPH), dict(ok, box=[5, 5, 5.5, 20])], uid=uid)  # the rest of the batch is saved
        self.assertEqual(([k["id"] for k in d["kept"]], d["bad"][0]["refused"], d["bad"][0]["errors"]), (["m2"], ["m1"], ["box size"]))
        self.assertEqual({x["id"]: x["rev"] for x in d["bad"][0]["labels"]}, dict(m1=0, m2=1))  # the server's version, to put back
        for path in ("/server.py", "/edits/f1.json", "/edits/users.json", "/img/../index.json", "/frames/f1.json", "/masks/f1.json",
                     "/crop/f1/../../index.json", "/crop/f1/m9.jpg", "/index.json", "/img/..jpg"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.get(path)
            e.exception.close()

    def test_tile_picture_and_page(self):
        self.assertEqual(self.get("/crop/f1/m1.jpg"), b"\xff\xd8tile")
        self.assertIn(b"<title>Mask review</title>", self.get("/"))


class TheCut(Base):
    """What stays at the cut: removed never, added or checked always, the rest by score."""

    def test_fixed_cut_hides_and_exports(self):
        rows = self.csv()
        self.assertEqual({k: r["status"] for k, r in rows.items()}, dict(m1="kept", m2="under the cut"))
        self.assertEqual([a["attributes"]["label"] for a in self.coco()["annotations"]], ["m1"])
        uid = self.user()
        self.assertEqual(self.send("/api/cut", dict(uid=uid, session="aaaaaa", cut=10))[0], 403)  # fixed when the server started
        self.put([self.label("m2", ok=True)], uid=uid)  # a reviewer keeps it, whatever its score
        self.assertEqual(sorted(a["attributes"]["label"] for a in self.coco()["annotations"]), ["m1", "m2"])
        self.put([self.label("m1", removed=True)], uid=uid)
        self.assertEqual([a["attributes"]["label"] for a in self.coco()["annotations"]], ["m2"])

    def test_frame_marked_right_covers_what_was_shown(self):
        uid = self.user()
        self.put([], done=True, cut=50, uid=uid)  # m2 (40) was under the cut, so not shown and not checked
        f = self.s.merged("f1")
        self.assertEqual((f["done_cut"], store.checked(f, f["labels"][0]), store.checked(f, f["labels"][1])), (50, True, False))
        self.assertEqual({k: r["label_checked"] for k, r in self.csv().items()}, dict(m1="yes", m2="no"))
        self.stop()
        self.start(cut=90)  # a stricter cut later: m1 (80) was checked when the frame was marked right, so it stays
        self.assertEqual([a["attributes"]["label"] for a in self.coco()["annotations"]], ["m1"])

    def test_exact_masks(self):
        a = self.coco()["annotations"][0]
        m = json.loads((self.root / "masks/f1.json").read_text())["m1"]
        self.assertEqual((a["segmentation"], a["area"], a["score"], a["attributes"]["score"]), (m, 200, 0.8, 80))
        p = self.coco(seg="polygon")["annotations"][0]
        self.assertEqual(p["segmentation"], [[10, 10, 30, 10, 30, 20, 10, 20]])
        self.put([self.label("m1", box=[10, 10, 40, 20])])
        self.assertEqual(self.coco()["annotations"][0]["segmentation"], [])  # a moved box leaves the mask behind

    def test_half_pixel_move_drops_the_mask(self):
        self.put([self.label("m1", box=[10.5, 10, 30.5, 20])])  # the page drops the outline at half a pixel: so does the export
        a = self.coco()["annotations"][0]
        self.assertEqual((a["segmentation"], a["attributes"]["change"]), ([], "box moved"))

    def test_whole_number_cuts(self):
        self.assertEqual([store.clean_cut(v) for v in (55.5, 54.5, 40.4, 0, 100)], [56, 55, 40, 0, 100])
        self.stop()
        self.start(cut=55.5)
        self.assertEqual((self.s.cut, json.loads(self.get("/api/all"))["cut"]), (56, 56))

    def test_no_scores_means_no_cut(self):
        idx = json.loads((self.root / "index.json").read_text())
        idx.update(score=None, cut=None)
        (self.root / "index.json").write_text(json.dumps(idx))
        f = json.loads((self.root / "frames/f1.json").read_text())
        for lab in f["labels"]:
            lab.pop("s")
        (self.root / "frames/f1.json").write_text(json.dumps(f))
        self.stop()
        self.start()
        self.assertIsNone(json.loads(self.get("/api/all"))["cut"])
        self.assertEqual(len(self.coco()["annotations"]), 2)


class Tune(Base):
    mode = "tune"

    def test_reviewers_move_the_cut(self):
        a, b = self.user("A", "a@example.org"), self.user("B", "b@example.org")
        self.assertEqual(self.send("/api/cut", dict(uid=a, session="aaaaaa", cut=35))[1], dict(cut=35))
        e = self.sync(b, "bbbbbb")[1]
        self.assertEqual((e["cut"], e["events"][0]["cut"], e["events"][0]["by"]), (35, 35, "A"))
        self.assertEqual(sorted(x["attributes"]["label"] for x in self.coco()["annotations"]), ["m1", "m2"])
        for bad in (-1, 101, "40", None, True):
            self.assertEqual(self.send("/api/cut", dict(uid=a, session="aaaaaa", cut=bad))[0], 400)
        self.stop()
        self.start()  # the saved cut comes back
        self.assertEqual((self.s.cut, self.s.cut0), (35, 50))
        self.stop()
        self.start(mode="fixed")  # also when the review is served at a fixed cut, unless one is given
        self.assertEqual(self.s.cut, 35)
        self.stop()
        self.start(mode="fixed", cut=70)
        self.assertEqual((self.s.cut, [x["attributes"]["label"] for x in self.coco()["annotations"]]), (70, ["m1"]))


class Decisions(Base):
    mode = "tune"

    def test_checked_frames_leave_out_labels_nobody_saw(self):
        uid = self.user()
        self.put([], done=True, cut=50, uid=uid)  # m2 (40) was under the cut: not seen
        self.assertEqual([i["frame"] for i in self.coco("checked")["images"]], ["f1"])
        self.send("/api/cut", dict(uid=uid, session="aaaaaa", cut=30))  # now m2 stays, unseen
        self.assertEqual(self.coco("checked")["images"], [])
        self.assertEqual({r["frame_checked"] for r in self.csv().values()}, {"partly"})
        self.put([self.label("m2", ok=True)], uid=uid)
        self.assertEqual([i["frame"] for i in self.coco("checked")["images"]], ["f1"])

    def test_marked_right_only_onto_the_same_masks(self):
        uid = self.user()
        self.put([self.label("m1", taxon=OPH, ok=True)], done=True, cut=30, uid=uid)
        d = json.loads(self.get("/export/decisions.json"))
        self.assertEqual((d["frames"][0]["done"], d["frames"][0]["masks0"]), (True, [["m1", [10, 10, 30, 20]], ["m2", [50, 50, 60, 60]]]))
        other = make_bundle()
        f = json.loads((other / "frames/f1.json").read_text())
        f["labels"][1]["box"] = [20, 30, 40, 50]  # rebuilt: the same ids on other masks
        (other / "frames/f1.json").write_text(json.dumps(f))
        try:
            s = store.Store(other, mode="fixed", cut=90)
            me = s.set_profile(dict(name="B"))
            r = s.load_decisions(dict(uid=me["uid"], decisions=d))
            self.assertEqual((r["labels"], r["skipped"]), (1, 1))  # m1 is the same mask; the frame's mark is not carried over
            g = s.merged("f1")
            self.assertEqual((g["done"], [store.kept(g, lab, 90) for lab in g["labels"]]), (False, [True, False]))
        finally:
            shutil.rmtree(other)

    def test_frames_nobody_marked_stay_as_they_are(self):
        uid = self.user()
        self.put([self.label("m1", note="seen")], uid=uid)  # edited, never marked right
        d = json.loads(self.get("/export/decisions.json"))
        self.assertIsNone(d["frames"][0]["done"])
        other = make_bundle()
        try:
            s = store.Store(other, mode="tune")
            me = s.set_profile(dict(name="B"))
            s.save(dict(uid=me["uid"], session=None, changes=[dict(frame="f1", done=True, labels=[])]))
            s.load_decisions(dict(uid=me["uid"], decisions=d))
            self.assertTrue(s.merged("f1")["done"])  # still marked right
        finally:
            shutil.rmtree(other)

    def test_labels_before_the_cut(self):
        uid = self.user()
        bad = dict(kind="mask-review-decisions", version=2, cut=10, frames="not a list")
        self.assertEqual(self.send("/api/decisions", dict(uid=uid, session="aaaaaa", decisions=bad))[0], 400)
        self.assertEqual(self.s.cut, 50)  # a file that is not one moves nothing
        d = dict(kind="mask-review-decisions", version=2, cut=10, frames=[dict(frame="f1", labels=[
            dict(id="m1", box0=[10, 10, 30, 20], box=[10, 10, 30, 20], taxon=dict(id="159463", name="x")),
            dict(id="m2", box0=[50, 50, 60, 60], box=[50, 50, 60, 60], taxon=None, ok=True)])])
        r = self.send("/api/decisions", dict(uid=uid, session="aaaaaa", decisions=d))[1]
        self.assertEqual((r["labels"], r["refused"], r["cut"]), (1, 1, 10))  # the label that is not one is refused on its own

    def test_a_drawn_box_removed_later(self):
        uid = self.user()
        self.put([self.label("xabcd12", box=[70, 10, 90, 30], taxon=SAB)], uid=uid)
        first = json.loads(self.get("/export/decisions.json"))
        self.put([self.label("xabcd12", removed=True)], uid=uid)
        second = json.loads(self.get("/export/decisions.json"))
        self.assertTrue(second["frames"][0]["labels"][0]["removed"])
        other = make_bundle()
        try:
            s = store.Store(other, mode="tune")
            me = s.set_profile(dict(name="B"))
            s.load_decisions(dict(uid=me["uid"], decisions=first))
            self.assertIn("xabcd12", [a["attributes"]["label"] for a in json.loads(s.export_coco())["annotations"]])
            s.load_decisions(dict(uid=me["uid"], decisions=second))
            self.assertNotIn("xabcd12", [a["attributes"]["label"] for a in json.loads(s.export_coco())["annotations"]])
        finally:
            shutil.rmtree(other)

    def test_round_trip_into_another_copy(self):
        uid = self.user()
        self.send("/api/cut", dict(uid=uid, session="aaaaaa", cut=30))
        self.put([self.label("m1", taxon=OPH, note="n"), self.label("m2", removed=True), self.label("xabcd12", box=[70, 10, 90, 30], taxon=SAB)],
                 done=True, cut=30, uid=uid)
        d = json.loads(self.get("/export/decisions.json"))
        self.assertEqual((d["kind"], d["version"], d["cut"], [len(f["labels"]) for f in d["frames"]]), ("mask-review-decisions", 2, 30, [3]))
        want = (self.get("/export/labels.csv"), self.coco())
        other = make_bundle()
        try:
            s = store.Store(other, mode="tune")
            me = s.set_profile(dict(name="Second"))
            r = s.load_decisions(dict(uid=me["uid"], decisions=d, name="d.json"))
            self.assertEqual((r["labels"], r["frames"], r["skipped"], r["cut"]), (3, 1, 0, 30))
            got = s.export_coco()
            self.assertEqual([(a["attributes"]["label"], a["category_id"], a["bbox"]) for a in json.loads(got)["annotations"]],
                             [(a["attributes"]["label"], a["category_id"], a["bbox"]) for a in want[1]["annotations"]])
            rows = {r["label"]: r for r in csv.DictReader(io.StringIO(s.export_csv().decode()))}
            self.assertEqual({k: r["status"] for k, r in rows.items()}, {r["label"]: r["status"] for r in csv.DictReader(io.StringIO(want[0].decode()))})
            fixed = store.Store(make_bundle(), mode="fixed")
            me2 = fixed.set_profile(dict(name="Third"))
            r = fixed.load_decisions(dict(uid=me2["uid"], decisions=d))
            self.assertTrue(r["cut_ignored"])
            self.assertEqual(fixed.cut, 50)
            shutil.rmtree(fixed.root)
        finally:
            shutil.rmtree(other)

    def test_only_the_same_masks(self):
        uid = self.user()
        d = dict(kind="mask-review-decisions", version=2, frames=[
            dict(frame="f1", done=None, labels=[dict(id="m1", box0=[10, 10, 30, 20], box=[10, 10, 30, 20], taxon=None, removed=True),
                                                dict(id="m2", box0=[0, 0, 5, 5], box=[0, 0, 5, 5], removed=True),
                                                dict(id="m7", box0=[1, 1, 2, 2], box=[1, 1, 2, 2], removed=True)]),
            dict(frame="nope", labels=[])])
        r = self.send("/api/decisions", dict(uid=uid, session="aaaaaa", decisions=d))[1]
        self.assertEqual((r["labels"], r["skipped"]), (1, 3))
        self.assertTrue(self.s.merged("f1")["labels"][0]["removed"])
        self.assertFalse(self.s.merged("f1")["labels"][1].get("removed"))
        self.assertEqual(self.send("/api/decisions", dict(uid=uid, decisions=dict(kind="other")))[0], 400)
        self.assertEqual(self.send("/api/decisions", dict(uid="nope", decisions=d))[0], 401)

    def test_review_page_files(self):
        """A decisions file of the earlier review page: a cut and rejections by mask number and box."""
        uid = self.user()
        d = dict(kind="mask-review-decisions", version=1, cut=45, rejected=[dict(frame="f1", n=1, box=[10, 10, 30, 20]), dict(frame="f1", n=2, box=[0, 0, 1, 1])])
        r = self.send("/api/decisions", dict(uid=uid, session="aaaaaa", decisions=d))[1]
        self.assertEqual((r["labels"], r["skipped"], r["cut"]), (1, 1, 45))
        self.assertTrue(self.s.merged("f1")["labels"][0]["removed"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
