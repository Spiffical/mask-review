"""A synthetic seabed to try the tool on: no data, no GPU, no network.

Bright blobs (brittle stars, sponges, anemones, soft corals) get high scores; pale lumps, stones and shell shards get
low ones and no name. The pictures are drawn here, so they carry no data licence. The name list beside it holds a dozen
real WoRMS taxa, so naming and "Include lower taxa" work offline.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import bundle

KINDS = [  # agents' label, shape, colour (BGR), score range, size range, reason
    ("brittle star", "star", (150, 150, 235), (82, 99), (22, 44), "five arms and a central disc, outline fits"),
    ("sponge", "blob", (120, 220, 240), (70, 96), (18, 40), "rounded yellow body with a clear edge"),
    ("anemone", "blob", (90, 140, 245), (64, 95), (16, 34), "orange disc with a darker centre"),
    ("soft coral", "blob", (200, 170, 225), (48, 86), (14, 30), "pale lobed colony, partly in shadow"),
    (None, "blob", (150, 150, 140), (22, 62), (10, 24), "pale lump, could be a small sponge or a pebble"),
    (None, "rock", (110, 112, 104), (3, 34), (14, 36), "grey angular object with a hard shadow, likely a stone"),
    (None, "rock", (205, 210, 208), (5, 40), (8, 18), "flat white shard, likely shell"),
]
WEIGHTS = np.array([0.16, 0.2, 0.14, 0.12, 0.16, 0.14, 0.08])


def frames(n_frames: int = 4, seed: int = 7):
    import cv2
    rng = np.random.default_rng(seed)
    for k in range(n_frames):
        w, h = 1800, 1200
        base = rng.normal(0, 1, (h // 8, w // 8)).astype(np.float32)
        base = cv2.resize(cv2.GaussianBlur(base, (0, 0), 3), (w, h), interpolation=cv2.INTER_CUBIC)
        fine = cv2.GaussianBlur(rng.normal(0, 1, (h, w)).astype(np.float32), (0, 0), 1.2)
        g = 46 + 60 * base + 9 * fine
        img = np.clip(np.dstack([g * 1.12 + 10, g * 1.02 + 6, g * 0.86]), 0, 255).astype(np.uint8)
        labels, taken = [], np.zeros((h, w), bool)
        for _ in range(int(rng.integers(26, 44))):
            name, shape, col, (v0, v1), (s0, s1), why = KINDS[int(rng.choice(len(KINDS), p=WEIGHTS))]
            r = int(rng.integers(s0, s1))
            cx, cy = int(rng.integers(r + 6, w - r - 6)), int(rng.integers(r + 6, h - r - 6))
            m = np.zeros((h, w), np.uint8)
            if shape == "star":
                cv2.circle(m, (cx, cy), max(4, r // 4), 1, -1)
                a0 = rng.uniform(0, 6.28)
                for j in range(5):
                    a = a0 + j * 6.2832 / 5 + rng.normal(0, 0.12)
                    cv2.line(m, (cx, cy), (int(cx + r * np.cos(a)), int(cy + r * np.sin(a))), 1, max(2, r // 9))
            elif shape == "rock":
                pts = np.array([[cx + r * np.cos(a) * rng.uniform(0.55, 1), cy + r * np.sin(a) * rng.uniform(0.55, 1)]
                                for a in np.sort(rng.uniform(0, 6.2832, int(rng.integers(5, 8))))], np.int32)
                cv2.fillPoly(m, [pts], 1)
            else:
                cv2.ellipse(m, (cx, cy), (r, int(r * rng.uniform(0.6, 1))), float(rng.uniform(0, 180)), 0, 360, 1, -1)
            m = m.astype(bool)
            if (m & taken).any():
                continue
            taken |= cv2.dilate(m.astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool)
            shade = np.clip(rng.normal(1.0, 0.12), 0.7, 1.3)
            soft = cv2.GaussianBlur(m.astype(np.float32), (0, 0), 1.6)[..., None]
            img = np.clip(img.astype(np.float32) * (1 - soft * 0.9) + np.array(col, np.float32) * shade * soft * 0.9, 0, 255).astype(np.uint8)
            score = int(np.clip(rng.integers(v0, v1 + 1) + rng.normal(0, 4), 0, 100))
            labels.append(dict(mask=m, label=name, origin="click" if rng.random() < 0.35 else "sam3", score=score, why=why))
        img = cv2.GaussianBlur(img, (0, 0), 0.8)
        yield dict(id=f"demo_{k + 1:02d}", image=img, file_name=f"demo_{k + 1:02d}.jpg", labels=labels)


def taxa() -> dict:
    return json.loads((Path(__file__).parent / "data" / "demo_taxa.json").read_text())


def build(out: Path, n_frames: int = 4, log=print) -> dict:
    return bundle.write(frames(n_frames), out, title="Synthetic seabed (demo)", score_name="agent score", cut=60, taxa=taxa(),
                        start_names="map", source=dict(kind="demo", note="synthetic pictures drawn by mask_review.demo"), log=log)
