"""mask-review: check the masks an annotation pipeline made.

    mask-review demo [--tune]                       a synthetic seabed, built and opened in the browser
    mask-review build --coco F --images D --out B   a bundle from any COCO file (scores optional)
    mask-review build --job J --out B               a bundle from a labelling job of the SAM 3 agent loop
    mask-review build --images D --out B            a bundle of pictures without labels, to box and name from scratch
    mask-review serve B                             the review at the bundle's cut, fixed
    mask-review serve B --cut 70                    ... at another fixed cut
    mask-review serve B --tune                      the review with full control of the cut
    mask-review export B --coco out.json --csv out.csv [--decisions F]
    mask-review taxa NAME ... [--names F] [--map F] --out taxa.json

Run `mask-review COMMAND --help` for the options of each.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__


def _serve_args(ap, port=8780):
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--tune", action="store_true", help="let the reviewers move the cut: slider, histogram, the masks nearest the cut")
    ap.add_argument("--cut", type=float, help="the cut on the score, a whole number 0-100. Without --tune it is fixed; with it, where the slider starts")
    ap.add_argument("--port", type=int, default=None, help=f"port (default {port}, or the next free one)")
    ap.add_argument("--bind", default="127.0.0.1", help="address to listen on (default 127.0.0.1: this computer only; 0.0.0.0 for a team)")
    ap.add_argument("--edits", help="where the reviewers' work is kept (default: BUNDLE/edits)")


def cmd_serve(a):
    from . import server
    server.serve(a.bundle, mode="tune" if a.tune else "fixed", cut=a.cut, port=a.port or 8780, bind=a.bind,
                 open_browser=a.open, port_given=a.port is not None, edits=a.edits)


def cmd_demo(a):
    from . import demo, server
    out = Path(a.out)
    if a.rebuild and out.exists():
        if not (out / "index.json").exists():
            raise SystemExit(f"{out} is not a bundle; not deleting it")
        shutil.rmtree(out)
    if not (out / "index.json").exists():
        demo.build(out)
    server.serve(out, mode="tune" if a.tune else "fixed", cut=a.cut, port=a.port or 8780, bind=a.bind,
                 open_browser=not a.no_open, port_given=a.port is not None, edits=a.edits)


def cmd_build(a):
    from . import bundle, sources, taxa as T
    names = T.empty()
    if a.taxa:
        names = json.loads(Path(a.taxa).read_text())
    if sum(x is not None for x in (a.job, a.coco)) > 1:
        raise SystemExit("give --job or --coco, not both")
    if a.job:
        frames = sources.from_job(Path(a.job), a.layer, only=a.frames)
        title = a.title or f"Masks of {Path(a.job).name}"
        source = dict(kind="job", job=Path(a.job).name, layer=a.layer)
        score = a.score_name or "verifier vote"
        cut = 60 if a.cut is None else a.cut
    elif a.coco:
        if not a.images:
            raise SystemExit("build --coco needs --images, the folder of its pictures")
        frames = sources.from_coco(Path(a.coco), Path(a.images), score_key=a.score_key)
        title = a.title or f"Masks of {Path(a.coco).stem}"
        source = dict(kind="coco", coco=Path(a.coco).name)
        score = a.score_name or "score"
        cut = 50 if a.cut is None else a.cut
    elif a.images:
        frames = sources.images_only(Path(a.images))
        title, source, score, cut = a.title or f"Pictures of {Path(a.images).name}", dict(kind="images"), "score", None
    else:
        raise SystemExit("give --job, --coco (with --images) or --images")
    bundle.write(frames, Path(a.out), title=title, score_name=score, cut=cut, taxa=names, start_names=a.names,
                 max_width=a.max_width, source=source, logs_name=a.logs_name, video=a.video)
    print(f"Next: mask-review serve {a.out}" + ("" if cut is None else f"   (or --tune to move the cut, or --cut N)"))


def cmd_export(a):
    from . import store
    from .store import Store
    root = Path(a.bundle)
    if not (a.coco or a.csv or a.decisions_out):
        raise SystemExit("say what to write: --coco, --csv and/or --decisions-out")
    tmp = None
    edits = Path(a.edits) if a.edits else root / "edits"
    if a.decisions:
        tmp = Path(tempfile.mkdtemp(prefix="mask-review-"))
        if edits.exists():
            shutil.copytree(edits, tmp / "edits")
        edits = tmp / "edits"
    try:
        s = Store(root, mode="tune" if a.decisions else "fixed", cut=a.cut, edits=edits)
        if a.decisions:
            me = s.set_profile(dict(name="mask-review export"))
            r = s.load_decisions(dict(uid=me["uid"], decisions=json.loads(Path(a.decisions).read_text()), name=Path(a.decisions).name))
            print(f"{a.decisions}: {r['labels']} labels on {r['frames']} frames" + (f", {r['skipped']} matched no mask here" if r["skipped"] else "")
                  + (f"; cut {r['cut']}" if r.get("cut") is not None else ""))
            if a.cut is not None:
                s.cut = store.clean_cut(a.cut)
        if a.coco:
            Path(a.coco).write_bytes(s.export_coco(checked_only=a.checked_only, polygons=a.polygons))
            d = json.loads(Path(a.coco).read_text())
            print(f"{a.coco}: {len(d['annotations'])} labels on {len(d['images'])} frames, {len(d['categories'])} names (cut {s.cut})")
        if a.csv:
            Path(a.csv).write_bytes(s.export_csv())
            print(f"{a.csv}: every label, with what the reviewers did")
        if a.decisions_out:
            Path(a.decisions_out).write_text(json.dumps(s.decisions(), indent=1) + "\n")
            print(f"{a.decisions_out}: the decisions")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def cmd_taxa(a):
    from . import taxa
    if not (a.name or a.names or a.map or a.biigle):
        raise SystemExit("give names, --names files, --map or --biigle")
    taxa.build(names=a.name, files=a.names or [], label_map=Path(a.map) if a.map else None, biigle=a.biigle or [], out=Path(a.out))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mask-review", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"mask-review {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="serve a bundle for review")
    p.add_argument("bundle", type=Path)
    _serve_args(p)
    p.add_argument("--open", action="store_true", help="open the page in the browser")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("demo", help="build a synthetic bundle and open it")
    p.add_argument("--out", default="mask-review-demo", help="where to build it (default ./mask-review-demo)")
    p.add_argument("--rebuild", action="store_true", help="draw it again, dropping its review")
    p.add_argument("--no-open", action="store_true", help="do not open the browser")
    _serve_args(p)
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("build", help="make a bundle from a labelling job, a COCO file or a folder of pictures")
    p.add_argument("--job", help="a labelling job of the SAM 3 agent loop (manifest.json, final/, review/decisions/)")
    p.add_argument("--layer", default="full", help="--job: the layer before the verifier's cut (default full)")
    p.add_argument("--frames", nargs="*", help="--job: only these frames")
    p.add_argument("--coco", help="a COCO file (annotations with or without scores)")
    p.add_argument("--images", help="the folder of the pictures (for --coco), or pictures alone to label from scratch")
    p.add_argument("--score-key", default="score", help="--coco: the annotation field with the score (also read from attributes)")
    p.add_argument("--score-name", help="what the page calls the score (default: verifier vote for a job, score for COCO)")
    p.add_argument("--cut", type=float, help="the cut the bundle opens at (default 60 for a job, 50 for COCO)")
    p.add_argument("--taxa", help="a name list made by `mask-review taxa` (its --map gives the agents' labels a starting name)")
    p.add_argument("--names", choices=["labels", "map", "none"], default="labels",
                   help="starting names: labels (default) = the name list's map, else the agents' label as typed; map = the map only; none")
    p.add_argument("--max-width", type=int, default=0, help="scale wider pictures down to this width (default: full size)")
    p.add_argument("--video", action="store_true", help="--job: add the clips of frames cut from video (needs ffmpeg; off by default)")
    p.add_argument("--logs-name", help="what to call the frames' logs, when the frames carry them")
    p.add_argument("--title")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("export", help="write the review's COCO, CSV or decisions without a server")
    p.add_argument("bundle", type=Path)
    p.add_argument("--decisions", help="lay this decisions file over the review first (the bundle itself is not changed)")
    p.add_argument("--edits", help="the reviewers' work, when the review was served with --edits (default: BUNDLE/edits)")
    p.add_argument("--cut", type=float, help="the cut to export at (default: the review's)")
    p.add_argument("--coco", help="write the kept labels as COCO here")
    p.add_argument("--csv", help="write every label as CSV here")
    p.add_argument("--decisions-out", help="write the decisions file here")
    p.add_argument("--checked-only", action="store_true", help="COCO: only the frames marked right")
    p.add_argument("--polygons", action="store_true", help="COCO: outlines as polygons instead of exact RLE masks")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("taxa", help="make a name list from WoRMS (needs the internet)")
    p.add_argument("name", nargs="*", help="scientific names or AphiaIDs")
    p.add_argument("--names", nargs="*", help="files of names: .txt, .csv or .json")
    p.add_argument("--map", help="a .json map of the agents' labels to taxa: {\"brittle star\": \"Ophiuroidea\"}")
    p.add_argument("--biigle", nargs="*", help="BIIGLE label-tree exports (.json)")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_taxa)

    a = ap.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
