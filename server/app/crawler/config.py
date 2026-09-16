"""爬取内核配置。

含端点、区服清单与限速参数（默认 60 请求/5 分钟）。
"""
from __future__ import annotations

# Steam 端点
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
REVIEWS_URL = (
    "https://store.steampowered.com/appreviews/{appid}?json=1&language=all&purchase_type=all"
)

# 数据新鲜度阈值（小时）
STALE_HOURS = 12

# 区域列表 (code, display_name, currency)
# 覆盖 Steam 全货币区（欧元区以 pt 为基线 cc，返回 EUR 统一定价）
CC_LIST: list[tuple[str, str, str]] = [
    ("cn", "中国（CNY）", "CNY"),
    ("ru", "俄罗斯（RUB）", "RUB"),
    ("kz", "哈萨克斯坦（KZT）", "KZT"),
    ("ua", "乌克兰（UAH）", "UAH"),
    ("pk", "南亚（USD）", "USD"),
    ("tr", "土耳其（USD）", "USD"),
    ("ar", "阿根廷（USD）", "USD"),
    ("az", "阿塞拜疆（USD）", "USD"),
    ("vn", "越南（VND）", "VND"),
    ("id", "印尼（IDR）", "IDR"),
    ("in", "印度（INR）", "INR"),
    ("br", "巴西（BRL）", "BRL"),
    ("cl", "智利（CLP）", "CLP"),
    ("jp", "日本（JPY）", "JPY"),
    ("hk", "中国香港（HKD）", "HKD"),
    ("ph", "菲律宾（PHP）", "PHP"),
    ("tw", "中国台湾（TWD）", "TWD"),
    # ↑ 中国香港、中国台湾为固有命名，不得改为"香港特别行政区"等变体
    # ── 扩展货币区 ──
    ("kw", "科威特（KWD）", "KWD"),
    ("sa", "沙特阿拉伯（SAR）", "SAR"),
    ("za", "南非（ZAR）", "ZAR"),
    ("qa", "卡塔尔（QAR）", "QAR"),
    ("my", "马来西亚（MYR）", "MYR"),
    ("th", "泰国（THB）", "THB"),
    ("pe", "秘鲁（PEN）", "PEN"),
    ("mx", "墨西哥（MXN）", "MXN"),
    ("sg", "新加坡（SGD）", "SGD"),
    ("ae", "阿联酋（AED）", "AED"),
    ("uy", "乌拉圭（UYU）", "UYU"),
    ("co", "哥伦比亚（COP）", "COP"),
    ("kr", "韩国（KRW）", "KRW"),
    ("nz", "新西兰（NZD）", "NZD"),
    ("pl", "波兰（PLN）", "PLN"),
    ("cr", "哥斯达黎加（CRC）", "CRC"),
    ("ca", "加拿大（CAD）", "CAD"),
    ("us", "美国（USD）", "USD"),
    ("au", "澳大利亚（AUD）", "AUD"),
    ("pt", "欧元区（EUR）", "EUR"),
    ("gb", "英国（GBP）", "GBP"),
    ("no", "挪威（NOK）", "NOK"),
    ("il", "以色列（ILS）", "ILS"),
    ("ch", "瑞士（CHF）", "CHF"),
]

CIS_REGIONS = ["kz", "ua"]

DEFAULT_WORKER_COUNT = 30
# 直连 Steam 在部分网络下 TLS 握手可能超过 10s（实测 9.7s），12s 超时会误杀；放宽到 20s
HTTP_TIMEOUT = 20

# ── 主轮 worker 数：按可用出口 IP 节点数开启（crawl/service._resolve_worker_count）──
# 口径 = proxies.pool_stats() 的 available：手动池可用 + Clash 在跑订阅的存活
# 出口 IP 按「一个出口 IP 算一个」去重（多入口同落地只算一个）。
# 规则：一个可用出口 IP 开一个 worker 参与并发，上限 WORKERS_MAX；无可用出口
# 数据（未配代理 / 内核没跑，直连形态）回退 DEFAULT_WORKER_COUNT。
WORKERS_MAX = 60

# 请求频率主闸在 crawler/rate_limit.py（200 发/5 分钟，与捆绑包刷新共享窗口预算）。

# 版本后缀关键词字典（key 统一小写匹配）
EDITION_DICT = {
    "deluxe edition": "豪华版",
    "ultimate edition": "终极版",
    "gold edition": "黄金版",
    "special edition": "特别版",
    "premium edition": "高级版",
    "game of the year edition": "年度版",
    "goty edition": "年度版",
    "complete edition": "完整版",
    "definitive edition": "终极典藏版",
    "enhanced edition": "增强版",
    "anniversary edition": "周年版",
    "collector's edition": "收藏版",
    "legendary edition": "传奇版",
    # 版本显示修复增量。刻意不加 "standard edition"（会把标准版
    # named-sub 挤出候选池）与 "gourmet edition"（Gourmet 是 bundle-as-sub，
    # 版本后缀与捆绑包识别是两套判据，加进来会自相矛盾）
    "year one edition": "首年版",
    "royal edition": "皇家版",
    "prime edition": "尊享版",
}
