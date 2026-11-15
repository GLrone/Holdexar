//go:build windows

// 生成绑定的固定地真测试：槽位期望值逐条抄自官方 WebView2.h（COM 接口一经
// 发布不可变，这组数字是外部锚点）。生成器或人工误改 com_generated.go 时，
// 本测试先红于任何运行时崩溃。
package main

import "testing"

func TestGeneratedSlotGroundTruth(t *testing.T) {
	pins := []struct {
		name string
		got  int
		want int
	}{
		{"ICoreWebView2.add_NavigationStarting", slotICoreWebView2AddNavigationStarting, 7},
		{"ICoreWebView2.add_NewWindowRequested", slotICoreWebView2AddNewWindowRequested, 44},
		{"NavigationStarting.get_Uri", slotICoreWebView2NavigationStartingEventArgsUri, 3},
		{"NavigationStarting.get_IsUserInitiated", slotICoreWebView2NavigationStartingEventArgsIsUserInitiated, 4},
		{"NavigationStarting.get_IsRedirected", slotICoreWebView2NavigationStartingEventArgsIsRedirected, 5},
		{"NavigationStarting.get_Cancel", slotICoreWebView2NavigationStartingEventArgsCanceled, 7},
		{"NavigationStarting.put_Cancel", slotICoreWebView2NavigationStartingEventArgsSetCancel, 8},
		{"NavigationStarting.get_NavigationId", slotICoreWebView2NavigationStartingEventArgsNavigationId, 9},
		{"NewWindow.get_Uri", slotICoreWebView2NewWindowRequestedEventArgsUri, 3},
		{"NewWindow.get_Handled", slotICoreWebView2NewWindowRequestedEventArgsHandled, 7},
		{"NewWindow.put_Handled", slotICoreWebView2NewWindowRequestedEventArgsSetHandled, 6},
		{"NewWindow.get_IsUserInitiated", slotICoreWebView2NewWindowRequestedEventArgsIsUserInitiated, 8},
		{"ProcessFailed.get_ProcessFailedKind", slotICoreWebView2ProcessFailedEventArgsProcessFailedKind, 3},
	}
	for _, p := range pins {
		if p.got != p.want {
			t.Errorf("%s 槽位=%d，官方应为 %d", p.name, p.got, p.want)
		}
	}
}

// TestGeneratedSlotsTable 产物表与常量同源一致：把关键方法在表中的位置
// （槽 = 3 + 序号）再对一遍，防表和常量单边漂移。
func TestGeneratedSlotsTable(t *testing.T) {
	check := func(iface string, slot int, method string) {
		t.Helper()
		rows, ok := generatedSlots[iface]
		if !ok {
			t.Fatalf("generatedSlots 缺接口 %s", iface)
		}
		idx := slot - 3
		if idx < 0 || idx >= len(rows) {
			t.Fatalf("%s.%s 槽 %d 越出表（表长 %d）", iface, method, slot, len(rows))
		}
		if rows[idx] != method {
			t.Fatalf("%s 槽 %d 应为 %s，表中实为 %s", iface, slot, method, rows[idx])
		}
	}
	check("ICoreWebView2", slotICoreWebView2AddNavigationStarting, "m:add_NavigationStarting")
	check("ICoreWebView2", slotICoreWebView2AddNewWindowRequested, "m:add_NewWindowRequested")
	check("ICoreWebView2NavigationStartingEventArgs", slotICoreWebView2NavigationStartingEventArgsSetCancel, "propput:put_Cancel")
	check("ICoreWebView2NavigationStartingEventArgs", slotICoreWebView2NavigationStartingEventArgsCanceled, "propget:get_Cancel")
	check("ICoreWebView2NewWindowRequestedEventArgs", slotICoreWebView2NewWindowRequestedEventArgsSetHandled, "propput:put_Handled")
	check("ICoreWebView2ProcessFailedEventArgs", slotICoreWebView2ProcessFailedEventArgsProcessFailedKind, "propget:get_ProcessFailedKind")
}
