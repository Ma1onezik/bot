import re
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

# === 1. КОНФИГУРАЦИЯ ===
CONFIG = {
    "BASE_URL": "https://mozg.rest",
    "HALL_NAMES": [
        "Зал Ресторана", "Зал Аджикинежаль", "Зал Гостиница",
        "Зал Дровамука", "Летник",
    ],
    "DELIVERY_NAMES": ["Доставка", "Доставка Адж", "Доставка Дрова"],
    "TAKEOUT_NAMES": ["Вынос"],
    "HALL_IDS": ["9", "14", "15", "16", "17"],
    "DELIVERY_IDS": ["10", "1001", "1002"],
    "TAKEOUT_IDS": ["11"],
    "DEFAULT_REST_KEY": "7|2d153fbb-a2d8-40a5-a34c-5a88952d409a",
}

# === 2. АВТОРИЗАЦИЯ (JSON API) ===
def authenticate_and_get_org_id(login, password):
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/login"
    payload = {"login": login, "password": password}
    response = requests.post(url, json=payload)
    response.raise_for_status()
    data = response.json()

    if isinstance(data, list):
        token = data[0]["data"]["access_token"]
        refresh_token = data[0]["data"].get("refresh_token", "")
    else:
        token = data["data"]["access_token"]
        refresh_token = data["data"].get("refresh_token", "")

    if not token:
        raise Exception("Не удалось получить токен")

    org_url = f"{CONFIG['BASE_URL']}/api/v1/user/organizations"
    headers = {"Authorization": f"Bearer {token}"}
    org_response = requests.get(org_url, headers=headers)
    org_response.raise_for_status()
    org_data = org_response.json()
    items = org_data.get("data", {}).get("items", [])

    if not items:
        raise Exception("У пользователя нет доступа ни к одной организации")

    org_id = org_data.get("data", {}).get("currentOrganizationId")
    if not org_id and items:
        org_id = items[0].get("organizationId")

    if not org_id:
        raise Exception("Не удалось определить ID организации")

    print(f"✅ Авторизация успешна. Organization ID: {org_id}")
    return org_id, token, refresh_token


def get_access_token(login, password):
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/login"
    payload = {"login": login, "password": password}
    response = requests.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data[0]["data"]["access_token"]
    return data["data"]["access_token"]


def refresh_access_token(refresh_token):
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/refresh-tokens"
    payload = {"refresh_token": refresh_token}
    response = requests.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data[0]["data"]["access_token"]
    return data["data"]["access_token"]


# === 3. РЕСТОРАНЫ + ЗАЛ/ДОСТАВКА (JSON API) ===
def fetch_restaurants_data(token, org_id, date_from, date_to):
    """
    Запрашивает данные по ресторанам с разбивкой по подразделениям (hllg).
    Возвращает: {rest_name: {...}}
    """
    url = f"{CONFIG['BASE_URL']}/api/v1/sales/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "organization_id": org_id,
        "period": {"from": date_from, "to": date_to},
        "metrics": [
            "realsum", "realsum_kit", "realsum_bar",
            "orders", "guests",
            "cheque_guest", "cheque_order",
            "kit_qntt", "bar_qntt",
            "avg_kit_qntt_guest", "avg_bar_qntt_guest",
            "avg_kit_qntt_order", "avg_bar_qntt_order",
        ],
        "group_by": ["rest", "hllg"],
        "totals": False,
    }

    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        print(f"⚠️ Ошибка запроса для {date_from}: {response.status_code}")
        return {}

    data = response.json()
    rows = data.get("data", {}).get("rows", [])
    result = {}

    for row in rows:
        labels = row.get("labels", {})
        metrics = row.get("metrics", {})
        rest_name = labels.get("rest", "Неизвестно")
        hllg_name = labels.get("hllg", "")

        def safe_get(key):
            return metrics.get(key, {}).get("value", 0)

        revenue = safe_get("realsum")
        revenue_kit = safe_get("realsum_kit")
        revenue_bar = safe_get("realsum_bar")
        orders = safe_get("orders")
        guests = safe_get("guests")
        kit_qntt = safe_get("kit_qntt")
        bar_qntt = safe_get("bar_qntt")

        if rest_name not in result:
            result[rest_name] = {
                "revenue": 0,
                "revenue_hall": 0,
                "revenue_delivery": 0,
                "revenue_takeout": 0,
                "revenue_kit": 0,
                "revenue_bar": 0,
                "orders": 0,
                "guests": 0,
                "kit_qntt": 0,
                "bar_qntt": 0,
                "avg_check_guest": 0,
                "avg_check_order": 0,
                "avg_kit_order": 0,
                "avg_bar_order": 0,
                "avg_kit_guest": 0,
                "avg_bar_guest": 0,
                "plan": 0,
                "plan_guests": 0,
            }

        r = result[rest_name]
        r["revenue"] += revenue
        r["revenue_kit"] += revenue_kit
        r["revenue_bar"] += revenue_bar
        r["orders"] += orders
        r["guests"] += guests
        r["kit_qntt"] += kit_qntt
        r["bar_qntt"] += bar_qntt

        if hllg_name in CONFIG["HALL_NAMES"]:
            r["revenue_hall"] += revenue
        elif hllg_name in CONFIG["DELIVERY_NAMES"]:
            r["revenue_delivery"] += revenue
        elif hllg_name in CONFIG["TAKEOUT_NAMES"]:
            r["revenue_takeout"] += revenue

    # Пересчёт средних из абсолютных сумм
    for rest_name, r in result.items():
        if r["orders"] > 0:
            r["avg_check_order"] = r["revenue"] / r["orders"]
            r["avg_kit_order"] = r["kit_qntt"] / r["orders"]
            r["avg_bar_order"] = r["bar_qntt"] / r["orders"]
        if r["guests"] > 0:
            r["avg_check_guest"] = r["revenue"] / r["guests"]
            r["avg_kit_guest"] = r["kit_qntt"] / r["guests"]
            r["avg_bar_guest"] = r["bar_qntt"] / r["guests"]

    return result


