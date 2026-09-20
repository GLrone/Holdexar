"""汇率 Provider 适配器包：每个模块只负责一个外部源的 HTTP/协议细节。

业务层（rates/history.py、rates/service.py）不感知 Provider 的专属概念
（quotes 键名 / access_key / currencies 参数形态等），只消费统一的
「rate_date → {currency: rate_to_cny}」语义。
"""
