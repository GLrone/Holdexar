package main

import (
    "encoding/json"
    "os"
)

func main() {
    image, err := os.Executable()
    if err != nil { os.Exit(91) }
    env := map[string]string{}
    for _, key := range []string{
        "HOLDEXAR_INSTALL_ROOT", "HOLDEXAR_DATA_DIR", "WEBVIEW2_USER_DATA_FOLDER",
        "HOLDEXAR_CONTRACT_SENTINEL", "HOLDEXAR_BACKGROUND", "HOLDEXAR_PORT", "HOLDEXAR_LOCK_PORT",
        "HOLDEXAR_UPDATE_ATTEMPT_ID", "HOLDEXAR_UPDATE_DATA_TOKEN", "HOLDEXAR_LAUNCHER_PID",
        "HOLDEXAR_DEV_URL", "HOLDEXAR_UPDATE_RESTART", "HOLDEXAR_PREV_PID",
    } { env[key] = os.Getenv(key) }
    payload, err := json.Marshal(map[string]any{"exe":image,"args":os.Args[1:],"env":env,"pid":os.Getpid()})
    if err != nil { os.Exit(92) }
    if err = os.WriteFile(os.Getenv("HOLDEXAR_CONTRACT_OBSERVED"), payload, 0600); err != nil { os.Exit(93) }
    os.Exit(37)
}
