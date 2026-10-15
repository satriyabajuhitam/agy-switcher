# 9. Modular Architecture and Zero-Dependency Sub-Package Decomposition

Date: 2026-10-15

## Status

Accepted

## Context

Pada roadmap `agy-switch` (Pilar 5: Modularisasi Arsitektur Internal & Zero External Dependencies), basis kode telah berkembang secara pesat:
1. **Pembengkakan Berkas Monolit**:
   Berkas `scripts/agy_switch.py` telah berkembang dari 1.639 baris menjadi 4.064 baris, menggabungkan logika vault token, HTTP telemetri kuota, algoritma rotasi cerdas, process supervisor `language_server`, diagnostik `doctor` 5-lapisan, lifecycle hook pre-invocation, formatter CLI (tabel, listicle, JSON), dan router CLI ke dalam satu berkas tunggal.
2. **Duplikasi & Isolasi Berkas Watcher**:
   Berkas `scripts/watch.py` mencapai 1.609 baris, mendefinisikan daemon manager, presence detector, log watcher, dan cooldown manager secara terpisah dengan duplikasi parsial penanganan berkas log aman dan fallback impor `AgySwitchEngine`.
3. **Kerapuhan Pemeliharaan (Maintenance Fragility)**:
   Perubahan kecil pada satu domain (misalnya diagnostik doctor atau telemetri) berisiko menimbulkan regresi pada domain lain (misalnya lifecycle hook atau process supervisor).
4. **Kendala Kompatibilitas Mundur (Backward Compatibility Invariants)**:
   - Terdapat 505 unit test eksisting yang memanggil dan mem-patch simbol `agy_switch` secara ekstensif (seperti `patch("agy_switch.fetch_account_quota")`).
   - Mekanisme instalasi mandiri `install.sh` membuat symlink launcher dari `~/.local/bin/agy-switch` langsung ke `scripts/agy_switch.py`.
   - Repositori wajib mematuhi aturan nol dependensi eksternal (hanya Python 3.10+ standard library dan POSIX bash).

## Decision

1. **Topologi Paket Internal (`scripts/agy_switcher/`) & Re-Export Facade Launcher**:
   - Membentuk sub-paket internal Python murni di direktori `scripts/agy_switcher/` dengan berkas `__init__.py` yang me-re-export seluruh simbol publik.
   - Mengubah `scripts/agy_switch.py` menjadi launcher berbobot ringan (`Re-Export Facade Launcher`) yang mengeksekusi `cli.main()` sekaligus mengekspos seluruh simbol publik, fungsi pembantu, dan kelas ke namespace `agy_switch` untuk menjamin kompatibilitas mundur 100%.
   - Mengubah `scripts/watch.py` menjadi executable shim tipis yang me-re-export kelas-kelas watcher dan memanggil `watcher.main()`.

2. **Dekomposisi Granular 10 Modul**:
   Memecah monolit ke dalam 10 modul dengan batas tanggung jawab tunggal (*Single Responsibility Principle*):
   - `exceptions.py`: Seluruh tipe exception domain (`AgySwitchError`, `InvalidTokenError`, `AccountNotFoundError`, `ActiveTurnRunningError`, `DaemonError`, `DaemonAlreadyRunningError`).
   - `models.py`: Struktur data murni dan dataclass (`DiagnosticItem`, `DiagnosticReport`, `QuotaStatus`, `EvaluationCandidate`).
   - `supervisor.py`: Pengawasan proses sistem (`locate_language_server_pid`, `is_server_busy`, restart aman, turn safety guards melalui kelas `ProcessSupervisor`).
   - `telemetry.py`: Klien HTTP CloudCode API (`QuotaClient`), pengambilan kuota paralel/single, parsing defensif token & timestamp, perhitungan bucket countdown, dan manajemen cache disk (`last_known_quota.json`).
   - `rotation.py`: Mesin rotasi cerdas (`RotationEngine`) dengan heuristik `cache_optimized`, `drain_window`, `auto_highest`, penalti cooldown, dan kemudi kuota model (`--model`).
   - `vault.py`: Operasi inti vault kredensial (`AgySwitchEngine`), penyimpanan token atomik ganda (`os.replace`), operasi CRUD akun (`add`, `login`, `remove`, `rename`), konfigurasi persisten, dan migrasi legacy.
   - `watcher.py`: Log watcher aktif dan manajemen siklus hidup daemon (`PresenceDetector`, `CooldownManager`, `LogWatcher`, `DaemonManager`, generator unit user systemd).
   - `doctor.py`: Mesin audit kesehatan mandiri 5-lapisan (`DoctorEngine`, `run_doctor`, `auto_heal`).
   - `hook.py`: Penanganan lifecycle hook (`handle_pre_invocation_hook`) dengan evaluasi 3-tingkat (*Tiered Cache-First Polling*).
   - `formatters.py`: Seluruh logika penyajian data antarmuka (Listicle Formatter, tabel Markdown, laporan status, dan diagnostik).
   - `cli.py`: Definisi argumen argparse dan router dispatching subcommand.

