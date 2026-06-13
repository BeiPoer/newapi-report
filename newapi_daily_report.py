#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a daily operations report for one or more new-api instances.

The script uses new-api admin HTTP APIs with a system access token. It does not
connect to the database directly and only writes text/HTML report files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]


DEFAULT_QUOTA_PER_UNIT = 500000.0
DEFAULT_USD_EXCHANGE_RATE = 7.3
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_DISPLAY_CURRENCY = "CNY"
PAGE_SIZE = 100
LOG_TYPE_CONSUME = 2
REDEMPTION_STATUS_USED = 3
TOP_N = 10

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")


class ReportError(Exception):
    """User-facing report generation error."""


@dataclass
class SiteConfig:
    name: str = ""
    base_url: str = ""
    access_token: str = ""
    user_id: str = ""
    timezone: str = DEFAULT_TIMEZONE
    quota_per_unit: float = DEFAULT_QUOTA_PER_UNIT
    usd_exchange_rate: float = DEFAULT_USD_EXCHANGE_RATE
    display_currency: str = "auto"
    custom_currency_symbol: str = "¤"
    custom_currency_exchange_rate: float = 1.0
    timeout_seconds: int = 30
    request_delay_seconds: float = 0.5
    api_rate_limit_max_requests: int = 150
    api_rate_limit_window_seconds: float = 180.0
    max_retries: int = 6
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 60.0


Config = SiteConfig


@dataclass
class AppConfig:
    timezone: str = DEFAULT_TIMEZONE
    quota_per_unit: float = DEFAULT_QUOTA_PER_UNIT
    usd_exchange_rate: float = DEFAULT_USD_EXCHANGE_RATE
    display_currency: str = "auto"
    custom_currency_symbol: str = "¤"
    custom_currency_exchange_rate: float = 1.0
    timeout_seconds: int = 30
    request_delay_seconds: float = 0.5
    api_rate_limit_max_requests: int = 150
    api_rate_limit_window_seconds: float = 180.0
    max_retries: int = 6
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 60.0
    report_base_url: str = ""
    sites: list[SiteConfig] = field(default_factory=list)


@dataclass
class RuntimeOptions:
    target_date: dt.date
    start_time: dt.datetime
    end_time: dt.datetime
    start_ts: int
    end_ts: int
    timezone_name: str
    output_dir: Path
    output_format: str


@dataclass
class MoneySettings:
    quota_per_unit: float
    usd_exchange_rate: float
    display_currency: str
    currency_symbol: str
    custom_currency_symbol: str = "¤"
    custom_currency_exchange_rate: float = 1.0
    options_loaded: bool = False
    warning: str = ""


@dataclass
class UserInfo:
    user_id: int
    username: str = ""
    phone: str = ""
    group: str = ""
    quota: int = 0
    used_quota: int = 0
    created_at: int = 0
    status: int = 0


@dataclass
class RechargeUserStats:
    user_id: int
    today_amount: float = 0.0
    historical_amount: float = 0.0
    today_count: int = 0
    latest_complete_time: int = 0
    methods: set[str] = field(default_factory=set)
    providers: set[str] = field(default_factory=set)


@dataclass
class ConsumeUserStats:
    user_id: int
    quota: int = 0
    request_count: int = 0
    tokens: int = 0
    model_quota: dict[str, int] = field(default_factory=dict)
    username_from_logs: str = ""


@dataclass
class ReportData:
    users: dict[int, UserInfo]
    today_new_users: list[UserInfo]
    all_success_topups: list[dict[str, Any]]
    today_success_topups: list[dict[str, Any]]
    all_used_redemptions: list[dict[str, Any]]
    today_used_redemptions: list[dict[str, Any]]
    recharge_users: list[RechargeUserStats]
    today_consume_logs: list[dict[str, Any]]
    consume_users: list[ConsumeUserStats]
    consume_top10: list[ConsumeUserStats]
    stat_quota: int
    money: MoneySettings
    warnings: list[str]


@dataclass
class SiteReport:
    site: SiteConfig
    runtime: RuntimeOptions
    data: ReportData | None = None
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成 new-api 运营日报，只输出文字简报和 HTML 单页。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "date_arg",
        nargs="?",
        help="统计日期，支持 YYYYMMDD 或 YYYY-MM-DD。例如 20260613。",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("config.json")),
        help="配置文件路径。",
    )
    parser.add_argument(
        "--date",
        help="统计日期，支持 YYYYMMDD 或 YYYY-MM-DD。默认使用配置时区下的今天。",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).with_name("reports")),
        help="报表输出目录。",
    )
    parser.add_argument(
        "--format",
        choices=("text", "html", "all"),
        default="all",
        help="输出类型。all 表示微信纯文本简报 + HTML。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只验证配置和接口权限，不生成日报。",
    )
    return parser.parse_args()


