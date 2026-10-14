# 7. Presence-Aware Watcher & Siklus Hidup Daemon

Date: 2026-10-14

## Status

Accepted

## Context

Pada roadmap `agy-switch` (Pilar 3: Log Watcher Cerdas Berbasis Presence Desktop & Pengerasan Izin), evaluasi terhadap implementasi awal `scripts/watch.py` mengungkap sejumlah keterbatasan arsitektur dan celah keandalan runtime:

1. **Jalur Log Naif & Absennya Deteksi Desktop**:
   Implementasi `watch.py` sebelumnya memindai folder non-eksisten `~/.gemini/antigravity/log` dan hanya mengandalkan log CLI (`~/.gemini/antigravity-cli/log/*.log`). Sementara itu, sesi percakapan aktif Antigravity Desktop menulis transkrip ke `~/.gemini/antigravity/brain/<session-id>/.system_generated/logs/transcript.jsonl`.
2. **Risiko I/O Storm pada 190+ Riwayat Sesi**:
   Direktori `~/.gemini/antigravity/brain/` menampung ratusan subfolder sesi historis. Memindai seluruh berkas transkrip secara berkala (*brute-force scan*) akan memicu lonjakan disk I/O dan konsumsi CPU yang sangat tinggi. Diperlukan mekanisme penargetan selektif yang hanya menelusuri sesi yang benar-benar sedang aktif.
3. **False Positive pada Deteksi Kuota Habis**:
   Menggunakan regex sederhana pada baris mentah rentan memicu rotasi palsu (*false switch*) apabila prompt pengguna, respons model, atau kode yang sedang diinspeksi agen sekadar menyebutkan teks string `RESOURCE_EXHAUSTED` atau `429`.
4. **Celah Keamanan Izin Berkas Cooldown**:
   Penulisan `cooldown.json` belum menerapkan pengerasan izin berkas (mode `0600`), berpotensi terbaca oleh proses atau pengguna lain di mesin bersama (*shared host* / multi-user).
5. **Cooldown Statis vs Siklus Reset Kuota**:
   Penerapan durasi cooldown tetap (flat 60 menit) tidak selaras dengan telemetri reset kuota riil; akun berpotensi dirotasi kembali sebelum kuota mingguan/5-jam reset, atau sebaliknya tertahan terlalu lama padahal kuotanya telah pulih dalam 10 menit.
6. **Ketiadaan Manajemen Siklus Hidup Daemon**:
   `watch.py` hanya dapat dijalankan di foreground terminal. Belum ada mekanisme kontrol terintegrasi (`start`, `stop`, `status`), pencegahan duplikasi proses via PID lock, maupun integrasi dengan `systemd --user`.

## Decision

1. **Multi-Tier Presence-Aware Discovery**
   Menentukan direktori sesi aktif Antigravity Desktop secara presisi tanpa I/O storm melalui strategi 2 tingkat:
   - **Tier 1 (Proc FD Inspection)**: Memeriksa symlink file descriptor `/proc/<pid>/fd/*` dari proses `language_server` yang mengarah ke berkas kunci `~/.gemini/antigravity/presence/<session-id>.lock`. Pendekatan ini 100% deterministik membuktikan sesi yang sedang dikontrol IDE.
   - **Tier 2 (Fallback via Lock mtime)**: Jika pembacaan `/proc` dibatasi lingkungan sandbox/container, sistem memilih berkas `.lock` dengan timestamp modifikasi (`mtime`) paling mutakhir di dalam `~/.gemini/antigravity/presence/`.
   - Mengarahkan tailing transkrip secara spesifik hanya ke `~/.gemini/antigravity/brain/<active-id>/.system_generated/logs/transcript.jsonl`, digabungkan dengan log CLI aktif di `~/.gemini/antigravity-cli/log/*.log`.

2. **Structured JSON-First Event Filtering**
   - Untuk berkas `transcript.jsonl`, setiap baris yang berawalan `{` diparse sebagai JSON.
   - Sistem HANYA mengevaluasi pola kehabisan kuota (`RESOURCE_EXHAUSTED`, `code 429`, `Individual quota reached`, `weekly quota reached`) jika event memiliki status `status == "ERROR"`, tipe `ERROR_MESSAGE`, atau berada dalam payload field `error`.
   - Untuk berkas log CLI mentah (`*.log`), regex tetap diterapkan langsung pada baris teks.

