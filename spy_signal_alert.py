"""
============================================================================
SPY SIGNAL ALERT BOT — сигнал в Telegram, сделку жмёшь сам
============================================================================
Полуавтомат: скрипт раз в день считает сигнал нашей стратегии и присылает
его в Telegram. Ордера НЕ отправляются — решение и клик за тобой.

ЛОГИКА (та же, что в бэктесте на 33 годах):
  * Close vs SMA200 с гистерезисом +-1%.
    Выше верхней границы -> RISK-ON (быть в SPY).
    Ниже нижней -> RISK-OFF (быть в кэше).
    В мёртвой зоне режим не меняется (защита от пилы).
  * Таймфрейм: ДНЕВНОЙ (свежие бесплатные данные + это та версия,
    что валидирована 33 годами истории).

ПОВЕДЕНИЕ:
  * Смена режима -> ГРОМКОЕ уведомление "ПОРА ДЕЙСТВОВАТЬ".
  * Режим не изменился -> тихий ежедневный статус (можно отключить
    флагом NOTIFY_ONLY_ON_CHANGE = True, тогда молчит без новостей).
  * Предыдущий режим хранится в файле state.json (чтобы знать,
    произошла ли смена).

ИСТОЧНИК ДАННЫХ:
  * yfinance (не требует ключей Alpaca) — проще для сигнального бота.
    Alpaca-ключи здесь не нужны вообще.

ЗАПУСК:
  pip install yfinance pandas numpy requests
  export TELEGRAM_TOKEN="123456:ABC..."
  export TELEGRAM_CHAT_ID="123456789"
  python spy_signal_alert.py

АВТОЗАПУСК РАЗ В ДЕНЬ (GitHub Actions) — см. файл workflow рядом.
============================================================================
"""

from __future__ import annotations

import os
import sys
import json
from datetime import datetime

import numpy as np
import pandas as pd
import requests

# ===========================================================================
# CONFIG
# ===========================================================================
SYMBOL = "SPY"
MA_LEN = 200                    # SMA200 на дневках
BAND = 0.01                     # гистерезис +-1%
SUGGESTED_LEVERAGE = 2.0        # что показывать в подсказке по размеру
NOTIFY_ONLY_ON_CHANGE = False   # True = молчать, если режим не менялся
STATE_FILE = "state.json"       # тут хранится прошлый режим


# ===========================================================================
# TELEGRAM
# ===========================================================================
def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[!] Нет TELEGRAM_TOKEN / TELEGRAM_CHAT_ID в окружении.")
        print("    Сообщение, которое ушло бы:\n")
        print(text)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=20)
        r.raise_for_status()
        print("[+] Отправлено в Telegram.")
        return True
    except Exception as e:
        print(f"[!] Ошибка отправки в Telegram: {e}")
        return False


# ===========================================================================
# СОСТОЯНИЕ (прошлый режим)
# ===========================================================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"risk_on": None, "updated": None}


def save_state(risk_on: bool, bar_date: str):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"risk_on": risk_on, "updated": bar_date}, f)


