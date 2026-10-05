# 2. Arsitektur Fast Telemetry untuk CLI Commands

Date: 2026-10-05

## Status

Accepted

## Context

Perintah CLI `agy-switch` seperti `list --quota`, `status`, dan `switch --auto` memerlukan akses data kuota. Mengambil kuota secara berurutan atau memanggil CloudCode API di setiap perintah (terutama `status`) berpotensi lambat. Kita membutuhkan arsitektur CLI yang seketika (instant) dan cerdas.

## Decision

1. **`list --quota` (Concurrent Ping)**: Menggunakan `threading` bawaan Python (zero external dependencies) untuk mengambil kuota dari CloudCode API secara konkuren untuk semua akun di vault. Ini memastikan list ditampilkan dalam hitungan milidetik.
2. **`status` (Cache-Driven)**: Mengandalkan cache file `last_known_quota.json`. Jika usia cache $< 60$ detik, gunakan nilainya agar perintah `status` (yang sering dipakai untuk shell prompt) tetap *zero-lag*. Jika kedaluwarsa, lakukan *fast-probe*.
3. **`switch --auto` (Optimal Exit)**: Jika `RotationEngine` (dengan aturan `cache_optimized` atau `drain_window`) mendeteksi bahwa akun aktif saat ini sudah yang paling optimal, CLI akan mencetak informasi log dan exit sukses (`exit 0`) tanpa menyentuh file token vault atau melakukan switch.

## Consequences

**Positif:**
- Eksekusi `status` menjadi instan, ideal untuk integrasi ke terminal prompt.
- Waktu eksekusi `list --quota` dipangkas drastis (O(1) relative to account count).
- Terhindar dari overhead pertukaran token dan restart Language Server yang tidak perlu jika akun sudah dalam status optimal.

**Negatif/Risiko:**
- Implementasi thread konkuren membutuhkan error handling khusus (misal 1 API thread timeout tidak boleh crash command utama).
- Akurasi persentase `status` tertinggal maksimal 60 detik dari status nyata Google.
