/* ECharts option 契约（图表红线）—— 本地 ESLint 规则，error 级。
 *
 * 定位：按「option 对象」（含 series 属性的对象字面量）识别图表配置，
 * 逐项做实质断言——必须校验取值本身，不能只校验属性是否存在；取值是变量
 * 引用时同样报错（无法静态校验在这条红线上等于没写）。
 *
 * 四条断言：
 *   1. animation        必须显式声明。入场一律 false —— echarts 入场动画由 rAF 驱动，
 *                       后台标签页 rAF 被限流会永停首帧（柱高 0 = 空图）。数据过渡另用
 *                       一个只在用户交互时打开的开关（rates / bills 的 animateChart）。
 *   2. axisPointer      有坐标轴就必须声明。tooltip 是浮层，axisPointer 画在轴上，
 *                       是 tooltip 主题化唯一覆盖不到的悬停元素；不配就是 echarts 默认
 *                       的灰线 + 灰底白字标签框。
 *   3. tooltip 主题     必须对象字面量 + 显式 backgroundColor。
 *   4. 无颜色字面量     option 子树内禁止 #hex / rgb() / rgba() / hsl()。色值一律走
 *                       api/chartTheme.ts 出口——写死的字面量不跟随主题切换。
 *                       用 token 而非 AST 递归扫描：token 覆盖整段区间，且能看见
 *                       模板字面量里内嵌的颜色（`<span style="color:#2ed573">`）。
 */
const COLOR_RE =
  /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b|\b(?:rgba?|hsla?)\s*\(/

/** 对象字面量里名为 name 的属性（支持带引号的键；shorthand 也算） */
function findProp(objExpr, name) {
  for (const p of objExpr.properties) {
    if (p.type !== 'Property' || p.computed) continue
    const k = p.key
    if (k.type === 'Identifier' && k.name === name) return p
    if (k.type === 'Literal' && String(k.value) === name) return p
  }
  return null
}

const AXIS_KEYS = ['xAxis', 'yAxis', 'radiusAxis', 'angleAxis', 'singleAxis']

export default {
  meta: {
    type: 'problem',
    docs: {
      description: 'ECharts option 主题化与动画契约（图表红线）',
    },
    schema: [],
    messages: {
      needAnimation:
        '图表红线：option 含 series 时必须显式声明 animation。入场一律 animation:false——echarts 入场动画由 rAF 驱动，后台标签页里 rAF 被限流会永停首帧（柱高 0，看起来就是空图）；数据过渡另用一个只在用户交互时打开的开关（见 rates / bills 的 animateChart）。',
      needAxisPointer:
        '图表红线：option 有坐标轴时必须显式声明 axisPointer（走 api/chartTheme.ts 的 axisPointerStyle 出口）。悬停时贴在轴上的指示线与轴标签框不配就是 echarts 默认的灰线 + 灰底白字——tooltip 主题化覆盖不到它。',
      needTooltipTheme:
        '图表红线（悬停提示窗主题同步）：tooltip 必须写成对象字面量并显式声明 backgroundColor/borderColor/textStyle（走 api/chartTheme.ts 的 useTipPalette 出口）。取值来自变量引用的写法无法静态校验，在这条红线上等于没写。',
      noColorLiteral:
        '图表红线：option 内禁止颜色字面量（#hex / rgb() / rgba() / hsl()），一律从 api/chartTheme.ts 的 useChartPalette() / useTipPalette() 取 CSS token——写死的色值不跟随主题切换。',
    },
  },
  create(context) {
    const sourceCode = context.sourceCode

    return {
      ObjectExpression(node) {
        const series = findProp(node, 'series')
        if (!series) return // 不是 echarts option 对象

        if (!findProp(node, 'animation')) {
          context.report({ node: series.key, messageId: 'needAnimation' })
        }

        const hasAxis = AXIS_KEYS.some((k) => findProp(node, k))
        if (hasAxis && !findProp(node, 'axisPointer')) {
          context.report({ node: series.key, messageId: 'needAxisPointer' })
        }

        const tooltip = findProp(node, 'tooltip')
        if (tooltip) {
          const v = tooltip.value
          if (v.type !== 'ObjectExpression' || !findProp(v, 'backgroundColor')) {
            context.report({ node: v, messageId: 'needTooltipTheme' })
          }
        }

        for (const tok of sourceCode.getTokens(node, { includeComments: false })) {
          if (tok.type !== 'String' && tok.type !== 'Template') continue
          if (COLOR_RE.test(tok.value)) {
            context.report({ loc: tok.loc.start, messageId: 'noColorLiteral' })
          }
        }
      },
    }
  },
}
