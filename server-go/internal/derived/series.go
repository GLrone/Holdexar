package derived

// 游戏系列识别：本地名称聚类，零网络开销。输入 games.name / name_en，
// 名称相似启发式 + 并查集聚类，写回 games.series_id。
// 语义约定：NULL = 未计算；'' = 已计算但落单；非空 = 系列标识名。
// 宁缺毋滥：每条规则带负面词表或长度门槛，防一条错边污染整簇。

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"
)

const (
	clusterMinMember = 2
	seriesIDMax      = 100
)

// 参与聚类的类型：正作才有「系列」语义；NULL = 未分类按可聚类处理。
const clusterableCond = "(type = 'GAME' OR type IS NULL)"

// 分隔符前缀判定的负面中文前缀：二字通用词，撞车率高。
var badCnBases = map[string]bool{
	"模拟": true, "我的": true, "传奇": true, "动作": true, "逃离": true,
	"星际": true, "少女": true, "勇者": true, "我是": true,
}

// 公共词前缀判定的负面英文首词：冠词与泛用起手词；另含品牌伞人名前缀
// （旗下是多条平行系列，品牌相同不等于同系列）。
var badEnStarts = map[string]bool{
	"the": true, "a": true, "an": true, "i": true, "super": true,
	"world": true, "of": true, "simulator": true, "vr": true,
	"project": true, "tom": true, "sid": true,
}

// 泛用次词：公共前缀恰好 2 词且次词是虚词时证据不足。
var genericMinor = map[string]bool{
	"of": true, "the": true, "a": true, "an": true, "and": true,
	"&": true, "to": true, "am": true,
}

// 罗马数字 → 阿拉伯（只用于比对归一，不动存储数据）。
var roman = map[string]string{
	"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8",
	"ix": "9", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
}

var romanValues = func() map[string]bool {
	m := map[string]bool{}
	for _, v := range roman {
		m[v] = true
	}
	return m
}()

// 官定同系的名称改写表：外传官方名不以系列词开头，按 token 串前缀把
// 系列词置换到开头，让公共前缀规则照常生效。
var forcedSeriesPrefix = []struct {
	head   string
	series []string
}{
	{"crisis core final fantasy", []string{"final", "fantasy"}},
	{"lightning returns", []string{"final", "fantasy"}},
	{"stranger of paradise final fantasy", []string{"final", "fantasy"}},
	{"world of final fantasy", []string{"final", "fantasy"}},
	{"ender lilies", []string{"ender", "series"}},
	{"ender magnolia", []string{"ender", "series"}},
}

var (
	enSplitRe = regexp.MustCompile(`[^a-z0-9]+`)
	cnKeepRe  = regexp.MustCompile(`[^\x{4e00}-\x{9fff}]+`)
	sepRe     = regexp.MustCompile(`[:：\-–—]`)
	tailVerRe = regexp.MustCompile(`\d{1,2}k\d{2}`)
)

func enTokens(name string) []string {
	lowered := strings.ToLower(name)
	lowered = strings.ReplaceAll(lowered, "'", "")
	lowered = strings.ReplaceAll(lowered, "’", "")
	raw := strings.Fields(enSplitRe.ReplaceAllString(lowered, " "))
	out := make([]string, len(raw))
	for i, t := range raw {
		if v, ok := roman[t]; ok {
			out[i] = v
		} else {
			out[i] = t
		}
	}
	return out
}

func cnChars(name string) string {
	return cnKeepRe.ReplaceAllString(name, "")
}

// seriesTokens 参与系列判定的英文 token 序列；命中改写表以系列词置换开头。
func seriesTokens(nameEn *string, name string) []string {
	base := name
	if nameEn != nil && *nameEn != "" {
		base = *nameEn
	}
	toks := enTokens(base)
	joined := strings.Join(toks, " ")
	for _, f := range forcedSeriesPrefix {
		if strings.HasPrefix(joined, f.head) {
			return append(append([]string{}, f.series...), toks[len(strings.Fields(f.head)):]...)
		}
	}
	return toks
}

// sepBase 分隔符前缀：首个冒号/连字符/破折号前的部分。
func sepBase(name string) (string, string, bool) {
	loc := sepRe.FindStringIndex(name)
	if loc == nil {
		return "", "", false
	}
	raw := strings.TrimSpace(name[:loc[0]])
	if raw == "" {
		return "", "", false
	}
	return raw, strings.Join(enTokens(raw), " "), true
}

