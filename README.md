# Hollister Restock Watcher

Runs on GitHub Actions every ~5 minutes and sends a WhatsApp message (CallMeBot) when a watched Hollister product comes back in stock.

- Products and sizes: edit `config.json`.
- Phone numbers and API keys: stored in the repository secret `NOTIFY_SECRETS` (never in the code).
- Test message: Actions tab -> Hollister restock check -> Run workflow -> tick "test".
