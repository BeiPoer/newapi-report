# new-api 运营日报脚本

这个目录是独立报表工具，不直连数据库，只通过 new-api 的 HTTP 管理接口读取数据。脚本只生成适合微信粘贴的纯文本简报和 HTML 单页，不生成 CSV。

## 首次配置

1. 在 new-api 后台给具备管理员权限的用户生成系统访问令牌。
2. 复制配置模板：

```powershell
Copy-Item .\config.example.json .\config.json
```

3. 编辑 `config.json`，在 `sites` 里配置一个或多个站点：

- `name`：站点名称，会显示在微信简报和 HTML 报表中。
- `base_url`：该 new-api 站点地址，例如 `http://127.0.0.1:3000`。
- `access_token`：该站点后台生成的系统访问令牌。
- `user_id`：令牌所属用户 ID。new-api 管理接口要求请求头 `New-Api-User` 与令牌用户一致。
- `display_currency`：建议保持 `auto`。如果令牌不是 Root 权限，脚本无法读取系统配置，会使用本地配置的默认金额口径。
- `report_base_url`：HTML 报表公开访问目录。Nginx 如果把域名路径转发到 `reports` 目录，就填对应 URL，例如 `https://report.example.com/newapi/`。
- `request_delay_seconds`：每次接口请求之间的最小间隔，默认 `0.5` 秒，用于避免分页读取消费日志时触发限流。
- `api_rate_limit_max_requests` / `api_rate_limit_window_seconds`：客户端滑动窗口限速，默认 `180` 秒内最多 `150` 次请求。new-api 默认全局 API 限流通常是 `180` 秒 `180` 次，脚本默认保留 30 次余量给其他后台请求。
- `max_retries`：遇到 HTTP 429/5xx 等临时错误时的最大重试次数，默认 `6`。
- `retry_base_seconds` / `retry_max_seconds`：指数退避重试等待时间，遇到服务端 `Retry-After` 会优先使用服务端建议。

只统计一个站点时，`sites` 保留一个对象即可；多个站点就继续追加对象。

示例：

```json
{
  "timezone": "Asia/Shanghai",
  "quota_per_unit": 500000,
  "usd_exchange_rate": 7.3,
  "display_currency": "auto",
  "report_base_url": "https://report.example.com/newapi/",
  "request_delay_seconds": 0.5,
  "api_rate_limit_max_requests": 150,
  "api_rate_limit_window_seconds": 180,
  "max_retries": 6,
  "sites": [
    {
      "name": "主站",
      "base_url": "http://127.0.0.1:3000",
      "access_token": "填写主站系统访问令牌",
      "user_id": "1"
    },
    {
      "name": "备用站",
      "base_url": "http://127.0.0.1:3001",
      "access_token": "填写备用站系统访问令牌",
      "user_id": "1"
    }
  ]
}
```

兼容旧的单站配置：如果没有 `sites` 数组，脚本仍会读取顶层 `base_url`、`access_token`、`user_id`，站点名默认显示为 `站点1`。

## 运行

本机没有全局 Python 时，先创建 uv 虚拟环境：

```powershell
uv venv
```

验证配置和权限：

```powershell
uv run .\newapi_daily_report.py --check
```

生成今天日报：

```powershell
uv run .\newapi_daily_report.py
```

生成指定日期日报：

```powershell
uv run .\newapi_daily_report.py --date 2026-06-13
```

## 输出

默认输出到 `reports` 目录：

- `newapi-daily-YYYY-MM-DD.txt`：微信纯文本简报，多个站点会按站点名分段。
- `newapi-daily-YYYY-MM-DD.html`：HTML 单页，多个站点会按站点名分段展示核心指标、充值用户明细、消耗排行 Top 10 和运营分析。

如果配置了 `report_base_url`，微信纯文本简报顶部会自动带上 HTML 链接，例如：

```text
HTML详情：https://report.example.com/newapi/newapi-daily-2026-06-13.html
```

这个 URL 的文件名必须能通过你的 Nginx 直接访问到 `reports/newapi-daily-YYYY-MM-DD.html`。

## 数据口径

- 今日充值：成功订单 `status == success`，按 `complete_time` 归属统计日期。
- 今日消耗：消费日志 `type=2`，消耗金额由 `quota / QuotaPerUnit` 折算。
- 消耗排行 Top 10：按用户聚合今日消费 `quota` 后排序，同 quota 时按请求次数降序，再按用户 ID 升序。
- 今日新增注册：用户 `created_at` 落在统计日期内。
- 历史总充值：当前接口可拉取到的全部成功充值订单按用户聚合。

如果消费日志关闭，今日消耗人数和消耗排行可能偏低或为空；脚本会在异常提示中说明。

脚本默认会按 `180` 秒最多 `150` 次请求主动限速，适配 new-api 默认全局 API 限流。请求量大时生成时间会变长，这是为了避免触发服务端 HTTP 429。

如果某个站点消费日志很多，分页过程中仍触发 HTTP 429，脚本会自动等待并重试。重试仍失败时，不会让整个站点日报失败；脚本会使用已拉取到的消费日志计算排行，并在简报/HTML 的提示中说明排行可能不完整。