func commonPrefixLen(a, b []string) int {
	n := 0
	for i := 0; i < len(a) && i < len(b); i++ {
		if a[i] != b[i] {
			break
		}
		n++
	}
	return n
}

// isSequelNumber 续作序号词；"0" 是版本标记（2.0 → [2, 0]）不算。
func isSequelNumber(tok string) bool {
	return (allDigits(tok) && tok != "0") || romanValues[tok]
}

func allDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// isVersionTail 规则 5 尾部「版本号」词：纯序号之外容年货式 2K21/2K26。
func isVersionTail(tok string) bool {
	return isSequelNumber(tok) || tailVerRe.MatchString(tok)
}

func baseWoTailNum(base string) string {
	toks := strings.Split(base, " ")
	if len(toks) > 1 && isSequelNumber(toks[len(toks)-1]) {
		toks = toks[:len(toks)-1]
	}
	return strings.Join(toks, " ")
}

// enUsable 裸序号名（"2" / "II"）是零证据——两两相似度 1.0 会互连成
// 枢纽把各自正确的对黏成一大簇。
func enUsable(toks []string) bool {
	return len(toks) > 0 && !(len(toks) == 1 && isSequelNumber(toks[0]))
}

// isSameSeries 两个名字是否同系列（宁缺毋滥，任一规则命中即算）。
func isSameSeries(name1 string, en1 *string, name2 string, en2 *string) bool {
	t1 := seriesTokens(en1, name1)
	t2 := seriesTokens(en2, name2)
	c1 := cnChars(name1)
	c2 := cnChars(name2)
	en1All := name1
	if en1 != nil && strings.TrimSpace(*en1) != "" {
		en1All = *en1
	}
	en2All := name2
	if en2 != nil && strings.TrimSpace(*en2) != "" {
		en2All = *en2
	}
	// 结构规则剥离开头冠词再比：冠词是装饰，剥掉后 The Witcher 1/2/3
	// 与 Witcher 1/2/3 的前缀判定同轨
	s1 := t1
	if len(s1) > 0 && (s1[0] == "the" || s1[0] == "a" || s1[0] == "an") {
		s1 = s1[1:]
	}
	s2 := t2
	if len(s2) > 0 && (s2[0] == "the" || s2[0] == "a" || s2[0] == "an") {
		s2 = s2[1:]
	}

	// 1) 英文分隔符前缀一致（≥4 字符；前缀只差尾部序号也算）
	_, b1n, b1ok := sepBase(en1All)
	_, b2n, b2ok := sepBase(en2All)
	if b1ok && b2ok {
		for _, pair := range [][2]string{
			{b1n, b2n},
			{baseWoTailNum(b1n), baseWoTailNum(b2n)},
		} {
			if pair[0] == pair[1] && len(pair[0]) >= 4 {
				return true
			}
		}
	}

	// 2) 中文分隔符前缀一致，≥2 字且不在负面词表
	cb1, _, cb1ok := sepBase(name1)
	cb2, _, cb2ok := sepBase(name2)
	if cb1ok && cb2ok {
		base1 := cnChars(cb1)
		base2 := cnChars(cb2)
		if len([]rune(base1)) >= 2 && base1 == base2 && !badCnBases[base1] {
			return true
		}
	}

	// 3) 英文连续公共词前缀 ≥2；恰好 2 词时弱证据一档（首词不在负面
	//    词表且次词非虚词），≥3 词前缀本身已够特异
	pre := commonPrefixLen(s1, s2)
	if pre >= 2 && (pre >= 3 || (!badEnStarts[s1[0]] && !genericMinor[s1[1]])) {
		return true
	}

	// 4) 纯序号续作：短名是长名的完整词前缀，且前缀后紧跟序号
	short, long_ := s1, s2
	if len(s2) < len(s1) {
		short, long_ = s2, s1
	}
	if enUsable(short) && len(long_) > len(short) && prefixEqual(long_, short) &&
		isSequelNumber(long_[len(short)]) {
		return true
	}

	// 5) 同前缀 + 尾部版本号互异（FIFA 22/23、2K25/2K26、Doom 3/2016）；
	//    前缀 ≥3 字符防编号撞车，2 词时同规则 3 的弱证据门槛
	if pre >= 1 {
		rest1, rest2 := s1[pre:], s2[pre:]
		strong := pre >= 3 || (!badEnStarts[s1[0]] && (pre == 1 || !genericMinor[s1[1]]))
		if strong && len(rest1) == 1 && len(rest2) == 1 &&
			isVersionTail(rest1[0]) && isVersionTail(rest2[0]) &&
			rest1[0] != rest2[0] && len(strings.Join(s1[:pre], "")) >= 3 {
			return true
		}
	}

	// 6) 中文严格前缀（辐射4 → 辐射：新维加斯），≥2 字且不在负面词表
	if c1 != c2 && len([]rune(c1)) >= 2 && len([]rune(c2)) >= 2 {
		shortC, longC := c1, c2
		if len([]rune(c2)) < len([]rune(c1)) {
			shortC, longC = c2, c1
		}
		if strings.HasPrefix(longC, shortC) && !badCnBases[shortC] {
			return true
		}
	}

	// 7) 中文完全一致 + 英文有佐证（短中文名撞车最频繁，须英文作保）
	if c1 != "" && c1 == c2 {
		ratio := sequenceRatio(strings.ToLower(en1All), strings.ToLower(en2All))
		if commonPrefixLen(s1, s2) >= 1 ||
			(ratio >= 0.5 && enUsable(s1) && enUsable(s2)) {
			return true
		}
	}

	// 8) 高相似度兜底（容错重制版/版本差异的英文写法）
	if enUsable(s1) && enUsable(s2) {
		if sequenceRatio(strings.Join(s1, " "), strings.Join(s2, " ")) > 0.90 {
			return true
		}
	}
	return false
}

