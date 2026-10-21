import html
import ssl
import threading
import time
from datetime import datetime, timedelta

import certifi
import requests
from requests.adapters import HTTPAdapter

# === 1. КОНФИГУРАЦИЯ ===
CONFIG = {
    "BASE_URL": "https://mozg.rest",

    # Как отличаем доставку от зала.
    # 1) Ручной список ID подразделений (hllg) доставки - самый надёжный способ.
    "DELIVERY_HLLG_IDS": set(),          # например {"7", "8"}
    # 2) Автоопределение по ключевым словам в названии подразделения или его типа.
    "DELIVERY_KEYWORDS": ["достав", "самовывоз", "delivery", "курьер", "яндекс", "деливери"],
    # 3) Автоопределение по признаку guestType == "order" (по умолчанию выключено).
    "DELIVERY_BY_GUEST_TYPE": False,

    # Сеть: сколько раз повторять запрос при обрыве соединения / 502 / 503 / 504 / 429.
    "HTTP_RETRIES": 4,
    # Если ошибка SSLEOFError остаётся - поставьте True (запретить TLS 1.3, оставить TLS 1.2).
    "FORCE_TLS12": False,
}

class MozgAuthError(Exception):
    """Токен недействителен или истёк (HTTP 401)."""


NBSP = "\u00a0"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
SEP = "\n\n" + "-" * 12 + "\n\n"

BASE_METRICS = [
    "realsum", "realsum_kit", "realsum_bar",
    "orders", "guests",
    "cheque_guest", "cheque_order",
    "avg_kit_qntt_order", "avg_bar_qntt_order",
    "avg_kit_qntt_guest", "avg_bar_qntt_guest",
]


# === HTTP: СЕССИЯ С ПОВТОРАМИ (лечит SSLEOFError / обрывы соединения) ===
RETRY_STATUSES = {429, 502, 503, 504}
_local = threading.local()      # у каждого потока своя сессия (бот ходит в API через to_thread)


class _TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if CONFIG["FORCE_TLS12"]:
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


def _get_session():
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.mount("https://", _TLSAdapter())
        _local.session = s
    return s


def _reset_session():
    """Закрываем сессию: после обрыва в пуле могут остаться «мёртвые» соединения."""
    s = getattr(_local, "session", None)
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
    _local.session = None


