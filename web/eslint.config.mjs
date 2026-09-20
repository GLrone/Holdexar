/* ════════════════════════════════════════════════════════════════════
   Holdexar 前端 L4 强制层 —— 框架束口 lint
   运行：npm run lint        全量（含存量迁移 warn）
         npm run lint:gate   门禁（--quiet 只报 error，新增违规即拦截）

   标准框架优先红线（error 级；豁免文件降回迁移期 warn）：
   · 视图层禁止 import element-plus / @element-plus/icons-vue → Hl* / HlIcon
   · 模板禁止书写任何 el-* 组件 → 一律使用 components/ui 的 Hl* 组件
     （存量由 LEGACY_FILES 豁免）

   收口规则（error，立即生效；存量豁免见下方 LEGACY_FILES）：
   · 原生 <select> / el-select / el-drawer / el-dialog
     → HlSelect / HlDrawer / HlDialog（vue/no-restricted-html-elements）
   · 视图层禁止直连 flagUrl / currencyFlagUrl
     → api/selectOptions.ts 统一出口 或 <RegionFlag>/<CurrencyFlag>
     （核心 no-restricted-syntax 匹配 import 说明符；组件/出口本体豁免）
   存量豁免：仅 LEGACY_FILES 列出的文件（与例外登记表一一对应），
   迁移期 warn 规则对豁免文件照常生效；新文件加入豁免清单视同违反收口规则。

   双语红线（error，全量生效）：
   · .vue / .ts 禁止硬编码用户可见中文 → useI18n() 的 t()，词典见 src/locales/
     词典目录整目录豁免（那是中文的正确落点）。存量清单 I18N_LEGACY_FILES
     为空——**任何文件新增中文文案都是构建失败**，无降级通道。
     「数据不是文案」的少数例外走下方的中文数据值白名单（逐字放行，非整文件）。
   ════════════════════════════════════════════════════════════════════ */
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

import chartOptionContract from './eslint-rules/chart-option-contract.mjs'
import noHardcodedCjk from './eslint-rules/no-hardcoded-cjk.mjs'

/** 收口规则存量豁免清单 —— ⚠️ 与「例外登记表」必须一一对应。 */
const LEGACY_FILES = [
  // ── HlSelect 能力缺口期遗留（multiple / filterable / 紧凑尺寸补齐后回归）──
  'src/views/proxies/Index.vue', // ElMessageBox JS confirm ×4（EP 直连存量）
  'src/views/rates/Index.vue', // el-button ×1 + el-table ×5（EP 直连存量，重构时迁 Hl*）
  // ── 文本链接当按键存量（迁 HlButton variant="text" 后移除）──
  'src/views/dashboard/Index.vue', // <a @click> 跳转 ×1 + el-button text ×3
  // ── 自绘下拉/弹层（不进 HlSelect 迁移清单）──
  'src/components/business/HlNavbar.vue', // 自制地区/排序下拉
  'src/views/family/Index.vue', // 自制带搜索地区弹层
  // ── 标准框架优先红线豁免（EP 直连存量，逐页迁 Hl* 后移除）──
  'src/views/settings/Index.vue', // el-input ×3
  // ── flagUrl 直连存量（统一出口就绪后可迁，非违规）──
  'src/views/alerts/Index.vue',
  'src/views/game-detail/Index.vue',
  'src/components/business/PriceTrendDrawer.vue',
  'src/views/crawl/Index.vue', // el-* 存量 + flagUrl 直连（页面重构时一并迁 Hl*）
  'src/components/business/HlFilterPanel.vue',
  'src/components/business/HlGameCard.vue',
]

