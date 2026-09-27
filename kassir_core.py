# language: Python 3.10+
# file: kassir_core.py
# ядро: парсинг Kassir.kg + API + watcher. Без Telegram — чистый сетевой слой.

import json
import re
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable
from curl_cffi import requests as creq


GATEWAY = "https://gateway.kassir.kg"
WWW = "https://www.kassir.kg"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": WWW,
    "Referer": f"{WWW}/",
}


# ---------------------------------------------------------------- парсинг

def parse_next_data(html: str) -> dict:
    """Вытащить RSC-пейлоад Next.js и вернуть объект item (сеанс + схема зала)."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', html)
    rsc = "".join(json.loads(f'"{c}"') for c in chunks)

    m = re.search(
        r'"item":\{"date_time":.*?"slug":"[^"]+","svg_type":"[^"]+",'
        r'"theater":\{.*?\},"ticket_types":\[\]\}',
        rsc, re.S
    )
    if not m:
        raise RuntimeError("не нашёл item в __NEXT_DATA__")
    item = json.loads("{" + m.group(0) + "}")
    return item["item"]


def parse_svg_map(html: str) -> dict:
    """html_id -> {sector, row, seat}"""
    out = {}
    for m in re.finditer(
        r'<g id="(seat_\d+)"\s+data-sector="([^"]+)"\s+data-row="(\d+)"\s+data-seat="(\d+)"',
        html
    ):
        html_id, sector, row, seat = m.groups()
        out[html_id] = {"sector": sector, "row": int(row), "seat": int(seat)}
    return out


def occupied_svg_ids(order_items: list) -> set:
    """html_id занятых/забронированных мест из GET /v1/session/order/item."""
    return {oi["html_id"] for oi in order_items if oi.get("html_id")}


def free_svg_ids(all_seats: list, occupied: set) -> set:
    """Свободные = все места зала минус занятые."""
    return {s["html_id"] for s in all_seats if s["html_id"] not in occupied}


# ---------------------------------------------------------------- API-клиент

@dataclass
class KassirClient:
    user_uuid: str

    def get_order_items(self, slug: str) -> list:
        """GET /v1/session/order/item — список ЗАНЯТЫХ мест для сеанса."""
        r = creq.get(
            f"{GATEWAY}/v1/session/order/item",
            params={"slug": slug, "user_uuid": self.user_uuid},
            headers=HEADERS, impersonate="chrome", timeout=20,
        )
        r.raise_for_status()
        return r.json().get("payload", {}).get("order_items", [])

    def get_basket(self) -> dict:
        """GET /v1/basket — текущая корзина + таймер (в секундах)."""
        r = creq.get(
            f"{GATEWAY}/v1/basket",
            params={"user_uuid": self.user_uuid},
            headers=HEADERS, impersonate="chrome", timeout=20,
        )
        r.raise_for_status()
        return r.json().get("payload", {}) or {}

    def add_to_cart(self, event_id: int, session_id: int, ticket_seat_id: int) -> dict:
        """POST /v1/order/item — забронировать место."""
        body = {
            "event_id": event_id,
            "session_id": session_id,
            "ticket_seat_id": ticket_seat_id,
            "user_uuid": self.user_uuid,
        }
        r = creq.post(
            f"{GATEWAY}/v1/order/item",
            json=body, headers=HEADERS, impersonate="chrome", timeout=20,
        )
        raw = r.text or ""
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw[:400]}
        parsed["_http"] = r.status_code
        return parsed


# ---------------------------------------------------------------- сессии/схема

def fetch_session(slug: str) -> dict:
    """GET страницы сеанса → item (сеанс + схема зала)."""
    r = creq.get(f"{WWW}/ru/session/{slug}/order",
                 headers=HEADERS, impersonate="chrome", timeout=30)
    r.raise_for_status()
    return parse_next_data(r.text)


def fetch_svg_map(slug: str) -> dict:
    """html_id → {sector, row, seat}."""
    r = creq.get(f"{WWW}/ru/session/{slug}/order",
                 headers=HEADERS, impersonate="chrome", timeout=30)
    r.raise_for_status()
    return parse_svg_map(r.text)


def list_sessions_for_event(event_slug: str) -> list:
    """Из одной страницы сеанса достаём все даты этого мероприятия."""
    item = fetch_session(event_slug)
    sessions = [{
        "slug": item["slug"],
        "date_time": item["date_time"],
        "session_id": item["id"],
        "event_id": item["event"]["id"],
        "title": item["event"]["title"],
    }]
    for o in item.get("other_sessions", []) or []:
        sessions.append({
            "slug": o["slug"],
            "date_time": o["date_time"],
            "session_id": o["id"],
            "event_id": item["event"]["id"],
            "title": item["event"]["title"],
        })
    return sessions


# ---------------------------------------------------------------- watcher

@dataclass
class Target:
    slug: str
    event_id: int
    session_id: int
    title: str
    date_time: str
    ticket_seat_ids: list = field(default_factory=list)
    svg_ids: list = field(default_factory=list)
    svg_map: dict = field(default_factory=dict)


class Watcher:
    """Держит места в корзине и восстанавливает, если слетели."""

    def __init__(self, client: KassirClient, target: Target,
                 on_event: Optional[Callable[[str], None]] = None,
                 interval: float = 4.0):
        self.client = client
        self.target = target
        self.on_event = on_event or (lambda s: None)
        self.interval = interval
        self.stop_flag = threading.Event()
        self.thread: Optional[threading.Thread] = None

    # -- бронирование --

    def _book_once(self, tid: int) -> dict:
        # небольшая пауза чтобы не флудить
        time.sleep(0.4)
        return self.client.add_to_cart(self.target.event_id,
                                       self.target.session_id, tid)

    def book_all(self) -> dict:
        """Пробует всё. Возвращает {'ok': n, 'fail': n, 'errors': [...]}"""
        ok, fail, errors = 0, 0, []
        for sid, tid in zip(self.target.svg_ids, self.target.ticket_seat_ids):
            if self.stop_flag.is_set():
                break
            res = self._book_once(tid)
            info = self.target.svg_map.get(sid, {})
            title = f"{info.get('sector','?')} ряд {info.get('row','?')} место {info.get('seat','?')}"
            if res.get("status") == "success":
                ok += 1
                self.on_event(f"✓ {title}  booking_id={res.get('payload',{}).get('booking_id')}")
            else:
                fail += 1
                err = res.get("status") or res.get("error") or res.get("raw") or "?"
                errors.append(f"✗ {title}  →  {err}  (http {res.get('_http')})")
                self.on_event(errors[-1])
        return {"ok": ok, "fail": fail, "errors": errors}

    # -- корзина --

    def _held(self) -> set:
        """Что из наших мест реально лежит в корзине прямо сейчас."""
        basket = self.client.get_basket()
        items = basket.get("basket") or basket.get("items") or []
        held = set()
        for it in items:
            tid = it.get("ticket_seat_id") or it.get("id")
            if tid in self.target.ticket_seat_ids:
                held.add(tid)
        return held

    def basket_timer(self) -> Optional[int]:
        try:
            return self.client.get_basket().get("timer")
        except Exception:
            return None

    # -- цикл --

    def _loop(self):
        self.on_event("watcher запущен")
        while not self.stop_flag.is_set():
            try:
                held = self._held()
                missing = set(self.target.ticket_seat_ids) - held
                timer = self.basket_timer()
                self.on_event(
                    f"[{time.strftime('%H:%M:%S')}] удержано {len(held)}/"
                    f"{len(self.target.ticket_seat_ids)}"
                    + (f", таймер {timer}s" if timer is not None else "")
                )
                if missing:
                    self.on_event(f"потеряно {len(missing)} мест — перебрасываю")
                    self.book_all()
            except Exception as e:
                self.on_event(f"watcher: ошибка {e!r}")
            self.stop_flag.wait(self.interval)
        self.on_event("watcher остановлен")

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=3)