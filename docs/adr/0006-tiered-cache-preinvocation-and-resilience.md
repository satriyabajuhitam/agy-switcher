# 6. Optimasi Performa Tiered Cache & Ketahanan PreInvocation Hook

Date: 2026-10-14

## Status

Accepted

## Context

Pada roadmap `agy-switch` (Pilar 2: Optimasi Performa & Ketahanan PreInvocation Hook), evaluasi mendalam terhadap handler hook (`handle_pre_invocation_hook`) mengungkap sejumlah kelemahan performa dan celah keandalan runtime:

1. **Latensi Turn Agen yang Tinggi (Greedy Full Polling)**:
   Pada setiap turn sebelum model dijalankan, `handle_pre_invocation_hook` mengeksekusi pemanggilan HTTP konkuren ke seluruh akun terdaftar via `fetch_all_accounts_quota`. Panggilan jaringan ini menambahkan overhead latensi 300ms hingga 2.000ms pada setiap giliran prompt agen, bahkan ketika akun aktif masih memiliki kuota 90%+ yang baru saja diverifikasi.
2. **Ketiadaan Persistensi Cache pada Hook**:
   Hook tidak pernah memanggil `client.save_cache()`. Akibatnya, telemetri kuota segar yang didapatkan saat hook berjalan tidak pernah disimpan ke `~/.antigravity-vault/cache/last_known_quota.json`, membuang informasi berharga untuk evaluasi selanjutnya dan untuk CLI (`status` / `quota`).
3. **False Alarm 'All Accounts Exhausted' saat Jaringan Terganggu**:
   Logika hook sebelumnya menganggap kondisi `not quotas` (yang terjadi saat timeout, DNS error, atau offline) sama dengan kehabisan kuota total (`all_exhausted = True`). Hal ini menyebabkan sistem menginjeksi pesan peringatan palsu `⚠️ All Antigravity accounts have exhausted their quota` ke agen, padahal akun masih memiliki kuota penuh dan hanya koneksi telemetri yang terputus sesaat.
4. **Stale ISO Timestamp Expiry saat Refresh Token OAuth**:
   Ketika access token kedaluwarsa (HTTP 401), method `refresh_access_token` berhasil meminta token baru ke endpoint Google OAuth namun mengabaikan nilai `expires_in` dari respon JSON. Akibatnya, field `expiry` di `token.json` tetap menyimpan timestamp lama yang sudah basi, membingungkan subsistem diagnostik dan pengawas integritas token.

## Decision

1. **Tiered Cache-First Polling (3 Tingkat)**
   Menerapkan pola polling adaptif bertingkat pada `handle_pre_invocation_hook`:
   - **Tier 1 (Healthy Pass - 0ms Overhead)**:
     Jika akun aktif memiliki data kuota di cache lokal dengan sisa kuota sehat $\ge 25\%$ dan usia cache masih segar (TTL $\le 5$ menit / 300 detik), hook langsung melakukan bypass 0ms (`return {}`) tanpa melakukan request jaringan apapun.
   - **Tier 2 (Single-Account Probe - ~300ms Overhead)**:
     Jika cache akun aktif telah expired ($> 5$ menit) atau sisa kuota akun aktif berada pada rentang menengah ($15\% \le \text{quota} < 25\%$), hook hanya melakukan probe HTTP ke akun aktif via `fetch_account_quota`. Jika setelah di-probe kuota akun aktif terbukti masih $\ge 15\%$, turn diizinkan berlanjut tanpa perlu mem-probe akun standby lainnya.
   - **Tier 3 (Full Standby Probe & Auto-Rotation - Saat Kritis)**:
     Hanya jika kuota akun aktif terbukti kritis ($< 15\%$, sesuai threshold `cache_optimized` pada ADR 0001), hook melakukan probe konkuren ke seluruh akun standby untuk memilih dan merotasi ke akun cadangan terbaik via `RotationEngine`.

