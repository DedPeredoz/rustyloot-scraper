# -*- coding: utf-8 -*-
# rustyloot_sniffer.py
#
# Запускается в GitHub Actions (headless).
# Логин/пароль берутся из переменных окружения RL_USERNAME / RL_PASSWORD.
# Слушает WebSocket (performance logs), пытается вытащить инвентарь и краткие данные,
# сохраняет в inventory.json и rustyloot_market.json, пишет логи.

import os
import json
import time
import logging
import argparse
from datetime import datetime
from collections import deque

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

AUTH_URL = "https://rustyloot.gg/?auth=true"
WITHDRAW_URL = "https://rustyloot.gg/?withdraw=true&rust=true"
LOG_FILE = "rustyloot_sniffer.log"
INV_FILE = "inventory.json"
OUT_FILE = "rustyloot_market.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)

def setup_driver(headless: bool = True) -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    # включаем перехват performance-логов
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return webdriver.Chrome(options=opts)

def wait_css(driver, css, timeout=30):
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, css))
    )

def safe_get_perf(driver):
    out = []
    try:
        raw = driver.get_log("performance")
    except Exception:
        return out
    for entry in raw:
        try:
            msg = json.loads(entry["message"])["message"]
            out.append(msg)
        except Exception:
            pass
    return out

def iter_ws_frames(driver):
    for m in safe_get_perf(driver):
        method = m.get("method", "")
        if method == "Network.webSocketFrameReceived":
            yield "in", m["params"]["response"]["payloadData"]
        elif method == "Network.webSocketFrameSent":
            yield "out", m["params"]["response"]["payloadData"]

def parse_socketio(payload):
    # Socket.IO text frame: '42' + JSON array: ["event", {...}]
    if not isinstance(payload, str) or not payload.startswith("42"):
        return None
    try:
        arr = json.loads(payload[2:])
        if isinstance(arr, list) and arr:
            return arr[0], arr[1:]
    except Exception:
        return None

def try_extract_inventory(args):
    if not args:
        return []
    first = args[0]
    cands = []
    if isinstance(first, dict):
        if isinstance(first.get("data"), dict) and isinstance(first["data"].get("inventory"), list):
            cands.append(first["data"]["inventory"])
        if isinstance(first.get("inventory"), list):
            cands.append(first["inventory"])
    if isinstance(first, list):
        cands.append(first)
    for c in cands:
        if isinstance(c, list):
            return c
    return []

def merge_inventory(agg, items):
    for it in items:
        name = it.get("name") or it.get("market_hash_name") or it.get("title") or "UNKNOWN"
        cents = it.get("price") or it.get("price_cents") or 0
        try:
            price = float(cents) / 100.0
        except Exception:
            price = 0.0
        qty = int(it.get("amount") or it.get("quantity") or 1)

        rec = agg.setdefault(name, {"amount": 0, "total_price": 0.0})
        rec["amount"] += qty
        rec["total_price"] += price
    return agg

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def login(driver, username, password):
    logging.info("Открываю страницу авторизации…")
    driver.get(AUTH_URL)
    wait_css(driver, "input[placeholder='Email or Username']", 45)

    u = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Email or Username']")
    p = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Password']")
    u.clear(); u.send_keys(username)
    p.clear(); p.send_keys(password)

    # submit
    btns = driver.find_elements(By.CSS_SELECTOR, "button[type='submit']")
    (btns[0] if btns else p).click()

    WebDriverWait(driver, 45).until(lambda d: "auth=true" not in d.current_url)
    logging.info("Авторизация успешна")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=180, help="Время работы, сек.")
    ap.add_argument("--headless", action="store_true", help="Headless режим (для Actions включай)")
    args = ap.parse_args()

    username = os.getenv("RL_USERNAME", "")
    password = os.getenv("RL_PASSWORD", "")
    if not username or not password:
        logging.error("Нет RL_USERNAME/RL_PASSWORD в окружении.")
        return 1

    inv = {}
    market_sample = []  # место для любых кратких цен, если появятся в событиях

    driver = setup_driver(headless=args.headless)
    try:
        login(driver, username, password)
        logging.info("Открываю магазин…")
        driver.get(WITHDRAW_URL)

        seen = deque(maxlen=2000)
        start = time.time()
        logging.info("Слушаю Socket.IO события…")

        while time.time() - start < args.duration:
            for direction, payload in iter_ws_frames(driver):
                evt = parse_socketio(payload)
                if not evt:
                    continue
                name, evt_args = evt

                sig = (direction, name, json.dumps(evt_args, ensure_ascii=False, sort_keys=True))
                if sig in seen:
                    continue
                seen.append(sig)

                stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"[{stamp}] {direction} EVENT '{name}': {str(evt_args)[:500]}")

                items = try_extract_inventory(evt_args)
                if items:
                    merge_inventory(inv, items)
                    save_json(INV_FILE, inv)
                    logging.info(f"Инвентарь сохранён: {len(inv)} уник. позиций → {INV_FILE}")

                # если попадутся агрегированные цены — можно тут наполнять market_sample

            time.sleep(0.2)

        # финальный дамп (если market_sample наполнили)
        save_json(OUT_FILE, {"inventory": inv, "market_sample": market_sample})
        logging.info(f"Готово. Итоги: {OUT_FILE}")

        # быстрая выборка для логов
        first5 = list(inv.items())[:5]
        if first5:
            print("\nTop-5 из инвентаря:")
            for name, rec in first5:
                print(f" • {name} — qty={rec['amount']}  total≈${rec['total_price']:.2f}")
        else:
            print("Инвентарь не пойман.")

        return 0
    finally:
        driver.quit()

if __name__ == "__main__":
    raise SystemExit(main())
