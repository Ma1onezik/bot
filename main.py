import requests
from datetime import datetime, timedelta

# === 1. КОНФИГУРАЦИЯ ===
CONFIG = {
    "BASE_URL": "https://mozg.rest",
}

# === 2. АВТОРИЗАЦИЯ ===
def authenticate_and_get_org_id(login, password):
    """
    Авторизация в Mozg и получение ID организации.
    Возвращает (organization_id, access_token, refresh_token).
    """
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/login"
    payload = {
        "login": login,
        "password": password
    }
    
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
    """Получение JWT токена через API."""
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/login"
    payload = {
        "login": login,
        "password": password
    }
    
    response = requests.post(url, json=payload)
    response.raise_for_status()
    
    data = response.json()
    if isinstance(data, list):
        return data[0]["data"]["access_token"]
    return data["data"]["access_token"]


def refresh_access_token(refresh_token):
    """Обновление access_token с помощью refresh_token."""
    url = f"{CONFIG['BASE_URL']}/api/v1/auth/refresh-tokens"
    payload = {
        "refresh_token": refresh_token
    }
    
    response = requests.post(url, json=payload)
    response.raise_for_status()
    
    data = response.json()
    if isinstance(data, list):
        return data[0]["data"]["access_token"]
    return data["data"]["access_token"]


# === 3. ЗАПРОС ДАННЫХ ===
def fetch_restaurants_data(token, org_id, date_from, date_to):
    """Запрашивает данные по всем ресторанам за указанный период."""
    url = f"{CONFIG['BASE_URL']}/api/v1/sales/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "organization_id": org_id,
        "period": {"from": date_from, "to": date_to},
        "metrics": [
            "realsum",
            "realsum_kit",
            "realsum_bar",
            "orders",
            "guests",
            "cheque_guest",
            "cheque_order",
            "avg_kit_qntt_order",
            "avg_bar_qntt_order",
            "avg_kit_qntt_guest",
            "avg_bar_qntt_guest",
            "plan",
            "plan_guests",
        ],
        "group_by": ["rest"],
        "totals": True
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
        
        def safe_get(key):
            return metrics.get(key, {}).get("value", 0)
        
        # Получаем значения
        revenue = safe_get("realsum")
        revenue_kit = safe_get("realsum_kit")
        revenue_bar = safe_get("realsum_bar")
        orders = safe_get("orders")
        guests = safe_get("guests")
        
        # Резервный расчет средних чеков, если API не вернул
        avg_check_order = safe_get("cheque_order")
        if avg_check_order == 0 and orders > 0 and revenue > 0:
            avg_check_order = revenue / orders
        
        avg_check_guest = safe_get("cheque_guest")
        if avg_check_guest == 0 and guests > 0 and revenue > 0:
            avg_check_guest = revenue / guests
        
        result[rest_name] = {
            "revenue": revenue,
            "revenue_kit": revenue_kit,
            "revenue_bar": revenue_bar,
            "orders": orders,
            "guests": guests,
            "avg_check_guest": avg_check_guest,
            "avg_check_order": avg_check_order,
            "avg_kit_order": safe_get("avg_kit_qntt_order"),
            "avg_bar_order": safe_get("avg_bar_qntt_order"),
            "avg_kit_guest": safe_get("avg_kit_qntt_guest"),
            "avg_bar_guest": safe_get("avg_bar_qntt_guest"),
            "plan": safe_get("plan"),
            "plan_guests": safe_get("plan_guests"),
        }
    
    return result


# === 4. ПОИСК АКТУАЛЬНОЙ ДАТЫ ===
def find_actual_date(token, org_id):
    """Находит последнюю дату с ненулевой выручкой."""
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    today_data = fetch_restaurants_data(token, org_id, today_str, today_str)
    
    has_today_data = False
    for rest_data in today_data.values():
        if rest_data.get("revenue", 0) > 0:
            has_today_data = True
            break
    
    if has_today_data:
        print(f"✅ Найдены данные за сегодня: {today_str}")
        return today_str, "сегодня"
    
    yesterday = today - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    print(f"📅 Данных за сегодня нет, используем вчера: {yesterday_str}")
    return yesterday_str, "вчера"


