"""Agent Runtime 域：可持久化、可恢复、可取消的运行骨架。

概念与既有领域语言对齐：Run 对标 crawl 的 Job（一次有账本的执行），
Step 对标 Attempt（run 内一次执行单元）。P1 阶段只含 Runtime 骨架
（三表账本 + 状态机 + 取消原语 + fake runner），Provider / Tool /
Approval / MCP 按迁移方案逐阶段接入。
"""