func prefixEqual(long_, short []string) bool {
	for i, t := range short {
		if long_[i] != t {
			return false
		}
	}
	return true
}

// ── 聚类 ────────────────────────────────────────────────────────

type seriesMember struct {
	appid int64
	name  string
	nameE *string
	toks  []string
	cn    string
}

func newSeriesMember(appid int64, name string, nameEn *string) seriesMember {
	return seriesMember{appid: appid, name: name, nameE: nameEn,
		toks: seriesTokens(nameEn, name), cn: cnChars(name)}
}

func (m seriesMember) bucketKeys() [][2]string {
	var keys [][2]string
	toks := m.toks
	if len(toks) > 0 {
		first := toks[0]
		if len(toks) > 1 && (toks[0] == "the" || toks[0] == "a" || toks[0] == "an") {
			first = toks[1]
		}
		keys = append(keys, [2]string{"EN", first})
		if len(toks) > 1 && !genericMinor[toks[1]] && !allDigits(toks[1]) {
			keys = append(keys, [2]string{"EN2", toks[1]})
		}
	}
	rs := []rune(m.cn)
	if len(rs) >= 2 {
		keys = append(keys, [2]string{"CN", string(rs[:2])})
	}
	return keys
}

// SeriesRow 聚类输入行（games 表读取投影）。
type SeriesRow struct {
	AppID    int64
	Name     *string
	NameEn   *string
	SeriesID *string
}

type seriesCluster struct {
	name string
	ids  []int64
}

