package main

import (
	"context"
	"fmt"
	"strings"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/plugins"
)

type pluginHistorySource struct{ manager *plugins.Manager }

func (s *pluginHistorySource) Available(ctx context.Context) (bool, error) {
	entry, _, err := s.manager.ActiveDataSource(ctx)
	return entry != nil, err
}

func (s *pluginHistorySource) Fetch(ctx context.Context, appid int64, region string) ([]dbwriter.CommunityObservation, error) {
	entry, data, err := s.manager.ActiveDataSource(ctx)
	if err != nil {
		return nil, err
	}
	if entry == nil {
		return nil, games.ErrHistorySourceUnavailable
	}
	if data.AppID != appid || !strings.EqualFold(data.Region, region) {
		return nil, nil
	}
	currency := notifications.CurrencyForRegion(strings.ToUpper(data.Region))
	if currency == "" {
		return nil, fmt.Errorf("历史数据地区没有已知币种")
	}
	rows := make([]dbwriter.CommunityObservation, 0, len(data.Observations))
	for _, point := range data.Observations {
		at, err := time.ParseInLocation("2006-01-02", point.Date, time.FixedZone("UTC+8", 8*3600))
		if err != nil {
			return nil, fmt.Errorf("历史观测日期无效")
		}
		// 静态数据包没有可核验的商品身份或汇率证据。
		rows = append(rows, dbwriter.CommunityObservation{SnapshotAt: at, PriceMinor: point.PriceMinor, Currency: currency, ProductUnknown: true, Source: "plugin:" + entry.Manifest.ID})
	}
	return rows, nil
}