# === 5. РАСЧЕТ ИЗМЕНЕНИЙ ===
def calc_change(current, previous):
    """Безопасный расчет изменения в процентах."""
    if previous == 0 and current == 0:
        return 0.0
    elif previous == 0 and current > 0:
        return 100.0
    elif previous > 0:
        return ((current - previous) / previous) * 100
    return 0.0


# === 6. ЕЖЕДНЕВНЫЙ ОТЧЕТ ===
def fetch_full_analytics(login="", password="", org_id=None, token=None):
    """Собирает данные за актуальную дату с сравнениями."""
    if not token:
        if not login or not password:
            raise Exception("Для авторизации нужны логин и пароль или токен")
        token = get_access_token(login, password)
    
    if not org_id:
        if not login or not password:
            raise Exception("Для определения организации нужны логин и пароль")
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
        target = target_data.get(rest_name, {})
        last_year = last_year_data.get(rest_name, {})
        month = month_data.get(rest_name, {})
        last_year_month = last_year_month_data.get(rest_name, {})
        
        # Текущие данные
        revenue = target.get("revenue", 0)
        revenue_kit = target.get("revenue_kit", 0)
        revenue_bar = target.get("revenue_bar", 0)
        orders = target.get("orders", 0)
        guests = target.get("guests", 0)
        avg_check_order = target.get("avg_check_order", 0)
        avg_check_guest = target.get("avg_check_guest", 0)
        avg_kit_order = target.get("avg_kit_order", 0)
        avg_bar_order = target.get("avg_bar_order", 0)
        avg_kit_guest = target.get("avg_kit_guest", 0)
        avg_bar_guest = target.get("avg_bar_guest", 0)
        plan = target.get("plan", 0)
        plan_guests = target.get("plan_guests", 0)
        
        # Данные за прошлый год
        revenue_last_year = last_year.get("revenue", 0)
        guests_last_year = last_year.get("guests", 0)
        orders_last_year = last_year.get("orders", 0)
        avg_check_order_last_year = last_year.get("avg_check_order", 0)
        avg_check_guest_last_year = last_year.get("avg_check_guest", 0)
        
        # Данные за месяц
        revenue_month = month.get("revenue", 0)
        revenue_last_year_month = last_year_month.get("revenue", 0)
        
        # Расчеты
        occupancy = (guests / orders) if orders > 0 else 0
        occupancy_last_year = (guests_last_year / orders_last_year) if orders_last_year > 0 else 0
        
        kit_percent = (revenue_kit / revenue * 100) if revenue > 0 else 0
        bar_percent = (revenue_bar / revenue * 100) if revenue > 0 else 0
        
        # Выполнение плана (в процентах)
        plan_completion = (revenue / plan * 100) if plan > 0 else 0
        plan_guests_completion = (guests / plan_guests * 100) if plan_guests > 0 else 0
        
        # Среднесуточная выручка
        days_in_month = target_date.day
        avg_daily_current = revenue_month / days_in_month if days_in_month > 0 else 0
        avg_daily_last_year = revenue_last_year_month / days_in_month if days_in_month > 0 else 0
        
        restaurants_report.append({
            "name": rest_name,
            "date_label": date_label,
            # Текущие показатели
            "revenue": revenue,
            "revenue_kit": revenue_kit,
            "revenue_bar": revenue_bar,
            "kit_percent": kit_percent,
            "bar_percent": bar_percent,
            "orders": orders,
            "guests": guests,
            "avg_check_order": avg_check_order,
            "avg_check_guest": avg_check_guest,
            "occupancy": occupancy,
            "avg_kit_order": avg_kit_order,
            "avg_bar_order": avg_bar_order,
            "avg_kit_guest": avg_kit_guest,
            "avg_bar_guest": avg_bar_guest,
            # План
            "plan": plan,
            "plan_guests": plan_guests,
            "plan_completion": plan_completion,
            "plan_guests_completion": plan_guests_completion,
            # Сравнение с прошлым годом
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
            # Месячные показатели
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
    """
    Собирает данные за последние 7 дней с сравнением с аналогичной неделей прошлого года.
    """
    if not token:
        if not login or not password:
            raise Exception("Для авторизации нужны логин и пароль или токен")
        token = get_access_token(login, password)
    
    if not org_id:
        if not login or not password:
            raise Exception("Для определения организации нужны логин и пароль")
        org_id, _, _ = authenticate_and_get_org_id(login, password)
    
    # Определяем периоды
    today = datetime.now()
    end_date = today - timedelta(days=1)  # Вчера (чтобы данные были полные)
    start_date = end_date - timedelta(days=6)  # 7 дней включая вчера
    
    # Та же неделя в прошлом году
    last_year_end = end_date - timedelta(days=365)
    last_year_start = start_date - timedelta(days=365)
    
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    last_year_start_str = last_year_start.strftime("%Y-%m-%d")
    last_year_end_str = last_year_end.strftime("%Y-%m-%d")
    
    print(f"📊 Собираем данные за неделю {start_str} - {end_str}...")
    print(f"📊 Сравнение с неделей {last_year_start_str} - {last_year_end_str} прошлого года...")
    
    # Получаем данные
    current_week_data = fetch_restaurants_data(token, org_id, start_str, end_str)
    last_year_week_data = fetch_restaurants_data(token, org_id, last_year_start_str, last_year_end_str)
    
    if not current_week_data:
        raise Exception("Не удалось получить данные за текущую неделю")
    
    # Собираем все рестораны
    all_restaurants = set(current_week_data.keys()) | set(last_year_week_data.keys())
    all_restaurants = sorted([r for r in all_restaurants if r != "Итого"])
    
    restaurants_report = []
    
    for rest_name in all_restaurants:
        current = current_week_data.get(rest_name, {})
        last_year = last_year_week_data.get(rest_name, {})
        
        # Текущая неделя
        revenue = current.get("revenue", 0)
        revenue_kit = current.get("revenue_kit", 0)
        revenue_bar = current.get("revenue_bar", 0)
        orders = current.get("orders", 0)
        guests = current.get("guests", 0)
        avg_check_order = current.get("avg_check_order", 0)
        avg_check_guest = current.get("avg_check_guest", 0)
        avg_kit_order = current.get("avg_kit_order", 0)
        avg_bar_order = current.get("avg_bar_order", 0)
        avg_kit_guest = current.get("avg_kit_guest", 0)
        avg_bar_guest = current.get("avg_bar_guest", 0)
        plan = current.get("plan", 0)
        plan_guests = current.get("plan_guests", 0)
        
        # Прошлый год
        revenue_last_year = last_year.get("revenue", 0)
        orders_last_year = last_year.get("orders", 0)
        guests_last_year = last_year.get("guests", 0)
        avg_check_order_last_year = last_year.get("avg_check_order", 0)
        avg_check_guest_last_year = last_year.get("avg_check_guest", 0)
        
        # Расчеты
        occupancy = (guests / orders) if orders > 0 else 0
        occupancy_last_year = (guests_last_year / orders_last_year) if orders_last_year > 0 else 0
        
        kit_percent = (revenue_kit / revenue * 100) if revenue > 0 else 0
        bar_percent = (revenue_bar / revenue * 100) if revenue > 0 else 0
        
        # Выполнение плана (за неделю)
        plan_completion = (revenue / plan * 100) if plan > 0 else 0
        plan_guests_completion = (guests / plan_guests * 100) if plan_guests > 0 else 0
        
        # Среднедневные показатели за неделю
        days_in_week = 7
        avg_daily_revenue = revenue / days_in_week if days_in_week > 0 else 0
        avg_daily_revenue_last_year = revenue_last_year / days_in_week if days_in_week > 0 else 0
        
        restaurants_report.append({
            "name": rest_name,
            "start_date": start_str,
            "end_date": end_str,
            # Текущие показатели
            "revenue": revenue,
            "revenue_kit": revenue_kit,
            "revenue_bar": revenue_bar,
            "kit_percent": kit_percent,
            "bar_percent": bar_percent,
            "orders": orders,
            "guests": guests,
            "avg_check_order": avg_check_order,
            "avg_check_guest": avg_check_guest,
            "occupancy": occupancy,
            "avg_kit_order": avg_kit_order,
            "avg_bar_order": avg_bar_order,
            "avg_kit_guest": avg_kit_guest,
            "avg_bar_guest": avg_bar_guest,
            # План
            "plan": plan,
            "plan_guests": plan_guests,
            "plan_completion": plan_completion,
            "plan_guests_completion": plan_guests_completion,
            # Сравнение с прошлым годом
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
            # Среднедневные
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


# === 8. ФОРМАТИРОВАНИЕ ЕЖЕДНЕВНОГО ОТЧЕТА ===
def format_report(analytics):
    """Форматирует ежедневный отчет с разбивкой по ресторанам."""
    
    def fmt_money(value):
        return f"{value:,.0f} руб."
    
    def fmt_percent(value):
        return f"{value:+.2f}%"
    
    def fmt_qntt(value):
        return f"{value:.2f} шт."
    
    date_str = datetime.strptime(analytics['date'], '%Y-%m-%d').strftime('%d.%m.%Y')
    date_label = analytics['date_label']
    
    report = f"📊 **Аналитика за {date_str} ({date_label})**\n"
    report += "═" * 40 + "\n\n"
    
    for idx, rest in enumerate(analytics['restaurants']):
        if idx > 0:
            report += "─" * 40 + "\n\n"
        
        report += f"🏠 **{rest['name']}**\n"
        report += "─" * 30 + "\n"
        
        report += f"💰 **Выручка:** {fmt_money(rest['revenue'])}\n"
        report += f"   {fmt_percent(rest['revenue_change'])} к {date_str} прошлого года\n"
        report += f"   🍽️ Блюда: {fmt_money(rest['revenue_kit'])} ({rest['kit_percent']:.1f}%)\n"
        report += f"   🍷 Напитки: {fmt_money(rest['revenue_bar'])} ({rest['bar_percent']:.1f}%)\n"
        report += "\n"
        
        report += f"🧾 **Заказов:** {rest['orders']} ({fmt_percent(rest['orders_change'])} к прошлому году)\n"
        report += f"👥 **Гостей:** {rest['guests']} ({fmt_percent(rest['guests_change'])})\n"
        report += f"💳 **Ср. чек (заказ):** {fmt_money(rest['avg_check_order'])} ({fmt_percent(rest['avg_check_order_change'])})\n"
        report += f"👤 **Ср. чек (гость):** {fmt_money(rest['avg_check_guest'])} ({fmt_percent(rest['avg_check_guest_change'])})\n"
        report += f"👥 **Наполняемость:** {rest['occupancy']:.2f} ({fmt_percent(rest['occupancy_change'])})\n"
        report += "\n"
        
        report += f"🍽️ **Ср. блюд на заказ:** {fmt_qntt(rest['avg_kit_order'])}\n"
        report += f"🍷 **Ср. напитков на заказ:** {fmt_qntt(rest['avg_bar_order'])}\n"
        report += f"🍽️ **Ср. блюд на гостя:** {fmt_qntt(rest['avg_kit_guest'])}\n"
        report += f"🍷 **Ср. напитков на гостя:** {fmt_qntt(rest['avg_bar_guest'])}\n"
        report += "\n"
        
        # План
        if rest['plan'] > 0:
            report += f"🎯 **План на день:** {fmt_money(rest['plan'])}\n"
            plan_pct = rest['plan_completion']
            plan_diff = plan_pct - 100
            report += f"📊 **Выполнение плана:** {plan_pct:.2f}%"
            if plan_diff > 0:
                report += f" (перевыполнение на {plan_diff:+.2f}%)"
            elif plan_diff < 0:
                report += f" (недовыполнение на {plan_diff:+.2f}%)"
            else:
                report += " (план выполнен точно)"
            report += "\n"
        
        if rest['plan_guests'] > 0:
            report += f"🎯 **План по гостям:** {rest['plan_guests']:.2f}\n"
            plan_guest_pct = rest['plan_guests_completion']
            plan_guest_diff = plan_guest_pct - 100
            report += f"📊 **Выполнение плана:** {plan_guest_pct:.2f}%"
            if plan_guest_diff > 0:
                report += f" (перевыполнение на {plan_guest_diff:+.2f}%)"
            elif plan_guest_diff < 0:
                report += f" (недовыполнение на {plan_guest_diff:+.2f}%)"
            else:
                report += " (план выполнен точно)"
            report += "\n"
        
        report += "\n"
        
        report += f"📊 **Выручка за месяц:** {fmt_money(rest['revenue_month'])}\n"
        report += f"📈 **Среднесут. (тек. месяц):** {fmt_money(rest['avg_daily_current'])}\n"
        report += f"📉 **Среднесут. (пр. год):** {fmt_money(rest['avg_daily_last_year'])}\n"
        report += f"🔁 **Изменение к пр. году:** {fmt_percent(rest['year_month_change'])}\n"
    
    return report


# === 9. ФОРМАТИРОВАНИЕ НЕДЕЛЬНОГО ОТЧЕТА ===
def format_weekly_report(analytics):
    """Форматирует недельный отчет с разбивкой по ресторанам."""
    
    def fmt_money(value):
        return f"{value:,.0f} руб."
    
    def fmt_percent(value):
        return f"{value:+.2f}%"
    
    def fmt_qntt(value):
        return f"{value:.2f} шт."
    
    start_date = datetime.strptime(analytics['start_date'], '%Y-%m-%d').strftime('%d.%m')
    end_date = datetime.strptime(analytics['end_date'], '%Y-%m-%d').strftime('%d.%m.%Y')
    last_year_start = datetime.strptime(analytics['last_year_start'], '%Y-%m-%d').strftime('%d.%m')
    last_year_end = datetime.strptime(analytics['last_year_end'], '%Y-%m-%d').strftime('%d.%m.%Y')
    
    report = f"📊 **Недельная аналитика**\n"
    report += f"📅 **Период:** {start_date} – {end_date}\n"
    report += f"📅 **Сравнение:** {last_year_start} – {last_year_end} (прошлый год)\n"
    report += "═" * 40 + "\n\n"
    
    for idx, rest in enumerate(analytics['restaurants']):
        if idx > 0:
            report += "─" * 40 + "\n\n"
        
        report += f"🏠 **{rest['name']}**\n"
        report += "─" * 30 + "\n"
        
        # Выручка за неделю
        report += f"💰 **Выручка за неделю:** {fmt_money(rest['revenue'])}\n"
        report += f"   {fmt_percent(rest['revenue_change'])} к прошлому году\n"
        
        # Блюда и напитки
        report += f"   🍽️ Блюда: {fmt_money(rest['revenue_kit'])} ({rest['kit_percent']:.1f}%)\n"
        report += f"   🍷 Напитки: {fmt_money(rest['revenue_bar'])} ({rest['bar_percent']:.1f}%)\n"
        report += "\n"
        
        # Основные метрики
        report += f"🧾 **Заказов:** {rest['orders']} ({fmt_percent(rest['orders_change'])} к прошлому году)\n"
        report += f"👥 **Гостей:** {rest['guests']} ({fmt_percent(rest['guests_change'])} к прошлому году)\n"
        report += f"💳 **Ср. чек (заказ):** {fmt_money(rest['avg_check_order'])} ({fmt_percent(rest['avg_check_order_change'])} к прошлому году)\n"
        report += f"👤 **Ср. чек (гость):** {fmt_money(rest['avg_check_guest'])} ({fmt_percent(rest['avg_check_guest_change'])} к прошлому году)\n"
        report += f"👥 **Наполняемость:** {rest['occupancy']:.2f} ({fmt_percent(rest['occupancy_change'])} к прошлому году)\n"
        report += "\n"
        
        # Количество блюд/напитков
        report += f"🍽️ **Ср. блюд на заказ:** {fmt_qntt(rest['avg_kit_order'])}\n"
        report += f"🍷 **Ср. напитков на заказ:** {fmt_qntt(rest['avg_bar_order'])}\n"
        report += f"🍽️ **Ср. блюд на гостя:** {fmt_qntt(rest['avg_kit_guest'])}\n"
        report += f"🍷 **Ср. напитков на гостя:** {fmt_qntt(rest['avg_bar_guest'])}\n"
        report += "\n"
        
        # Среднедневная выручка
        report += f"📈 **Среднедневная выручка:** {fmt_money(rest['avg_daily_revenue'])}\n"
        report += f"   {fmt_percent(rest['avg_daily_change'])} к прошлому году\n"
        report += "\n"
        
        # План
        if rest['plan'] > 0:
            plan_pct = rest['plan_completion']
            plan_diff = plan_pct - 100
            report += f"🎯 **План на неделю:** {fmt_money(rest['plan'])}\n"
            report += f"📊 **Выполнение плана:** {plan_pct:.2f}%"
            if plan_diff > 0:
                report += f" (перевыполнение на {plan_diff:+.2f}%)"
            elif plan_diff < 0:
                report += f" (недовыполнение на {plan_diff:+.2f}%)"
            else:
                report += " (план выполнен точно)"
            report += "\n"
        
        if rest['plan_guests'] > 0:
            plan_guest_pct = rest['plan_guests_completion']
            plan_guest_diff = plan_guest_pct - 100
            report += f"🎯 **План по гостям:** {rest['plan_guests']:.0f}\n"
            report += f"📊 **Выполнение плана:** {plan_guest_pct:.2f}%"
            if plan_guest_diff > 0:
                report += f" (перевыполнение на {plan_guest_diff:+.2f}%)"
            elif plan_guest_diff < 0:
                report += f" (недовыполнение на {plan_guest_diff:+.2f}%)"
            else:
                report += " (план выполнен точно)"
            report += "\n"
    
    return report


# === 10. ТОЧКА ВХОДА ===
if __name__ == "__main__":
    print("🚀 Запуск сбора аналитики...")
    print("⚠️ Этот файл предназначен для импорта в bot.py")
    
    try:
        test_login = "atatiyevskiy@adjiki.ru"
        test_password = "Qwerty211510"
        
        org_id, token, _ = authenticate_and_get_org_id(test_login, test_password)
        
        # Тестируем ежедневный отчет
        print("\n" + "=" * 50)
        print("📊 ЕЖЕДНЕВНЫЙ ОТЧЕТ")
        print("=" * 50)
        daily = fetch_full_analytics(
            login=test_login,
            password=test_password,
            org_id=org_id,
            token=token
        )
        print(format_report(daily))
        
        # Тестируем недельный отчет
        print("\n" + "=" * 50)
        print("📊 НЕДЕЛЬНЫЙ ОТЧЕТ")
        print("=" * 50)
        weekly = fetch_weekly_analytics(
            login=test_login,
            password=test_password,
            org_id=org_id,
            token=token
        )
        print(format_weekly_report(weekly))
        
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()

        
    #prod by D.Pankratov and M.Buanov