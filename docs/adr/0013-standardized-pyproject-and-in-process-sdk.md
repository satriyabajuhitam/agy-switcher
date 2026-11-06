# 13. Standardized pyproject.toml Packaging and In-Process SDK Facade

Date: 2026-11-06

## Status

Accepted (Supersedes packaging rejection in ADR-0003)

## Context

Dalam pengembangan ekosistem Triad Personal Assistant 24/7 (`agy-switcher` + `agy-telemote` + `agy-memory`), `agy-switcher` berperan sebagai penjaga kontinuitas kuota dan penyedia token multi-akun.

1. **Kebutuhan In-Process Integration pada `agy-telemote`**:
   `TurnRunner` pada gateway Telegram `agy-telemote` membutuhkan kemampuan memanggil mekanisme rotasi secara in-process saat mendeteksi error `RESOURCE_EXHAUSTED` (429) tanpa overhead eksekusi subprocess CLI atau parsing output teks terminal.
2. **Keterbatasan ADR-0003**:
   [ADR-0003](file:///srv/agy-workspaces/booster/agy-switcher/docs/adr/0003-public-distribution-strategy.md) menolak keberadaan `pyproject.toml` demi menjaga kesederhanaan direktori root. Penolakan ini membatasi integrasi karena consumer Python tidak dapat melakukan instalasi editable (`pip install -e .`) atau impor modul standar secara deterministik.
3. **Kendala Lingkungan Server Headless (VPS)**:
   - Di VPS/server tanpa antarmuka grafis browser, alur Google OAuth membutuhkan mekanisme penangkapan auth URL dan input auth code via CLI atau programatik bot.
   - Panggilan rotasi token di server headless sering kali terblokir jika proses switcher mencoba memeriksa atau merestart `language_server` desktop yang tidak relevan bagi giliran subproses CLI.
   - Migrasi armada token antar-server memerlukan mekanisme ekspor/impor yang portabel tanpa mengorbankan izin berkas privat (`0600`).

## Decision

Kami memutuskan untuk menstandarisasi paketasi repositori dan menyediakan antarmuka SDK in-process dengan tetap mempertahankan prinsip **Zero External Dependencies** (Python 3.10+ standard library murni):

1. **Standardisasi Packaging via `pyproject.toml` Berbasis `package_dir = {"": "scripts"}`**:
   - Menambahkan berkas `pyproject.toml` standar dengan build backend `setuptools.build_meta`.
   - Memetakan paket langsung ke `scripts/agy_switcher/` via `package_dir = {"": "scripts"}` tanpa merestrukturisasi repositori ke folder `src/`. Hal ini mencegah regresi path pada 741 unit test dan skrip instalasi eksisting.
   - Mendaftarkan console script kanonikal: `agy-switch = "agy_switcher.cli:main"`.
   - Skrip `install.sh` tetap dipertahankan sebagai metode instalasi mandiri untuk plugin Antigravity dan symlink CLI terminal.

2. **In-Process SDK Facade (`scripts/agy_switcher/api.py`)**:
   - Membangun antarmuka modul publik berbobot ringan di Layer 4 DAG:
     - `get_active_account() -> AccountInfo`
     - `auto_rotate_on_exhaustion(model: str = "gemini", reload_server: bool = False) -> RotationResult`
     - `switch_account(name: str, reload_server: bool = False) -> bool`
     - `get_fleet_quota_summary(model: str = "all") -> FleetQuotaSummary`
   - Menerapkan kebijakan **Disk-Only Swap** (`reload_server=False`) secara default untuk pemanggilan SDK programatik, sehingga rotasi token dilakukan murni di level berkas disk (`token.json`) tanpa memicu supervisor `language_server` dan aman dari blokir presence lock.

3. **Dual-Mode Headless Login (`agy-switch login --headless`)**:
   - Menyediakan alur login non-GUI yang mencetak URL otentikasi Google ke terminal dan menerima authorization code melalui stdin interaktif (`input()`).
   - Menyediakan parameter non-interaktif `--auth-code <code>` untuk automasi langsung via skrip atau bot Telegram.

4. **Utilitas Migrasi Vault (`vault export` & `vault import`)**:
   - Menggunakan modul standar `tarfile` dengan kompresi gzip (`.tar.gz`) untuk memaketkan direktori vault akun.
   - Menegakkan izin berkas aman `0600` pada arsip keluaran dan direktori hasil ekstraksi.
   - Menyertakan manifest checksum SHA-256 (`hashlib`) untuk verifikasi integritas isi paket saat impor.

## Consequences

### Positif:
- `agy-switcher` kini dapat diinstal via `pip install -e /path/to/agy-switcher` dan diimpor oleh `agy-telemote` (`import agy_switcher.api as api`).
- Eliminasi overhead subprocess pada saat penanganan kuota habis (429) di bot Telegram.
- Pengoperasian di headless VPS menjadi mulus melalui `login --headless` dan `vault export/import`.
- Struktur folder `scripts/`, symlink `install.sh`, dan 741 unit test tetap bekerja 100% tanpa perubahan jalur impor.
- Prinsip Zero External Pip Dependencies tetap terjaga secara mutlak.

### Negatif / Mitigasi:
- Penambahan `pyproject.toml` di root sedikit menambah jumlah berkas konfigurasi, namun disederhanakan murni menggunakan konfigurasi deklaratif standar `[build-system]` dan `[project]`.