// buildClusters 名称行 → 系列簇；纯启发式簇只含 ≥2 成员。覆盖表成员
// 先按键预成组（组名以键为准），再叠加名称启发式的边——成员经名称规则
// 命中的邻居自动并入。
func buildClusters(rows []SeriesRow, overrides map[string][]int64) []seriesCluster {
	var members []seriesMember
	for _, r := range rows {
		name := ""
		if r.Name != nil {
			name = *r.Name
		}
		if strings.TrimSpace(name) == "" {
			continue
		}
		members = append(members, newSeriesMember(r.AppID, name, r.NameEn))
	}
	parent := make([]int, len(members))
	for i := range parent {
		parent[i] = i
	}
	var find func(i int) int
	find = func(i int) int {
		for parent[i] != i {
			parent[i] = parent[parent[i]]
			i = parent[i]
		}
		return i
	}
	union := func(i, j int) {
		ri, rj := find(i), find(j)
		if ri != rj {
			parent[ri] = rj
		}
	}

	// 覆盖种子先落：同一键下全部成员互连成一组
	appidKey := map[int64]string{}
	keyMembers := map[string][]int{}
	if len(overrides) > 0 {
		indexByAppid := map[int64]int{}
		for i, m := range members {
			indexByAppid[m.appid] = i
		}
		for key, ids := range overrides {
			for _, appid := range ids {
				if _, ok := appidKey[appid]; !ok {
					appidKey[appid] = key
				}
				if idx, ok := indexByAppid[appid]; ok {
					keyMembers[key] = append(keyMembers[key], idx)
				}
			}
		}
		for _, idxs := range keyMembers {
			for _, j := range idxs[1:] {
				union(idxs[0], j)
			}
		}
	}

	buckets := map[[2]string][]int{}
	for i, m := range members {
		for _, key := range m.bucketKeys() {
			buckets[key] = append(buckets[key], i)
		}
	}
	for _, indices := range buckets {
		if len(indices) < 2 {
			continue
		}
		for a := 0; a < len(indices); a++ {
			for b := a + 1; b < len(indices); b++ {
				i, j := indices[a], indices[b]
				if find(i) == find(j) {
					continue
				}
				mi, mj := members[i], members[j]
				if isSameSeries(mi.name, mi.nameE, mj.name, mj.nameE) {
					union(i, j)
				}
			}
		}
	}

	groups := map[int][]seriesMember{}
	var roots []int
	for i, m := range members {
		root := find(i)
		if _, ok := groups[root]; !ok {
			roots = append(roots, root)
		}
		groups[root] = append(groups[root], m)
	}

	var out []seriesCluster
	for _, root := range roots {
		grp := groups[root]
		keysHit := map[string]int{}
		for _, m := range grp {
			if key, ok := appidKey[m.appid]; ok {
				keysHit[key]++
			}
		}
		var name string
		if len(keysHit) > 0 {
			keys := make([]string, 0, len(keysHit))
			for k := range keysHit {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			best := keys[0]
			for _, k := range keys[1:] {
				if keysHit[k] > keysHit[best] {
					best = k
				}
			}
			name = best
		} else {
			if len(grp) < clusterMinMember {
				continue
			}
			name = seriesName(grp)
		}
		sort.Slice(grp, func(i, j int) bool { return grp[i].appid < grp[j].appid })
		ids := make([]int64, len(grp))
		for i, m := range grp {
			ids[i] = m.appid
		}
		out = append(out, seriesCluster{name: name, ids: ids})
	}
	sort.Slice(out, func(i, j int) bool {
		return strings.ToLower(out[i].name) < strings.ToLower(out[j].name)
	})
	return out
}

// pyTitle 对齐 Python str.title：非字母后随字母大写。
func pyTitle(s string) string {
	prevLetter := false
	var b strings.Builder
	for _, r := range s {
		out := r
		if !prevLetter {
			out = unicode.ToUpper(r)
		}
		b.WriteRune(out)
		prevLetter = unicode.IsLetter(r)
	}
	return b.String()
}

func displayTokens(tokens []string) string {
	var kept []string
	for _, t := range tokens {
		if len(t) > 1 || allDigits(t) {
			kept = append(kept, t)
		}
	}
	joined := strings.Join(kept, " ")
	if strings.TrimSpace(joined) == "" {
		return ""
	}
	return pyTitle(joined)
}

// seriesName 系列标识名：成员英文名的逐位众数前缀（剥冠词后计算）。
// 位 0 只求半数一致（定锚系列词），位 ≥1 须全员一致。
func seriesName(grp []seriesMember) string {
	var tokenLists [][]string
	for _, m := range grp {
		toks := m.toks
		if len(toks) > 0 && (toks[0] == "the" || toks[0] == "a" || toks[0] == "an") {
			toks = toks[1:]
		}
		if len(toks) > 0 {
			tokenLists = append(tokenLists, toks)
		}
	}
	var prefix []string
	for i := 0; len(tokenLists) > 0 && i < 6; i++ {
		counts := map[string]int{}
		var order []string
		for _, toks := range tokenLists {
			if i < len(toks) {
				if _, ok := counts[toks[i]]; !ok {
					order = append(order, toks[i])
				}
				counts[toks[i]]++
			}
		}
		if len(order) == 0 {
			break
		}
		tok := order[0]
		for _, k := range order[1:] {
			if counts[k] > counts[tok] ||
				(counts[k] == counts[tok] && len(k) < len(tok)) {
				tok = k
			}
		}
		need := len(tokenLists)
		if i == 0 {
			need = (len(tokenLists) + 1) / 2
			if need < 2 {
				need = 2
			}
		}
		if counts[tok] < need {
			break
		}
		prefix = append(prefix, tok)
	}

	shortest := grp[0]
	for _, m := range grp[1:] {
		if len(derefStrOr(m.nameE, m.name)) < len(derefStrOr(shortest.nameE, shortest.name)) {
			shortest = m
		}
	}
	fallback := strings.TrimSpace(derefStrOr(shortest.nameE, shortest.name))
	if len(prefix) > 0 {
		name := displayTokens(prefix)
		if name == "" {
			name = displayTokens(tokenLists[0])
		}
		if name == "" {
			name = fallback
		}
		return truncateRunes(name, seriesIDMax)
	}
	return truncateRunes(fallback, seriesIDMax)
}

func derefStrOr(p *string, fallback string) string {
	if p != nil && *p != "" {
		return *p
	}
	return fallback
}

func truncateRunes(s string, n int) string {
	rs := []rune(s)
	if len(rs) <= n {
		return s
	}
	return string(rs[:n])
}

// ── 增量受影响行 ────────────────────────────────────────────────

// affectedRows 覆盖版本未变时的窄化：只重算受 changed 款波及的行
// （同簇成员 / 同桶名称邻居 / 覆盖成员）。
func affectedRows(rows []SeriesRow, changed map[int64]string, overrides map[string][]int64) []SeriesRow {
	byID := map[int64]SeriesRow{}
	groups := map[string]map[int64]bool{}
	buckets := map[[2]string]map[int64]bool{}
	memberOf := map[int64]seriesMember{}
	for _, r := range rows {
		byID[r.AppID] = r
		m := newSeriesMember(r.AppID, derefStrOr(r.Name, ""), r.NameEn)
		memberOf[r.AppID] = m
		if r.SeriesID != nil && *r.SeriesID != "" {
			if groups[*r.SeriesID] == nil {
				groups[*r.SeriesID] = map[int64]bool{}
			}
			groups[*r.SeriesID][r.AppID] = true
		}
		for _, key := range m.bucketKeys() {
			if buckets[key] == nil {
				buckets[key] = map[int64]bool{}
			}
			buckets[key][r.AppID] = true
		}
	}
	affected := map[int64]bool{}
	for aid := range changed {
		affected[aid] = true
	}
	for _, group := range changed {
		for aid := range groups[group] {
			affected[aid] = true
		}
	}
	for _, ids := range overrides {
		hit := false
		for _, aid := range ids {
			if _, ok := changed[aid]; ok {
				hit = true
				break
			}
		}
		if !hit {
			continue
		}
		for _, aid := range ids {
			affected[aid] = true
			if r, ok := byID[aid]; ok {
				sid := ""
				if r.SeriesID != nil {
					sid = *r.SeriesID
				}
				for other := range groups[sid] {
					affected[other] = true
				}
			}
		}
	}
	for aid := range changed {
		row, ok := byID[aid]
		if !ok {
			continue
		}
		m := memberOf[aid]
		for _, key := range m.bucketKeys() {
			for other := range buckets[key] {
				otherRow := byID[other]
				if isSameSeries(derefStrOr(row.Name, ""), row.NameEn,
					derefStrOr(otherRow.Name, ""), otherRow.NameEn) {
					affected[other] = true
					sid := ""
					if otherRow.SeriesID != nil {
						sid = *otherRow.SeriesID
					}
					for member := range groups[sid] {
						affected[member] = true
					}
				}
			}
		}
	}
	var out []SeriesRow
	for _, r := range rows {
		if affected[r.AppID] {
			out = append(out, r)
		}
	}
	return out
}

// ── refresh_series ──────────────────────────────────────────────

const savedVersionKey = "games.derived_inputs"

// RefreshSeries 重算系列归属并写回 games.series_id。changed 非 nil 时
// 走增量窄化（覆盖版本未变）；覆盖版本变更时全量重算并保存版本。
// 返回更新行数。事务与写闸由调用方持有。
func RefreshSeries(ctx context.Context, conn *sql.Conn, changed map[int64]string, overrides map[string][]int64) (int, error) {
	rows, err := querySeriesRows(ctx, conn, clusterableCond+" ORDER BY appid", nil)
	if err != nil {
		return 0, fmt.Errorf("系列候选行读取失败: %w", err)
	}
	// 覆盖成员不在可聚类集时补拉（金版/DEMO 等也可能被覆盖点名）
	inRows := map[int64]bool{}
	for _, r := range rows {
		inRows[r.AppID] = true
	}
	var listed []int64
	for _, ids := range overrides {
		for _, aid := range ids {
			if !inRows[aid] {
				listed = append(listed, aid)
				inRows[aid] = true
			}
		}
	}
	if len(listed) > 0 {
		sort.Slice(listed, func(i, j int) bool { return listed[i] < listed[j] })
		extra, err := querySeriesRows(ctx, conn,
			"appid IN ("+inPlaceholders(listed)+") ORDER BY appid", listed)
		if err != nil {
			return 0, fmt.Errorf("覆盖成员读取失败: %w", err)
		}
		rows = append(rows, extra...)
	}

	if changed != nil {
		saved, err := readSavedVersion(ctx, conn)
		if err != nil {
			return 0, err
		}
		if seriesVersionEqual(saved, overrides) {
			rows = affectedRows(rows, changed, overrides)
		} else if err := saveInputVersion(ctx, conn, saved, overrides); err != nil {
			return 0, err
		}
	}

	clusters := buildClusters(rows, overrides)
	newMap := map[int64]string{}
	for _, c := range clusters {
		for _, aid := range c.ids {
			newMap[aid] = c.name
		}
	}
	stmt, err := conn.PrepareContext(ctx,
		"UPDATE games SET series_id=? WHERE appid=?")
	if err != nil {
		return 0, fmt.Errorf("系列更新语句准备失败: %w", err)
	}
	defer stmt.Close()
	updates := 0
	for _, r := range rows {
		next := newMap[r.AppID]
		// NULL（未计算）与 ''（已计算落单）语义不同，NULL 必须写
		if r.SeriesID == nil || *r.SeriesID != next {
			if _, err := stmt.ExecContext(ctx, next, r.AppID); err != nil {
				return 0, fmt.Errorf("系列更新写入失败: %w", err)
			}
			updates++
		}
	}

	// 非可聚类行清系列（覆盖点名的除外）
	exclCond := "(NOT " + clusterableCond + ") AND series_id IS NOT NULL AND series_id != ''"
	var overrideIDs []int64
	for _, ids := range overrides {
		overrideIDs = append(overrideIDs, ids...)
	}
	if len(overrideIDs) > 0 {
		exclCond += " AND appid NOT IN (" + inPlaceholders(overrideIDs) + ")"
	}
	excluded, err := queryIDs(ctx, conn, exclCond, overrideIDs)
	if err != nil {
		return 0, fmt.Errorf("系列排除行读取失败: %w", err)
	}
	for _, aid := range excluded {
		if _, err := stmt.ExecContext(ctx, "", aid); err != nil {
			return 0, fmt.Errorf("系列清空写入失败: %w", err)
		}
		updates++
	}
	return updates, nil
}

func querySeriesRows(ctx context.Context, conn *sql.Conn, cond string, args []int64) ([]SeriesRow, error) {
	q := "SELECT appid, name, name_en, series_id FROM games WHERE " + cond
	rows, err := conn.QueryContext(ctx, q, int64sToAny(args)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []SeriesRow
	for rows.Next() {
		var r SeriesRow
		var name, nameEn, sid sql.NullString
		if err := rows.Scan(&r.AppID, &name, &nameEn, &sid); err != nil {
			return nil, err
		}
		r.Name = nullStrPtr(name)
		r.NameEn = nullStrPtr(nameEn)
		r.SeriesID = nullStrPtr(sid)
		out = append(out, r)
	}
	return out, rows.Err()
}

func queryIDs(ctx context.Context, conn *sql.Conn, cond string, args []int64) ([]int64, error) {
	rows, err := conn.QueryContext(ctx,
		"SELECT appid FROM games WHERE "+cond, int64sToAny(args)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []int64
	for rows.Next() {
		var aid int64
		if err := rows.Scan(&aid); err != nil {
			return nil, err
		}
		out = append(out, aid)
	}
	return out, rows.Err()
}

// readSavedVersion 读取派生输入版本（缺行 = nil）。
func readSavedVersion(ctx context.Context, conn *sql.Conn) (*string, error) {
	var saved sql.NullString
	err := conn.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key=?", savedVersionKey).Scan(&saved)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("派生输入版本读取失败: %w", err)
	}
	if !saved.Valid {
		return nil, nil
	}
	s := saved.String
	return &s, nil
}

// seriesVersionEqual 已存版本的 series 与当前覆盖表一致（全量→增量的切换判据）。
func seriesVersionEqual(saved *string, overrides map[string][]int64) bool {
	var version map[string]any
	if saved == nil || *saved == "" {
		version = map[string]any{}
	} else if err := json.Unmarshal([]byte(*saved), &version); err != nil {
		return false
	}
	raw, ok := version["series"]
	if !ok {
		return false
	}
	m, ok := raw.(map[string]any)
	if !ok || len(m) != len(overrides) {
		return false
	}
	for k, want := range overrides {
		arr, ok := m[k].([]any)
		if !ok || len(arr) != len(want) {
			return false
		}
		for i, id := range want {
			f, ok := arr[i].(float64)
			if !ok || int64(f) != id {
				return false
			}
		}
	}
	return true
}

// saveInputVersion 保存派生输入版本（对齐 json.dumps(sort_keys=True)）。
func saveInputVersion(ctx context.Context, conn *sql.Conn, saved *string, overrides map[string][]int64) error {
	version := map[string]any{}
	if saved != nil && *saved != "" {
		if err := json.Unmarshal([]byte(*saved), &version); err != nil {
			return fmt.Errorf("派生输入版本解析失败: %w", err)
		}
	}
	version["series"] = overrides
	if _, err := conn.ExecContext(ctx,
		"INSERT INTO app_settings(key, value_json) VALUES(?, ?) "+
			"ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
		savedVersionKey, pyJSONSorted(version)); err != nil {
		return fmt.Errorf("派生输入版本保存失败: %w", err)
	}
	return nil
}

// pyJSONSorted 对齐 Python json.dumps(sort_keys=True) 的紧凑渲染
// （键排序、", "/": " 分隔、ensure_ascii 转义）。
func pyJSONSorted(v any) string {
	var b strings.Builder
	writePyJSON(&b, v)
	return b.String()
}

func writePyJSON(b *strings.Builder, v any) {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case string:
		b.WriteString(pyJSONStr(x))
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case int:
		fmt.Fprintf(b, "%d", x)
	case int64:
		fmt.Fprintf(b, "%d", x)
	case float64:
		fmt.Fprintf(b, "%v", x)
	case []string:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(", ")
			}
			b.WriteString(pyJSONStr(e))
		}
		b.WriteByte(']')
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(", ")
			}
			writePyJSON(b, e)
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteString(", ")
			}
			b.WriteString(pyJSONStr(k))
			b.WriteString(": ")
			writePyJSON(b, x[k])
		}
		b.WriteByte('}')
	case map[string][]int64:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteString(", ")
			}
			b.WriteString(pyJSONStr(k))
			b.WriteString(": [")
			for j, id := range x[k] {
				if j > 0 {
					b.WriteString(", ")
				}
				fmt.Fprintf(b, "%d", id)
			}
			b.WriteByte(']')
		}
		b.WriteByte('}')
	default:
		b.WriteString(pyJSONStr(fmt.Sprint(v)))
	}
}

// pyJSONStr 对齐 ensure_ascii：控制位与非 ASCII 按 \uXXXX 小写转义。
func pyJSONStr(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			b.WriteString("\\\"")
		case r == '\\':
			b.WriteString("\\\\")
		case r == '\n':
			b.WriteString("\\n")
		case r == '\r':
			b.WriteString("\\r")
		case r == '\t':
			b.WriteString("\\t")
		case r < 0x20 || r == 0x7f || r > 0x7e:
			fmt.Fprintf(&b, "\\u%04x", r)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String()
}

func int64sToAny(ids []int64) []any {
	out := make([]any, len(ids))
	for i, v := range ids {
		out[i] = v
	}
	return out
}

func inPlaceholders(ids []int64) string {
	return strings.TrimSuffix(strings.Repeat("?,", len(ids)), ",")
}

func nullStrPtr(n sql.NullString) *string {
	if !n.Valid {
		return nil
	}
	s := n.String
	return &s
}
