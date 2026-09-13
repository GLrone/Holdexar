## 变更说明

<!-- 这个 PR 做了什么、为什么做。关联 Issue 请写 "Closes #N" -->

Closes #

## 改动范围

<!-- 勾选实际涉及的范围,帮助 reviewer 快速定位 -->

- [ ] 后端（server/）
- [ ] 前端（web/）
- [ ] 桌面壳（desktop/）
- [ ] 文档
- [ ] 构建 / 门禁 / 脚本

## 自查清单

<!-- 提交前请逐项确认 -->

- [ ] 视图层新代码用的是 `components/ui/` 的 Hl\* 组件,未直连 Element Plus（eslint error 级拦截）
- [ ] 地区 / 币种展示走 `RegionFlag` / `CurrencyFlag`,下拉选项走 `api/selectOptions.ts` 统一出口
- [ ] 挂路由与视图同批落地（无悬空引用,`npm run check:routes` 可过）
- [ ] 后端改动附了行为测试,`pytest -q` 全绿
- [ ] `npm run build` 通过（含 lint:gate + 路由完整性门禁）
- [ ] 涉及隐私数据（Cookie / 订阅 / 账单 / 真实 AppID）的日志与截图已打码

## 验证方式

<!-- 怎么验证的:pytest 结果、E2E 脚本、手动步骤或截图。真网验证请说明是否消耗激活配额等消耗性资源 -->
