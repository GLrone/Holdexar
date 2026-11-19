module holdexar/windowhost

go 1.27.0

require (
	github.com/wailsapp/go-webview2 v1.0.23
	golang.org/x/sys v0.49.0
	holdexar/hostkit v0.0.0
)

require github.com/jchv/go-winloader v0.0.0-20210711035445-715c2860da7e // indirect

replace holdexar/hostkit => ../../shared/hostkit
