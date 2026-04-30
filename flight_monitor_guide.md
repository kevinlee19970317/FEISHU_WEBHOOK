# Flight Monitor 使用与排障手册

## 1. 文件关系（先理解结构）
- `.github/workflows/flight_alert.yml`：定时触发器（什么时候跑）
- `requirements.txt`：依赖清单（运行前装什么）
- `monitor.py`：核心逻辑（怎么跑）
- `config.yaml`：策略参数（按什么规则跑）

调用链：
`flight_alert.yml` -> `pip install -r requirements.txt` -> `python monitor.py` -> 读取 `config.yaml` -> 触发提醒。

---

## 2. 邮箱接收地址配置
发到哪个邮箱由 `ALERT_EMAIL_TO` 决定。

在 GitHub：
`Settings -> Secrets and variables -> Actions -> New repository secret`

必填建议：
- `ALERT_EMAIL_TO`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASS`
- `SMTP_FROM`

---

## 3. RapidAPI 接入
### 3.1 安全提醒
如果 API key 曾在聊天/截图中明文出现，请先 Rotate（重置）后再使用。

### 3.2 推荐 Secrets
- `RAPIDAPI_KEY`
- `RAPIDAPI_HOST`（如 `booking-com15.p.rapidapi.com`）
- `RAPIDAPI_FLIGHTS_URL`（建议 `getMinPrice` 或搜索接口）
- `RAPIDAPI_CURRENCY`（如 `CNY`）
- `RAPIDAPI_EXTRA_PARAMS`（JSON 字符串）

### 3.3 getMinPrice 是否可用
可用，且通常比 `getFlightDetails` 更适合告警场景。
常用参数格式：
- `fromId=SHA.AIRPORT`
- `toId=FUK.AIRPORT`
- `cabinClass=ECONOMY`

---

## 4. 不接真实 API 的测试模式
如果你还没接好真实 API，可先联调通知链路：
- `MOCK_PRICE_MODE=true`
- `MOCK_BASELINE_PRICE=2000`
- `MOCK_PRICE=1200`
- `MOCK_IS_DIRECT=true`

测完记得改回 `MOCK_PRICE_MODE=false`。

---

## 5. 如何确认“真的触发了监控”
看 `Run monitor` 日志末尾两个指标：
- `fetched_records=...`：抓取到的票价条数
- `alerts_sent=...`：发出的提醒条数

判读：
- `fetched_records > 0`：API 数据接入成功
- `alerts_sent > 0`：触发条件成立并已提醒

若想强制验证通知链路，可临时设置：
- `FORCE_ALERT=true`

---

## 6. 常见问题排查
### 6.1 `fetched_records=0` / `alerts_sent=0`
通常是 API 没返回可解析票价，不一定是程序错误。
优先检查：
1. `fromId/toId` 是否为 `XXX.AIRPORT`
2. endpoint 是否返回 `minPrice` 或 `flights`
3. 是否缺少接口必填参数（用 `RAPIDAPI_EXTRA_PARAMS`）

建议临时加：
- `DEBUG_MONITOR=true`

日志会输出请求参数和返回结构。

### 6.2 `429 Too Many Requests`
表示被限流。建议：
- 降低运行频率
- 配置 `REQUEST_INTERVAL_SECONDS=1` 或 `2`
- 先减少航线数量验证


### 6.3 持续 429（每条路线都限流）
如果日志里连续出现 `rapidapi rate limited (429)`：
- 增加 `REQUEST_INTERVAL_SECONDS=1~3`
- 设置 `RAPIDAPI_RETRIES=2`（默认已启用退避重试）
- 临时只保留 1~2 条路线验证

脚本现在会在多次 429 后自动停止本轮剩余路线请求，避免把额度继续打满。

---

## 7. 语法检查命令在哪里执行
命令：
```bash
python -m py_compile monitor.py
```
可在：
1. 本地终端执行
2. GitHub Actions 的 step 里执行（推荐自动化）

---

## 8. Skyscanner版字段映射模板（可直接套用）

> 目标：把 Skyscanner 返回结构，映射成你 `monitor.py` 需要的统一格式：
> 
> `[{"depart_date": "YYYY-MM-DD", "price": 1234.0, "is_direct": true, "url": "..."}]`

```python
def map_skyscanner_result_to_prices(payload: dict, fallback_depart_date: str, deep_link: str = ""):
    """
    适配 monitor.py 统一结构。

    兼容两种常见形态：
    1) data.itineraries[] + pricingOptions[]
    2) data.flights[]（如果你接的是另一类返回）
    """
    result = []
    data = payload.get("data") or {}

    # 形态1：itineraries + pricingOptions
    itineraries = data.get("itineraries") or []
    for it in itineraries:
        pricing_options = it.get("pricingOptions") or []
        if not pricing_options:
            continue

        best = pricing_options[0]
        for opt in pricing_options[1:]:
            if (opt.get("price", {}).get("amount") or float("inf")) < (best.get("price", {}).get("amount") or float("inf")):
                best = opt

        amount = best.get("price", {}).get("amount")
        if amount is None:
            continue

        legs = it.get("legs") or []
        # 常见规则：所有 leg 都无经停才算直飞
        is_direct = True
        depart_date = fallback_depart_date
        for leg in legs:
            if leg.get("stopCount", 0) > 0:
                is_direct = False
            if leg.get("departure"):
                depart_date = str(leg["departure"])[:10]

        result.append({
            "depart_date": depart_date,
            "price": float(amount),
            "is_direct": is_direct,
            "url": best.get("url") or deep_link,
        })

    # 形态2：flights（兜底）
    flights = data.get("flights") or []
    for f in flights:
        price_obj = f.get("price") or {}
        amount = price_obj.get("amount") or price_obj.get("units") or price_obj.get("total")
        if amount is None:
            continue

        stop_count = f.get("stopCount")
        is_direct = (stop_count == 0) if stop_count is not None else bool(f.get("isDirect", False))
        depart_date = str(f.get("departureDate") or fallback_depart_date)[:10]

        result.append({
            "depart_date": depart_date,
            "price": float(amount),
            "is_direct": is_direct,
            "url": f.get("deepLink") or deep_link,
        })

    return result
```

### 接入位置建议
在你现有 `fetch_prices_via_rapidapi()` 之后，新增一个 `fetch_prices_via_skyscanner()`，最后让
`fetch_prices_for_route()` 走：

1. `MOCK_PRICE_MODE`（测试）
2. `Skyscanner`（主源）
3. `RapidAPI`（备源）
4. 空列表

这样你能保持现在整套告警逻辑不变，只替换“数据源入口”。
