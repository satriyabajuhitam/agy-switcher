# 17. GitHub Bootstrap Resilience, Daemon Mutual Exclusion, and Test Vault Isolation

Date: 2026-11-12

## Status

Accepted

## Context

Investigasi empiris terhadap runtime `agy-switch` pada mesin yang dipasang dari repositori GitHub mengungkap empat kejanggalan arsitektural yang berpotensi dialami oleh pengguna baru:

1. **Dual Watcher Daemon & Race Condition**:
   Proses daemon `watch run` dapat dijalankan berulang kali (misalnya satu melalui `systemd` user unit `agy-switch-watcher.service` dan lainnya via eksekusi terminal `agy-switch watch start`). Keduanya men-tail file log sesi yang sama (`transcript.jsonl`), menimpa file `watch.pid`, dan berebut (*race condition*) merotasi akun pada saat yang bersamaan ketika terjadi error HTTP 429 atau kuota habis.
2. **Polusi Log Produksi oleh Unit Test (Test Tainting)**:
   File log produksi `~/.antigravity-vault/watch.log` membengkak hingga ratusan kilobyte dan ribuan baris akibat pengujian unit (`verify-build.sh` / `test_watch.py`). Mock engine pada test suite yang tidak menyetel atribut `vault_dir` secara eksplisit jatuh kembali (*fallback*) ke `DEFAULT_VAULT_DIR` (`~/.antigravity-vault`), sehingga ratusan pergantian akun fiktif (`primary-acc`, `acc1`, `acc2`) tertulis langsung ke log riil pengguna.
3. **Fragmentasi Jalur Eksekusi & Ketiadaan Canonical Launcher**:
   Skrip instalasi `install.sh` saat ini hanya mengelola direktori plugin dan symlink `~/.local/bin/agy-switch`, namun belum mengotomatisasi registrasi lifecycle hook Antigravity (`hooks.json`) maupun service `systemd`. Selain itu, hook manual rentan didaftarkan menunjuk langsung ke path absolut klon lokal (misal `/srv/...` atau `/home/...`), menyebabkan path tidak valid atau inkonsisten ketika direktori workspace berpindah atau diperbarui.
4. **False Alarm Kedaluwarsa Token pada Akun Sekunder di `doctor`**:
   Diagnostik Layer 2 pada `agy-switch doctor` hanya memeriksa timestamp kedaluwarsa `access_token` yang secara alami kedaluwarsa setelah 1 jam. Hal ini menyebabkan belasan akun cadangan yang sedang dalam status *standby* dilaporkan berstatus `[WARN] Expired`, padahal seluruh akun tersebut memiliki `refresh_token` Google OAuth yang valid dan berhasil di-refresh otomatis saat request dijalankan.

---

## Decision

Kami memutuskan untuk mengadopsi empat pilar arsitektur perbaikan guna menjamin keandalan instalasi dan operasi pengguna GitHub:

### 1. Daemon Mutual Exclusion & Systemd Service Detection
- **Cross-Process File Lock**: `scripts/agy_switcher/watcher.py` wajib menerapkan mekanisme penguncian non-blocking (`fcntl.flock` pada `~/.antigravity-vault/watch.lock`). Jika sebuah instance watcher sedang berjalan, upaya menjalankan instance baru akan ditolak dengan pesan kesalahan yang jelas dan kode keluar non-zero.
- **Deteksi Layanan Systemd**: Perintah `agy-switch watch start` dan `watch run` harus memeriksa status layanan `systemctl --user is-active agy-switch-watcher.service`. Jika layanan systemd aktif, perintah interaktif dilarang memunculkan proses ganda dan wajib mengarahkan pengguna untuk mengelola daemon melalui `systemctl --user` atau perintah terpadu `agy-switch watch stop/start`.

