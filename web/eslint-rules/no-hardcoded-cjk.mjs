/* ════════════════════════════════════════════════════════════════════
   双语红线（用户可见文案禁止硬编码中文）。

   规则跑在 `lint:gate`（`eslint src --quiet`，只报 error）上——warn 级
   护栏在构建链里不起拦截作用。

   **报什么**（只报「字符串字面量」，注释天然不在 AST 里，不必对注释做白名单）：
   · JS/TS：String Literal、TemplateLiteral 的每个 quasi
   · Vue 模板：文本节点 VText、静态属性值 VLiteral（placeholder="搜索" 这类）

   **不报什么**（逐条对应一个误报源）：
   · 正则字面量 —— `/(\d{4})[-/年]/` 里的「年」是解析 Steam 日期串的字符集，
     不是文案（漏了这条会逼人写 `new RegExp()`）。
   · 结构属性 class / style / ref / key / slot / name —— 类名与插槽名是结构
     标识，不是用户可见文案。
   · 词典目录 src/locales/** —— 那是中文的落点，由 config 整目录豁免。

   **豁免机制与 EP 收口同构**：存量中文文件列进 eslint.config.mjs 的
   `I18N_LEGACY_FILES` 降为 warn，未列入的文件一律 error。清单只减不增。

   **`allow` 选项：中文字面量里的「数据」**。有些中文串是**协议值**而非文案，
   必须逐字保持中文——例如 `views/bills/LedgerTab.vue` 的 `tx_type` 筛选值：
   后端 `parser.py` 把英文规范化成中文存库（`TX_TYPE_NORM`），查询侧是
   `BillTxType.tx_type == tx_type` 的**等值匹配**，改一个字符筛选就静默返回空。

   用法是逐字白名单：`['error', { allow: ['购买', …] }]`——把「这是数据」
   写成可评审的配置，与「用 `\uXXXX` 转义骗过规则」有本质区别。
   白名单要尽量短、逐字，旁边写清用途；它不是逃生舱：词条化的文案被加进来
   等于红线在该文件失效。

   ⚠️ **模板必须显式订阅**：vue-eslint-parser 把 `<template>` 解析成另一棵
   AST（`templateBody`），ESLint 默认只遍历 `Program`。要拿到模板文本必须调
   `parserServices.defineTemplateBodyVisitor()`——vue 生态的既定机制
   （eslint-plugin-vue 的 `vue/no-restricted-syntax` 同样依赖它）。
   不订阅则脚本侧照常报错、模板侧 0 命中，而模板是 UI 文案的主场。
   ════════════════════════════════════════════════════════════════════ */

/** 汉字。刻意不含中文标点：只写「，」「·」不构成一句文案，而放宽到
 *  ＀-￯（全角形式）会把全角括号之类无关字符也扫进来。 */
const CJK = /[一-鿿]/

/** 结构属性：值是标识符而非文案。用户可见的属性
 *  （placeholder / title / alt / label / content …）不在此列，照常报。 */
const STRUCTURAL_ATTRS = new Set(['class', 'style', 'ref', 'key', 'slot', 'name', 'is'])

/** 从模板里的一个节点往回走，看它是否落在结构属性内。
 *  静态写法 `class="x"` 与绑定写法 `:class="'x'"` 结构不同：前者的值是
 *  `VAttribute.value`，后者是 `VExpressionContainer` 里的一枚 JS `Literal`，
 *  所以只在 `VAttribute` 上判会漏掉绑定式——两条路径必须收敛到同一判据。 */
function inStructuralAttr(node) {
  for (let n = node?.parent, depth = 0; n && depth < 3; n = n.parent, depth++) {
    if (n.type === 'VAttribute') {
      const key = n.key
      // 静态属性：key.name 是字符串；绑定属性：key.argument.name
      const name = typeof key?.name === 'string' ? key.name : key?.argument?.name
      return typeof name === 'string' && STRUCTURAL_ATTRS.has(name)
    }
  }
  return false
}

/** 报文里截一段出来，省去「哪个字面量」的来回找 */
function excerpt(text) {
  const i = text.search(CJK)
  const s = Math.max(0, i - 12)
  const frag = (s > 0 ? '…' : '') + text.slice(s, i + 18) + (i + 18 < text.length ? '…' : '')
  return frag.replace(/\s+/g, ' ')
}

export default {
  meta: {
    type: 'problem',
    docs: {
      description: '禁止硬编码中文文案，用户可见文案一律走 useI18n() 的 t()',
    },
    schema: [
      {
        type: 'object',
        properties: {
          allow: {
            type: 'array',
            items: { type: 'string' },
            description: '逐字豁免的**数据值**（协议枚举 / 与后端等值匹配的串），不是文案',
          },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      cjk:
        '双语红线：用户可见文案禁止硬编码中文（「{{ text }}」）。' +
        '请改走 useI18n() 的 t(\'<模块>.<区块>.<用途>\')，中文词条写进 src/locales/zh-CN/<模块>.ts、' +
        '英文写进 src/locales/en/<模块>.ts。存量文件在 I18N_LEGACY_FILES 豁免；' +
        '新文件出现即构建失败。',
    },
  },

  create(context) {
    /** 逐字豁免的数据值（见文件头 `allow` 选项说明）。整串相等才算，不做子串包含：
     *  若按包含判，`'购买'` 会连「购买记录」这类真文案一起放过去。 */
    const allowed = new Set(context.options[0]?.allow ?? [])

    /** 命中即报；`node` 决定报错落点 */
    function check(node, text) {
      if (typeof text !== 'string' || !CJK.test(text)) return
      if (allowed.has(text)) return
      context.report({ node, messageId: 'cjk', data: { text: excerpt(text) } })
    }

    const handlers = {
      // ── 脚本 AST ──
      Literal(node) {
        // node.regex 存在 ⇒ 正则字面量（见文件头「不报什么」第 1 条）
        if (node.regex) return
        if (inStructuralAttr(node)) return
        check(node, node.value)
      },
      // 模板字符串逐段查：`已加载 ${n} 款` 的 quasis 才是文案，插值表达式另走各自的 visitor
      TemplateLiteral(node) {
        if (inStructuralAttr(node)) return
        for (const q of node.quasis) check(q, q.value.cooked ?? q.value.raw)
      },

      // ── 模板 AST ──
      VText(node) {
        check(node, node.value)
      },
      VAttribute(node) {
        // `:foo="expr"` 的 key 是 VDirectiveKey，静态属性才有 key.name
        const name = node.key?.name
        if (typeof name !== 'string' || STRUCTURAL_ATTRS.has(name)) return
        if (!node.value) return // 无值属性（disabled / required …）
        check(node.value, node.value.value)
      },
    }

    const services = context.sourceCode.parserServices
    // 两棵树不相交，故同一组 handler 交给两边不会重复报：
    // 脚本节点只在 Program 里，模板节点（含 {{ }} / :bind 内的表达式字面量）只在 templateBody 里。
    if (typeof services?.defineTemplateBodyVisitor === 'function') {
      return services.defineTemplateBodyVisitor(handlers, handlers)
    }
    // 非 .vue（纯 .ts）或换了 parser：退化为只查脚本
    return handlers
  },
}
