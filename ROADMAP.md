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
- [x] **Pilar 5: Modularisasi Arsitektur Internal (Zero External Dependencies)**:
  - Dekomposisi monolit `scripts/agy_switch.py` (4.064 baris) dan `scripts/watch.py` (1.609 baris) menjadi sub-paket internal terstruktur di bawah `scripts/agy_switcher/` dengan *Strict 5-Layer DAG* (Layer 0 s/d Layer 4).
  - Preservasi antarmuka publik dan transparansi runtime mocking melalui *Dynamic Module Proxy Bridge* pada `hook.py` dan `cli.py`.
  - Re-Export Facade Launchers kanonikal (`scripts/agy_switch.py` & `scripts/watch.py`) dengan resolusi symlink `os.path.realpath`.
  - Suite pengujian statis AST DAG dan validasi integritas namespace terisolasi pada `tests/test_modular_architecture.py`.
- [x] **Siklus 1 (Triad): Fondasi Stabilitas Headless & Bugfixing Kritis**:
  - Eliminasi dead code tak terjangkau di `ProcessSupervisor._poll_process_exit` ([PRUNE]).
  - Active File Descriptor ownership scan di `/proc/*/fd` dan pembersihan otomatis stale presence lock file.
  - Pemindaian dual-directory `~/.gemini/antigravity/` (Desktop) dan `~/.gemini/antigravity-cli/` (CLI) di `presence.py` dan `watcher.py`.
  - Perbaikan perutean parameter `target_model` pada formatter tabel dan listicle kuota Claude vs Gemini.
- [x] **Siklus 2 (Triad): Paketasi Modul, In-Process SDK Interface, & Headless Operations**:
  - Standardisasi packaging deklaratif root `pyproject.toml` dengan mapping `package_dir = {"": "scripts"}` dan console script `agy-switch` (tetap zero external pip dependencies).
  - Pembangunan In-Process SDK Facade di `scripts/agy_switcher/api.py` (Layer 4 DAG) dengan Disk-Only Swap (`reload_server=False`) secara default untuk mencegah blokir presence lock selama failover otomatis.
  - Alur otentikasi remote VPS `login --headless` (mode interaktif stdin dan non-interaktif `--auth-code`).
  - Utilitas migrasi vault `vault export` dan `vault import` berbasis gzipped tarball murni standard library dengan verifikasi checksum SHA-256 dan penegakan izin `0600`.

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
- **Status**: Selesai (Shipped)
- **Deskripsi**:
  - Refaktorisasi berkas monolit `scripts/agy_switch.py` (4.064 baris) dan daemon `scripts/watch.py` (1.609 baris) menjadi sub-paket internal terstruktur di bawah `scripts/agy_switcher/` dengan *Strict 5-Layer DAG* (Layer 0: `exceptions.py`, `models.py`, `security.py`; Layer 1: `supervisor.py`, `formatters.py`, `telemetry.py`; Layer 2: `vault.py`, `rotation.py`, `doctor.py`, `watcher.py`, `presence.py`; Layer 3: `hook.py`; Layer 4: `cli.py`, `__init__.py`, `agy_switch.py`, `watch.py`).
  - Mengubah `scripts/agy_switch.py` dan `scripts/watch.py` menjadi *Re-Export Facade Launchers* berbobot ringan untuk menjamin kompatibilitas mundur 100%.
  - Penerapan *Dynamic Module Proxy Bridge* pada `hook.py` dan `cli.py` guna menjamin zero-regression pada seluruh test case eksisting.
  - Penyusunan suite validasi statis AST DAG dan uji isolasi namespace sub-proses pada `tests/test_modular_architecture.py`.

---

## 3. Peta Jalan Ekosistem Triad Personal Assistant 24/7 (OpenClaw-Style)

Rencana pengembangan strategis untuk mengoperasikan `agy-switcher` bersamaan dengan `agy-telemote` (Telegram gateway & concurrency runner) dan `agy-memory` (durable context, FTS5 & persona) guna mewujudkan asisten pribadi otonom 24/7 berbasis ekosistem Antigravity CLI.

Detail arsitektur, data flow, dan analisis kegagalan terdokumentasi di [`.scratch/triad-personal-assistant/spec.md`](.scratch/triad-personal-assistant/spec.md).

```
                      ┌────────────────────────────────────────┐
                      │         TELEGRAM USER / GROUP          │
                      └───────────────────┬────────────────────┘
                                          │
                                          ▼
                      ┌────────────────────────────────────────┐
                      │              agy-telemote              │
                      │  - Forum Topics & Concurrency Queue    │
                      │  - TurnRunner (Subprocess per Turn)    │
                      └──────────────┬──────────────────┬──────┘
                                     │                  │
                1. Context Directives│                  │ 2. Failover & In-Process SDK
                & Memory Hooks       │                  │    (Auto-Rotate on 429)
                                     ▼                  ▼
    ┌──────────────────────────────────┐              ┌──────────────────────────────────┐
    │            agy-memory            │              │           agy-switcher           │
    │  - Persona Isolation (SOUL.md)   │              │  - Multi-Account OAuth Vault     │
    │  - Durable Facts & SQLite FTS5   │◀─────────────│  - Fast Telemetry & Cooldown     │
    │  - Failure Reflection            │  Panic Event │  - Dual Sync Desktop + CLI       │
    └──────────────────────────────────┘              └──────────────────────────────────┘
                                     │                  │
                                     ▼                  ▼
                      ┌────────────────────────────────────────┐
                      │         Google Antigravity CLI         │
                      │   `agy -p "..." --conversation <id>`   │
                      └────────────────────────────────────────┘
```