/**
 * 双语红线存量豁免清单 —— 尚未迁移的用户可见中文所在文件。
 *
 * **只减不增**：新文件加进本清单视同违反红线；清单为空即表示红线全面生效。
 * 词典目录 src/locales/** 不在此列——那是中文的正确落点，整目录豁免。
 *
 * ⚠️ 与 LEGACY_FILES 的区别：那份是「组件体系」的存量豁免（EP 直连 / el-*），
 * 这份是「文案」的。两者独立收敛，不要合并——一个文件可以迁完文案却仍用 el-*。
 *
 * ⚠️ `data-section` 是**展示值不是结构属性**，别加进规则的结构白名单：
 * HlSectionRail 把它的值渲染进悬停气泡与 aria-label，用户看得见。但它同时是
 * ProductTour 的 querySelector 目标。
 *
 * `data-section` 统一写法：**属性值直接写词条 key**（`data-section="alerts.section.rules"`），
 * HlSectionRail 读到时 `t(label)` 再显示，ProductTour 的 target 写同一串。
 * 不要写成 `:data-section="t(...)"`：那样属性值随语言变，选择器切语言即断，
 * 且属性里带中文会让文件永远出不了本表。
 * 代价是 HlSectionRail 需要把 label 当 key 解释（非 key 的原样显示以兼容）。
 */
const I18N_LEGACY_FILES = [
  // 存量豁免清单：列进这里的文件，双语红线降为 warn。**只减不增**。
  // 当前为空——双语红线对全部文件都是 error 级，任何新增中文文案即构建失败。
]

/** flagUrl 直连禁令的许可文件（组件/出口本体，非"豁免"） */
const FLAG_IMPL_FILES = [
  'src/api/selectOptions.ts', // 统一选项出口本体
  'src/api/currencies.ts', // currencyFlagUrl 实现层
  'src/components/RegionFlag.vue', // 默认带旗组件
  'src/components/CurrencyFlag.vue',
]

const FLAG_MSG =
  '收口规则：视图层禁止直连 flagUrl/currencyFlagUrl，请改用 api/selectOptions.ts 统一出口或 <RegionFlag>/<CurrencyFlag> 组件（旗由出口统一挂载，忘旗在结构上不可能）。'

/* ECharts 图表红线（option 契约）由 eslint-rules/chart-option-contract.mjs 的
   自写规则承担：按「含 series 的 option 对象」定位，逐项做实质断言
   （animation / axisPointer / tooltip 主题 / 无颜色字面量），见该文件头部说明。 */

/** Element Plus 直连 import 禁令 group（error 非豁免 / warn 豁免文件共用） */
const EP_IMPORT_GROUP = [
  'element-plus',
  'element-plus/*',
  '@element-plus/icons-vue',
  '@element-plus/icons-vue/*',
]

const EL_WILDCARD_SELECTOR = 'VElement[name=/^el-/]'
const EP_IMPORT_ERR_MSG =
  '收口红线（标准框架优先）：视图层禁止直连 Element Plus / @element-plus/icons-vue——组件一律从 components/ui 取 Hl*，图标走 HlIcon（ui/icons.ts 认可清单）。存量文件在例外登记表豁免；新文件出现即构建失败。'
const EP_IMPORT_WARN_MSG =
  '框架束口：视图层禁止直连 Element Plus，请使用 components/ui 的 Hl* 组件。'
const EL_WILDCARD_ERR_MSG =
  '收口红线（标准框架优先）：视图层禁止使用 el-* 组件，一律改用 components/ui 的 Hl* 等价组件（映射见设计系统规划文档第五节）。存量文件在例外登记表豁免；新文件出现即构建失败。'
