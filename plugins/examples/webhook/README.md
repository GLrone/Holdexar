# HTTPS notification plugin

This package contains data only. Holdexar validates its declaration and sends the JSON request through its notification service. It does not load Python, JavaScript, shell commands or executable files from the package.

Replace the example hostname and fixed endpoint with a service you control, and adapt the body to that service's documented API. The example hostname is a placeholder and does not send notifications. Keep the endpoint hostname in `domains`. Credentials belong in password configuration fields and must not be embedded in the package.

Run `python test_pack.py` to check packaging, then `python pack.py example.webhook.hlx`. The command prints the archive's SHA-256. Existing output files are not overwritten. Install the archive from Settings → Plugins, enter the service token, enable the plugin, and send a test notification. Enabling it selects that notification channel.

The archive contains exactly one regular file, `manifest.json`, and must be at most 256 KiB. The manifest must be at most 64 KiB. API version `1` supports `notification.webhook` with a fixed HTTPS endpoint, exact domain declarations, JSON body templates and `Authorization` or `X-*` headers. Templates support `{{subject}}`, `{{body}}` and `{{config.field}}` in string values; JSON keys remain literal. Configuration supports string, number and boolean properties, optional `enum`, `default`, `title`, `description`, and string `format: "password"`. Unknown configuration fields are rejected.

To offer an index, serve HTTPS JSON with this structure and the archive at its declared URL:

```json
{"apiVersion":1,"plugins":[{"manifest":{},"sha256":"ARCHIVE_SHA256","url":"https://your-domain.example/plugin.hlx"}]}
```

Replace `manifest` with the entire packaged declaration. A checksum verifies archive integrity against that index; it does not authenticate the author. These packages are unsigned. Users choose the index source explicitly. Private addresses, redirects and downloads outside the size limit are rejected. Uninstalling a plugin preserves existing business history.
