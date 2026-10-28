"""Generate the mock ERP extracts used in the demo.

erp_orders_baseline.csv  - what the sheet is pre-loaded with (25 orders)
erp_orders_update.csv    - the "latest" ERP extract: 6 changed orders, 5 new orders, rest unchanged
"""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
FIELDS = ["Order ID", "Customer", "Product", "Quantity", "Order Value",
          "Order Date", "Requested Delivery", "ERP Status", "Carrier", "Tracking Number"]

random.seed(42)
CUSTOMERS = ["Acme Industrial", "Globex Corp", "Initech", "Umbrella Logistics", "Stark Components",
             "Wayne Enterprises", "Wonka Foods", "Hooli", "Vandelay Imports", "Soylent Co"]
PRODUCTS = [("Hydraulic Pump HP-200", 1850), ("Control Valve CV-45", 420), ("Servo Motor SM-12", 960),
            ("Bearing Kit BK-9", 135), ("Sensor Array SA-7", 610), ("PLC Module PM-3", 2250)]
CARRIERS = ["FedEx Freight", "UPS", "DHL", "XPO"]

START = date(2026, 10, 5)


def make_order(n: int, status: str) -> dict:
    prod, price = random.choice(PRODUCTS)
    qty = random.choice([2, 5, 10, 20, 40])
    ordered = START + timedelta(days=random.randint(0, 14))
    shipped = status in ("Shipped", "Delivered")
    return {
        "Order ID": f"SO-{10000 + n}",
        "Customer": random.choice(CUSTOMERS),
        "Product": prod,
        "Quantity": qty,
        "Order Value": qty * price,
        "Order Date": ordered.isoformat(),
        "Requested Delivery": (ordered + timedelta(days=random.randint(7, 21))).isoformat(),
        "ERP Status": status,
        "Carrier": random.choice(CARRIERS) if shipped else "",
        "Tracking Number": f"1Z{random.randint(10**9, 10**10 - 1)}" if shipped else "",
    }


def write(name: str, rows: list[dict]) -> None:
    with open(HERE / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {name}: {len(rows)} rows")


statuses = (["Delivered"] * 6 + ["Shipped"] * 6 + ["Picking"] * 5 + ["Open"] * 6 + ["Backordered"] * 2)
random.shuffle(statuses)
baseline = [make_order(i, s) for i, s in enumerate(statuses, start=1)]
write("erp_orders_baseline.csv", baseline)

# --- latest extract: copy baseline, then mutate a few rows and append new ones
update = [dict(r) for r in baseline]
by_id = {r["Order ID"]: r for r in update}


def ship(r):
    r["ERP Status"] = "Shipped"
    r["Carrier"] = random.choice(CARRIERS)
    r["Tracking Number"] = f"1Z{random.randint(10**9, 10**10 - 1)}"


open_ids = [r["Order ID"] for r in baseline if r["ERP Status"] in ("Open", "Picking")]
shipped_ids = [r["Order ID"] for r in baseline if r["ERP Status"] == "Shipped"]
backordered = [r["Order ID"] for r in baseline if r["ERP Status"] == "Backordered"]

for oid in open_ids[:3]:                                   # moved forward / shipped
    ship(by_id[oid])
by_id[shipped_ids[0]]["ERP Status"] = "Delivered"           # delivered
r = by_id[open_ids[3]]                                      # customer changed qty + date
r["Quantity"] = int(r["Quantity"]) + 10
r["Order Value"] = int(r["Order Value"]) * (int(r["Quantity"])) // (int(r["Quantity"]) - 10)
r["Requested Delivery"] = (date.fromisoformat(r["Requested Delivery"]) + timedelta(days=5)).isoformat()
by_id[backordered[0]]["Requested Delivery"] = (
    date.fromisoformat(by_id[backordered[0]]["Requested Delivery"]) + timedelta(days=14)).isoformat()

n = len(baseline)
update += [make_order(n + i, s) for i, s in enumerate(["Open", "Open", "Picking", "Open", "Backordered"], start=1)]
write("erp_orders_update.csv", update)
