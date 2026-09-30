import requests
from datetime import datetime, timedelta

BASE_URL = "https://mozg.rest"
LOGIN = "atatiyevskiy@adjiki.ru"
PASSWORD = "Qwerty211510"

# Авторизация
r = requests.post(f"{BASE_URL}/api/v1/auth/login", json={"login": LOGIN, "password": PASSWORD})
data = r.json()
token = data[0]["data"]["access_token"] if isinstance(data, list) else data["data"]["access_token"]

r = requests.get(f"{BASE_URL}/api/v1/user/organizations", headers={"Authorization": f"Bearer {token}"})
org_id = r.json()["data"]["currentOrganizationId"]

# Пробуем разные метрики
yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

payload = {
    "organization_id": org_id,
    "period": {"from": yesterday, "to": yesterday},
   "metrics": [
    "realsum", "guests", "orders",
    "kit_qntt", "bar_qntt",
    "avg_kit_qntt_guest", "avg_bar_qntt_guest",
    ],
    "group_by": ["rest", "hllg"],
    "totals": False,
}

r = requests.post(
    f"{BASE_URL}/api/v1/sales/query",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json=payload,
)
print(f"HTTP: {r.status_code}")
if r.status_code == 200:
    rows = r.json().get("data", {}).get("rows", [])
    print(f"Строк: {len(rows)}")
    if rows:
        import json
        print(json.dumps(rows[0], ensure_ascii=False, indent=2))
else:
    print(r.text[:500])