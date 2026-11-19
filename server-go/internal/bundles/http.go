package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"strconv"
	"strings"

	"holdexar/server-go/internal/httpx"
)

// RegisterOptions 装配选项：Accounting 为手动刷新记账包装，Maintain 为列表/详情入口派生列维护钩子（nil 均跳过）。
type RegisterOptions struct {
	Accounting func(ctx context.Context, job string, body func() (map[string]any, error)) (map[string]any, error)
	Maintain   func(ctx context.Context)
}

func reply(w http.ResponseWriter, status int, value any) {
	httpx.WriteJSON(w, status, value)
}

func (s *Service) Register(mux *http.ServeMux, opts RegisterOptions) {
	maintain := func(ctx context.Context) {
		if opts.Maintain != nil {
			opts.Maintain(ctx)
		}
	}
	mux.HandleFunc("GET /api/v1/bundles", func(w http.ResponseWriter, r *http.Request) {
		maintain(r.Context())
		listOpts, verrs := parseListOptions(r)
		if len(verrs) > 0 {
			replyValidation(w, verrs)
			return
		}
		listOpts.PrimarySteamID = primarySteamID(r.Context(), s.DB)
		res, err := ListBundles(r.Context(), s.DB, listOpts)
		if err != nil {
			reply(w, 500, map[string]any{"detail": err.Error()})
			return
		}
		reply(w, 200, res)
	})
	mux.HandleFunc("GET /api/v1/bundles/follows", func(w http.ResponseWriter, r *http.Request) {
		ids, err := s.FollowedBundleIDs(r.Context())
		if err != nil {
			reply(w, 500, map[string]any{"detail": err.Error()})
			return
		}
		reply(w, 200, map[string]any{"bundleIds": ids})
	})
	mux.HandleFunc("PUT /api/v1/bundles/{bundle_id}/follow", func(w http.ResponseWriter, r *http.Request) {
		id, ok := pathBundleID(w, r)
		if !ok {
			return
		}
		res, err := s.FollowBundle(r.Context(), id)
		replyLifecycle(w, res, err)
	})
	mux.HandleFunc("DELETE /api/v1/bundles/{bundle_id}/follow", func(w http.ResponseWriter, r *http.Request) {
		id, ok := pathBundleID(w, r)
		if !ok {
			return
		}
		res, err := s.UnfollowBundle(r.Context(), id)
		replyLifecycle(w, res, err)
	})
	mux.HandleFunc("POST /api/v1/bundles/{bundle_id}/remove", func(w http.ResponseWriter, r *http.Request) {
		id, ok := pathBundleID(w, r)
		if !ok {
			return
		}
		res, err := s.RemoveBundle(r.Context(), id)
		if err != nil {
			var missing *ErrBundleNotFound
			if errors.As(err, &missing) {
				reply(w, 404, map[string]any{"detail": err.Error()})
				return
			}
			reply(w, 500, map[string]any{"detail": err.Error()})
			return
		}
		reply(w, 200, res)
	})
	mux.HandleFunc("POST /api/v1/bundles/{bundle_id}/restore", func(w http.ResponseWriter, r *http.Request) {
		id, ok := pathBundleID(w, r)
		if !ok {
			return
		}
		res, err := s.RestoreBundle(r.Context(), id)
		replyLifecycle(w, res, err)
	})
	mux.HandleFunc("POST /api/v1/bundles/refresh", func(w http.ResponseWriter, r *http.Request) {
		body := func() (map[string]any, error) {
			res, err := s.RefreshWithEnqueue(r.Context())
			if err != nil {
				return nil, err
			}
			return res, nil
		}
		var result map[string]any
		var err error
		if opts.Accounting != nil {
			result, err = opts.Accounting(r.Context(), "bundle_refresh", body)
		} else {
			result, err = body()
		}
		if err != nil {
			if errors.Is(err, ErrRegionsDisabled) || errors.Is(err, ErrUnrecognizedRef) || errors.Is(err, ErrImportFetchFailed) {
				reply(w, 400, map[string]any{"detail": err.Error()})
				return
			}
			reply(w, 500, map[string]any{"detail": err.Error()})
			return
		}
		reply(w, 200, result)
	})
	mux.HandleFunc("POST /api/v1/bundles/import", func(w http.ResponseWriter, r *http.Request) {
		text, err := importText(w, r)
		if err != nil {
			reply(w, 422, map[string]any{"detail": "捆绑包导入参数无效"})
			return
		}
		outcome, err := s.ImportBundle(r.Context(), text)
		if err != nil {
			if errors.Is(err, ErrUnrecognizedRef) || errors.Is(err, ErrImportFetchFailed) || errors.Is(err, ErrRegionsDisabled) {
				reply(w, 400, map[string]any{"detail": err.Error()})
				return
			}
			reply(w, 500, map[string]any{"detail": err.Error()})
			return
		}
		if !outcome.OK {
			reply(w, 200, map[string]any{"ok": false, "detail": outcome.Detail})
			return
		}
		reply(w, 200, map[string]any{"ok": true, "bundleId": outcome.BundleID, "kind": outcome.Kind,
			"existed": outcome.Existed, "regionPrices": outcome.RegionPrices, "name": outcome.Name})
	})
	mux.HandleFunc("GET /api/v1/bundles/{bundle_id}", func(w http.ResponseWriter, r *http.Request) {
		maintain(r.Context())
		id, ok := pathBundleID(w, r)
		if !ok {
			return
		}
		detail, err := GetBundleDetail(r.Context(), s.DB, id)
		if err != nil {
			reply(w, 500, map[string]any{"detail": err.Error()})
			return
		}
		if detail == nil {
			reply(w, 404, map[string]any{"detail": "bundle not found"})
			return
		}
		reply(w, 200, detail)
	})
}

