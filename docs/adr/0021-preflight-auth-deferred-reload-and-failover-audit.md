# 21. Pre-Flight Auth Gate, Deferred Safe-Turn Reload, and Failover Audit Quarantine

Date: 2026-11-26

## Status

Accepted (Amends ADR 0010 for in-turn sub-shell invocations)

## Context

Investigasi mendalam dan reproduksi end-to-end terhadap anomali perintah `agy-switch switch <name> --force` dan mekanisme failover `PreInvocation` hook di lingkungan runtime Antigravity aktif mengungkap empat celah arsitektural yang saling berkaitan:

1. **Validasi Switch Terbatas pada Skema JSON (`validate_token_file`)**:
   `AgySwitchEngine.switch_tokens()` (`scripts/agy_switcher/vault.py`) dan router CLI (`scripts/agy_switcher/cli.py`) hanya memverifikasi kelengkapan struktur JSON token (`access_token`, `refresh_token`, `token_type`, `expiry`, `auth_method`) tanpa menguji validitas auth/HTTP terhadap CloudCode API sebelum menimpa token aktif dan membunuh `language_server`. Akun dengan token yang telah dicabut atau mengembalikan `HTTP 403 Forbidden` tetap diaktifkan.
2. **Cacat Atomisitas Check-Before-Write pada `switch_tokens()`**:
   `switch_tokens()` menimpa berkas token target (`jetski-standalone-oauth-token` dan `antigravity-oauth-token`) serta pointer `~/.antigravity-vault/active` terlebih dahulu sebelum memanggil `restart_language_server(force=force)` yang menjalankan `verify_turn_safety()`. Akibatnya, bahkan ketika perintah `switch` ditolak dengan `ActiveTurnRunningError` (exit code 2), pointer `active` dan token di disk sudah terlanjur berubah (*split-brain* terhadap memori `language_server`).
3. **Synchronous Parricide pada Eksekusi In-Turn (`--force` & `switch --auto`)**:
   Sesuai ADR 0010, `switch --force` dan `switch --auto` mengirim `SIGTERM`/`SIGKILL` secara sinkron ke `language_server`. Ketika dieksekusi dari dalam sub-shell tool call milik turn Antigravity aktif (yakni sebagai proses turunan/descendant dari `language_server`), sinyal terminasi tersebut membunuh proses induknya sendiri (*parricide*), memutus koneksi RPC host dan membatalkan turn (`Tool execution was canceled - server restart`).
4. **Silent Rollback & Hilangnya Jejak Audit pada `hook-pre-invocation`**:
   Ketika `handle_pre_invocation_hook()` (`scripts/agy_switcher/hook.py`) mendeteksi `error_type: auth` (`HTTP 401/403`) pada akun aktif baru (`automator`), hook mengeksekusi failover kembali ke akun sehat (`honey`), mengevaksi `automator` dari `last_known_quota.json`, hanya mencetak diagnostik ke `stderr` yang bersifat sementara, dan memancarkan `ephemeralMessage` generik yang identik dengan rotasi kuota biasa. `watch.log`, `agy-switch status`, `agy-switch list`, dan `agy-switch doctor` tidak menyimpan jejak apa pun bahwa akun target baru saja diejeksi oleh hook failover karena korupsi auth.

## Decision

Kami menetapkan arsitektur hardening lima pilar untuk mengamankan alur manual switch, rotasi in-turn, dan auditabilitas failover hook:

### 1. Atomic Check-Before-Write pada `AgySwitchEngine.switch_tokens()`
- Seluruh pemeriksaan prasyarat—meliputi keberadaan akun, validasi skema `validate_token_file()`, pengecekan keamanan turn (`verify_turn_safety()` apabila `restart_server=True` dan bukan mode deferred), serta verifikasi auth pre-flight opsional—wajib diselesaikan **sebelum** `_sync_tokens_to_targets()` maupun `set_active_name()` menyentuh disk.
- Penolakan pada fase pre-flight (`ActiveTurnRunningError`, `InvalidTokenError`, `AccountAuthError`) menjamin zero side-effect pada berkas token aktif, `active`, maupun `state.json`.

