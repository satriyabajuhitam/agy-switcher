# 11. Headless Presence Dual-Directory Resolution and Stale Lock Pruning

Date: 2026-10-30

## Status

Accepted

## Context

Pada operasional `agy-switch` di lingkungan server headless, VPS, dan sesi Antigravity CLI murni, ditemukan tiga kendala struktural yang menghambat keandalan rotasi otomatis 24/7:

1. **Stale Presence Lock Trap pada `ProcessSupervisor`**:
   Antigravity Desktop dan CLI membuat berkas penanda sesi 0-byte berakhiran `.lock` di dalam `~/.gemini/antigravity/presence/` dan `~/.gemini/antigravity-cli/presence/`.
   Ketika proses CLI selesai, crash, atau server me-reboot, berkas-berkas lock yatim piatu ini tetap tertinggal di disk tanpa dibersihkan oleh Antigravity.
   Sebelumnya, metode `ProcessSupervisor.is_server_busy()` hanya melakukan pengecekan pasif via `p_dir.iterdir()`. Keberadaan satu saja berkas `.lock` yatim menyebabkan `is_server_busy()` selamanya mengembalikan nilai `True`. Akibatnya, perintah `switch` menolak mengganti akun dengan error `ActiveTurnRunningError` kecuali pengguna menyertakan flag `--force`.

2. **Kebutaan Sesi CLI pada `PresenceDetector` dan `LogWatcher`**:
   `PresenceDetector` sebelumnya hanya dikonfigurasi untuk memindai path Desktop (`~/.gemini/antigravity/presence` dan `~/.gemini/antigravity/brain`).
   Ketika agen atau pengguna menjalankan Antigravity via CLI murni (`agy`), sesi dan transkrip percakapan berada di bawah namespace `~/.gemini/antigravity-cli/`. Akibatnya, `LogWatcher` daemon tidak dapat menemukan berkas transkrip sesi CLI aktif dan gagal mendeteksi error kuota habis (HTTP 429) untuk memicu rotasi otomatis.

3. **Formatter Model Kuota Ter-hardcode (Gemini Only)**:
   `format_accounts_table` dan `format_accounts_listicle` meng-hardcode header kolom `GEMINI QUOTA` dan hanya memanggil ekstraktor kuota Gemini. Ketika pengguna menjalankan perintah `agy-switch quota --model claude`, parameter model diabaikan oleh antarmuka keluaran terminal.

4. **Unreachable Dead Code**:
   Terdapat perulangan duplikat yang tak terjangkau setelah baris `return False` di `scripts/agy_switcher/supervisor.py` pada metode `_poll_process_exit`.

## Decision

1. **Active File Descriptor Ownership Scan & Stale Lock Auto-Pruning (`P0-1`)**:
   - `ProcessSupervisor.is_server_busy()` tidak lagi mempercayai keberadaan berkas `.lock` secara mentah.
   - Supervisor memvalidasi apakah berkas `.lock` tersebut benar-benar dipegang oleh file descriptor (`/proc/*/fd`) dari proses yang sedang aktif berjalan di sistem.
   - Jika berkas `.lock` tidak dimiliki oleh proses hidup apa pun, berkas tersebut diklasifikasikan sebagai *stale lock*, tidak dihitung sebagai penanda server sibuk (`is_server_busy() == False`), dan secara proaktif dihapus (*pruned* via `os.unlink()`) dari disk.

2. **Unified Dual-Directory Presence Discovery (`P0-2`)**:
   - `PresenceDetector` dan `LogWatcher` mendukung pemindaian terpadu lintas namespace `~/.gemini/antigravity` (Desktop) dan `~/.gemini/antigravity-cli` (CLI).
   - Jika ditemukan sesi aktif di kedua namespace secara bersamaan, sistem secara deterministik memilih sesi dengan `st_mtime` paling baru sebagai target pemantauan utama.

3. **Universal Model Formatter Parameter (`P0-3`)**:
   - Menambahkan parameter `target_model: str = "gemini"` pada `format_accounts_table` dan `format_accounts_listicle`.
   - Meneruskan opsi `--model` dari router CLI ke formatter untuk merender header dinamis (`GEMINI QUOTA` vs `CLAUDE QUOTA`) serta membaca metrik kuota yang sesuai dengan model target.

4. **Dead Code Elimination (`P0-4`)**:
   - Menghapus perulangan unreachable di `scripts/agy_switcher/supervisor.py#L181-L194`.

## Consequences

**Positif:**
- Menghilangkan *false positive* status server sibuk pada server headless/VPS tanpa mengharuskan pengguna menggunakan flag `--force`.
- Ratusan berkas lock yatim piatu di storage dibersihkan secara higienis dan otomatis.
- `LogWatcher` daemon mampu mendeteksi rotasi kuota pada sesi CLI murni (`agy`) maupun Desktop Antigravity 2.0.
- Perintah `agy-switch quota --model claude` menampilkan tabel kuota Claude secara akurat.

**Negatif / Trade-offs:**
- Pengecekan `/proc/*/fd` memerlukan izin baca direktori proc proses pengguna. Pada Linux standar, proses milik pengguna yang sama dapat membaca `/proc/<pid>/fd` miliknya sendiri tanpa hambatan. Jika sistem berkas `/proc` tidak tersedia atau dimock saat pengujian, fallback berbasis timestamp/in-memory tetap dipertahankan.
