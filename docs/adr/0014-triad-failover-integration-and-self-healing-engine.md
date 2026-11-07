# 14. Triad Failover Integration and Self-Healing Engine

Date: 2026-11-06

## Status

Accepted

## Context

Dalam mewujudkan asisten pribadi otonom 24/7 (ala OpenClaw) berbasis ekosistem Antigravity CLI, tiga komponen independen dioperasikan secara sinergis:
- **`agy-switcher`**: Multi-account OAuth vault manager dan failover engine.
- **`agy-telemote`**: Telegram bot gateway dan asynchronous turn runner.
- **`agy-memory`**: Pengelola persona (`SOUL.md`), direktif, dan SQLite FTS5 failure recall.

Tantangan utama operasional adalah **penanganan kehabisan kuota model (HTTP 429 / `RESOURCE_EXHAUSTED`)**:
1. **Kegagalan Mid-Turn**: Kuota dapat habis di tengah proses inferensi atau streaming token yang panjang. Pemeriksaan kuota secara proaktif sebelum setiap giliran (*pre-turn check*) tidak hanya menambah latensi 1–2 detik pada setiap interaksi Telegram, tetapi juga tetap rentan terhadap kegagalan mid-turn tersebut.
2. **Preservasi Konteks Percakapan**: Giliran yang gagal tidak boleh merusak riwayat percakapan (`conversation_id`). Rotasi token harus dapat melanjutkan sesi yang sama tanpa memicu reset percakapan bagi pengguna Telegram.
3. **Invarian Keterpisahan Mutlak (*Strict Loose Coupling*)**: Ketiga subsistem wajib dapat diinstal dan dijalankan 100% mandiri tanpa dependensi wajib di `pyproject.toml` (Zero External Dependencies).
4. **Respon Saat Seluruh Armada Habis (*Fleet Exhaustion*)**: Jika semua akun telah habis terpakai, sistem membutuhkan mekanisme refleksi diri (*self-reflection*) agar asisten mengenali status keterbatasan ini dan memberikan estimasi waktu pulih yang akurat kepada pengguna.

## Decision

Kami memutuskan untuk mengintegrasikan mesin failover mandiri berprinsip *Graceful Enhancement* dengan rincian arsitektur sebagai berikut:

### 1. Reaktif Mid-Turn Failover Loop pada `TurnRunner`
- `TurnRunner` pada `agy-telemote` menjalankan subproses `agy` tanpa latensi pre-turn check.
- **Deteksi Ganda (*Dual-Inspection Detection*)**:
  - Tangkap status error dari event stream JSON (`event == "result"` dengan `status == "ERROR"` atau `error` mengandung indikator kuota).
  - Tangkap kode keluar subproses (`returncode != 0`) dan pola `stderr` (`RESOURCE_EXHAUSTED`, HTTP `429`, atau `quota`).
- **In-Process Failover Execution**:
  - Melakukan *safe soft-import* ke `agy_switcher.api`. Jika tidak terpasang, teruskan pesan kuota habis ke pengguna secara anggun.
  - Jika terpasang, panggil `switcher_api.auto_rotate_on_exhaustion(model=mapped_model, reload_server=False)`. Mekanisme ini menggunakan **Disk-Only Swap**, sehingga token baru langsung aktif di disk tanpa menyentuh supervisor proses desktop.

### 2. Bounded Retry & Preservasi Konteks Percakapan
- **Batas Pengulangan (*Retry Limit*)**: Dibatasi maksimal **2 kali percobaan** per giliran (atau $\min(2, \text{jumlah\_akun} - 1)$) guna mencegah *infinite loop* apabila error dipicu oleh anomali non-kuota.
- **Identical Conversation Reuse**: Mengingat direktori sesi `~/.gemini/antigravity-cli/brain/<uuid>/` bersifat agnostik terhadap akun OAuth, subproses retry dieksekusi dengan argumen `--conversation <id>` yang identik sehingga konteks dialog tetap utuh 100%.
- **Transparansi UI**: Mengirimkan status transien ke Telegram (`sink.on_status("🔄 Kuota akun habis, beralih ke akun cadangan...")`) untuk memberi tahu pengguna secara halus tanpa mencemari riwayat chat utama.

### 3. Protokol Fleet Exhaustion & Emisi Panic Event
- Ketika seluruh akun di vault berada dalam masa cooldown atau kehabisan kuota (`success == False`):
  1. **Refleksi Kegagalan ke `agy-memory`**: Jika `agy-memory` terpasang, catat structured `Failure Report` (Kategori: `failure`, Confidence: 0.9, Judul: `Fleet Quota Exhaustion`) ke dalam SQLite FTS5. Melalui ADR-0014 `agy-memory`, laporan ini akan otomatis di-recall saat sesi berikutnya dimulai setelah kuota pulih.
  2. **Notifikasi Telegram Berbasis ETA**: Menghitung waktu reset terdekat dari seluruh armada menggunakan `get_fleet_quota_summary()` dan menyajikannya dalam pesan ramah ke pengguna (contoh: *"Seluruh kuota akun sedang dalam masa tunggu. Kuota diperkirakan pulih pada pk 12:30 (15 menit lagi)."*).

### 4. Perintah Interaktif Telegram (`/quota` & `/switch`) dan Alert Daemon
- Handler `/quota` dan `/switch` memanfaatkan fungsi In-Process SDK (`get_fleet_quota_summary`, `switch_account`):
  - `/quota` menyajikan ringkasan kuota akun aktif dan cadangan secara visual.
  - `/switch` menampilkan inline keyboard buttons untuk pergantian akun instan oleh admin.
- Daemon `watcher.py` di `agy-switcher` menyediakan kanal alert opsional non-blocking melalui variabel lingkungan (`TELEGRAM_ALERT_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID`) atau webhook untuk memberitahukan rotasi background otomatis tanpa mengikat dependensi ke basis data `agy-telemote`.

## Consequences

### Positif:
- **Zero Turn Drop**: Kegagalan kuota 429 diatasi secara otomatis dan transparan tanpa disrupsi percakapan pengguna.
- **Latensi Optimal**: Bebas dari overhead polling kuota sebelum giliran; failover hanya berjalan saat error benar-benar terjadi.
- **Kemandirian Modul Tetap Terjaga**: Sinergi Triad berjalan murni melalui *safe soft-import* dan *graceful degradation*. Jika satu atau dua komponen tidak terpasang, sistem yang ada tetap bekerja normal 100%.
- **Kecerdasan Reflektif**: `agy-memory` menangkap riwayat kehabisan armada sebagai pelajaran untuk menuntun perilaku asisten di masa mendatang.

### Negatif / Mitigasi:
- *Jeda Eksekusi Saat Failover*: Eksekusi giliran yang mengalami retry membutuhkan waktu sedikit lebih lama karena harus meluncurkan subproses baru. Dimitigasi dengan pengiriman sinyal status transien visual (`on_status`) ke Telegram.
- *Batas Retry*: Kegagalan yang berulang lebih dari 2 kali akan langsung memicu Panic Event dan penghentian turn demi melindungi armada dari pemborosan kuota berantai.
