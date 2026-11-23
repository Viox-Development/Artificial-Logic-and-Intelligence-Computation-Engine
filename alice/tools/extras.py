"""Tools for the extras: notes, morning brief, weekly report, media and shutdown timer."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from alice.tools.base import COMPUTER, DANGER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class Notes(Tool):
    name = "notes"
    description = ("Saves, searches and lists the user's short notes ('take a note', 'note this down', 'what was in "
                   "my notes').\n"
                   "- Save what the user said as it is; don't summarize it or add comments.\n"
                   "- For lasting facts and preferences about the user use memory; for notes during a flight use "
                   "flight kneeboard_note.\n"
                   "- undo deletes only the last note added.")
    parameters = obj({
        "action": {"type": "string", "enum": ["add", "search", "list", "undo"],
                   "description": "add: save text | search: search by query and/or a date range | list: the last 20 "
                                  "notes | undo: delete the last note added."},
        "text": {"type": "string", "description": "For add: the note's text, as the user said it."},
        "query": {"type": "string", "description": "For search: words to look for."},
        "since": {"type": "string", "description": "Start: '7d', '2h', 'yesterday', 'today' or an ISO date."},
        "until": {"type": "string", "description": "End, same format as since. Empty: now."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ

    def run(self, ctx: ToolContext, action: str, text: str = "", query: str = "", since: str | None = None,
            until: str | None = None) -> ToolResult:
        from alice.core.store import parse_time
        from alice.extras import notes

        if action == "add":
            if not text.strip():
                raise ToolError("text gerekli")
            notes.add(text)
            return ToolResult("not alındı")
        if action == "undo":
            n = notes.remove_last()
            return ToolResult(f"silindi: {n.text}" if n else "silinecek not yok")
        try:
            t0, t1 = parse_time(since), parse_time(until)
        except ValueError as e:
            raise ToolError(str(e)) from e
        found = notes.search(query if action == "search" else "", t0, t1)
        if action == "list":
            found = found[-20:]
        return ToolResult("\n".join(n.line() for n in found[-50:]) or "not bulunamadı")


class Report(Tool):
    name = "report"
    description = ("Returns the data of ready-made summary reports.\n"
                   "- morning: the morning summary (weather, school/internship today, tasks and alerts from the "
                   "night, what's left from yesterday). Tell it to the user briefly and warmly; don't read the list "
                   "out as it is.\n"
                   "- pc: a computer report for the last days (temperatures, usage, events) and an HTML file; you can "
                   "show the file with send_file.")
    parameters = obj({
        "kind": {"type": "string", "enum": ["morning", "pc"], "description": "morning: morning summary | pc: PC "
                                                                             "report."},
        "days": {"type": "integer", "description": "For pc: how many days. Default 7."},
    }, ["kind"])
    risk = READ

    def run(self, ctx: ToolContext, kind: str, days: int = 7) -> ToolResult:
        from alice.core.schedule import TaskStore
        from alice.extras import brief, report
        from alice.tools.core import _store

        if kind == "morning":
            return ToolResult(brief.render(brief.gather(ctx.config.data.get("brief", {}), _store(ctx), TaskStore())))
        r = report.build(_store(ctx), days)
        return ToolResult(f"{report.text(r)}\nRapor: {report.save_html(r)}")


class Weather(Tool):
    name = "weather"
    description = ("Returns a city's current and today's weather: temperature, low/high and chance of rain.\n"
                   "- When asked about the weather, use this instead of web_search or web_fetch; it is faster and "
                   "exact.\n"
                   "- If no city is given, the city or location in the settings is used.\n"
                   "- Covers only today; if asked about later days, say so and don't make up a forecast.")
    parameters = obj({"city": {"type": "string", "description": "City, e.g. 'İzmir'. Empty: the configured city or "
                                                                "location."}})
    risk = READ

    def run(self, ctx: ToolContext, city: str = "") -> ToolResult:
        from alice.extras import brief

        return ToolResult(brief.weather(city or ctx.config.data.get("brief", {}).get("city", ""), auto=True))


class Media(Tool):
    name = "media"
    description = ("Controls the media playing (music, video) and says what is playing (now): Spotify, Sonora, "
                   "YouTube, whichever app. For \"what's playing\" use this first (the Spotify connection only sees "
                   "Spotify). System volume, timed pause, shutting the computer down. Not for OPENING a new song or "
                   "video (youtube or browser).")
    parameters = obj({
        "action": {"type": "string", "enum": ["now", "play_pause", "next", "previous", "stop", "mute", "volume_up",
                                              "volume_down", "set_volume", "pause_in", "shutdown_in",
                                              "cancel_shutdown"],
                   "description": "now: what's playing (song, artist, app) | play_pause: pause/resume | "
                                  "next/previous: next/previous track | mute: mute/unmute | volume_up/volume_down: "
                                  "times steps | set_volume: set to percent | pause_in: pause the music after minutes "
                                  "| shutdown_in: shut the computer down after minutes | cancel_shutdown."},
        "times": {"type": "integer", "description": "For volume_up/down: number of presses (each ~2%). Default 5."},
        "percent": {"type": "integer", "description": "For set_volume: 0-100."},
        "minutes": {"type": "number", "description": "For pause_in and shutdown_in: minutes."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return DANGER if args.get("action") == "shutdown_in" else COMPUTER

    def run(self, ctx: ToolContext, action: str, times: int = 5, percent: int | None = None,
            minutes: float | None = None) -> ToolResult:
        from alice.extras import media

        if action == "set_volume":
            if percent is None:
                raise ToolError("percent gerekli")
            return ToolResult(media.set_volume(percent))
        if action in ("pause_in", "shutdown_in"):
            if not minutes or minutes <= 0:
                raise ToolError("minutes gerekli")
            return ToolResult(media.pause_in(minutes) if action == "pause_in" else media.shutdown_in(minutes))
        if action == "cancel_shutdown":
            return ToolResult(media.cancel_shutdown())
        from alice.extras import nowplaying

        if action == "now":
            return ToolResult(nowplaying.describe(nowplaying.now()))
        if action in nowplaying.CONTROL and nowplaying.control(action):
            # Goes to the app actually playing (the media key used to hit the last-focused one); report, don't assume
            import time

            time.sleep(0.7)
            return ToolResult(f"tamam; şimdi: {nowplaying.describe(nowplaying.now())}")
        return ToolResult(media.key(action, times if action.startswith("volume") else 1))


YT_VIDEO = re.compile(r'"videoRenderer":\{"videoId":"([\w-]{11})".*?"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', re.S)


YT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0 Safari/537.36", "Accept-Language": "tr-TR,tr;q=0.9"}


_YT_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}


def youtube_search(query: str, limit: int = 5, client: httpx.Client | None = None) -> list[tuple[str, str]]:
    """YouTube search: [(title, video_id)]. Read from the ytInitialData in the page HTML (no API key needed).
    Results are cached for 10 min: YouTube may reorder results on every request, so the "first video" stays the same."""
    import time

    key = query.strip().lower()
    hit = _YT_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 600 and client is None:
        return hit[1][:limit]
    c = client or httpx.Client(timeout=8, follow_redirects=True, cookies={"CONSENT": "YES+1", "SOCS": "CAI"})
    try:
        r = c.get("https://www.youtube.com/results", params={"search_query": query, "hl": "tr", "gl": "TR"},
                  headers=YT_HEADERS)
    finally:
        if client is None:  # new client for every search: don't leave the socket open
            c.close()
    out: list[tuple[str, str]] = []
    for vid, title in YT_VIDEO.findall(r.text):
        if vid not in (v for _, v in out):
            try:
                title = json.loads(f'"{title}"')
            except ValueError:
                pass  # not an escaped JSON string: keep the raw title
            out.append((title, vid))
        if len(out) >= limit:
            break
    if out:
        _YT_CACHE[key] = (time.monotonic(), out)
    return out


def with_time(url: str, seconds: int) -> str:
    """Add or replace the start time in seconds on a YouTube URL (&t=120)."""
    url = re.sub(r"([?&])t=\d+s?&?", r"\1", url).rstrip("?&")
    return f"{url}{'&' if '?' in url else '?'}t={max(0, int(seconds))}"


class YouTube(Tool):
    name = "youtube"
    description = ("Finds a new video on YouTube and opens it in the user's own browser (in the open YouTube tab if "
                   "there is one), or seeks the open video to a minute.\n"
                   "- Use for: opening a video that isn't on screen ('open the X video', 'play the song Y'), 'go to "
                   "minute two'.\n"
                   "- Don't use for: a video/song already visible on screen (browser snapshot + click, or reflex), "
                   "opening a site (YouTube Music included: windows navigate or browser goto), pause/resume (media "
                   "play_pause).\n"
                   "- Write query the way the user said it; if what they want is unclear, ask first.")
    parameters = obj({
        "action": {"type": "string", "enum": ["search", "play", "seek"],
                   "description": "search: lists the first videos for query (opens nothing) | play: opens the "
                                  "index-th video for query, or url | seek: moves the open video to second at."},
        "query": {"type": "string", "description": "Search phrase, e.g. 'Coldplay Yellow'."},
        "url": {"type": "string", "description": "For play: a direct video address (instead of query)."},
        "index": {"type": "integer", "description": "For play: which result; 1 = the first. Default 1."},
        "at": {"type": "integer", "description": "Start second; e.g. minute 2 = 120."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "search" else COMPUTER

    def run(self, ctx: ToolContext, action: str, query: str | None = None, url: str | None = None,
            index: int = 1, at: int | None = None) -> ToolResult:
        from alice.tools.desktop import current_tab_url, navigate_tab

        if action == "seek":
            if at is None:
                raise ToolError("seek için at (saniye) gerekli")
            cur = url or current_tab_url()
            if "youtube.com/watch" not in cur and "youtu.be/" not in cur:
                raise ToolError(f"öndeki sekmede YouTube videosu yok ({cur[:80] or 'adres okunamadı'}); önce play")
            return ToolResult(navigate_tab(with_time(cur, at), new_tab=False))
        if action == "search" or (action == "play" and not url):
            if not query:
                raise ToolError("query gerekli")
            try:
                hits = youtube_search(query)
            except httpx.HTTPError as e:
                raise ToolError(f"YouTube'a ulaşılamadı: {e.__class__.__name__}") from e
            if not hits:
                return ToolResult("video bulunamadı")
            if action == "search":
                return ToolResult("\n".join(f"{i}. {t} — https://www.youtube.com/watch?v={v}"
                                            for i, (t, v) in enumerate(hits, 1)))
            title, vid = hits[max(1, min(int(index or 1), len(hits))) - 1]
            url = f"https://www.youtube.com/watch?v={vid}"
        else:
            title = url
        if at:
            url = with_time(url, at)
        return ToolResult(f"oynatılıyor: {title}. {navigate_tab(url)}")


TOOLS = [Notes(), Report(), Weather(), Media(), YouTube()]
