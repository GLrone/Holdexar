package proxypool

func NameKeys(name string) []string {
	text := pyTrimSpace(name)
	if text == "" {
		return nil
	}
	keys := []string{text}
	if base := nameBase(text); base != "" && base != text {
		keys = append(keys, base)
	}
	return keys
}