### 2. Pre-Flight Auth Gate (Fail-Closed Auth / Fail-Open Network)
- **Cakupan Default**: Manual CLI `agy-switch switch <name>` (`cli.py`) mengaktifkan `verify_auth=True` secara default. Di tingkat engine/SDK (`AgySwitchEngine.switch_tokens(..., verify_auth=False)` dan `api.py`), parameter `verify_auth` tetap opt-in (`False` secara default) agar pemanggil internal yang telah melakukan probe telemetri (`hook.py`, `watcher.py`) maupun kontrak SDK eksisting tidak mengalami *double HTTP round-trip*.
- **1x Best-Effort Token Refresh & Penolakan Fail-Closed**: Saat probe auth menerima `HTTP 401` atau `HTTP 403`, klien melakukan 1x percobaan *best-effort* `refresh_access_token()` apabila `refresh_token` tersedia. Jika setelah refresh endpoint tetap mengembalikan error auth terkonfirmasi (`401`/`403` atau `invalid_grant`), `switch_tokens()` mengangkat `AccountAuthError(AgySwitchError)` sebelum mutasi disk maupun restart server, mencatat status karantina akun ke `cache/failover_state.json`, dan mengarahkan pengguna menjalankan `agy-switch login <alias>`.
- **Resiliensi Offline (Fail-Open)**: Gangguan jaringan sementara (`URLError`, `TimeoutError`, HTTP 5xx) memancarkan peringatan ke `stderr` dan tetap mengizinkan switch berjalan (*fail-open*) agar operasional offline tidak terblokir.
- **Pemisahan Flag Override**: Menambahkan flag eksplisit `--skip-auth-check` pada subcommand `switch` yang terpisah sepenuhnya dari `--force` (di mana `--force` hanya mengatur override turn-busy, bukan melewati validasi kredensial).

### 3. Ancestor Lineage Detection & Deferred Safe-Turn Reload (Amends ADR 0010)
- Menambahkan `ProcessSupervisor.is_invoked_by_language_server()` (`scripts/agy_switcher/supervisor.py`) untuk menelusuri rantai `ppid` dari `os.getpid()` melalui `proc_dir/<pid>/stat` (dengan batas kedalaman dan proteksi siklus).
- Apabila `switch --force` atau `switch --auto` dipanggil dari dalam sub-shell turunan `language_server` (in-turn), mekanisme restart sinkron otomatis diturunkan menjadi **Deferred Safe-Turn Reload**:
  - Mengeksekusi *Atomic Disk-Only Swap* untuk token Desktop/CLI dan pointer `active`.
  - Menulis marker atomik `~/.antigravity-vault/cache/pending_reload.json` (mode `0600`, `ttl_seconds: 120`, berisi `target_account`, `requested_at`, dan `reason`).
  - `LogWatcher.poll_once()` (`scripts/agy_switcher/watcher.py`) memeriksa `pending_reload.json` yang masih valid; begitu `_is_turn_quiescent()` terverifikasi pada *Safe Turn Boundary* (ADR 0016), daemon mengeksekusi `restart_language_server(force=True)`, menulis audit log, dan menghapus marker.
- Apabila daemon `LogWatcher` sedang tidak berjalan, CLI tetap mengeksekusi Disk-Only Swap + menulis marker `pending_reload.json` (TTL 120 detik) + mencetak panduan eksplisit ke stdout tanpa men-spawn proses waiter latar belakang ad-hoc.
- Eksekusi dari terminal eksternal (bukan descendant `language_server`) mempertahankan perilaku sinkron ADR 0010 sepenuhnya. Flag darurat eksplisit (`--force-restart-now`) tetap disediakan apabila operator sengaja menghendaki terminasi server seketika di tengah turn.

### 4. Failover Audit & Auth Quarantine (Dual Persistence)
- **Persistent Audit Log (`watch.log`)**: `handle_pre_invocation_hook()` (`scripts/agy_switcher/hook.py`) mengimpor `append_secure_log_entry` dari `agy_switcher.presence` dan mencatat setiap rotasi (`trigger: auth_corruption` maupun `quota_rotation`) ke `watch.log` (mode `0600`, aman symlink, terisolasi otomatis pada test runner).
- **State Terstruktur Terpisah (`cache/failover_state.json`)**: Menyimpan `last_failover` dan peta `auth_errors` dalam file terpisah berizin `0600` (bukan di `last_known_quota.json`), menjaga kompatibilitas skema sanitasi cache serta menjaga fast-path Tier 1 `<40ms` pada `hook-pre-invocation` (ADR 0020).
- **Siklus Hidup State**:
  - `auth_errors[<alias>]` bersifat persisten sampai akun benar-benar pulih (otomatis dibersihkan saat `login_account()` / `add_account()` menimpa token atau saat live probe kuota sukses).
  - `last_failover` ditampilkan di `agy-switch status` selama usia event $\le 24$ jam atau sampai manual switch baru berhasil dilakukan.