# ===========================================================================
# ДАННЫЕ + СИГНАЛ
# ===========================================================================
def fetch_data() -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(SYMBOL, period="2y", interval="1d",
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    if len(df) < MA_LEN + 1:
        raise RuntimeError(f"Мало данных: {len(df)} баров, нужно {MA_LEN}+")
    return df


def compute_signal(df: pd.DataFrame, prev_risk_on) -> dict:
    close = df["Close"]
    sma = close.rolling(MA_LEN).mean()

    c = float(close.iloc[-1])
    s = float(sma.iloc[-1])
    upper, lower = s * (1 + BAND), s * (1 - BAND)

    if c > upper:
        risk_on = True
    elif c < lower:
        risk_on = False
    else:
        # мёртвая зона: держим прошлый режим (при первом запуске — по SMA)
        risk_on = prev_risk_on if prev_risk_on is not None else (c > s)

    # расстояние до границ переключения — полезно видеть заранее
    to_upper = (upper / c - 1) * 100
    to_lower = (1 - lower / c) * 100

    return {
        "risk_on": risk_on,
        "close": c, "sma": s, "upper": upper, "lower": lower,
        "to_upper": to_upper, "to_lower": to_lower,
        "bar_date": df.index[-1].strftime("%Y-%m-%d"),
    }


# ===========================================================================
# ФОРМАТ СООБЩЕНИЯ
# ===========================================================================
def build_message(sig: dict, prev, changed: bool) -> str:
    regime = "RISK-ON 🟢" if sig["risk_on"] else "RISK-OFF 🔴"

    if changed:
        head = "🚨 <b>СМЕНА РЕЖИМА — ПОРА ДЕЙСТВОВАТЬ</b> 🚨\n\n"
        if sig["risk_on"]:
            action = (f"➡️ <b>ПОКУПАТЬ {SYMBOL}</b>\n"
                      f"Цена пробила верхнюю границу — рынок над трендом.\n"
                      f"Размер: весь капитал (при плече "
                      f"{SUGGESTED_LEVERAGE}x — удвоенный номинал).")
        else:
            action = (f"➡️ <b>ПРОДАВАТЬ {SYMBOL}, ВЫХОД В КЭШ</b>\n"
                      f"Цена ушла под нижнюю границу — рынок под трендом.\n"
                      f"Закрыть позицию полностью.")
    else:
        head = "📊 <b>Ежедневный статус</b> (без изменений)\n\n"
        action = ("Ничего делать не нужно — режим прежний."
                  if prev is not None else
                  "Первый запуск: зафиксирован текущий режим.")

    # Показываем расстояние только до ТОЙ границы, которая сейчас важна:
    # в RISK-ON следим за нижней (когда продавать), в RISK-OFF — за верхней.
    if sig["risk_on"]:
        watch = (f"<b>Следующее действие — продажа:</b>\n"
                 f"• Сработает при падении до ${sig['lower']:.2f} "
                 f"(−{sig['to_lower']:.2f}% от текущей)")
    else:
        watch = (f"<b>Следующее действие — покупка:</b>\n"
                 f"• Сработает при росте до ${sig['upper']:.2f} "
                 f"(+{sig['to_upper']:.2f}% от текущей)")

    body = (
        f"{head}"
        f"<b>Режим:</b> {regime}\n"
        f"{action}\n\n"
        f"<b>Данные на {sig['bar_date']}:</b>\n"
        f"• Цена {SYMBOL}: <b>${sig['close']:.2f}</b>\n"
        f"• SMA{MA_LEN}: ${sig['sma']:.2f}\n"
        f"• Зона гистерезиса: ${sig['lower']:.2f} — ${sig['upper']:.2f}\n\n"
        f"{watch}\n"
    )
    return body


# ===========================================================================
# MAIN
# ===========================================================================
def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Считаю сигнал {SYMBOL}...")

    state = load_state()
    prev = state.get("risk_on")

    df = fetch_data()
    sig = compute_signal(df, prev)

    changed = (prev is not None) and (sig["risk_on"] != prev)
    first_run = prev is None

    print(f"  Бар {sig['bar_date']} | Close ${sig['close']:.2f} | "
          f"SMA{MA_LEN} ${sig['sma']:.2f} | "
          f"режим {'ON' if sig['risk_on'] else 'OFF'} | "
          f"смена: {'ДА' if changed else 'нет'}")

    # Решаем, слать ли сообщение
    should_send = changed or first_run or (not NOTIFY_ONLY_ON_CHANGE)
    if should_send:
        msg = build_message(sig, prev, changed)
        send_telegram(msg)
    else:
        print("  Режим не менялся, уведомление подавлено (NOTIFY_ONLY_ON_CHANGE).")

    save_state(sig["risk_on"], sig["bar_date"])
    print("  Состояние сохранено.")


if __name__ == "__main__":
    main()
