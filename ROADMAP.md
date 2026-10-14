# agy-switch Roadmap

Dokumen ini melacak rencana pengembangan strategis, fitur masa depan, dan riwayat rilis `agy-switch`.

---

## 1. Status Fitur Terkini (Shipped / Implemented)

- [x] **Global Fast Quota Telemetry**:
  - `QuotaClient` berbasis `urllib.request` murni (tanpa pip dependency) untuk direct hit ke CloudCode API.
  - Fast cache telemetri di `~/.antigravity-vault/cache/last_known_quota.json`.
  - Integrasi perintah `status` dan `list --quota`.
- [x] **Intelligent Auto-Rotation (`RotationEngine`)**:
  - Heuristik `cache_optimized`, `drain_window`, dan `auto_highest`.
  - Dukungan `switch --auto` untuk pemilihan instan akun cadangan terbaik.
- [x] **Lifecycle Hooks (`PreInvocation`)**:
  - Registrasi otomatis ke `~/.gemini/config/hooks.json`.
- [x] **Unified Bootstrap & Home Layout (`user-home-bootstrap`)**:
  - Deteksi instalasi mandiri via `curl | bash`, repositori standar di `~/agy-switcher`.

- [x] **Pilar 1: Penyelarasan Kontrak CLI, Subcommand `quota`, & Bucket Metadata**:
  - Implementasi subcommand `agy-switch quota` lengkap di argparse sesuai kontrak dokumentasi `README.md`.
  - Preservasi bucket mingguan dan 5-jam pada `parse_quota_response()` untuk sorting deterministik dan countdown reset.
  - Perbaikan dead parameter `--format list` dengan *Listicle Formatter* bernomor (non-markdown table) untuk integrasi chat agen.
- [x] **Pilar 2: Optimasi Performa & Ketahanan PreInvocation Hook**:
  - *Tiered Cache-First Polling*: 0ms pass untuk akun aktif dengan kuota sehat ($\ge 25\%$), probe single-account saat expire, dan full standby probe hanya saat kuota kritis ($< 15\%$).
  - Penyimpanan cache otomatis via `client.save_cache()` pada saat hook berjalan.
  - Penanganan jaringan offline vs kuota habis riil (cegah false alarm saat gangguan koneksi).
  - Preservasi ISO timestamp `expiry` saat token OAuth di-refresh.
- [x] **Pilar 3: Log Watcher Cerdas Berbasis Presence Desktop & Pengerasan Izin**:
  - *Presence-Aware Active Transcript Tailing*: Baca pointer aktif dari `~/.gemini/antigravity/presence/*.lock` untuk men-tail `brain/<active-id>/.system_generated/logs/transcript.jsonl` tanpa I/O storm.
  - Pengerasan izin berkas `cooldown.json` ke mode `0600`.
  - Manajemen daemon terpadu `agy-switch watch [start|stop|status]` dengan PID locking dan generator systemd user service.
- [x] **Pilar 4: Diagnostik Terpadu `doctor`, Model Steering, & Manajemen Vault Lanjutan**:
  - Subcommand `agy-switch doctor` untuk self-check otomatis 5-layer (permissions, tokens, hooks, daemon, language server) dengan opsi `--fix` dan output JSON murni.
  - Dukungan parameter `--model [gemini|claude]` pada `switch --auto` dan konfigurasi persisten `config default-model`.
  - Perintah `agy-switch rename <old_alias> <new_alias>` dengan transactional rollback guarantee.

---

## 2. Peta Jalan Fitur Mendatang (Upcoming Features & Strategic Pillars)

Detail spesifikasi teknis dan analisis dampak sistem terdokumentasi di [`.scratch/strategic-roadmap/spec.md`](.scratch/strategic-roadmap/spec.md).

### Pilar 1: Penyelarasan Kontrak CLI, Subcommand `quota`, & Bucket Metadata
- **Status**: Selesai (Shipped)
- **Deskripsi**:
  - Implementasi subcommand `agy-switch quota` lengkap di argparse sesuai kontrak dokumentasi `README.md`.
  - Preservasi bucket mingguan dan 5-jam pada `parse_quota_response()` untuk sorting deterministik dan countdown reset.
  - Perbaikan dead parameter `--format list` dengan *Listicle Formatter* bernomor (non-markdown table) untuk integrasi chat agen.

### Pilar 2: Optimasi Performa & Ketahanan PreInvocation Hook
- **Status**: Selesai (Shipped)
- **Deskripsi**:
  - *Tiered Cache-First Polling*: 0ms pass untuk akun aktif dengan kuota sehat ($\ge 25\%$), probe single-account saat expire, dan full standby probe hanya saat kuota kritis ($< 15\%$).
  - Penyimpanan cache otomatis via `client.save_cache()` pada saat hook berjalan.
  - Penanganan jaringan offline vs kuota habis riil (cegah false alarm saat gangguan koneksi).
  - Preservasi ISO timestamp `expiry` saat token OAuth di-refresh.

### Pilar 3: Log Watcher Cerdas Berbasis Presence Desktop & Pengerasan Izin
- **Status**: Selesai (Shipped)
- **Deskripsi**:
  - *Presence-Aware Active Transcript Tailing*: Baca pointer aktif dari `~/.gemini/antigravity/presence/*.lock` untuk men-tail `brain/<active-id>/.system_generated/logs/transcript.jsonl` tanpa I/O storm.
  - Pengerasan izin berkas `cooldown.json` ke mode `0600`.
  - Manajemen daemon terpadu `agy-switch watch [start|stop|status]` dengan PID locking dan generator systemd user service.

### Pilar 4: Diagnostik Terpadu `doctor`, Model Steering, & Manajemen Vault Lanjutan
- **Status**: Selesai (Shipped)
- **Deskripsi**:
  - Subcommand `agy-switch doctor` untuk self-check otomatis izin berkas, integritas token, registrasi hook, watcher daemon, dan supervisor `language_server`.
  - Dukungan parameter `--model [gemini|claude]` pada `switch --auto`.
  - Perintah `agy-switch rename <old_alias> <new_alias>`.

### Pilar 5: Modularisasi Arsitektur Internal (Zero External Dependencies)
- **Status**: Prioritas 1 (Upcoming)
- **Deskripsi**:
  - Refaktorisasi berkas monolit `scripts/agy_switch.py` menjadi sub-paket internal terstruktur di bawah `scripts/agy_switcher/` (`vault.py`, `telemetry.py`, `rotation.py`, `supervisor.py`, `formatters.py`, `cli.py`) dengan entry point tetap kompatibel.