# === 4. ПОИСК АКТУАЛЬНОЙ ДАТЫ ===
def find_actual_date(token, org_id):
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    today_data = fetch_restaurants_data(token, org_id, today_str, today_str)

    has_today_data = any(d.get("revenue", 0) > 0 for d in today_data.values())

    if has_today_data:
        print(f"✅ Найдены данные за сегодня: {today_str}")
        return today_str, "сегодня"

    yesterday = today - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    print(f"📅 Данных за сегодня нет, используем вчера: {yesterday_str}")
    return yesterday_str, "вчера"


# === 5. РАСЧЕТ ИЗМЕНЕНИЙ ===
def calc_change(current, previous):
    if previous == 0 and current == 0:
        return 0.0
    elif previous == 0 and current > 0:
        return 100.0
    elif previous > 0:
        return ((current - previous) / previous) * 100
    return 0.0


# === 6. ЕЖЕДНЕВНЫЙ ОТЧЕТ ===
def fetch_full_analytics(login="", password="", org_id=None, token=None):
    if not token:
        token = get_access_token(login, password)

    if not org_id:
        org_id, _, _ = authenticate_and_get_org_id(login, password)

    target_date_str, date_label = find_actual_date(token, org_id)
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")

    last_year_date = target_date - timedelta(days=365)
    last_year_str = last_year_date.strftime("%Y-%m-%d")

    first_day_of_month = target_date.replace(day=1).strftime("%Y-%m-%d")
    last_year_month_start = last_year_date.replace(day=1).strftime("%Y-%m-%d")

    print(f"📊 Собираем данные за {target_date.strftime('%d.%m.%Y')} ({date_label})...")

    target_data = fetch_restaurants_data(token, org_id, target_date_str, target_date_str)
    last_year_data = fetch_restaurants_data(token, org_id, last_year_str, last_year_str)
    month_data = fetch_restaurants_data(token, org_id, first_day_of_month, target_date_str)
    last_year_month_data = fetch_restaurants_data(token, org_id, last_year_month_start, last_year_str)

    if not target_data:
        raise Exception(f"Не удалось получить данные за {target_date_str}")

    all_restaurants = set(target_data.keys()) | set(last_year_data.keys()) | set(month_data.keys())
    all_restaurants = sorted([r for r in all_restaurants if r != "Итого"])

    restaurants_report = []

    for rest_name in all_restaurants:
        t = target_data.get(rest_name, {})
        ly = last_year_data.get(rest_name, {})
        m = month_data.get(rest_name, {})
        lym = last_year_month_data.get(rest_name, {})

        revenue = t.get("revenue", 0)
        revenue_kit = t.get("revenue_kit", 0)
        revenue_bar = t.get("revenue_bar", 0)
        orders = t.get("orders", 0)
        guests = t.get("guests", 0)
        avg_check_order = t.get("avg_check_order", 0)
        avg_check_guest = t.get("avg_check_guest", 0)
        avg_kit_guest = t.get("avg_kit_guest", 0)
        avg_bar_guest = t.get("avg_bar_guest", 0)
        plan = t.get("plan", 0)
        plan_guests = t.get("plan_guests", 0)

        revenue_last_year = ly.get("revenue", 0)
        guests_last_year = ly.get("guests", 0)
        orders_last_year = ly.get("orders", 0)
        avg_check_order_last_year = ly.get("avg_check_order", 0)
        avg_check_guest_last_year = ly.get("avg_check_guest", 0)

        revenue_month = m.get("revenue", 0)
        revenue_last_year_month = lym.get("revenue", 0)

        occupancy = (guests / orders) if orders > 0 else 0
        occupancy_last_year = (guests_last_year / orders_last_year) if orders_last_year > 0 else 0

        kit_percent = (revenue_kit / revenue * 100) if revenue > 0 else 0
        bar_percent = (revenue_bar / revenue * 100) if revenue > 0 else 0

        plan_completion = (revenue / plan * 100) if plan > 0 else 0
        plan_guests_completion = (guests / plan_guests * 100) if plan_guests > 0 else 0

        days_in_month = target_date.day
        avg_daily_current = revenue_month / days_in_month if days_in_month > 0 else 0
        avg_daily_last_year = revenue_last_year_month / days_in_month if days_in_month > 0 else 0

        restaurants_report.append({
            "name": rest_name,
            "date_label": date_label,
            "revenue": revenue,
            "revenue_hall": t.get("revenue_hall", 0),
            "revenue_delivery": t.get("revenue_delivery", 0),
            "revenue_takeout": t.get("revenue_takeout", 0),
            "revenue_kit": revenue_kit,
            "revenue_bar": revenue_bar,
            "kit_percent": kit_percent,
            "bar_percent": bar_percent,
            "orders": orders,
            "guests": guests,
            "avg_check_order": avg_check_order,
            "avg_check_guest": avg_check_guest,
            "occupancy": occupancy,
            "avg_kit_guest": avg_kit_guest,
            "avg_bar_guest": avg_bar_guest,
            "plan": plan,
            "plan_guests": plan_guests,
            "plan_completion": plan_completion,
            "plan_guests_completion": plan_guests_completion,
            "revenue_last_year": revenue_last_year,
            "revenue_change": calc_change(revenue, revenue_last_year),
            "guests_last_year": guests_last_year,
            "guests_change": calc_change(guests, guests_last_year),
            "orders_last_year": orders_last_year,
            "orders_change": calc_change(orders, orders_last_year),
            "avg_check_order_last_year": avg_check_order_last_year,
            "avg_check_order_change": calc_change(avg_check_order, avg_check_order_last_year),
            "avg_check_guest_last_year": avg_check_guest_last_year,
            "avg_check_guest_change": calc_change(avg_check_guest, avg_check_guest_last_year),
            "occupancy_last_year": occupancy_last_year,
            "occupancy_change": calc_change(occupancy, occupancy_last_year),
            "revenue_month": revenue_month,
            "avg_daily_current": avg_daily_current,
            "avg_daily_last_year": avg_daily_last_year,
            "year_month_change": calc_change(avg_daily_current, avg_daily_last_year),
        })

    return {
        "date": target_date_str,
        "date_label": date_label,
        "restaurants": restaurants_report,
    }


