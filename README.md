# mask-review

A web page for checking the masks an annotation pipeline made. The person who knows the animals looks at every mask,
decides which ones stay, fixes or adds boxes, and names them with [WoRMS](https://www.marinespecies.org) taxa. The result
is a COCO file with exact masks, a CSV, and a decisions file that records what was decided.

Pipelines that give each mask a score (a verifier's vote, a detector's confidence) drop the masks under a cut. The right
cut depends on the use: a count that must not include look-alikes wants a high one, a first survey that should miss
nothing wants a low one. mask-review runs in two ways:

- **At a fixed cut** (the default). The masks under the cut are not shown. The reviewer goes through the rest: checks
  them, removes wrong ones, fixes boxes, adds missed animals and names them.
- **With the cut free** (`--tune`). The reviewer also sees the scores of all masks in a histogram, moves the cut, and
  looks at the masks scored just under and just over it, which is the quickest way to judge a cut.

What a reviewer decides is kept in both: a mask someone removed never stays, and a mask someone checked or named always
stays, whatever the cut.

It was written for the expert check of masks made by a SAM 3 agent loop (paper in review; citation to follow), but it
reads any COCO file, with or without scores, and pictures without any labels.

## Install

Python 3.9 or newer.

```bash
git clone https://github.com/Spiffical/mask-review
cd mask-review
pip install -e .
```

The server itself uses the standard library only; building a bundle needs numpy and OpenCV, which pip installs.

## Try it

```bash
mask-review demo            # a synthetic seabed at a fixed cut of 60; opens in the browser
mask-review demo --tune     # the same with the cut free
```

The demo draws its own pictures, so it needs no data and no network (naming uses a small built-in list of WoRMS taxa;
with the internet, the name search also asks WoRMS). It is built in `./mask-review-demo`; `--rebuild` draws it again and
drops its review.

## Review your own masks

First make a bundle: a folder with the pictures, the masks and their scores. Then serve it.

```bash
# any COCO file: masks as RLE or polygons, or boxes only; scores 0-1 or 0-100, in `score` or `attributes.score`
mask-review build --coco results.json --images photos/ --out review/ --score-name "detector score" --cut 50

# a labelling job of the SAM 3 agent loop: the masks before the verifier's cut, with its votes
mask-review build --job labeling/dive12 --out review/

# pictures without labels, to box and name from scratch
mask-review build --images photos/ --out review/

mask-review serve review/              # at the bundle's cut, fixed
mask-review serve review/ --cut 70     # at another fixed cut
mask-review serve review/ --tune       # with the cut free
```

The page opens at `http://127.0.0.1:8780/`. Each reviewer gives a name the first time (an email is optional; see
[Several reviewers](#several-reviewers)). Everything saves by itself, in `review/edits/`.

Build options worth knowing:

| Option | What it does |
| --- | --- |
| `--cut N` | the cut the bundle opens at, a whole number from 0 to 100 (default 60 for a job, 50 for COCO) |
| `--score-key KEY` | the COCO field that holds the score (default `score`) |
| `--score-name NAME` | what the page calls the score |
| `--taxa taxa.json` | a name list (see [Names](#names)); its map gives the agents' labels a starting WoRMS name |
| `--names labels\|map\|none` | starting names: the map, else the agents' label as typed (default); the map only; none |
| `--max-width PX` | smaller pictures (full size by default, because small animals need it) |
| `--video` | for frames cut from video: add their clips (needs ffmpeg; off by default) |

A bundle is never rebuilt over a review: build into a new folder and load the old review's decisions file into it.

## Going through the masks

There are three ways through them.

- **A frame at a time.** `S` goes through every animal in the frame, the unnamed first, then one name after the other.
  Enter says the label is right, a number key or a typed name changes it, Delete removes it, the arrow keys move on
  without deciding. After the last one, Enter marks the frame right and opens the next.
- **A name at a time.** `T` shows tiles of every animal with one name, from all frames. A click opens the animal in its
  frame with its name box; Enter goes on to the next tile and Esc returns to the same place in the tiles. Tick tiles
  (Shift for a range) to name, remove or check many at once.
- **The cut** (with `--tune`). `C` shows the scores of all masks as a histogram and the masks scored just under and at
  or just over the cut, side by side. Drag in the histogram, use the slider, or press `J` / `K` (with Shift: 5) to move
  the cut. If the masks just under the cut are animals, lower it; if those over it are not, raise it. ✓ on a tile keeps
  that mask whatever the cut, ✕ removes it, and a click opens it in its frame (Esc comes back).

| To | Do |
| --- | --- |
| Remove a wrong label | Point at it and click its ✕, or press `X`, or Delete when it is selected |
| Keep a label under the cut | Point at it and click ✓ (with `--tune`, when labels under the cut are shown) |
| Put a removed label back | `R` shows removed labels, crossed out; click ↺ on one, or press `X` |
| Rename | Click the animal, type a common or scientific name, pick the taxon (Enter takes the first) |
| Use a name again and again | Star it (☆). Starred names get the number keys 1 to 9 |
| Fix a box | Click the animal, drag a corner or an edge; drag inside the box to move it |
| Add a missing animal | `B`, drag a box, name it |
| Mark doubt | "Not sure of the name", or a note |
| See better | Wheel to zoom, drag to pan, `L` brightens, `H` hides the labels, `N` writes every name (with its score, when the cut can move) |
| Show labels under the cut | `D`, dashed (with `--tune`) |
| Undo | `Z`, as far back as needed |

## What stays

A label a reviewer removed never stays. A label a reviewer added, checked or named always stays. Every other label goes
by its score: it stays when its score is at or above the cut, or when it has no score. A label is checked when a
reviewer said it is right, or when its frame was marked right while the label was shown (its score at or above the cut
of that moment), so lowering the cut later never makes masks nobody saw look checked.

## Take the result away

From the page's Export menu, or without a server with `mask-review export`:

- **COCO, all frames** or **checked frames only**: the labels that stay, one category per name with its AphiaID and
  rank. A frame counts as checked when it was marked right and none of the labels it keeps went unseen (a cut lowered
  later can bring in labels nobody looked at; such a frame shows a yellow count in the frame strip until it is checked
  again). Masks are exact (COCO RLE, as the pipeline made them); `seg=polygon` (or `export --polygons`) gives outlines as
  polygons instead. A box a reviewer moved or drew has its box only.
- **CSV**: every label with its status (kept, under the cut, removed), score, name, AphiaID, box, what was changed and
  who changed it.
- **Decisions**: everything the reviewers decided (the cut, and each label they changed, with the box it started with).
  Load it into another copy of the same bundle with **Load decisions…** in the Export menu, or apply it without a
  server:

```bash
mask-review export review/ --coco kept.json --csv labels.csv
mask-review export review/ --decisions decisions.json --coco kept.json   # a saved review laid over the bundle
mask-review export review/ --edits /elsewhere/edits --coco kept.json     # a review served with --edits
```

A decision counts only on the same mask: the same frame, label number and starting box, and a frame marked right is
carried over only onto a frame with the same masks. Decisions files of the earlier static review page (a cut and
rejected masks) load too.

## Several reviewers

Several people can work on one bundle at once. Run the server where they can all reach it:

```bash
mask-review serve review/ --bind 0.0.0.0 --port 8780
```

Changes show up in every open page within about a second; initials at the top say who is where, and a dotted box marks
the animal someone else has open. Labels are saved one by one, so two people collide only when they change the same
animal at the same moment, and the second is then shown the first one's version. Every save is also written to
`edits/history.jsonl` with who made it.

There is no password: anyone who can open the page can edit, and the name a reviewer gives is taken as given. Keep the
server on a private network. An email, when a reviewer gives one, brings their stars back on another computer; it stays
in `edits/users.json` on the server and never appears in the page, the exports or the history.

`--edits DIR` keeps the reviewers' work outside the bundle, for a bundle on a read-only drive. See
[docs/team-server.md](docs/team-server.md) for keeping a server running and backing up the work.

## Names

The name search looks in the bundle's name list first and then asks WoRMS itself, from the reviewer's browser. An old
name is stored under its accepted one. A name WoRMS does not hold, such as a morphotype, can be kept as typed; it then
has no AphiaID.

A name list is optional. It makes the names in it searchable offline and lets the tiles show what lies under a name
("Include lower taxa"). Make one with WoRMS (needs the internet; answers are cached):

```bash
mask-review taxa Ophiuroidea Asteroidea 123084 --names site_species.txt --map labels.json --out taxa.json
```

`--names` reads .txt (one name per line), .csv (a `name` or `scientificName` column, and optionally `aphia_id` or
`AphiaID`) and .json files; `--biigle` reads BIIGLE label-tree exports. `--map` is a JSON map from the agents' labels to
taxa (`{"brittle star": "Ophiuroidea"}`): with `build --taxa taxa.json`, each label starts with its taxon.

## Video

Most reviews are of still pictures, and video is off unless a bundle is built with it. For frames cut from video,
`build --job ... --video` re-encodes each frame's clip (ffmpeg); the page can then play it from the labelled moment over
the frame (`V`), step through it, and make a new frame from another moment (`F`).

## Bundles from other pipelines

A bundle can be written from Python, which is how a pipeline adds what the page shows beside a frame:

```python
from mask_review import bundle

frames = [dict(id="dive12_0001", image_path="frames/0001.jpg",
               labels=[dict(rle=mask_rle, score=87, label="sea star", why="five arms, clear edge"),
                       dict(box=[120, 40, 180, 95], score=31, label="sponge")],
               time="2024-05-01 10:12:03", logs=[dict(name="Asteroidea", taxon=None, dt=-4, n=1, comment="")])]
bundle.write(frames, "review/", title="Dive 12", score_name="verifier vote", cut=60, logs_name="Dive log")
```

See [docs/formats.md](docs/formats.md) for the bundle, the decisions file and the exports.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests build small bundles, run the server, and check that the page's rule for what stays gives the server's answers
(that one needs Node). Run `mask-review demo` to see a change.

## Acknowledgements

Taxon names and classifications come from the World Register of Marine Species: WoRMS Editorial Board (2026). World
Register of Marine Species. https://www.marinespecies.org, doi:10.14284/170.

## Licence

MIT. Check the licence of your pictures before you share a bundle: it holds them.