3. **Dynamic Reset Cooldown & Pengerasan Izin Berkas Mode `0600`**
   - Penulisan `~/.antigravity-vault/cache/cooldown.json` dilakukan secara atomik melalui penulisan berkas temporer berizin `0600` (`os.open(..., os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)`) diikuti `os.replace`.
   - Durasi cooldown dihitung dinamis berdasarkan nilai `reset_time` akun terkait dari `last_known_quota.json` (`cooldown_until = reset_time`). Jika metadata `reset_time` tidak tersedia atau bernilai lampau, sistem menggunakan durasi default 60 menit.

4. **Strict Damping Interval (Pencegahan Switch Storm)**
   - Menetapkan interval redaman minimum 15 detik pasca-rotasi.
   - Selama jendela 15 detik ini, seluruh event error 429 lanjutan diabaikan untuk meredam sisa stream error dari giliran model yang tertunda (*lagging chunk stream*).

5. **Manajemen Siklus Hidup Daemon (`agy-switch watch`)**
   - Menambahkan subcommand CLI `watch` dengan aksi:
     - `run`: Menjalankan watcher secara interaktif di foreground.
     - `start`: Memulai daemon di background via subproses terpisah (`start_new_session=True`), mencatat PID ke `~/.antigravity-vault/watch.pid` (mode `0600`), dan mengarahkan output log ke `~/.antigravity-vault/watch.log`.
     - `stop`: Mengirimkan sinyal `SIGTERM` ke PID yang tercatat di `watch.pid`, menunggu hingga 5 detik untuk graceful shutdown, mengirim `SIGKILL` jika hang, dan membersihkan berkas PID.
     - `status`: Memeriksa apakah daemon sedang berjalan via `os.kill(pid, 0)` dan menampilkan info sesi yang sedang dipantau.
     - `--generate-systemd`: Menghasilkan berkas unit layanan systemd user (`~/.config/systemd/user/agy-switch-watcher.service`) untuk autostart otomatis saat sistem booting/login.

6. **Notifikasi Visual Desktop Non-Blocking**
   - Saat rotasi otomatis berhasil dieksekusi di tengah turn, watcher memanggil `notify-send` secara asinkron (jika terpasang di sistem Linux/WSL) untuk memberikan umpan balik visual singkat ke pengguna tanpa memblokir alur utama tailing.

## Considered Options & Rejected Alternatives

- **Brute-Force Polling Seluruh 190+ Transkrip**: Ditolak karena memicu ribuan disk read/stat per detik yang membebani filesystem I/O dan memperlambat responsivitas IDE.
- **Pure Raw Regex pada Transkrip JSONL**: Ditolak karena agen sering membaca, menulis, atau mendiskusikan error 429 dalam kode/prompt, memicu rotasi palsu yang tidak perlu.
- **Static Fixed 60-Minute Cooldown**: Ditolak karena batas kuota mingguan memerlukan masa tunggu lebih panjang, sedangkan batas 5-jam yang hampir reset hanya butuh beberapa menit.
- **In-Process POSIX Double-Fork (`os.fork`)**: Ditolak demi menjaga portabilitas dan menyederhanakan pengelolaan state memori Python; subproses terpisah via `start_new_session=True` jauh lebih mudah diaudit dan diisolasi.
- **Restart Language Server saat 429 Mid-Turn**: Ditolak sesuai aturan mutlak *Turn Safety & Process Supervisor* (ADR 0001); pembaruan token dilakukan murni secara atomik pada berkas kredensial, sehingga giliran berikutnya atau retry pengguna langsung menggunakan akun baru tanpa menghentikan IDE.

## Consequences

**Positif:**
- Deteksi kegagalan kuota Desktop berlangsung instan (sub-detik) tanpa I/O storm pada riwayat sesi.
- Bebas dari false positive berkat filtering terstruktur JSON status `ERROR`.
- Keamanan kredensial dan cache terjamin dengan pengerasan izin mode `0600` pada `cooldown.json` dan `watch.pid`.
- Manajemen daemon yang ergonomis bagi pengguna Linux/WSL via CLI standar (`watch start/stop/status`) maupun `systemd`.
- Akurasi rotasi meningkat drastis dengan dynamic cooldown berbasis `reset_time`.

**Negatif/Risiko:**
- Membutuhkan penanganan khusus jika struktur berkas transkrip internal Antigravity Desktop mengalami perubahan skema di masa depan (mitigasi: fallback gracefully ke text regex).
- Pada lingkungan tanpa `/proc` (misal restricted jail), resolusi sesi mengandalkan timestamp `mtime` berkas `.lock`.
