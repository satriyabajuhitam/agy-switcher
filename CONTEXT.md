# Context

Kamus/Glossary domain arsitektur untuk `agy-switch`. Semua penamaan kode, fungsi, dan tiket harus merujuk pada istilah berikut.

- **Vault**: Direktori lokal yang diproteksi ketat (`~/.antigravity-vault/`) untuk menyimpan token OAuth mentah. *Avoid:* store, db, secret folder.
- **QuotaClient**: Modul klien HTTP super cepat berbasis `urllib.request` murni yang melakukan hit ke internal Google CloudCode API. *Avoid:* CLI fetcher, quota scraper.
- **RotationEngine**: Modul pembuat keputusan (heuristik) yang menentukan akun mana yang paling optimal untuk diputar, menggunakan algoritma `cache_optimized`, `drain_window`, atau `auto_highest`. *Avoid:* AutoSwitcher, brain.
- **Fast Telemetry**: Mekanisme akuisisi metrik (seperti kuota sisa) secara paralel/caching tanpa harus bergantung pada subprocess (seperti `agy -p /usage`).
- **Atomic Swap**: Pemindahan/overwrite kredensial dari vault ke target CLI/Desktop (misal `os.replace`) yang menjamin state tidak akan pernah corrupt.
- **Unified Bootstrap**: Kemampuan `install.sh` mendeteksi eksekusi tanpa clone lokal (seperti via pipe `curl | bash`), otomatis meng-clone ke direktori home (`~/agy-switcher`), dan mengeksekusi instalasi global. *Avoid:* auto-installer, curl script, remote runner.
- **Home Repository Layout**: Pola peletakan folder repositori standar di root direktori user (`~/agy-switcher`) untuk kemudahan navigasi manual dan pembaruan via `git pull`. *Avoid:* custom path, deep workspace root.
- **Hybrid Quota Schema**: Skema struktur data telemetri kuota yang menggabungkan nilai worst-case agregat di level atas (`quota`, `reset_time`) dengan rincian per-jendela (`buckets: {"5h": ..., "weekly": ...}`) untuk menjamin kompatibilitas mundur. *Avoid:* dual schema, nested-only quota, flat schema.
- **Listicle Formatter**: Format presentasi CLI berbasis bullet list ringkas satu-baris non-tabel yang dioptimalkan untuk konsumsi terminal compact dan prompt agen AI. *Avoid:* table view, multiline block format, raw dump.
- **Tiered Cache-First Polling**: Pola evaluasi telemetri kuota 3-tingkat pada `PreInvocation` hook (0ms pass untuk kuota sehat $\ge 25\%$, single probe akun aktif saat cache expired atau kuota menengah $15\%-24\%$, dan full probe standby saat kuota kritis $< 15\%$) untuk memangkas overhead turn agen tanpa mengorbankan ketepatan auto-rotasi. *Avoid:* greedy polling, blind rotation, turn-blocking probe.
- **Account-Level Cache Freshness**: Pelacakan usia cache secara independen per akun via timestamp `cached_at` di dalam struktur `last_known_quota.json`, memungkinkan validasi TTL parsial saat single probe tanpa membatalkan cache akun standby yang masih valid. *Avoid:* whole-cache invalidation, global mtime freshness.