3. **Strict 5-Layer DAG (Directed Acyclic Graph)**:
   Menerapkan arsitektur dependensi berlapis satu arah yang mencegah terjadinya *circular imports* tanpa trik impor dinamis:
   - **Layer 0 (Foundation)**: `exceptions.py`, `models.py` (Nol dependensi internal).
   - **Layer 1 (Core Services & IO)**: `supervisor.py`, `formatters.py`, `telemetry.py` (Hanya bergantung pada Layer 0).
   - **Layer 2 (Domain Engines)**: `vault.py`, `rotation.py`, `watcher.py`, `doctor.py` (Bergantung pada Layer 0 dan Layer 1; `doctor.py` beroperasi berbasis path direktori & `supervisor` tanpa impor `vault.py` tingkat modul).
   - **Layer 3 (Workflow & Orchestration)**: `hook.py` (Bergantung pada Layers 0–2).
   - **Layer 4 (Presentation & Dispatch)**: `cli.py`, `__init__.py`, `agy_switch.py`, `watch.py` launcher (Mengorkestrasi seluruh layer).

4. **Unified Facade Pattern pada `AgySwitchEngine`**:
   `AgySwitchEngine` di `vault.py` mengomposisikan instance `ProcessSupervisor` dan `DoctorEngine` secara internal, mempertahankan tanda tangan metode publik kanonikal (`locate_language_server_pid`, `is_server_busy`, `run_diagnostics`, `fix_diagnostics`, `_is_valid_expiry`, dll.) sehingga seluruh integrasi pemanggil tidak mengalami breaking change.

5. **Dynamic Module Proxy Bridge untuk Hook & CLI**:
   Pada `hook.py` dan `cli.py`, pemanggilan fungsi telemetri (`fetch_account_quota`, `fetch_all_accounts_quota`) dan instansiasi kelas (`QuotaClient`, `RotationEngine`) diselesaikan melalui pemeriksaan runtime `sys.modules.get("agy_switch")`. Pola ini menjamin bahwa pengujian unit eksisting yang mem-patch `agy_switch.*` akan langsung direspons tanpa modifikasi pada 505 test case eksisting.

6. **Verifikasi Terisolasi & Test Suite Modular**:
   Menambahkan `tests/test_modular_architecture.py` untuk menguji integritas import package, kepatuhan lapisan DAG via modul `ast`, dan isolasi sub-modul secara mandiri, berdampingan dengan 505 test suite eksisting.


## Considered Options & Rejected Alternatives

- **Layout Paket Standar `src/agy_switcher/`**: Ditolak karena memerlukan modifikasi pada skrip instalasi `install.sh`, alur symlink `user-home-bootstrap`, dan konfigurasi path lingkungan agen tanpa memberikan nilai tambah pada runtime zero-dependency.
- **Struktur Datar (Flat) dengan Impor Lokal di Dalam Fungsi**: Ditolak karena menyembunyikan ketergantungan melingkar (circular imports) di balik runtime execution, menyulitkan static analysis, dan meningkatkan overhead pemanggilan fungsi.
- **Penghapusan Total `scripts/watch.py`**: Ditolak demi mencegah rusaknya integrasi systemd user service atau skrip monitoring eksternal yang mungkin memanggil `scripts/watch.py` secara langsung.
- **Refaktorisasi Menyeluruh Berkas Test Eksisting**: Ditolak untuk menjamin prinsip *Zero Regression Guarantee*. Semua pengujian lama harus tetap hijau untuk membuktikan bahwa refaktorisasi modular tidak mengubah semantik operasional sistem sedikit pun.

## Decision Drivers & Seam Invariants

- **Zero External Dependencies**: Seluruh sub-modul beroperasi murni dengan modul standar Python (`urllib.request`, `json`, `os`, `sys`, `pathlib`, `stat`, `subprocess`, `signal`, `time`, `datetime`, `re`, `math`).
- **Turn Safety Preservation**: Komposisi `ProcessSupervisor` menjaga keselamatan giliran aktif agen sebelum mematikan atau merestart `language_server`.
- **Atomic Credential Integrity**: `vault.py` memegang kendali eksklusif terhadap operasi `os.replace` untuk token Desktop dan CLI.
- **100% Backward Compatibility**: Seluruh kontrak CLI dan API Python yang telah digunakan pada rilis sebelumnya tetap valid tanpa degradasi.

## Negative Consequences & Failure Modes

- Pemeliharaan facade re-export di `scripts/agy_switch.py` membutuhkan kedisiplinan agar setiap penambahan kelas/fungsi publik baru di sub-modul juga diekspos melalui `__all__` jika ditujukan untuk konsumsi publik.
- Overhead bridging via `sys.modules` pada `hook.py` menambahkan satu percabangan lookup lookup ringan (~mikrodetik), namun trade-off ini sepadan demi stabilitas ratusan unit test otomasi.
