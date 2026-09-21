"""Клиент API курсов валют ЦБ РФ (через сервис cbr-xml-daily.ru).

Поддерживаемые возможности:
- Текущие курсы (daily_json.js)
- Курс конкретной валюты с изменением за день
- Динамика курса за N дней (archive)
- Конвертация валюты в рубли
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ─── Исключения ──────────────────────────────────────────────────────────────

class CBRAPIError(Exception):
    """Базовое исключение модуля."""


class CurrencyNotFoundError(CBRAPIError):
    """Валюта не найдена."""


class APIConnectionError(CBRAPIError):
    """Ошибка соединения с API."""


# ─── dataclass для результатов ───────────────────────────────────────────────

@dataclass
class CurrencyRate:
    """Курс одной валюты к рублю."""
    code: str
    name: str
    nominal: int
    value: float
    previous: float
    rate_date: str

    @property
    def rub_price(self) -> float:
        """Цена единицы валюты в рублях."""
        return self.value / self.nominal

    @property
    def change(self) -> float:
        return self.value / self.nominal - self.previous / self.nominal

    @property
    def change_pct(self) -> float:
        prev = self.previous / self.nominal
        return (self.change / prev * 100) if prev else 0.0

    @property
    def trend_emoji(self) -> str:
        if self.change > 0:
            return "📈"
        if self.change < 0:
            return "📉"
        return "➖"


@dataclass
class DynamicsPoint:
    """Точка динамики курса."""
    day: str
    rub_price: float


# ─── Основной класс ──────────────────────────────────────────────────────────

class CBRAPI:
    """Клиент API ЦБ РФ.

    Args:
        timeout: таймаут запросов в секундах.
    """

    _BASE = "https://www.cbr-xml-daily.ru"
    _URL_DAILY = f"{_BASE}/daily_json.js"

    def __init__(self, timeout: int = 10) -> None:
        self._timeout = timeout
        self._session = requests.Session()

    # ─── Внутренние методы ───────────────────────────────────────────────────

    def _make_request(self, url: str) -> dict[str, Any]:
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.RequestException as exc:
            raise APIConnectionError(f"Не удалось подключиться к API ЦБ: {exc}") from exc

        if resp.status_code == 404:
            raise CurrencyNotFoundError("Данные не найдены.")
        if resp.status_code != 200:
            raise CBRAPIError(f"API вернул статус {resp.status_code}")

        return resp.json()

    def _parse_valute(self, code: str, valute: dict[str, Any], rate_date: str) -> CurrencyRate:
        if code not in valute:
            raise CurrencyNotFoundError(f"Валюта «{code}» не найдена. Проверьте код (например USD, EUR).")
        v = valute[code]
        return CurrencyRate(
            code=v["CharCode"],
            name=v["Name"],
            nominal=int(v["Nominal"]),
            value=float(v["Value"]),
            previous=float(v["Previous"]),
            rate_date=rate_date,
        )

    @staticmethod
    def _format_date(raw: str) -> str:
        try:
            return datetime.fromisoformat(raw).strftime("%d.%m.%Y")
        except (ValueError, TypeError):
            return raw[:10]

    # ─── Публичные методы ────────────────────────────────────────────────────

    def get_daily(self) -> dict[str, CurrencyRate]:
        """Получить все курсы на текущий день.

        Returns:
            Словарь {код валюты: CurrencyRate}.
        """
        data = self._make_request(self._URL_DAILY)
        rate_date = self._format_date(data.get("Date", ""))
        return {
            code: self._parse_valute(code, v, rate_date)
            for code, v in data.get("Valute", {}).items()
        }

    def get_currency(self, code: str) -> CurrencyRate:
        """Получить курс конкретной валюты.

        Args:
            code: код валюты, например "USD", "EUR".
        """
        code = code.upper().strip()
        data = self._make_request(self._URL_DAILY)
        rate_date = self._format_date(data.get("Date", ""))
        return self._parse_valute(code, data.get("Valute", {}), rate_date)

    def get_dynamics(self, code: str, days: int = 7) -> list[DynamicsPoint]:
        """Получить динамику курса за последние N дней.

        Args:
            code: код валюты, например "USD".
            days: количество дней (включая сегодняшний курс).
        """
        code = code.upper().strip()
        points: list[DynamicsPoint] = []

        for offset in range(days - 1, -1, -1):
            day = date.today() - timedelta(days=offset)
            if offset == 0:
                url = self._URL_DAILY
            else:
                url = f"{self._BASE}/archive/{day:%Y/%m/%d}/daily_json.js"
            try:
                data = self._make_request(url)
            except CurrencyNotFoundError:
                continue  # выходной без данных — пропускаем
            v = data.get("Valute", {}).get(code)
            if not v:
                raise CurrencyNotFoundError(f"Валюта «{code}» не найдена.")
            points.append(DynamicsPoint(
                day=day.strftime("%d.%m"),
                rub_price=float(v["Value"]) / int(v["Nominal"]),
            ))

        if not points:
            raise CBRAPIError("Не удалось получить динамику курса.")
        return points

    def convert_to_rub(self, amount: float, code: str) -> tuple[float, CurrencyRate]:
        """Конвертировать сумму в рубли.

        Args:
            amount: сумма в валюте.
            code: код валюты, например "USD".

        Returns:
            Кортеж (сумма в рублях, курс).
        """
        rate = self.get_currency(code)
        return amount * rate.rub_price, rate
