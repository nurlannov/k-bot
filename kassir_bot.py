# language: Python 3.10+
# file: kassir_bot.py
# target: Kassir.kg
# deps: pip install curl_cffi
# run:  python kassir_bot.py 4474-raimaly-menen-begimai-etno-miuzikli

import json
import re
import sys
import time
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional
from curl_cffi import requests as creq

from config import USER_UUID


GATEWAY = "https://gateway.kassir.kg"
WWW = "https://www.kassir.kg"


# ---------------------------------------------------------------- утилиты

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
        raise RuntimeError("не нашёл item в __NEXT_DATA__ — изменилась разметка страницы")
    item = json.loads("{" + m.group(0) + "}")
    return item["item"]


def fetch_session(slug: str) -> dict:
    """GET страницы сеанса → распарсенный item."""
    url = f"{WWW}/ru/session/{slug}/order"
    r = creq.get(url, impersonate="chrome", timeout=30)
    r.raise_for_status()
    return parse_next_data(r.text)


def parse_svg_map(html: str) -> dict:
    """html_id -> {sector, row, seat} из <g id="seat_N" data-sector=... data-row=... data-seat=...>"""
    out = {}
    for m in re.finditer(
        r'<g id="(seat_\d+)"\s+data-sector="([^"]+)"\s+data-row="(\d+)"\s+data-seat="(\d+)"',
        html
    ):
        html_id, sector, row, seat = m.groups()
        out[html_id] = {"sector": sector, "row": int(row), "seat": int(seat)}
    return out


def seat_title_from_svg(html_id: str, svg_map: dict) -> str:
    """Красивое имя места из SVG-карты."""
    info = svg_map.get(html_id)
    if not info:
        return html_id
    return f"{info['sector']} | ряд {info['row']} | место {info['seat']}"


# ---------------------------------------------------------------- клиент API