> [!IMPORTANT]
> **Invarian Arsitektur: Standalone Independence & Graceful Degradation (Zero Hard Coupling)**
> Ketiga komponen (`agy-switcher`, `agy-telemote`, `agy-memory`) **wajib dapat diinstal, dikonfigurasi, dan berjalan secara 100% independen tanpa saling mewajibkan keberadaan satu sama lain**:
> 1. **Zero Hard Pip Dependencies**: Tidak ada modul yang saling mencantumkan satu sama lain di blok `dependencies` pada `pyproject.toml`. Masing-masing tetap mempertahankan prinsip *Zero External Dependencies* (hanya Python 3.10+ stdlib).
> 2. **Soft Import & Dynamic Capability Probing**: Sinergi antar-modul dilakukan melalui *safe soft-import* (`try: import agy_switcher.api except ImportError: None`) atau pengecekan ketersediaan perintah CLI saat runtime.
> 3. **Graceful Fallback Behaviour**:
>    - **`agy-telemote` tanpa `agy-switcher`**: Bot tetap berfungsi penuh menjalankan sesi `agy`. Jika kuota habis (429), bot mengembalikan pesan error standar Antigravity ke pengguna tanpa crash. Perintah `/quota` dan `/switch` menampilkan pesan ramah bahwa `agy-switcher` belum terpasang.
>    - **`agy-switcher` tanpa `agy-telemote` / `agy-memory`**: Bekerja 100% sebagai CLI vault manager, Desktop/CLI token switcher, daemon watcher, dan PreInvocation hook.
>    - **`agy-memory` tanpa `agy-switcher` / `agy-telemote`**: Bekerja 100% mengelola persona `SOUL.md`, SQLite FTS5, dan PreInvocation hook tanpa memedulikan akun mana yang sedang aktif.

---

### Siklus 1: Fondasi Stabilitas Headless & Bugfixing Kritis
* **Fokus**: Menghilangkan false positive dan bug blocking pada headless VPS / server CLI.
* **Status**: Selesai (Shipped)
* **Tiket Pekerjaan**:
  - [x] **[P0-1] Perbaikan Stale Lock Trap pada `ProcessSupervisor.is_server_busy`**:
    - Validasi keaktifan lock melalui kepemilikan file descriptor PID aktif di `/proc` (bukan sekadar `iterdir()`).
    - Otomatis bersihkan lock basi yang tidak memiliki proses hidup terkait di `scripts/agy_switcher/supervisor.py`.
  - [x] **[P0-2] Dual-Directory Presence & Brain CLI Scan**:
    - Tambahkan pemindaian `~/.gemini/antigravity-cli/presence` dan `~/.gemini/antigravity-cli/brain` di `presence.py` dan `watcher.py` agar transkrip sesi CLI terpantau.
  - [x] **[P0-3] Perbaikan Formatter Model Claude**:
    - Teruskan parameter `target_model` dari `cli.py` ke `format_accounts_table` dan `format_accounts_listicle` di `formatters.py`.
    - Tampilkan header dinamis `CLAUDE QUOTA` vs `GEMINI QUOTA`.
  - [x] **[P0-4] Pembersihan Unreachable Dead Code**:
    - Hapus duplikat perulangan tak terjangkau setelah `return False` di `scripts/agy_switcher/supervisor.py#L181-L194`.
* **Kriteria Selesai (DoD)**:
  - Rotasi `agy-switch switch <alias>` tidak lagi diblokir oleh lock file yatim pada server headless.
  - `agy-switch quota --model claude` mencetak tabel kuota Claude yang akurat.
  - Suite unit test hijau: `python3 -m unittest discover tests/`.

---

