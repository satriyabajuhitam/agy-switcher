# 22. Vault-Authoritative Sync, Global Quiescence, and Continuous Drift Guard

Date: 2026-11-26

## Status

Accepted (Amends ADR 0016 and ADR 0021)

## Context

Investigasi empiris terhadap ketidaksinkronan (*split-brain*) antara Antigravity Desktop (`~/.gemini/jetski-standalone-oauth-token` + memori `language_server`) dan Antigravity CLI / `Vault` (`~/.gemini/antigravity-cli/antigravity-oauth-token` + `~/.antigravity-vault/active`) mengungkap empat celah struktural yang saling menguatkan:

1. **Zombie Write-Back oleh `language_server`**:
   Ketika perpindahan akun terjadi tanpa me-restart `language_server`, proses Go `language_server` tetap memegang kredensial akun lama di RAM. Saat *access token* lama kedaluwarsa (~1 jam), `language_server` me-refresh token akun lama dan menulisnya kembali ke `jetski-standalone-oauth-token` (dilengkapi `writer_id` dan format `expiry` nanodetik Go), menimpa akun aktif yang ditulis `agy-switch`. Pada restart server berikutnya, Desktop kembali memuat akun lama tersebut sehingga desinkronisasi menjadi permanen.
2. **False Positive Permanen pada `ProcessSupervisor.is_server_busy()`**:
   `is_server_busy()` menandai server sibuk apabila terdapat file descriptor terbuka menuju `presence/*.lock` atau proses anak non-zombie di bawah `language_server`. Di runtime Antigravity 2.0, `language_server` **selalu** memegang fd `.lock` untuk tab percakapan yang terbuka dan menjalankan proses anak permanen `sidecar_sdk` (Node.js). Akibatnya `_is_turn_quiescent()` selalu mengembalikan `False`, membuat `pending_reload.json` (TTL 120 detik) dan *Proactive Threshold Failover* (ADR 0016) tidak pernah tereksekusi.
3. **Deteksi Langkah Idle yang Tidak Akurat pada `transcript.jsonl`**:
   `_is_turn_quiescent()` sebelumnya menganggap `type == "USER_INPUT"` (prompt pengguna baru masuk) dan `status == "DONE"` pada `type == "GENERIC"` (hasil tool baru kembali) sebagai kondisi idle, padahal keduanya terjadi di tengah turn aktif. Langkah idle sejati pada transkrip adalah `type == "PLANNER_RESPONSE"` dengan `status == "DONE"` **tanpa** `tool_calls`.
4. **Celah Konvergensi pada Jalur Auto-Switch & Visibilitas CLI**:
   `PreInvocation` hook (`hook.py`) dan In-Process SDK (`api.py`) melakukan *Disk-Only Swap* (`restart_server=False`) tanpa pernah menjadwalkan `pending_reload.json`. Selain itu, `agy-switch status` dan `agy-switch list` hanya membaca pointer `Vault` tanpa memeriksa maupun memperingatkan adanya desinkronisasi terhadap berkas token Desktop/CLI.

## Decision

Kami menetapkan arsitektur sinkronisasi Desktop–CLI yang *always-convergent* dan *self-healing* melalui lima pilar terpadu:

### 1. Vault-Authoritative Sync (Single Source of Truth)
- `Vault` (`~/.antigravity-vault/active` beserta `accounts/<alias>/token.json`) ditetapkan sebagai satu-satunya otoritas akun aktif (*Strict `agy-switch` Workflow*).
- Tidak ada mekanisme *auto-adopt* atau sinkronisasi dua arah dari UI Profile Desktop. Segala penyimpangan identitas kredensial pada `jetski-standalone-oauth-token` maupun `antigravity-oauth-token` terhadap `Vault` diklasifikasikan sebagai *drift* (termasuk *Zombie Write-Back*) dan disembuhkan kembali mengikuti `Vault`.
- Perbaikan manual on-demand tetap menggunakan permukaan CLI eksisting yang ringkas (`agy-switch switch <alias>` dan `agy-switch doctor --fix` yang juga menjadwalkan reload saat token Desktop diperbaiki), tanpa menambah subcommand `adopt-desktop` baru.

### 2. Global Quiescence untuk Deteksi Safe Turn Boundary (Amends ADR 0016)
- `ProcessSupervisor` mengecualikan proses anak sidecar permanen (`sidecar_sdk`) dari penentuan kesibukan proses anak `language_server`, sehingga hanya proses eksekusi turn nyata (misalnya sub-shell perintah tool) yang menandai proses anak sibuk.
- Penentuan *Safe Turn Boundary* pada `LogWatcher` menerapkan invarian **Global Quiescence**:
  - Memeriksa **seluruh** sesi percakapan aktif (berdasarkan seluruh `presence/*.lock` yang sedang dipegang oleh fd proses hidup).
  - Sebuah sesi dinyatakan idle **hanya** apabila catatan terakhir pada `transcript.jsonl`-nya adalah `type == "PLANNER_RESPONSE"` dengan `status == "DONE"` dan **tidak memiliki `tool_calls`** (menolak `USER_INPUT`, `GENERIC`, `IN_PROGRESS`/`RUNNING`/`STREAMING`, maupun `PLANNER_RESPONSE` dengan `tool_calls` sebagai mid-turn).
  - Jika tidak ada sesi/presence lock aktif dan tidak ada proses anak non-sidecar di bawah `language_server`, sistem dinyatakan quiescent.
  - Reload `language_server` hanya dieksekusi saat **seluruh** tab aktif quiescent dan tidak ada proses anak non-sidecar yang berjalan; override darurat tetap tersedia lewat `--force-restart-now`.

