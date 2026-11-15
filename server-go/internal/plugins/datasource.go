package plugins

import (
	"fmt"
	"strings"
	"time"
)

// 静态数据源只供观测，商品身份与价格合流由核心决定。

// DataSourceData 数据包数据文件（observations.json）。
type DataSourceData struct {
	AppID        int64             `json:"appid"`
	Region       string            `json:"region"`
	Observations []DataSourcePoint `json:"observations"`
}

// DataSourcePoint 单条历史观测（日粒度）。
type DataSourcePoint struct {
	Date           string `json:"date"`                     // YYYY-MM-DD
	PriceMinor     *int64 `json:"priceMinor,omitempty"`     // 主单位 × 100，全币种统一
	CnyFen         *int64 `json:"cnyFen,omitempty"`         // 兼容字段；折算由核心按历史汇率计算
	ProductUnknown bool   `json:"productUnknown,omitempty"` // 商品身份未知 → 独立序列
}

// maxDataSourceData 数据文件字节上限（与安装包上限一致）。
const maxDataSourceData = 256 << 10

// maxDataSourcePoints 单包观测条目上限（对齐合流写入器单批上限）。
const maxDataSourcePoints = 5000

// ValidateDataSourceData 解析并校验数据文件原文（严格 JSON；未知字段拒绝）。
func ValidateDataSourceData(raw []byte) (*DataSourceData, error) {
	if len(raw) > maxDataSourceData {
		return nil, fmt.Errorf("数据文件 %d 字节超过上限 %d", len(raw), maxDataSourceData)
	}
	var data DataSourceData
	if e := strictJSON(raw, &data); e != nil {
		return nil, fmt.Errorf("数据文件不是合法的观测集")
	}
	if data.AppID <= 0 {
		return nil, fmt.Errorf("数据文件缺少有效的游戏编号")
	}
	data.Region = strings.ToLower(strings.TrimSpace(data.Region))
	if data.Region == "" || len(data.Region) > 5 || strings.ToLower(data.Region) != data.Region {
		return nil, fmt.Errorf("数据文件地区码不合法")
	}
	if len(data.Observations) == 0 {
		return nil, fmt.Errorf("数据文件不包含任何观测")
	}
	if len(data.Observations) > maxDataSourcePoints {
		return nil, fmt.Errorf("数据文件观测 %d 条超过上限 %d", len(data.Observations), maxDataSourcePoints)
	}
	seen := map[string]struct{}{}
	beijing := time.FixedZone("UTC+8", 8*3600)
	now := time.Now().In(beijing)
	today := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, beijing)
	for i := range data.Observations {
		point := &data.Observations[i]
		if _, dup := seen[point.Date]; dup {
			return nil, fmt.Errorf("观测日期 %s 重复", point.Date)
		}
		seen[point.Date] = struct{}{}
		day, e := time.ParseInLocation("2006-01-02", point.Date, beijing)
		if e != nil {
			return nil, fmt.Errorf("观测日期 %s 不是 YYYY-MM-DD", point.Date)
		}
		if day.After(today) {
			return nil, fmt.Errorf("观测日期 %s 晚于当前时间", point.Date)
		}
		if point.PriceMinor == nil {
			return nil, fmt.Errorf("观测 %s 缺少本币价格", point.Date)
		}
		if point.PriceMinor != nil && *point.PriceMinor <= 0 {
			return nil, fmt.Errorf("观测 %s 价格必须为正数", point.Date)
		}
		if point.CnyFen != nil && *point.CnyFen <= 0 {
			return nil, fmt.Errorf("观测 %s 折算价必须为正数", point.Date)
		}
	}
	return &data, nil
}

// DataSourceMember 数据源包的数据成员名。
const DataSourceMember = "observations.json"

// validateDataSourceManifest 数据源 kind 的清单约束：无网络投递面。
func (m Manifest) validateDataSourceManifest() error {
	if m.Endpoint != "" || len(m.Domains) != 0 || len(m.Headers) != 0 || m.Body != nil {
		return fmt.Errorf("数据源插件不携带网络投递面")
	}
	if len(m.ConfigSchema.Properties) != 0 || len(m.ConfigSchema.Required) != 0 {
		return fmt.Errorf("数据源插件不接受配置项")
	}
	return nil
}
