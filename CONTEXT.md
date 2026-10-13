# Context

Kamus/Glossary domain arsitektur untuk `agy-switch`. Semua penamaan kode, fungsi, dan tiket harus merujuk pada istilah berikut.

- **Vault**: Direktori lokal yang diproteksi ketat (`~/.antigravity-vault/`) untuk menyimpan token OAuth mentah. *Avoid:* store, db, secret folder.
- **QuotaClient**: Modul klien HTTP super cepat berbasis `urllib.request` murni yang melakukan hit ke internal Google CloudCode API. *Avoid:* CLI fetcher, quota scraper.
- **RotationEngine**: Modul pembuat keputusan (heuristik) yang menentukan akun mana yang paling optimal untuk diputar, menggunakan algoritma `cache_optimized`, `drain_window`, atau `auto_highest`. *Avoid:* AutoSwitcher, brain.
- **Fast Telemetry**: Mekanisme akuisisi metrik (seperti kuota sisa) secara paralel/caching tanpa harus bergantung pada subprocess (seperti `agy -p /usage`).
- **Atomic Swap**: Pemindahan/overwrite kredensial dari vault ke target CLI/Desktop (misal `os.replace`) yang menjamin state tidak akan pernah corrupt.
- **Unified Bootstrap**: Kemampuan `install.sh` mendeteksi eksekusi tanpa clone lokal (seperti via pipe `curl | bash`), otomatis meng-clone ke direktori home (`~/agy-switcher`), dan mengeksekusi instalasi global. *Avoid:* auto-installer, curl script, remote runner.
- **Home Repository Layout**: Pola peletakan folder repositori standar di root direktori user (`~/agy-switcher`) untuk kemudahan navigasi manual dan pembaruan via `git pull`. *Avoid:* custom path, deep workspace root.