### 3. Always-Converge Deferred Safe-Turn Reload di Semua Jalur Switch (Amends ADR 0021)
- Memperluas TTL `cache/pending_reload.json` dari `120 detik` menjadi **`1800 detik` (30 menit)** agar tahan terhadap turn agen berdurasi panjang tanpa kedaluwarsa prematur.
- Menjadwalkan `pending_reload.json` pada seluruh jalur switch yang tidak langsung me-restart `language_server`:
  - Manual/auto CLI switch in-turn (descendant `language_server`).
  - `PreInvocation` hook (`hook-pre-invocation`) pasca-rotasi.
  - In-Process SDK (`api.py`: `auto_rotate_on_exhaustion` dan `switch_account` saat `reload_server=False`), menjaga eksekusi non-blocking bagi consumer (`agy-telemote`) sekaligus menjamin konvergensi Desktop.
  - Satu-satunya opt-out eksplisit yang menekan penjadwalan `pending_reload.json` adalah flag CLI `--no-restart`.
- **Pre/Post-Restart Re-Sync Guard**: Tepat sebelum dan sesudah `LogWatcher` mengeksekusi restart `language_server` untuk `pending_reload.json`, daemon memverifikasi dan menuliskan ulang *Atomic Swap* dari akun aktif `Vault` terkini ke Desktop & CLI guna menutup *race condition* penulisan saat shutdown server.

### 4. Continuous Drift Guard di Dalam `LogWatcher`
- `LogWatcher.poll_once()` menjalankan pemeriksaan integritas lokal ringan (tanpa panggilan HTTP, dengan throttle ringan agar nol overhead I/O) yang membandingkan identitas kredensial (refresh token / email / auth_method) pada Desktop token dan CLI token terhadap akun aktif `Vault`.
- Apabila *drift* terdeteksi:
  - Otomatis menyembuhkan berkas menyimpang via *Atomic Swap* dari `Vault`.
  - Apabila berkas Desktop (`jetski-standalone-oauth-token`) yang menyimpang dan `language_server` sedang berjalan, otomatis menjadwalkan `pending_reload.json` (`reason="desktop_drift_guard"`) agar memori Desktop ikut direload pada *Global Quiescence* berikutnya.
  - Mencatat peristiwa pemulihan drift ke `watch.log` (mode `0600`).

### 5. Visibilitas Desync pada `status` dan `list`
- `agy-switch status` dan `agy-switch list` memverifikasi kesesuaian identitas token Desktop & CLI terhadap `Vault` secara lokal (tanpa HTTP) dan menampilkan peringatan desinkronisasi eksplisit (menyebutkan target mana yang menyimpang dan apakah *Pending Reload* sedang mengantre) alih-alih hanya menampilkan isi pointer `Vault`.

## Considered Options & Rejected Alternatives

- **Bidirectional / Desktop-Follow Sync**: Ditolak karena `language_server` menuliskan `writer_id` dan `mtime` baru baik saat login manual di UI maupun saat *Zombie Write-Back*; adopsi otomatis akan menyebabkan auto-switch ter-rollback diam-diam ke akun lama.
- **Menambah Subcommand `agy-switch sync --from-desktop` / `adopt-desktop`**: Ditolak demi menjaga permukaan CLI minimal (*deep module*) dan konsistensi alur kerja tunggal melalui `agy-switch login` / `switch` / `doctor --fix`.
- **Single-Tab Quiescence**: Ditolak karena semua tab berbagi satu proses `language_server`; me-restart server saat tab lain masih menjalankan turn aktif akan memutus pekerjaan tab lain dan melanggar *Active Turn Safety*.
- **Immediate Force-Kill di Hook / In-Turn Switch**: Ditolak karena menimbulkan *mid-turn parricide* (ADR 0021).
- **Mempertahankan TTL `pending_reload.json` 120 Detik**: Ditolak karena turn agen kompleks rutin melampaui 2 menit, menyebabkan reload gagal terpicu dan memicu siklus *Zombie Write-Back*.

## Consequences

**Positif:**
- Menutup tuntas akar masalah *Zombie Write-Back* sehingga Desktop (`language_server`) dan CLI selalu konvergen baik pada manual switch, `switch --auto`, `PreInvocation` hook, `LogWatcher`, maupun In-Process SDK.
- Mengaktifkan kembali mekanisme *Deferred Safe-Turn Reload* dan *Proactive Threshold Failover* (ADR 0016) yang sebelumnya terblokir oleh false positive `sidecar_sdk` dan open presence lock.
- Memberikan proteksi multi-tab penuh (*Global Quiescence*) dan transparansi langsung di `status` / `list`.

**Negatif / Mitigasi:**
- *Pengguna yang Login Langsung di Desktop UI akan Di-revert oleh Drift Guard*: Dimitigasi dengan konvensi workflow tunggal via `agy-switch login`/`switch` dan catatan log eksplisit di `watch.log` serta peringatan di `status`.
- *Reload Tertunda Saat Tab Lain Sibuk*: Pada skenario multi-tab sibuk berkepanjangan, reload menunggu seluruh tab selesai (hingga batas 30 menit); dimitigasi dengan indikator `Pending Reload` di `status` dan override eksplisit `--force-restart-now` bila operator ingin memaksa reload seketika.