### 2. Strict Mock Vault Isolation pada Pengujian
- **Eliminasi Fallback ke Vault Produksi**: Seluruh modul penulisan audit (`append_secure_log_entry`) dan inisialisasi `LogWatcher` dilarang keras jatuh kembali ke direktori produksi `~/.antigravity-vault` jika `engine.vault_dir` bernilai `None`, tidak valid, atau bertipe mock (`MagicMock`).
- **Pemberian Jalur Isolasi Eksplisit**: Eksekusi test suite dan build gate (`verify-build.sh`) harus mengharuskan atau secara otomatis mengisolasi vault path ke direktori sementara (*ephemeral temporary directory*) menggunakan environment variable `AGY_VAULT_DIR` atau fixture test `tempfile.TemporaryDirectory`.

### 3. End-to-End Bootstrap Installer & Canonical Launcher
- **Penyelarasan Canonical Launcher**: Seluruh integrasi eksternal—termasuk `hooks.json` (`PreInvocation`) dan unit systemd `agy-switch-watcher.service`—wajib menunjuk secara eksklusif ke satu jalur kanonik: `~/.local/bin/agy-switch` (bukan skrip Python di direktori klon lokal).
- **Alur Instalasi Satu Langkah**: Memperbarui `install.sh` untuk menyediakan opsi instalasi lengkap (*all-in-one setup*) yang mencakup:
  1. Instalasi/symlink plugin ke direktori tujuan (`--global` atau `--local`).
  2. Pembuatan executable symlink kanonik `~/.local/bin/agy-switch`.
  3. Konfigurasi otomatis lifecycle hook `PreInvocation` di `~/.gemini/config/hooks.json`.
  4. Pemasangan dan pengaktifan layanan latar belakang `systemd` user service (`agy-switch-watcher.service`).

### 4. Refresh-Aware Standby Validation pada Diagnostik `doctor`
- **Pemisahan Akun Primer vs Sekunder**:
  - Untuk **Akun Aktif (Primary)**: `token.expiry` tetap diaudit secara ketat. Jika access token aktif kedaluwarsa, status tetap `[FAIL]`.
  - Untuk **Akun Cadangan (Secondary / Standby)**: Pemeriksaan dialihkan untuk memverifikasi keberadaan dan validitas field `token.refresh_token` (non-empty string). Jika access token telah lewat masa 1 jam tetapi `refresh_token` tersedia, akun diklasifikasikan sebagai `[PASS]` (Standby Ready).
- **Peringatan Spesifik**: Status `[WARN]` atau `[FAIL]` pada akun sekunder hanya dimunculkan jika `refresh_token` tidak ditemukan, kredensial JSON rusak, atau payload JWT tidak dapat diekstrak.

---

## Consequences

### Positif:
- **Zero Race Condition**: Terjaminnya eksekusi tunggal daemon watcher mencegah konflik saat eksekusi failover kuota 429.
- **Log Produksi Higienis**: File `watch.log` bersih dari polusi mock switch akun fiktif dari unit test.
- **Pengalaman Pengguna GitHub yang Mulus**: Pengguna yang melakukan instalasi dari repositori GitHub mendapatkan setup otomatis yang langsung bekerja (*ready to run*) tanpa kebingungan konfigurasi hook dan systemd terpisah.
- **Eliminasi False Alarm**: Diagnostik `doctor` memberikan laporan status kredensial yang akurat tanpa meminta pengguna melakukan login ulang yang tidak perlu.

### Negatif / Mitigasi:
- *Dependensi fcntl*: Kebutuhan `fcntl.flock` spesifik untuk lingkungan POSIX/Linux, yang selaras dengan target platform Antigravity CLI dan WSL (didukung secara bawaan oleh pustaka standar Python).
- *Manajemen Kunci Stale*: Jika mesin mati mendadak (*ungraceful power-off*), lock berbasis kernel OS (`flock`) secara otomatis dilepaskan oleh sistem operasi sehingga tidak akan meninggalkan *deadlock* pada startup berikutnya.
