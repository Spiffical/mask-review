# Formats

## The bundle

`mask-review build` (or `mask_review.bundle.write`) writes a folder that `mask-review serve` serves. The build never
changes after it is made; the reviewers' work goes to `edits/`.

```
index.json             format, title, uid, score (its name, or null), cut (the default), source, logs (a name), frames
frames/<id>.json       one frame and its labels
masks/<id>.json        {label id: COCO RLE}: each label's exact mask, for the exports
img/<id>.jpg           the picture
thumb/<id>.jpg         the frame strip's picture
crop/<id>/<label>.jpg  each label's tile
video/<clip>.mp4       only for bundles built with video
taxa.json              the name list (may be empty)
edits/                 the review (below)
```

A frame:

```json
{"id": "dive12_0001", "w": 1920, "h": 1080, "file": "0001.jpg", "clip": "dive12_0001", "video": "", "fps": 0, "frame_at": 0,
 "time": "2024-05-01 10:12:03", "logs": [{"name": "Asteroidea", "taxon": null, "dt": -4, "n": 1, "comment": ""}],
 "labels": [{"id": "m1", "box": [120.0, 40.0, 180.0, 95.0], "poly": [[[121, 40], [180, 52], [150, 95]]],
             "s": 87, "why": "five arms, clear edge", "made": "SAM 3", "agent": "sea star",
             "taxon": {"id": 123080, "name": "Asteroidea", "rank": "Class"}, "crop": [99, 17, 102]}]}
```

`box` is [x0, y0, x1, y1] in the picture's pixels; `poly` is the outline the page draws (rings of points, simplified to
under a pixel; empty for a box alone); `s` is the score, 0 to 100 (absent when the label has none); `why` the reason
for it; `made` the step that found it; `agent` the agents' name for it; `taxon` its starting name (null: none);
`crop` the square of its tile. `time`, `depth`, `dive` and `logs` are optional and shown beside the frame.
Label ids are `m` and the label's number in its source (its place in the job's layer, or among its picture's
annotations in a COCO file). Bundles written by the earlier label editor have the same shape without scores, and are
served as they are.

## The review: `edits/`

```
edits/<frame>.json     the labels someone changed, each with its revision and who changed it last, and whether the
                       frame was marked right (with the cut of that moment, done_cut)
edits/history.jsonl    every save, with who sent it: nothing is ever lost
edits/cut.json         the cut, when reviewers moved it (--tune)
edits/users.json       profiles: names, optional emails, starred names. Emails never leave this file
edits/new/             frames reviewers made from the video, with their pictures
```

## The decisions file

What the reviewers decided, in one file. The page's Export menu and `mask-review export --decisions-out` write it;
**Load decisions…** in the page and `mask-review export --decisions` read it.

```json
{"kind": "mask-review-decisions", "version": 2, "bundle": "1ec14a062335", "title": "Dive 12", "source": {"kind": "coco"},
 "score": "verifier vote", "cut": 55, "mode": "tune", "saved": "2026-11-25T22:31:00Z",
 "frames": [{"frame": "dive12_0001", "done": true, "done_cut": 55, "by": "A. Reviewer", "at": "2026-11-25T22:30:12Z",
             "masks0": [["m1", [120.0, 40.0, 180.0, 95.0]], ["m2", [300.0, 210.0, 330.0, 260.0]]],
             "labels": [{"id": "m1", "box0": [120.0, 40.0, 180.0, 95.0], "score": 87, "box": [118.0, 40.0, 182.0, 97.0],
                         "taxon": {"id": 123080, "name": "Asteroidea", "rank": "Class"}, "note": "", "unsure": false,
                         "removed": false, "ok": true, "by": "A. Reviewer", "at": "2026-11-25T22:29:58Z"},
                        {"id": "xk3q9a", "box0": null, "box": [400.0, 300.0, 440.0, 330.0], "taxon": null, "removed": false, "ok": false}]}]}
```

A label counts only on the same mask: the same frame, label id and starting box (`box0`, within a pixel). Labels a
reviewer drew have ids starting with `x` and no `box0`; one drawn and removed again is in the file with `removed: true`.
`done` is true when the frame was marked right, false when it was opened again, and null when nobody did either; it is
carried over only onto a frame that starts with the same masks (`masks0`). A label that is not one (a box under a pixel,
a name over 200 characters) is refused on its own and counted. The file's cut is used last, and not at all when the
review's cut is fixed. Cuts are whole numbers from 0 to 100. Files of the earlier static review page (`version` 1: a `cut` and `rejected` masks by frame, number
and box) are read as removals.

## The exports

**COCO** (`/export/labels.coco.json?frames=all|checked&seg=rle|polygon`, or `mask-review export --coco`): the labels
that stay. `images` carry the frame id and whether the frame was checked (marked right, with no label kept now that
nobody saw; `frames=checked` gives only those); `categories` are names, with `aphia_id`
and `rank` (labels without a name are `unnamed`); each annotation has `bbox`, `area`, `segmentation` (the exact mask
as COCO RLE, polygons with `seg=polygon`, or `[]` for a box a reviewer moved or drew), `score` (0 to 1, or null) and
`attributes` (label id, score 0 to 100, what changed, checked, not sure, note, the step that found it, the agents' label
and reason, who changed it last, and `source_id`, the annotation id of a COCO source). `info` records the cut and the
mode.

**CSV** (`/export/labels.csv`, or `mask-review export --csv`): one row per label, kept or not: frame, image, whether
the frame was checked (yes; partly: marked right, but a lowered cut has brought in labels nobody saw; no) and by whom, label id, status (kept, under the cut, removed), kept, score, cut, x, y, width,
height, scientific name, AphiaID, rank, the agents' label, not sure, note, what changed, the step that found it, whether
the label was checked, who changed it last, the agents' reason; then time, depth and dive when the frames carry them,
and the clip, its time and whether it is a new frame when the bundle has video.
