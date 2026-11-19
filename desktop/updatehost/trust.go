//go:build windows

package main

import "holdexar/hostkit"

func versionGT(first, second string) bool {
	return hostkit.VersionGT(first, second)
}

func verifyStagedTrust(stagingDir, _ string, targetVersion string) error {
	return hostkit.VerifyStagedTrust(stagingDir, targetVersion)
}
