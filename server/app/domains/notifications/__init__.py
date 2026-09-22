"""通知域：价格事件 → 策略 → 候选 → 聚合摘要 → 投递。

事实（`price_events`）与服务行为（通知）分离：事件没有渠道状态，候选才有。
触发点只有 Cycle 的 finalizing，没有通知 scheduler。
"""
