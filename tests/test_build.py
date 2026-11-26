"""Building bundles from each input, and the command line. Small made-up pictures, no network."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from mask_review import bundle, cli, demo, rle, sources, store, taxa


def quiet(*a, **k):
    pass


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def picture(self, name, w=64, h=48):
        img = np.full((h, w, 3), 60, np.uint8)
        cv2.circle(img, (20, 20), 8, (200, 180, 90), -1)
        path = self.tmp / "pics" / name
        path.parent.mkdir(exist_ok=True)
        cv2.imwrite(str(path), img)
        return path

    def test_demo(self):
        s = demo.build(self.tmp / "b", n_frames=1, log=quiet)
        idx = json.loads((self.tmp / "b/index.json").read_text())
        f = json.loads((self.tmp / "b/frames/demo_01.json").read_text())
        masks = json.loads((self.tmp / "b/masks/demo_01.json").read_text())
        self.assertEqual((idx["format"], idx["score"], idx["cut"], s["frames"]), (2, "agent score", 60, 1))
        self.assertEqual(set(masks), {lab["id"] for lab in f["labels"]})
        named = {lab["agent"]: lab["taxon"] for lab in f["labels"] if lab["agent"]}
        self.assertEqual(named.get("brittle star", {}).get("id", 123084), 123084)  # the name list's map gives starting names
        self.assertTrue(all(lab["taxon"] is None for lab in f["labels"] if not lab["agent"]))
        for lab in f["labels"]:
            self.assertTrue((self.tmp / "b/crop/demo_01" / f"{lab['id']}.jpg").exists())
            self.assertEqual(rle.bbox(rle.decode(masks[lab["id"]])), lab["box"])
        with self.assertRaises(SystemExit):  # a bundle with a reviewer's work is never built over
            (self.tmp / "b/edits").mkdir()
            (self.tmp / "b/edits/demo_01.json").write_text("{}")
            demo.build(self.tmp / "b", n_frames=1, log=quiet)

    def test_coco(self):
        self.picture("a.jpg")
        self.picture("b.png")
        m = np.zeros((48, 64), bool)
        m[12:28, 12:28] = True
        coco = dict(images=[dict(id=1, file_name="a.jpg", width=64, height=48), dict(id=2, file_name="b.png", width=64, height=48),
                            dict(id=3, file_name="missing.jpg", width=64, height=48)],
                    categories=[dict(id=1, name="sea star"), dict(id=2, name="sponge")],
                    annotations=[dict(id=10, image_id=1, category_id=1, bbox=[12, 12, 16, 16], segmentation=rle.encode(m), score=0.9),
                                 dict(id=11, image_id=1, category_id=2, bbox=[40, 5, 10, 8], segmentation=[[40, 5, 50, 5, 50, 13, 40, 13]], score=0.3),
                                 dict(id=12, image_id=1, category_id=2, bbox=[2, 30, 6, 6], segmentation=[], score=0.55,
                                      attributes=dict(why="small and pale"))])
        (self.tmp / "c.json").write_text(json.dumps(coco))
        bundle.write(sources.from_coco(self.tmp / "c.json", self.tmp / "pics", log=quiet), self.tmp / "b", cut=50, log=quiet,
                     taxa=dict(taxa=[dict(id=123080, name="Asteroidea", rank="Class")], classes={"sea star": 123080}))
        idx = json.loads((self.tmp / "b/index.json").read_text())
        self.assertEqual([f["id"] for f in idx["frames"]], ["a", "b"])
        f = json.loads((self.tmp / "b/frames/a.json").read_text())
        by = {lab["id"]: lab for lab in f["labels"]}
        self.assertEqual({k: v["s"] for k, v in by.items()}, dict(m1=90, m2=30, m3=55))  # 0-1 scores become 0-100
        self.assertEqual((by["m1"]["taxon"]["id"], by["m2"]["taxon"], by["m3"]["poly"], by["m3"]["why"], by["m1"]["src"]),
                         (123080, dict(id=None, name="sponge", rank=""), [], "small and pale", 10))
        masks = json.loads((self.tmp / "b/masks/a.json").read_text())
        self.assertEqual(masks["m1"], rle.encode(m))  # the mask given is the mask kept, exactly
        self.assertNotIn("m3", masks)  # a box alone stays a box
        s = store.Store(self.tmp / "b")
        out = json.loads(s.export_coco())
        self.assertEqual([a["attributes"]["source_id"] for a in out["annotations"]], [10, 12])  # 30 is under the cut of 50

    def test_job(self):
        job = self.tmp / "job"
        (job / "frames").mkdir(parents=True)
        (job / "final").mkdir()
        (job / "review/decisions").mkdir(parents=True)
        img = cv2.imread(str(self.picture("f.jpg")))
        cv2.imwrite(str(job / "frames/f_1.png"), img)
        m1, m2 = np.zeros((48, 64), bool), np.zeros((48, 64), bool)
        m1[10:20, 10:30] = True
        m2[30:40, 40:50] = True
        (job / "manifest.json").write_text(json.dumps(dict(frames=[dict(frame_id="f_1", width=64, height=48, image="/somewhere/f 1.jpg")])))
        (job / "final/full.json").write_text(json.dumps({"f_1": [dict(rle=rle.encode(m1), box=[10, 10, 30, 20], label="coral", origin="sam3"),
                                                                 dict(rle=rle.encode(m2), label="worm", origin="click")]}))
        (job / "review/decisions/v_v01.jsonl").write_text(json.dumps(dict(frame="f_1", votes={"1": dict(p=72, why="clear"), "2": dict(p=31, why="faint")})) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["build", "--job", str(job), "--out", str(self.tmp / "b")])
        f = json.loads((self.tmp / "b/frames/f_1.json").read_text())
        self.assertEqual([(lab["id"], lab["s"], lab["why"], lab["made"], lab["taxon"]["name"]) for lab in f["labels"]],
                         [("m1", 72, "clear", "SAM 3", "coral"), ("m2", 31, "faint", "an agent's click", "worm")])
        idx = json.loads((self.tmp / "b/index.json").read_text())
        self.assertEqual((idx["score"], idx["cut"], idx["source"]), ("verifier vote", 60, dict(kind="job", job="job", layer="full")))
        self.assertEqual(f["file"], "f 1.jpg")

    def test_images_only(self):
        self.picture("x.jpg")
        bundle.write(sources.images_only(self.tmp / "pics"), self.tmp / "b", log=quiet)
        idx = json.loads((self.tmp / "b/index.json").read_text())
        self.assertEqual((idx["score"], idx["cut"], idx["frames"][0]["n"]), (None, None, 0))

    def test_export_command_with_decisions(self):
        demo.build(self.tmp / "b", n_frames=1, log=quiet)
        f = json.loads((self.tmp / "b/frames/demo_01.json").read_text())
        low = next(lab for lab in f["labels"] if lab["s"] < 60)
        high = next(lab for lab in f["labels"] if lab["s"] >= 60)
        d = dict(kind="mask-review-decisions", version=2, cut=60, frames=[dict(frame="demo_01", done=None, labels=[
            dict(id=low["id"], box0=low["box"], box=low["box"], taxon=None, ok=True),
            dict(id=high["id"], box0=high["box"], box=high["box"], taxon=high["taxon"], removed=True)])])
        (self.tmp / "d.json").write_text(json.dumps(d))
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["export", str(self.tmp / "b"), "--coco", str(self.tmp / "plain.json")])
            cli.main(["export", str(self.tmp / "b"), "--decisions", str(self.tmp / "d.json"), "--coco", str(self.tmp / "out.json"),
                      "--csv", str(self.tmp / "out.csv"), "--decisions-out", str(self.tmp / "again.json")])
        plain = {a["attributes"]["label"] for a in json.loads((self.tmp / "plain.json").read_text())["annotations"]}
        out = {a["attributes"]["label"] for a in json.loads((self.tmp / "out.json").read_text())["annotations"]}
        self.assertEqual(out, (plain | {low["id"]}) - {high["id"]})
        self.assertFalse(any((self.tmp / "b/edits").glob("demo_01.json")))  # the bundle itself is not changed
        again = json.loads((self.tmp / "again.json").read_text())
        self.assertEqual(sorted(x["id"] for x in again["frames"][0]["labels"]), sorted([low["id"], high["id"]]))

    def test_dotted_ids_and_reading_without_writing(self):
        img = np.full((40, 60, 3), 90, np.uint8)
        m = np.zeros((40, 60), bool)
        m[5:15, 5:25] = True
        bundle.write([dict(id="dive12.0001", image=img, labels=[dict(mask=m, score=70)])], self.tmp / "b", log=quiet, cut=50)
        self.assertTrue((self.tmp / "b/frames/dive12.0001.json").exists())  # the pipeline's ids keep their dots
        from mask_review import server
        self.assertTrue(server.FILE.fullmatch("/img/dive12.0001.jpg"))
        s = store.Store(self.tmp / "b")
        self.assertEqual(len(json.loads(s.export_coco())["annotations"]), 1)
        self.assertFalse((self.tmp / "b/edits").exists())  # reading and exporting write nothing

    def test_export_reads_the_edits_folder_given(self):
        demo.build(self.tmp / "b", n_frames=1, log=quiet)
        s = store.Store(self.tmp / "b", edits=self.tmp / "work")
        me = s.set_profile(dict(name="A"))
        f = s.merged("demo_01")
        lab = next(x for x in f["labels"] if x["s"] >= 60)
        s.save(dict(uid=me["uid"], changes=[dict(frame="demo_01", labels=[dict(id=lab["id"], base=0, box=lab["box"], taxon=lab["taxon"], removed=True)])]))
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["export", str(self.tmp / "b"), "--coco", str(self.tmp / "plain.json")])
            cli.main(["export", str(self.tmp / "b"), "--edits", str(self.tmp / "work"), "--coco", str(self.tmp / "work.json")])
        plain = {a["attributes"]["label"] for a in json.loads((self.tmp / "plain.json").read_text())["annotations"]}
        work = {a["attributes"]["label"] for a in json.loads((self.tmp / "work.json").read_text())["annotations"]}
        self.assertEqual(plain - work, {lab["id"]})

    def test_name_files(self):
        (self.tmp / "n.txt").write_text("# a comment\nOphiuroidea\n\nPorifera\n")
        (self.tmp / "n.csv").write_text("scientificName,AphiaID\nAsteroidea,123080\n,558\n")
        (self.tmp / "n.json").write_text(json.dumps({"Actiniaria": 1360}))
        (self.tmp / "t.json").write_text(json.dumps([dict(name="Biota", labels=[dict(name="Actiniaria sp4"), dict(name="Sebastes (Sebastomus) sp."), dict(name="rock")])]))
        self.assertEqual(taxa.read_names(self.tmp / "n.txt"), [("Ophiuroidea", None), ("Porifera", None)])
        self.assertEqual(taxa.read_names(self.tmp / "n.csv"), [("Asteroidea", "123080"), ("", "558")])
        self.assertEqual(taxa.read_names(self.tmp / "n.json"), [("Actiniaria", 1360)])
        self.assertEqual(taxa.biigle_names(self.tmp / "t.json"), ["Actiniaria", "Sebastes (Sebastomus)"])


if __name__ == "__main__":
    unittest.main()