# === 7. НЕДЕЛЬНЫЙ ОТЧЕТ ===
def fetch_weekly_analytics(login="", password="", org_id=None, token=None):
    if not token:
        token = get_access_token(login, password)

    if not org_id:
        org_id, _, _ = authenticate_and_get_org_id(login, password)

    today = datetime.now()
    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=6)

    last_year_end = end_date - timedelta(days=365)
    last_year_start = start_date - timedelta(days=365)

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    last_year_start_str = last_year_start.strftime("%Y-%m-%d")
    last_year_end_str = last_year_end.strftime("%Y-%m-%d")

    current_week_data = fetch_restaurants_data(token, org_id, start_str, end_str)
    last_year_week_data = fetch_restaurants_data(token, org_id, last_year_start_str, last_year_end_str)

    if not current_week_data:
        raise Exception("Не удалось получить данные за текущую неделю")

    all_restaurants = set(current_week_data.keys()) | set(last_year_week_data.keys())
    all_restaurants = sorted([r for r in all_restaurants if r != "Итого"])

    restaurants_report = []

    for rest_name in all_restaurants:
        c = current_week_data.get(rest_name, {})
        ly = last_year_week_data.get(rest_name, {})

        revenue = c.get("revenue", 0)
        revenue_kit = c.get("revenue_kit", 0)
        revenue_bar = c.get("revenue_bar", 0)
        orders = c.get("orders", 0)
        guests = c.get("guests", 0)
        avg_check_order = c.get("avg_check_order", 0)
        avg_check_guest = c.get("avg_check_guest", 0)
        avg_kit_guest = c.get("avg_kit_guest", 0)
        avg_bar_guest = c.get("avg_bar_guest", 0)
        plan = c.get("plan", 0)
        plan_guests = c.get("plan_guests", 0)

        revenue_last_year = ly.get("revenue", 0)
        orders_last_year = ly.get("orders", 0)
        guests_last_year = ly.get("guests", 0)
        avg_check_order_last_year = ly.get("avg_check_order", 0)
        avg_check_guest_last_year = ly.get("avg_check_guest", 0)

        occupancy = (guests / orders) if orders > 0 else 0
        occupancy_last_year = (guests_last_year / orders_last_year) if orders_last_year > 0 else 0

        kit_percent = (revenue_kit / revenue * 100) if revenue > 0 else 0
        bar_percent = (revenue_bar / revenue * 100) if revenue > 0 else 0

        plan_completion = (revenue / plan * 100) if plan > 0 else 0
        plan_guests_completion = (guests / plan_guests * 100) if plan_guests > 0 else 0

        avg_daily_revenue = revenue / 7
        avg_daily_revenue_last_year = revenue_last_year / 7

        restaurants_report.append({
            "name": rest_name,
            "start_date": start_str,
            "end_date": end_str,
            "revenue": revenue,
            "revenue_hall": c.get("revenue_hall", 0),
            "revenue_delivery": c.get("revenue_delivery", 0),
            "revenue_takeout": c.get("revenue_takeout", 0),
            "revenue_kit": revenue_kit,
            "revenue_bar": revenue_bar,
            "kit_percent": kit_percent,
            "bar_percent": bar_percent,
            "orders": orders,
            "guests": guests,
            "avg_check_order": avg_check_order,
            "avg_check_guest": avg_check_guest,
            "occupancy": occupancy,
            "avg_kit_guest": avg_kit_guest,
            "avg_bar_guest": avg_bar_guest,
            "plan": plan,
            "plan_guests": plan_guests,
            "plan_completion": plan_completion,
            "plan_guests_completion": plan_guests_completion,
            "revenue_last_year": revenue_last_year,
            "revenue_change": calc_change(revenue, revenue_last_year),
            "orders_last_year": orders_last_year,
            "orders_change": calc_change(orders, orders_last_year),
            "guests_last_year": guests_last_year,
            "guests_change": calc_change(guests, guests_last_year),
            "avg_check_order_last_year": avg_check_order_last_year,
            "avg_check_order_change": calc_change(avg_check_order, avg_check_order_last_year),
            "avg_check_guest_last_year": avg_check_guest_last_year,
            "avg_check_guest_change": calc_change(avg_check_guest, avg_check_guest_last_year),
            "occupancy_last_year": occupancy_last_year,
            "occupancy_change": calc_change(occupancy, occupancy_last_year),
            "avg_daily_revenue": avg_daily_revenue,
            "avg_daily_revenue_last_year": avg_daily_revenue_last_year,
            "avg_daily_change": calc_change(avg_daily_revenue, avg_daily_revenue_last_year),
        })

    return {
        "start_date": start_str,
        "end_date": end_str,
        "last_year_start": last_year_start_str,
        "last_year_end": last_year_end_str,
        "restaurants": restaurants_report,
    }


