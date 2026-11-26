# Running a review for a team

One machine serves the bundle; reviewers open the page in their browsers. The machine needs Python 3.10 or newer and the
bundle; serving needs nothing else. Building a bundle needs numpy and OpenCV, so build it anywhere and copy it; on a
machine that only serves, `pip install --no-deps` skips them.

```bash
pip install git+https://github.com/Spiffical/mask-review
rsync -a review/ server:reviews/dive12/          # the bundle, built elsewhere
ssh server
mask-review serve reviews/dive12 --bind 0.0.0.0 --port 8780            # a fixed cut
mask-review serve reviews/dive12 --bind 0.0.0.0 --port 8780 --tune     # or with the cut free
```

There is no password: anyone who can reach the port can open the page and edit. Serve it only on a private network (a
VPN or a lab network), or behind a reverse proxy that asks for one.

## Keep it running

Under `screen` or `tmux`, or as a systemd service:

```ini
# /etc/systemd/system/mask-review-dive12.service
[Unit]
Description=mask-review: dive 12
After=network.target

[Service]
User=reviews
ExecStart=/usr/local/bin/mask-review serve /srv/reviews/dive12 --bind 0.0.0.0 --port 8780
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## The reviewers' work

Everything the reviewers do is in the bundle's `edits/` folder (or the folder given with `--edits`): one file per
frame they changed, `history.jsonl` with every save, `users.json` with their names and the emails they gave, and
`cut.json` when the cut was moved. Back up that folder; the rest of the bundle never changes. To hand the work on
without the emails, use the decisions file (Export, Decisions), or `mask-review export`.

## Updating the tool

Stop the server, `pip install -U git+https://github.com/Spiffical/mask-review`, start it again. Open pages say that the
page was updated and offer to load the new one; nothing they had not saved yet is lost, because a page that loses the
server keeps its changes and sends them when it is back.