def parse_report_date(value: str, source: str = "日期") -> dt.date:
    text = str(value or "").strip()
    if not text:
        raise ReportError(f"{source}不能为空。")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ReportError(f"{source}格式必须是 YYYYMMDD 或 YYYY-MM-DD，例如 20260613。") from exc


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ReportError(
            f"配置文件不存在：{path}\n"
            f"请复制 {path.with_name('config.example.json')} 为 config.json 后填写 sites。"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"配置文件不是合法 JSON：{path}，{exc}") from exc

    try:
        app_config = AppConfig(
            timezone=str(raw.get("timezone") or DEFAULT_TIMEZONE).strip(),
            quota_per_unit=float(raw.get("quota_per_unit", DEFAULT_QUOTA_PER_UNIT)),
            usd_exchange_rate=float(raw.get("usd_exchange_rate", DEFAULT_USD_EXCHANGE_RATE)),
            display_currency=str(raw.get("display_currency") or "auto").strip(),
            custom_currency_symbol=str(raw.get("custom_currency_symbol") or "¤").strip(),
            custom_currency_exchange_rate=float(raw.get("custom_currency_exchange_rate", 1.0)),
            timeout_seconds=int(raw.get("timeout_seconds", 30)),
            request_delay_seconds=float(raw.get("request_delay_seconds", 0.5)),
            api_rate_limit_max_requests=int(raw.get("api_rate_limit_max_requests", 150)),
            api_rate_limit_window_seconds=float(raw.get("api_rate_limit_window_seconds", 180.0)),
            max_retries=int(raw.get("max_retries", 6)),
            retry_base_seconds=float(raw.get("retry_base_seconds", 2.0)),
            retry_max_seconds=float(raw.get("retry_max_seconds", 60.0)),
            report_base_url=str(raw.get("report_base_url") or "").strip(),
        )
    except (TypeError, ValueError) as exc:
        raise ReportError(f"配置文件中数值字段格式错误：{exc}") from exc

    raw_sites = raw.get("sites")
    if raw_sites is None:
        raw_sites = [raw]
    if not isinstance(raw_sites, list) or not raw_sites:
        raise ReportError("配置文件缺少 sites 数组，或 sites 为空。")

    sites: list[SiteConfig] = []
    for index, raw_site in enumerate(raw_sites, 1):
        if not isinstance(raw_site, dict):
            raise ReportError(f"sites[{index}] 必须是对象。")
        site_name = str(raw_site.get("name") or f"站点{index}").strip()
        missing = [key for key in ("base_url", "access_token", "user_id") if not str(raw_site.get(key, "")).strip()]
        if missing:
            raise ReportError(f"{site_name} 缺少必填项：{', '.join(missing)}")
        try:
            sites.append(
                SiteConfig(
                    name=site_name,
                    base_url=str(raw_site["base_url"]).strip().rstrip("/"),
                    access_token=str(raw_site["access_token"]).strip(),
                    user_id=str(raw_site["user_id"]).strip(),
                    timezone=str(raw_site.get("timezone") or app_config.timezone).strip(),
                    quota_per_unit=float(raw_site.get("quota_per_unit", app_config.quota_per_unit)),
                    usd_exchange_rate=float(raw_site.get("usd_exchange_rate", app_config.usd_exchange_rate)),
                    display_currency=str(raw_site.get("display_currency", app_config.display_currency) or "auto").strip(),
                    custom_currency_symbol=str(raw_site.get("custom_currency_symbol", app_config.custom_currency_symbol) or "¤").strip(),
                    custom_currency_exchange_rate=float(
                        raw_site.get("custom_currency_exchange_rate", app_config.custom_currency_exchange_rate)
                    ),
                    timeout_seconds=int(raw_site.get("timeout_seconds", app_config.timeout_seconds)),
                    request_delay_seconds=float(raw_site.get("request_delay_seconds", app_config.request_delay_seconds)),
                    api_rate_limit_max_requests=int(
                        raw_site.get("api_rate_limit_max_requests", app_config.api_rate_limit_max_requests)
                    ),
                    api_rate_limit_window_seconds=float(
                        raw_site.get("api_rate_limit_window_seconds", app_config.api_rate_limit_window_seconds)
                    ),
                    max_retries=int(raw_site.get("max_retries", app_config.max_retries)),
                    retry_base_seconds=float(raw_site.get("retry_base_seconds", app_config.retry_base_seconds)),
                    retry_max_seconds=float(raw_site.get("retry_max_seconds", app_config.retry_max_seconds)),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ReportError(f"{site_name} 的数值字段格式错误：{exc}") from exc
    app_config.sites = sites
    return app_config


def get_timezone(name: str) -> dt.tzinfo:
    normalized = (name or DEFAULT_TIMEZONE).strip()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(normalized)
        except Exception:
            pass
    if normalized in {DEFAULT_TIMEZONE, "Asia/Chongqing", "Asia/Harbin", "Asia/Shanghai"}:
        return dt.timezone(dt.timedelta(hours=8), normalized)
    if normalized.upper() in {"UTC", "Z"}:
        return dt.timezone.utc
    if normalized.startswith(("UTC+", "UTC-")):
        sign = 1 if normalized[3] == "+" else -1
        offset_text = normalized[4:]
        try:
            if ":" in offset_text:
                hours_text, minutes_text = offset_text.split(":", 1)
                hours = int(hours_text)
                minutes = int(minutes_text)
            else:
                hours = int(offset_text)
                minutes = 0
            return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes), normalized)
        except ValueError:
            pass
    raise ReportError("当前 Python 不支持 zoneinfo，请使用 Python 3.9+ 或将 timezone 设置为 Asia/Shanghai。")


def build_runtime_options(args: argparse.Namespace, config: Config) -> RuntimeOptions:
    tz = get_timezone(config.timezone)
    now = dt.datetime.now(tz)
    if args.date and args.date_arg:
        cli_date = parse_report_date(args.date_arg, "位置日期参数")
        option_date = parse_report_date(args.date, "--date")
        if cli_date != option_date:
            raise ReportError("位置日期参数和 --date 指定了不同日期，请只保留一个。")
        target_date = cli_date
    elif args.date_arg:
        target_date = parse_report_date(args.date_arg, "位置日期参数")
    elif args.date:
        target_date = parse_report_date(args.date, "--date")
    else:
        target_date = now.date()

    start_time = dt.datetime.combine(target_date, dt.time.min, tzinfo=tz)
    if target_date == now.date():
        end_time = now
    else:
        end_time = dt.datetime.combine(target_date, dt.time.max, tzinfo=tz).replace(microsecond=0)

    output_dir = Path(args.output_dir).resolve()
    return RuntimeOptions(
        target_date=target_date,
        start_time=start_time,
        end_time=end_time,
        start_ts=int(start_time.timestamp()),
        end_ts=int(end_time.timestamp()),
        timezone_name=config.timezone,
        output_dir=output_dir,
        output_format=args.format,
    )


class NewApiClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._last_request_at = 0.0
        self._request_timestamps: deque[float] = deque()

    def get(self, path: str, params: dict[str, Any] | None = None, *, allow_failure: bool = False) -> Any:
        url = self._build_url(path, params)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {self.config.access_token}",
                "New-Api-User": self.config.user_id,
                "User-Agent": "newapi-daily-report/1.0",
            },
            method="GET",
        )
        retryable_statuses = {408, 429, 500, 502, 503, 504}
        max_retries = max(0, self.config.max_retries)
        for attempt in range(max_retries + 1):
            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    body = decode_response_body(response.read(), response.headers.get("Content-Encoding", ""))
                break
            except urllib.error.HTTPError as exc:
                body = decode_response_body(exc.read(), exc.headers.get("Content-Encoding", ""), errors="replace")
                if exc.code in retryable_statuses and attempt < max_retries:
                    wait_seconds = self._retry_wait_seconds(attempt, exc.headers)
                    self._log_retry(url, exc.code, attempt + 1, max_retries, wait_seconds)
                    time.sleep(wait_seconds)
                    continue
                if allow_failure:
                    return {"success": False, "message": f"HTTP {exc.code}: {body}", "data": None}
                raise ReportError(f"请求失败：{url}\nHTTP {exc.code}: {body}") from exc
            except urllib.error.URLError as exc:
                if attempt < max_retries:
                    wait_seconds = self._retry_wait_seconds(attempt, None)
                    self._log_retry(url, "连接错误", attempt + 1, max_retries, wait_seconds)
                    time.sleep(wait_seconds)
                    continue
                if allow_failure:
                    return {"success": False, "message": str(exc), "data": None}
                raise ReportError(f"无法连接 new-api：{url}\n{exc}") from exc
        else:  # pragma: no cover - defensive guard
            if allow_failure:
                return {"success": False, "message": "请求失败且未获得响应", "data": None}
            raise ReportError(f"请求失败且未获得响应：{url}")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            if allow_failure:
                return {"success": False, "message": f"响应不是 JSON：{body[:300]}", "data": None}
            raise ReportError(f"响应不是 JSON：{url}\n{body[:300]}") from exc

        if not isinstance(payload, dict):
            if allow_failure:
                return {"success": False, "message": "响应 JSON 不是对象", "data": None}
            raise ReportError(f"响应 JSON 不是对象：{url}")

        if payload.get("success") is not True:
            if allow_failure:
                return payload
            message = payload.get("message") or "接口返回失败"
            raise ReportError(f"接口返回失败：{url}\n{message}")
        return payload.get("data")

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        query = ""
        if params:
            clean_params = {key: value for key, value in params.items() if value is not None}
            query = "?" + urllib.parse.urlencode(clean_params)
        return f"{self.config.base_url}{path}{query}"

    def _throttle(self) -> None:
        self._enforce_window_rate_limit()
        delay = max(0.0, self.config.request_delay_seconds)
        if delay > 0:
            now = time.monotonic()
            wait_seconds = delay - (now - self._last_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        self._last_request_at = time.monotonic()
        self._request_timestamps.append(self._last_request_at)

    def _enforce_window_rate_limit(self) -> None:
        max_requests = self.config.api_rate_limit_max_requests
        window_seconds = self.config.api_rate_limit_window_seconds
        if max_requests <= 0 or window_seconds <= 0:
            return
        while True:
            now = time.monotonic()
            cutoff = now - window_seconds
            while self._request_timestamps and self._request_timestamps[0] <= cutoff:
                self._request_timestamps.popleft()
            if len(self._request_timestamps) < max_requests:
                return
            wait_seconds = self._request_timestamps[0] + window_seconds - now + 0.25
            if wait_seconds <= 0:
                continue
            self._log_rate_limit_wait(wait_seconds, max_requests, window_seconds)
            time.sleep(wait_seconds)

    def _retry_wait_seconds(self, attempt: int, headers: Any) -> float:
        retry_after = parse_retry_after(headers)
        if retry_after is not None:
            return min(max(0.0, retry_after), max(0.0, self.config.retry_max_seconds))
        base = max(0.1, self.config.retry_base_seconds)
        delay = base * (2 ** attempt)
        return min(delay, max(0.1, self.config.retry_max_seconds))

    def _log_retry(self, url: str, reason: Any, attempt: int, max_retries: int, wait_seconds: float) -> None:
        print(
            f"请求触发重试，{wait_seconds:.1f}s 后继续（{attempt}/{max_retries}，原因：{reason}）：{url}",
            file=sys.stderr,
        )

    def _log_rate_limit_wait(self, wait_seconds: float, max_requests: int, window_seconds: float) -> None:
        print(
            f"达到客户端限速：{window_seconds:g}s 内最多 {max_requests} 次请求，等待 {wait_seconds:.1f}s 后继续。",
            file=sys.stderr,
        )


def decode_response_body(body: bytes, content_encoding: str, *, errors: str = "strict") -> str:
    if not body:
        return ""
    encoding = (content_encoding or "").lower().strip()
    if "br" in encoding:
        raise ReportError("接口返回了 Brotli 压缩响应，但脚本只使用标准库；请让反向代理关闭 br 压缩或返回 gzip/identity。")
    try:
        if "gzip" in encoding or body.startswith(b"\x1f\x8b"):
            body = gzip.decompress(body)
        elif "deflate" in encoding:
            body = zlib.decompress(body)
    except (OSError, zlib.error):
        return body.decode("utf-8", errors=errors)
    return body.decode("utf-8", errors=errors)


def parse_retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    raw = ""
    try:
        raw = str(headers.get("Retry-After", "") or "").strip()
    except AttributeError:
        raw = ""
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds())


