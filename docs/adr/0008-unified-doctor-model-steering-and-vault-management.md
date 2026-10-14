# 8. Unified Doctor, Model Steering, & Full-State Vault Management

Date: 2026-10-14

## Status

Accepted

## Context

Pada roadmap `agy-switch` (Pilar 4: Diagnostik Terpadu `doctor` & Manajemen Vault Lanjutan), terdapat kebutuhan mendesak untuk memperkuat kemampuan observabilitas mandiri, fleksibilitas pemilihan model pada rotasi otomatis, serta tata kelola alias akun tanpa merusak integritas vault:

1. **Ketiadaan Alat Diagnostik Mandiri Terpadu**:
   Pengguna dan agen sering kali kesulitan mendiagnosis kegagalan operasional (seperti izin berkas yang terlalu longgar, token kadaluwarsa/corrupt, desinkronisasi antara token Desktop dan CLI, hook lifecycle yang belum terpasang, atau proses `language_server` yang mati/macet). Diperlukan satu subcommand `doctor` yang memvalidasi kondisi end-to-end secara deterministik.
2. **Keterbatasan Rotasi Model-Spesifik pada `switch --auto`**:
   Saat ini, `switch --auto` di CLI mengasumsikan model default `"gemini"`, mengabaikan kebutuhan pengguna yang ingin merotasi akun berdasarkan kuota model Claude (`Claude 3.5 Sonnet`). Tanpa parameter `--model` dan konfigurasi default persisten, rotasi otomatis berisiko memilih akun dengan kuota Gemini penuh padahal kuota Claude akun tersebut sudah habis (0%).
3. **Risiko Fragmentasi Metadata Saat Rename Akun**:
   Mengganti alias akun secara manual di filesystem berisiko meninggalkan broken state: pointer `~/.antigravity-vault/active` menjadi dangling, data cache pada `last_known_quota.json` dan penalti cooldown pada `cooldown.json` menjadi zombie, serta berpotensi mengacaukan sesi yang sedang berjalan jika dilakukan di tengah giliran aktif (*active turn*) agen.

## Decision

1. **Subcommand Self-Healing Diagnostics (`agy-switch doctor [--fix] [--json]`)**
   - **5-Tier Comprehensive Health Audit**:
     1. *Permissions Audit*: Memvalidasi izin POSIX direktori vault `~/.antigravity-vault` (`0700`), folder akun `accounts/` (`0700`), berkas token `token.json` (`0600`), berkas pointer `active` (`0600`), berkas cache telemetri (`0600`), dan berkas `watch.pid` (`0600`).
     2. *Token & Identity Integrity*: Memeriksa struktur JSON token wajib (`access_token`, `refresh_token`, `token_type`, `expiry`), mengekstrak email pengguna via decode JWT `id_token` tanpa dependensi eksternal, dan mendeteksi token kadaluwarsa.
     3. *Dual-Destination Sync*: Memverifikasi sinkronisasi byte-for-byte antara kredensial Desktop (`~/.gemini/jetski-standalone-oauth-token`), kredensial CLI (`~/.gemini/antigravity-cli/antigravity-oauth-token`), dan akun aktif di vault.
     4. *Lifecycle Hook Status*: Memastikan pendaftaran hook `PreInvocation` di `~/.gemini/config/hooks.json` dengan path skrip/binari yang valid dan executable.
     5. *Supervisor & Process Health*: Memeriksa PID dan status keaktifan/kesibukan `language_server`, serta status daemon `watch` (mendeteksi file `watch.pid` basi / stale).
   - **Severity 3-Level Exit Codes**:
     - `0` (PASS): Semua audit bersih atau berstatus optimal.
     - `1` (WARN): Peringatan non-fatal (misal: `language_server` belum berjalan, hook non-aktif, atau 1 akun sekunder kadaluwarsa namun akun aktif sehat).
     - `2` (FAIL): Kegagalan kritis integritas (kebocoran izin berkas, token corrupt, desinkronisasi kredensial aktif, atau pointer aktif dangling).
   - **Non-Destructive Auto-Healing (`--fix`)**:
     - Otomatis merestorasi izin berkas (`chmod 0700` direktori, `0600` berkas token/cache/pid).
     - Menghapus berkas `watch.pid` basi jika proses daemon sudah tidak aktif.
     - Memasang ulang registrasi hook `PreInvocation` jika hilang atau mengarah ke path usang.
     - Menyinkronkan kembali token ganda Desktop/CLI menggunakan kredensial akun aktif dari vault jika terjadi desinkronisasi.
     - Anomali token rusak/hilang ditandai `UNFIXABLE` dengan instruksi bagi user/agen untuk menjalankan `agy-switch login <alias>`.

