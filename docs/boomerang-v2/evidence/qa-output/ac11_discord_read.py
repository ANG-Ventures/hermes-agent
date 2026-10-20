"""AC-11 independent oracle (Argus, t_df1e3b88): READ-ONLY Discord GET of the origin channel.

Counts every message that names t_1b09baf2 from 00:03:00 to 00:30:00 PDT 2026-10-20, so "one warning
line" and "no late [auto-handled]" are counted, not first-match. The token is never printed.
"""
import datetime as dt
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

CHANNEL = "1561469136796782633"
CARD = "t_1b09baf2"
TZ = dt.timezone(dt.timedelta(hours=-7))
start = dt.datetime(2026, 10, 20, 0, 3, 0, tzinfo=TZ).timestamp()
end = dt.datetime(2026, 10, 20, 0, 30, 0, tzinfo=TZ).timestamp()


def token() -> str:
    for raw in pathlib.Path.home().joinpath(".hermes/.env").read_text().splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("no DISCORD_BOT_TOKEN")


after = str((int(start * 1000) - 1420070400000) << 22)
msgs, tok = [], token()
for _ in range(10):
    url = f"https://discord.com/api/v10/channels/{CHANNEL}/messages?" + urllib.parse.urlencode({"after": after, "limit": 100})
    req = urllib.request.Request(url, headers={"Authorization": f"Bot {tok}", "User-Agent": "argus-qa (read-only)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        page = json.loads(r.read())
    if not page:
        break
    msgs += page
    after = max(page, key=lambda d: int(d["id"]))["id"]
    if len(page) < 100:
        break

rows = []
for d in msgs:
    ts = dt.datetime.fromisoformat(d["timestamp"]).timestamp()
    if ts > end:
        continue
    text = (d.get("content") or "") + "".join("\n" + (e.get("title") or "") + "\n" + (e.get("description") or "") for e in d.get("embeds") or [])
    if CARD in text:
        rows.append({"ts": dt.datetime.fromtimestamp(ts, TZ).isoformat(), "author_bot": bool(d["author"].get("bot")),
                     "text": text[:240]})
rows.sort(key=lambda r: r["ts"])
lost = [r for r in rows if re.search(r"auto-handle lost\s+" + CARD, r["text"])]
handled = [r for r in rows if re.search(r"\[auto-handled\]\s*" + CARD, r["text"])]
print(json.dumps({"window": ["00:03:00", "00:30:00"], "messages_scanned": len(msgs), "naming_card": rows,
                  "lost_lines": len(lost), "auto_handled_lines": len(handled)}, indent=1, ensure_ascii=False))