def fetch_page(client: NewApiClient, path: str, page: int, extra_params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = {"p": page, "page_size": PAGE_SIZE}
    if extra_params:
        params.update(extra_params)
    data = client.get(path, params)
    if not isinstance(data, dict):
        raise ReportError(f"分页接口返回 data 不是对象：{path}")
    items = data.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ReportError(f"分页接口返回 items 不是列表：{path}")
    data["items"] = items
    data["total"] = int(data.get("total") or 0)
    return data


def iter_paged_items(
    client: NewApiClient,
    path: str,
    extra_params: dict[str, Any] | None = None,
    *,
    stop_after_page=None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        data = fetch_page(client, path, page, extra_params)
        page_items = [item for item in data["items"] if isinstance(item, dict)]
        items.extend(page_items)
        total = data["total"]
        if stop_after_page is not None and stop_after_page(page_items):
            break
        if not page_items or page * PAGE_SIZE >= total:
            break
        page += 1
    return items


def iter_paged_items_partial(
    client: NewApiClient,
    path: str,
    extra_params: dict[str, Any] | None = None,
    *,
    label: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    page = 1
    while True:
        try:
            data = fetch_page(client, path, page, extra_params)
        except ReportError as exc:
            warnings.append(f"{label}读取到第 {page} 页时失败，已使用前 {format_int(len(items))} 条记录继续生成报表：{exc}")
            break
        page_items = [item for item in data["items"] if isinstance(item, dict)]
        items.extend(page_items)
        total = data["total"]
        if not page_items or page * PAGE_SIZE >= total:
            break
        page += 1
    return items, warnings


def parse_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def fetch_options(client: NewApiClient, config: Config) -> MoneySettings:
    settings = MoneySettings(
        quota_per_unit=config.quota_per_unit if config.quota_per_unit > 0 else DEFAULT_QUOTA_PER_UNIT,
        usd_exchange_rate=config.usd_exchange_rate if config.usd_exchange_rate > 0 else DEFAULT_USD_EXCHANGE_RATE,
        display_currency=(config.display_currency or "auto").upper(),
        currency_symbol="",
        custom_currency_symbol=config.custom_currency_symbol or "¤",
        custom_currency_exchange_rate=config.custom_currency_exchange_rate
        if config.custom_currency_exchange_rate > 0
        else 1.0,
    )

    payload = client.get("/api/option/", allow_failure=True)
    if not isinstance(payload, list):
        settings.warning = "系统配置接口不可访问，金额展示使用 config.json 默认值。"
        normalize_money_settings(settings)
        return settings

    option_map: dict[str, str] = {}
    for item in payload:
        if isinstance(item, dict) and "key" in item:
            option_map[str(item.get("key"))] = str(item.get("value", ""))

    if option_map.get("QuotaPerUnit"):
        settings.quota_per_unit = parse_float(option_map["QuotaPerUnit"], settings.quota_per_unit)
    if option_map.get("USDExchangeRate"):
        settings.usd_exchange_rate = parse_float(option_map["USDExchangeRate"], settings.usd_exchange_rate)

    if settings.display_currency == "AUTO":
        settings.display_currency = option_map.get("general_setting.quota_display_type", "").upper() or DEFAULT_DISPLAY_CURRENCY
    if option_map.get("general_setting.custom_currency_symbol"):
        settings.custom_currency_symbol = option_map["general_setting.custom_currency_symbol"]
    if option_map.get("general_setting.custom_currency_exchange_rate"):
        settings.custom_currency_exchange_rate = parse_float(
            option_map["general_setting.custom_currency_exchange_rate"],
            settings.custom_currency_exchange_rate,
        )

    settings.options_loaded = True
    normalize_money_settings(settings)
    return settings


def normalize_money_settings(settings: MoneySettings) -> None:
    if settings.quota_per_unit <= 0:
        settings.quota_per_unit = DEFAULT_QUOTA_PER_UNIT
    if settings.usd_exchange_rate <= 0:
        settings.usd_exchange_rate = DEFAULT_USD_EXCHANGE_RATE

    display = (settings.display_currency or DEFAULT_DISPLAY_CURRENCY).upper()
    if display == "AUTO":
        display = DEFAULT_DISPLAY_CURRENCY
    if display not in {"USD", "CNY", "TOKENS", "CUSTOM"}:
        display = DEFAULT_DISPLAY_CURRENCY
    settings.display_currency = display

    if display == "USD":
        settings.currency_symbol = "$"
    elif display == "CNY":
        settings.currency_symbol = "￥"
    elif display == "CUSTOM":
        settings.currency_symbol = settings.custom_currency_symbol or "¤"
        if settings.custom_currency_exchange_rate <= 0:
            settings.custom_currency_exchange_rate = 1.0
    else:
        settings.currency_symbol = ""


def quota_to_display_amount(quota: int | float, settings: MoneySettings) -> float:
    if settings.display_currency == "TOKENS":
        return float(quota)
    usd = float(quota) / settings.quota_per_unit
    if settings.display_currency == "CNY":
        return usd * settings.usd_exchange_rate
    if settings.display_currency == "CUSTOM":
        return usd * settings.custom_currency_exchange_rate
    return usd


def format_money(amount: float, settings: MoneySettings) -> str:
    if settings.display_currency == "TOKENS":
        return f"{amount:,.0f} tokens"
    symbol = settings.currency_symbol
    return f"{symbol}{amount:,.2f}"


def format_quota_money(quota: int | float, settings: MoneySettings) -> str:
    return format_money(quota_to_display_amount(quota, settings), settings)


def format_percent(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        return "0.0%"
    return f"{value * 100:.1f}%"


def format_int(value: int | float) -> str:
    return f"{int(value):,}"


def format_ts(timestamp: int, timezone_name: str) -> str:
    if timestamp <= 0:
        return "-"
    tz = get_timezone(timezone_name)
    return dt.datetime.fromtimestamp(timestamp, tz).strftime("%Y-%m-%d %H:%M:%S")


def topup_display_amount(topup: dict[str, Any], settings: MoneySettings) -> float:
    method = safe_text(topup.get("payment_method"))
    money = parse_float(topup.get("money"), 0.0)
    amount = parse_float(topup.get("amount"), 0.0)
    if method == "enterprise_alipay_cny" and settings.display_currency == "CNY":
        return amount
    if money > 0:
        if settings.display_currency == "USD":
            return money
        if settings.display_currency == "CNY":
            return money * settings.usd_exchange_rate
        if settings.display_currency == "CUSTOM":
            return money * settings.custom_currency_exchange_rate
        if settings.display_currency == "TOKENS":
            return money * settings.quota_per_unit
    if settings.display_currency == "TOKENS":
        return amount * settings.quota_per_unit
    if settings.display_currency == "CNY":
        return amount * settings.usd_exchange_rate
    if settings.display_currency == "CUSTOM":
        return amount * settings.custom_currency_exchange_rate
    return amount


def redemption_user_id(redemption: dict[str, Any]) -> int:
    user_id = parse_int(redemption.get("used_user_id"))
    if user_id <= 0:
        user_id = parse_int(redemption.get("user_id"))
    return user_id


def redemption_display_amount(redemption: dict[str, Any], settings: MoneySettings) -> float:
    return quota_to_display_amount(parse_int(redemption.get("quota")), settings)


def is_today_timestamp(timestamp: int, runtime: RuntimeOptions) -> bool:
    return runtime.start_ts <= timestamp <= runtime.end_ts


def fetch_users(client: NewApiClient, runtime: RuntimeOptions) -> tuple[dict[int, UserInfo], list[UserInfo]]:
    raw_users = iter_paged_items(client, "/api/user/")
    users: dict[int, UserInfo] = {}
    today_new: list[UserInfo] = []
    for item in raw_users:
        user_id = parse_int(item.get("id"))
        if user_id <= 0:
            continue
        user = UserInfo(
            user_id=user_id,
            username=safe_text(item.get("username")) or safe_text(item.get("display_name")),
            phone=safe_text(item.get("phone")),
            group=safe_text(item.get("group")),
            quota=parse_int(item.get("quota")),
            used_quota=parse_int(item.get("used_quota")),
            created_at=parse_int(item.get("created_at")),
            status=parse_int(item.get("status")),
        )
        users[user_id] = user
        if is_today_timestamp(user.created_at, runtime):
            today_new.append(user)
    today_new.sort(key=lambda user: (user.created_at, user.user_id), reverse=True)
    return users, today_new


def fetch_topups(client: NewApiClient, runtime: RuntimeOptions) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_topups = iter_paged_items(client, "/api/user/topup")
    success_topups: list[dict[str, Any]] = []
    today_topups: list[dict[str, Any]] = []
    for item in all_topups:
        if safe_text(item.get("status")).lower() != "success":
            continue
        complete_time = parse_int(item.get("complete_time"))
        if complete_time <= 0:
            continue
        success_topups.append(item)
        if is_today_timestamp(complete_time, runtime):
            today_topups.append(item)
    return success_topups, today_topups


def fetch_redemptions(client: NewApiClient, runtime: RuntimeOptions) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    raw_redemptions, warnings = iter_paged_items_partial(client, "/api/redemption/", label="兑换码")
    used_redemptions: list[dict[str, Any]] = []
    today_redemptions: list[dict[str, Any]] = []
    for item in raw_redemptions:
        if parse_int(item.get("status")) != REDEMPTION_STATUS_USED:
            continue
        redeemed_time = parse_int(item.get("redeemed_time"))
        user_id = redemption_user_id(item)
        quota = parse_int(item.get("quota"))
        if redeemed_time <= 0 or user_id <= 0 or quota <= 0:
            continue
        used_redemptions.append(item)
        if is_today_timestamp(redeemed_time, runtime):
            today_redemptions.append(item)
    return used_redemptions, today_redemptions, warnings


def fetch_consume_logs(client: NewApiClient, runtime: RuntimeOptions) -> tuple[list[dict[str, Any]], int, list[str]]:
    warnings: list[str] = []
    stat_quota = 0
    try:
        stat_data = client.get(
            "/api/log/stat",
            {
                "type": LOG_TYPE_CONSUME,
                "start_timestamp": runtime.start_ts,
                "end_timestamp": runtime.end_ts,
            },
        )
        if isinstance(stat_data, dict):
            stat_quota = parse_int(stat_data.get("quota"))
        else:
            warnings.append("消费统计接口返回格式异常，今日消耗金额使用消费日志聚合结果。")
    except ReportError as exc:
        warnings.append(f"消费统计接口读取失败，今日消耗金额使用消费日志聚合结果：{exc}")

    logs, log_warnings = iter_paged_items_partial(
        client,
        "/api/log/",
        {
            "type": LOG_TYPE_CONSUME,
            "start_timestamp": runtime.start_ts,
            "end_timestamp": runtime.end_ts,
        },
        label="消费日志",
    )
    warnings.extend(log_warnings)
    log_quota = sum(parse_int(item.get("quota")) for item in logs)
    if stat_quota == 0 and log_quota > 0:
        stat_quota = log_quota
    elif stat_quota != log_quota and logs:
        warnings.append(
            f"消费统计接口 quota({format_int(stat_quota)}) 与日志聚合 quota({format_int(log_quota)}) 不一致，"
            "日报总消耗采用统计接口值，排行采用日志聚合。"
        )
    return logs, stat_quota, warnings


def aggregate_recharge_users(
    success_topups: list[dict[str, Any]],
    today_topups: list[dict[str, Any]],
    used_redemptions: list[dict[str, Any]],
    today_redemptions: list[dict[str, Any]],
    settings: MoneySettings,
) -> list[RechargeUserStats]:
    historical_amounts = aggregate_historical_recharge_amounts(success_topups, used_redemptions, settings)
    stats: dict[int, RechargeUserStats] = {}
    for user_id, amount in historical_amounts.items():
        item = stats.setdefault(user_id, RechargeUserStats(user_id=user_id))
        item.historical_amount = amount

    for topup in today_topups:
        user_id = parse_int(topup.get("user_id"))
        if user_id <= 0:
            continue
        item = stats.setdefault(user_id, RechargeUserStats(user_id=user_id))
        amount = topup_display_amount(topup, settings)
        item.today_amount += amount
        item.today_count += 1
        item.latest_complete_time = max(item.latest_complete_time, parse_int(topup.get("complete_time")))
        if safe_text(topup.get("payment_method")):
            item.methods.add(safe_text(topup.get("payment_method")))
        if safe_text(topup.get("payment_provider")):
            item.providers.add(safe_text(topup.get("payment_provider")))

    for redemption in today_redemptions:
        user_id = redemption_user_id(redemption)
        if user_id <= 0:
            continue
        item = stats.setdefault(user_id, RechargeUserStats(user_id=user_id))
        amount = redemption_display_amount(redemption, settings)
        item.today_amount += amount
        item.today_count += 1
        item.latest_complete_time = max(item.latest_complete_time, parse_int(redemption.get("redeemed_time")))
        item.methods.add("兑换码")
        item.providers.add("redemption")

    today_users = [item for item in stats.values() if item.today_count > 0]
    today_users.sort(key=lambda item: (-item.today_amount, item.user_id))
    return today_users


def aggregate_historical_recharge_amounts(
    success_topups: list[dict[str, Any]],
    used_redemptions: list[dict[str, Any]],
    settings: MoneySettings,
) -> dict[int, float]:
    amounts: dict[int, float] = {}
    for topup in success_topups:
        user_id = parse_int(topup.get("user_id"))
        if user_id <= 0:
            continue
        amounts[user_id] = amounts.get(user_id, 0.0) + topup_display_amount(topup, settings)
    for redemption in used_redemptions:
        user_id = redemption_user_id(redemption)
        if user_id <= 0:
            continue
        amounts[user_id] = amounts.get(user_id, 0.0) + redemption_display_amount(redemption, settings)
    return amounts


def aggregate_consume_users(logs: list[dict[str, Any]]) -> list[ConsumeUserStats]:
    stats: dict[int, ConsumeUserStats] = {}
    for log in logs:
        user_id = parse_int(log.get("user_id"))
        if user_id <= 0:
            continue
        item = stats.setdefault(user_id, ConsumeUserStats(user_id=user_id))
        quota = parse_int(log.get("quota"))
        item.quota += quota
        item.request_count += 1
        item.tokens += parse_int(log.get("prompt_tokens")) + parse_int(log.get("completion_tokens"))
        model_name = safe_text(log.get("model_name")) or "未知模型"
        item.model_quota[model_name] = item.model_quota.get(model_name, 0) + quota
        if not item.username_from_logs:
            item.username_from_logs = safe_text(log.get("username"))
    users = list(stats.values())
    users.sort(key=lambda item: (-item.quota, -item.request_count, item.user_id))
    return users


def build_report_data(client: NewApiClient, config: Config, runtime: RuntimeOptions) -> ReportData:
    warnings: list[str] = []
    money = fetch_options(client, config)
    if money.warning:
        warnings.append(money.warning)

    users, today_new_users = fetch_users(client, runtime)
    success_topups, today_topups = fetch_topups(client, runtime)
    used_redemptions, today_redemptions, redemption_warnings = fetch_redemptions(client, runtime)
    warnings.extend(redemption_warnings)
    consume_logs, stat_quota, consume_warnings = fetch_consume_logs(client, runtime)
    warnings.extend(consume_warnings)

    recharge_users = aggregate_recharge_users(success_topups, today_topups, used_redemptions, today_redemptions, money)
    consume_users = aggregate_consume_users(consume_logs)
    consume_top10 = consume_users[:TOP_N]

    missing_recharge_users = [item.user_id for item in recharge_users if item.user_id not in users]
    missing_consume_users = [item.user_id for item in consume_top10 if item.user_id not in users]
    if missing_recharge_users:
        warnings.append(f"部分充值用户在用户列表中缺失：{', '.join(map(str, missing_recharge_users[:10]))}。")
    if missing_consume_users:
        warnings.append(f"消耗 Top 10 中部分用户在用户列表中缺失：{', '.join(map(str, missing_consume_users[:10]))}。")
    if not consume_logs and stat_quota > 0:
        warnings.append("消费统计有金额但消费日志为空，今日消耗人数和 Top 10 可能偏低或为空。")
    if not consume_logs and stat_quota == 0:
        warnings.append("今日未读取到消费日志；若实际有请求，请确认系统是否启用了消费日志记录。")

    return ReportData(
        users=users,
        today_new_users=today_new_users,
        all_success_topups=success_topups,
        today_success_topups=today_topups,
        all_used_redemptions=used_redemptions,
        today_used_redemptions=today_redemptions,
        recharge_users=recharge_users,
        today_consume_logs=consume_logs,
        consume_users=consume_users,
        consume_top10=consume_top10,
        stat_quota=stat_quota,
        money=money,
        warnings=dedupe_strings(warnings),
    )


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def get_user_name(users: dict[int, UserInfo], user_id: int, fallback: str = "") -> str:
    user = users.get(user_id)
    if user and user.username:
        return user.username
    return fallback or f"用户 {user_id}"


def get_user_phone(users: dict[int, UserInfo], user_id: int) -> str:
    user = users.get(user_id)
    return user.phone if user else ""


def get_user_group(users: dict[int, UserInfo], user_id: int) -> str:
    user = users.get(user_id)
    return user.group if user else ""


def get_user_quota(users: dict[int, UserInfo], user_id: int) -> int:
    user = users.get(user_id)
    return user.quota if user else 0


def methods_summary(item: RechargeUserStats) -> str:
    parts = sorted(value for value in item.methods if value)
    providers = sorted(value for value in item.providers if value)
    if providers:
        parts.extend(f"网关:{value}" for value in providers)
    return " / ".join(parts) if parts else "-"


def main_model(item: ConsumeUserStats) -> str:
    if not item.model_quota:
        return "-"
    model, quota = sorted(item.model_quota.items(), key=lambda pair: (-pair[1], pair[0]))[0]
    return f"{model}({format_int(quota)})"


def concentration(amounts: list[float], n: int) -> float:
    total = sum(amounts)
    if total <= 0:
        return 0.0
    return sum(sorted(amounts, reverse=True)[:n]) / total


def quota_concentration(items: list[ConsumeUserStats], n: int) -> float:
    total = sum(item.quota for item in items)
    if total <= 0:
        return 0.0
    return sum(item.quota for item in items[:n]) / total


def build_insights(data: ReportData) -> list[str]:
    insights: list[str] = []
    recharge_amounts = [item.today_amount for item in data.recharge_users]
    consume_user_ids = {item.user_id for item in data.consume_users}
    recharge_user_ids = {item.user_id for item in data.recharge_users}
    new_user_ids = {item.user_id for item in data.today_new_users}
    top_consume_ids = {item.user_id for item in data.consume_top10}

    insights.append(
        "充值集中度：Top1 / Top3 / Top10 分别贡献 "
        f"{format_percent(concentration(recharge_amounts, 1))}、"
        f"{format_percent(concentration(recharge_amounts, 3))}、"
        f"{format_percent(concentration(recharge_amounts, 10))}。"
    )
    insights.append(
        "消耗集中度：Top1 / Top3 / Top10 分别贡献 "
        f"{format_percent(quota_concentration(data.consume_users, 1))}、"
        f"{format_percent(quota_concentration(data.consume_users, 3))}、"
        f"{format_percent(quota_concentration(data.consume_users, 10))}。"
    )

    new_recharge_ids = recharge_user_ids & new_user_ids
    insights.append(
        f"新客转化：今日新增注册 {len(new_user_ids)} 人，其中当日完成充值 {len(new_recharge_ids)} 人。"
    )

    used_after_recharge = recharge_user_ids & consume_user_ids
    insights.append(
        f"充值后使用：今日充值用户 {len(recharge_user_ids)} 人，其中今日有消耗 {len(used_after_recharge)} 人。"
    )

    high_value_ids = recharge_user_ids & top_consume_ids
    if high_value_ids:
        names = ", ".join(get_user_name(data.users, user_id) for user_id in sorted(high_value_ids))
        insights.append(f"高价值用户：同时出现在今日充值用户和消耗 Top10 中的用户为 {names}。")
    else:
        insights.append("高价值用户：今日充值用户与消耗 Top10 暂无交集。")

    inactive_ids = recharge_user_ids - consume_user_ids
    if inactive_ids:
        top_inactive = sorted(
            (item for item in data.recharge_users if item.user_id in inactive_ids),
            key=lambda item: (-item.today_amount, item.user_id),
        )[:5]
        names = ", ".join(
            f"{get_user_name(data.users, item.user_id)}({format_money(item.today_amount, data.money)})"
            for item in top_inactive
        )
        insights.append(f"待激活用户：今日有充值但无消耗 {len(inactive_ids)} 人，金额较高的包括 {names}。")
    else:
        insights.append("待激活用户：今日充值用户均已有消耗记录。")

    high_consume_no_recharge = [item for item in data.consume_top10 if item.user_id not in recharge_user_ids]
    if high_consume_no_recharge:
        names = ", ".join(
            f"{get_user_name(data.users, item.user_id, item.username_from_logs)}({format_quota_money(item.quota, data.money)})"
            for item in high_consume_no_recharge[:5]
        )
        insights.append(f"高消耗未充值：消耗 Top10 中今日未充值用户 {len(high_consume_no_recharge)} 人，包括 {names}。")
    else:
        insights.append("高消耗未充值：消耗 Top10 用户今日均有充值记录。")

    return insights


def build_kpis(data: ReportData) -> dict[str, Any]:
    total_recharge = sum(item.today_amount for item in data.recharge_users)
    recharge_people = len(data.recharge_users)
    recharge_count = len(data.today_success_topups) + len(data.today_used_redemptions)
    avg_recharge = total_recharge / recharge_people if recharge_people else 0.0
    total_consume_amount = quota_to_display_amount(data.stat_quota, data.money)
    consume_people = len(data.consume_users)
    new_user_count = len(data.today_new_users)
    return {
        "total_recharge": total_recharge,
        "recharge_people": recharge_people,
        "recharge_count": recharge_count,
        "avg_recharge": avg_recharge,
        "total_consume_amount": total_consume_amount,
        "consume_people": consume_people,
        "new_user_count": new_user_count,
    }


def top_recharge_amounts(data: ReportData, limit: int = 3) -> list[float]:
    return [item.today_amount for item in data.recharge_users[:limit]]


def top_consume_amounts(data: ReportData, limit: int = 3) -> list[float]:
    return [quota_to_display_amount(item.quota, data.money) for item in data.consume_top10[:limit]]


def format_top_amounts(amounts: list[float], settings: MoneySettings) -> str:
    if not amounts:
        return "暂无"
    return "，".join(f"第{index + 1}名 {format_money(amount, settings)}" for index, amount in enumerate(amounts))


def render_site_text_brief(report: SiteReport) -> list[str]:
    if report.data is None:
        return [
            f"【{report.site.name}】",
            f"统计至 {report.runtime.end_time.strftime('%H:%M')}",
            f"获取失败：{report.error or '未知错误'}",
        ]
    data = report.data
    runtime = report.runtime
    kpis = build_kpis(data)
    lines: list[str] = []
    lines.append(f"【{report.site.name}】")
    lines.append(f"统计至 {runtime.end_time.strftime('%H:%M')}")
    lines.append(f"充值：{format_money(kpis['total_recharge'], data.money)} / {format_int(kpis['recharge_people'])}人 / {format_int(kpis['recharge_count'])}笔")
    lines.append(f"消耗：{format_money(kpis['total_consume_amount'], data.money)} / {format_int(kpis['consume_people'])}人")
    lines.append(f"新增注册：{format_int(kpis['new_user_count'])}人")
    lines.append(f"充值Top3：{format_top_amounts(top_recharge_amounts(data), data.money)}")
    lines.append(f"消耗Top3：{format_top_amounts(top_consume_amounts(data), data.money)}")
    if data.warnings:
        lines.append("")
        if len(data.warnings) == 1:
            lines.append(f"提示：{data.warnings[0]}")
        else:
            for index, warning in enumerate(data.warnings, start=1):
                lines.append(f"提示{index}：{warning}")
    return lines


def render_text_brief(reports: list[SiteReport]) -> str:
    return render_text_brief_with_url(reports, "")


def render_text_brief_with_url(reports: list[SiteReport], report_base_url: str = "") -> str:
    if not reports:
        return "new-api 运营日报\n无可用站点配置。\n"

    first_runtime = reports[0].runtime
    lines: list[str] = []
    lines.append(f"new-api 运营日报 {first_runtime.target_date.isoformat()}")
    lines.append(f"站点数：{len(reports)}")
    html_url = build_public_report_url(report_base_url, html_report_filename(first_runtime))
    if html_url and first_runtime.output_format in {"html", "all"}:
        lines.append(f"HTML详情：{html_url}")
    lines.append("")
    for index, report in enumerate(reports):
        if index > 0:
            lines.append("")
        lines.extend(render_site_text_brief(report))
    return "\n".join(lines) + "\n"


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_site_html_block(report: SiteReport) -> str:
    if report.data is None:
        return f"""
    <article class="site-report">
      <section class="site-heading site-error">
        <h2>{esc(report.site.name)}</h2>
        <p>{esc(report.site.base_url)}</p>
        <div class="error-message">获取失败：{esc(report.error or '未知错误')}</div>
      </section>
    </article>"""

    data = report.data
    runtime = report.runtime
    kpis = build_kpis(data)
    insights = build_insights(data)
    recharge_by_user = {item.user_id: item for item in data.recharge_users}
    historical_by_user = aggregate_historical_recharge_amounts(
        data.all_success_topups,
        data.all_used_redemptions,
        data.money,
    )

    kpi_cards = [
        ("总充值金额", format_money(kpis["total_recharge"], data.money)),
        ("充值人数", f"{format_int(kpis['recharge_people'])} 人"),
        ("充值笔数", f"{format_int(kpis['recharge_count'])} 笔"),
        ("人均充值", format_money(kpis["avg_recharge"], data.money)),
        ("消耗金额", format_money(kpis["total_consume_amount"], data.money)),
        ("消耗人数", f"{format_int(kpis['consume_people'])} 人"),
        ("新增注册", f"{format_int(kpis['new_user_count'])} 人"),
    ]

    recharge_rows = ""
    if data.recharge_users:
        for item in data.recharge_users:
            recharge_rows += f"""
            <tr>
              <td>{item.user_id}</td>
              <td>{esc(get_user_name(data.users, item.user_id))}</td>
              <td>{esc(get_user_phone(data.users, item.user_id) or "-")}</td>
              <td class="num">{esc(format_money(item.today_amount, data.money))}</td>
              <td class="num">{esc(format_money(item.historical_amount, data.money))}</td>
              <td class="num">{esc(format_int(item.today_count))}</td>
              <td>{esc(format_ts(item.latest_complete_time, runtime.timezone_name))}</td>
              <td>{esc(methods_summary(item))}</td>
            </tr>"""
    else:
        recharge_rows = '<tr><td colspan="8" class="empty">今日暂无成功充值。</td></tr>'

    consume_rows = ""
    if data.consume_top10:
        for index, item in enumerate(data.consume_top10, 1):
            recharge = recharge_by_user.get(item.user_id)
            today_recharge = recharge.today_amount if recharge else 0.0
            historical = historical_by_user.get(item.user_id, 0.0)
            consume_rows += f"""
            <tr>
              <td class="rank">{index}</td>
              <td>{item.user_id}</td>
              <td>{esc(get_user_name(data.users, item.user_id, item.username_from_logs))}</td>
              <td>{esc(get_user_phone(data.users, item.user_id) or "-")}</td>
              <td class="num">{esc(format_quota_money(item.quota, data.money))}</td>
              <td class="num">{esc(format_int(item.quota))}</td>
              <td class="num">{esc(format_int(item.request_count))}</td>
              <td class="num">{esc(format_int(item.tokens))}</td>
              <td>{esc(main_model(item))}</td>
              <td>{'是' if item.user_id in recharge_by_user else '否'}</td>
              <td class="num">{esc(format_money(today_recharge, data.money))}</td>
              <td class="num">{esc(format_money(historical, data.money))}</td>
              <td class="num">{esc(format_quota_money(get_user_quota(data.users, item.user_id), data.money))}</td>
            </tr>"""
    else:
        consume_rows = '<tr><td colspan="13" class="empty">今日暂无消费日志，无法生成消耗排行。</td></tr>'

    warning_block = ""
    if data.warnings:
        warning_items = "\n".join(f"<li>{esc(item)}</li>" for item in data.warnings)
        warning_block = f"""
        <section>
          <h2>异常提示</h2>
          <ul class="analysis warning-list">
            {warning_items}
          </ul>
        </section>"""

    cards_html = "\n".join(
        f'<div class="kpi-card"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>'
        for label, value in kpi_cards
    )
    insights_html = "\n".join(f"<li>{esc(item)}</li>" for item in insights)

    return f"""
    <article class="site-report">
      <section class="site-heading">
        <h2>{esc(report.site.name)}</h2>
        <p>{esc(runtime.start_time.strftime('%Y-%m-%d %H:%M:%S'))} - {esc(runtime.end_time.strftime('%Y-%m-%d %H:%M:%S'))}（{esc(runtime.timezone_name)}）</p>
        <p>{esc(report.site.base_url)}</p>
      </section>

      <section>
        <h2>核心指标</h2>
        <div class="kpi-grid">
          {cards_html}
        </div>
      </section>

      <section>
        <h2>充值用户明细</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>用户ID</th>
                <th>用户名</th>
                <th>手机号</th>
                <th class="num">今日充值金额</th>
                <th class="num">历史总充值金额</th>
                <th class="num">今日充值笔数</th>
                <th>最近充值时间</th>
                <th>来源/支付方式</th>
              </tr>
            </thead>
            <tbody>{recharge_rows}</tbody>
          </table>
        </div>
      </section>

      <section>
        <h2>消耗排行 Top 10</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th class="num">排名</th>
                <th>用户ID</th>
                <th>用户名</th>
                <th>手机号</th>
                <th class="num">今日消耗金额</th>
                <th class="num">今日消耗 quota</th>
                <th class="num">请求次数</th>
                <th class="num">token 数</th>
                <th>主要消耗模型</th>
                <th>今日充值</th>
                <th class="num">今日充值金额</th>
                <th class="num">历史总充值金额</th>
                <th class="num">当前余额</th>
              </tr>
            </thead>
            <tbody>{consume_rows}</tbody>
          </table>
        </div>
      </section>

      <section>
        <h2>运营分析</h2>
        <ul class="analysis">
          {insights_html}
        </ul>
      </section>

      {warning_block}

      <section>
        <h2>口径说明</h2>
        <p class="footnote">
          金额展示口径：{esc(data.money.display_currency)}，
          QuotaPerUnit={esc(f'{data.money.quota_per_unit:g}')}，
          USDExchangeRate={esc(f'{data.money.usd_exchange_rate:g}')}。
          今日充值按成功支付订单 complete_time 和已使用兑换码 redeemed_time 归属统计日期；
          今日消耗按消费日志 type=2 聚合；
          历史总充值金额基于当前接口可拉取到的全部成功支付订单和已使用兑换码聚合。
        </p>
      </section>
    </article>"""


def render_html(reports: list[SiteReport]) -> str:
    report_date = reports[0].runtime.target_date.isoformat() if reports else dt.date.today().isoformat()
    success_count = sum(1 for report in reports if report.data is not None)
    failed_count = len(reports) - success_count
    site_blocks = "\n".join(render_site_html_block(report) for report in reports)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>new-api 运营日报 {esc(report_date)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #667085;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-soft: #e3f4f1;
      --warn: #9a3412;
      --warn-bg: #fff7ed;
      --shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      line-height: 1.55;
    }}
    header {{
      padding: 32px 28px 18px;
      background: #0b3b3a;
      color: #fff;
    }}
    header h1 {{
      margin: 0 0 8px;
      font-size: 28px;
      letter-spacing: 0;
    }}
    header p {{
      margin: 0;
      color: rgba(255, 255, 255, 0.78);
      font-size: 14px;
    }}
    main {{
      width: min(1440px, calc(100% - 32px));
      margin: 24px auto 48px;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      margin: 18px 0;
      padding: 20px;
    }}
    h2 {{
      margin: 0 0 16px;
      font-size: 18px;
    }}
    .site-report {{
      margin-bottom: 34px;
    }}
    .site-heading h2 {{
      font-size: 22px;
      margin-bottom: 8px;
    }}
    .site-heading p {{
      margin: 4px 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .site-error {{
      border-color: #fed7aa;
      background: var(--warn-bg);
    }}
    .error-message {{
      color: var(--warn);
      font-weight: 650;
      margin-top: 10px;
    }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .kpi-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
      background: #fbfcfd;
      min-height: 86px;
    }}
    .kpi-card span {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
    }}
    .kpi-card strong {{
      display: block;
      color: var(--accent);
      font-size: 24px;
      font-weight: 700;
      word-break: break-word;
    }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      min-width: 920px;
      background: #fff;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
      font-size: 13px;
    }}
    th {{
      background: #eef3f5;
      color: #344054;
      font-weight: 650;
    }}
    tr:last-child td {{ border-bottom: none; }}
    td.num, th.num {{ text-align: right; }}
    td.rank {{
      font-weight: 700;
      color: var(--accent);
      text-align: right;
    }}
    .empty {{
      text-align: center;
      color: var(--muted);
      padding: 22px;
    }}
    .analysis {{
      margin: 0;
      padding-left: 20px;
    }}
    .analysis li {{
      margin: 8px 0;
    }}
    .warning-list {{
      color: var(--warn);
      background: var(--warn-bg);
      border: 1px solid #fed7aa;
      border-radius: 8px;
      padding: 12px 16px 12px 32px;
    }}
    .footnote {{
      color: var(--muted);
      font-size: 13px;
    }}
    @media (max-width: 900px) {{
      header {{ padding: 26px 18px 16px; }}
      main {{ width: min(100% - 20px, 1440px); margin-top: 14px; }}
      section {{ padding: 14px; }}
      .kpi-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .kpi-card strong {{ font-size: 20px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>new-api 运营日报</h1>
    <p>{esc(report_date)}，站点数 {len(reports)}，成功 {success_count}，失败 {failed_count}</p>
  </header>
  <main>
    {site_blocks}
  </main>
</body>
</html>
"""


def text_report_filename(runtime: RuntimeOptions) -> str:
    return f"newapi-daily-{runtime.target_date.isoformat()}.txt"


def html_report_filename(runtime: RuntimeOptions) -> str:
    return f"newapi-daily-{runtime.target_date.isoformat()}.html"


def build_public_report_url(report_base_url: str, filename: str) -> str:
    base = (report_base_url or "").strip()
    if not base:
        return ""
    if not base.endswith("/"):
        base += "/"
    return base + urllib.parse.quote(filename)


def write_outputs(text_brief: str, html_text: str, runtime: RuntimeOptions) -> tuple[Path | None, Path | None]:
    runtime.output_dir.mkdir(parents=True, exist_ok=True)
    text_path: Path | None = None
    html_path: Path | None = None
    if runtime.output_format in {"text", "all"}:
        text_path = runtime.output_dir / text_report_filename(runtime)
        text_path.write_text(text_brief, encoding="utf-8")
    if runtime.output_format in {"html", "all"}:
        html_path = runtime.output_dir / html_report_filename(runtime)
        html_path.write_text(html_text, encoding="utf-8")
    return text_path, html_path


def run_check(app_config: AppConfig) -> None:
    has_error = False
    for site in app_config.sites:
        print(f"[{site.name}]")
        client = NewApiClient(site)
        try:
            data = fetch_page(client, "/api/user/", 1)
            total = data.get("total", 0)
            print("配置和管理员接口验证通过。")
            print(f"base_url: {site.base_url}")
            print(f"user_id: {site.user_id}")
            print(f"用户列表 total: {total}")
            option_payload = client.get("/api/option/", allow_failure=True)
            if isinstance(option_payload, list):
                print("系统配置接口验证通过：可自动读取金额展示配置。")
            else:
                print("系统配置接口不可访问：日报会使用 config.json 中的金额默认值。")
        except ReportError as exc:
            has_error = True
            print(f"验证失败：{exc}")
        print("")
    if has_error:
        raise ReportError("至少一个站点验证失败。")


def main() -> int:
    args = parse_args()
    try:
        app_config = load_config(Path(args.config).resolve())

        if args.check:
            run_check(app_config)
            return 0

        reports: list[SiteReport] = []
        for site in app_config.sites:
            runtime = build_runtime_options(args, site)
            client = NewApiClient(site)
            try:
                reports.append(SiteReport(site=site, runtime=runtime, data=build_report_data(client, site, runtime)))
            except ReportError as exc:
                reports.append(SiteReport(site=site, runtime=runtime, error=str(exc)))

        text_brief = render_text_brief_with_url(reports, app_config.report_base_url)
        html_text = render_html(reports)
        runtime = build_runtime_options(args, app_config.sites[0])
        text_path, html_path = write_outputs(text_brief, html_text, runtime)

        print(text_brief)
        print("生成文件：")
        if text_path:
            print(f"文字简报：{text_path}")
        if html_path:
            print(f"HTML：{html_path}")
        return 0
    except ReportError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