2. **Model-Aware Quota Steering (`switch --auto --model` & `config default-model`)**
   - Mendukung argumen CLI `agy-switch switch --auto [--model MODEL]`.
   - Normalisasi dwigrup: nilai input model dinormalisasi ke target model key (`gemini` vs `claude`), mendukung shorthand (`gemini`, `claude`) maupun model identifier spesifik (`gemini-3.8-pro`, `claude-3-5-sonnet`).
   - Persistent Configuration: Menambahkan subcommand `agy-switch config default-model [gemini|claude]` yang mencatat preferensi model di `~/.antigravity-vault/config.json`.
   - Hierarki Resolusi Model: `CLI --model` -> `config.json: default_model` -> Fallback default `"gemini"`.
   - Isolasi Kuota Mutlak (*Strict No-Fallback*): Jika seluruh akun kehabisan kuota untuk model yang diminta, rotasi dibatalkan dengan pesan error eksplisit (`All accounts exhausted for model '<model>'`) guna mencegah pergantian akun ke akun yang juga tidak memiliki kuota untuk model yang diinginkan.

3. **Full-State Account Alias Migration (`agy-switch rename <old_alias> <new_alias> [--force]`)**
   - Validasi Karakter Ketat: Memvalidasi `new_alias` dengan regex `^[a-zA-Z0-9._-]+$`, menolak karakter spasi, path separator (`/`, `\`), dan directory traversal (`..`).
   - Validasi Eksistensi: Memastikan direktori `accounts/<old_alias>` ada, dan `accounts/<new_alias>` belum pernah terdaftar.
   - Active Turn Safety Guard: Jika akun yang di-rename adalah akun yang sedang aktif dan `language_server` sedang menjalankan giliran aktif (*busy turn*), operasi ditolak kecuali flag `--force` disertakan.
   - 4-Langkah Propagasi Atomik:
     1. Rename direktori `accounts/<old_alias>` ke `accounts/<new_alias>`.
     2. Jika `old_alias == active_account`: perbarui isi berkas `active` secara atomik menjadi `new_alias`.
     3. Migrasi entri key `old_alias` -> `new_alias` pada `~/.antigravity-vault/cache/last_known_quota.json`.
     4. Migrasi entri key `old_alias` -> `new_alias` pada `~/.antigravity-vault/cache/cooldown.json`.

## Considered Options & Rejected Alternatives

- **Pemisahan Perintah Khusus `repair` vs `doctor --fix`**: Ditolak demi konsistensi pola tool standar (seperti `npm audit fix` atau `brew doctor`), menyatukan audit dan auto-remediasi di bawah satu subcommand.
- **Cross-Model Quota Fallback**: Ditolak karena jika seorang developer secara eksplisit meminta switch untuk kuota Claude, memutar ke akun yang hanya memiliki kuota Gemini tidak memecahkan masalah kuota Claude yang habis dan akan memicu error 429 lanjutan di tengah prompt Claude.
- **Shallow Directory-Only Rename**: Ditolak karena meninggalkan state cache dan cooldown dalam kondisi terpecah (orphaned entries), serta memicu status akun aktif tidak valid jika akun yang di-rename sedang berstatus aktif.

## Decision Drivers & Seam Invariants

- **Zero External Dependencies**: Seluruh pengecekan izin berkas POSIX, validasi JWT payload, parsing config JSON, dan migrasi state atomik menggunakan modul standar Python (`os`, `stat`, `json`, `base64`, `re`, `pathlib`).
- **Turn Safety Preservation**: Operasi yang menyentuh kredensial aktif (`rename` dan `--fix` re-sync) wajib menghormati kondisi proses `language_server`.

## Negative Consequences & Failure Modes

- Pelaksanaan `--fix` tidak dapat memperbaiki token yang benar-benar rusak/revoked oleh Google OAuth server secara otomatis; pengguna tetap harus melakukan otentikasi ulang manual via `login`.
- Penegakan isolasi kuota mutlak berarti auto-switch akan gagal jika tidak ada satu pun akun yang memiliki kuota sisa untuk model yang ditargetkan, mengharuskan pengguna beralih model atau menunggu siklus reset kuota.
