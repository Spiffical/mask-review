"""Writing a bundle: the folder `mask-review serve` serves.

    index.json             title, format, the score's name and default cut, the frames in order
    frames/<id>.json       a frame: size, image file, labels (box, outline, score, the agents' label and reason,
                           starting name, tile square), and optional time, logs and video
    masks/<id>.json        each label's exact mask as COCO RLE, for the exports
    img/<id>.jpg           the picture
    thumb/<id>.jpg         the frame strip's picture
    crop/<id>/<label>.jpg  each label's tile
    video/<clip>.mp4       only when built with video
    taxa.json              the name list (optional)
    edits/                 the reviewers' work: written by the server, never by a build

The agents' labels never change after a build: the reviewers' version lives in edits/. A frame is a dict with `id`, an
`image` (BGR array) or `image_path`, an optional `file_name`, and `labels`: dicts with a `mask` (boolean array), `rle`
(COCO RLE) or `polygons` (COCO polygon lists), or only a `box` [x0, y0, x1, y1]; and optionally `score` (0 to 100), `label` (the agents' name for
it), `origin`, `why` (the reason for the score), `taxon` ({id, name, rank}: the starting name) and `n` (its number).
Optional frame fields shown on the page: `time`, `depth`, `dive`, `logs`; and for video `video_path` and `offset`.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import numpy as np

from . import rle as R

FORMAT = 2
# How the page names the step that found a mask, for the origins this project's pipeline writes; others are shown as given.
ORIGINS = {"sam3": "SAM 3", "megalodon": "FathomNet megalodon", "click": "an agent's click",
           "check_click": "an agent's click (final check)", "exemplar": "look-alike search", "detector": "a detector",
           "taxon_box": "an earlier run"}
TILE = 360


def safe(s) -> str:
    """A frame id: letters, digits, _ - and . (as the pipeline's ids), at most 80 long."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s)).strip("_.")[:80] or "frame"


def polygons(mask: np.ndarray, eps: float = 0.8) -> list:
    """A mask's outer outlines as [[[x, y], ...], ...] in frame pixels (specks under 2% of the largest part dropped)."""
    import cv2
    box = R.bbox(mask)
    if box is None:
        return []
    x0, y0, x1, y1 = (int(v) for v in box)
    sub = np.ascontiguousarray(mask[y0:y1, x0:x1]).astype(np.uint8)
    sub = cv2.copyMakeBorder(sub, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    cs = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[-2]
    big = max((cv2.contourArea(c) for c in cs), default=0)
    out = []
    for c in cs:
        if cv2.contourArea(c) < max(6, 0.02 * big):
            continue
        p = cv2.approxPolyDP(c, eps, True).reshape(-1, 2)
        if len(p) >= 3:
            out.append([[int(x) + x0 - 1, int(y) + y0 - 1] for x, y in p])
    return out


def square(box, W, H):
    """The square a label's tile shows: [x, y, side], inside the frame where it fits, else centred over it. The page has
    the same rule (cropOf) for boxes a reviewer moved or added."""
    x0, y0, x1, y1 = box
    s = round(min(max(1.7 * max(x1 - x0, y1 - y0), 72), max(W, H)))

    def at(lo, hi, n):
        return round((n - s) / 2 if s >= n else min(max((lo + hi - s) / 2, 0), n - s))
    return [at(x0, x1, W), at(y0, y1, H), s]


def tile(img, sq):
    """The tile's picture: the square cut from the image (padded with black outside it), at most TILE pixels a side."""
    import cv2
    x, y, s = sq
    H, W = img.shape[:2]
    out = np.zeros((s, s, 3), np.uint8)
    ax, ay, bx, by = max(x, 0), max(y, 0), min(x + s, W), min(y + s, H)
    if bx > ax and by > ay:
        out[ay - y:by - y, ax - x:bx - x] = img[ay:by, ax:bx]
    if s > TILE:
        out = cv2.resize(out, (TILE, TILE), interpolation=cv2.INTER_AREA)
    return out


def read_image(f):
    import cv2
    if f.get("image") is not None:
        return f["image"]
    img = cv2.imread(str(f["image_path"]), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f["image_path"])
    return img


def taxon_of(t):
    if not t:
        return None
    return dict(id=t.get("id") if isinstance(t.get("id"), int) else None, name=str(t["name"]).strip(), rank=t.get("rank") or "")


def write(frames, out: Path, *, title: str = "Mask review", score_name: str = "score", cut: float | None = None,
          taxa: dict | None = None, start_names: str = "labels", max_width: int = 0, quality: int = 90,
          source: dict | None = None, logs_name: str | None = None, video: bool = False, log=print) -> dict:
    """Write a bundle (see the module docstring). cut: the cut the page opens at (None: the labels carry no scores, or
    nothing is cut). taxa: a name list (taxa.build); its `classes` give each agents' label its starting name. start_names:
    "labels" (default) also starts every other label with the agents' label as a name without a WoRMS ID, "map" only
    with the list's classes, "none" starts everything unnamed (a label's own `taxon` always wins). max_width > 0 scales
    wider pictures down (outlines and boxes stay in the original pixels)."""
    import cv2
    out = Path(out)
    if (out / "index.json").exists() and (out / "edits").exists() and any((out / "edits").glob("*.json")):
        raise SystemExit(f"{out} holds a reviewer's work (edits/). Build into a new folder; the decisions file of the old "
                         "one can be loaded into the new one.")
    for sub in ("frames", "masks", "img", "thumb", "crop"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    taxa = taxa or dict(taxa=[], above={}, classes={})
    by_id = {t["id"]: t for t in taxa.get("taxa", [])}
    classes = {k.lower(): v for k, v in (taxa.get("classes") or {}).items()}
    index, seen, n_labels, n_scored, clips, t0 = [], set(), 0, 0, {}, time.time()
    for k, f in enumerate(frames, 1):
        img = read_image(f)
        H, W = img.shape[:2]
        fid = safe(f["id"])
        while fid in seen:
            fid += "_"
        seen.add(fid)
        (out / "crop" / fid).mkdir(exist_ok=True)
        labels, masks = [], {}
        for j, q in enumerate(f.get("labels", []), 1):
            n = int(q.get("n") or j)
            lid = f"m{n}"
            if any(x["id"] == lid for x in labels):
                raise ValueError(f"frame {f['id']}: two labels numbered {n}")
            m = q.get("mask")
            if m is None and q.get("rle"):
                rle = R.compress(q["rle"])
                if [int(v) for v in rle["size"]] != [H, W]:
                    raise ValueError(f"frame {f['id']} label {n}: mask is {rle['size']}, image is {[H, W]}")
                m = R.decode(rle)
            elif m is None and q.get("polygons"):
                m = np.zeros((H, W), np.uint8)
                cv2.fillPoly(m, [np.asarray(p, np.float32).reshape(-1, 2).round().astype(np.int32) for p in q["polygons"]], 1)
                m = m.astype(bool) if m.any() else None
            if m is not None:
                m = np.asarray(m, dtype=bool)
                if m.shape != (H, W):
                    raise ValueError(f"frame {f['id']} label {n}: mask is {list(m.shape)}, image is {[H, W]}")
            box = q.get("box") or (R.bbox(m) if m is not None else None)
            if box is None:
                continue  # an empty mask without a box: nothing to show
            box = [round(min(max(float(v), 0.0), float(W if i % 2 == 0 else H)), 1) for i, v in enumerate(box)]
            if box[2] - box[0] < 1 or box[3] - box[1] < 1:
                continue
            agent = str(q.get("label") or "").strip()
            if "taxon" in q:
                start = taxon_of(q["taxon"])
            elif agent.lower() in classes and classes[agent.lower()] in by_id and start_names != "none":
                t = by_id[classes[agent.lower()]]
                start = dict(id=t["id"], name=t["name"], rank=t["rank"])
            elif agent and start_names == "labels":
                start = dict(id=None, name=agent, rank="")
            else:
                start = None
            lab = dict(id=lid, box=box, made=ORIGINS.get(q.get("origin"), q.get("origin") or "the agents"), agent=agent,
                       taxon=start, poly=polygons(m) if m is not None else [], crop=square(box, W, H))
            s = q.get("score")
            if s is not None:
                s = float(s)
                lab["s"] = int(s) if s == int(s) else round(s, 1)
                n_scored += 1
            if q.get("why"):
                lab["why"] = str(q["why"])[:400]
            if q.get("src") is not None:
                lab["src"] = q["src"]
            if m is not None:
                masks[lid] = R.encode(m)
            cv2.imwrite(str(out / "crop" / fid / f"{lid}.jpg"), tile(img, lab["crop"]), [cv2.IMWRITE_JPEG_QUALITY, 85])
            labels.append(lab)
        small = img
        if max_width and W > max_width:
            small = cv2.resize(img, (max_width, round(H * max_width / W)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(out / "img" / f"{fid}.jpg"), small, [cv2.IMWRITE_JPEG_QUALITY, quality])
        th = cv2.resize(img, (240, max(1, round(240 * H / W))) if W / H >= 240 / 135 else (max(1, round(135 * W / H)), 135),
                        interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(out / "thumb" / f"{fid}.jpg"), th, [cv2.IMWRITE_JPEG_QUALITY, 80])
        frame = dict(id=fid, w=W, h=H, file=f.get("file_name") or f"{fid}.jpg", labels=labels, clip=f.get("clip") or fid,
                     video="", fps=0, frame_at=float(f.get("offset") or 0))
        for key in ("time", "depth", "dive", "logs", "names"):
            if f.get(key) is not None:
                frame[key] = f[key]
        if video and f.get("video_path"):
            from . import video as V
            name = safe(f.get("clip") or Path(f["video_path"]).stem)
            if name not in clips:
                clips[name] = V.prepare(Path(f["video_path"]), out / "video" / f"{name}.mp4", W, H)
            if clips[name]:
                frame.update(clip=name, video=name, fps=clips[name], frame_at=V.find_still(img, out / "video" / f"{name}.mp4", float(f.get("offset") or 0))[0])
        (out / "frames" / f"{fid}.json").write_text(json.dumps(frame, separators=(",", ":")))
        (out / "masks" / f"{fid}.json").write_text(json.dumps(masks, separators=(",", ":")))
        index.append(dict(id=fid, n=len(labels), unnamed=sum(x["taxon"] is None for x in labels), clip=frame["clip"], at=frame["frame_at"],
                          **({"time": frame["time"]} if frame.get("time") else {})))
        n_labels += len(labels)
        if k % 25 == 0:
            log(f"  {k} frames, {n_labels} labels ({time.time() - t0:.0f} s)")
    if not index:
        raise SystemExit("no frames were written")
    d = dict(format=FORMAT, title=title, uid=uuid.uuid4().hex[:12], built=time.strftime("%Y-%m-%d %H:%M"),
             score=score_name if n_scored else None, cut=int(float(cut) + 0.5) if cut is not None and n_scored else None,
             source=source or {}, logs=logs_name, frames=index)
    (out / "index.json").write_text(json.dumps(d, indent=1))
    (out / "taxa.json").write_text(json.dumps(taxa))
    files = [p for p in out.rglob("*") if p.is_file()]
    summary = dict(frames=len(index), labels=n_labels, scored=n_scored, files=len(files),
                   mb=round(sum(p.stat().st_size for p in files) / 1e6, 1))
    log(f"bundle {out}: {summary['frames']} frames, {n_labels} labels ({n_scored} scored), "
        f"{summary['files']} files, {summary['mb']} MB")
    return summary
