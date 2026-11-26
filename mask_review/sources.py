"""Where the masks come from: a labelling job of the SAM 3 agent loop, or any COCO file with scores and images.

Each reader yields frames for bundle.write (see its docstring).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import rle as R

IMAGES = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")


# --------------------------------------------------------------------------------------------- a labelling job
def job_votes(root: Path) -> dict:
    """{frame: {mask number (str): {"p", "why"}}} from the verifier's review/decisions/v_*.jsonl."""
    out = {}
    for p in sorted((root / "review" / "decisions").glob("v_*.jsonl")):
        for line in p.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                out.setdefault(d["frame"], {}).update({str(k): v for k, v in (d.get("votes") or {}).items()})
    return out


def from_job(root: Path, layer: str = "full", only=None, log=print):
    """A job's layer (final/<layer>.json: the masks before the verifier's cut) with the verifier's votes as scores.
    A job without votes gives labels without scores (no cut). Frames cut from video keep their clip and moment."""
    root = Path(root)
    man = json.loads((root / "manifest.json").read_text())
    recs = json.loads((root / "final" / f"{layer}.json").read_text())
    votes = job_votes(root)
    if not any(votes.values()):
        log(f"{root.name}: no verifier votes (review/decisions/v_*.jsonl): the labels carry no scores, so nothing is cut")
    for fr in man["frames"]:
        fid = fr["frame_id"]
        if only and fid not in only:
            continue
        path = root / "frames" / f"{fid}.png"
        if not path.exists() and fr.get("image"):
            path = Path(fr["image"])
        labels = []
        for i, q in enumerate(recs.get(fid, []), 1):
            v = votes.get(fid, {}).get(str(i))
            labels.append(dict(n=i, rle=q["rle"], box=q.get("box"), label=q.get("label"), origin=q.get("origin"),
                               score=None if v is None else v.get("p"), why=None if v is None else v.get("why")))
        f = dict(id=fid, image_path=path, file_name=Path(fr.get("image") or path).name, labels=labels)
        if fr.get("video"):
            f.update(video_path=Path(fr["video"]), offset=fr.get("offset") or fr.get("t") or 0,
                     clip=Path(fr["video"]).stem)
        for k in ("time", "depth", "dive"):
            if fr.get(k) is not None:
                f[k] = fr[k]
        yield f


# ------------------------------------------------------------------------------------------- any COCO results
def coco_scale(anns, key):
    vals = [score_of(a, key) for a in anns]
    vals = [v for v in vals if v is not None]
    return 100.0 if vals and max(vals) <= 1.0 else 1.0


def score_of(a, key):
    v = a.get(key, (a.get("attributes") or {}).get(key))
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def coco_shape(a):
    """An annotation's mask as given: dict(rle=...) or dict(polygons=...), or {} when it has only a box. Decoded one at a
    time when the bundle is written, so a frame with hundreds of large masks never holds them all at once."""
    seg = a.get("segmentation")
    if isinstance(seg, dict) and seg.get("counts") is not None:
        return dict(rle=R.compress(seg))
    if isinstance(seg, list) and any(len(p) >= 6 for p in seg):
        return dict(polygons=[p for p in seg if len(p) >= 6])
    return {}


def from_coco(coco: Path, images: Path, score_key: str = "score", log=print):
    """A COCO file whose annotations may carry a score (0 to 1 or 0 to 100, also read from `attributes`), with RLE or
    polygon masks or boxes only. Every image becomes a frame, with or without annotations."""
    d = json.loads(Path(coco).read_text())
    cats = {c["id"]: c.get("name", str(c["id"])) for c in d.get("categories", [])}
    by = {}
    for a in d.get("annotations", []):
        by.setdefault(a["image_id"], []).append(a)
    scale = coco_scale(d.get("annotations", []), score_key)
    for im in d.get("images", []):
        path = Path(images) / im["file_name"]
        if not path.exists():
            alt = Path(images) / Path(im["file_name"]).name
            if not alt.exists():
                log(f"skipped {im['file_name']}: not found under {images}")
                continue
            path = alt
        labels = []
        for k, a in enumerate(by.get(im["id"], []), 1):
            at = a.get("attributes") or {}
            s = score_of(a, score_key)
            x, y, bw, bh = a.get("bbox") or (0, 0, 0, 0)
            box = [x, y, x + bw, y + bh] if bw and bh else None
            labels.append(dict(coco_shape(a), n=k, box=box, label=at.get("label") or cats.get(a.get("category_id")), origin=at.get("origin"),
                               score=None if s is None else s * scale, why=at.get("why") or at.get("reason"), src=a.get("id")))
        yield dict(id=Path(im["file_name"]).stem, image_path=path, file_name=im["file_name"], labels=labels)


def images_only(folder: Path):
    """A folder of images without any labels: for boxing and naming from scratch."""
    for p in sorted(Path(folder).iterdir()):
        if p.suffix.lower() in IMAGES:
            yield dict(id=p.stem, image_path=p, file_name=p.name, labels=[])