func replyLifecycle(w http.ResponseWriter, res map[string]any, err error) {
	if err != nil {
		reply(w, 500, map[string]any{"detail": err.Error()})
		return
	}
	reply(w, 200, res)
}

func replyValidation(w http.ResponseWriter, issues []httpx.ValErr) {
	httpx.WriteValidationError(w, issues...)
}

func issue(loc []any, typ, msg string, input any) httpx.ValErr {
	return httpx.ValErr{Loc: loc, Type: typ, Msg: msg, Input: input}
}

const (
	msgFloatParsing = "Input should be a valid number, unable to parse string as a number"
	msgBoolParsing  = "Input should be a valid boolean, unable to interpret input"
)

func parseListOptions(r *http.Request) (ListOptions, []httpx.ValErr) {
	q := r.URL.Query()
	opts := ListOptions{Sort: "diff", RegionMode: "all", DiffType: "absolute"}
	var issues []httpx.ValErr
	if raw := strings.TrimSpace(q.Get("sort")); raw != "" {
		opts.Sort = raw
	}
	if raw := q.Get("offset"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil {
			issues = append(issues, issue([]any{"query", "offset"}, "int_parsing", httpx.IntParseMsg, raw))
		} else if value < 0 {
			issues = append(issues, issue([]any{"query", "offset"}, "greater_than_equal", "Input should be greater than or equal to 0", raw))
		} else {
			opts.Offset = value
		}
	}
	if raw := q.Get("limit"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil {
			issues = append(issues, issue([]any{"query", "limit"}, "int_parsing", httpx.IntParseMsg, raw))
		} else if value < 1 {
			issues = append(issues, issue([]any{"query", "limit"}, "greater_than_equal", "Input should be greater than or equal to 1", raw))
		} else if value > 200 {
			issues = append(issues, issue([]any{"query", "limit"}, "less_than_equal", "Input should be less than or equal to 200", raw))
		} else {
			opts.Limit = value
		}
	}
	for name, target := range map[string]*bool{
		"removed": &opts.Removed, "followed_only": &opts.FollowedOnly,
		"completable_only": &opts.CompletableOnly, "cn_lowest_only": &opts.CnLowestOnly,
		"only_discounted": &opts.OnlyDiscounted, "gift_only": &opts.GiftOnly,
		"hide_owned": &opts.HideOwned, "hide_family": &opts.HideFamily,
	} {
		value, ok := parseQueryBool(q.Get(name))
		if !ok {
			issues = append(issues, issue([]any{"query", name}, "bool_parsing", msgBoolParsing, q.Get(name)))
			continue
		}
		*target = value
	}
	opts.Query = q.Get("query")
	opts.Region = q.Get("region")
	if raw := q.Get("region_mode"); raw != "" {
		opts.RegionMode = raw
	}
	for name, target := range map[string]**float64{
		"price_min": &opts.PriceMin, "price_max": &opts.PriceMax,
		"diff_min": &opts.DiffMin, "diff_max": &opts.DiffMax,
	} {
		raw := q.Get(name)
		if raw == "" {
			continue
		}
		value, err := strconv.ParseFloat(raw, 64)
		if err != nil {
			issues = append(issues, issue([]any{"query", name}, "float_parsing", msgFloatParsing, raw))
			continue
		}
		if math.IsNaN(value) || math.IsInf(value, 0) {
			issues = append(issues, issue([]any{"query", name}, "finite_number", "Input should be a finite number", raw))
			continue
		}
		*target = &value
	}
	if raw := q.Get("diff_type"); raw != "" {
		opts.DiffType = raw
	}
	return opts, issues
}

func parseQueryBool(raw string) (bool, bool) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "":
		return false, true
	case "1", "true":
		return true, true
	case "0", "false":
		return false, true
	}
	return false, false
}

func pathBundleID(w http.ResponseWriter, r *http.Request) (int64, bool) {
	raw := r.PathValue("bundle_id")
	id, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		replyValidation(w, []httpx.ValErr{issue([]any{"path", "bundle_id"}, "int_parsing", httpx.IntParseMsg, raw)})
		return 0, false
	}
	return id, true
}

func primarySteamID(ctx context.Context, db *sql.DB) string {
	var sid string
	if err := db.QueryRowContext(ctx,
		"SELECT steam_id FROM steam_accounts ORDER BY bound_at, steam_id LIMIT 1").Scan(&sid); err == nil {
		return sid
	}
	var raw string
	if err := db.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key='account.steam_id'").Scan(&raw); err != nil {
		return ""
	}
	var value string
	if json.Unmarshal([]byte(raw), &value) == nil {
		return value
	}
	return ""
}

func importText(w http.ResponseWriter, r *http.Request) (string, error) {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
	decoder.UseNumber()
	var payload map[string]any
	if err := decoder.Decode(&payload); err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			return "", err
		}
		return "", nil
	}
	value, exists := payload["text"]
	if !exists || value == nil {
		return "", nil
	}
	switch typed := value.(type) {
	case string:
		return typed, nil
	case json.Number:
		return typed.String(), nil
	case bool:
		if typed {
			return "True", nil
		}
		return "False", nil
	default:
		raw, err := json.Marshal(value)
		if err != nil {
			return "", nil
		}
		return string(raw), nil
	}
}
