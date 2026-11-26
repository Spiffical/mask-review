"""The web server: one bundle, its review, and the page. Standard library only.

    mask-review serve BUNDLE [--cut N | --tune] [--port 8780] [--bind 127.0.0.1] [--edits DIR]

The bundle's frames are never changed. The reviewers' version of a frame is edits/<frame>.json, and every save is also
appended to edits/history.jsonl with who sent it, so nothing is lost. Several people can work at once: labels are saved
one by one, each with its own revision, and every page asks /api/sync for the others' changes about once a second.
People have a profile (a name, and an email if they give one, in edits/users.json); an email never leaves that file.
"""
from __future__ import annotations

import gzip
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store import ID, Refused, Store, Unknown

MAX_BODY = 4 * 1024 * 1024
MAX_BIG = 32 * 1024 * 1024  # a new frame's picture, a decisions file
FRAME = re.compile(rf"/api/frame/({ID})")
FILE = re.compile(rf"/(img|thumb)/({ID})\.jpg")
CROP = re.compile(rf"/crop/({ID})/(m\d{{1,7}})\.jpg")
VIDEO = re.compile(rf"/video/({ID})\.mp4")
RANGE = re.compile(r"bytes=(\d*)-(\d*)")
DROP = re.compile(rf"/api/new/({ID})")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mask-review"
    sys_version = ""
    timeout = 60

    @property
    def store(self) -> Store:
        return self.server.store

    def log_message(self, form, *args):
        if self.path != "/api/sync" and not getattr(self.server, "quiet", False):
            super().log_message(form, *args)

    def reply(self, code, data, kind="application/json", cache="no-store", name=None):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        pack = kind != "image/jpeg" and len(data) > 20000 and "gzip" in self.headers.get("Accept-Encoding", "")
        if pack:
            data = gzip.compress(data, 5)
        self.send_response(code)
        self.send_header("Content-Type", kind)
        if pack:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if name:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        self.wfile.write(data)

    def clip(self, path):
        """A video, whole or by byte range."""
        size = path.stat().st_size
        start, end = 0, size - 1
        asked = RANGE.fullmatch(self.headers.get("Range", "").strip())
        if asked and (asked[1] or asked[2]):
            start, end = (int(asked[1]), min(int(asked[2] or end), end)) if asked[1] else (max(size - int(asked[2]), 0), end)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if asked else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if asked:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                part = f.read(min(left, 256 * 1024))
                if not part:
                    break
                try:
                    self.wfile.write(part)
                except (BrokenPipeError, ConnectionResetError):  # the player jumped elsewhere and dropped this request
                    self.close_connection = True
                    return
                left -= len(part)

    def do_GET(self):
        url = urlsplit(self.path)
        path, q = url.path, parse_qs(url.query)
        s = self.store
        fr, file, crop, video = FRAME.fullmatch(path), FILE.fullmatch(path), CROP.fullmatch(path), VIDEO.fullmatch(path)
        stem = s.index.get("title") or "labels"
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:60] or "labels"
        if path in ("/", "/index.html"):
            self.reply(200, s.page_path.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/all":
            self.reply(200, s.everything())
        elif path == "/taxa.json":
            self.reply(200, s.taxa_path.read_bytes() if s.taxa_path.exists() else b'{"taxa": [], "above": {}, "classes": {}}')
        elif path in ("/export/labels.csv", "/labels.csv"):
            self.reply(200, s.export_csv(), "text/csv; charset=utf-8", name=f"{stem}_labels.csv")
        elif path in ("/export/labels.coco.json", "/labels.coco.json"):
            checked = q.get("frames", ["checked" if path == "/labels.coco.json" else "all"])[0] == "checked"
            poly = q.get("seg", ["rle"])[0] == "polygon"
            self.reply(200, s.export_coco(checked_only=checked, polygons=poly),
                       name=f"{stem}_{'checked' if checked else 'all'}{'_polygons' if poly else ''}.coco.json")
        elif path == "/export/decisions.json":
            self.reply(200, json.dumps(s.decisions(), indent=1).encode(), name=f"{stem}_decisions.json")
        elif fr and s.exists(fr[1]):
            self.reply(200, s.merged(fr[1]))
        elif (file and file[2] in s.ids or crop and crop[1] in s.ids) and (s.root / path[1:]).exists():
            self.reply(200, (s.root / path[1:]).read_bytes(), "image/jpeg", "max-age=86400")
        elif file and (s.new / f"{file[2]}.json").exists():
            self.reply(200, (s.new / (file[2] + ("_t" if file[1] == "thumb" else "") + ".jpg")).read_bytes(), "image/jpeg", "max-age=86400")
        elif video and video[1] in s.videos:
            self.clip(s.root / path[1:])
        else:
            self.reply(404, dict(error="not found"))

    def do_POST(self):
        here, s = self.client_address[0], self.store
        acts = {"/api/save": lambda b: s.save(b, here), "/api/sync": s.sync, "/api/bye": s.bye, "/api/profile": s.set_profile,
                "/api/fav": s.set_fav, "/api/hello": lambda b: s.mine(b["uid"], s.person(b["uid"])),
                "/api/new": lambda b: s.placed(s.add_frame(b, here)), "/api/cut": lambda b: s.set_cut(b, here),
                "/api/decisions": lambda b: s.load_decisions(b, here)}
        act = acts.get(self.path)
        drop = DROP.fullmatch(self.path)
        if drop and (s.new / f"{drop[1]}.json").exists():
            act = lambda b: s.drop_frame(drop[1], bool(b.get("removed")), b, here) or s.placed(drop[1])  # noqa: E731
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        big = self.path in ("/api/new", "/api/decisions")
        if not act or not 0 < length <= (MAX_BIG if big else MAX_BODY):
            self.close_connection = True
            self.reply(400, dict(error="bad request"))
            return
        try:
            self.reply(200, act(json.loads(self.rfile.read(length))))
        except Unknown:
            self.reply(401, dict(error="no such user"))
        except Refused as err:
            self.reply(403, dict(error=str(err)))
        except (ValueError, AttributeError, TypeError, KeyError) as err:
            self.reply(400, dict(error=str(err)))


def make(root, mode="fixed", cut=None, port=8780, bind="127.0.0.1", quiet=False, edits=None):
    """A server for a bundle, not started yet."""
    store = Store(root, mode=mode, cut=cut, edits=edits)
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.store = store
    server.quiet = quiet
    try:
        store.log(serve=dict(mode=mode, cut=store.cut, port=server.server_port))
    except OSError as e:
        server.server_close()
        raise SystemExit(f"cannot write the review to {store.edits} ({e.strerror}). Give --edits DIR, a folder you can write to.")
    return server


def serve(root, mode="fixed", cut=None, port=8780, bind="127.0.0.1", open_browser=False, port_given=False, edits=None, log=print):
    """Serve a bundle until Ctrl+C. When the port is taken and was not asked for, the next free one is used."""
    for p in range(port, port + (1 if port_given else 20)):
        try:
            server = make(root, mode, cut, p, bind, edits=edits)
            break
        except OSError as e:
            if port_given or p == port + 19:
                raise SystemExit(f"port {p}: {e.strerror}")
    s = server.store
    host = "127.0.0.1" if bind in ("0.0.0.0", "::", "") else bind
    url = f"http://{host}:{server.server_port}/"
    n = sum(len(s.base(f["id"])["labels"]) for f in s.index["frames"])
    how = ("no scores, so nothing is cut" if s.cut is None else
           f"cut {s.cut} on the {s.index.get('score') or 'score'}, " + ("which reviewers can move" if s.mode == "tune" else "fixed"))
    log(f"{s.index.get('title', 'Mask review')}: {len(s.index['frames'])} frames, {n} labels; {how}.")
    log(f"Open {url}  (Ctrl+C stops it; the reviewers' work is in {s.edits})")
    if bind in ("0.0.0.0", "::"):
        log("Anyone who can reach this machine on that port can open and edit the review: there is no password.")
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        server.server_close()