# === 8. ОФИЦИАНТЫ — HTML form API ===
def _login_to_form_api(login, password):
    """Логин через форму Mozg (cookie-сессия)."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/154.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": CONFIG["BASE_URL"],
        "Referer": f"{CONFIG['BASE_URL']}/auth",
    })

    response = session.post(
        f"{CONFIG['BASE_URL']}/?form=logreg",
        data={
            "login": login,
            "pass": password,
            "responseType": "json",
            "log": "1",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        allow_redirects=True,
    )
    response.raise_for_status()

    if "PHPSESSID" not in session.cookies:
        raise Exception(
            "Логин не удался: сервер не вернул PHPSESSID. "
            f"HTTP {response.status_code}, ответ: {response.text[:300]}"
        )

    print(f"✅ Cookie-сессия получена (PHPSESSID={session.cookies.get('PHPSESSID')})")
    return session


def fetch_waiters_html(session, rest_key, date_from, date_to):
    """
    Запрашивает HTML-отчёт rep_empl_sales_effect через cookie-сессию.
    date_from / date_to — в формате DD.MM.YYYY
    rest_key — например "7|2d153fbb-a2d8-40a5-a34c-5a88952d409a"
    """
    data = [
        ("finc_rest[]", rest_key),
        ("rprt_select", "rep_empl_sales_effect"),
        ("from_analyser", "1"),
        ("empl_grouping", "empl"),
        ("avgType", "guest"),
        ("pk", "1"),
        ("rate", "50-30-20"),
        ("pstn", ""),
        ("finc_crrg_type[]", "1"),
        ("date_type", "order_date"),
        ("finc_from", date_from),
        ("finc_to", date_to),
        ("action", "load"),
    ]
    for hid in CONFIG["HALL_IDS"] + CONFIG["DELIVERY_IDS"]:
        data.append(("finc_hllg[]", hid))

    response = session.post(
        f"{CONFIG['BASE_URL']}/?form=rep_empl_sales_effect",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.raise_for_status()

    try:
        json_response = response.json()
        return json_response.get("report", "")
    except Exception:
        return response.text


def _parse_waiters_html(html):
    """Парсит HTML-таблицу официантов."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=re.compile(r"rep_empl_sales_effect\d+table"))
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    waiters = []
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 15:
            continue
        if not tds[0].find("input", class_="check-row"):
            continue

        def to_num(s):
            if not s or s == "-":
                return 0.0
            s = s.replace("\xa0", "").replace(" ", "").replace(",", ".")
            try:
                return float(s)
            except ValueError:
                return 0.0

        waiters.append({
            "name": tds[2].get_text(strip=True),
            "revenue": to_num(tds[3].get_text(strip=True)),
            "avg_kit_guest": to_num(tds[13].get_text(strip=True)),
            "avg_bar_guest": to_num(tds[14].get_text(strip=True)),
        })

    waiters.sort(key=lambda w: w["revenue"], reverse=True)
    return waiters


