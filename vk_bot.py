"""VK-бот отслеживания курсов валют с интеграцией API ЦБ РФ.

Команды:
  курсы                     — основные валюты к рублю
  курс <код>                — курс конкретной валюты (например: курс USD)
  конвертер <сумма> <код>   — конвертация в рубли (например: конвертер 100 usd)
  динамика <код>            — динамика курса за 7 дней
  помощь                    — справка
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
from typing import Any

import vk_api
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.longpoll import VkLongPoll, VkEventType
from dotenv import load_dotenv

from CBRAPI import (
    CBRAPI,
    CBRAPIError,
    CurrencyNotFoundError,
    APIConnectionError,
    CurrencyRate,
    DynamicsPoint,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vk_bot")


# ─── Форматирование ответов ──────────────────────────────────────────────────

POPULAR_CODES: list[str] = ["USD", "EUR", "GBP", "CNY", "JPY", "CHF", "KZT", "TRY"]


def _sign(value: float) -> str:
    return f"+{value:.4f}" if value >= 0 else f"{value:.4f}"


def format_rates(rates: dict[str, CurrencyRate], codes: list[str]) -> str:
    """Форматирует таблицу курсов основных валют."""
    rate_date = next(iter(rates.values())).rate_date
    lines = [f"💱 Курсы ЦБ РФ на {rate_date}", ""]
    for code in codes:
        r = rates.get(code)
        if not r:
            continue
        lines.append(f"{r.trend_emoji} {r.code}: {r.rub_price:.4f} ₽  ({_sign(r.change)})")
    return "\n".join(lines)


def format_currency(r: CurrencyRate) -> str:
    """Форматирует курс конкретной валюты."""
    return "\n".join([
        f"{r.trend_emoji} {r.code} — {r.name}",
        "",
        f"💰 Курс ЦБ: {r.nominal} {r.code} = {r.value:.4f} ₽",
        f"📊 За единицу: {r.rub_price:.4f} ₽",
        f"🔁 Изменение за день: {_sign(r.change)} ₽ ({r.change_pct:+.2f}%)",
        f"📅 Дата: {r.rate_date}",
    ])


def format_dynamics(code: str, points: list[DynamicsPoint]) -> str:
    """Форматирует динамику курса за период."""
    lines = [f"📅 Динамика {code} за {len(points)} дн.", ""]
    first, last = points[0].rub_price, points[-1].rub_price
    diff = last - first
    diff_pct = (diff / first * 100) if first else 0.0

    for p in points:
        bar = _bar_for_price(p.rub_price, first, last)
        lines.append(f"  {p.day}: {p.rub_price:.4f} ₽  {bar}")

    lines += [
        "",
        f"📉 Минимум: {min(p.rub_price for p in points):.4f} ₽",
        f"📈 Максимум: {max(p.rub_price for p in points):.4f} ₽",
        f"{'📈' if diff >= 0 else '📉'} Изменение: {_sign(diff)} ₽ ({diff_pct:+.2f}%)",
    ]
    return "\n".join(lines)


def _bar_for_price(price: float, minimum: float, maximum: float) -> str:
    """Простая визуализация уровня цены за период."""
    if maximum <= minimum:
        return "▓▓▓▓▓"
    filled = round((price - minimum) / (maximum - minimum) * 5)
    filled = max(0, min(5, filled))
    return "▓" * filled + "░" * (5 - filled)


def format_conversion(amount: float, rub: float, r: CurrencyRate) -> str:
    """Форматирует результат конвертации."""
    return "\n".join([
        f"🔄 Конвертация",
        "",
        f"{amount:g} {r.code} = {rub:.2f} ₽",
        f"💰 По курсу ЦБ: {r.nominal} {r.code} = {r.value:.4f} ₽",
        f"📅 Дата: {r.rate_date}",
    ])


HELP_TEXT = (
    "🤖 Бот курсов валют ЦБ РФ — справка\n"
    "\n"
    "Команды (или используйте кнопки):\n"
    "  курсы                    — основные валюты к рублю\n"
    "  курс <код>               — курс валюты (курс USD)\n"
    "  конвертер <сумма> <код>  — в рубли (конвертер 100 usd)\n"
    "  динамика <код>           — за 7 дней (динамика EUR)\n"
    "  помощь                   — эта справка\n"
    "\n"
    "Примеры:\n"
    "  курсы\n"
    "  курс CNY\n"
    "  конвертер 250 EUR\n"
    "  динамика USD"
)


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def build_main_keyboard() -> str:
    """Главная клавиатура — постоянная, с командами и популярными валютами."""
    kb = VkKeyboard(one_time=False, inline=False)
    kb.add_button("Курсы", color=VkKeyboardColor.POSITIVE, payload={"cmd": "rates"})
    kb.add_button("Помощь", color=VkKeyboardColor.NEGATIVE, payload={"cmd": "help"})
    kb.add_line()
    kb.add_button("Курс USD", color=VkKeyboardColor.PRIMARY, payload={"cmd": "currency", "code": "USD"})
    kb.add_button("Курс EUR", color=VkKeyboardColor.PRIMARY, payload={"cmd": "currency", "code": "EUR"})
    kb.add_button("Курс CNY", color=VkKeyboardColor.PRIMARY, payload={"cmd": "currency", "code": "CNY"})
    return kb.get_keyboard()


def build_currency_keyboard(code: str) -> str:
    """Контекстная клавиатура после запроса курса."""
    kb = VkKeyboard(inline=True)
    kb.add_button(f"Динамика {code}", color=VkKeyboardColor.PRIMARY, payload={"cmd": "dynamics", "code": code})
    kb.add_button(f"100 {code} в ₽", color=VkKeyboardColor.SECONDARY, payload={"cmd": "convert", "amount": 100, "code": code})
    kb.add_line()
    kb.add_button("Ко всем курсам", color=VkKeyboardColor.POSITIVE, payload={"cmd": "rates"})
    return kb.get_keyboard()


# ─── Парсинг команд ──────────────────────────────────────────────────────────

_CONVERT_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([a-z]{3})\s*$")


def _parse_command(text: str) -> tuple[str, str] | None:
    """Возвращает (команда, аргумент) или None."""
    text = text.strip().lower()

    if text in ("помощь", "помоги", "help", "start", "начать", "справка"):
        return ("help", "")

    if text in ("курсы", "курсы валют", "rates"):
        return ("rates", "")

    m = _CONVERT_RE.match(text.replace("конвертер", "", 1).strip()) if text.startswith("конвертер") else None
    if m:
        return ("convert", f"{m.group(1)} {m.group(2)}")

    for prefix in ("динамика", "dynamics"):
        if text.startswith(prefix):
            arg = text[len(prefix):].strip()
            if arg:
                return ("dynamics", arg)

    for prefix in ("курс ", "курс"):
        if text.startswith(prefix):
            arg = text[len(prefix):].strip()
            if arg:
                return ("currency", arg)

    return None


# ─── Бот ─────────────────────────────────────────────────────────────────────

class VKCurrencyBot:
    """VK LongPoll-бот курсов валют.

    Args:
        vk_token: токен VK (группы).
        cbr_api: экземпляр CBRAPI.
    """

    def __init__(self, vk_token: str, cbr_api: CBRAPI) -> None:
        self._vk_session = vk_api.VkApi(token=vk_token)
        self._api = self._vk_session.get_api()
        self._cbr = cbr_api
        self._longpoll = VkLongPoll(self._vk_session)

    def _send(self, user_id: int, text: str, keyboard: str | None = None) -> None:
        params: dict[str, Any] = {
            "user_id": user_id,
            "message": text,
            "random_id": random.randint(1, 2**31),
        }
        if keyboard:
            params["keyboard"] = keyboard
        self._api.messages.send(**params)

    def _handle_rates(self, user_id: int) -> None:
        try:
            rates = self._cbr.get_daily()
            self._send(user_id, format_rates(rates, POPULAR_CODES), keyboard=build_main_keyboard())
        except APIConnectionError:
            self._send(user_id, "⚠️ Не удалось подключиться к API ЦБ РФ. Попробуйте позже.", keyboard=build_main_keyboard())
        except CBRAPIError as exc:
            logger.error("CBR API error: %s", exc)
            self._send(user_id, "⚠️ Ошибка при получении курсов.", keyboard=build_main_keyboard())

    def _handle_currency(self, user_id: int, arg: str) -> None:
        try:
            rate = self._cbr.get_currency(arg)
            self._send(user_id, format_currency(rate), keyboard=build_currency_keyboard(rate.code))
        except CurrencyNotFoundError:
            self._send(user_id, f"😕 Валюта «{arg.upper()}» не найдена. Проверьте код (USD, EUR, CNY…).", keyboard=build_main_keyboard())
        except APIConnectionError:
            self._send(user_id, "⚠️ Не удалось подключиться к API ЦБ РФ. Попробуйте позже.", keyboard=build_main_keyboard())
        except CBRAPIError as exc:
            logger.error("CBR API error: %s", exc)
            self._send(user_id, "⚠️ Ошибка при получении курса.", keyboard=build_main_keyboard())

    def _handle_convert(self, user_id: int, arg: str) -> None:
        try:
            amount_str, code = arg.split()
            amount = float(amount_str.replace(",", "."))
        except ValueError:
            self._send(user_id, "🤔 Формат: конвертер <сумма> <код>. Пример: конвертер 100 usd", keyboard=build_main_keyboard())
            return

        try:
            rub, rate = self._cbr.convert_to_rub(amount, code)
            self._send(user_id, format_conversion(amount, rub, rate), keyboard=build_currency_keyboard(rate.code))
        except CurrencyNotFoundError:
            self._send(user_id, f"😕 Валюта «{code.upper()}» не найдена.", keyboard=build_main_keyboard())
        except APIConnectionError:
            self._send(user_id, "⚠️ Не удалось подключиться к API ЦБ РФ. Попробуйте позже.", keyboard=build_main_keyboard())
        except CBRAPIError as exc:
            logger.error("CBR API error: %s", exc)
            self._send(user_id, "⚠️ Ошибка при конвертации.", keyboard=build_main_keyboard())

    def _handle_dynamics(self, user_id: int, arg: str) -> None:
        try:
            points = self._cbr.get_dynamics(arg, days=7)
            self._send(user_id, format_dynamics(arg.upper(), points), keyboard=build_currency_keyboard(arg.upper()))
        except CurrencyNotFoundError:
            self._send(user_id, f"😕 Валюта «{arg.upper()}» не найдена.", keyboard=build_main_keyboard())
        except APIConnectionError:
            self._send(user_id, "⚠️ Не удалось подключиться к API ЦБ РФ. Попробуйте позже.", keyboard=build_main_keyboard())
        except CBRAPIError as exc:
            logger.error("CBR API error: %s", exc)
            self._send(user_id, "⚠️ Ошибка при получении динамики.", keyboard=build_main_keyboard())

    def run(self) -> None:
        logger.info("Бот запущен. Ожидание сообщений...")
        for event in self._longpoll.listen():
            if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                try:
                    self._on_message(event)
                except Exception:
                    logger.exception("Необработанная ошибка для user_id=%s", event.user_id)
                    self._send(event.user_id, "⚠️ Внутренняя ошибка. Попробуйте позже.", keyboard=build_main_keyboard())

    def _on_message(self, event: VkEventType) -> None:
        text: str = event.text or ""
        user_id: int = event.user_id

        logger.info("Сообщение от %s: %s", user_id, text)

        # Достаём payload из raw-события LongPoll (event.raw[6] — поле payload)
        raw_payload = ""
        try:
            if hasattr(event, "raw") and len(event.raw) > 6 and event.raw[6]:
                raw_payload = str(event.raw[6])
        except Exception:
            pass

        if raw_payload:
            try:
                payload = json.loads(raw_payload)
                payload_cmd = payload.get("cmd", "")
                code = payload.get("code", "")
                amount = payload.get("amount", "")
                arg = f"{amount} {code}".strip() if payload_cmd == "convert" else code
                logger.info("Payload: cmd=%s arg=%s", payload_cmd, arg)
                return self._dispatch(user_id, payload_cmd, arg)
            except (json.JSONDecodeError, TypeError):
                pass

        parsed = _parse_command(text)
        if parsed is None:
            self._send(user_id, "🤔 Не понял. Нажмите кнопку или напишите «помощь».", keyboard=build_main_keyboard())
            return
        cmd, arg = parsed
        self._dispatch(user_id, cmd, arg)

    def _dispatch(self, user_id: int, cmd: str, arg: str) -> None:
        """Маршрутизация команды (единая точка для payload и текста)."""
        if cmd == "help":
            self._send(user_id, HELP_TEXT, keyboard=build_main_keyboard())
        elif cmd == "rates":
            self._handle_rates(user_id)
        elif cmd == "currency":
            self._handle_currency(user_id, arg)
        elif cmd == "convert":
            self._handle_convert(user_id, arg)
        elif cmd == "dynamics":
            self._handle_dynamics(user_id, arg)
        else:
            self._send(user_id, "🤔 Неизвестная команда. Нажмите кнопку или напишите «помощь».", keyboard=build_main_keyboard())


# ─── Точка входа ─────────────────────────────────────────────────────────────

def main() -> None:
    vk_token = os.getenv("VK_TOKEN", "")
    if not vk_token:
        logger.error("VK_TOKEN не задан. Укажите в .env")
        sys.exit(1)

    cbr = CBRAPI()
    bot = VKCurrencyBot(vk_token=vk_token, cbr_api=cbr)
    bot.run()


if __name__ == "__main__":
    main()
