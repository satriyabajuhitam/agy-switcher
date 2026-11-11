# 16. Proactive Threshold Failover and Safe Turn Boundary

Date: 2026-11-11

## Status

Accepted

## Context

Pada integrasi `agy-switch` dengan Antigravity 2.0 Desktop / Web (Remote Control / Jetski Language Server) dan Antigravity CLI, evaluasi empiris terhadap kegagalan auto-switch pada sisa kuota rendah (misalnya 14%) mengungkap kesenjangan arsitektural yang kritis:

1. **Absennya Eksekutor Hook pada Language Server**:
   Konfigurasi `auto_switch: true` pada `~/.antigravity-vault/config.json` mengandalkan hook `PreInvocation` (`hook-pre-invocation` di `hooks.json`). Namun, eksekutor hook (`hooks_manager.go`) secara eksklusif hanya berjalan di dalam biner CLI (`agy`). Pada sesi Antigravity Web / Desktop yang digerakkan oleh proses latar belakang `language_server`, hook `PreInvocation` tidak pernah dieksekusi sebelum giliran prompt.
2. **Keterbatasan Disk-Only Swap pada Hook**:
   Sesuai ADR 0010, hook `PreInvocation` menjalankan `switch_tokens(..., restart_server=False)`. Namun, `language_server` menyimpan kredensial OAuth di dalam memori proses (*in-memory credential caching*). Pergantian token di disk tanpa memuat ulang server menyebabkan Language Server tetap menggunakan token akun lama yang sekarat, dan UI Antigravity tidak memperbarui status akun.
3. **Keterbatasan Reaktif pada Daemon Watcher**:
   Daemon latar belakang (`LogWatcher`) sebelumnya bersifat murni reaktif terhadap error HTTP 429 (`RESOURCE_EXHAUSTED`). Selama kuota akun aktif belum 0% (seperti pada sisa 14%), Google belum melempar error 429, sehingga watcher tidak terpicu dan akun tidak pernah berpindah secara preventif.
4. **False Lockout pada Manajemen Cooldown**:
   Ketika akun aktif mengalami limit kuota, `watcher.py` menetapkan waktu `cooldown_until` berdasarkan waktu reset mingguan (dapat mencapai 6–7 hari ke depan). Selain itu, logika penyaringan akun di watcher (`if acc_name in cooldowns: continue`) mengabaikan status pemulihan kuota riil, mengunci akun-akun cadangan yang sebenarnya memiliki kuota melimpah (80%–100%) dari rotasi.

## Decision

Kami memutuskan untuk memperbarui arsitektur failover `agy-switch` dengan empat pilar terpadu:

### 1. Proactive Threshold Failover di Dalam Daemon Watcher
- Memindahkan tanggung jawab deteksi kuota preventif ke dalam daemon latar belakang (`LogWatcher`), menjadikannya mekanisme *single-source-of-truth* yang bekerja seragam di Antigravity Desktop, Web, dan CLI tanpa bergantung pada lifecycle hook IDE.
- Daemon `LogWatcher` memantau sisa kuota akun aktif secara berkala dan mengeksekusi rotasi proaktif jika kuota akun aktif berada di bawah ambang batas kritis (default: $\le 20\%$, dapat dikonfigurasi melalui `quota_threshold`).

### 2. Deteksi Batas Giliran Aman (*Safe Turn Boundary*)
- Mencegah gangguan terhadap inferensi atau tool call yang sedang berjalan:
  - Watcher memantau transkrip aktif (`transcript.jsonl`) dan mendeteksi penyelesaian turn ketika sebuah langkah mencatat `status == "DONE"` atau `type == "USER_INPUT"` (menandakan model telah selesai merespons dan sedang menunggu prompt pengguna berikutnya).
  - Watcher memverifikasi status proses Language Server melalui `ProcessSupervisor.is_server_busy()`.
- Rotasi proaktif dan pemuatan ulang Language Server (`restart_server=True, force=True`) **HANYA** dieksekusi pada *Safe Turn Boundary* (saat server dalam kondisi *idle* di jeda antar turn), memastikan turn aktif tidak pernah terputus.

### 3. Heuristik Threshold Cerdas dengan Preservasi Drain Window
- Rotasi proaktif hanya dijalankan jika:
  1. Kuota akun aktif saat ini $\le \text{threshold}$ (misal $\le 20\%$).
  2. Terdapat minimal satu akun cadangan (*standby*) yang memiliki kuota sehat ($\ge 70\%$).
  3. Akun aktif **TIDAK** berada dalam jendela *Drain Window* (jika sisa waktu reset kuota akun aktif $\le 30$ menit dan saldo $> 5\%$, akun tetap dipertahankan agar kuota yang hampir hangus terpakai maksimal).

### 4. Kekebalan Cooldown Berbasis Telemetri (*Telemetry-Aware Cooldown Immunity*)
- Menghapus pemblokiran kaku `if acc_name in cooldowns: continue` pada `watcher.py`.
- **Kekebalan Telemetri**: Jika sebuah akun yang tercatat di `cooldown.json` terbukti memiliki kuota sehat ($\ge 70\%$) berdasarkan telemetri segar atau cache mutakhir, akun tersebut secara otomatis dinyatakan kebal (*exempt*) dari penalti cooldown dan sah untuk dipilih sebagai target rotasi.
- **Plafon Cooldown 5-Jam**: Durasi cooldown reaktif akibat error 429 dibatasi maksimal mengikuti siklus reset bucket rolling 5-jam (atau fallback 30 menit), dan **dilarang** menggunakan timestamp reset mingguan, mencegah akun terkunci selama berhari-hari.

## Consequences

### Positif:
- **Zero-Interruption Experience**: Pergantian akun terjadi secara proaktif dan mulus di jeda antar turn sebelum pengguna mengalami error HTTP 429 di tengah percakapan.
- **Universal Desktop & Web Compatibility**: Bekerja 100% andal di Antigravity 2.0 Web / Desktop tanpa memerlukan dukungan hook internal dari `language_server`.
- **UI Sinkron Seketika**: Karena rotasi pada Safe Turn Boundary me-reload Language Server yang sedang idle, antarmuka Antigravity Desktop langsung memuat akun baru beserta avatar dan sisa kuota segarnya.
- **Optimalisasi Armada Akun**: Eliminasi false lockout mengembalikan akun-akun berkuota tinggi yang sebelumnya terkunci ke dalam pool rotasi aktif.

### Negatif / Mitigasi:
- *Overhead Telemetry Ringan*: Pemeriksaan kuota pada akhir turn menambahkan panggilan HTTP singkat (~300ms) saat turn selesai, namun diminimalisasi dengan cache lokal 5 menit jika kuota sebelumnya tercatat tinggi.
- *Jeda Reload 1-2 Detik*: Restart Language Server pada Safe Turn Boundary membutuhkan jeda ~1 detik sebelum giliran pengguna berikutnya diproses, yang tidak terasa karena terjadi saat pengguna sedang membaca respons atau mengetik prompt baru.