def _http(method, url, **kwargs):
    kwargs.setdefault("timeout", 60)
    attempts = max(int(CONFIG["HTTP_RETRIES"]), 1)
    for attempt in range(1, attempts + 1):
        try:
            response = _get_session().request(method, url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _reset_session()
            if attempt == attempts:
                raise
            print(f"Сбой соединения ({type(e).__name__}), попытка {attempt}/{attempts}, повторяю...")
            time.sleep(2 * attempt)
            continue
        if response.status_code in RETRY_STATUSES and attempt < attempts:
            print(f"Mozg вернул {response.status_code}, попытка {attempt}/{attempts}, повторяю...")
            time.sleep(2 * attempt)
            continue
        return response


# === 2. АВТОРИЗАЦИЯ ===
def _extract(data, key):
    if isinstance(data, list):
        return data[0]["data"].get(key, "")
    return data["data"].get(key, "")


def authenticate_and_get_org_id(login, password):
    """Возвращает (organization_id, access_token, refresh_token)."""
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/login"
    response = _http("POST", url, json={"login": login, "password": password}, timeout=60)
    response.raise_for_status()
    data = response.json()

    token = _extract(data, "access_token")
    refresh_token = _extract(data, "refresh_token")
    if not token:
        raise Exception("Не удалось получить токен")

    org_response = _http("GET", 
        f"{CONFIG['BASE_URL']}/api/v1/user/organizations",
        headers={"Authorization": f"Bearer {token}"}, timeout=60,
    )
    org_response.raise_for_status()
    org_data = org_response.json()
    items = org_data.get("data", {}).get("items", [])
    if not items:
        raise Exception("У пользователя нет доступа ни к одной организации")

    org_id = org_data.get("data", {}).get("currentOrganizationId") or items[0].get("organizationId")
    if not org_id:
        raise Exception("Не удалось определить ID организации")

    print(f"Авторизация успешна. Organization ID: {org_id}")
    return org_id, token, refresh_token


def get_access_token(login, password):
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/login"
    response = _http("POST", url, json={"login": login, "password": password}, timeout=60)
    response.raise_for_status()
    return _extract(response.json(), "access_token")


def refresh_access_token(refresh_token):
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/refresh-tokens"
    response = _http("POST", url, json={"refresh_token": refresh_token}, timeout=60)
    response.raise_for_status()
    return _extract(response.json(), "access_token")


def refresh_tokens(refresh_token):
    """Обменивает refresh-токен на новую пару. Возвращает (access_token, refresh_token).
    Новый refresh-токен обязательно сохраняйте: старый после обмена перестаёт работать."""
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/refresh-tokens"
    response = _http("POST", url, json={"refresh_token": refresh_token}, timeout=60)
    response.raise_for_status()
    data = response.json()
    return _extract(data, "access_token"), _extract(data, "refresh_token") or refresh_token


# === 3. СПРАВОЧНИКИ: РЕСТОРАНЫ И КАНАЛЫ (ЗАЛ / ДОСТАВКА) ===
def get_restaurants(token, org_id):
    """Список активных ресторанов: [{"key": "1|R01", "name": "Ресторан 1"}]."""
    response = _http("GET", 
        f"{CONFIG['BASE_URL']}/api/v1/cube/dictionaries",
        headers={"Authorization": f"Bearer {token}"},
        params={"types": "rest", "organization_id": org_id},
        timeout=60,
    )
    response.raise_for_status()
    items = response.json().get("data", {}).get("items", {}).get("rest", [])
    return [
        {"key": r["restKey"], "name": r.get("desc", r["restKey"])}
        for r in items if r.get("active", True)
    ]


def _is_delivery_text(text):
    text = (text or "").lower()
    return any(kw in text for kw in CONFIG["DELIVERY_KEYWORDS"])


def _to_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def get_channel_ids(token, org_id, verbose=True):
    """
    Определяет ID подразделений зала и доставки.
    Возвращает {"hall": [ids] или None, "delivery": [ids]}.
    hall=None значит "справочник недоступен, фильтр по залу не применять".
    """
    response = _http("GET", 
        f"{CONFIG['BASE_URL']}/api/v1/cube/dictionaries",
        headers={"Authorization": f"Bearer {token}"},
        params={"types": "hllg,hllgType", "includeInactive": "true", "organization_id": org_id},
        timeout=60,
    )
    if response.status_code == 401:
        raise MozgAuthError("Токен недействителен или истёк")
    if response.status_code != 200:
        print(f"Не удалось загрузить справочник подразделений: {response.status_code}")
        return {"hall": None, "delivery": []}

    items = response.json().get("data", {}).get("items", {})
    type_desc = {t.get("type"): t.get("desc", "") for t in items.get("hllgType", [])}
    manual = {str(x) for x in CONFIG["DELIVERY_HLLG_IDS"]}

    hall, delivery = [], []
    hall_names, delivery_names, warnings = [], [], []

    for h in items.get("hllg", []):
        hid = _to_id(h.get("id"))
        desc = h.get("desc", "")
        guest_type = h.get("guestType", "")
        is_delivery = (
            str(hid) in manual
            or _is_delivery_text(desc)
            or _is_delivery_text(type_desc.get(h.get("type"), ""))
            or (CONFIG["DELIVERY_BY_GUEST_TYPE"] and guest_type == "order")
        )
        label = f"{desc} [id={hid}, {guest_type}]"
        if is_delivery:
            delivery.append(hid)
            delivery_names.append(label)
        else:
            hall.append(hid)
            hall_names.append(label)
            if guest_type == "order":
                warnings.append(label)

    if verbose:
        print("Классификация подразделений:")
        print("  Доставка:", "; ".join(delivery_names) or "не найдено")
        print("  Зал:     ", "; ".join(hall_names) or "не найдено")
        if warnings:
            print("  ВНИМАНИЕ: в зале есть подразделения с guestType=order (возможно доставка/самовывоз):")
            print("  ", "; ".join(warnings))

    return {"hall": hall, "delivery": delivery}


# === 4. ЗАПРОСЫ ДАННЫХ ===
def _sales_query(token, payload):
    response = _http("POST", 
        f"{CONFIG['BASE_URL']}/api/v1/sales/query",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload, timeout=90,
    )
    if response.status_code == 401:
        raise MozgAuthError("Токен недействителен или истёк")
    if response.status_code != 200:
        period = payload.get("period", {}).get("from")
        print(f"Ошибка запроса ({period}): {response.status_code} {response.text[:200]}")
        return []
    return response.json().get("data", {}).get("rows", [])


def _safe_div(a, b):
    return a / b if b else 0.0


def _empty_metrics():
    return {
        "revenue": 0.0, "revenue_kit": 0.0, "revenue_bar": 0.0,
        "orders": 0.0, "guests": 0.0,
        "avg_check_order": 0.0, "avg_check_guest": 0.0,
        "avg_kit_order": 0.0, "avg_bar_order": 0.0,
        "avg_kit_guest": 0.0, "avg_bar_guest": 0.0,
    }


def _empty_entry():
    return {"key": None, "hall": _empty_metrics(), "delivery": _empty_metrics(), "plan": 0, "plan_guests": 0}


def _parse_metrics(m):
    """Берём готовые значения Mozg; пересчитываем только если Mozg вернул 0."""
    def val(key):
        return m.get(key, {}).get("value", 0) or 0

    revenue, orders, guests = val("realsum"), val("orders"), val("guests")
    return {
        "revenue": revenue,
        "revenue_kit": val("realsum_kit"),
        "revenue_bar": val("realsum_bar"),
        "orders": orders,
        "guests": guests,
        "avg_check_order": val("cheque_order") or _safe_div(revenue, orders),
        "avg_check_guest": val("cheque_guest") or _safe_div(revenue, guests),
        "avg_kit_order": val("avg_kit_qntt_order"),
        "avg_bar_order": val("avg_bar_qntt_order"),
        "avg_kit_guest": val("avg_kit_qntt_guest"),
        "avg_bar_guest": val("avg_bar_qntt_guest"),
    }


def fetch_plan_data(token, org_id, date_from, date_to, rest_keys=None):
    """План выручки и гостей по ресторанам (метрики plan / plan_guests из Mozg)."""
    payload = {
        "organization_id": org_id,
        "period": {"from": date_from, "to": date_to},
        "metrics": ["plan", "plan_guests"],
        "group_by": ["rest"],
    }
    if rest_keys:
        payload["filters"] = {"rest": rest_keys}

    plans = {}
    for row in _sales_query(token, payload):
        name = row.get("labels", {}).get("rest", "Неизвестно")
        m = row.get("metrics", {})
        plans[name] = {
            "key": row.get("keys", {}).get("rest"),
            "plan": m.get("plan", {}).get("value", 0) or 0,
            "plan_guests": m.get("plan_guests", {}).get("value", 0) or 0,
        }
    return plans


def fetch_channel_data(token, org_id, date_from, date_to, channel_ids,
                       rest_keys=None, with_plan=True):
    """
    Продажи отдельно по залу и по доставке. Для каждого канала Mozg считает
    все показатели сам (фильтр по подразделениям), поэтому средние чеки
    совпадают с отчётами Mozg.
    Возвращает {rest_name: {"hall": {...}, "delivery": {...}, "plan": x, "plan_guests": y}}
    """
    result = {}

    for channel in ("hall", "delivery"):
        ids = channel_ids.get(channel)
        if ids is not None and len(ids) == 0:
            continue  # у канала нет подразделений

        filters = {}
        if ids:
            filters["hllg"] = ids
        if rest_keys:
            filters["rest"] = rest_keys

        payload = {
            "organization_id": org_id,
            "period": {"from": date_from, "to": date_to},
            "metrics": BASE_METRICS,
            "group_by": ["rest"],
        }
        if filters:
            payload["filters"] = filters

        for row in _sales_query(token, payload):
            name = row.get("labels", {}).get("rest", "Неизвестно")
            entry = result.setdefault(name, _empty_entry())
            entry["key"] = row.get("keys", {}).get("rest") or entry["key"]
            entry[channel] = _parse_metrics(row.get("metrics", {}))

    if with_plan:
        for name, p in fetch_plan_data(token, org_id, date_from, date_to, rest_keys).items():
            entry = result.setdefault(name, _empty_entry())
            entry["key"] = entry["key"] or p.get("key")
            entry["plan"] = p["plan"]
            entry["plan_guests"] = p["plan_guests"]

    return result


# === 5. РАСЧЁТЫ ===
def calc_change(current, previous):
    """Изменение в процентах."""
    if previous == 0 and current == 0:
        return 0.0
    elif previous == 0 and current > 0:
        return 100.0
    elif previous > 0:
        return ((current - previous) / previous) * 100
    return 0.0


def derive_metrics(m, with_guests):
    """
    with_guests=True  - зал (гости, наполняемость, чек на гостя)
    with_guests=False - доставка (только заказы)
    """
    d = {
        "revenue": m["revenue"],
        "revenue_kit": m["revenue_kit"],
        "revenue_bar": m["revenue_bar"],
        "kit_percent": _safe_div(m["revenue_kit"], m["revenue"]) * 100,
        "bar_percent": _safe_div(m["revenue_bar"], m["revenue"]) * 100,
        "orders": m["orders"],
        "avg_check_order": m["avg_check_order"],
        "avg_kit_order": m["avg_kit_order"],
        "avg_bar_order": m["avg_bar_order"],
    }
    if with_guests:
        d.update({
            "guests": m["guests"],
            "avg_check_guest": m["avg_check_guest"],
            "occupancy": _safe_div(m["guests"], m["orders"]),
            "avg_kit_guest": m["avg_kit_guest"],
            "avg_bar_guest": m["avg_bar_guest"],
        })
    return d


def build_block(cur_m, prev_m, with_guests):
    cur = derive_metrics(cur_m, with_guests)
    prev = derive_metrics(prev_m, with_guests)
    block = dict(cur)
    for key in cur:
        block[f"{key}_ly"] = prev[key]
        block[f"{key}_change"] = calc_change(cur[key], prev[key])
    return block


def build_rest_report(name, cur, prev):
    cur = cur or _empty_entry()
    prev = prev or _empty_entry()

    hall = build_block(cur["hall"], prev["hall"], with_guests=True)
    delivery = build_block(cur["delivery"], prev["delivery"], with_guests=False)

    total_revenue = cur["hall"]["revenue"] + cur["delivery"]["revenue"]
    total_revenue_ly = prev["hall"]["revenue"] + prev["delivery"]["revenue"]
    total_orders = cur["hall"]["orders"] + cur["delivery"]["orders"]
    total_orders_ly = prev["hall"]["orders"] + prev["delivery"]["orders"]

    plan = cur.get("plan", 0)
    plan_guests = cur.get("plan_guests", 0)

    return {
        "key": cur.get("key") or prev.get("key"),
        "name": name,
        "hall": hall,
        "delivery": delivery,
        "total_revenue": total_revenue,
        "total_revenue_ly": total_revenue_ly,
        "total_revenue_change": calc_change(total_revenue, total_revenue_ly),
        "total_orders": total_orders,
        "total_orders_ly": total_orders_ly,
        "total_orders_change": calc_change(total_orders, total_orders_ly),
        "delivery_share": _safe_div(delivery["revenue"], total_revenue) * 100,
        # План выручки сравниваем с итогом (зал + доставка),
        # план по гостям - с гостями зала.
        "plan": plan,
        "plan_completion": _safe_div(total_revenue, plan) * 100,
        "plan_guests": plan_guests,
        "plan_guests_completion": _safe_div(cur["hall"]["guests"], plan_guests) * 100,
        "plan_today": 0,
    }


def _days_between(date_from, date_to):
    d1 = datetime.strptime(date_from, "%Y-%m-%d")
    d2 = datetime.strptime(date_to, "%Y-%m-%d")
    return max((d2 - d1).days + 1, 1)


def _login_if_needed(login, password, org_id, token):
    if not token:
        if not login or not password:
            raise Exception("Для авторизации нужны логин и пароль или токен")
        token = get_access_token(login, password)
    if not org_id:
        if not login or not password:
            raise Exception("Для определения организации нужны логин и пароль")
        org_id, _, _ = authenticate_and_get_org_id(login, password)
    return org_id, token


# === 6. ОТЧЁТ ЗА ВЧЕРА ===
def fetch_full_analytics(login="", password="", org_id=None, token=None, date=None, rest_keys=None):
    """
    Данные за ВЧЕРА (или за дату date="YYYY-MM-DD") с разбивкой зал/доставка,
    сравнением с прошлым годом, планом за день и планом на сегодня.
    rest_keys - список ресторанов ["1|R01"] (если нужен один ресторан).
    """
    org_id, token = _login_if_needed(login, password, org_id, token)
    channel_ids = get_channel_ids(token, org_id)

    if date:
        target_date = datetime.strptime(date, "%Y-%m-%d")
    else:
        target_date = datetime.now() - timedelta(days=1)
    target_date_str = target_date.strftime("%Y-%m-%d")

    last_year_date = target_date - timedelta(days=365)
    last_year_str = last_year_date.strftime("%Y-%m-%d")
    first_day_of_month = target_date.replace(day=1).strftime("%Y-%m-%d")
    last_year_month_start = last_year_date.replace(day=1).strftime("%Y-%m-%d")

    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")

    print(f"Собираем данные за {target_date.strftime('%d.%m.%Y')}...")

    target_data = fetch_channel_data(token, org_id, target_date_str, target_date_str, channel_ids, rest_keys)
    last_year_data = fetch_channel_data(token, org_id, last_year_str, last_year_str, channel_ids, rest_keys)
    month_data = fetch_channel_data(token, org_id, first_day_of_month, target_date_str, channel_ids, rest_keys, with_plan=False)
    ly_month_data = fetch_channel_data(token, org_id, last_year_month_start, last_year_str, channel_ids, rest_keys, with_plan=False)
    plans_today = fetch_plan_data(token, org_id, today_str, today_str, rest_keys)

    if not target_data:
        raise Exception(f"Не удалось получить данные за {target_date_str}")

    all_restaurants = sorted(set(target_data) | set(last_year_data) | set(month_data))
    all_restaurants = [r for r in all_restaurants if r != "Итого"]

    days_cur = _days_between(first_day_of_month, target_date_str)
    days_ly = _days_between(last_year_month_start, last_year_str)

    restaurants_report = []
    for rest_name in all_restaurants:
        report = build_rest_report(rest_name, target_data.get(rest_name), last_year_data.get(rest_name))
        report["plan_today"] = plans_today.get(rest_name, {}).get("plan", 0)

        m_cur = month_data.get(rest_name) or _empty_entry()
        m_ly = ly_month_data.get(rest_name) or _empty_entry()
        hall_m, deliv_m = m_cur["hall"]["revenue"], m_cur["delivery"]["revenue"]
        hall_ly, deliv_ly = m_ly["hall"]["revenue"], m_ly["delivery"]["revenue"]

        report["month"] = {
            "label": f"{datetime.strptime(first_day_of_month, '%Y-%m-%d'):%d.%m}-{target_date:%d.%m}",
            "hall": hall_m,
            "delivery": deliv_m,
            "total": hall_m + deliv_m,
            "avg_daily_hall": hall_m / days_cur,
            "avg_daily_hall_change": calc_change(hall_m / days_cur, hall_ly / days_ly),
            "avg_daily_delivery": deliv_m / days_cur,
            "avg_daily_delivery_change": calc_change(deliv_m / days_cur, deliv_ly / days_ly),
            "avg_daily_total": (hall_m + deliv_m) / days_cur,
            "avg_daily_total_change": calc_change((hall_m + deliv_m) / days_cur, (hall_ly + deliv_ly) / days_ly),
        }
        restaurants_report.append(report)

    return {
        "date": target_date_str,
        "today": today_str,
        "restaurants": restaurants_report,
    }


# === 7. НЕДЕЛЬНЫЙ ОТЧЁТ ===
def fetch_weekly_analytics(login="", password="", org_id=None, token=None, rest_keys=None):
    """Последние 7 дней (до вчера) и та же неделя прошлого года."""
    org_id, token = _login_if_needed(login, password, org_id, token)
    channel_ids = get_channel_ids(token, org_id)

    end_date = datetime.now() - timedelta(days=1)
    start_date = end_date - timedelta(days=6)
    ly_end = end_date - timedelta(days=365)
    ly_start = start_date - timedelta(days=365)

    s, e = start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
    ls, le = ly_start.strftime("%Y-%m-%d"), ly_end.strftime("%Y-%m-%d")

    print(f"Собираем данные за неделю {s} - {e}...")
    current_week = fetch_channel_data(token, org_id, s, e, channel_ids, rest_keys)
    last_year_week = fetch_channel_data(token, org_id, ls, le, channel_ids, rest_keys, with_plan=False)

    if not current_week:
        raise Exception("Не удалось получить данные за текущую неделю")

    all_restaurants = sorted(r for r in (set(current_week) | set(last_year_week)) if r != "Итого")

    restaurants_report = []
    for rest_name in all_restaurants:
        report = build_rest_report(rest_name, current_week.get(rest_name), last_year_week.get(rest_name))
        report["avg_daily_total"] = report["total_revenue"] / 7
        report["avg_daily_total_change"] = calc_change(report["total_revenue"], report["total_revenue_ly"])
        report["avg_daily_hall"] = report["hall"]["revenue"] / 7
        report["avg_daily_hall_change"] = report["hall"]["revenue_change"]
        report["avg_daily_delivery"] = report["delivery"]["revenue"] / 7
        report["avg_daily_delivery_change"] = report["delivery"]["revenue_change"]
        restaurants_report.append(report)

    return {
        "start_date": s, "end_date": e,
        "last_year_start": ls, "last_year_end": le,
        "restaurants": restaurants_report,
    }


# === 7.1. ОТЧЁТ ЗА ПРОИЗВОЛЬНЫЙ ПЕРИОД ===
def fetch_period_analytics(login="", password="", org_id=None, token=None,
                           date_from=None, date_to=None, rest_keys=None):
    """
    Отчёт за период date_from..date_to (оба "YYYY-MM-DD", включительно)
    и сравнение с тем же периодом прошлого года (сдвиг на 365 дней, как в остальных отчётах).
    """
    if not date_from or not date_to:
        raise Exception("Укажите даты периода: date_from и date_to")
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    if start > end:
        raise Exception("Начало периода позже конца")

    org_id, token = _login_if_needed(login, password, org_id, token)
    channel_ids = get_channel_ids(token, org_id)

    days = _days_between(date_from, date_to)
    ly_start, ly_end = start - timedelta(days=365), end - timedelta(days=365)
    ls, le = ly_start.strftime("%Y-%m-%d"), ly_end.strftime("%Y-%m-%d")

    print(f"Собираем данные за период {date_from} - {date_to}...")
    current = fetch_channel_data(token, org_id, date_from, date_to, channel_ids, rest_keys)
    last_year = fetch_channel_data(token, org_id, ls, le, channel_ids, rest_keys, with_plan=False)

    if not current:
        raise Exception(f"Не удалось получить данные за период {date_from} - {date_to}")

    all_restaurants = sorted(r for r in (set(current) | set(last_year)) if r != "Итого")

    restaurants_report = []
    for rest_name in all_restaurants:
        report = build_rest_report(rest_name, current.get(rest_name), last_year.get(rest_name))
        report["avg_daily_total"] = report["total_revenue"] / days
        report["avg_daily_total_change"] = report["total_revenue_change"]
        report["avg_daily_hall"] = report["hall"]["revenue"] / days
        report["avg_daily_hall_change"] = report["hall"]["revenue_change"]
        report["avg_daily_delivery"] = report["delivery"]["revenue"] / days
        report["avg_daily_delivery_change"] = report["delivery"]["revenue_change"]
        restaurants_report.append(report)

    return {
        "start_date": date_from, "end_date": date_to,
        "last_year_start": ls, "last_year_end": le,
        "days": days,
        "restaurants": restaurants_report,
    }


# === 8. ПЛАН НА СЕГОДНЯ ===
def fetch_today_plan(login="", password="", org_id=None, token=None, rest_keys=None):
    """План выручки на сегодня по данным Mozg (метрика plan)."""
    org_id, token = _login_if_needed(login, password, org_id, token)
    today_str = datetime.now().strftime("%Y-%m-%d")
    plans = fetch_plan_data(token, org_id, today_str, today_str, rest_keys)
    return {
        "date": today_str,
        "restaurants": [{"key": p.get("key"), "name": n, "plan": p["plan"]}
                        for n, p in sorted(plans.items()) if n != "Итого"],
    }


# === 9. СТАТИСТИКА ПО ОФИЦИАНТАМ ===
def fetch_waiters_report(login="", password="", org_id=None, token=None,
                         rest_key=None, date=None, hall_only=True):
    """
    Продажи по официантам одного ресторана за день (по умолчанию вчера).
    hall_only=True - только подразделения зала (без доставки).
    """
    if not rest_key:
        raise Exception("Укажите ресторан: rest_key вида '1|R01'")
    org_id, token = _login_if_needed(login, password, org_id, token)

    day = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now() - timedelta(days=1)
    day_str = day.strftime("%Y-%m-%d")

    filters = {"rest": [rest_key]}
    if hall_only:
        hall_ids = get_channel_ids(token, org_id, verbose=False).get("hall")
        if hall_ids:
            filters["hllg"] = hall_ids

    payload = {
        "organization_id": org_id,
        "period": {"from": day_str, "to": day_str},
        "metrics": ["realsum", "orders", "guests", "cheque_order", "cheque_guest",
                    "avg_kit_qntt_guest", "avg_bar_qntt_guest"],
        "group_by": ["employee_id"],
        "filters": filters,
    }

    waiters, rest_name = [], ""
    for row in _sales_query(token, payload):
        keys, labels = row.get("keys", {}), row.get("labels", {})
        m = _parse_metrics(row.get("metrics", {}))
        if m["revenue"] == 0 and m["orders"] == 0:
            continue
        waiters.append({
            "key": keys.get("employee_id"),
            "name": labels.get("employee_id") or str(keys.get("employee_id")),
            **m,
        })
    waiters.sort(key=lambda w: w["revenue"], reverse=True)

    return {"date": day_str, "rest_key": rest_key, "waiters": waiters}


# === 10. ПРОВЕРКА ДАННЫХ (сверка с Mozg) ===
def check_data(login="", password="", org_id=None, token=None, date=None, rest_keys=None):
    """
    Печатает сверку: по каждому подразделению показывает чек на гостя из Mozg и
    пересчёт выручка/гости, а также сравнивает сумму зал + доставка с общим итогом Mozg.
    Запустите для даты, где цифры не сошлись, и пришлите вывод.
    """
    org_id, token = _login_if_needed(login, password, org_id, token)
    day = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now() - timedelta(days=1)
    d = day.strftime("%Y-%m-%d")
    channel_ids = get_channel_ids(token, org_id)
    delivery_ids = set(channel_ids.get("delivery") or [])

    base_filters = {"rest": rest_keys} if rest_keys else {}
    metrics = ["realsum", "orders", "guests", "cheque_guest", "cheque_order"]

    def q(group_by, extra=None):
        payload = {"organization_id": org_id, "period": {"from": d, "to": d},
                   "metrics": metrics, "group_by": group_by}
        f = dict(base_filters)
        f.update(extra or {})
        if f:
            payload["filters"] = f
        return _sales_query(token, payload)

    print(f"\nСВЕРКА за {d}")
    print("-" * 60)
    sums = {}
    for row in q(["rest", "hllg"]):
        lab, keys, m = row.get("labels", {}), row.get("keys", {}), row.get("metrics", {})
        v = lambda k: m.get(k, {}).get("value", 0) or 0
        rev, orders, guests, cg = v("realsum"), v("orders"), v("guests"), v("cheque_guest")
        calc = _safe_div(rev, guests)
        channel = "доставка" if _to_id(keys.get("hllg")) in delivery_ids else "зал"
        flag = "" if abs(cg - calc) < 1 else "  <-- чек Mozg != выручка/гости"
        print(f"{lab.get('rest')} | {lab.get('hllg')} ({channel})")
        print(f"   выручка {rev:,.0f}, заказов {orders:.0f}, гостей {guests:.0f}")
        print(f"   чек на гостя Mozg {cg:,.0f}, пересчёт {calc:,.0f}{flag}")
        s = sums.setdefault(lab.get("rest"), {"rev": 0, "orders": 0, "guests": 0})
        s["rev"] += rev; s["orders"] += orders; s["guests"] += guests

    print("-" * 60)
    print("Сумма по подразделениям против итога Mozg по ресторану:")
    for row in q(["rest"]):
        name = row.get("labels", {}).get("rest")
        m = row.get("metrics", {})
        v = lambda k: m.get(k, {}).get("value", 0) or 0
        s = sums.get(name, {"rev": 0, "orders": 0, "guests": 0})
        print(f"{name}: выручка {s['rev']:,.0f} / {v('realsum'):,.0f}, "
              f"заказов {s['orders']:.0f} / {v('orders'):.0f}, "
              f"гостей {s['guests']:.0f} / {v('guests'):.0f}, "
              f"чек на гостя Mozg {v('cheque_guest'):,.0f}")


# === 11. ФОРМАТИРОВАНИЕ ДЛЯ TELEGRAM (parse_mode="HTML") ===
# Без эмодзи и спецсимволов: только текст, <b> и короткие строки.
def fmt_money(value):
    return f"{value:,.0f}".replace(",", NBSP) + f"{NBSP}руб."


def fmt_int(value):
    return f"{value:,.0f}".replace(",", NBSP)


def fmt_qntt(value):
    return f"{value:.1f}"


def fmt_delta(value):
    return "0%" if abs(value) < 0.05 else f"{value:+.1f}%"


def _fmt_date(dt):
    return f"{dt:%d.%m.%Y} ({WEEKDAYS[dt.weekday()]})"


def _hall_text(h):
    t = "<b>В РЕСТОРАНЕ</b>\n"
    t += f"Выручка: <b>{fmt_money(h['revenue'])}</b> ({fmt_delta(h['revenue_change'])})\n"
    t += f"Блюда: {fmt_money(h['revenue_kit'])} ({h['kit_percent']:.0f}%)\n"
    t += f"Напитки: {fmt_money(h['revenue_bar'])} ({h['bar_percent']:.0f}%)\n"
    t += f"Заказов: <b>{fmt_int(h['orders'])}</b> ({fmt_delta(h['orders_change'])})\n"
    t += f"Гостей: <b>{fmt_int(h['guests'])}</b> ({fmt_delta(h['guests_change'])})\n"
    t += f"Чек на заказ: {fmt_money(h['avg_check_order'])} ({fmt_delta(h['avg_check_order_change'])})\n"
    t += f"Чек на гостя: {fmt_money(h['avg_check_guest'])} ({fmt_delta(h['avg_check_guest_change'])})\n"
    t += f"Наполняемость: <b>{h['occupancy']:.2f}</b> ({fmt_delta(h['occupancy_change'])})\n"
    t += f"Блюд на заказ: {fmt_qntt(h['avg_kit_order'])}, на гостя: {fmt_qntt(h['avg_kit_guest'])}\n"
    t += f"Напитков на заказ: {fmt_qntt(h['avg_bar_order'])}, на гостя: {fmt_qntt(h['avg_bar_guest'])}"
    return t


def _delivery_text(d):
    t = "<b>ДОСТАВКА</b>\n"
    if d["revenue"] == 0 and d["revenue_ly"] == 0 and d["orders"] == 0:
        return t + "Нет продаж за период"
    t += f"Выручка: <b>{fmt_money(d['revenue'])}</b> ({fmt_delta(d['revenue_change'])})\n"
    t += f"Блюда: {fmt_money(d['revenue_kit'])} ({d['kit_percent']:.0f}%)\n"
    t += f"Напитки: {fmt_money(d['revenue_bar'])} ({d['bar_percent']:.0f}%)\n"
    t += f"Заказов: <b>{fmt_int(d['orders'])}</b> ({fmt_delta(d['orders_change'])})\n"
    t += f"Чек на заказ: {fmt_money(d['avg_check_order'])} ({fmt_delta(d['avg_check_order_change'])})\n"
    t += f"Блюд на заказ: {fmt_qntt(d['avg_kit_order'])}\n"
    t += f"Напитков на заказ: {fmt_qntt(d['avg_bar_order'])}"
    return t


def _total_text(rest):
    t = "<b>ИТОГО (зал и доставка)</b>\n"
    t += f"Выручка: <b>{fmt_money(rest['total_revenue'])}</b> ({fmt_delta(rest['total_revenue_change'])})\n"
    t += f"Заказов: {fmt_int(rest['total_orders'])} ({fmt_delta(rest['total_orders_change'])})\n"
    t += f"Доля доставки: {rest['delivery_share']:.0f}%"
    return t


def _plan_text(rest, period_word):
    lines = []
    if rest["plan"] > 0:
        lines.append(f"Выручка: {fmt_money(rest['plan'])}")
        lines.append(f"Выполнено: <b>{rest['plan_completion']:.1f}%</b>")
    if rest["plan_guests"] > 0:
        lines.append(f"Гости (зал): {fmt_int(rest['plan_guests'])}")
        lines.append(f"Выполнено: <b>{rest['plan_guests_completion']:.1f}%</b>")
    if not lines:
        return ""
    return f"<b>ПЛАН НА {period_word}</b>\n" + "\n".join(lines)


def _plan_today_text(rest, today_dt):
    head = f"<b>ПЛАН НА СЕГОДНЯ, {today_dt:%d.%m.%Y}</b>\n"
    if rest.get("plan_today", 0) > 0:
        return head + f"Выручка: <b>{fmt_money(rest['plan_today'])}</b>"
    return head + "В Mozg не задан"


def _month_text(m):
    t = f"<b>МЕСЯЦ ({m['label']})</b>\n"
    t += f"Выручка: <b>{fmt_money(m['total'])}</b>\n"
    t += f"Зал: {fmt_money(m['hall'])}\n"
    t += f"Доставка: {fmt_money(m['delivery'])}\n"
    t += "<b>В среднем за день</b> (к прошлому году)\n"
    t += f"Зал: {fmt_money(m['avg_daily_hall'])} ({fmt_delta(m['avg_daily_hall_change'])})\n"
    t += f"Доставка: {fmt_money(m['avg_daily_delivery'])} ({fmt_delta(m['avg_daily_delivery_change'])})\n"
    t += f"Итого: {fmt_money(m['avg_daily_total'])} ({fmt_delta(m['avg_daily_total_change'])})"
    return t


def format_report_parts(analytics):
    """Отчёт за вчера: одно сообщение на ресторан."""
    dt = datetime.strptime(analytics["date"], "%Y-%m-%d")
    ly = dt - timedelta(days=365)
    today_dt = datetime.strptime(analytics["today"], "%Y-%m-%d")

    parts = []
    for rest in analytics["restaurants"]:
        blocks = [
            f"<b>{html.escape(rest['name'])}</b>\n"
            f"Вчера, {_fmt_date(dt)}\n"
            f"В скобках: изменение к {ly:%d.%m.%Y}",
            _hall_text(rest["hall"]),
            _delivery_text(rest["delivery"]),
            _total_text(rest),
        ]
        plan = _plan_text(rest, "ВЧЕРА")
        if plan:
            blocks.append(plan)
        blocks.append(_plan_today_text(rest, today_dt))
        blocks.append(_month_text(rest["month"]))
        parts.append("\n\n".join(blocks))
    return parts


def format_report(analytics):
    return SEP.join(format_report_parts(analytics))


def format_weekly_report_parts(analytics):
    start = datetime.strptime(analytics["start_date"], "%Y-%m-%d")
    end = datetime.strptime(analytics["end_date"], "%Y-%m-%d")
    ls = datetime.strptime(analytics["last_year_start"], "%Y-%m-%d")
    le = datetime.strptime(analytics["last_year_end"], "%Y-%m-%d")

    parts = []
    for rest in analytics["restaurants"]:
        avg = "<b>В СРЕДНЕМ ЗА ДЕНЬ</b>\n"
        avg += f"Зал: {fmt_money(rest['avg_daily_hall'])} ({fmt_delta(rest['avg_daily_hall_change'])})\n"
        avg += f"Доставка: {fmt_money(rest['avg_daily_delivery'])} ({fmt_delta(rest['avg_daily_delivery_change'])})\n"
        avg += f"Итого: {fmt_money(rest['avg_daily_total'])} ({fmt_delta(rest['avg_daily_total_change'])})"

        blocks = [
            f"<b>{html.escape(rest['name'])}</b>\n"
            f"Неделя: {start:%d.%m}-{end:%d.%m.%Y}\n"
            f"В скобках: изменение к {ls:%d.%m}-{le:%d.%m.%Y}",
            _hall_text(rest["hall"]),
            _delivery_text(rest["delivery"]),
            _total_text(rest),
            avg,
        ]
        plan = _plan_text(rest, "НЕДЕЛЮ")
        if plan:
            blocks.append(plan)
        parts.append("\n\n".join(blocks))
    return parts


def format_weekly_report(analytics):
    return SEP.join(format_weekly_report_parts(analytics))


def format_period_report_parts(analytics):
    """Отчёт за произвольный период: одно сообщение на ресторан."""
    start = datetime.strptime(analytics["start_date"], "%Y-%m-%d")
    end = datetime.strptime(analytics["end_date"], "%Y-%m-%d")
    ls = datetime.strptime(analytics["last_year_start"], "%Y-%m-%d")
    le = datetime.strptime(analytics["last_year_end"], "%Y-%m-%d")
    days = analytics["days"]

    if days == 1:
        period_line = f"Период: {_fmt_date(start)}"
        compare_line = f"В скобках: изменение к {ls:%d.%m.%Y}"
    else:
        period_line = f"Период: {start:%d.%m.%Y} - {end:%d.%m.%Y} ({days} дн.)"
        compare_line = f"В скобках: изменение к {ls:%d.%m.%Y} - {le:%d.%m.%Y}"

    parts = []
    for rest in analytics["restaurants"]:
        blocks = [
            f"<b>{html.escape(rest['name'])}</b>\n{period_line}\n{compare_line}",
            _hall_text(rest["hall"]),
            _delivery_text(rest["delivery"]),
            _total_text(rest),
        ]
        if days > 1:
            avg = "<b>В СРЕДНЕМ ЗА ДЕНЬ</b>\n"
            avg += f"Зал: {fmt_money(rest['avg_daily_hall'])} ({fmt_delta(rest['avg_daily_hall_change'])})\n"
            avg += f"Доставка: {fmt_money(rest['avg_daily_delivery'])} ({fmt_delta(rest['avg_daily_delivery_change'])})\n"
            avg += f"Итого: {fmt_money(rest['avg_daily_total'])} ({fmt_delta(rest['avg_daily_total_change'])})"
            blocks.append(avg)
        plan = _plan_text(rest, "ПЕРИОД")
        if plan:
            blocks.append(plan)
        parts.append("\n\n".join(blocks))
    return parts


def format_period_report(analytics):
    return SEP.join(format_period_report_parts(analytics))


def format_today_plan_parts(plan_data):
    dt = datetime.strptime(plan_data["date"], "%Y-%m-%d")
    parts = []
    for r in plan_data["restaurants"]:
        text = f"<b>{html.escape(r['name'])}</b>\nПлан выручки на сегодня, {_fmt_date(dt)}\n"
        text += f"<b>{fmt_money(r['plan'])}</b>" if r["plan"] > 0 else "В Mozg не задан"
        parts.append(text)
    return parts


def format_waiters_parts(report, rest_name="", limit=3800):
    """Официанты: рейтинг по выручке. Делится на несколько сообщений, если длинный."""
    dt = datetime.strptime(report["date"], "%Y-%m-%d")
    head = f"<b>{html.escape(rest_name)}</b>\nОфицианты, {_fmt_date(dt)}\n" if rest_name \
        else f"<b>Официанты</b>, {_fmt_date(dt)}\n"

    waiters = report["waiters"]
    if not waiters:
        return [head + "\nНет продаж за период"]

    total = sum(w["revenue"] for w in waiters)
    head += f"Всего: <b>{fmt_money(total)}</b>, официантов: {len(waiters)}"

    entries = []
    for i, w in enumerate(waiters, 1):
        e = f"<b>{i}. {html.escape(str(w['name']))}</b>\n"
        e += f"Выручка: {fmt_money(w['revenue'])}\n"
        e += f"Заказов: {fmt_int(w['orders'])}, гостей: {fmt_int(w['guests'])}\n"
        e += f"Чек на гостя: {fmt_money(w['avg_check_guest'])}\n"
        e += f"Наполняемость: <b>{_safe_div(w['guests'], w['orders']):.2f}</b>\n"
        e += f"Блюд на гостя: {fmt_qntt(w['avg_kit_guest'])}, напитков на гостя: {fmt_qntt(w['avg_bar_guest'])}"
        entries.append(e)

    parts, current = [], head
    for e in entries:
        if len(current) + len(e) + 2 > limit:
            parts.append(current)
            current = e
        else:
            current += "\n\n" + e
    parts.append(current)
    return parts


# === 12. ТОЧКА ВХОДА (ручная проверка из консоли) ===
if __name__ == "__main__":
    import re
    print("Файл предназначен для импорта в bot.py. Ручной запуск - для проверки.")
    try:
        test_login = ""
        test_password = ""
        org_id, token, _ = authenticate_and_get_org_id(test_login, test_password)

        check_data(org_id=org_id, token=token)   # сверка цифр

        daily = fetch_full_analytics(org_id=org_id, token=token)
        for part in format_report_parts(daily):
            print(re.sub(r"</?b>", "", part) + "\n" + "=" * 30)

        rests = get_restaurants(token, org_id)
        if rests:
            w = fetch_waiters_report(org_id=org_id, token=token, rest_key=rests[0]["key"])
            for part in format_waiters_parts(w, rests[0]["name"]):
                print(re.sub(r"</?b>", "", part))
    except Exception as e:
        print(f"Ошибка: {e}")
        import traceback
        traceback.print_exc()

    # prod by D.Pankratov and M.Buanov