- **Transparansi UI & CLI**:
  - `ephemeralMessage` pada `hook.py` membedakan failover akibat korupsi auth (`⚠️ Auth failure on **{active_name}** ... Auto-rotated to **{target_account}** ... Run 'agy-switch login {active_name}' to repair.`) sembari tetap mempertahankan substring `"Auto-rotated to **{target_account}**"` untuk kompatibilitas mundur.
  - `agy-switch status` menampilkan `Last Failover` dan `Pending Reload` (bila aktif); `agy-switch list` menampilkan penanda `(Auth Error)`; `agy-switch doctor` Layer 2 melaporkan akun berstatus `auth_errors` (`FAIL` bila aktif, `WARN` bila standby) beserta saran `agy-switch login <alias>`.

### 5. Pelestarian 5-Layer Architecture DAG
- Seluruh perubahan mematuhi hierarki ketergantungan searah tanpa dependensi eksternal: Layer 0 (`exceptions.py`: `AccountAuthError`) $\to$ Layer 1 (`supervisor.py`, `telemetry.py`, `formatters.py`) $\to$ Layer 2 (`vault.py`, `watcher.py`, `doctor.py`, `presence.py`) $\to$ Layer 3 (`hook.py`) $\to$ Layer 4 (`cli.py`).

## Considered Options & Rejected Alternatives

- **Warn-Only pada Pre-Flight Auth Check**: Ditolak karena membiarkan token `403` aktif dan me-restart `language_server` hanya akan memicu failover diam-diam di turn berikutnya dan merusak sesi kerja pengguna.
- **Strict Fail-Closed saat Offline / Timeout**: Ditolak karena gangguan jaringan sementara atau lingkungan sandbox terisolasi tidak boleh memblokir perpindahan akun manual secara lokal.
- **Hard-Reject In-Turn Switch Tanpa Disk Swap**: Ditolak karena menyulitkan operator yang ingin menyiapkan pergantian akun dari sesi chat aktif sebelum turn berakhir.
- **Spawn Subproses Waiter Ad-Hoc saat `LogWatcher` Mati**: Ditolak karena melanggar disiplin kontrol daemon (`Daemon Mutual Exclusion`, ADR 0017) dan berisiko meninggalkan proses latar belakang liar tanpa pengelolaan PID/systemd.
- **Menyimpan Metadata Failover di `last_known_quota.json`**: Ditolak karena `_sanitize_cache_payload()` membersihkan entri non-kuota serta berisiko membebani fast-path parsing Tier 1 (`<40ms`, ADR 0020).
- **Auto-Expiry `auth_errors` Setelah 60 Menit**: Ditolak karena token yang dicabut atau mendapat `HTTP 403` tidak akan pulih sendiri seiring waktu tanpa re-login atau verifikasi probe yang berhasil.

## Consequences

**Positif:**
- Mencegah pergantian ke akun korup (`401/403`) dan mengeliminasi siklus restart-lalu-rollback yang membatalkan turn agen.
- Menjamin `switch_tokens()` bersifat atomik *check-before-write* tanpa kebocoran mutasi parsial saat `ActiveTurnRunningError`.
- Menghilangkan *mid-turn parricide* dari sub-shell agen sambil tetap mempertahankan reload instan untuk terminal eksternal (ADR 0010).
- Memberikan visibilitas penuh atas ejeksi failover hook di `watch.log`, `ephemeralMessage`, `status`, `list`, dan `doctor`.

**Negatif / Mitigasi:**
- *Tambahan Latensi ~300ms pada Manual CLI Switch*: Pemeriksaan auth langsung menambah satu panggilan HTTP singkat pada perintah `agy-switch switch <name>`. Mitigasi: menggunakan timeout ketat 2.0 detik, fail-open pada error jaringan, dan dapat dilewati kapan saja dengan `--skip-auth-check`.
- *Reload UI Tertunda saat Watcher Mati*: Pada eksekusi in-turn tanpa `LogWatcher`, UI Desktop baru memuat kredensial baru setelah server direstart manual atau turn berakhir; dimitigasi dengan instruksi stdout yang jelas dan marker `pending_reload.json` (TTL 120s).