def fetch_waiters_analytics(login="", password="", org_id=None, token=None,
                             period="today", rest_key=None):
    """
    Отчёт по официантам через HTML form API.
    period: "today" | "week"
    """
    if period == "today":
        if not token:
            token = get_access_token(login, password)
        target_date_str, date_label = find_actual_date(token, org_id)
        d = datetime.strptime(target_date_str, "%Y-%m-%d")
        date_from = date_to = d.strftime("%d.%m.%Y")
        iso_from = iso_to = target_date_str
    else:
        today = datetime.now()
        end_d = today - timedelta(days=1)
        start_d = end_d - timedelta(days=6)
        date_from = start_d.strftime("%d.%m.%Y")
        date_to = end_d.strftime("%d.%m.%Y")
        iso_from = start_d.strftime("%Y-%m-%d")
        iso_to = end_d.strftime("%Y-%m-%d")
        date_label = "за неделю"

    if not rest_key:
        rest_key = CONFIG["DEFAULT_REST_KEY"]

    print(f"👨‍🍳 Собираем данные по сотрудникам ({date_from} — {date_to})...")

    try:
        session = _login_to_form_api(login, password)
        html = fetch_waiters_html(session, rest_key, date_from, date_to)
        waiters = _parse_waiters_html(html)
    except Exception as e:
        print(f"⚠️ Ошибка официантов: {e}")
        return {
            "date_from": iso_from,
            "date_to": iso_to,
            "date_label": date_label,
            "waiters": [],
            "error": str(e),
        }

    return {
        "date_from": iso_from,
        "date_to": iso_to,
        "date_label": date_label,
        "waiters": waiters,
    }


