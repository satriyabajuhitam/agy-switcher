# 5. Kontrak CLI Quota dan Skema Telemetri Hibrida

Date: 2026-10-14

## Status

Accepted

## Context

Pada roadmap `agy-switch` (Pilar 1: Penyelarasan Kontrak CLI & Telemetri Quota), teridentifikasi sejumlah celah fungsional dan ketidakkonsistenan kontrak CLI:
1. Subcommand `agy-switch quota` belum terdaftar di `argparse`, sehingga pemanggilan langsung menghasilkan error argument tidak dikenal, sementara pengguna dan skrip otomatisasi mengantisipasi perintah `agy-switch quota`.
2. Pengambilan kuota via `list --quota` meratakan (*flattening*) respon internal Google CloudCode API menjadi nilai tunggal persentase terendah (`quota`) dan waktu reset (`reset_time`). Struktur ini membuang informasi granular mengenai batas jendela geser 5 jam (`5h`) versus batas mingguan (`weekly`).
3. Opsi format listicle (`--format list`) pada `list` berstatus *dead parameter* karena implementasi selalu mengarahkan output ke tabel ASCII (`format_accounts_table`). Pada lingkungan terminal sempit atau prompt agen AI, format tabel lebar rentan terpotong dan sulit diparsing secara deterministik.
4. Urutan penampilan akun saat memiliki persentase kuota identik belum deterministik, dan informasi waktu reset disajikan sebagai timestamp mentah bukannya countdown waktu yang mudah dipahami.
5. Claude diproyeksikan akan segera mengalami depresiasi pada ekosistem Google Antigravity, sehingga pemeliharaan kolom dan bucket Claude menambah kompleksitas tanpa nilai jangka panjang.

## Decision

1. **Dedicated Subcommand `agy-switch quota` & Preservasi `list --quota`**
   - Mendaftarkan subcommand mandiri `quota` pada `argparse` yang secara implisit selalu mengeksekusi telemetri kuota konkuren (`fetch_all_accounts_quota`).
   - Mendukung opsi `--format [table|list]` (default: `table`) dan `--json`.
   - Tetap mempertahankan flag `--quota` pada `list` sebagai alias yang kompatibel ke belakang (*backward-compatible*).

2. **Skema Telemetri Hibrida (Hybrid Quota Schema)**
   - Memperkaya skema keluaran `QuotaClient.parse_quota_response()` tanpa mematahkan konsumen yang ada (`RotationEngine`, `status`, dan berkas cache `last_known_quota.json`).
   - Struktur data mempertahankan nilai worst-case agregat di top-level (`quota`, `reset_time`), disertai sub-kamus `buckets` yang memuat detail per-jendela:
     ```json
     {
       "gemini": {
         "quota": 80,
         "reset_time": 1791201600,
         "buckets": {
           "5h": {
             "quota": 80,
             "reset_time": 1791201600
           },
           "weekly": {
             "quota": 95,
             "reset_time": 1791633600
           }
         }
       }
     }
     ```

3. **Fokus Eksklusif Telemetri Gemini (Depresiasi Claude)**
   - Output representasi CLI (tabel dan listicle) difokuskan eksklusif pada model Gemini.
   - Claude diabaikan dari kolom visual untuk memangkas densitas tampilan dan mengantisipasi depresiasi upstream Antigravity.

4. **Listicle Formatter untuk `--format list`**
   - Menghidupkan parameter `--format list` dengan fungsi formatter baru `format_accounts_listicle()`.
   - Menggunakan format bullet ringkas satu-baris per akun yang mencakup:
     - Marker status aktif (`*` untuk akun aktif, spasi untuk standby).
     - Nama akun dan email.
     - Persentase kuota efektif (worst-case).
     - Rincian bucket `5h` dan `weekly` jika tersedia.
     - Countdown waktu reset relatif (contoh: `in 2h 15m`).

5. **Sorting Deterministik Berbasis 3-Tier Tie-Breaking**
   - Pengurutan akun pada `quota` dilakukan secara deterministik:
     1. Kuota efektif descending (sisa kuota terbanyak di posisi atas).
     2. Earliest reset countdown ascending (yang akan reset lebih cepat diprioritaskan jika kuota sama).
     3. Nama alias akun alfabetis ascending (menjamin output stabil tanpa acak antarpanggilan).

6. **Fallback Cache Bertingkat saat Kegagalan Jaringan**
   - Jika `fetch_account_quota` gagal (offline / timeout), sistem mencari data historis di `last_known_quota.json`.
   - Jika cache ditemukan, nilai kuota disajikan dengan penanda eksplisit `(Cached)`.
   - Jika cache tidak ada, akun ditandai secara tegas sebagai `Error / Unreachable`.

## Considered Options & Rejected Alternatives

- **Strict Hierarchical Schema (`{"gemini": {"5h": ..., "weekly": ...}}` tanpa top-level `quota`)**: Ditolak karena mematahkan kontrak `RotationEngine.get_quota_status()`, handler `PreInvocation` hook, dan test suite yang mengandalkan key `"quota"`.
- **Flat Enriched Schema (`quota_5h`, `quota_weekly` di top-level)**: Ditolak karena mengotori namespace dan kurang fleksibel jika Google menambahkan jenis bucket baru di masa depan.
- **Hapus Flag `--quota` dari `list`**: Ditolak untuk mencegah breaking change pada alur skrip pengguna atau plugin yang memanggil `agy-switch list --quota`.
- **Kolom Ganda Gemini dan Claude pada Tampilan**: Ditolak berdasarkan konfirmasi bahwa Claude mendekati fase depresiasi pada Antigravity.

## Consequences

**Positif:**
- Memenuhi kontrak perintah `agy-switch quota` yang intuitif bagi pengguna terminal dan agen AI.
- Kompatibilitas mundur penuh: modul `RotationEngine` tetap beroperasi tanpa perlu modifikasi struktural berisiko.
- Format listicle ramah konsumsi LLM prompt dan terminal mobile/compact tanpa gangguan line-wrap ASCII tabel.
- Tampilan offline yang informatif tanpa memunculkan false alarm 0% kuota.

**Negatif/Risiko:**
- Sedikit peningkatan ukuran berkas cache JSON karena penyimpanan struktur bucket ganda.
- Perlu sinkronisasi unit test untuk memvalidasi parser bucket baru dan listicle formatter.
