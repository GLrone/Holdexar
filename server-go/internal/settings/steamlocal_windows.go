//go:build windows

package settings

import (
	"database/sql"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"golang.org/x/sys/windows/registry"

	"holdexar/server-go/internal/store"
)

type steamAccount struct {
	SteamID     string `json:"steam_id"`
	PersonaName string `json:"persona_name"`
}

const (
	steamID64Base = uint64(76561197960265728)
	// 登录用户文件上限字节（超限视为异常环境不入库）。
	maxLoginusersBytes = 2 * 1024 * 1024
)

var steamTokenRe = regexp.MustCompile(`"(?:\\.|[^"\\])*"|//[^\r\n]*|[{}]|[^\s{}"]+`)

func decodeSteamToken(token string) string {
	if len(token) < 2 || token[0] != '"' || token[len(token)-1] != '"' {
		return token
	}
	value := token[1 : len(token)-1]
	replacer := regexp.MustCompile(`\\(.)`)
	return replacer.ReplaceAllStringFunc(value, func(match string) string {
		switch match[1] {
		case 'n':
			return "\n"
		case 'r':
			return "\r"
		case 't':
			return "\t"
		}
		return match[1:]
	})
}

func isSteamID64(value string) bool {
	n, err := strconv.ParseUint(value, 10, 64)
	if err != nil {
		return false
	}
	return steamID64Base <= n && n <= steamID64Base+(1<<32)-1
}

func parseLoginusers(contents string) []steamAccount {
	var tokens []string
	for _, token := range steamTokenRe.FindAllString(contents, -1) {
		if strings.HasPrefix(token, "//") {
			continue
		}
		tokens = append(tokens, token)
	}
	for position := 0; position+1 < len(tokens); position++ {
		if decodeSteamToken(tokens[position]) != "users" || tokens[position+1] != "{" {
			continue
		}
		accounts := []steamAccount{}
		cursor := position + 2
		for cursor < len(tokens) {
			key := decodeSteamToken(tokens[cursor])
			if key == "}" {
				return accounts
			}
			if cursor+1 >= len(tokens) {
				return nil
			}
			if tokens[cursor+1] != "{" {
				cursor += 2
				continue
			}
			cursor += 2
			if isSteamID64(key) {
				persona, next, ok := readSteamAccount(tokens, cursor)
				if !ok {
					return nil
				}
				cursor = next
				accounts = append(accounts, steamAccount{SteamID: key, PersonaName: persona})
			} else {
				next, ok := skipVDFBlock(tokens, cursor)
				if !ok {
					return nil
				}
				cursor = next
			}
		}
		return nil
	}
	return nil
}

func readSteamAccount(tokens []string, position int) (string, int, bool) {
	persona := ""
	for position < len(tokens) {
		key := decodeSteamToken(tokens[position])
		if key == "}" {
			return persona, position + 1, true
		}
		if position+1 >= len(tokens) {
			break
		}
		value := tokens[position+1]
		if value == "{" {
			next, ok := skipVDFBlock(tokens, position+2)
			if !ok {
				return "", 0, false
			}
			position = next
			continue
		}
		if key == "PersonaName" {
			persona = decodeSteamToken(value)
		}
		position += 2
	}
	return "", 0, false
}

func skipVDFBlock(tokens []string, position int) (int, bool) {
	depth := 1
	for position < len(tokens) && depth > 0 {
		switch tokens[position] {
		case "{":
			depth++
		case "}":
			depth--
		}
		position++
	}
	if depth > 0 {
		return 0, false
	}
	return position, true
}

var steamRoots = registeredSteamRoots

func registeredSteamRoots() []string {
	locations := []struct {
		hive    registry.Key
		keyName string
		values  []string
	}{
		{registry.CURRENT_USER, `SOFTWARE\Valve\Steam`, []string{"SteamPath", "InstallPath", "SteamExe"}},
		{registry.LOCAL_MACHINE, `SOFTWARE\Valve\Steam`, []string{"SteamPath", "InstallPath", "SteamExe"}},
		{registry.CURRENT_USER, `SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\steam.exe`, nil},
		{registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\steam.exe`, nil},
	}
	views := []uint32{uint32(registry.WOW64_64KEY), uint32(registry.WOW64_32KEY), 0}
	roots := []string{}
	seen := map[string]bool{}
	for _, location := range locations {
		for _, view := range views {
			openFlags := uint32(registry.READ) | view
			key, err := registry.OpenKey(location.hive, location.keyName, openFlags)
			if err != nil {
				continue
			}
			valueNames := location.values
			if valueNames == nil {
				valueNames = []string{""}
			}
			for _, name := range valueNames {
				value, _, err := key.GetStringValue(name)
				if err != nil {
					continue
				}
				candidate := strings.Trim(strings.TrimSpace(os.ExpandEnv(strings.TrimSpace(value))), `"`)
				candidate = expandUserHome(candidate)
				if strings.EqualFold(filepath.Ext(candidate), ".exe") {
					candidate = filepath.Dir(candidate)
				}
				if candidate == "" {
					continue
				}
				if !seen[strings.ToLower(candidate)] {
					seen[strings.ToLower(candidate)] = true
					roots = append(roots, candidate)
				}
			}
			key.Close()
		}
	}
	return roots
}

