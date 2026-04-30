# 4 个文件之间的关系（机票提醒项目）

## 总览
这 4 个文件可以理解成一条流水线：

1. `flight_alert.yml`：负责“什么时候运行”。
2. `requirements.txt`：负责“运行前装什么依赖”。
3. `monitor.py`：负责“运行时做什么逻辑”。
4. `config.yaml`：负责“逻辑用什么参数”。

## 逐个说明

### 1) `.github/workflows/flight_alert.yml`（调度器）
- 由 GitHub Actions 定时触发（例如每 6 小时）。
- 在云端执行：checkout 代码 → 安装 Python → `pip install -r requirements.txt` → `python monitor.py`。
- 通过 `env` 把 `FEISHU_WEBHOOK` 注入给程序。

### 2) `requirements.txt`（依赖清单）
- 给 workflow 告诉要安装哪些 Python 包。
- 没有它，`monitor.py` 里 `import requests` / `import yaml` 等可能报错。

### 3) `config.yaml`（策略配置）
- 不是代码，而是参数：比如监控航线、未来 90 天、直飞、阈值 0.7。
- `monitor.py` 每次运行先读取这个文件，再按参数执行。

### 4) `monitor.py`（核心执行程序）
- 读取 `config.yaml`。
- 获取/计算价格并判断是否满足触发条件。
- 如果满足条件，就通过 `FEISHU_WEBHOOK` 发飞书消息。

## 调用关系（从上到下）
`flight_alert.yml` → `pip install -r requirements.txt` → `python monitor.py` → 读取 `config.yaml` → 触发飞书提醒。

## 一句话记忆
- `yml` 决定“何时跑”，
- `txt` 决定“用什么跑”，
- `py` 决定“怎么跑”，
- `yaml` 决定“按什么规则跑”。
