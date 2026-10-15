# 10. Exhaustion-Triggered Force Reload and Post-Restart Cursor Seeking

Date: 2026-10-15

## Status

Accepted

## Context

Pada integrasi `agy-switch` dengan Antigravity 2.0 Desktop app, fitur auto-switch menghadapi kendala sinkronisasi status UI dan sesi proses:
1. **In-Memory Credential Caching pada Language Server**:
   Aplikasi Antigravity 2.0 Desktop berkomunikasi secara langsung dengan proses latar belakang `language_server`. Saat `language_server` pertama kali berjalan, ia membaca berkas kredensial `~/.gemini/jetski-standalone-oauth-token` dan menyimpan token sesi OAuth di memori proses.
2. **Kegagalan Pembaruan UI Tanpa Restart Server**:
   Sebelumnya, implementasi rotasi otomatis pada `LogWatcher` (`scripts/agy_switcher/watcher.py`) dan PreInvocation hook memanggil `switch_tokens(target_account, restart_server=False, force=True)` demi mematuhi aturan umum *Active Turn Guarding* (ADR 0001). Meskipun berkas kredensial di disk (`jetski-standalone-oauth-token` dan `antigravity-oauth-token`) berhasil diganti secara atomik, ketiadaan restart pada `language_server` menyebabkan antarmuka (UI) Antigravity 2.0 tidak memperbarui avatar/email akun aktif.
3. **Model Terhenti Saat Kuota Habis Total (0%)**:
   Karena `language_server` masih mempertahankan token akun lama yang telah habis kuotanya, panggilan inferensi lanjutan memicu error HTTP 429 (`RESOURCE_EXHAUSTED`). Model berhenti merespons di tengah percakapan, dan UI menampilkan peringatan kuota habis meskipun vault telah menunjuk akun baru.
4. **Blokir Presence Lock pada CLI `switch --auto`**:
   Saat jendela Antigravity 2.0 Desktop terbuka, berkas presence lock (`~/.gemini/antigravity/presence/*.lock`) selalu ada. Perintah CLI `agy-switch switch --auto` yang dijalankan pengguna gagal dengan `ActiveTurnRunningError` karena flag `--force` sebelumnya bernilai `False` secara default.

## Decision

1. **Exhaustion-Triggered Force Reload pada LogWatcher Daemon**:
   - Ketika `LogWatcher` mendeteksi error kuota `RESOURCE_EXHAUSTED` (kode 429) pada transkrip atau log CLI, daemon mengidentifikasi bahwa turn model telah terhenti akibat batasan penyedia.
   - `LogWatcher` akan mengeksekusi rotasi dengan `engine.switch_tokens(target_account, restart_server=True, force=True)`.
   - Supervisor akan mengirim sinyal `SIGTERM` secara terpaksa (`force=True`) ke `language_server`. WSL Relay / proses supervisor Antigravity secara otomatis akan me-respawn `language_server` baru.
   - Saat proses baru aktif, ia membaca berkas `jetski-standalone-oauth-token` yang baru diperbarui, menyinkronkan UI Antigravity 2.0 secara instan, dan memungkinkan percakapan dilanjutkan tanpa peringatan kuota.

2. **Default `--force` pada CLI `agy-switch switch --auto`**:
   - Mengubah perilaku default router CLI pada perintah `switch --auto`: jika pengguna tidak menyertakan `--no-restart`, opsi `force` secara otomatis bernilai `True`.
   - Hal ini memastikan presence lock pasif dari jendela IDE yang sedang terbuka tidak memblokir auto-switch yang sah.

3. **Perlindungan PreInvocation Hook (Preservation of Zero-Restart Hook)**:
   - `PreInvocation` hook (`scripts/agy_switcher/hook.py`) tetap mempertahankan `restart_server=False`.
   - Alasan: Hook dieksekusi sebagai child sub-proses dari `language_server` sesaat sebelum memanggil tool. Mematikan `language_server` dari dalam hook akan menyebabkan crash seketika pada turn aktif. Jika kuota benar-benar habis di tengah turn, `LogWatcher` yang akan menangani failover dan me-reload `language_server`.

4. **Post-Restart Cursor Seeking & Strict Damping Interval**:
   - Untuk mencegah perulangan deteksi 429 dari entri log lama yang tersisa (kondisi *switch storm* / *restart loop*), `LogWatcher` langsung memajukan file cursor transkrip ke akhir berkas (EOF / seek to end) sesaat setelah pergantian berhasil.
   - Menegakkan `damping_interval` sebesar 15 detik pasca-rotasi, di mana seluruh log 429 yang mungkin tersisa dalam buffer stream diabaikan sepenuhnya.

5. **Notifikasi Sistem Transparan (Desktop Notification)**:
   - Memperbarui pesan `notify-send` agar secara eksplisit mengabarkan pengguna:
     `🔄 Antigravity switched to <account> & reloaded language_server (quota 429)`
   - Mencatat seluruh jejak rotasi dan reload secara persisten ke `~/.antigravity-vault/watch.log`.

## Consequences

**Positif:**
- Antarmuka Antigravity 2.0 Desktop app otomatis memperbarui akun aktif saat kuota akun lama habis.
- Pengguna tidak lagi mengalami kondisi model terhenti atau peringatan kuota macet saat melanjutkan percakapan.
- Bebas dari restart loop berkat perpaduan EOF cursor seeking dan damping interval 15 detik.
- CLI `agy-switch switch --auto` menjadi andal dan bebas friksi tanpa perlu mengingat flag `--force`.

**Negatif/Risiko:**
- Terjadi jeda koneksi singkat (~1-2 detik) pada jendela IDE saat `language_server` me-respawn via WSL Relay. Dampak ini sangat minimal dan dapat diterima karena model pada saat itu sudah terhenti akibat error 429.
