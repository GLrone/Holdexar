package derived

// difflib SequenceMatcher.ratio 的最小移植（规则 7/8 的名称相似度依赖）：
// 最长匹配块递归 + 相邻块合并；junk 恒空（调用处不传 junk）。
// autojunk 仅在 len(b) ≥ 200 时生效（剔除 b 中高频元素）——系列比对对象
// 是短名称串，正常走不到，但按上游语义完整移植以保一致。

type seqMatch struct {
	aPos, bPos, size int
}

type seqMatcher struct {
	a, b     []rune
	b2j      map[rune][]int
	bpopular map[rune]bool
}

func newSeqMatcher(a, b []rune) *seqMatcher {
	m := &seqMatcher{a: a, b: b, b2j: map[rune][]int{}}
	for j, r := range b {
		m.b2j[r] = append(m.b2j[r], j)
	}
	if n := len(b); n >= 200 {
		ntest := n/100 + 1
		m.bpopular = map[rune]bool{}
		for r, idxs := range m.b2j {
			if len(idxs) > ntest {
				m.bpopular[r] = true
				delete(m.b2j, r)
			}
		}
	}
	return m
}

// findLongestMatch [alo,ahi)×[blo,bhi) 窗口内最长公共块；b2j 缺席元素
// （junk/popular）不参与匹配，块端可经相等延伸吸收它们。
func (m *seqMatcher) findLongestMatch(alo, ahi, blo, bhi int) seqMatch {
	besti, bestj, bestsize := alo, blo, 0
	j2len := map[int]int{}
	for i := alo; i < ahi; i++ {
		newj2len := map[int]int{}
		for _, j := range m.b2j[m.a[i]] {
			if j < blo {
				continue
			}
			if j >= bhi {
				break
			}
			k := j2len[j-1] + 1
			newj2len[j] = k
			if k > bestsize {
				besti, bestj, bestsize = i-k+1, j-k+1, k
			}
		}
		j2len = newj2len
	}
	for besti > alo && bestj > blo && m.a[besti-1] == m.b[bestj-1] {
		besti, bestj, bestsize = besti-1, bestj-1, bestsize+1
	}
	for besti+bestsize < ahi && bestj+bestsize < bhi &&
		m.a[besti+bestsize] == m.b[bestj+bestsize] {
		bestsize++
	}
	return seqMatch{besti, bestj, bestsize}
}

func (m *seqMatcher) matchingBlocks() []seqMatch {
	type span struct{ alo, ahi, blo, bhi int }
	queue := []span{{0, len(m.a), 0, len(m.b)}}
	var blocks []seqMatch
	for len(queue) > 0 {
		s := queue[len(queue)-1]
		queue = queue[:len(queue)-1]
		mt := m.findLongestMatch(s.alo, s.ahi, s.blo, s.bhi)
		if mt.size == 0 {
			continue
		}
		blocks = append(blocks, mt)
		if s.alo < mt.aPos && s.blo < mt.bPos {
			queue = append(queue, span{s.alo, mt.aPos, s.blo, mt.bPos})
		}
		if mt.aPos+mt.size < s.ahi && mt.bPos+mt.size < s.bhi {
			queue = append(queue, span{mt.aPos + mt.size, s.ahi, mt.bPos + mt.size, s.bhi})
		}
	}
	sortSeqMatches(blocks)
	var out []seqMatch
	i1, j1, k1 := 0, 0, 0
	for _, b := range blocks {
		if i1+k1 == b.aPos && j1+k1 == b.bPos {
			k1 += b.size
			continue
		}
		if k1 > 0 {
			out = append(out, seqMatch{i1, j1, k1})
		}
		i1, j1, k1 = b.aPos, b.bPos, b.size
	}
	if k1 > 0 {
		out = append(out, seqMatch{i1, j1, k1})
	}
	out = append(out, seqMatch{len(m.a), len(m.b), 0})
	return out
}

func sortSeqMatches(blocks []seqMatch) {
	for i := 1; i < len(blocks); i++ {
		for j := i; j > 0; j-- {
			x, y := blocks[j-1], blocks[j]
			if x.aPos < y.aPos || (x.aPos == y.aPos && x.bPos < y.bPos) {
				break
			}
			blocks[j-1], blocks[j] = y, x
		}
	}
}

// ratio 相似度 = 2·匹配数/(len(a)+len(b))；双空串 = 1.0。
func (m *seqMatcher) ratio() float64 {
	matches := 0
	for _, b := range m.matchingBlocks() {
		matches += b.size
	}
	if length := len(m.a) + len(m.b); length > 0 {
		return 2.0 * float64(matches) / float64(length)
	}
	return 1.0
}

func sequenceRatio(a, b string) float64 {
	return newSeqMatcher([]rune(a), []rune(b)).ratio()
}