2. **Account-Level Cache Freshness & Otomasi `save_cache()`**
   - Menambahkan field `cached_at` (epoch timestamp) pada tingkat entri akun (`accounts[name]["cached_at"]`) di dalam `last_known_quota.json`. Hal ini memungkinkan penentuan TTL 5 menit secara independen untuk masing-masing akun, tanpa merusak skema hybrid ADR 0005.
   - Menyimpan cache secara otomatis via `client.save_cache(..., merge=True)` setiap kali terjadi probe jaringan (baik Tier 2 single probe maupun Tier 3 full probe). Pada Tier 1 (0ms pass), tidak ada operasi tulis disk yang dieksekusi sehingga terbebas dari disk I/O overhead.

3. **Isolasi Gangguan Jaringan & Eliminasi False Alarm**
   - Memisahkan kegagalan jaringan (offline, DNS error, timeout) dari kehabisan kuota riil.
   - Jika request jaringan gagal, sistem melakukan graceful fallback ke nilai kuota pada cache lokal `last_known_quota.json`.
   - Jika cache lokal juga tidak tersedia, turn agen tetap diizinkan berjalan menggunakan akun aktif saat ini secara diam-diam tanpa memicu false alarm.
   - Peringatan `⚠️ All Antigravity accounts have exhausted their quota` **HANYA** diizinkan muncul jika minimal satu akun berhasil di-probe dan seluruh akun yang terverifikasi menghasilkan sisa kuota 0%.

4. **Preservasi ISO 8601 Expiry Timestamp pada Token Refresh**
   - Mengubah `QuotaClient.refresh_access_token` untuk mengekstrak field `expires_in` dari respons JSON endpoint Google OAuth, mengembalikan struktur metadata `{"access_token": str, "expires_in": int}`.
   - Callback refresh menghitung ISO 8601 UTC timestamp baru: `(datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")`.
   - Menyesuaikan signature `engine.update_account_access_token(name: str, access_token: str, expiry: str | None = None)` agar field `expiry` diperbarui secara atomik bersama `access_token` di dalam `token.json` vault dan disinkronkan ke target Desktop dan CLI.

## Considered Options & Rejected Alternatives

- **Binary Cache-First (TTL Flat 2 Menit Tanpa Tiering)**: Ditolak karena mengabaikan sisa kapasitas kuota; akun dengan kuota 90% tetap dipaksa probe setiap 2 menit, sedangkan akun dengan kuota 5% bisa lolos tanpa rotasi jika berada di dalam jendela TTL.
- **Pure Reactive Polling (100% Pasrah ke Error 429)**: Ditolak karena membiarkan model gagal di tengah jalan (turn failure) dan mengandalkan daemon watcher untuk interupsi, yang menurunkan user experience dibandingkan rotasi preventif transparan di `PreInvocation`.
- **Global Filesystem mtime sebagai TTL Cache**: Ditolak karena saat single-account probe memperbarui berkas cache, mtime seluruh berkas berubah sehingga akun-akun standby yang tidak di-probe akan secara keliru dianggap baru saja diperbarui.
- **Strict Blocking saat Jaringan Offline**: Ditolak karena transient network issue pada endpoint telemetri CloudCode tidak boleh menghentikan agen jika Language Server lokal masih mampu berkomunikasi dengan backend model utama.

## Consequences

**Positif:**
- Memangkas latensi turn agen hingga 0ms pada lebih dari 85-90% eksekusi turn tipikal, secara drastis meningkatkan responsivitas interaksi agen.
- Mengeliminasi false alarm "all accounts exhausted" yang membingungkan pengguna saat terjadi fluktuasi koneksi internet.
- Memastikan integritas data vault terjaga dengan timestamp `expiry` token yang selalu mutakhir setelah refresh otomatis.
- Sinkronisasi telemetri kuota hook ke `last_known_quota.json` memastikan perintah CLI (`status` dan `quota`) selalu menyajikan data terkini tanpa perlu fetch berulang.

**Negatif/Risiko:**
- Membutuhkan penyesuaian unit test pada `tests/test_hook_lifecycle.py` yang sebelumnya memvalidasi kemunculan warning 'exhausted' saat response jaringan kosong.
- Logika percabangan di `handle_pre_invocation_hook` menjadi sedikit lebih kaya (3 tiering states), membutuhkan cakupan pengujian komprehensif untuk setiap skenario kuota.
