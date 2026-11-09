"""Local labeling UI: `python -m evals.baseline label` serves it on
http://127.0.0.1:8714 (loopback only; messages never leave the machine).

For each episode it shows exactly what v0.14's detector saw for that chat on
that day: the day's new messages plus the replayed context and date anchors
the reader prepended, and v0.14's detections with what the pipeline did.
"""

import json
import sqlite3
import webbrowser
from datetime import date, datetime, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scheduling_agent import config, reader

from evals.baseline import contacts, episodes, labels, paths

PORT = 8714
UI = Path(__file__).with_name("label_ui.html")


class Store:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.queue = episodes.read(paths.EPISODES)
        self.by_id = {e.id: e for e in self.queue}
        self.names = contacts.load()
        self.chat_names = _chat_names()

    def labels(self) -> dict[str, labels.EpisodeLabel]:
        return labels.load(paths.LABELS)

    def queue_view(self) -> dict[str, object]:
        done = self.labels()
        return {
            "items": [
                {"id": e.id, "stratum": e.stratum, "day": e.day,
                 "chat": self.chat_title(e.chat_id), "n_detections": len(e.detections),
                 "labeled": e.id in done}
                for e in self.queue
            ],
            "sizes": episodes.stratum_sizes(self.queue),
        }

    def chat_title(self, chat_id: int) -> str:
        display, handles = self.chat_names.get(chat_id, ("", []))
        if display:
            return display
        people = [contacts.name_for(h, self.names) for h in handles]
        return ", ".join(people[:4]) + (f" +{len(people) - 4}" if len(people) > 4 else "") or f"chat {chat_id}"

    def episode_view(self, eid: str) -> dict[str, object]:
        ep = self.by_id[eid]
        msgs = []
        for m in _thread_for(ep.chat_id, ep.day, tuple(self.cfg["blocked_contacts"]),
                             int(self.cfg["date_context_lookback_days"]),
                             int(self.cfg["date_context_max_messages"])):
            msgs.append({
                "who": contacts.name_for(str(m["sender"]), self.names),
                "from_me": bool(m["from_me"]),
                "text": m["text"],
                "time": datetime.fromtimestamp(float(m["unix_ts"])).strftime("%a %b %-d, %-I:%M %p"),
                "context": bool(m.get("is_context")),
            })
        lab = self.labels().get(eid)
        return {
            "episode": {"id": ep.id, "day": ep.day, "stratum": ep.stratum,
                        "weekday": date.fromisoformat(ep.day).strftime("%A"),
                        "detections": ep.detections},
            "chat": self.chat_title(ep.chat_id),
            "messages": msgs,
            "label": None if lab is None else {
                "detections": [d.__dict__ for d in lab.detections],
                "missed": [m.__dict__ for m in lab.missed], "note": lab.note,
            },
        }


def _chat_names() -> dict[int, tuple[str, list[str]]]:
    conn = sqlite3.connect(f"file:{reader.CHAT_DB}?mode=ro", uri=True, timeout=5)
    try:
        out: dict[int, tuple[str, list[str]]] = {}
        for rowid, display in conn.execute("SELECT ROWID, COALESCE(display_name, '') FROM chat"):
            out[rowid] = (display, [])
        for chat_id, handle in conn.execute(
            "SELECT chj.chat_id, h.id FROM chat_handle_join chj JOIN handle h ON chj.handle_id = h.ROWID"
        ):
            if chat_id in out:
                out[chat_id][1].append(handle)
        return out
    finally:
        conn.close()


@lru_cache(maxsize=64)
def _threads_for_day(day: str, blocked: tuple[str, ...], anchor_days: int, anchor_max: int) -> dict[int, list[dict[str, object]]]:
    """What the backfill's reader call returned for that day's window."""
    start = datetime.combine(date.fromisoformat(day), datetime.min.time())
    end = start + timedelta(days=1)
    threads = reader.get_threads_since(
        last_apple_ts=reader.unix_to_apple(start.timestamp()),
        lookback_days=1, blocked=list(blocked),
        date_context_lookback_days=anchor_days, date_context_max=anchor_max,
        end_apple_ts=reader.unix_to_apple(end.timestamp()),
    )
    return {int(t["chat_id"]): list(t["messages"]) for t in threads}


def _thread_for(chat_id: int, day: str, blocked: tuple[str, ...], anchor_days: int, anchor_max: int) -> list[dict[str, object]]:
    return _threads_for_day(day, blocked, anchor_days, anchor_max).get(chat_id, [])


def make_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # quiet
            return

        def _json(self, code: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/":
                data = UI.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)
            elif url.path == "/api/queue":
                self._json(200, store.queue_view())
            elif url.path == "/api/episode":
                eid = parse_qs(url.query).get("id", [""])[0]
                if eid not in store.by_id:
                    self._json(404, {"error": "unknown episode"})
                else:
                    self._json(200, store.episode_view(eid))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/label":
                self._json(404, {"error": "not found"})
                return
            n = int(self.headers.get("Content-Length", "0"))
            try:
                raw = json.loads(self.rfile.read(n))
                if not isinstance(raw, dict):
                    raise labels.LabelError("body must be an object")
                lab = labels.parse(raw)
                if lab.episode_id not in store.by_id:
                    raise labels.LabelError("unknown episode")
                labels.append(lab, paths.LABELS)
                self._json(200, {"ok": True})
            except (labels.LabelError, json.JSONDecodeError) as e:
                self._json(400, {"error": str(e)})

    return Handler


def serve(open_browser: bool = True) -> None:
    if not paths.EPISODES.exists():
        raise SystemExit("No episodes yet. Run `python -m evals.baseline run` first.")
    store = Store()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(store))
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Labeling UI at {url}  (Ctrl-C to stop; progress is saved after every episode)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
