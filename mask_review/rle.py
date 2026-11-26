"""COCO run-length encoding without pycocotools.

`encode` writes the compressed string form that pycocotools writes (`{"size": [h, w], "counts": "..."}`), and `decode`
reads it, or the uncompressed form with a list of counts, so a bundle needs no pycocotools. Counts are run lengths over
the mask read column by column, starting with a run of zeros. Only encode, decode and bbox need numpy (imported when
called), so the server, which only reads counts, runs on the standard library.
"""
from __future__ import annotations


def _to_string(counts) -> str:
    """pycocotools' rleToString: 5 bits a character, a continuation bit, counts from the third on as differences."""
    out = []
    for i, x in enumerate(counts):
        x = int(x)
        if i > 2:
            x -= int(counts[i - 2])
        more = True
        while more:
            c = x & 0x1F
            x >>= 5
            more = (x != -1) if (c & 0x10) else (x != 0)
            if more:
                c |= 0x20
            out.append(chr(c + 48))
    return "".join(out)


def _from_string(s: str) -> list[int]:
    """pycocotools' rleFrString."""
    counts, p, n = [], 0, len(s)
    while p < n:
        x, k, more = 0, 0, True
        while more:
            c = ord(s[p]) - 48
            x |= (c & 0x1F) << (5 * k)
            more = bool(c & 0x20)
            p += 1
            k += 1
            if not more and (c & 0x10):
                x |= -1 << (5 * k)
        if len(counts) > 2:
            x += counts[-2]
        counts.append(x)
    return counts


def counts_of(rle: dict) -> list[int]:
    c = rle["counts"]
    if isinstance(c, bytes):
        c = c.decode("ascii")
    return _from_string(c) if isinstance(c, str) else [int(v) for v in c]


def encode(mask) -> dict:
    """A boolean mask (h, w) as compressed COCO RLE."""
    import numpy as np
    m = np.asarray(mask, dtype=bool)
    h, w = m.shape
    flat = m.ravel(order="F")
    if not flat.size:
        return {"size": [h, w], "counts": _to_string([0])}
    edges = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    runs = np.diff(np.concatenate(([0], edges, [flat.size]))).tolist()
    if flat[0]:
        runs = [0] + runs
    return {"size": [int(h), int(w)], "counts": _to_string(runs)}


def decode(rle: dict):
    """Compressed or uncompressed COCO RLE as a boolean mask (h, w)."""
    import numpy as np
    h, w = (int(v) for v in rle["size"])
    counts = counts_of(rle)
    values = np.zeros(len(counts), dtype=bool)
    values[1::2] = True
    flat = np.repeat(values, counts)
    if flat.size != h * w:
        raise ValueError(f"RLE counts cover {flat.size} pixels, not {h} x {w}")
    return flat.reshape((h, w), order="F")


def compress(rle: dict) -> dict:
    """Any COCO RLE in the compressed string form."""
    c = rle["counts"]
    if isinstance(c, (str, bytes)):
        return {"size": [int(v) for v in rle["size"]], "counts": c.decode("ascii") if isinstance(c, bytes) else c}
    return {"size": [int(v) for v in rle["size"]], "counts": _to_string([int(v) for v in c])}


def area(rle: dict) -> int:
    return int(sum(counts_of(rle)[1::2]))


def bbox(mask) -> list[float]:
    """[x0, y0, x1, y1] of a mask's pixels (x1, y1 one past the last), or None for an empty mask."""
    import numpy as np
    m = np.asarray(mask, dtype=bool)
    ys, xs = np.nonzero(m)
    if not len(xs):
        return None
    return [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)]