func expandUserHome(path string) string {
	if path == "~" || strings.HasPrefix(path, `~\`) || strings.HasPrefix(path, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, strings.TrimPrefix(strings.TrimPrefix(path, "~"), `\`))
		}
	}
	return path
}

func discoverLocalSteamAccounts(roots []string) []steamAccount {
	accounts := map[string]steamAccount{}
	order := []string{}
	for _, root := range roots {
		if _, err := os.Stat(filepath.Join(root, "steam.exe")); err != nil {
			continue
		}
		loginusers := filepath.Join(root, "config", "loginusers.vdf")
		info, err := os.Stat(loginusers)
		if err != nil || info.Size() > maxLoginusersBytes {
			continue
		}
		file, err := os.Open(loginusers)
		if err != nil {
			continue
		}
		raw, err := io.ReadAll(file)
		file.Close()
		if err != nil {
			continue
		}
		contents := strings.TrimPrefix(string(raw), "\ufeff")
		for _, account := range parseLoginusers(contents) {
			if _, seen := accounts[account.SteamID]; !seen {
				accounts[account.SteamID] = account
				order = append(order, account.SteamID)
			}
		}
	}
	out := make([]steamAccount, 0, len(order))
	for _, id := range order {
		out = append(out, accounts[id])
	}
	return out
}

func registerSteamLocalRoutes(mux *http.ServeMux, h *handler) {
	mux.HandleFunc("GET /api/v1/settings/steam-local-accounts", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"accounts": discoverLocalSteamAccounts(steamRoots())})
	})
	mux.HandleFunc("POST /api/v1/settings/steam-local-accounts/import", func(w http.ResponseWriter, r *http.Request) {
		steamID, verr := decodeSteamIDImport(w, r)
		if verr != nil {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []valErr{*verr}})
			return
		}
		var account *steamAccount
		for _, candidate := range discoverLocalSteamAccounts(steamRoots()) {
			if candidate.SteamID == steamID {
				account = &candidate
				break
			}
		}
		if account == nil {
			writeJSON(w, http.StatusNotFound, map[string]any{"detail": "本机 Steam 账号记录已不可用"})
			return
		}
		if err := h.gate.WithWrite(r.Context(), store.PriorityInteractive, func(conn *sql.Conn) error {
			return WriteValue(r.Context(), conn, keySteamID, account.SteamID)
		}); err != nil {
			writeJSON(w, http.StatusInternalServerError, detailNull())
			return
		}
		h.notify(r.Context(), []kvWrite{{key: keySteamID}}, nil)
		writeJSON(w, http.StatusOK, account)
	})
}

func decodeSteamIDImport(w http.ResponseWriter, r *http.Request) (string, *valErr) {
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		return "", &valErr{Type: "missing", Loc: []any{"body"}, Msg: "Field required"}
	}
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" {
		return "", &valErr{Type: "missing", Loc: []any{"body"}, Msg: "Field required", Input: nil}
	}
	var top any
	if err := json.Unmarshal([]byte(trimmed), &top); err != nil {
		return "", &valErr{Type: "json_invalid", Loc: []any{"body"}, Msg: "JSON decode error", Input: map[string]any{}}
	}
	fields, ok := top.(map[string]any)
	if !ok {
		return "", &valErr{Type: "model_attributes_type", Loc: []any{"body"},
			Msg: "Input should be a valid dictionary or object to extract fields from", Input: top}
	}
	value, present := fields["steam_id"]
	if !present || value == nil {
		return "", &valErr{Type: "missing", Loc: []any{"body", "steam_id"}, Msg: "Field required", Input: fields}
	}
	text, ok := value.(string)
	if !ok {
		return "", &valErr{Type: "string_type", Loc: []any{"body", "steam_id"},
			Msg: "Input should be a valid string", Input: value}
	}
	if len(text) < 17 {
		return "", &valErr{Type: "string_too_short", Loc: []any{"body", "steam_id"},
			Msg: "String should have at least 17 characters", Input: text, Ctx: map[string]any{"min_length": 17}}
	}
	if len(text) > 17 {
		return "", &valErr{Type: "string_too_long", Loc: []any{"body", "steam_id"},
			Msg: "String should have at most 17 characters", Input: text, Ctx: map[string]any{"max_length": 17}}
	}
	return text, nil
}