# === 9. ФОРМАТИРОВАНИЕ ЕЖЕДНЕВНОГО ОТЧЕТА ===
def format_report(analytics):
    def money(v):
        return f"{v:,.0f} ₽".replace(",", " ")

    def pct(v):
        return f"{v:+.1f}%"

    def q(v):
        return f"{v:.2f}"

    date_str = datetime.strptime(analytics['date'], '%Y-%m-%d').strftime('%d.%m.%Y')
    label = analytics['date_label']

    lines = [f"📊 *Аналитика за {date_str}* ({label})\n"]

    for rest in analytics['restaurants']:
        lines.append(f"🏠 *{rest['name']}*")
        lines.append(
            f"💵 *{money(rest['revenue'])}* ({pct(rest['revenue_change'])} г/г)\n"
            f"   🍽 Зал: {money(rest['revenue_hall'])}\n"
            f"   🛵 Доставка: {money(rest['revenue_delivery'])}\n"
            f"   🥡 Вынос: {money(rest.get('revenue_takeout', 0))}\n"
            f"   🍴 Блюда: {money(rest['revenue_kit'])} ({rest['kit_percent']:.0f}%)\n"
            f"   🍷 Напитки: {money(rest['revenue_bar'])} ({rest['bar_percent']:.0f}%)"
        )

        lines.append(
            f"\n🧾 Заказов: *{rest['orders']}* ({pct(rest['orders_change'])})\n"
            f"👥 Гостей: *{rest['guests']}* ({pct(rest['guests_change'])})\n"
            f"💳 Чек/заказ: {money(rest['avg_check_order'])}\n"
            f"👤 Чек/гость: {money(rest['avg_check_guest'])}\n"
            f"🍽 Блюд/гость: {q(rest['avg_kit_guest'])}\n"
            f"🍷 Напитков/гость: {q(rest['avg_bar_guest'])}"
        )

        if rest['plan'] > 0:
            plan_pct = rest['plan_completion']
            mark = "✅" if plan_pct >= 100 else "⚠️"
            lines.append(f"\n🎯 План: {money(rest['plan'])} — {mark} *{plan_pct:.1f}%*")

        lines.append(
            f"\n📅 Месяц: {money(rest['revenue_month'])}\n"
            f"📈 Ср/сут: {money(rest['avg_daily_current'])} ({pct(rest['year_month_change'])})"
        )
        lines.append("")

    return "\n".join(lines)


# === 10. ФОРМАТИРОВАНИЕ НЕДЕЛЬНОГО ОТЧЕТА ===
def format_weekly_report(analytics):
    def money(v):
        return f"{v:,.0f} ₽".replace(",", " ")

    def pct(v):
        return f"{v:+.1f}%"

    def q(v):
        return f"{v:.2f}"

    start_date = datetime.strptime(analytics['start_date'], '%Y-%m-%d').strftime('%d.%m')
    end_date = datetime.strptime(analytics['end_date'], '%Y-%m-%d').strftime('%d.%m.%Y')
    ly_start = datetime.strptime(analytics['last_year_start'], '%Y-%m-%d').strftime('%d.%m')
    ly_end = datetime.strptime(analytics['last_year_end'], '%Y-%m-%d').strftime('%d.%m.%Y')

    lines = [
        f"📊 *Недельная аналитика*",
        f"📅 *Период:* {start_date} – {end_date}",
        f"📅 *Сравнение:* {ly_start} – {ly_end} (прошлый год)",
        ""
    ]

    for rest in analytics['restaurants']:
        lines.append(f"🏠 *{rest['name']}*")
        lines.append(
            f"💵 *{money(rest['revenue'])}* ({pct(rest['revenue_change'])} г/г)\n"
            f"   🍽 Зал: {money(rest['revenue_hall'])}\n"
            f"   🛵 Доставка: {money(rest['revenue_delivery'])}\n"
            f"   🥡 Вынос: {money(rest.get('revenue_takeout', 0))}\n"
            f"   🍴 Блюда: {money(rest['revenue_kit'])} ({rest['kit_percent']:.0f}%)\n"
            f"   🍷 Напитки: {money(rest['revenue_bar'])} ({rest['bar_percent']:.0f}%)"
        )

        lines.append(
            f"\n🧾 Заказов: *{rest['orders']}* ({pct(rest['orders_change'])})\n"
            f"👥 Гостей: *{rest['guests']}* ({pct(rest['guests_change'])})\n"
            f"💳 Чек/заказ: {money(rest['avg_check_order'])}\n"
            f"👤 Чек/гость: {money(rest['avg_check_guest'])}\n"
            f"🍽 Блюд/гость: {q(rest['avg_kit_guest'])}\n"
            f"🍷 Напитков/гость: {q(rest['avg_bar_guest'])}"
        )

        lines.append(f"\n📈 Ср/сут: {money(rest['avg_daily_revenue'])} ({pct(rest['avg_daily_change'])})")

        if rest['plan'] > 0:
            plan_pct = rest['plan_completion']
            mark = "✅" if plan_pct >= 100 else "⚠️"
            lines.append(f"\n🎯 План: {money(rest['plan'])} — {mark} *{plan_pct:.1f}%*")

        lines.append("")

    return "\n".join(lines)


