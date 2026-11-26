"""COCO RLE without pycocotools: the strings below were written by pycocotools 2.0 for the same masks."""
import unittest

import numpy as np

from mask_review import rle

REFERENCE = {  # name: (size, counts, area), from pycocotools.mask.encode
    "block_5x6": ([5, 6], ";320004", 9),
    "one_1x1": ([1, 1], "01", 1),
    "empty_4x4": ([4, 4], "`0", 0),
    "edges_7x9": ([7, 9], "016000L10O010O010O010O4006", 23),
}


def masks():
    m = np.zeros((5, 6), bool)
    m[1:4, 2:5] = True
    yield "block_5x6", m
    yield "one_1x1", np.ones((1, 1), bool)
    yield "empty_4x4", np.zeros((4, 4), bool)
    m = np.zeros((7, 9), bool)
    m[0, :] = True
    m[:, 8] = True
    m[3:5, 2:6] = True
    yield "edges_7x9", m


class RLE(unittest.TestCase):
    def test_same_strings_as_pycocotools(self):
        for name, m in masks():
            size, counts, area = REFERENCE[name]
            got = rle.encode(m)
            self.assertEqual((got["size"], got["counts"], rle.area(got)), (size, counts, area), name)
            self.assertTrue(np.array_equal(rle.decode(dict(size=size, counts=counts)), m), name)

    def test_long_runs_and_negative_differences(self):
        m = np.zeros((300, 200), bool)
        m[40:260, 30:170] = True
        m[100:120, 60:140] = False
        r = rle.encode(m)
        self.assertTrue(r["counts"].startswith("`j8l6`2000"))  # pycocotools' string for this mask
        self.assertEqual(rle.area(r), 29200)
        self.assertTrue(np.array_equal(rle.decode(r), m))

    def test_random_round_trips(self):
        g = np.random.default_rng(1)
        for _ in range(200):
            m = g.random((int(g.integers(1, 40)), int(g.integers(1, 40)))) < g.random()
            self.assertTrue(np.array_equal(rle.decode(rle.encode(m)), m))

    def test_uncompressed_and_bytes(self):
        un = {"size": [3, 4], "counts": [2, 3, 4, 3]}
        self.assertEqual(rle.compress(un)["counts"], "2340")  # pycocotools.mask.frPyObjects gives the same
        self.assertEqual(int(rle.decode(un).sum()), 6)
        self.assertEqual(rle.compress({"size": [1, 1], "counts": b"01"})["counts"], "01")
        with self.assertRaises(ValueError):
            rle.decode({"size": [2, 2], "counts": [1, 1]})

    def test_bbox(self):
        m = np.zeros((10, 10), bool)
        m[2:5, 3:9] = True
        self.assertEqual(rle.bbox(m), [3.0, 2.0, 9.0, 5.0])
        self.assertIsNone(rle.bbox(np.zeros((3, 3), bool)))


if __name__ == "__main__":
    unittest.main()
