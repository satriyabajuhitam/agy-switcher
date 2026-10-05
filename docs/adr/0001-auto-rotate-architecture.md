# 1. Arsitektur Auto-Rotate & Fast Quota Telemetry

Date: 2026-10-05

## Status

Accepted

## Context

Proyek `agy-switch` saat ini membutuhkan mekanisme rotasi akun otomatis (auto-rotate) saat kuota model (seperti Gemini 5-hour limit) hampir habis. Pendekatan sebelumnya mengharuskan rotasi manual, dan pengecekan kuota menggunakan proses CLI `agy -p /usage` yang sangat lambat (memakan waktu hingga 30 detik) dan berpotensi mengganggu eksekusi agen.

Kita memerlukan arsitektur yang:
1. **Cepat**: Tidak memblokir alur agen.
2. **Aman**: Bekerja sinkron dengan siklus hidup agen (Agent Lifecycle).
3. **Cerdas**: Tidak membuang-buang kuota yang sebentar lagi reset, serta mengoptimalkan retensi prompt cache.
4. **Context-Aware**: Mampu membedakan model Google (Gemini) dan non-Google (Claude) karena keduanya memiliki bucket kuota yang terpisah.

## Decision

Kita telah merancang arsitektur Auto-Rotate yang memanfaatkan empat pilar utama:

1. **PreInvocation Lifecycle Hook (`hooks.json`)**
   Kita akan mendaftarkan script hook `agy-switch` (via perintah `agy-switch hook install`) ke dalam mekanisme `PreInvocation` bawaan Antigravity. Hook ini berjalan seketika *sebelum* agen mengeksekusi model, sehingga aman untuk melakukan pertukaran kredensial secara atomik (`os.replace`) tanpa me-restart Language Server di tengah eksekusi.

2. **Fast Quota Telemetry (Direct CloudCode HTTP API)**
   Kita tidak akan menggunakan subprocess CLI untuk mengecek kuota. Sebagai gantinya, kita akan membangun helper di Python yang murni menggunakan `urllib.request` untuk melakukan request POST langsung ke internal API `v1internal:retrieveUserQuotaSummary`. Klien ini akan berjalan dalam hitungan milidetik.

3. **Dynamic Model Affinity & Worst-Case Bucket Evaluation**
   Hook `PreInvocation` menerima payload JSON via `stdin` yang berisi `modelName` (misal: `gemini-3.7-flash-high` atau `claude-3-5-sonnet`). Klien kita akan mem-parsing payload ini dan mengarahkan target evaluasi secara dinamis ke grup model yang tepat (Gemini atau Claude). 
   Selain itu, karena setiap model memiliki dua *bucket* (batas 5-jam dan batas mingguan), sistem akan menggunakan **pendekatan Worst-Case (Nilai Terendah)**. Sistem akan mengambil persentase `remainingFraction` yang paling kecil di antara kedua bucket tersebut sebagai acuan utama kuota, memastikan rotasi terpicu jika salah satu batas kritis.

4. **Rotation Heuristics (`cache_optimized` & Predictive Drain)**
   `RotationEngine` tidak hanya melihat sisa kuota, tetapi menggunakan pendekatan prediktif:
   - **Cache-Optimized Default**: Selama akun saat ini memiliki kuota > 15%, rotasi akan ditahan untuk menjaga *prompt cache hit rate* agar latensi tetap rendah.
   - **Drain Window**: Jika reset kuota tinggal $\le 30$ menit dan saldo $> 5\%$, akun dipertahankan agar kuota yang hampir hangus terpakai maksimal.
   - **Imminent Reset**: Akun di pool yang akan reset dalam $\le 45$ menit dan saldo $> 10\%$ akan diprioritaskan untuk dipakai.

Konfigurasi global untuk fitur ini (Enable/Disable) disimpan di `~/.antigravity-vault/config.json` dan dikontrol lewat sub-command `agy-switch config auto-switch [on|off]`.

## Consequences

**Positif:**
- Pergantian akun terjadi transparan (tanpa campur tangan user) berkat *PreInvocation hook*.
- Kinerja agen tidak terhambat karena fast-telemetry sangat ringan dan tidak spawn subproses.
- Hemat biaya dan latensi karena cache terjaga lewat aturan `cache_optimized`.
- Kuota terutilisasi optimal dengan algoritma *drain window*.

**Negatif/Risiko:**
- Kita harus secara manual menangani token OAuth parsing dan HTTP requests menggunakan standard library tanpa `requests`, yang meningkatkan sedikit kompleksitas parser.
- Google sewaktu-waktu bisa mengubah format payload dari internal CloudCode API.