# === 11. ФОРМАТИРОВАНИЕ ОТЧЕТА ПО СОТРУДНИКАМ ===
def format_waiters_report(analytics):
    def money(v):
        return f"{v:,.0f} ₽".replace(",", " ")

    def q(v):
        return f"{v:.2f}"

    date_from = datetime.strptime(analytics['date_from'], '%Y-%m-%d').strftime('%d.%m.%Y')
    date_to = datetime.strptime(analytics['date_to'], '%Y-%m-%d').strftime('%d.%m.%Y')
    label = analytics.get('date_label', '')

    if analytics['date_from'] == analytics['date_to']:
        header = f"👨‍🍳 *Сотрудники за {date_from}* ({label})"
    else:
        header = f"👨‍🍳 *Сотрудники за {date_from} – {date_to}*"

    lines = [header, ""]

    if analytics.get("error"):
        lines.append(f"⚠️ {analytics['error']}")
        return "\n".join(lines)

    waiters = analytics.get("waiters") or []
    if not waiters:
        lines.append("Нет данных по сотрудникам за выбранный период.")
        return "\n".join(lines)

    for i, w in enumerate(waiters, 1):
        lines.append(
            f"{i}. *{w['name']}*\n"
            f"   💰 {money(w['revenue'])}\n"
            f"   🍽 {q(w['avg_kit_guest'])} блюд / гостя\n"
            f"   🍷 {q(w['avg_bar_guest'])} напитков / гостя"
        )

    return "\n".join(lines)


# === 12. ТОЧКА ВХОДА ===
if __name__ == "__main__":
    print("🚀 Запуск сбора аналитики...")

    try:
        test_login = " "
        test_password = " "

        org_id, token, _ = authenticate_and_get_org_id(test_login, test_password)

        print("\n" + "=" * 50)
        print("📊 ЕЖЕДНЕВНЫЙ ОТЧЕТ")
        print("=" * 50)
        daily = fetch_full_analytics(login=test_login, password=test_password, org_id=org_id, token=token)
        print(format_report(daily))

        print("\n" + "=" * 50)
        print("📊 НЕДЕЛЬНЫЙ ОТЧЕТ")
        print("=" * 50)
        weekly = fetch_weekly_analytics(login=test_login, password=test_password, org_id=org_id, token=token)
        print(format_weekly_report(weekly))

        print("\n" + "=" * 50)
        print("👨‍🍳 ОТЧЕТ ПО СОТРУДНИКАМ (сегодня)")
        print("=" * 50)
        waiters_today = fetch_waiters_analytics(login=test_login, password=test_password, org_id=org_id, token=token, period="today")
        print(format_waiters_report(waiters_today))

        print("\n" + "=" * 50)
        print("👨‍🍳 ОТЧЕТ ПО СОТРУДНИКАМ (неделя)")
        print("=" * 50)
        waiters_week = fetch_waiters_analytics(login=test_login, password=test_password, org_id=org_id, token=token, period="week")
        print(format_waiters_report(waiters_week))

    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()


    # prod by D.Pankratov and M.Buanov