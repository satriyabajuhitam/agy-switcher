# 15. Gemini-Exclusive Architecture and Transcript-Bounded Watcher

Date: 2026-11-11

## Status

Accepted

## Context

Dalam pengoperasian daemon pemantau kuota (`LogWatcher`) dan mesin rotasi akun (`agy-switcher`), ditemukan dua masalah arsitektural penting:

1. **False-Positive Quota Trigger dari Log CLI**:
   - `PresenceDetector.get_watch_targets()` sebelumnya memindai file transkrip aktif sekaligus seluruh berkas log CLI mentah di `~/.gemini/antigravity-cli/log/*.log`.
   - Berkas log CLI tersebut mencatat seluruh keluaran terminal (stdout dan stderr). Ketika pengguna atau agen menjalankan perintah seperti unit test (`tests/test_watch.py`, `scripts/verify-build.sh`), `git diff`, atau `grep` yang mencetak teks tiruan seperti `RESOURCE_EXHAUSTED` atau `code 429`, watcher menangkap teks tersebut tanpa validasi struktur JSON.
   - Hal ini memicu pergantian akun otomatis padahal akun aktif masih memiliki sisa kuota 80%–100%.

2. **Rotasi Beruntun (*Chain-Switching & Cooldown Cascade*)**:
   - Setiap kali terjadi deteksi 429 (termasuk false positive), akun aktif langsung dimasukkan ke `cooldown.json` dengan penalti jeda selama 60 menit.
   - Dengan jeda peredam (*damping window*) yang hanya 15 detik, baris error berikutnya dari proses yang sama langsung memicu rotasi ke akun ketiga, keempat, dan seterusnya secara beruntun.

3. **Depresiasi Model Non-Gemini di Ekosistem Antigravity**:
   - Model pihak ketiga (Claude/Anthropic) akan segera ditiadakan dari layanan Antigravity.
   - Mempertahankan logika pemisahan keluarga model, penanganan cooldown multi-model, serta inferensi model ganda di `RotationEngine`, `telemetry.py`, dan `api.py` menambah kompleksitas yang tidak lagi relevan dan rawan menimbulkan kebocoran evaluasi kuota (*cross-model leakage*).

## Decision

Kami memutuskan untuk mereorganisasi arsitektur `agy-switch` dengan tiga pilar utama:

### 1. Transcript-Bounded Watching
- **Eksklusi File Log Mentah**: Menghentikan pemantauan file `~/.gemini/antigravity-cli/log/*.log` dari `PresenceDetector.get_watch_targets()`.
- **Transkrip Saja**: Watcher strictly hanya memantau berkas sesi aktif `transcript.jsonl` (baik sesi Desktop maupun CLI) yang memiliki skema terstruktur.
- **Validasi Skema JSON**: Deteksi kuota hanya dievaluasi jika baris valid JSON memiliki `status == "ERROR"`, `type == "ERROR_MESSAGE"`, atau payload `error` non-kosong, serta mengabaikan status `DONE` dan tipe `USER_INPUT`. Hal ini menjamin 100% kekebalan terhadap teks output terminal dan unit test.

### 2. Arsitektur Eksklusif Gemini (*Hard Retirement of Non-Gemini Models*)
- **Pembersihan Telemetri & Cache**:
  - Menghapus pemindaian kelompok kuota Claude, sub-kunci cache `claude`, dan parser kuota Anthropic di `telemetry.py`.
  - Struktur cache `last_known_quota.json` disederhanakan hanya menyimpan metrik kuota Gemini.
- **Penyederhanaan Mesin Rotasi (`RotationEngine`)**:
  - Menghapus percabangan model ganda, normalisasi token Claude, dan logika penalti cooldown multi-model.
  - Seluruh algoritma evaluasi (`auto_highest`, `imminent_reset`, `drain_window`) dioptimalkan secara eksklusif untuk kuota Gemini.
- **Pembersihan Kontrak CLI & Vault Config**:
  - Menghapus opsi `--model claude` dan argumen `default-model claude`. Default model menjadi mutlak `gemini`.
  - Dokumentasi dan formatter CLI disesuaikan hanya menampilkan metrik kuota Gemini.

### 3. Pre-Switch Quota Probe & Perpanjangan Damping Window
- **Verifikasi Kuota Sebelum Switch (*Pre-Switch Probe*)**:
  - Ketika watcher mendeteksi indikator `RESOURCE_EXHAUSTED` pada transkrip, sebelum memanggil `switch_tokens`, watcher memvalidasi sisa kuota Gemini akun aktif via telemetri cepat / cache.
  - Jika sisa kuota Gemini masih di atas batas ambang sehat (>25%), perintah pergantian dibatalkan (menganggap 429 sebagai anomali sesaat / non-quota glitch) dan akun aktif dipertahankan.
- **Perpanjangan Damping Window**:
  - Nilai default `damping_seconds` dinaikkan dari 15 detik menjadi **60 detik** guna meredam lonjakan error beruntun dan mencegah efek domino pergantian akun.

## Consequences

### Positif:
- **Zero False-Positive Switches**: Output terminal, test suite, dan log acak tidak akan pernah lagi memicu pergantian akun yang tidak diinginkan.
- **Eliminasi Chain-Switching**: Kombinasi verifikasi sisa kuota aktif dan damping 60 detik mencegah akun diganti beruntun dalam hitungan detik.
- **Kode Ramping & Cepat**: Penghapusan model non-Gemini memangkas ratusan baris kode percabangan, mengurangi overhead I/O cache, dan menghilangkan potensi bug *cross-model quota leakage*.

### Negatif / Mitigasi:
- *CLI Non-Session Execution*: Perintah terminal mandiri yang tidak menghasilkan file `transcript.jsonl` tidak akan dipantau oleh `LogWatcher`.
  - *Mitigasi*: Untuk proses otomatis non-interaktif, kegagalan ditangani secara reaktif oleh `TurnRunner` via In-Process SDK (`auto_rotate_on_exhaustion`), bukan oleh daemon watcher.
- *Breaking Change Opsi Model*: Pemanggilan CLI dengan argumen `--model claude` akan menghasilkan pesan kesalahan.
  - *Mitigasi*: Dokumentasi CLI dan skill diperbarui secara serentak.