### Siklus 2: Paketasi Modul & In-Process SDK Interface
* **Fokus**: Menjadikan `agy-switcher` sebagai library Python standar yang dapat diinstal (`pip install -e .`) dan diimpor langsung oleh `agy-telemote`.
* **Status**: Selesai (Shipped — [PR #2](https://github.com/satriyabajuhitam/agy-switcher/pull/2))
* **Tiket Pekerjaan**:
  - [x] **[P1-1] Standardisasi Packaging Python (`pyproject.toml`)**:
    - Konfigurasi `pyproject.toml` berbasis standard-library setuptools tanpa external dependencies.
    - Pendaftaran console script CLI `agy-switch = "agy_switcher.cli:main"`.
  - [x] **[P1-2] In-Process SDK Facade (`scripts/agy_switcher/api.py`)**:
    - Sediakan antarmuka publik Python murni type-hinted:
      - `get_active_account() -> AccountInfo`
      - `auto_rotate_on_exhaustion(model="gemini") -> RotationResult`
      - `switch_account(name: str) -> bool`
      - `get_fleet_quota_summary(model="all") -> FleetQuotaSummary`
  - [x] **[P1-3] Alur Otentikasi Remote VPS (`login --headless`)**:
    - Tangkap URL otentikasi Google dari `stderr` saat memanggil `agy -p "auth check"` tanpa TUI curses.
    - Terima kode otentikasi via stdin/argumen dan simpan token langsung ke vault.
  - [x] **[P2-4] Utilitas Migrasi Vault (`export` & `import`)**:
    - Perintah `agy-switch vault export --bundle <file.tar.gz>` dan `agy-switch vault import <file.tar.gz>`.
* **Kriteria Selesai (DoD)**:
  - `pip install -e /srv/agy-workspaces/booster/agy-switcher` sukses tanpa error build.
  - `python3 -c "import agy_switcher.api as api; print(api.get_active_account())"` berjalan tanpa subprocess.

---

### Siklus 3: Integrasi Triad & Self-Healing Failover Engine
* **Fokus**: Menghubungkan failover otomatis di `agy-telemote` dan pencatatan refleksi kegagalan di `agy-memory`.
* **Status**: Direncanakan (Planned)
* **Tiket Pekerjaan**:
  - [ ] **Mid-Turn Failover Retry Loop pada `TurnRunner` (`agy-telemote`)**:
    - Tangkap status error `RESOURCE_EXHAUSTED` (429) pada eksekusi `agy`.
    - Lakukan *safe soft-import* ke `agy_switcher.api`; jika tidak terpasang, teruskan pesan kuota habis ke pengguna tanpa crash.
    - Jika `agy_switcher` tersedia, panggil `auto_rotate_on_exhaustion()` dan ulangi eksekusi `agy -p ... --conversation <conv_id>` secara transparan tanpa kehilangan konteks.
  - [ ] **[P2-3] Perintah Kontrol Telegram di `agy-telemote`**:
    - Handler `/quota`: Menampilkan kapasitas armada akun aktif dan standby jika `agy-switcher` terpasang; jika tidak, tampilkan panduan aktivasi opsional.
    - Handler `/switch`: Mengganti akun aktif via Telegram inline keyboard buttons secara aman.
    - Pembaruan `/status`: Menyertakan info akun dan kapasitas aktif di blok ringkasan sesi (opsional jika switcher aktif).
  - [ ] **Penyambungan Panic Event ke `agy-memory` (Opsional)**:
    - Kirim failure report ke `agy-memory` hanya jika `agy-memory` terpasang pada sistem; jika tidak, cukup catat ke log lokal `agy-switcher`.
  - [ ] **[P3-2] Webhook / Telegram Alert pada Watcher Daemon**:
    - Dukungan notifikasi via callback/bot Telegram saat daemon mendeteksi rotasi otomatis di background.
* **Kriteria Selesai (DoD)**:
  - Error 429 pada panggilan asisten Telegram memicu rotasi token dan melanjutkan tugas secara transparan (*zero turn drop*) saat `agy-switcher` terpasang.
  - `agy-telemote` tetap dapat dinyalakan dan digunakan normal 100% tanpa kehadiran `agy-switcher` maupun `agy-memory`.
  - Interaksi `/quota` dan `/switch` berjalan interaktif di Telegram.

---

### Siklus 4: Automasi Ekosistem, Matriks Kuota & Otonomi MCP
* **Fokus**: Memaksimalkan kenyamanan administrasi, komparasi multi-model, dan kapabilitas self-reflection AI via MCP.
* **Status**: Direncanakan (Planned)
* **Tiket Pekerjaan**:
  - [ ] **[P2-1] Otomatisasi Lifecycle Hook & Dynamic Paths**:
    - Otomatisasi registrasi hook di `install.sh` dan sediakan `uninstall_hook` di `vault.py`.
    - Terapkan dynamic path dan subshell multi-PATH guard: `PATH="$HOME/.gemini/antigravity/bin:$HOME/.local/bin:$PATH"`.
  - [ ] **[P2-2] Dual-Model Quota Matrix View (`--matrix` / `--all-models`)**:
    - Tampilkan matriks kapasitas Gemini dan Claude secara berdampingan dalam satu tabel CLI.
  - [ ] **[P3-1] Native Stdlib MCP Server (`mcpServers.agy-switch`)**:
    - Bangun MCP server berbasis standard-library JSON-RPC (`scripts/agy_switcher/mcp_server.py`).
    - Daftarkan tools: `agy_check_quota`, `agy_list_accounts`, `agy_switch_account`.
* **Kriteria Selesai (DoD)**:
  - Instalasi via `bash install.sh --local` langsung mengaktifkan auto-rotasi out-of-the-box.
  - Tampilan `agy-switch quota --matrix` mencakup kedua keluarga model.
  - Agen Antigravity dapat memeriksa kuotanya sendiri via pemanggilan tool MCP.