@dataclass
class KassirClient:
    user_uuid: str

    def get_order_items(self, slug: str) -> list:
        """GET /v1/session/order/item — список свободных мест для сеанса."""
        r = creq.get(
            f"{GATEWAY}/v1/session/order/item",
            params={"slug": slug, "user_uuid": self.user_uuid},
            impersonate="chrome",
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("payload", {}).get("order_items", [])

    def get_basket(self) -> dict:
        """GET /v1/basket — текущая корзина + таймер (в секундах)."""
        r = creq.get(
            f"{GATEWAY}/v1/basket",
            params={"user_uuid": self.user_uuid},
            impersonate="chrome",
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("payload", {}) or {}

    def add_to_cart(self, event_id: int, session_id: int, ticket_seat_id: int) -> dict:
        """POST /v1/order/item — забронировать место."""
        r = creq.post(
            f"{GATEWAY}/v1/order/item",
            json={
                "event_id": event_id,
                "session_id": session_id,
                "ticket_seat_id": ticket_seat_id,
                "user_uuid": self.user_uuid,
            },
            impersonate="chrome",
            timeout=20,
        )
        return r.json() if r.content else {"status": "error", "http": r.status_code}

    def clear_basket(self) -> dict:
        """POST /v1/basket/clear — очистить корзину."""
        r = creq.post(
            f"{GATEWAY}/v1/basket/clear",
            json={"user_uuid": self.user_uuid},
            impersonate="chrome",
            timeout=20,
        )
        return r.json() if r.content else {}


# ---------------------------------------------------------------- цель брони

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


# ---------------------------------------------------------------- бот

class KassirBot:
    def __init__(self, user_uuid: str):
        self.client = KassirClient(user_uuid=user_uuid)
        self.target: Optional[Target] = None
        self.stop_flag = threading.Event()

    # -------- интерактивный выбор --------

    def pick_session(self, item: dict) -> dict:
        others = item.get("other_sessions", []) or []
        if not others:
            return item
        print(f"\nВыбранный сеанс: {item['date_time']}")
        print("Другие сеансы этого мероприятия:")
        for i, s in enumerate(others, 1):
            print(f"  {i}. {s['date_time']}  (slug: {s['slug']})")
        print("  0. оставить выбранный")
        ans = input("→ номер сеанса: ").strip()
        if ans == "0" or not ans:
            return item
        try:
            picked = others[int(ans) - 1]
        except (ValueError, IndexError):
            print("неверный ввод, оставляю текущий")
            return item
        return fetch_session(picked["slug"])

    def pick_sector(self, svg_map: dict, available_svg_ids: set) -> str:
        live = sorted({svg_map[i]["sector"] for i in available_svg_ids if i in svg_map})
        if not live:
            raise RuntimeError("нет свободных мест ни в одном секторе")
        print("\nДоступные сектора:")
        for i, s in enumerate(live, 1):
            print(f"  {i}. {s}")
        while True:
            ans = input("→ номер сектора: ").strip()
            try:
                return live[int(ans) - 1]
            except (ValueError, IndexError):
                print("неверный ввод")

    def pick_seats(self, sector: str, svg_map: dict, available_svg_ids: set) -> list:
        rows = sorted({
            svg_map[i]["row"] for i in available_svg_ids
            if i in svg_map and svg_map[i]["sector"] == sector
        })
        if not rows:
            raise RuntimeError(f"в секторе {sector} нет свободных мест")
        print(f"\nСвободные ряды в {sector}: {rows}")
        print("ввод: '4' — весь ряд 4 | '4,13' — место 13 в ряду 4 | "
              "'4,13-15' — диапазон | 'done' — закончить")

        picked: list = []
        while True:
            ans = input("→ ").strip()
            if ans.lower() in ("done", "готово", ""):
                if picked:
                    return picked
                print("ничего не выбрано")
                continue
            try:
                if "," in ans:
                    row_s, rest = ans.split(",", 1)
                    row = int(row_s.strip())
                    if "-" in rest:
                        a, b = rest.split("-", 1)
                        seats = range(int(a), int(b) + 1)
                    else:
                        seats = [int(rest)]
                else:
                    row = int(ans)
                    seats = [
                        svg_map[i]["seat"] for i in available_svg_ids
                        if i in svg_map and svg_map[i]["sector"] == sector
                        and svg_map[i]["row"] == row
                    ]
            except ValueError:
                print("формат: 4,13  или  4,13-15  или  4  или  done")
                continue

            added = 0
            for i in available_svg_ids:
                info = svg_map.get(i)
                if not info:
                    continue
                if info["sector"] != sector or info["row"] != row:
                    continue
                if info["seat"] in seats and i not in picked:
                    picked.append(i)
                    added += 1
            print(f"  +{added} мест (всего {len(picked)})")

    # -------- сборка цели --------

    def build_target(self, slug: str) -> Target:
        item = fetch_session(slug)
        item = self.pick_session(item)

        r = creq.get(f"{WWW}/ru/session/{item['slug']}/order",
                     impersonate="chrome", timeout=30)
        svg_map = parse_svg_map(r.text)

        avail = self.client.get_order_items(item["slug"])
        available_svg_ids = {oi.get("html_id") for oi in avail if oi.get("html_id")}

        print(f"\nМероприятие:     {item['event']['title']}")
        print(f"Сеанс:           {item['date_time']}")
        print(f"Площадка:        {item['theater']['title']}")
        print(f"Свободных мест:  {len(available_svg_ids)}")

        sector = self.pick_sector(svg_map, available_svg_ids)
        picked_svg = self.pick_seats(sector, svg_map, available_svg_ids)

        ticket_map = {s["html_id"]: s["id"] for s in item["scheme"]["seats"]}
        ticket_ids = [ticket_map[s] for s in picked_svg if s in ticket_map]

        t = Target(
            slug=item["slug"],
            event_id=item["event"]["id"],
            session_id=item["id"],
            title=item["event"]["title"],
            date_time=item["date_time"],
            ticket_seat_ids=ticket_ids,
            svg_ids=picked_svg,
            svg_map=svg_map,
        )

        print(f"\nВыбрано {len(t.ticket_seat_ids)} мест:")
        for sid, tid in zip(t.svg_ids, t.ticket_seat_ids):
            print(f"  {seat_title_from_svg(sid, svg_map)}  →  ticket_seat_id={tid}")

        return t

    # -------- бронирование --------

    def try_book_all(self) -> None:
        assert self.target
        for sid, tid in zip(self.target.svg_ids, self.target.ticket_seat_ids):
            if self.stop_flag.is_set():
                return
            res = self.client.add_to_cart(
                self.target.event_id, self.target.session_id, tid)
            title = seat_title_from_svg(sid, self.target.svg_map)
            if res.get("status") == "success":
                p = res.get("payload", {})
                print(f"  ✓ {title}  booking_id={p.get('booking_id')}  "
                      f"price={p.get('price')}")
            else:
                print(f"  ✗ {title}  →  {res.get('status', res)}")

    def held_ticket_ids(self) -> set:
        assert self.target
        basket = self.client.get_basket()
        items = basket.get("basket") or basket.get("items") or []
        held = set()
        for it in items:
            tid = it.get("ticket_seat_id") or it.get("id")
            if tid in self.target.ticket_seat_ids:
                held.add(tid)
        return held

    def watcher_loop(self) -> None:
        assert self.target
        print("\nwatcher запущен. пиши 'стоп' для выхода.\n")
        while not self.stop_flag.is_set():
            try:
                held = self.held_ticket_ids()
                missing = set(self.target.ticket_seat_ids) - held
                basket = self.client.get_basket()
                timer = basket.get("timer")
                timer_s = f", таймер {timer}s" if timer is not None else ""
                print(f"[{time.strftime('%H:%M:%S')}] удержано "
                      f"{len(held)}/{len(self.target.ticket_seat_ids)}{timer_s}")

                if missing:
                    print(f"  потеряно {len(missing)} мест — перебрасываю")
                    self.try_book_all()
            except Exception as e:
                print(f"  watcher: ошибка {e!r}")
            self.stop_flag.wait(5)

    # -------- жизненный цикл --------

    def run(self, slug: str) -> None:
        self.target = self.build_target(slug)
        print("\nпервичное бронирование:")
        self.try_book_all()

        def stdin_loop():
            while not self.stop_flag.is_set():
                try:
                    line = input().strip().lower()
                except EOFError:
                    self.stop_flag.set()
                    return
                if line in ("стоп", "stop", "s", "q", "quit", "exit"):
                    print("получен стоп — завершаю")
                    self.stop_flag.set()
                    return

        threading.Thread(target=stdin_loop, daemon=True).start()
        try:
            self.watcher_loop()
        except KeyboardInterrupt:
            print("\nCtrl+C — выход")
        finally:
            self.stop_flag.set()


# ---------------------------------------------------------------- точка входа

def main():
    if len(sys.argv) >= 2:
        slug = sys.argv[1]
    else:
        slug = input("slug сеанса (например 4474-raimaly-menen-begimai-etno-miuzikli): ").strip()

    user_uuid = USER_UUID

    bot = KassirBot(user_uuid=user_uuid)
    bot.run(slug)


if __name__ == "__main__":
    main()