const EL_WILDCARD_WARN_MSG =
  '框架束口：模板禁止使用 el-* 组件，请改用 components/ui 的 Hl* 组件。'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'public/**', 'src/**/*.d.ts'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        extraFileExtensions: ['.vue'],
        sourceType: 'module',
      },
    },
  },
  {
    rules: {
      /* ── 项目约定（消除存量噪音，聚焦束口信号）── */
      // TS 项目关闭 no-undef（类型/导入存在性由 tsc 负责，规则无法识别 TS 类型）
      'no-undef': 'off',
      // 路由视图按 views/<module>/Index.vue 约定命名
      'vue/multi-word-component-names': 'off',
      // 存量问题降级为警告，不阻塞束口信号
      '@typescript-eslint/no-unused-vars': 'warn',
      'vue/no-unused-vars': 'warn',

      /* ── 框架束口（el-* 通配 / EP import）见下方「收口规则」块；
         豁免文件由 LEGACY_FILES 块降回 warn，此处不再重复配置 ── */
    },
  },

  /* ═══ 收口规则（error，立即生效）═══ */
  {
    files: ['src/**/*.{vue,ts}'],
    plugins: {
      holdexar: {
        rules: {
          'chart-option-contract': chartOptionContract,
          'no-hardcoded-cjk': noHardcodedCjk,
        },
      },
    },
    rules: {
      // ECharts 图表红线：option 契约（animation / axisPointer / tooltip 主题 / 无颜色字面量）
      'holdexar/chart-option-contract': 'error',
      // 双语红线：用户可见文案禁止硬编码中文（存量见 I18N_LEGACY_FILES）
      'holdexar/no-hardcoded-cjk': 'error',
      // 组件禁令：原生 select / el-select / el-drawer / el-dialog（独立规则键，
      // 不与上面 warn 级块冲突；数组型规则的选择器平铺传递）
      'vue/no-restricted-html-elements': [
        'error',
        {
          element: 'select',
          message:
            '收口规则：禁止原生 <select>，请使用 HlSelect（原生下拉无旗支持且样式脱离双主题体系）。存量例外须在例外登记表登记并加入豁免清单。',
        },
        {
          element: 'el-select',
          message:
            '收口规则：el-select 全屏遮罩层拦截底层点击且无旗支持，请改用 HlSelect（多选例外见登记表）。',
        },
        {
          element: 'el-drawer',
          message:
            '收口规则：el-drawer 即使 :modal="false" 也渲染全屏 wrapper 拦截底层点击，请改用 HlDrawer。',
        },
        {
          element: 'el-dialog',
          message: '收口规则：请使用 HlDialog。',
        },
      ],
      // 标准框架优先红线 + 文本链接当按键禁令（S1/S2/S3）——模板 AST 选择器，
      // 必须走插件版规则（核心版 no-restricted-syntax 只看脚本 AST）
      'vue/no-restricted-syntax': [
        'error',
        // el-* 通配：任何 Element Plus 组件都不得出现在视图层（标准框架优先）
        {
          selector: EL_WILDCARD_SELECTOR,
          message: EL_WILDCARD_ERR_MSG,
        },
        // S1: href="#" 假链接（占位按键）
        {
          selector: 'VElement[name="a"] > VStartTag > VAttribute[key.name="href"][value.value="#"]',
          message:
            '收口规则：禁止 href="#" 假链接当按键——按键一律 HlButton（variant="text" 即文本按键形态）；站内导航用 <router-link :to>。',
        },
        // S2: 无 href 却带 @click 的动作链接（JS 跳转假链接）
        // 静态 href 与动态绑定 :href 都是真实链接语义，一并豁免
        {
          selector:
            'VElement[name="a"]:not(:has(VStartTag > VAttribute[key.name="href"])):not(:has(VStartTag > VAttribute[directive=true][key.argument.name="href"])):has(VAttribute[directive=true][key.name.name="on"])',
          message:
            '收口规则：禁止无 href 的 <a> 当按键（@click 跳转属按键语义）——用 HlButton；站内导航用 <router-link :to>；真实外链 <a :href="url"> 除外。',
        },
        // S3: el-button 的 text / link 变体（文本按键形态必须走 HlButton）
        {
          selector: 'VElement[name="el-button"] > VStartTag > VAttribute[key.name="text"]',
          message:
            '收口规则：el-button text 变体即文本链接按键，请改用 HlButton（variant="text"）。',
        },
        {
          selector: 'VElement[name="el-button"] > VStartTag > VAttribute[key.name="link"]',
          message:
            '收口规则：el-button link 变体即文本链接按键，请改用 HlButton（variant="text"）。',
        },
      ],
      // flagUrl / currencyFlagUrl 直连禁令（脚本 AST：import 说明符）
      'no-restricted-syntax': [
        'error',
        {
          selector:
            "ImportDeclaration[source.value=/api\\/(regions|currencies)/] > ImportSpecifier[imported.name=/^(flagUrl|currencyFlagUrl)$/]",
          message: FLAG_MSG,
        },
      ],
      // 标准框架优先红线：EP 直连 import（error；豁免文件由 LEGACY 块降回 warn）
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: EP_IMPORT_GROUP,
              message: EP_IMPORT_ERR_MSG,
            },
          ],
        },
      ],
    },
  },

  /* ═══ 许可清单：flagUrl 实现层（出口/组件本体）═══ */
  {
    files: FLAG_IMPL_FILES,
    rules: {
      'no-restricted-syntax': 'off',
    },
  },

  /* ═══ 存量豁免：LEGACY_FILES ═══
     不能整键 'off'：vue/no-restricted-syntax 同键承载「el-* warn」
     与「收口 error」，关闭会连带吞掉 warn。因此显式重设该键为仅 el-* 通配的
     warn 配置（flat config 后块覆盖前块，等效关闭收口 error、保留可见性）；
     no-restricted-syntax（flagUrl 禁令）无豁免语义，直接 off；
     no-restricted-imports 同理重设为 warn（EP 直连存量）。 */
  {
    files: LEGACY_FILES,
    rules: {
      'vue/no-restricted-html-elements': 'off',
      'no-restricted-syntax': 'off',
      'vue/no-restricted-syntax': [
        'warn',
        {
          selector: EL_WILDCARD_SELECTOR,
          message: EL_WILDCARD_WARN_MSG,
        },
      ],
      'no-restricted-imports': [
        'warn',
        {
          patterns: [
            {
              group: EP_IMPORT_GROUP,
              message: EP_IMPORT_WARN_MSG,
            },
          ],
        },
      ],
    },
  },

  {
    // 豁免：兼容层注册（main.ts）；框架层自身目前不依赖 EP
    files: ['src/main.ts'],
    rules: {
      'no-restricted-imports': 'off',
    },
  },

  /* ═══ 双语红线：词典本体与存量豁免 ══ */
  {
    // 词典目录是中文的正确落点，不是违规。整目录关掉——
    // 若逐条 t() 化，词典自身就没法写中文了。
    files: ['src/locales/**'],
    rules: {
      'holdexar/no-hardcoded-cjk': 'off',
    },
  },
  // 存量未迁移文案：降为 warn（可见、不拦构建）。清单只减不增，当前为空
  // ——双语红线对全部文件都是 error 级。
  // 空清单不生成该块（而非传 `files: []`）：flat config 的 `files: []` 是报错
  // 而非空操作。保留条件分支，清单再被填上时该块自动生效。
  ...(I18N_LEGACY_FILES.length
    ? [
        {
          files: I18N_LEGACY_FILES,
          rules: { 'holdexar/no-hardcoded-cjk': 'warn' },
        },
      ]
    : []),

  /* ═══ 中文数据值白名单（是数据、不是文案，译不得）═══
     `views/bills/LedgerTab.vue` 的 tx_type 筛选值逐字列在这里——这些串必须
     保持中文：`server/app/domains/bills/parser.py` 用 `TX_TYPE_NORM` 把英文
     类型名规范化成中文再落库，查询侧是 `BillTxType.tx_type == tx_type` 的
     **等值匹配**，前端改成英文筛选会静默返回空列表。

     放行的是**这几个串**，不是这个文件：文件仍在 error 级，里面新写的中文
     文案照样报错。不整文件豁免的原因：文件进 I18N_LEGACY_FILES 会让真文案
     混进「已知存量」的噪音里，而那份清单的全部价值就是它没有噪音。
     规则本体与 `allow` 的完整说明见 eslint-rules/no-hardcoded-cjk.mjs。 */
  {
    files: ['src/views/bills/LedgerTab.vue'],
    rules: {
      'holdexar/no-hardcoded-cjk': [
        'error',
        { allow: ['购买', '礼物购买', '游戏内购买', '退款'] },
      ],
    },
  },
)
